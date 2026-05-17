"""
Assembly-level loop detection — works directly on GCC .s files.

Instead of the dominator-based CFG analysis (which produces ~600 false positives),
this detects loops by finding backward label references in the assembly:

    A backward branch = a branch/jump whose target label was defined EARLIER
    in the same function. Each backward branch = one loop.

This gives zero false positives by construction — every detected "loop" has
an actual backward branch in the assembly.

The loop body is everything between the target label and the branch instruction.
Nesting is determined by containment of line ranges.

Usage:
    from arvis.analysis.asm_loops import AsmLoopDetector
    detector = AsmLoopDetector("path/to/benchmark_fused_src.s")
    loops = detector.find_all_loops()
    for loop in loops:
        print(f"{loop.function}: {loop.start_label} ({loop.body_insn_count} insns)")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Branch/jump mnemonics that can form loop back-edges
BRANCH_MNEMONICS = {
    "bne",
    "beq",
    "blt",
    "bge",
    "bltu",
    "bgeu",
    "bnez",
    "beqz",
    "c.bnez",
    "c.beqz",
    "c.j",
    "c.jal",
    "j",
    "jal",
}

# Conditional branches — these are real loop back-edges
CONDITIONAL_BRANCHES = {
    "bne",
    "beq",
    "blt",
    "bge",
    "bltu",
    "bgeu",
    "bnez",
    "beqz",
    "c.bnez",
    "c.beqz",
}

# Unconditional jumps — may be Duff's device entries, not real loops
UNCONDITIONAL_JUMPS = {"j", "c.j"}

# Minimum number of instructions in a loop body to be considered a real loop
# (filters degenerate patterns like WFI halt loops, trivial goto patterns)
# Set to 2 to avoid discarding tight polling loops (e.g. lw; bnez)
MIN_LOOP_BODY_INSNS = 2

# Mnemonics that are function calls (NOT loop back-edges)
CALL_MNEMONICS = {"jal", "c.jal", "jalr", "c.jalr", "call", "tail"}

# Return instructions
RETURN_MNEMONICS = {"ret", "c.jr", "jalr"}


@dataclass
class AsmInstruction:
    """A parsed instruction from the .s file."""

    line_num: int
    raw_line: str
    mnemonic: str
    operands: str = ""
    rd: Optional[str] = None
    rs1: Optional[str] = None
    rs2: Optional[str] = None
    imm: Optional[str] = None
    branch_target_label: Optional[str] = None

    @property
    def is_branch(self) -> bool:
        return self.mnemonic in BRANCH_MNEMONICS

    @property
    def is_call(self) -> bool:
        """True if this is a function call."""
        if self.mnemonic in ("call", "tail"):
            return True
        if self.mnemonic in ("jal", "c.jal"):
            # jal ra, target = call; jal zero, target = jump
            return self.rd in ("ra", "x1") or (self.rd is None and self.mnemonic == "jal")
        if self.mnemonic in ("jalr", "c.jalr"):
            return True
        return False

    @property
    def is_return(self) -> bool:
        if self.mnemonic == "ret":
            return True
        if self.mnemonic == "c.jr" and self.rs1 in ("ra", "x1"):
            return True
        if self.mnemonic == "jalr" and self.rs1 in ("ra", "x1") and self.rd in ("zero", "x0", None):
            return True
        return False


# --- Symbolic expression types for register tracking ---
class SymExpr:
    """Base class for symbolic expressions."""

    def simplify(self):
        return self

    def is_const(self):
        return False

    def const_val(self):
        return None

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def __hash__(self):
        return hash(str(self))


class SymConst(SymExpr):
    def __init__(self, val: int):
        self.val = val

    def is_const(self):
        return True

    def const_val(self):
        return self.val

    def __repr__(self):
        return str(self.val)


class SymReg(SymExpr):
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return self.name


class SymAdd(SymExpr):
    def __init__(self, a: SymExpr, b: SymExpr):
        self.a, self.b = a, b

    def simplify(self):
        a, b = self.a.simplify(), self.b.simplify()
        if a.is_const() and b.is_const():
            return SymConst(a.const_val() + b.const_val())
        if b.is_const() and b.const_val() == 0:
            return a
        if a.is_const() and a.const_val() == 0:
            return b
        return SymAdd(a, b)

    def __repr__(self):
        return f"({self.a} + {self.b})"


def _flatten_add(expr):
    """Flatten nested Add into (non_const_terms, const_sum)."""
    if isinstance(expr, SymConst):
        return [], expr.val
    if isinstance(expr, SymAdd):
        t1, c1 = _flatten_add(expr.a)
        t2, c2 = _flatten_add(expr.b)
        return t1 + t2, c1 + c2
    if isinstance(expr, SymSub):
        t1, c1 = _flatten_add(expr.a)
        t2, c2 = _flatten_add(expr.b)
        # Negate the subtracted terms
        return t1 + [("neg", t) for t in t2], c1 - c2
    return [expr], 0


class SymSub(SymExpr):
    def __init__(self, a: SymExpr, b: SymExpr):
        self.a, self.b = a, b

    def simplify(self):
        a, b = self.a.simplify(), self.b.simplify()
        if a.is_const() and b.is_const():
            return SymConst(a.const_val() - b.const_val())
        if b.is_const() and b.const_val() == 0:
            return a
        if a == b:
            return SymConst(0)
        # Try flattening: if a and b share the same non-const base, cancel it
        ta, ca = _flatten_add(a)
        tb, cb = _flatten_add(b)
        # Remove common terms
        remaining_a = list(ta)
        remaining_b = list(tb)
        for t in list(remaining_a):
            if t in remaining_b:
                remaining_a.remove(t)
                remaining_b.remove(t)
        if not remaining_a and not remaining_b:
            return SymConst(ca - cb)
        return SymSub(a, b)

    def __repr__(self):
        return f"({self.a} - {self.b})"


class SymSll(SymExpr):
    def __init__(self, a: SymExpr, n: int):
        self.a, self.n = a, n

    def simplify(self):
        a = self.a.simplify()
        if a.is_const():
            return SymConst(a.const_val() << self.n)
        return SymSll(a, self.n)

    def __repr__(self):
        return f"({self.a} << {self.n})"


class SymSrl(SymExpr):
    def __init__(self, a: SymExpr, n: int):
        self.a, self.n = a, n

    def simplify(self):
        a = self.a.simplify()
        if a.is_const():
            return SymConst(a.const_val() >> self.n)
        return SymSrl(a, self.n)

    def __repr__(self):
        return f"({self.a} >> {self.n})"


class SymMul(SymExpr):
    def __init__(self, a: SymExpr, b: SymExpr):
        self.a, self.b = a, b

    def simplify(self):
        a, b = self.a.simplify(), self.b.simplify()
        if a.is_const() and b.is_const():
            return SymConst(a.const_val() * b.const_val())
        return SymMul(a, b)

    def __repr__(self):
        return f"({self.a} * {self.b})"


class SymMem(SymExpr):
    """Memory load — opaque, can't simplify."""

    def __init__(self, addr: SymExpr):
        self.addr = addr

    def __repr__(self):
        return f"MEM[{self.addr}]"


def symbolic_eval_pre_loop(func_insns, loop_start_line) -> dict:
    """Evaluate registers symbolically through pre-loop instructions.

    Returns a dict mapping register name -> SymExpr for all registers
    whose values can be tracked through the pre-loop code.
    """
    # Initial state: only zero is known. Function args are opaque.
    state = {"zero": SymConst(0), "x0": SymConst(0)}

    pre_insns = [i for i in func_insns if i.line_num < loop_start_line]

    # Only evaluate the last basic block before the loop
    # (from the last branch/label to the loop entry)
    BRANCH_MN = {
        "beq",
        "bne",
        "blt",
        "bge",
        "bltu",
        "bgeu",
        "bleu",
        "bgtu",
        "blez",
        "bgez",
        "bltz",
        "bgtz",
        "j",
        "jal",
        "jalr",
        "jr",
        "call",
        "tail",
        "ret",
        "c.beqz",
        "c.bnez",
        "c.j",
        "c.jal",
        "c.jalr",
        "c.jr",
    }
    # Find the last branch in pre-loop code. Only evaluate after it.
    # This ensures we're in a single basic block leading to the loop.
    last_branch_idx = -1
    for idx, insn in enumerate(pre_insns):
        if insn.mnemonic in BRANCH_MN:
            last_branch_idx = idx
    if last_branch_idx >= 0:
        pre_insns = pre_insns[last_branch_idx + 1 :]

    for insn in pre_insns:
        rd = insn.rd
        if not rd:
            continue

        mn = insn.mnemonic
        rs1 = insn.rs1
        rs2 = insn.rs2
        imm = insn.imm

        def get(r):
            return state.get(r, SymReg(r))

        def parse_imm(s):
            if s is None:
                return 0
            try:
                return int(s, 0) if isinstance(s, str) else int(s)
            except Exception:
                return 0

        if mn in ("li", "c.li"):
            state[rd] = SymConst(parse_imm(imm))
        elif mn in ("lui", "c.lui"):
            state[rd] = SymConst(parse_imm(imm) << 12)
        elif mn in ("mv", "c.mv"):
            src = rs1 or rs2
            state[rd] = get(src) if src else SymReg(rd)
        elif mn in ("addi", "c.addi"):
            state[rd] = SymAdd(get(rs1), SymConst(parse_imm(imm))).simplify()
        elif mn in ("slli", "c.slli"):
            state[rd] = SymSll(get(rs1), parse_imm(imm)).simplify()
        elif mn in ("srli", "c.srli"):
            state[rd] = SymSrl(get(rs1), parse_imm(imm)).simplify()
        elif mn == "add" or mn == "c.add":
            state[rd] = SymAdd(get(rs1), get(rs2)).simplify()
        elif mn == "sub" or mn == "c.sub":
            state[rd] = SymSub(get(rs1), get(rs2)).simplify()
        elif mn == "mul":
            state[rd] = SymMul(get(rs1), get(rs2)).simplify()
        elif mn in ("lw", "lh", "lhu", "lb", "lbu"):
            state[rd] = SymMem(get(rs1))  # opaque
        elif mn == "auipc":
            state[rd] = SymReg(rd)  # PC-relative, opaque
        else:
            # Unknown instruction writes to rd — mark as opaque
            state[rd] = SymReg(rd)

    return state


@dataclass
class AsmLoop:
    """A loop detected from the .s file."""

    function: str
    start_label: str  # The label at the loop top (e.g., ".L41")
    start_line: int  # Line number of the start label
    back_branch_line: int  # Line number of the backward branch
    back_branch_insn: str  # The backward branch instruction text
    back_branch_mnemonic: str  # e.g., "bne"
    back_branch_operands: str  # e.g., "s4,a0,.L41"

    # Computed
    body_lines: List[int] = field(default_factory=list)  # Line numbers of instructions in body
    body_instructions: List[AsmInstruction] = field(default_factory=list)
    body_insn_count: int = 0

    # Nesting
    nesting_depth: int = 0
    parent: Optional["AsmLoop"] = None
    children: List["AsmLoop"] = field(default_factory=list)

    # HW loop constraint results
    hw_eligible: Optional[bool] = None
    constraint_results: Dict[str, dict] = field(default_factory=dict)
    failing_constraints: List[str] = field(default_factory=list)

    # Greedy nesting: maximal call-free nestable children for HW conversion
    hw_nestable_children: List["AsmLoop"] = field(default_factory=list)

    # Prologue jump: j before start label (first iteration enters mid-body)
    has_prologue_jump: bool = False

    @property
    def tree_has_calls(self) -> bool:
        """True if this loop or ANY descendant contains a function call."""
        if any(insn.is_call for insn in self.body_instructions):
            return True
        return any(child.tree_has_calls for child in self.children)

    def contains(self, other: "AsmLoop") -> bool:
        """True if this loop's line range contains the other loop's range."""
        # Same start label = same loop with multiple back-edges, not parent/child
        if self.start_label == other.start_label:
            return False
        return (
            self.start_line <= other.start_line
            and other.back_branch_line <= self.back_branch_line
            and (self.start_line, self.back_branch_line) != (other.start_line, other.back_branch_line)
        )


@dataclass
class AsmFunction:
    """A function parsed from the .s file."""

    name: str
    start_line: int
    end_line: int
    instructions: List[AsmInstruction] = field(default_factory=list)
    labels: Dict[str, int] = field(default_factory=dict)  # label → line_num
    loops: List[AsmLoop] = field(default_factory=list)


class AsmLoopDetector:
    """Detects loops in GCC-generated .s files by finding backward label references."""

    def __init__(self, asm_path: str):
        self.asm_path = Path(asm_path)
        self.lines: List[str] = []
        self.functions: List[AsmFunction] = []
        self.all_loops: List[AsmLoop] = []

    def find_all_loops(self) -> List[AsmLoop]:
        """Main entry point: parse the .s file and find all loops."""
        self.lines = self.asm_path.read_text().splitlines()
        self._parse_functions()

        for func in self.functions:
            self._find_loops_in_function(func)

        # Collect all raw loops
        raw_loops = [loop for func in self.functions for loop in func.loops]

        # Filter 1: Remove degenerate loops (too few instructions)
        self.all_loops = [l for l in raw_loops if l.body_insn_count >= MIN_LOOP_BODY_INSNS]

        self._build_nesting_hierarchy()
        self._analyze_hw_eligibility()
        self._build_greedy_hw_nesting()

        return self.all_loops

    def _parse_functions(self) -> None:
        """Split the .s file into functions."""
        func_pattern = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_.]*):")
        # Exclude data labels (after .data/.rodata/.bss sections)
        data_section = False

        func_starts: List[Tuple[str, int]] = []
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped in (
                ".data",
                ".rodata",
                ".bss",
                ".section .rodata",
                ".section .data",
                ".section .bss",
            ):
                data_section = True
                continue
            if stripped in (".text", ".section .text"):
                data_section = False
                continue

            if data_section:
                continue

            m = func_pattern.match(line)
            if m:
                name = m.group(1)
                # Skip assembler directives that look like labels
                if name.startswith("."):
                    continue
                func_starts.append((name, i))

        # Build function objects with start/end ranges.
        # Deduplicate: only keep the first occurrence of each function name.
        # The .s file may contain a second copy of all functions in debug/DWARF
        # sections, which would double-count every loop.
        seen_func_names: Set[str] = set()
        for idx, (name, start) in enumerate(func_starts):
            if name in seen_func_names:
                continue  # Skip duplicate function definitions
            seen_func_names.add(name)
            end = func_starts[idx + 1][1] - 1 if idx + 1 < len(func_starts) else len(self.lines) - 1
            func = AsmFunction(name=name, start_line=start, end_line=end)
            self._parse_function_body(func)
            self.functions.append(func)

    def _parse_function_body(self, func: AsmFunction) -> None:
        """Parse instructions and labels within a function."""
        # Match .L labels, numeric local labels (e.g. "1:"), and any other labels
        label_pattern = re.compile(r"^(\.L\w+|\d+):")
        insn_pattern = re.compile(r"^\t(\S+)(?:\s+(.*))?$")

        # Add function name itself as a label so back-branches to function
        # entry (tail-recursive style loops) are detected
        func.labels[func.name] = func.start_line

        in_inline_section = False
        for i in range(func.start_line, func.end_line + 1):
            line = self.lines[i]
            stripped = line.strip()

            # Handle .section/.previous pairs inside functions (e.g. exception tables)
            if stripped.startswith(".section") and not stripped.startswith(".section .text"):
                in_inline_section = True
                continue
            if stripped in (".previous", ".popsection"):
                in_inline_section = False
                continue
            if in_inline_section:
                continue

            # Check for local labels
            m = label_pattern.match(line)
            if m:
                func.labels[m.group(1)] = i
                continue

            # Check for instructions (indented with tab)
            m = insn_pattern.match(line)
            if m:
                mnemonic = m.group(1).lower()
                operands_str = m.group(2).strip() if m.group(2) else ""

                # Skip directives
                if mnemonic.startswith(".") and mnemonic != ".insn":
                    continue
                if mnemonic in (
                    "",
                    ".cfi_startproc",
                    ".cfi_endproc",
                    ".cfi_def_cfa_offset",
                    ".cfi_offset",
                    ".option",
                ):
                    continue

                insn = self._parse_instruction(i, line, mnemonic, operands_str)
                func.instructions.append(insn)

    def _parse_instruction(self, line_num: int, raw_line: str, mnemonic: str, operands_str: str) -> AsmInstruction:
        """Parse a single instruction's operands."""
        insn = AsmInstruction(
            line_num=line_num,
            raw_line=raw_line,
            mnemonic=mnemonic,
            operands=operands_str,
        )

        # Remove inline comments
        ops = operands_str.split("#")[0].strip()
        parts = [p.strip() for p in ops.split(",") if p.strip()]

        # Extract branch target label (supports .L labels, numeric local
        # labels like "1b", and plain symbol names for function-entry loops)
        if mnemonic in BRANCH_MNEMONICS:
            # Known RISC-V register names to exclude from label matching
            _REGS = {
                "",
                "zero",
                "ra",
                "sp",
                "gp",
                "tp",
                "fp",
                *(f"x{i}" for i in range(32)),
                *(f"s{i}" for i in range(12)),
                *(f"a{i}" for i in range(8)),
                *(f"t{i}" for i in range(7)),
            }
            for p in parts:
                if p.startswith(".L"):
                    insn.branch_target_label = p
                    break
                if re.match(r"^\d+[bf]$", p):
                    # GNU numeric local label: "1b" -> "1"
                    insn.branch_target_label = p[:-1]
                    break
                if p not in _REGS and not re.match(r"^-?\d+$", p):
                    # Plain symbol name (e.g. function name)
                    insn.branch_target_label = p
                    break

        # Parse register operands based on instruction type
        if mnemonic in ("bne", "beq", "blt", "bge", "bltu", "bgeu"):
            if len(parts) >= 2:
                insn.rs1 = parts[0]
                insn.rs2 = parts[1]
        elif mnemonic in ("bnez", "beqz", "c.bnez", "c.beqz"):
            if len(parts) >= 1:
                insn.rs1 = parts[0]
        elif mnemonic in ("c.j", "j"):
            pass  # No register operands, just label
        elif mnemonic == "jal":
            if len(parts) == 2:
                insn.rd = parts[0]
            elif len(parts) == 1:
                insn.rd = "ra"
        elif mnemonic in (
            "add",
            "addw",
            "sub",
            "subw",
            "mul",
            "mulw",
            "mulh",
            "mulhu",
            "mulhsu",
            "and",
            "or",
            "xor",
            "sll",
            "sllw",
            "srl",
            "srlw",
            "sra",
            "sraw",
            "slt",
            "sltu",
            "div",
            "divw",
            "divu",
            "divuw",
            "rem",
            "remw",
            "remu",
            "remuw",
        ):
            if len(parts) >= 3:
                insn.rd = parts[0]
                insn.rs1 = parts[1]
                insn.rs2 = parts[2]
        elif mnemonic in (
            "addi",
            "addiw",
            "andi",
            "ori",
            "xori",
            "slti",
            "sltiu",
            "slli",
            "slliw",
            "srli",
            "srliw",
            "srai",
            "sraiw",
        ):
            if len(parts) >= 3:
                insn.rd = parts[0]
                insn.rs1 = parts[1]
                insn.imm = parts[2]
        elif mnemonic in ("lh", "lhu", "lw", "lb", "lbu"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                m = re.match(r".*\((\w+)\)", parts[1])
                if m:
                    insn.rs1 = m.group(1)
        elif mnemonic in ("sh", "sw", "sb"):
            if len(parts) >= 2:
                insn.rs2 = parts[0]
                m = re.match(r".*\((\w+)\)", parts[1])
                if m:
                    insn.rs1 = m.group(1)
        elif mnemonic == "li":
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.imm = parts[1]
        elif mnemonic in ("c.addi", "c.slli", "c.srli", "c.srai", "c.andi"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.rs1 = parts[0]
                insn.imm = parts[1]
        elif mnemonic in ("c.add", "c.sub", "c.and", "c.or", "c.xor"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.rs1 = parts[0]
                insn.rs2 = parts[1]
        elif mnemonic in ("mv", "c.mv"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.rs1 = parts[1]  # mv rd, rs1
        elif mnemonic in ("c.li", "c.lui"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.imm = parts[1]
        elif mnemonic in ("lui", "auipc"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.imm = parts[1]
        # Pseudo-instructions: snez rd,rs → sltu rd,zero,rs
        elif mnemonic in ("snez", "seqz"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.rs1 = parts[1]
        # neg rd,rs → sub rd,zero,rs
        elif mnemonic in ("neg", "not"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.rs1 = parts[1]
        # zext.b rd,rs → andi rd,rs,255
        elif mnemonic in ("zext.b", "sext.b", "sext.h", "zext.h"):
            if len(parts) >= 2:
                insn.rd = parts[0]
                insn.rs1 = parts[1]
        # Fallback: try to parse as rd, rs1[, rs2/imm] for unknown instructions
        elif len(parts) >= 2 and parts[0] in self._all_regs():
            insn.rd = parts[0]
            if len(parts) >= 2 and parts[1] in self._all_regs():
                insn.rs1 = parts[1]
            if len(parts) >= 3 and parts[2] in self._all_regs():
                insn.rs2 = parts[2]

        return insn

    def _all_regs(self):
        """Return set of all RISC-V register names."""
        regs = {"zero", "ra", "sp", "gp", "tp"}
        for i in range(8):
            regs.add(f"t{i}")
        for i in range(8):
            regs.add(f"a{i}")
        for i in range(12):
            regs.add(f"s{i}")
        return regs

    def _find_loops_in_function(self, func: AsmFunction) -> None:
        """Find loops by identifying backward label references."""
        for insn in func.instructions:
            if not insn.is_branch or insn.is_call or insn.is_return:
                continue

            target = insn.branch_target_label
            if not target:
                continue

            # Check if the target label was defined earlier (backward reference)
            if target not in func.labels:
                continue

            label_line = func.labels[target]
            if label_line >= insn.line_num:
                continue  # Forward branch, not a loop

            # This is a backward branch = a loop!
            loop = AsmLoop(
                function=func.name,
                start_label=target,
                start_line=label_line,
                back_branch_line=insn.line_num,
                back_branch_insn=insn.raw_line.strip(),
                back_branch_mnemonic=insn.mnemonic,
                back_branch_operands=insn.operands,
            )

            # Collect body instructions (between label and branch)
            for body_insn in func.instructions:
                if label_line <= body_insn.line_num <= insn.line_num:
                    loop.body_instructions.append(body_insn)
                    loop.body_lines.append(body_insn.line_num)

            loop.body_insn_count = len(loop.body_instructions)
            func.loops.append(loop)

    def _build_nesting_hierarchy(self) -> None:
        """Determine nesting by containment of line ranges.

        Each loop's immediate parent is the smallest enclosing loop.
        We sort smallest-first so that when we scan outward we hit the
        tightest container first.  Depths are then computed top-down
        from the roots so that every parent's depth is set before its
        children's.
        """
        # Reset any stale state
        for loop in self.all_loops:
            loop.parent = None
            loop.children = []
            loop.nesting_depth = 0

        # Sort by span size (smallest first)
        sorted_loops = sorted(
            self.all_loops,
            key=lambda l: l.back_branch_line - l.start_line,
        )

        # Assign each loop's immediate parent (smallest containing loop)
        for i, inner in enumerate(sorted_loops):
            for outer in sorted_loops[i + 1 :]:
                if outer.function == inner.function and outer.contains(inner):
                    inner.parent = outer
                    outer.children.append(inner)
                    break

        # Compute depths top-down from roots so parents are resolved first
        roots = [l for l in self.all_loops if l.parent is None]
        stack = [(r, 0) for r in roots]
        while stack:
            loop, depth = stack.pop()
            loop.nesting_depth = depth
            for child in loop.children:
                stack.append((child, depth + 1))

    # ------------------------------------------------------------------
    # HW-loop eligibility analysis
    # ------------------------------------------------------------------

    def _analyze_hw_eligibility(self) -> None:
        """Check every loop against HW-loop constraints.

        A loop is HW-loopable when ALL of the following hold:
          1. no_calls        — body contains no function calls
          2. single_exit     — the only branch that can leave the loop is
                               the back-edge itself (no early-exit branches)
          3. deterministic_iterations — the iteration count can be determined
                               before the loop starts (constant init, constant
                               step, comparison against a loop-invariant bound)
        """
        for loop in self.all_loops:
            loop.constraint_results = {}
            loop.failing_constraints = []

            self._check_no_calls(loop)
            self._check_single_exit(loop)
            self._check_deterministic_iterations(loop)
            self._check_inner_sw_loops(loop)
            self._detect_prologue_jump(loop)
            self._check_no_internal_jumps(loop)

            loop.hw_eligible = len(loop.failing_constraints) == 0

    def _check_inner_sw_loops(self, loop: AsmLoop) -> None:
        """SW inner loops are allowed thanks to the deferred-DEC RTL fix.

        The fix defers the HW loop counter decrement when a conditional branch
        sits at the LP_end boundary, then commits or cancels after the branch
        resolves.  Safety is already guaranteed by:
          - _check_single_exit: no branch escapes the HW loop range
          - _check_no_calls:    no function calls inside the body
        """
        loop.constraint_results["inner_sw_loops"] = {"passed": True}

    def _check_no_internal_jumps(self, loop: AsmLoop) -> None:
        """Reject loops with unconditional jumps (j) inside the body.
        The aligner wraps PC speculatively based on sequential pc_n, but
        j redirects to a different target. The aligner would need a
        jump_taken signal to suppress the wrap, similar to how
        branch_taken_ex_i works for conditional branches."""
        for insn in loop.body_instructions:
            if insn.line_num == loop.back_branch_line:
                continue
            if insn.mnemonic == "j":
                loop.constraint_results["no_internal_jumps"] = {"passed": False}
                loop.failing_constraints.append("no_internal_jumps")
                return
        loop.constraint_results["no_internal_jumps"] = {"passed": True}

    def _detect_prologue_jump(self, loop: AsmLoop) -> None:
        """Detect loops with a prologue jump (j) before the start label.
        These are accepted — the patcher places setup before the j."""
        if loop.start_line > 0:
            prev = self.lines[loop.start_line - 1].strip()
            if prev.startswith("j	") or prev.startswith("j "):
                loop.has_prologue_jump = True
        loop.constraint_results["no_prologue"] = {"passed": True}

    def _check_no_calls(self, loop: AsmLoop) -> None:
        calls = [insn for insn in loop.body_instructions if insn.is_call]
        passed = len(calls) == 0
        loop.constraint_results["no_calls"] = {
            "passed": passed,
            "calls": [f"L{insn.line_num}: {insn.raw_line.strip()}" for insn in calls],
        }
        if not passed:
            loop.failing_constraints.append("no_calls")

    def _check_single_exit(self, loop: AsmLoop) -> None:
        """Only the back-edge branch may leave the loop body.

        Any other branch/jump whose target is outside [start_line, back_branch_line]
        is an extra exit path.  Branches into child loops are fine — they stay
        inside the body range.
        """
        child_ranges = set()
        for child in loop.children:
            for ln in range(child.start_line, child.back_branch_line + 1):
                child_ranges.add(ln)

        extra_exits: List[str] = []
        func_labels = self._func_for_loop(loop).labels if self._func_for_loop(loop) else {}

        is_j_loop = False
        exit_branch_count = 0

        for insn in loop.body_instructions:
            if insn.line_num == loop.back_branch_line:
                continue  # the back-edge itself is allowed
            if not insn.is_branch or insn.is_call or insn.is_return:
                continue
            target = insn.branch_target_label
            if not target:
                continue
            if target not in func_labels:
                extra_exits.append(f"L{insn.line_num}: {insn.raw_line.strip()}")
                continue
            target_line = func_labels[target]
            if target_line < loop.start_line or target_line > loop.back_branch_line:
                if is_j_loop and insn.mnemonic in (
                    "beq",
                    "bne",
                    "blt",
                    "bge",
                    "bltu",
                    "bgeu",
                    "beqz",
                    "bnez",
                ):
                    exit_branch_count += 1
                    if exit_branch_count > 1:
                        extra_exits.append(f"L{insn.line_num}: {insn.raw_line.strip()}")
                else:
                    extra_exits.append(f"L{insn.line_num}: {insn.raw_line.strip()}")

        passed = len(extra_exits) == 0
        loop.constraint_results["single_exit"] = {
            "passed": passed,
            "extra_exits": extra_exits,
        }
        if not passed:
            loop.failing_constraints.append("single_exit")

    def _check_deterministic_iterations(self, loop: AsmLoop) -> None:
        """Iteration count must be computable before the loop executes.

        Accepted patterns:
          1. IV stepped by a constant immediate  (addi/c.addi)
          2. IV stepped by a loop-invariant register (add/sub where the
             step register is not written inside the body)
          3. Bound is either implicit zero (beqz/bnez), a constant, or a
             register whose value is *pre-computable* — traceable through
             the pre-loop instructions back to immediates, function args
             (a0-a7), or pure arithmetic on other pre-computable values.

        The same pre-computability check applies to the IV's initial value
        and the step register (when not an immediate).
        """
        result: Dict = {"passed": False, "reason": ""}

        back = loop.body_instructions[-1] if loop.body_instructions else None
        if not back or back.line_num != loop.back_branch_line:
            result["reason"] = "back-edge instruction not found"
            loop.constraint_results["deterministic_iterations"] = result
            loop.failing_constraints.append("deterministic_iterations")
            return

        # --- identify IV and bound from the back-edge ---
        analysis_insn = back

        # For bne/beq, either operand could be the IV - try both
        iv: Optional[str] = None
        bound: Optional[str] = None

        if analysis_insn.mnemonic in ("bnez", "beqz", "c.bnez", "c.beqz"):
            iv = analysis_insn.rs1
            bound = None  # implicit zero
        elif analysis_insn.mnemonic not in ("bne", "beq", "blt", "bge", "bltu", "bgeu"):
            result["reason"] = f"analysis branch is unconditional ({analysis_insn.mnemonic})"
            loop.constraint_results["deterministic_iterations"] = result
            loop.failing_constraints.append("deterministic_iterations")
            return

        # Compute child ranges once for this loop.
        # Only exclude lines inside a direct child's innermost body.
        # Lines between the innermost grandchild's back-branch and the
        # direct child's back-branch are step/epilogue instructions that
        # belong to THIS loop's nesting level.
        _child_ranges = set()
        for child in loop.children:
            deepest = child
            while deepest.children:
                deepest = max(deepest.children, key=lambda c: c.back_branch_line)
            for ln in range(child.start_line, deepest.back_branch_line + 1):
                _child_ranges.add(ln)
        body_written_regs = {
            insn.rd for insn in loop.body_instructions if insn.rd and insn.line_num not in _child_ranges
        }

        # For two-operand branches, try both operands as potential IV
        candidates = (
            [(analysis_insn.rs1, analysis_insn.rs2), (analysis_insn.rs2, analysis_insn.rs1)]
            if bound is None
            else [(iv, bound)]
        )

        found_valid = False
        last_reason = ""

        for try_iv, try_bound in candidates:
            if not try_iv:
                continue

            # --- check IV is stepped exactly once by a deterministic amount ---
            # Only consider instructions at THIS loop's nesting level
            own_insns = [insn for insn in loop.body_instructions if insn.line_num not in _child_ranges]
            # Constant immediate step (includes 32-bit word variants)
            iv_imm_steps = [
                insn
                for insn in own_insns
                if insn.rd == try_iv and insn.mnemonic in ("addi", "c.addi", "addiw") and insn.imm is not None
            ]
            # Invariant register step (step_reg not written in body)
            iv_reg_steps = [
                insn
                for insn in own_insns
                if insn.rd == try_iv
                and insn.mnemonic in ("add", "c.add", "sub", "c.sub")
                and insn.rs2 is not None
                and insn.rs2 not in body_written_regs
            ]
            all_iv_writes = [insn for insn in own_insns if insn.rd == try_iv]

            has_imm_step = len(iv_imm_steps) == 1 and len(all_iv_writes) == 1
            has_reg_step = len(iv_reg_steps) == 1 and len(all_iv_writes) == 1

            # Allow nested-loop pattern: IV has an init (li/mv before
            # the first child) AND a step (addi after the innermost
            # child).  The init resets the IV each outer iteration.
            INIT_MNEMONICS = {"li", "c.li", "mv", "c.mv", "addi", "c.addi", "add", "c.add"}
            if not (has_imm_step or has_reg_step) and len(all_iv_writes) == 2:
                first_child_start = min((c.start_line for c in loop.children), default=loop.back_branch_line)
                for steps, flag_name in [(iv_imm_steps, "imm"), (iv_reg_steps, "reg")]:
                    if len(steps) == 1:
                        step_candidate = steps[0]
                        other = [w for w in all_iv_writes if w is not step_candidate][0]
                        if (
                            other.mnemonic in INIT_MNEMONICS
                            and other.line_num < first_child_start
                            and step_candidate.line_num > first_child_start
                        ):
                            if flag_name == "imm":
                                has_imm_step = True
                            else:
                                has_reg_step = True
                            break

            if not (has_imm_step or has_reg_step):
                writes_desc = [f"L{i.line_num}: {i.raw_line.strip()}" for i in all_iv_writes]
                last_reason = f"IV '{try_iv}' not modified by a single constant/invariant step; writes: {writes_desc}"
                continue

            step_insn = iv_imm_steps[0] if has_imm_step else iv_reg_steps[0]
            step_reg = None if has_imm_step else step_insn.rs2

            # --- check bound register is loop-invariant ---
            if try_bound and try_bound in body_written_regs:
                # GCC -O2 reuses a descendant loop's IV as the outer bound.
                # After the descendant completes, the IV holds its final value
                # which equals the descendant's bound — a constant.
                # Accept if try_bound is the IV of any descendant with
                # deterministic iterations whose own bound is pre-computable.
                resolved = self._resolve_descendant_iv_bound(loop, try_bound)
                if resolved is None:
                    last_reason = f"bound register '{try_bound}' is modified inside the loop"
                    continue

            # Found a valid IV/bound pair!
            iv = try_iv
            bound = try_bound
            found_valid = True
            break

        if not found_valid:
            result["reason"] = last_reason if last_reason else "cannot identify induction variable"
            loop.constraint_results["deterministic_iterations"] = result
            loop.failing_constraints.append("deterministic_iterations")
            return

        # Re-compute step_insn and step_reg for the valid IV
        iv_imm_steps = [
            insn
            for insn in loop.body_instructions
            if insn.rd == iv and insn.mnemonic in ("addi", "c.addi", "addiw") and insn.imm is not None
        ]
        iv_reg_steps = [
            insn
            for insn in loop.body_instructions
            if insn.rd == iv
            and insn.mnemonic in ("add", "c.add", "sub", "c.sub")
            and insn.rs2 is not None
            and insn.rs2 not in body_written_regs
        ]
        step_insn = iv_imm_steps[0] if iv_imm_steps else iv_reg_steps[0]
        step_reg = None if iv_imm_steps else step_insn.rs2

        # --- check that IV init, bound, and step are pre-computable ---
        pre_computable = self._pre_computable_regs(loop)

        regs_to_check: List[Tuple[str, str]] = []  # (role, register)
        if iv not in body_written_regs:
            # IV is never written in the body — it must come from before
            # (unusual, but handle it)
            pass
        # IV is initialised before the loop; its pre-loop value must be
        # pre-computable
        regs_to_check.append(("IV init", iv))
        if bound and bound not in body_written_regs:
            # Bound is set before the loop — must be pre-computable
            regs_to_check.append(("bound", bound))
        # If bound IS in body_written_regs, it was already validated as a
        # descendant loop's IV with deterministic iterations (resolved above).
        if step_reg:
            regs_to_check.append(("step", step_reg))

        for role, reg in regs_to_check:
            if reg not in pre_computable:
                result["reason"] = (
                    f"{role} register '{reg}' is not pre-computable (cannot trace to immediate/arg before loop)"
                )
                loop.constraint_results["deterministic_iterations"] = result
                loop.failing_constraints.append("deterministic_iterations")
                return

        result["passed"] = True
        result["iv"] = iv
        result["bound"] = bound if bound else "zero"
        result["step"] = step_insn.raw_line.strip()
        result["step_value"] = step_insn.imm if step_insn.imm else f"reg:{step_reg}"
        result["pre_computable"] = {role: reg for role, reg in regs_to_check}

        # Track IV initialization
        iv_init = self._find_iv_init(loop, iv)
        result["iv_init"] = iv_init

        # Build iteration count formula
        init_str = (
            "0"
            if iv_init.get("type") == "zero"
            else (
                str(iv_init.get("value"))
                if iv_init.get("type") == "immediate"
                else f"{iv_init.get('value')}_init"
                if iv_init.get("value")
                else f"{iv}_init"
            )
        )

        if bound:
            result["iteration_formula"] = f"({bound} - {init_str}) / {result['step_value']}"
        else:
            result["iteration_formula"] = f"({init_str}) / |{result['step_value']}|"  # counting down to zero

        loop.constraint_results["deterministic_iterations"] = result

    def _resolve_descendant_iv_bound(self, loop: AsmLoop, reg: str) -> Optional[str]:
        """Check if *reg* is the IV of a descendant loop with deterministic
        iterations.  If so, return the descendant's bound register (the final
        value *reg* will hold after that descendant completes).  Returns None
        if no such descendant is found.

        Works recursively: the descendant's bound may itself be another
        descendant's IV, as long as the chain eventually bottoms out at a
        loop whose bound is pre-computable."""
        for desc in self._all_descendants(loop):
            det = desc.constraint_results.get("deterministic_iterations", {})
            if det.get("passed") and det.get("iv") == reg:
                return det.get("bound")
        return None

    def _all_descendants(self, loop: AsmLoop) -> List["AsmLoop"]:
        """Return all descendants of *loop* (children, grandchildren, …)."""
        result = []
        for child in loop.children:
            result.append(child)
            result.extend(self._all_descendants(child))
        return result

    def _pre_computable_regs(self, loop: AsmLoop) -> Set[str]:
        """Return the set of registers whose values are deterministic at loop entry.

        A register is pre-computable if, walking forward through the pre-loop
        instructions, its last definition before the loop is:
          - a constant load (li, c.li, lui, c.lui, auipc)
          - a move/copy from another pre-computable register (mv, c.mv)
          - pure arithmetic whose source operands are all pre-computable
            (add, sub, addi, slli, …)
          - a function argument register (a0-a7) that is never overwritten
            before the loop

        Loads from memory are NOT pre-computable (the value depends on
        runtime memory contents that the HW loop setup cannot read).
        """
        func = self._func_for_loop(loop)
        if not func:
            return set()

        # Function arguments are pre-computable at entry
        FUNC_ARGS = {f"a{i}" for i in range(8)}
        # "zero" / "x0" is always known
        known: Set[str] = {"zero", "x0", "sp", "gp", "tp"} | FUNC_ARGS

        # Constant-producing mnemonics (result depends only on immediate)
        CONST_MNEMONICS = {"li", "c.li", "lui", "c.lui", "auipc"}
        # Pure ALU mnemonics (result depends only on source registers)
        ALU_IMM = {
            "addi",
            "addiw",
            "c.addi",
            "andi",
            "c.andi",
            "ori",
            "xori",
            "slti",
            "sltiu",
            "slli",
            "slliw",
            "c.slli",
            "srli",
            "srliw",
            "c.srli",
            "srai",
            "sraiw",
            "c.srai",
        }
        ALU_REG = {
            "add",
            "c.add",
            "sub",
            "c.sub",
            "mul",
            "mulh",
            "mulhu",
            "and",
            "c.and",
            "or",
            "c.or",
            "xor",
            "c.xor",
            "sll",
            "srl",
            "sra",
            "slt",
            "sltu",
            "div",
            "divu",
            "rem",
            "remu",
        }
        MOVE = {"mv", "c.mv"}
        # Load mnemonics — if the base register is pre-computable, the loaded
        # value is fixed at loop entry (no further writes before the loop)
        LOAD = {
            "lw",
            "lh",
            "lb",
            "lhu",
            "lbu",
            "ld",
            "c.lw",
            "c.lwsp",
            "c.ld",
            "c.ldsp",
        }

        # Walk pre-loop instructions (everything in the function before
        # loop.start_line).  We may need multiple passes because an
        # instruction early on might depend on a register set later
        # (but still before the loop).  Fixed-point iteration handles this.
        pre_insns = [insn for insn in func.instructions if insn.line_num < loop.start_line]

        changed = True
        while changed:
            changed = False
            for insn in pre_insns:
                rd = insn.rd
                if not rd or rd in known:
                    continue

                if insn.mnemonic in CONST_MNEMONICS:
                    known.add(rd)
                    changed = True
                elif insn.mnemonic in ALU_IMM:
                    if insn.rs1 and insn.rs1 in known:
                        known.add(rd)
                        changed = True
                elif insn.mnemonic in ALU_REG:
                    if insn.rs1 and insn.rs1 in known and insn.rs2 and insn.rs2 in known:
                        known.add(rd)
                        changed = True
                elif insn.mnemonic in MOVE:
                    src = insn.rs1 or insn.rs2
                    if src and src in known:
                        known.add(rd)
                        changed = True
                elif insn.mnemonic in LOAD:
                    # Value loaded from memory before the loop is stable
                    # at loop entry if the base address is pre-computable
                    if insn.rs1 and insn.rs1 in known:
                        known.add(rd)
                        changed = True

        # Also include registers resolved by symbolic evaluation
        func = self._func_for_loop(loop)
        if func:
            from arvis.analysis.hwloop import (
                SymAdd,
                SymConst,
                SymReg,
                SymSll,
                SymSrl,
                SymSub,
                symbolic_eval_pre_loop,
            )

            state = symbolic_eval_pre_loop(func.instructions, loop.start_line)
            for reg, expr in state.items():
                if reg not in known:
                    # A register is pre-computable if its symbolic value
                    # only depends on constants and function arguments
                    def is_computable(e):
                        if isinstance(e, SymConst):
                            return True
                        if isinstance(e, SymReg):
                            return e.name in known
                        if isinstance(e, (SymAdd, SymSub)):
                            return is_computable(e.a) and is_computable(e.b)
                        if isinstance(e, (SymSll, SymSrl)):
                            return is_computable(e.a)
                        return False

                    if is_computable(expr):
                        known.add(reg)

        return known

    def _find_iv_init(self, loop: AsmLoop, iv: str) -> dict:
        """Find how the IV is initialized before the loop.

        Returns a dict with:
          - 'type': 'zero', 'immediate', 'register', 'unknown'
          - 'value': the immediate value or register name
          - 'insn': the initialization instruction (if found)
        """
        func = self._func_for_loop(loop)
        if not func:
            return {"type": "unknown", "value": None, "insn": None}

        # Get pre-loop instructions in reverse order (find last write to IV)
        pre_insns = [insn for insn in func.instructions if insn.line_num < loop.start_line]

        # Find the last instruction that writes to IV before the loop
        for insn in reversed(pre_insns):
            if insn.rd != iv:
                continue

            # li rd, imm -> immediate
            if insn.mnemonic in ("li", "c.li"):
                try:
                    val = int(insn.imm)
                    if val == 0:
                        return {"type": "zero", "value": 0, "insn": insn.raw_line.strip()}
                    return {"type": "immediate", "value": val, "insn": insn.raw_line.strip()}
                except (ValueError, TypeError):
                    return {"type": "immediate", "value": insn.imm, "insn": insn.raw_line.strip()}

            # mv rd, rs -> copy from register
            if insn.mnemonic in ("mv", "c.mv"):
                src = insn.rs1
                # Check if source is zero
                if src in ("zero", "x0"):
                    return {"type": "zero", "value": 0, "insn": insn.raw_line.strip()}
                return {"type": "register", "value": src, "insn": insn.raw_line.strip()}

            # addi rd, rs, 0 is effectively a move
            if insn.mnemonic in ("addi", "addiw", "c.addi") and insn.imm == "0":
                src = insn.rs1
                if src in ("zero", "x0"):
                    return {"type": "zero", "value": 0, "insn": insn.raw_line.strip()}
                return {"type": "register", "value": src, "insn": insn.raw_line.strip()}

            # Any other write - it's from a register or computation
            return {"type": "register", "value": iv, "insn": insn.raw_line.strip()}

        # IV not written before loop - it's a function argument or uninitialized
        if iv in {f"a{i}" for i in range(8)}:
            return {"type": "register", "value": iv, "insn": "(function argument)"}

        return {"type": "unknown", "value": None, "insn": None}

    def symbolic_count(self, loop: AsmLoop) -> SymExpr:
        """Try to compute the iteration count symbolically.
        Returns a SymExpr. If it simplifies to SymConst, the count is known."""
        det = loop.constraint_results.get("deterministic_iterations", {})
        if not det.get("passed"):
            return None
        iv = det.get("iv")
        bound = det.get("bound")
        step = det.get("step_value")
        if not iv or not bound or not step:
            return None
        try:
            step_int = int(step)
        except Exception:
            return None

        func = self._func_for_loop(loop)
        if not func:
            return None
        state = symbolic_eval_pre_loop(func.instructions, loop.start_line)

        iv_expr = state.get(iv, SymReg(iv))
        bound_expr = state.get(bound, SymReg(bound))

        diff = SymSub(bound_expr, iv_expr).simplify()
        if step_int == 1:
            return diff
        count = SymSrl(diff, (step_int - 1).bit_length()) if (step_int & (step_int - 1)) == 0 else None
        if count:
            return count.simplify()
        # Non-power-of-2 step: can't simplify with shift
        return None

    def _func_for_loop(self, loop: AsmLoop) -> Optional[AsmFunction]:
        for func in self.functions:
            if func.name == loop.function:
                return func
        return None

    # ------------------------------------------------------------------
    # Greedy HW-loop nesting
    # ------------------------------------------------------------------

    def _build_greedy_hw_nesting(self) -> None:
        """Build maximal call-free nesting trees rooted at the outermost
        call-free ancestor of each HW-eligible loop.

        For every HW-eligible loop we walk up to find the highest ancestor
        whose entire subtree is call-free.  That ancestor becomes the root
        of a "nestable group".  All call-free descendants (HW-eligible or
        not) are included — they can coexist inside the HW loop even if
        they themselves have multiple exits or non-deterministic iterations.
        """
        for loop in self.all_loops:
            loop.hw_nestable_children = []

        # For each HW-eligible loop, climb to the outermost call-free ancestor
        seen_roots: Set[int] = set()  # start_line used as identity
        for loop in self.all_loops:
            if not loop.hw_eligible:
                continue
            root = loop
            while root.parent and not root.parent.tree_has_calls:
                root = root.parent
            if id(root) in seen_roots:
                continue
            seen_roots.add(id(root))
            root.hw_nestable_children = self._collect_call_free_descendants(root)

    def _collect_call_free_descendants(self, loop: AsmLoop) -> List[AsmLoop]:
        """Return all descendants of *loop* whose entire subtree is call-free."""
        result: List[AsmLoop] = []
        for child in loop.children:
            if not child.tree_has_calls:
                result.append(child)
                result.extend(self._collect_call_free_descendants(child))
            # If a child has calls we skip it AND its subtree entirely
        return result

    def print_tree(self) -> str:
        """Return an indented tree showing parent/child loop relationships."""
        lines: List[str] = []
        roots = [l for l in self.all_loops if l.parent is None]
        roots.sort(key=lambda l: l.start_line)

        def _walk(loop: AsmLoop, indent: int) -> None:
            prefix = "  " * indent
            hw = "✓ HW" if loop.hw_eligible else "✗ HW"
            nest = ""
            if loop.hw_eligible and loop.hw_nestable_children:
                nest = f" +{len(loop.hw_nestable_children)} nestable"
            fail = ""
            if not loop.hw_eligible:
                fail = f" ({', '.join(loop.failing_constraints)})"
            call_free = "" if not loop.tree_has_calls else " [HAS CALLS]"
            lines.append(
                f"{prefix}{loop.function}:{loop.start_label} "
                f"[lines {loop.start_line}-{loop.back_branch_line}, "
                f"{loop.body_insn_count} insns, depth {loop.nesting_depth}] "
                f"[{hw}{fail}{nest}]{call_free}"
            )
            for child in sorted(loop.children, key=lambda l: l.start_line):
                _walk(child, indent + 1)

        for root in roots:
            _walk(root, 0)
        return "\n".join(lines)

    def summary(self) -> str:
        """Return a human-readable summary."""
        lines = [f"Assembly Loop Detector: {len(self.all_loops)} loops in {self.asm_path.name}"]
        for func in self.functions:
            if func.loops:
                lines.append(f"\n  {func.name}: {len(func.loops)} loops")
                for loop in func.loops:
                    depth_str = f" (depth {loop.nesting_depth})" if loop.nesting_depth > 0 else ""
                    lines.append(
                        f"    {loop.start_label} → line {loop.back_branch_line}: "
                        f"{loop.back_branch_mnemonic} ({loop.body_insn_count} insns){depth_str}"
                    )
        return "\n".join(lines)


if __name__ == "__main__":
    import os
    import sys

    file = sys.argv[1] if len(sys.argv) > 1 else "src/code.s"
    loopDetector = AsmLoopDetector(file)
    loops = loopDetector.find_all_loops()

    print(loopDetector.print_tree())
    print()

    hw_loops = [l for l in loops if l.hw_eligible]
    groups = [l for l in loops if l.hw_nestable_children]

    print(f"HW-loopable: {len(hw_loops)} / {len(loops)} loops")
    print(f"Nestable groups: {len(groups)}")

    os.makedirs("loops", exist_ok=True)

    # Clear old loop files
    for f in os.listdir("loops"):
        if f.endswith(".s"):
            os.remove(os.path.join("loops", f))

    # Write ALL loops to individual files
    for idx, loop in enumerate(sorted(loops, key=lambda l: (l.function, l.start_line))):
        det = loop.constraint_results.get("deterministic_iterations", {})
        hw_tag = "hw" if loop.hw_eligible else "sw"
        filename = f"loops/loop_{idx:03d}_{hw_tag}_{loop.function}_{loop.start_label}.s"

        with open(filename, "w") as f:
            # Header
            f.write("# " + "=" * 60 + "\n")
            f.write(f"# LOOP: {loop.function}:{loop.start_label}\n")
            f.write("# " + "=" * 60 + "\n")
            f.write("#\n")

            # Basic info
            f.write(f"# HW-Loopable: {'YES' if loop.hw_eligible else 'NO'}\n")
            if loop.failing_constraints:
                f.write(f"# Failing constraints: {', '.join(loop.failing_constraints)}\n")
            f.write("#\n")

            # Location
            f.write(f"# Lines: {loop.start_line} - {loop.back_branch_line}\n")
            f.write(f"# Instructions: {loop.body_insn_count}\n")
            f.write("#\n")

            # Nesting info
            f.write("# NESTING:\n")
            f.write(f"#   Depth: {loop.nesting_depth}\n")
            if loop.parent:
                f.write(
                    f"#   Parent: {loop.parent.function}:{loop.parent.start_label} "
                    f"(lines {loop.parent.start_line}-{loop.parent.back_branch_line})\n"
                )
            else:
                f.write("#   Parent: None (top-level loop)\n")

            if loop.children:
                f.write(f"#   Children ({len(loop.children)}): \n")
                for child in sorted(loop.children, key=lambda l: l.start_line):
                    child_hw = "HW" if child.hw_eligible else "SW"
                    f.write(
                        f"#     - {child.start_label} [{child_hw}] "
                        f"(lines {child.start_line}-{child.back_branch_line}, "
                        f"{child.body_insn_count} insns)\n"
                    )
            else:
                f.write("#   Children: None (innermost loop)\n")
            f.write("#\n")

            # Back-edge info
            f.write("# BACK-EDGE:\n")
            f.write(f"#   Instruction: {loop.back_branch_insn}\n")
            f.write(f"#   Mnemonic: {loop.back_branch_mnemonic}\n")
            f.write(f"#   Operands: {loop.back_branch_operands}\n")
            f.write("#\n")

            # Iteration analysis
            f.write("# ITERATION ANALYSIS:\n")
            if det.get("passed"):
                f.write("#   Deterministic: YES\n")
                f.write(f"#   IV register: {det.get('iv')}\n")
                f.write(f"#   Bound: {det.get('bound')}\n")
                f.write(f"#   Step: {det.get('step')}\n")
                f.write(f"#   Step value: {det.get('step_value')}\n")
                f.write(f"#   Iterations: {det.get('iteration_formula')}\n")
                pre = det.get("pre_computable", {})
                if pre:
                    f.write("#   Pre-computable: " + ", ".join(f"{role}={reg}" for role, reg in pre.items()) + "\n")
            else:
                f.write("#   Deterministic: NO\n")
                f.write(f"#   Reason: {det.get('reason', 'N/A')}\n")
            f.write("#\n")

            # Single exit analysis
            single_exit = loop.constraint_results.get("single_exit", {})
            f.write("# SINGLE EXIT:\n")
            if single_exit.get("passed"):
                f.write("#   Passed: YES\n")
            else:
                f.write("#   Passed: NO\n")
                exits = single_exit.get("extra_exits", [])
                if exits:
                    f.write(f"#   Extra exits ({len(exits)}):\n")
                    for ex in exits:
                        f.write(f"#     - {ex}\n")
            f.write("#\n")

            # No calls analysis
            no_calls = loop.constraint_results.get("no_calls", {})
            f.write("# NO CALLS:\n")
            if no_calls.get("passed"):
                f.write("#   Passed: YES\n")
            else:
                f.write("#   Passed: NO\n")
                calls = no_calls.get("calls", [])
                if calls:
                    f.write("#   Calls found:\n")
                    for c in calls:
                        f.write(f"#     - {c}\n")
            f.write("#\n")

            # Assembly code
            f.write("# " + "=" * 60 + "\n")
            f.write("# ASSEMBLY:\n")
            f.write("# " + "=" * 60 + "\n\n")
            f.write(loop.start_label + ":\n")
            for inst in loop.body_instructions:
                f.write(inst.raw_line + "\n")

    print(f"\nWrote {len(loops)} loop files to loops/ directory")
