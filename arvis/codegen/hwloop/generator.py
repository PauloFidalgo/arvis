#!/usr/bin/env python3
"""
HW Loop Code Generator

Generates hardware loop setup instructions for HW-loopable loops detected by loop.py.

Custom instruction encoding:
  - 31:27: COUNT register (5 bits)
  - 26:25: 00 (reserved)
  - 24:20: LOOP_END register (5 bits)
  - 19:15: LOOP_START register (5 bits)
  - 9:8:   offset encoding (size of last insn: 00=2B, 01=4B, 10=6B, 11=8B)
  - 7:     Loop ID (with HW_LOOP_BITS=1)
  - 6:0:   Opcode 0x7b

Loop ID assignment for nested loops:
  - Outermost loop = 0
  - Children get sequential IDs in DFS order
  - Max 8 nested loops (IDs 0-7)

Example nesting:
  outer:          ID=0
    first_loop:   ID=1
      child:      ID=2
    second_loop:  ID=3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from arvis.analysis.hwloop import AsmLoop, AsmLoopDetector


@dataclass
class HWLoopEncoding:
    """Opcode/funct3 for hwloop instructions (assigned dynamically after fused slots)."""

    bounds_opcode: int = 0x7B
    bounds_funct3: int = 0b010
    count_opcode: int = 0x7B
    count_funct3: int = 0b011
    start_opcode: int = 0x7B
    start_funct3: int = 0b100
    end_opcode: int = 0x7B
    end_funct3: int = 0b101


# Default encoding (CUSTOM_3, backward compatible)
DEFAULT_HWLOOP_ENCODING = HWLoopEncoding()


# RISC-V register name to number mapping
REG_MAP = {
    "zero": 0,
    "ra": 1,
    "sp": 2,
    "gp": 3,
    "tp": 4,
    "t0": 5,
    "t1": 6,
    "t2": 7,
    "s0": 8,
    "fp": 8,
    "s1": 9,
    "a0": 10,
    "a1": 11,
    "a2": 12,
    "a3": 13,
    "a4": 14,
    "a5": 15,
    "a6": 16,
    "a7": 17,
    "s2": 18,
    "s3": 19,
    "s4": 20,
    "s5": 21,
    "s6": 22,
    "s7": 23,
    "s8": 24,
    "s9": 25,
    "s10": 26,
    "s11": 27,
    "t3": 28,
    "t4": 29,
    "t5": 30,
    "t6": 31,
}
# Add x0-x31 aliases
for i in range(32):
    REG_MAP[f"x{i}"] = i


@dataclass
class RemovableInsns:
    """Instructions that can be removed when converting to HW loop."""

    back_branch: Optional[str] = None  # Always removable
    iv_step: Optional[str] = None  # The IV step instruction
    iv_step_removable: bool = False  # True if IV not used elsewhere
    iv_usage: str = "unknown"  # How IV is used: 'control_only', 'value', 'pointer', 'index', 'alu'
    total_bytes_saved: int = 0  # Bytes saved per iteration
    potential_with_autoincr: int = 0  # Additional bytes if HW has auto-increment


@dataclass
class HWLoopConfig:
    """Configuration for a single HW loop."""

    loop: AsmLoop
    loop_id: int

    # Registers for the custom instruction
    count_reg: str  # Register holding iteration count
    loop_start_reg: str  # Register holding loop start address
    loop_end_reg: str  # Register holding loop end address

    # Whether we need to compute/load these values
    count_needs_calc: bool = False
    count_calc_insns: List[str] = field(default_factory=list)

    # Offset encoding for last instruction
    offset_encoding: int = 0  # 00=4B, 01=6B, 10=8B
    _id_mask: int = 0x1

    # Register spill/restore for setup
    spill_save: List[str] = field(default_factory=list)
    spill_restore: List[str] = field(default_factory=list)

    # Removable instructions analysis
    removable: RemovableInsns = field(default_factory=RemovableInsns)


@dataclass
class HWLoopGroup:
    """A group of nested loops that can be configured together."""

    root: AsmLoop
    configs: List[HWLoopConfig] = field(default_factory=list)


class HWLoopGenerator:
    """Generates HW loop setup code for detected loops."""

    def __init__(
        self,
        detector: AsmLoopDetector,
        hw_loop: int = 2,
        exclude_fns: Optional[Set[str]] = None,
        only_fns: Optional[Set[str]] = None,
        encoding: Optional[HWLoopEncoding] = None,
    ):
        self.detector = detector
        self.groups: List[HWLoopGroup] = []
        self.hw_loop = hw_loop
        self.exclude_fns = exclude_fns or set()
        self.only_fns = only_fns
        self.hw_loop_bits = max(1, (hw_loop - 1).bit_length())
        self.id_mask = (1 << self.hw_loop_bits) - 1
        self.enc = encoding or DEFAULT_HWLOOP_ENCODING

        # Temp register pool for address calculations
        self.temp_regs = ["t0", "t1", "t2", "t3", "t4", "t5", "t6"]

    def generate(self) -> List[HWLoopGroup]:
        """Generate HW loop configurations for all eligible loops."""
        self.groups = []

        # Find all HW-eligible root loops (no HW-eligible parent)
        for loop in self.detector.all_loops:
            if not loop.hw_eligible:
                continue
            if loop.function in self.exclude_fns:
                continue
            if self.only_fns is not None and loop.function not in self.only_fns:
                continue

            # Check if this is a root of a nestable group
            if loop.parent and loop.parent.hw_eligible:
                continue  # Will be handled as part of parent's group

            group = self._build_group(loop)
            if group:
                self.groups.append(group)

        return self.groups

    def _build_group(self, root: AsmLoop) -> Optional[HWLoopGroup]:
        """Build a HW loop group starting from root, selecting loops with
        highest benefit when more loops exist than HW_LOOP registers."""
        group = HWLoopGroup(root=root)

        # Collect all HW-eligible loops in this tree (DFS order = outermost first)
        all_eligible = self._collect_hw_eligible_dfs(root)

        if len(all_eligible) <= self.hw_loop:
            loops_to_config = all_eligible
        else:
            # Score-based selection: always include the root, fill remaining
            # slots with highest-benefit candidates.
            scored = [(self._estimate_benefit(l), l) for l in all_eligible]
            selected = {id(root)}
            remaining_slots = self.hw_loop - 1
            candidates = [(s, l) for s, l in scored if id(l) != id(root)]
            candidates.sort(key=lambda x: -x[0])
            for score, loop in candidates:
                if remaining_slots <= 0:
                    break
                if score > 0:
                    selected.add(id(loop))
                    remaining_slots -= 1
            for loop in all_eligible:
                if remaining_slots <= 0:
                    break
                if id(loop) not in selected:
                    selected.add(id(loop))
                    remaining_slots -= 1
            loops_to_config = [l for l in all_eligible if id(l) in selected]

        # Assign loop IDs and create configs
        for loop_id, loop in enumerate(loops_to_config):
            cfg = self._create_config(loop, loop_id)
            if cfg is None:
                return None
            group.configs.append(cfg)

        return group

    def _estimate_benefit(self, loop: AsmLoop) -> int:
        """Estimate net cycle benefit of HW-looping this loop.

        Benefit = iterations × branch_saved - setup_cost × executions_of_setup
        """
        iters = self._estimate_iterations(loop)
        if iters is None:
            iters = 8  # conservative default

        # Branch removal saves 1 cycle per iteration
        branch_saved_per_iter = 1

        # Setup cost depends on whether count is constant
        is_const = self._is_constant_count(loop)
        # Count calc: 1 insn for constant, 2-4 for dynamic
        count_calc_cost = 1 if is_const else 3
        # hwloop.count instruction: 1 cycle
        # hwloop.bounds (for root) or hwloop.start/end: ~2 cycles (executed once)
        hwloop_insn_cost = 1
        setup_per_exec = count_calc_cost + hwloop_insn_cost

        # How many times does the setup execute?
        # For the root loop: once
        # For inner loops: once per parent iteration
        parent_iters = 1
        p = loop.parent
        while p and p.hw_eligible:
            pi = self._estimate_iterations(p)
            parent_iters *= pi if pi else 4
            p = p.parent

        total_saved = iters * branch_saved_per_iter * parent_iters
        total_cost = setup_per_exec * parent_iters

        # Bounds setup cost (only for root, executed once)
        if loop.parent is None or not loop.parent.hw_eligible:
            total_cost += 6  # bounds instructions + alignment

        return total_saved - total_cost

    def _estimate_iterations(self, loop: AsmLoop) -> Optional[int]:
        """Try to estimate the iteration count as a constant."""
        det = loop.constraint_results.get("deterministic_iterations", {})
        iv_init = det.get("iv_init", {})
        bound = det.get("bound", "")
        step = det.get("step_value")

        if not step:
            return None
        try:
            step_int = int(step)
        except (ValueError, TypeError):
            return None

        # Try to resolve bound to constant
        resolved = self._resolve_bound_to_const(loop, bound) if bound and bound != "zero" else bound

        if resolved and resolved.startswith("imm:"):
            bound_val = int(resolved[4:])
        elif bound == "zero":
            bound_val = 0
        else:
            return None

        init_type = iv_init.get("type", "")
        if init_type == "zero":
            init_val = 0
        elif init_type == "immediate":
            init_val = int(iv_init.get("value", 0))
        else:
            # Check constant-diff pattern
            init_insn = iv_init.get("insn", "")
            if "addi" in init_insn and bound and not resolved.startswith("imm:"):
                parts = init_insn.replace(",", " ").replace("\t", " ").split()
                if len(parts) >= 4:
                    try:
                        if parts[2] == bound:
                            return abs(int(parts[3])) // abs(step_int)
                    except (ValueError, IndexError):
                        pass
            return None

        if step_int == 0:
            return None
        return abs(bound_val - init_val) // abs(step_int)

    def _is_constant_count(self, loop: AsmLoop) -> bool:
        """Check if this loop's iteration count is a compile-time constant."""
        return self._estimate_iterations(loop) is not None

    def _resolve_bound_to_const(self, loop: AsmLoop, bound: str) -> str:
        """Try to resolve *bound* to a compile-time constant.

        Two paths:
        1. Descendant-IV chain: bound is an inner loop's IV whose final
           value equals the inner loop's bound.
        2. Direct trace: bound register is set by a constant `li` before
           the loop and not modified inside the loop body."""
        reg = bound
        visited = set()
        resolved_any = False
        while reg and reg not in visited:
            visited.add(reg)
            desc_bound = self.detector._resolve_descendant_iv_bound(loop, reg)
            if desc_bound is None:
                break
            reg = desc_bound
            resolved_any = True
        if resolved_any:
            val = self._trace_reg_to_const(loop, reg)
            if val is not None:
                return f"imm:{val}"
            return bound
        # Direct trace: bound register is set by a constant `li` before
        # the loop and not modified inside the loop body.
        val = self._trace_reg_to_const(loop, bound)
        if val is not None:
            return f"imm:{val}"
        return bound

    def _trace_reg_to_const(self, loop: AsmLoop, reg: str) -> Optional[int]:
        """Trace *reg* backwards through pre-loop instructions to a constant."""
        func = None
        for f in self.detector.functions:
            if f.name == loop.function:
                func = f
                break
        if not func:
            return None
        # Walk instructions before the loop, last definition wins
        last_def = None
        for insn in func.instructions:
            if insn.line_num >= loop.start_line:
                break
            if insn.rd == reg:
                last_def = insn
        if last_def is None:
            return None
        if last_def.mnemonic in ("li", "c.li") and last_def.imm is not None:
            try:
                return int(last_def.imm)
            except (ValueError, TypeError):
                return None
        return None

    def _collect_hw_eligible_dfs(self, loop: AsmLoop) -> List[AsmLoop]:
        """Collect HW-eligible loops in DFS order (for loop ID assignment)."""
        result = [loop]
        for child in sorted(loop.children, key=lambda l: l.start_line):
            if child.hw_eligible:
                result.extend(self._collect_hw_eligible_dfs(child))
        return result

    def _create_config(self, loop: AsmLoop, loop_id: int) -> HWLoopConfig:
        """Create HW loop configuration for a single loop."""
        det = loop.constraint_results.get("deterministic_iterations", {})

        iv = det.get("iv")
        bound = det.get("bound")
        step_value = det.get("step_value")

        # Collect ALL registers used by this loop, nested children, AND ancestor loops
        used_regs = self._collect_used_regs(loop)
        # Also exclude registers that are LIVE ACROSS this loop in ancestor loops.
        # A reg is unsafe if: (a) read/written by ancestor BEFORE this loop, or
        # (b) read by ancestor AFTER this loop. Either way, our setup would clobber it.
        parent = loop.parent
        while parent:
            for insn in parent.body_instructions:
                # Skip instructions that are INSIDE this loop's range
                if loop.start_line <= insn.line_num <= loop.back_branch_line:
                    continue
                if insn.rd:
                    used_regs.add(insn.rd)
                if insn.rs1:
                    used_regs.add(insn.rs1)
                if insn.rs2:
                    used_regs.add(insn.rs2)
            parent = parent.parent

        # Scan the function for registers used AFTER the loop (until function end).
        # This catches registers that are live across the loop but not in any parent.
        import re

        lines = open(str(self.detector.asm_path)).readlines()
        func_end = len(lines)
        for i in range(loop.back_branch_line + 1, len(lines)):
            if re.match(r"\s*\.size\s", lines[i]):
                func_end = i
                break
        for i in range(loop.back_branch_line + 1, func_end):
            line = lines[i].strip()
            if not line or line.startswith(".") or line.startswith("#"):
                continue
            for reg in re.findall(r"\b([ast]\d+|zero|ra|sp|gp|tp)\b", line):
                used_regs.add(reg)

        # Find free temp registers not used by the loop body
        free_temps = [r for r in self.temp_regs if r not in used_regs]

        # Only use callee-saved regs that the function ALREADY saves in its prologue.
        # Using unsaved s-regs would corrupt the caller's state.
        saved_in_prologue = self._find_saved_regs(loop)
        free_saved = [r for r in saved_in_prologue if r not in used_regs]
        free_all = free_temps + free_saved

        # The first instruction of the loop body overwrites its rd.
        # That register is dead at the setup point — safe to use as count_reg
        # without spilling, since the loop body will overwrite it immediately.
        if loop.body_instructions and loop.body_instructions[0].rd:
            first = loop.body_instructions[0]
            first_rd = first.rd
            # Only safe if first insn doesn't also READ this register
            if first_rd not in (first.rs1, first.rs2):
                if first_rd not in free_all and first_rd not in ("zero", "sp", "gp", "tp", "ra"):
                    if first_rd != iv and first_rd != bound:
                        free_all.insert(0, first_rd)  # prefer this — no spill needed

        abs_step = abs(int(step_value)) if step_value and step_value.lstrip("-").isdigit() else 1
        need_div = abs_step > 1 and (abs_step & (abs_step - 1)) != 0

        if len(free_all) < 1 or (len(free_all) < 2 and need_div):
            # Not enough free registers — spill t-regs
            # Prefer regs NOT used in loop body (spill is outside hot path)
            body_regs = self._collect_used_regs(loop)
            spill_candidates = [r for r in self.temp_regs if r not in body_regs and r not in free_all]
            if not spill_candidates:
                spill_candidates = [r for r in self.temp_regs if r in used_regs and r not in free_all]
            # Need 2 regs if step requires division (not power of 2)
            need = (2 if need_div else 1) - len(free_all)
            if len(spill_candidates) < need:
                return None
            spill_regs = spill_candidates[:need]
            free_all = free_all + spill_regs
            max_sp_off = self._find_max_sp_offset(loop)
            frame_size = self._find_frame_size(loop)
            spill_base = max_sp_off + 4
            spill_end = spill_base + len(spill_regs) * 4
            if frame_size > 0 and spill_end > frame_size:
                # No room in existing frame — grow stack temporarily.
                # We allocate just enough for the spill, write at offset 0,
                # then restore SP afterwards.
                grow_bytes = ((len(spill_regs) * 4 + 15) // 16) * 16  # 16-byte aligned
                spill_save = [f"    addi sp, sp, -{grow_bytes}  # grow stack for spill"]
                spill_restore = []
                for j, reg in enumerate(spill_regs):
                    offset = j * 4
                    spill_save.append(f"    sw {reg}, {offset}(sp)  # spill {reg}")
                    spill_restore.append(f"    lw {reg}, {offset}(sp)  # restore {reg}")
                spill_restore.append(f"    addi sp, sp, {grow_bytes}  # shrink stack")
            else:
                spill_save = []
                spill_restore = []
                for j, reg in enumerate(spill_regs):
                    offset = spill_base + j * 4
                    spill_save.append(f"    sw {reg}, {offset}(sp)  # spill {reg}")
                    spill_restore.append(f"    lw {reg}, {offset}(sp)  # restore {reg}")
        else:
            spill_save = []
            spill_restore = []

        # Resolve descendant-IV bounds to constant values.
        # GCC -O2 reuses inner loop IVs as outer loop bounds. The final
        # value of a descendant's IV equals the descendant's bound.
        # Follow the chain until we reach a pre-computable constant.
        resolved_bound = bound
        if bound and bound != "zero":
            resolved_bound = self._resolve_bound_to_const(loop, bound)

        # Only need count_reg (start/end are PC-relative in the bounds instruction)
        result = self._determine_count(loop, iv, resolved_bound, step_value, free_all)
        if result[0] is None:
            # Fallback: try symbolic analysis for constant count
            sym_count = self.detector.symbolic_count(loop)
            if sym_count and sym_count.is_const() and loop.nesting_depth == 0:
                val = sym_count.const_val()
                if val > 0:
                    fallback_reg = free_all[0] if free_all else "t2"
                    result = (
                        fallback_reg,
                        [f"    li {fallback_reg}, {val}  # symbolic: constant count"],
                    )
            if result[0] is None:
                return None
        count_reg, calc_insns = result

        # Determine offset encoding based on last body instruction size.
        # ARVIS Phase 4 (RTL pnult): the RTL pre-computes LP_last_addr =
        # LP_end - (last_is_4byte ? 4 : 2) at hwloop setup. Bit
        # instr[7+hw_loop_bits] of the lp.count instruction tells the RTL
        # whether the last body instruction is 4-byte (bit=1) or 2-byte
        # compressed (bit=0). Setting this correctly is mandatory: an
        # incorrect bit causes DEC to fire one instruction off, breaking
        # the loop.
        #
        # Subtlety: the .s file uses uncompressed mnemonics like `addi`/`add`
        # even when the assembler would later emit them as RV32C 2-byte
        # forms. AsmInstruction.size_bytes only sees the mnemonic, so it
        # cannot tell which `addi` will compress. To make the bit
        # deterministic, the patcher wraps the last body instruction in
        # `.option push; .option norvc; ...; .option pop` (see
        # _build_insertion_map), forcing it to 4 bytes regardless of
        # operands. Therefore we always set offset_enc = 1 here.
        offset_enc = 1

        return HWLoopConfig(
            loop=loop,
            loop_id=loop_id,
            count_reg=count_reg,
            loop_start_reg="",  # not used in new format
            loop_end_reg="",  # not used in new format
            count_needs_calc=len(calc_insns) > 0,
            count_calc_insns=calc_insns,
            offset_encoding=offset_enc,
            removable=self._analyze_removable(loop),
            spill_save=spill_save,
            spill_restore=spill_restore,
            _id_mask=self.id_mask,
        )

    def _find_max_sp_offset(self, loop: AsmLoop) -> int:
        """Find the maximum sp-relative offset used anywhere in the function."""
        import re

        lines = open(str(self.detector.asm_path)).readlines()
        func_label = loop.function + ":"
        in_func = False
        max_off = -4
        for i, line in enumerate(lines):
            if line.strip() == func_label:
                in_func = True
                continue
            if in_func and re.match(r"\s*\.size\s", line):
                break
            if in_func:
                m = re.search(r"(\d+)\(sp\)", line)
                if m:
                    max_off = max(max_off, int(m.group(1)))
        return max_off

    def _find_frame_size(self, loop: AsmLoop) -> int:
        """Find the stack frame size from the function prologue."""
        import re

        lines = open(str(self.detector.asm_path)).readlines()
        func_label = loop.function + ":"
        in_func = False
        for i, line in enumerate(lines):
            if line.strip() == func_label:
                in_func = True
                continue
            if in_func:
                m = re.match(r"\s*addi\s+sp\s*,\s*sp\s*,\s*(-\d+)", line)
                if m:
                    return abs(int(m.group(1)))
                if i > loop.start_line:
                    break
        return 0

    def _find_saved_regs(self, loop: AsmLoop) -> list:
        """Find callee-saved registers (s0-s11) that the function saves in its prologue.
        Only these are safe to use as temps without corrupting the caller."""
        import re

        saved = []
        # Scan raw assembly lines from function start to loop start
        lines = open(str(self.detector.asm_path)).readlines()
        func_label = loop.function + ":"
        in_func = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == func_label:
                in_func = True
                continue
            if i >= loop.start_line:
                break
            if not in_func:
                continue
            # Match: sw sN, offset(sp) or c.swsp
            m = re.match(r"\s*(?:c\.)?sw\s+(s\d+)\s*,.*\(sp\)", stripped)
            if m:
                reg = m.group(1)
                if reg not in saved:
                    saved.append(reg)
        return saved

    def _collect_used_regs(self, loop: AsmLoop) -> set:
        """Collect all registers read or written by a loop body (including back-edge)."""
        used = set()
        for insn in loop.body_instructions:
            if insn.rd:
                used.add(insn.rd)
            if insn.rs1:
                used.add(insn.rs1)
            if insn.rs2:
                used.add(insn.rs2)
        for child in loop.children:
            used |= self._collect_used_regs(child)
        return used

    def _collect_written_regs(self, loop: AsmLoop) -> set:
        """Collect registers WRITTEN by the loop body (excluding back-edge)."""
        written = set()
        for insn in loop.body_instructions:
            if insn.line_num == loop.back_branch_line:
                continue
            if insn.rd:
                written.add(insn.rd)
        for child in loop.children:
            written |= self._collect_written_regs(child)
        return written

    def _analyze_removable(self, loop: AsmLoop) -> RemovableInsns:
        """Analyze which instructions can be removed when using HW loop.

        Always removable:
          - Back-edge branch (bne, beq, etc.) - HW handles iteration

        Conditionally removable:
          - IV step instruction (addi iv, iv, X) - only if IV is not used
            for anything other than loop control (comparison + step)

        IV usage patterns:
          - control_only: IV only for loop control → step removable
          - pointer: IV is pointer incremented by sizeof → needs auto-incr HW
          - index: IV used in address calculation → not removable
          - value: IV value stored to memory → not removable
          - alu: IV used in ALU computation → not removable
        """
        result = RemovableInsns()
        det = loop.constraint_results.get("deterministic_iterations", {})
        iv = det.get("iv")
        step_value = det.get("step_value")

        # Back-edge is always removable
        back_insn = loop.body_instructions[-1] if loop.body_instructions else None
        if back_insn and back_insn.line_num == loop.back_branch_line:
            result.back_branch = back_insn.raw_line.strip()
            result.total_bytes_saved += 2 if back_insn.mnemonic.startswith("c.") else 4

        if not iv:
            return result

        # Find the IV step instruction
        iv_step_insn = None
        for insn in loop.body_instructions:
            if insn.rd == iv and insn.mnemonic in (
                "addi",
                "c.addi",
                "addiw",
                "add",
                "c.add",
                "sub",
                "c.sub",
            ):
                iv_step_insn = insn
                break

        if not iv_step_insn:
            return result

        result.iv_step = iv_step_insn.raw_line.strip()
        iv_step_bytes = 2 if iv_step_insn.mnemonic.startswith("c.") else 4
        iv_step_line = iv_step_insn.line_num

        # Analyze how IV is used in the loop body
        iv_stored = False  # IV value written to memory
        iv_in_load_addr = False  # IV used as base in load
        iv_in_store_addr = False  # IV used as base in store
        iv_in_alu = False  # IV used in ALU ops

        for insn in loop.body_instructions:
            # Skip the step instruction and back-edge
            if insn.line_num == iv_step_line:
                continue
            if insn.line_num == loop.back_branch_line:
                continue

            # If IV is written by any OTHER instruction, it's used as a temp
            # register — the step is NOT removable
            if insn.rd == iv:
                iv_in_alu = True

            # Check if IV value is stored to memory (sw iv, ...)
            if insn.mnemonic in ("sw", "sh", "sb", "sd", "c.sw", "c.sd") and insn.rs2 == iv:
                iv_stored = True
            # Check if IV used as base address in load
            elif insn.mnemonic in ("lw", "lh", "lb", "ld", "lhu", "lbu", "c.lw", "c.ld") and insn.rs1 == iv:
                iv_in_load_addr = True
            # Check if IV used as base address in store
            elif insn.mnemonic in ("sw", "sh", "sb", "sd", "c.sw", "c.sd") and insn.rs1 == iv:
                iv_in_store_addr = True
            # Check if IV used in ALU
            elif insn.rs1 == iv or insn.rs2 == iv:
                iv_in_alu = True

        # Determine IV usage pattern
        is_pointer_step = step_value in ("1", "-1", "2", "-2", "4", "-4", "8", "-8")

        if not (iv_stored or iv_in_load_addr or iv_in_store_addr or iv_in_alu):
            result.iv_usage = "control_only"
            result.iv_step_removable = not self._is_iv_live_after_loop(loop, iv)
        elif iv_stored:
            result.iv_usage = "value"
            result.iv_step_removable = False
        elif (iv_in_load_addr or iv_in_store_addr) and is_pointer_step and not iv_in_alu:
            result.iv_usage = "pointer"
            result.iv_step_removable = False
            # With auto-increment load/store, we could remove the step
            result.potential_with_autoincr = iv_step_bytes
        elif iv_in_load_addr or iv_in_store_addr:
            result.iv_usage = "index"
            result.iv_step_removable = False
        else:
            result.iv_usage = "alu"
            result.iv_step_removable = False

        return result

    def _is_iv_live_after_loop(self, loop: AsmLoop, iv: str) -> bool:
        """Check if the IV register is read before being written after the loop.
        Returns True if IV is live (read before overwritten), False if dead."""
        import re as _re

        lines = self.detector.lines

        def _find_label(label):
            target = label + ":"
            for j in range(len(lines)):
                if lines[j].strip() == target:
                    return j
            return None

        def _scan_from(start, depth=0):
            if depth > 3:
                return True  # too many jumps, conservatively live
            for i in range(start + 1, min(start + 31, len(lines))):
                line = lines[i].strip()
                if not line or line.startswith("#") or line.startswith("//"):
                    continue
                if line.startswith(".") and line.endswith(":"):
                    continue
                parts = _re.split(r"[,\s\t()]+", line)
                parts = [p for p in parts if p and not p.startswith("#")]
                if not parts:
                    continue
                mnem = parts[0]
                if mnem.startswith("."):
                    continue
                is_store = mnem in ("sw", "sh", "sb", "sd", "c.sw", "c.sd", "c.sh", "c.sb")
                is_branch = mnem in (
                    "beq",
                    "bne",
                    "blt",
                    "bge",
                    "bltu",
                    "bgeu",
                    "bgtu",
                    "bgt",
                    "ble",
                    "bleu",
                    "bnez",
                    "beqz",
                    "jal",
                    "jalr",
                    "call",
                )
                # Function return — IV is dead
                if mnem in ("ret", "c.jr") or (mnem == "jr" and len(parts) > 1 and parts[1] == "ra"):
                    return False
                # Unconditional jump — follow the target
                if mnem in ("j", "c.j", "tail"):
                    if len(parts) > 1:
                        tgt = _find_label(parts[1])
                        if tgt is not None:
                            return _scan_from(tgt, depth + 1)
                    return True  # can't resolve target, conservatively live
                if mnem == "jr":
                    return True
                if is_branch:
                    for p in parts[1:]:
                        if p == iv:
                            return True
                    continue
                if is_store:
                    for p in parts[1:]:
                        if p == iv:
                            return True
                    continue
                if len(parts) < 2:
                    continue
                rd = parts[1]
                srcs = parts[2:]
                for s in srcs:
                    if s == iv:
                        return True
                if rd == iv:
                    return False
            return True  # end of scan, conservatively live

        return _scan_from(loop.back_branch_line)

    def _determine_count(
        self, loop: AsmLoop, iv: str, bound: str, step_value: str, free_regs: list
    ) -> Tuple[str, List[str]]:
        """Determine the count register and any calculation instructions needed.

        General formula: COUNT = |bound - iv_init| / |step|

        Cases:
        1. init=0, step=1:       COUNT = bound             (direct_bound)
        2. init=0, step=2^n:     COUNT = bound >> n        (shift_only)
        3. init=0, step=k:       COUNT = bound / k         (div_only)
        4. init=X, step=1:       COUNT = bound - X         (sub_only)
        5. init=X, step=k:       COUNT = (bound - X) / k   (sub_div)
        6. bound=0, init=imm:    COUNT = imm / |step|      (countdown_imm)
        7. bound=0, init=reg:    COUNT = reg / |step|      (countdown_reg) *NEW*
        8. bound=X, step<0:      COUNT = (init - X) / |step|
        9. ptr diff, step=sizeof: COUNT = (end-start)/size (pointer_diff) *NEW*

        Returns (count_reg, list_of_setup_instructions)
        """
        calc_insns = []

        det = loop.constraint_results.get("deterministic_iterations", {})
        iv_init = det.get("iv_init", {})

        init_type = iv_init.get("type", "unknown")
        init_value = iv_init.get("value")

        # Parse step as integer
        try:
            step_int = int(step_value)
        except (ValueError, TypeError):
            step_int = None

        count_reg = free_regs[0] if len(free_regs) > 0 else "t2"
        # Ensure count_reg doesn't collide with bound or iv
        if count_reg == bound or count_reg == iv:
            # Try next free reg
            for r in free_regs[1:]:
                if r != bound and r != iv:
                    count_reg = r
                    break
            else:
                return (None, [])  # Can't find a safe count_reg
        # Extra temp for division
        extra_tmp = None
        for r in free_regs:
            if r != count_reg:
                extra_tmp = r
                break

        def need_extra_tmp():
            """Check if we'll need extra_tmp. If not available, return None to skip loop."""
            if extra_tmp is None:
                return None  # Signal: skip this loop
            return extra_tmp

        # Collect regs used in loop body to detect when we can return a
        # register directly vs needing to copy it to a free temp
        self._collect_used_regs(loop)
        written_regs = self._collect_written_regs(loop)

        def safe_return(reg, insns):
            """If reg is written by loop body, copy to count_reg first.
            Read-only usage is fine — the count is consumed by the setup insn
            before the loop body modifies anything."""
            if reg in written_regs and reg != count_reg:
                insns.append(f"    mv {count_reg}, {reg}  # copy count to safe reg")
                return (count_reg, insns)
            return (reg, insns)

        # === RESOLVED CONSTANT BOUND (from descendant-IV chain) ===
        if bound and bound.startswith("imm:"):
            bound_val = int(bound[4:])
            init_val = 0
            if init_type == "immediate":
                init_val = int(init_value)
            elif init_type == "zero":
                init_val = 0
            elif init_type == "register":
                # Check if init = bound + constant_offset (e.g., addi a5, a2, -96)
                # In that case, count = |offset| / step — a compile-time constant.
                init_insn = iv_init.get("insn", "")
                init_reg = init_value  # register name
                offset_const = None
                if "addi" in init_insn:
                    # Parse: addi init_reg, src_reg, imm
                    parts = init_insn.replace(",", " ").split()
                    if len(parts) >= 4:
                        src_reg = parts[2]
                        try:
                            imm = int(parts[3])
                            if src_reg == bound or (bound.startswith("imm:") and False):
                                offset_const = -imm  # count = |bound - init| / step = |imm| / step
                        except ValueError:
                            pass
                # Also check non-imm bound case: if bound is a register and
                # init = addi X, bound_reg, offset → diff = -offset
                if offset_const is None and not bound.startswith("imm:"):
                    if "addi" in init_insn:
                        parts = init_insn.replace(",", " ").split()
                        if len(parts) >= 4:
                            src_reg = parts[2]
                            try:
                                imm = int(parts[3])
                                if src_reg == bound:
                                    offset_const = -imm
                            except ValueError:
                                pass

                if offset_const is not None and step_int and offset_const != 0:
                    count = abs(offset_const) // abs(step_int)
                    if count > 0:
                        calc_insns.append(f"    li {count_reg}, {count}  # resolved constant count")
                        return (count_reg, calc_insns)

                # Fallback: emit runtime calculation
                abs_step = abs(step_int) if step_int else 1
                calc_insns.append(f"    li {count_reg}, {bound_val}")
                calc_insns.append(f"    sub {count_reg}, {count_reg}, {init_reg}")
                if abs_step > 1:
                    shift = (abs_step).bit_length() - 1
                    if (1 << shift) == abs_step:
                        calc_insns.append(f"    srli {count_reg}, {count_reg}, {shift}")
                    else:
                        et = need_extra_tmp()
                        if et is None:
                            return (None, [])
                        calc_insns.append(f"    li {et}, {abs_step}")
                        calc_insns.append(f"    divu {count_reg}, {count_reg}, {et}")
                calc_insns[-1] += "  # resolved bound"
                return (count_reg, calc_insns)
            else:
                # Unknown init type — fall through to normal handling
                # Strip the imm: prefix so downstream doesn't see it
                bound = bound  # will fail gracefully
            if init_type in ("zero", "immediate"):
                count = abs(bound_val - init_val) // abs(step_int) if step_int else 0
                if count > 0:
                    # If count equals bound_val, the original bound register
                    # already holds the right value — reuse it instead of li
                    orig_bound = loop.constraint_results.get("deterministic_iterations", {}).get("bound", "")
                    if count == bound_val and orig_bound and not orig_bound.startswith("imm"):
                        return safe_return(orig_bound, calc_insns)
                    calc_insns.append(f"    li {count_reg}, {count}  # resolved constant count")
                    return (count_reg, calc_insns)

        # === COUNTING DOWN TO ZERO ===
        if bound == "zero":
            if init_type == "zero":
                # Degenerate: 0 iterations
                calc_insns.append(f"    li {count_reg}, 0  # zero iterations")
                return (count_reg, calc_insns)

            abs_step = abs(step_int) if step_int else 1

            if init_type == "immediate":
                # countdown_imm: COUNT = |init| / |step|
                count_val = abs(int(init_value)) // abs_step if abs_step else abs(int(init_value))
                # The IV register already holds this value (from li iv, N before the loop)
                # Reuse it if the only write to IV in the body is the IV step (which gets removed)
                iv_writes = [
                    insn for insn in loop.body_instructions if insn.rd == iv and insn.line_num != loop.back_branch_line
                ]
                det.get("step", "")
                iv_only_step_write = all("addi" in insn.raw_line and iv in insn.raw_line for insn in iv_writes)
                if abs_step == 1 and iv_only_step_write:
                    return (iv, calc_insns)
                calc_insns.append(f"    li {count_reg}, {count_val}  # count = |{init_value}| / {abs_step}")
                return (count_reg, calc_insns)

            # countdown_reg: init is in a register, COUNT = reg / |step|
            init_reg = init_value if init_value else iv
            if abs_step == 1:
                # COUNT = init_reg directly (no calculation needed!)
                return safe_return(init_reg, calc_insns)
            elif abs_step > 0 and (abs_step & (abs_step - 1)) == 0:
                shift = abs_step.bit_length() - 1
                calc_insns.append(f"    srli {count_reg}, {init_reg}, {shift}  # count = {init_reg} >> {shift}")
            else:
                tmp = need_extra_tmp()
                if tmp is None:
                    return (None, [])
                calc_insns.append(f"    li {tmp}, {abs_step}")
                calc_insns.append(f"    divu {count_reg}, {init_reg}, {tmp}  # count = {init_reg} / {abs_step}")
            return (count_reg, calc_insns)

        # === CONSTANT DIFF: init = bound + offset (e.g., addi a5, a2, -96) ===
        # When the IV is initialized as bound_reg + constant, the iteration
        # count is |constant| / |step| — always constant regardless of the
        # bound's value, even if the IV is re-initialized each outer iteration.
        # IMPORTANT: only valid if bound is NOT modified between the init
        # instruction and the loop start.
        if init_type == "register" and bound and bound not in ("zero", None) and step_int:
            init_insn_str = iv_init.get("insn", "")
            if "addi" in init_insn_str:
                parts = init_insn_str.replace(",", " ").replace("\t", " ").split()
                if len(parts) >= 4:
                    try:
                        src_reg = parts[2]
                        imm = int(parts[3])
                        if src_reg == bound and imm != 0:
                            # Find the init instruction line
                            init_line = None
                            func = self.detector._func_for_loop(loop)
                            if func:
                                for fi in func.instructions:
                                    if fi.raw_line.strip().replace("\t", " ") == init_insn_str.replace("\t", " "):
                                        init_line = fi.line_num
                                        break
                            # Check if bound is modified between init and loop start
                            bound_modified = False
                            if init_line is not None:
                                func = self.detector._func_for_loop(loop)
                                if func:
                                    for fi in func.instructions:
                                        if fi.rd == bound and init_line < fi.line_num < loop.start_line:
                                            bound_modified = True
                                            break
                            if not bound_modified:
                                count = abs(imm) // abs(step_int)
                                if count > 0:
                                    calc_insns.append(f"    li {count_reg}, {count}  # resolved constant count")
                                    return (count_reg, calc_insns)
                    except (ValueError, IndexError):
                        pass

        # === COUNTING UP (step > 0) or DOWN to non-zero bound ===
        abs_step = abs(step_int) if step_int else 1

        # Check for pointer_diff pattern: init=reg, bound=reg, step=sizeof
        # This is common for: for(p=start; p<end; p++) where step=sizeof(*p)

        # Case: init = 0 (only safe for top-level loops; nested loops may
        # re-initialize the IV each outer iteration)
        if init_type == "zero" and not loop.parent:
            if abs_step == 1:
                return safe_return(bound, calc_insns)  # COUNT = bound directly
            elif abs_step > 0 and (abs_step & (abs_step - 1)) == 0:
                shift = abs_step.bit_length() - 1
                calc_insns.append(f"    srli {count_reg}, {bound}, {shift}  # count = {bound} / {abs_step}")
            else:
                tmp = need_extra_tmp()
                if tmp is None:
                    return (None, [])
                calc_insns.append(f"    li {tmp}, {abs_step}")
                calc_insns.append(f"    divu {count_reg}, {bound}, {tmp}  # count = {bound} / {abs_step}")
            return (count_reg, calc_insns)

        # Case: init = immediate constant
        if init_type == "immediate" and isinstance(init_value, int):
            if init_value == 0:
                # Same as init=0 case
                if abs_step == 1:
                    return safe_return(bound, calc_insns)
                elif abs_step > 0 and (abs_step & (abs_step - 1)) == 0:
                    shift = abs_step.bit_length() - 1
                    calc_insns.append(f"    srli {count_reg}, {bound}, {shift}  # count = {bound} / {abs_step}")
                else:
                    tmp = need_extra_tmp()
                if tmp is None:
                    return (None, [])
                calc_insns.append(f"    li {tmp}, {abs_step}")
                calc_insns.append(f"    divu {count_reg}, {bound}, {tmp}  # count = {bound} / {abs_step}")
                return (count_reg, calc_insns)

            # init is non-zero constant:
            # countup (step>0):   COUNT = (bound - init) / step
            # countdown (step<0): COUNT = (init - bound) / |step|
            if step_int is not None and step_int < 0:
                diff = init_value  # init - bound, but bound is a register
                comment = f"{init_value} - {bound}"
            else:
                diff = -init_value  # bound - init as addi offset
                comment = f"{bound} - {init_value}"

            if abs_step == 1:
                if step_int is not None and step_int < 0:
                    # COUNT = init - bound: need sub, can't use addi with register bound
                    calc_insns.append(f"    li {count_reg}, {init_value}")
                    calc_insns.append(f"    sub {count_reg}, {count_reg}, {bound}  # count = {comment}")
                else:
                    calc_insns.append(f"    addi {count_reg}, {bound}, {diff}  # count = {comment}")
            else:
                if step_int is not None and step_int < 0:
                    calc_insns.append(f"    li {count_reg}, {init_value}")
                    calc_insns.append(f"    sub {count_reg}, {count_reg}, {bound}  # {comment}")
                else:
                    calc_insns.append(f"    addi {count_reg}, {bound}, {diff}  # {comment}")
                if abs_step > 0 and (abs_step & (abs_step - 1)) == 0:
                    shift = abs_step.bit_length() - 1
                    calc_insns.append(f"    srli {count_reg}, {count_reg}, {shift}  # / {abs_step}")
                else:
                    tmp = need_extra_tmp()
                if tmp is None:
                    return (None, [])
                calc_insns.append(f"    li {tmp}, {abs_step}")
                calc_insns.append(f"    divu {count_reg}, {count_reg}, {tmp}  # / {abs_step}")
            return (count_reg, calc_insns)

        # Case: init is in a register
        # IMPORTANT: Use the IV register itself, not the source register,
        # because the source may have been overwritten by the time the
        # hwloop setup runs. The IV holds the correct initial value at
        # the point where setup is inserted (right before the loop label).
        init_reg = iv  # Always use IV, not init_value

        # For countdown loops (step < 0): COUNT = (init - bound) / |step|
        # For countup loops (step > 0):   COUNT = (bound - init) / step
        if step_int is not None and step_int < 0:
            hi_reg, lo_reg = init_reg, bound
        else:
            hi_reg, lo_reg = bound, init_reg

        if abs_step == 1:
            # Optimization: if bound is set by 'li bound, N' and bound is only
            # used in the back-branch (dead after hwloop removes it), reuse bound
            # register directly as count — no extra instructions needed
            bound_const = self._try_resolve_bound_li(loop, bound)
            if bound_const is not None:
                if init_type == "zero" and (step_int is None or step_int > 0):
                    # count = bound_const, bound reg already holds it
                    # Use bound register directly — zero calc instructions
                    return (bound, calc_insns)
                elif step_int is not None and step_int < 0:
                    calc_insns.append(f"    li {count_reg}, {bound_const}  # count (inlined bound)")
                    if lo_reg != "zero" and lo_reg != bound:
                        calc_insns.append(f"    sub {count_reg}, {count_reg}, {lo_reg}")
                else:
                    if lo_reg == "zero" or init_type == "zero":
                        return (bound, calc_insns)
                    else:
                        calc_insns.append(f"    sub {count_reg}, {bound}, {lo_reg}  # count = {bound} - {lo_reg}")
            else:
                calc_insns.append(f"    sub {count_reg}, {hi_reg}, {lo_reg}  # count = {hi_reg} - {lo_reg}")
        else:
            calc_insns.append(f"    sub {count_reg}, {hi_reg}, {lo_reg}  # {hi_reg} - {lo_reg}")
            if abs_step > 0 and (abs_step & (abs_step - 1)) == 0:
                shift = abs_step.bit_length() - 1
                calc_insns.append(f"    srli {count_reg}, {count_reg}, {shift}  # / {abs_step} (sizeof)")
            else:
                tmp = need_extra_tmp()
                if tmp is None:
                    return (None, [])
                calc_insns.append(f"    li {tmp}, {abs_step}")
                calc_insns.append(f"    divu {count_reg}, {count_reg}, {tmp}  # / {abs_step}")
        return (count_reg, calc_insns)

    def _try_resolve_bound_li(self, loop: AsmLoop, bound: str) -> int | None:
        """If bound reg is set by 'li bound, N' before the loop and is only
        used in the back-branch, return the constant N."""
        if not bound or bound == "zero":
            return None
        import re

        # Check if bound is used in the loop body (excluding the back-branch)
        for insn in loop.body_instructions:
            if insn.raw_line.strip() == loop.back_branch_insn:
                continue  # skip back-branch — it gets removed
            if bound in (insn.rd or "", insn.rs1 or "", insn.rs2 or ""):
                return None  # bound is used in loop body — can't inline
        # Search backwards from loop start for 'li bound, N'
        lines = open(str(self.detector.asm_path)).readlines()
        for i in range(loop.start_line - 1, max(0, loop.start_line - 20), -1):
            if i < len(lines):
                line = lines[i].strip()
                m = re.match(rf"li\s+{re.escape(bound)}\s*,\s*(-?\d+)", line)
                if m:
                    return int(m.group(1))
                # Stop if bound is written by something else
                if bound in line.split(",")[0] and not line.startswith("#"):
                    return None
        return None

    @staticmethod
    def _estimate_insn_size(insn) -> int:
        """Estimate the binary size of an instruction (2 or 4 bytes).

        Must match what the assembler actually emits. Wrong estimates cause
        the HW loop to either never trigger or trigger early — both fatal.
        """
        if insn.mnemonic.startswith("c."):
            return 2

        # Compressed register set: x8-x15 (s0,s1,a0-a5)
        CREG = {
            "s0",
            "fp",
            "s1",
            "a0",
            "a1",
            "a2",
            "a3",
            "a4",
            "a5",
            "x8",
            "x9",
            "x10",
            "x11",
            "x12",
            "x13",
            "x14",
            "x15",
        }

        def parse_imm(s):
            """Try to parse immediate from operand string."""
            if s is None:
                return None
            try:
                return int(s, 0)
            except Exception:
                return None

        def parse_mem_offset(insn):
            """Extract offset from load/store operand like '0(a5)' or '-4(a0)'."""
            import re

            m_off = re.search(r"(-?\d+)\(", insn.operands)
            if m_off:
                return int(m_off.group(1))
            if insn.imm is not None:
                return parse_imm(insn.imm)
            return None

        m = insn.mnemonic

        # sw rs2, offset(rs1): c.sw needs rs1,rs2 in CREG, offset 0-124 step 4, positive
        # c.swsp needs rs1=sp, offset 0-252 step 4
        if m == "sw":
            imm = parse_mem_offset(insn)
            if insn.rs1 == "sp" and imm is not None and 0 <= imm <= 252 and imm % 4 == 0:
                return 2  # c.swsp
            if insn.rs1 in CREG and insn.rs2 in CREG and imm is not None and 0 <= imm <= 124 and imm % 4 == 0:
                return 2  # c.sw
            return 4

        # lw rd, offset(rs1): similar constraints
        if m == "lw":
            imm = parse_mem_offset(insn)
            if insn.rd != "zero" and insn.rs1 == "sp" and imm is not None and 0 <= imm <= 252 and imm % 4 == 0:
                return 2  # c.lwsp
            if insn.rd in CREG and insn.rs1 in CREG and imm is not None and 0 <= imm <= 124 and imm % 4 == 0:
                return 2  # c.lw
            return 4

        # addi rd, rs1, imm: c.addi needs rd=rs1!=x0, imm in [-32,31] and imm!=0
        # c.addi16sp: rd=rs1=sp, imm multiple of 16, in [-512,496]
        # c.addi4spn: rd in CREG, rs1=sp, imm>0 multiple of 4
        if m == "addi":
            imm = parse_imm(insn.imm)
            if insn.rd == insn.rs1 and insn.rd != "zero" and imm is not None and -32 <= imm <= 31 and imm != 0:
                return 2  # c.addi
            if (
                insn.rd == "sp"
                and insn.rs1 == "sp"
                and imm is not None
                and imm % 16 == 0
                and -512 <= imm <= 496
                and imm != 0
            ):
                return 2  # c.addi16sp
            return 4

        # li rd, imm → c.li: rd!=x0, imm in [-32,31]
        if m == "li":
            imm = parse_imm(insn.imm)
            if insn.rd != "zero" and imm is not None and -32 <= imm <= 31:
                return 2
            return 4

        # mv rd, rs → c.mv: rd!=x0
        if m == "mv":
            return 2 if insn.rd != "zero" else 4

        # add rd, rs1, rs2 → c.add: rd=rs1!=x0
        if m == "add":
            return 2 if insn.rd == insn.rs1 and insn.rd != "zero" else 4

        # and/or/xor/sub → c.and/c.or/c.xor/c.sub: both operands in CREG, rd=rs1
        if m in ("and", "or", "xor", "sub"):
            if insn.rd == insn.rs1 and insn.rd in CREG and insn.rs2 in CREG:
                return 2
            return 4

        # slli/srli/srai → c.slli/c.srli/c.srai
        if m == "slli":
            imm = parse_imm(insn.imm)
            if insn.rd == insn.rs1 and insn.rd != "zero" and imm is not None and 1 <= imm <= 31:
                return 2
            return 4
        if m in ("srli", "srai"):
            imm = parse_imm(insn.imm)
            if insn.rd == insn.rs1 and insn.rd in CREG and imm is not None and 1 <= imm <= 31:
                return 2
            return 4

        # lui → c.lui: rd not x0/x2, imm in [1,31] or [0xfffe0,0xfffff]
        if m == "lui":
            return 2  # Usually compresses, hard to check exact imm

        # ret → c.jr ra (always 2)
        if m in ("ret", "nop", "j", "jr"):
            return 2

        # Default: 4 bytes
        return 4


class AsmPatcher:
    """Patches assembly to use HW loops.

    Transformations:
    1. Insert HW loop setup code before each loop group
    2. Remove back-edge branch instructions (HW handles iteration)
    3. Remove IV step instructions when IV is only used for control
    4. Add end labels after loop bodies
    """

    def __init__(self, generator: HWLoopGenerator, original_asm: str):
        self.generator = generator
        self.original_lines = original_asm.splitlines()
        self.patched_lines: List[str] = []
        self.branch_redirects: Dict[int, Tuple[str, str]] = {}

        # Filter out unsafe loops (entry branches from above skip setup)
        self._filter_unsafe_groups()

        # Build maps for efficient lookup
        self._build_removal_map()
        self._build_insertion_map()

    def _filter_unsafe_groups(self) -> None:
        """No-op: redirect approach handles entry branches."""
        pass

    def _find_function(self, func_name: str):
        """Find function object by name from the detector."""
        for f in self.generator.detector.functions:
            if f.name == func_name:
                return f
        return None

    def _build_removal_map(self) -> None:
        """Build map of line numbers to remove."""
        self.lines_to_remove: Dict[int, str] = {}  # line_num -> reason
        self.lines_to_comment: Dict[int, str] = {}  # line_num -> comment

        # Pick up bound-li deletions from the generator
        for line_num in getattr(self.generator, "_bound_li_deletions", set()):
            self.lines_to_remove[line_num] = "hwloop: bound register inlined"

        for group in self.generator.groups:
            for cfg in group.configs:
                loop = cfg.loop
                det = loop.constraint_results.get("deterministic_iterations", {})
                iv = det.get("iv")

                # Always remove back-edge branch
                self.lines_to_remove[loop.back_branch_line] = "hwloop: back-edge removed"

                # Remove IV step if removable and loop is innermost (no children)
                if cfg.removable.iv_step_removable and iv and not loop.children:
                    # Find the IV step instruction line
                    for insn in loop.body_instructions:
                        if insn.rd == iv and insn.mnemonic in (
                            "addi",
                            "c.addi",
                            "addiw",
                            "add",
                            "c.add",
                            "sub",
                            "c.sub",
                        ):
                            self.lines_to_remove[insn.line_num] = "hwloop: IV step removed"
                            break

    def _next_hwlp_idx(self) -> int:
        idx = self._hwlp_counter
        self._hwlp_counter += 1
        return idx

    def _build_insertion_map(self) -> None:
        """Build map of insertions (setup code and end labels).

        For nested loops with dynamic counts (inner loop count depends on
        outer loop IV), we split the setup:
          - la instructions (constant addresses) → before the outermost loop
          - count calculation + .insn → right before the inner loop's label
            (inside the outer loop, so it re-executes each outer iteration)
        """
        self.insertions_before: Dict[int, List[str]] = {}
        self.insertions_after: Dict[int, List[str]] = {}
        self._hwlp_markers: Dict[int, str] = {}  # idx -> start_label
        self._hwlp_counter = 0

        for group in self.generator.groups:
            self.current_group = group
            root = group.root

            # Separate configs into: root loop vs inner loops
            root_cfg = group.configs[0]  # ID=0 is always the root
            inner_cfgs = group.configs[1:]

            # --- Root loop setup: everything goes before root label ---
            root_setup = []
            root_setup.append(
                f"    # --- HW Loop: {root.function}:{root.start_label} ({len(group.configs)} loop(s)) ---"
            )

            # Root loop: count calc + hwloop.bounds + hwloop.count (2-instruction format)
            # Marker symbols for post-link fixup (non-local so they survive linking)
            root_setup.extend(root_cfg.spill_save)
            root_setup.extend(root_cfg.count_calc_insns)
            bounds_idx = self._next_hwlp_idx()
            root_setup.append(f"__hwlp_bounds_{bounds_idx}:")
            root_setup.extend(
                self._encode_bounds(root_cfg, f"{root_cfg.loop.start_label}", f"{root_cfg.loop.start_label}_hwend")
            )
            root_setup.append(self._encode_count_line(root_cfg))
            root_setup.extend(root_cfg.spill_restore)
            self._hwlp_markers[bounds_idx] = root_cfg.loop.start_label

            # Inner loops: bounds go here (addresses are constant, PC-relative)
            for cfg in inner_cfgs:
                inner_bounds_idx = self._next_hwlp_idx()
                root_setup.append(f"__hwlp_bounds_{inner_bounds_idx}:")
                root_setup.extend(self._encode_bounds(cfg, f"{cfg.loop.start_label}", f"{cfg.loop.start_label}_hwend"))
                self._hwlp_markers[inner_bounds_idx] = cfg.loop.start_label

            # (SW inner loops are safe thanks to the deferred-DEC RTL fix —
            #  no guard HW loop register needed.)

            root_line = root.start_line
            # If instruction before loop label is unconditional jump,
            # insert setup before the jump so it executes on first entry.
            # The count register may be live at this point, so spill it.
            if root_line > 0:
                prev = self.original_lines[root_line - 1].strip()
                if prev.startswith("j\t") or prev.startswith("j "):
                    root_line = root_line - 1
                    # Add spill/restore for count register around the setup
                    cr = root_cfg.count_reg
                    if cr not in ("zero", "sp", "gp", "tp"):
                        root_setup.insert(0, f"    sw {cr}, -8(sp)  # spill count reg (prologue)")
                        # Insert restore after count instruction
                        # Find the hwloop.count line and insert restore after it
                        for idx, line in enumerate(root_setup):
                            if "hwloop.count" in line:
                                root_setup.insert(idx + 1, f"    lw {cr}, -8(sp)  # restore count reg (prologue)")
                                break
            if root_line not in self.insertions_before:
                self.insertions_before[root_line] = []

            # Detect branches from above that jump to the loop start label.
            # Redirect them to go through the setup code.
            setup_label = f"{root.start_label}_hwsetup"
            needs_setup_label = False
            func = self._find_function(root.function)
            if func:
                import re

                branch_re = re.compile(
                    r"^\s*(?:beq|bne|blt|bge|bltu|bgeu|bgt|ble|bgtu|bleu|bnez|beqz|j|jal)\s+.*"
                    + re.escape(root.start_label)
                    + r"\b"
                )
                for ln in range(func.start_line, root_line):
                    if ln < len(self.original_lines) and branch_re.match(self.original_lines[ln]):
                        self.branch_redirects[ln] = (root.start_label, setup_label)
                        needs_setup_label = True

            if needs_setup_label:
                self.insertions_before[root_line] = [f"{setup_label}:"] + root_setup + self.insertions_before[root_line]
            else:
                self.insertions_before[root_line].extend(root_setup)
            # Emit LP_START marker right before the loop label
            # Align LP_START for hwloop hardware requirement
            self.insertions_before[root_line].append(f"__hwlp_start_{bounds_idx}:")

            # --- Inner loop setup: count calc + hwloop.count go before inner label ---
            for cfg in inner_cfgs:
                inner_setup = []
                inner_setup.append(f"    # --- HW Loop inner: {cfg.loop.start_label} (id={cfg.loop_id}) ---")
                inner_setup.extend(cfg.spill_save)
                inner_setup.extend(cfg.count_calc_insns)
                inner_setup.append(self._encode_count_line(cfg))
                inner_setup.extend(cfg.spill_restore)

                inner_line = cfg.loop.start_line
                if inner_line not in self.insertions_before:
                    self.insertions_before[inner_line] = []
                self.insertions_before[inner_line].extend(inner_setup)
                # Emit LP_START marker for inner loop
                for midx, mlbl in self._hwlp_markers.items():
                    if mlbl == cfg.loop.start_label:
                        self.insertions_before[inner_line].append(f"__hwlp_start_{midx}:")
                        break

            # --- End labels after each loop's back-edge ---
            # Sort configs by back_branch_line so inner loops come first.
            sorted_cfgs = sorted(group.configs, key=lambda c: c.loop.back_branch_line)
            prev_end_line = None
            for cfg in sorted_cfgs:
                loop = cfg.loop
                end_label = f"{loop.start_label}_hwend:"
                back_line = loop.back_branch_line

                # ARVIS Phase 4: force the last body instruction to 4 bytes by
                # wrapping it in `.option push; .option norvc; ...; .option pop`.
                # This makes the patcher's pnult bit (always set to 1 = 4-byte)
                # match the assembler's encoding deterministically. Without this,
                # operand-driven RV32C compression on instructions like `addi`
                # would silently turn the last body insn into 2 bytes, causing
                # the RTL to compute the wrong LP_last_addr and break the loop.
                #
                # NOTE: body_instructions[-1] is the back-edge branch which the
                # patcher REMOVES, so the actual last body insn after patching
                # is body_instructions[-2].
                if len(loop.body_instructions) >= 2:
                    last_insn = loop.body_instructions[-2]
                    last_line = last_insn.line_num
                    if not last_insn.mnemonic.startswith("c."):
                        if last_line not in self.insertions_before:
                            self.insertions_before[last_line] = []
                        self.insertions_before[last_line].insert(0, "    .option push")
                        self.insertions_before[last_line].append("    .option norvc  # ARVIS Phase 4: keep last body insn at 4B for pnult")
                        if last_line not in self.insertions_after:
                            self.insertions_after[last_line] = []
                        self.insertions_after[last_line].insert(0, "    .option pop")

                if back_line not in self.insertions_after:
                    self.insertions_after[back_line] = []

                # Pad small loop bodies to avoid prefetch buffer bug (body must be >= 16 bytes)
                body_bytes = sum(
                    HWLoopGenerator._estimate_insn_size(insn)
                    for insn in loop.body_instructions
                    if insn.line_num not in self.lines_to_remove
                )
                if body_bytes < 16:
                    pad_bytes = 16 - body_bytes
                    nops_needed = (pad_bytes + 3) // 4
                    for _ in range(nops_needed):
                        self.insertions_after[back_line].append("    .word 0x00000013  # pad: 4B NOP (prefetch min)")

                # Cascade guard: ensure >= 6 bytes between adjacent LP_end labels
                if prev_end_line is not None and prev_end_line == back_line:
                    self.insertions_after[back_line].append("    .word 0x00000013  # pad: cascade guard")
                    self.insertions_after[back_line].append("    .word 0x00000013  # pad: cascade guard")
                elif prev_end_line is not None:
                    gap_insns = [
                        insn
                        for insn in loop.body_instructions
                        if insn.line_num > prev_end_line
                        and insn.line_num <= back_line
                        and insn.line_num not in self.lines_to_remove
                    ]
                    gap_bytes = sum(HWLoopGenerator._estimate_insn_size(insn) for insn in gap_insns)
                    if gap_bytes < 6:
                        nops = (6 - gap_bytes + 3) // 4
                        for _ in range(nops):
                            self.insertions_after[back_line].append("    .word 0x00000013  # pad: cascade guard")

                self.insertions_after[back_line].append(end_label)
                # Emit marker symbol for post-link fixup
                lbl = loop.start_label
                for midx, mlbl in self._hwlp_markers.items():
                    if mlbl == lbl:
                        self.insertions_after[back_line].append(f"__hwlp_end_{midx}:")
                        break
                prev_end_line = back_line

            # --- Clear inner loop counters after outer loop exits ---
            if inner_cfgs:
                back_line = root.back_branch_line
                for cfg in inner_cfgs:
                    rd_field = cfg.loop_id & self.generator.id_mask
                    enc = self.generator.enc
                    encoding = (enc.count_funct3 << 12) | (rd_field << 7) | enc.count_opcode
                    self.insertions_after[back_line].append(
                        f"    .insn 0x{encoding:08x}  # clear LP{cfg.loop_id} counter"
                    )

    def _find_function(self, func_name: str):
        """Find function object by name from the detector."""
        for f in self.generator.detector.functions:
            if f.name == func_name:
                return f
        return None

    def _encode_bounds(self, cfg, start_label: str, end_label: str) -> list:
        """Generate bounds instruction(s). Uses compact BOUNDS for groups with
        <=2 loops, or separate START+END for groups with 3+ loops."""
        if len(self.current_group.configs) <= 2 and (self.generator.hw_loop_bits <= 1 or cfg.offset_encoding == 0):
            rd_field = (cfg.loop_id & self.generator.id_mask) | (
                (cfg.offset_encoding & 0x1) << self.generator.hw_loop_bits
            )
            enc = self.generator.enc
            return [
                f"    .word ((({end_label} - .) >> 1) << 20) | "
                f"(((({start_label} - .) >> 1) >> 3) << 15) | "
                f"({enc.bounds_funct3} << 12) | "
                f"(((({start_label} - .) >> 1) & 0x7) << 9) | "
                f"({rd_field} << 7) | "
                f"0x{enc.bounds_opcode:02x}  "
                f"# hwloop.bounds id={cfg.loop_id}"
            ]
        else:
            rd = cfg.loop_id & 0x1F
            enc = self.generator.enc
            return [
                f"    .word ((({start_label} - .) >> 1) << 20) | ({enc.start_funct3} << 12) | ({rd} << 7) | 0x{enc.start_opcode:02x}  # hwloop.start id={cfg.loop_id}",  # noqa: E501
                f"    .word ((({end_label} - .) >> 1) << 20) | ({enc.end_funct3} << 12) | ({rd} << 7) | 0x{enc.end_opcode:02x}  # hwloop.end id={cfg.loop_id}",  # noqa: E501
            ]

    def _encode_count_line(self, cfg: HWLoopConfig) -> str:
        """Generate hwloop.count instruction (funct3=011).
        Format: rs4[31:27] | ... | funct3[14:12] | rd[11:7] | opcode[6:0]
        rs4 (bits 31:27) = count register, rd = loop ID."""
        count_num = REG_MAP.get(cfg.count_reg, 0)
        rd_field = (cfg.loop_id & self.generator.id_mask) | ((cfg.offset_encoding & 0x1) << self.generator.hw_loop_bits)
        enc = self.generator.enc
        encoding = 0
        encoding |= (count_num & 0x1F) << 27  # rs4 field (bits 31:27)
        encoding |= (enc.count_funct3) << 12  # funct3
        encoding |= (rd_field & 0x1F) << 7
        encoding |= enc.count_opcode
        return f"    .insn 0x{encoding:08x}  # hwloop.count id={cfg.loop_id} count={cfg.count_reg}"

    def patch(self) -> str:
        """Apply all patches and return the modified assembly."""
        self.patched_lines = []

        for line_num, line in enumerate(self.original_lines):
            # Insert any lines that go before this line
            if line_num in self.insertions_before:
                for insert_line in self.insertions_before[line_num]:
                    self.patched_lines.append(insert_line)

            # Handle the current line
            if line_num in self.lines_to_remove:
                reason = self.lines_to_remove[line_num]
                self.patched_lines.append(f"    # REMOVED: {line.strip()}  # {reason}")
            elif line_num in self.branch_redirects:
                old_target, new_target = self.branch_redirects[line_num]
                redirected = line.replace(old_target, new_target)
                self.patched_lines.append(redirected)
            else:
                self.patched_lines.append(line)

            # Insert any lines that go after this line
            if line_num in self.insertions_after:
                for insert_line in self.insertions_after[line_num]:
                    self.patched_lines.append(insert_line)

        return "\n".join(self.patched_lines)

    def get_stats(self) -> Dict:
        """Get patching statistics."""
        total_removed = len(self.lines_to_remove)
        total_inserted = sum(len(v) for v in self.insertions_before.values())
        total_inserted += sum(len(v) for v in self.insertions_after.values())

        # Find max loop ID used (determines HW_LOOP parameter)
        max_loop_id = 0
        for group in self.generator.groups:
            for cfg in group.configs:
                max_loop_id = max(max_loop_id, cfg.loop_id)

        # HW_LOOP = max_loop_id + 1 (if max_id=0, we need 1 loop; if max_id=1, we need 2 loops)
        # But RTL requires HW_LOOP >= 2 due to clog2(1)=0 causing bit range issues
        hw_loop_param = max(2, max_loop_id + 1) if self.generator.groups else 0

        return {
            "lines_removed": total_removed,
            "lines_inserted": total_inserted,
            "net_change": total_inserted - total_removed,
            "loops_patched": sum(len(g.configs) for g in self.generator.groups),
            "max_loop_id": max_loop_id,
            "hw_loop_param": hw_loop_param,  # Pass this to verilator as -GHW_LOOP=N
        }


def main():
    import json
    import sys

    asm_file = sys.argv[1] if len(sys.argv) > 1 else "src/code_O3.s"
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    hw_loop = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    # Parse --exclude=fn1,fn2,... and --only=fn1,fn2,...
    exclude_fns: Set[str] = set()
    only_fns: Optional[Set[str]] = None
    fifo_depth: Optional[int] = None
    encoding: Optional["HWLoopEncoding"] = None
    for arg in sys.argv[4:]:
        if arg.startswith("--exclude="):
            exclude_fns.update(arg[len("--exclude=") :].split(","))
        elif arg.startswith("--only="):
            only_fns = set(arg[len("--only=") :].split(","))
        elif arg.startswith("--fifo-depth="):
            # Reject loops whose body is smaller than fifo_depth*4 bytes;
            # they can't be safely converted to hwloops because the
            # speculative prefetcher accumulates wrap fetches that
            # corrupt the body's accumulator after the loop exits.
            fifo_depth = int(arg[len("--fifo-depth=") :])
        elif arg.startswith("--opcode="):
            # Override hwloop opcode for all four instruction kinds
            # (bounds/count/start/end). Useful when ARVIS-pruned RTL
            # places hwloop in CUSTOM_0 (0x0B) instead of CUSTOM_3 (0x7B).
            if encoding is None:
                encoding = HWLoopEncoding()
            opc = int(arg[len("--opcode=") :], 0)
            encoding.bounds_opcode = opc
            encoding.count_opcode = opc
            encoding.start_opcode = opc
            encoding.end_opcode = opc
        elif arg.startswith("--funct3="):
            # Comma-separated bounds,count,start,end funct3 values, e.g.
            # --funct3=0,1,3,4 for the edn-pruned RTL.
            if encoding is None:
                encoding = HWLoopEncoding()
            parts = arg[len("--funct3=") :].split(",")
            if len(parts) != 4:
                raise SystemExit("--funct3 needs 4 comma-separated values: bounds,count,start,end")
            encoding.bounds_funct3 = int(parts[0], 0)
            encoding.count_funct3  = int(parts[1], 0)
            encoding.start_funct3  = int(parts[2], 0)
            encoding.end_funct3    = int(parts[3], 0)

    print(f"Analyzing {asm_file}...")
    detector = AsmLoopDetector(asm_file, fifo_depth=fifo_depth)
    loops = detector.find_all_loops()

    print(f"Found {len(loops)} loops, {sum(1 for l in loops if l.hw_eligible)} HW-eligible")
    if fifo_depth is not None:
        rejected = [l for l in loops if not l.hw_eligible
                    and "body_size_eligible" in l.failing_constraints]
        if rejected:
            print(f"  ({len(rejected)} loops rejected for body_size < {fifo_depth*4} bytes)")
    print()

    generator = HWLoopGenerator(detector, hw_loop=hw_loop, exclude_fns=exclude_fns, only_fns=only_fns,
                                encoding=encoding)
    groups = generator.generate()

    print(f"Generated {len(groups)} HW loop groups")

    # Patch the assembly
    original_asm = open(asm_file).read()
    patcher = AsmPatcher(generator, original_asm)
    patched_asm = patcher.patch()
    stats = patcher.get_stats()

    print("\nPatching stats:")
    print(f"  Lines removed: {stats['lines_removed']}")
    print(f"  Lines inserted: {stats['lines_inserted']}")
    print(f"  Net change: {stats['net_change']:+d} lines")
    print(f"  Loops patched: {stats['loops_patched']}")
    print(f"  Max loop ID used: {stats['max_loop_id']}")
    print(f"  HW_LOOP parameter: {stats['hw_loop_param']}  (use -GHW_LOOP={stats['hw_loop_param']})")

    # Write output
    if output_file:
        out_path = output_file
    else:
        out_path = asm_file.replace(".s", "_hwloop.s")

    with open(out_path, "w") as f:
        f.write(patched_asm)
        f.write("\n")  # Ensure newline at end
    print(f"\nWrote patched assembly to {out_path}")

    # Write JSON stats file for tooling integration
    json_path = out_path.replace(".s", ".json")
    json_stats = {
        "source_file": asm_file,
        "output_file": out_path,
        "total_loops": len(loops),
        "hw_eligible_loops": sum(1 for l in loops if l.hw_eligible),
        "loops_patched": stats["loops_patched"],
        "hw_loop_param": stats["hw_loop_param"],
        "verilator_flag": f"-GHW_LOOP={stats['hw_loop_param']}",
        "lines_removed": stats["lines_removed"],
        "lines_inserted": stats["lines_inserted"],
    }
    with open(json_path, "w") as f:
        json.dump(json_stats, f, indent=2)
    print(f"Wrote stats to {json_path}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    total_bytes_saved = 0
    removable_iv_steps = 0

    for group in groups:
        root = group.root
        print(f"\n{root.function}:{root.start_label}")
        print(f"  Nested loops: {len(group.configs)}")
        for cfg in group.configs:
            det = cfg.loop.constraint_results.get("deterministic_iterations", {})
            print(f"    ID={cfg.loop_id}: {cfg.loop.start_label}")
            print(f"      COUNT={cfg.count_reg}, START={cfg.loop_start_reg}, END={cfg.loop_end_reg}")
            print(f"      IV={det.get('iv')}, bound={det.get('bound')}, step={det.get('step_value')}")

            # Show removable instructions
            r = cfg.removable
            print(
                f"      Removable: branch={r.back_branch is not None}, "
                f"iv_step={r.iv_step_removable} ({r.total_bytes_saved}B/iter)"
            )
            total_bytes_saved += r.total_bytes_saved
            if r.iv_step_removable:
                removable_iv_steps += 1

    print("\n" + "=" * 70)
    print("REMOVABLE INSTRUCTIONS SUMMARY")
    print("=" * 70)

    # Count by IV usage pattern
    usage_counts = {}
    total_potential_autoincr = 0

    for group in groups:
        for cfg in group.configs:
            usage = cfg.removable.iv_usage
            usage_counts[usage] = usage_counts.get(usage, 0) + 1
            total_potential_autoincr += cfg.removable.potential_with_autoincr

    print(f"Total HW loops: {sum(len(g.configs) for g in groups)}")
    print("\nIV Usage Patterns:")
    for usage, count in sorted(usage_counts.items(), key=lambda x: -x[1]):
        removable = "✓ step removable" if usage == "control_only" else ""
        autoincr = "(auto-incr candidate)" if usage == "pointer" else ""
        print(f"  {usage:15} {count:3}  {removable}{autoincr}")

    print("\nBytes saved per iteration:")
    print(f"  Current (branch + control_only steps): {total_bytes_saved}B")
    print(
        f"  With auto-increment HW: +{total_potential_autoincr}B (total {total_bytes_saved + total_potential_autoincr}B)"  # noqa: E501
    )


if __name__ == "__main__":
    main()
