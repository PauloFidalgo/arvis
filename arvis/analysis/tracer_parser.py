"""
Parser for execution traces.

Supports three formats:
1. Full cv32e40p tracer format (trace_core_*.log with register values)
2. Simple CSV format (cycle,pc,instr)
3. Spike commit log format (-l --log-commits)

The cv32e40p tracer provides:
- Cycle-accurate timing
- Register reads/writes with values (x8:0x... reads, x8=0x... writes)
- Memory accesses (PA:0x... store:0x... / load:0x...)

The Spike commit log provides:
- PC + instruction bits + disassembly
- Register writebacks (xN 0xVAL)
- Memory accesses (mem 0xADDR 0xVAL)
- Source register values are RECONSTRUCTED via shadow register file
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .registers import normalize_reg as _canonical_normalize_reg

STORE_MNEMONICS = frozenset(
    {
        "sb",
        "sh",
        "sw",
        "sd",
        "c.sw",
        "c.swsp",
        "c.sd",
        "c.sdsp",
        "c.fsw",
        "c.fswsp",
        "c.fsd",
        "c.fsdsp",
    }
)

LOAD_MNEMONICS = frozenset(
    {
        "lb",
        "lbu",
        "lh",
        "lhu",
        "lw",
        "lwu",
        "ld",
        "c.lw",
        "c.lwsp",
        "c.ld",
        "c.ldsp",
        "c.flw",
        "c.flwsp",
        "c.fld",
        "c.fldsp",
    }
)


def normalize_reg(name: str) -> Optional[str]:
    """Normalize register name to xN format. Returns None if not a register."""
    if not name:
        return None
    return _canonical_normalize_reg(name)


def _parse_imm(s: str) -> Optional[int]:
    """Parse immediate value from various formats."""
    s = s.strip()
    if not s:
        return None
    try:
        if s.startswith("0x") or s.startswith("-0x"):
            return int(s, 16)
        return int(s)
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Online value tracker (O(1) memory per operand)
# ═══════════════════════════════════════════════════════════════════════════


class OnlineValueTracker:
    """
    Track register value constancy with O(1) memory.

    Instead of storing ALL values (O(n_executions)), tracks:
    - Whether the value is always the same
    - First/constant value
    - Count of observations
    - Up to max_unique unique values (for variance estimation)
    """

    __slots__ = ("count", "first_value", "is_constant", "unique_values", "_max_unique")

    def __init__(self, max_unique: int = 16):
        self.count = 0
        self.first_value = 0
        self.is_constant = True
        self.unique_values: Set[int] = set()
        self._max_unique = max_unique

    def add(self, value: int):
        self.count += 1
        if self.count == 1:
            self.first_value = value
            self.unique_values.add(value)
        else:
            if self.is_constant and value != self.first_value:
                self.is_constant = False
            if len(self.unique_values) < self._max_unique:
                self.unique_values.add(value)

    def to_list(self) -> List[int]:
        """
        Produce compact list compatible with DynamicProfile.register_values.
        For constant: [val] (1 element, consumers check len or is_constant).
        For varying: list of unique values seen.
        """
        if self.is_constant:
            return [self.first_value]
        return list(self.unique_values)


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class TraceEntry:
    """Single instruction trace entry."""

    cycle: int
    pc: int
    raw_instr: int
    mnemonic: str = ""
    operands: List[str] = field(default_factory=list)
    reg_reads: Dict[str, int] = field(default_factory=dict)  # reg: value_before
    reg_writes: Dict[str, int] = field(default_factory=dict)  # reg: value_after
    mem_addr: Optional[int] = None
    mem_store: Optional[int] = None
    mem_load: Optional[int] = None
    is_compressed: bool = False


@dataclass
class TraceProfile:
    """Aggregated profile from trace."""

    entries: List[TraceEntry] = field(default_factory=list)
    pc_exec_counts: Dict[int, int] = field(default_factory=dict)
    # (pc, reg) -> list of values seen
    register_values: Dict[Tuple[int, str], List[int]] = field(default_factory=dict)
    total_cycles: int = 0
    total_instructions: int = 0
    # Memory accesses: (pc, addr, is_write)
    memory_accesses: List[Tuple[int, int, bool]] = field(default_factory=list)
    # Trace format that was parsed
    trace_format: str = "unknown"
    # Quality stats
    stats: Dict[str, int] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# Main parser
# ═══════════════════════════════════════════════════════════════════════════


class TracerParser:
    """
    Parser for execution traces.

    Auto-detects format from file content:
    - cv32e40p tracer: lines start with whitespace + time + cycle
    - Simple CSV: lines match "cycle,pc,instr"
    - Spike: lines start with "core"
    """

    # ── cv32e40p tracer format ──
    # "     130    61 00000150 4481  c.li  x9,0  x9=0x00000000"
    FULL_RE = re.compile(
        r"\s*(\d+)\s+"  # Time
        r"(\d+)\s+"  # Cycle
        r"([0-9a-fA-F]+)\s+"  # PC
        r"([0-9a-fA-F]+)\s+"  # Instr
        r"(\S+)"  # Mnemonic
        r"(.*)"  # Rest (operands + reg/mem)
    )
    REG_READ_RE = re.compile(r"(x\d+):0x([0-9a-fA-F]+)")
    REG_WRITE_RE = re.compile(r"(x\d+)=0x([0-9a-fA-F]+)")
    MEM_RE = re.compile(
        r"PA:0x([0-9a-fA-F]+)\s+"
        r"(?:store:0x([0-9a-fA-F]+)|load:0x([0-9a-fA-F]+))?"
    )

    # ── Simple CSV format ──
    SIMPLE_RE = re.compile(r"(\d+),([0-9a-fA-F]+),([0-9a-fA-F]+)")

    # ── Spike format ──
    # Disasm:  core   0: 0xPC (0xBITS) mnemonic operands
    # Commit:  core   0: PRIV 0xPC (0xBITS) [xN 0xVAL] [mem 0xADDR 0xVAL]
    SPIKE_DISASM_RE = re.compile(
        r"core\s+\d+:\s+0x([0-9a-f]+)\s+"
        r"\(0x([0-9a-f]+)\)\s+"
        r"(\S+)\s*(.*)"
    )
    SPIKE_COMMIT_RE = re.compile(
        r"core\s+\d+:\s+\d+\s+"
        r"0x([0-9a-f]+)\s+"
        r"\(0x([0-9a-f]+)\)\s*(.*)"
    )
    SPIKE_REG_WB_RE = re.compile(r"(x\d+)\s+0x([0-9a-f]+)")
    SPIKE_MEM_RE = re.compile(r"mem\s+0x([0-9a-f]+)(?:\s+0x([0-9a-f]+))?")

    def __init__(self):
        # Shadow register file for Spike source value reconstruction
        self._shadow_rf: Dict[str, int] = {}
        self._reset_shadow_rf()

    def _reset_shadow_rf(self):
        """Initialize shadow register file to all zeros."""
        self._shadow_rf = {f"x{i}": 0 for i in range(32)}

    # ──────────────────────────────────────────────────────────────────
    # Format detection
    # ──────────────────────────────────────────────────────────────────

    def _detect_format(self, path: Path) -> str:
        """Detect trace format by examining the first non-empty lines."""
        with open(path, "r", errors="replace") as f:
            for _ in range(50):  # Check first 50 lines
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                # Spike format: starts with "core"
                if line.startswith("core"):
                    return "spike"

                # Simple CSV: digit,hex,hex
                if self.SIMPLE_RE.match(line):
                    return "simple"

                # cv32e40p tracer: starts with whitespace + digits
                if self.FULL_RE.match(line):
                    return "full"

                # Skip header lines (e.g., "Time Cycle PC ...")
                if line.startswith("Time") or line.startswith("#"):
                    continue

        return "unknown"

    # ──────────────────────────────────────────────────────────────────
    # Main entry point
    # ──────────────────────────────────────────────────────────────────

    def parse_file(
        self,
        path: Path,
        blocks: Optional[Dict] = None,
        max_instructions: int = 0,
        store_entries: bool = True,
        progress_every: int = 500_000,
    ) -> TraceProfile:
        """
        Parse trace file and return profile.

        Args:
            path: Path to trace file
            blocks: If provided (for Spike), maps PCs to blocks for
                    register value tracking with block-level granularity
            max_instructions: Stop after N instructions (0 = unlimited)
            store_entries: If True, store all TraceEntry objects in
                          profile.entries (uses more memory for large traces)
            progress_every: Print progress every N instructions (0 = never)
        """
        path = Path(path)
        fmt = self._detect_format(path)

        if fmt == "spike":
            return self._parse_spike(
                path,
                blocks,
                max_instructions,
                store_entries,
                progress_every,
            )
        elif fmt == "simple":
            with open(path) as f:
                return self._parse_simple(f)
        elif fmt == "full":
            with open(path) as f:
                return self._parse_full(f)
        else:
            raise ValueError(
                f"Unknown trace format in {path}. Expected cv32e40p tracer, simple CSV, or Spike commit log."
            )

    # ──────────────────────────────────────────────────────────────────
    # Format 1: cv32e40p tracer
    # ──────────────────────────────────────────────────────────────────

    def _parse_full(self, f) -> TraceProfile:
        """
        Parse full cv32e40p tracer format.

        Already has register read values (x8:0x...) and write values
        (x8=0x...) directly in the trace — no shadow RF needed.
        """
        profile = TraceProfile(trace_format="cv32e40p_tracer")

        for line in f:
            line = line.strip()
            if not line or line.startswith("Time"):
                continue

            m = self.FULL_RE.match(line)
            if not m:
                continue

            cycle = int(m.group(2))
            pc = int(m.group(3), 16)
            instr = int(m.group(4), 16)
            mnemonic = m.group(5)
            rest = m.group(6)

            entry = TraceEntry(
                cycle=cycle,
                pc=pc,
                raw_instr=instr,
                mnemonic=mnemonic,
                is_compressed=(len(m.group(4)) <= 4),
            )

            # Parse operands
            parts = rest.split()
            if parts:
                entry.operands = parts[0].split(",") if parts else []

            # Parse register reads (x8:0x...)
            for rm in self.REG_READ_RE.finditer(rest):
                reg = rm.group(1)
                val = int(rm.group(2), 16)
                entry.reg_reads[reg] = val
                key = (pc, reg)
                if key not in profile.register_values:
                    profile.register_values[key] = []
                profile.register_values[key].append(val)

            # Parse register writes (x8=0x...)
            for rm in self.REG_WRITE_RE.finditer(rest):
                reg = rm.group(1)
                val = int(rm.group(2), 16)
                entry.reg_writes[reg] = val

            # Parse memory access
            mm = self.MEM_RE.search(rest)
            if mm:
                entry.mem_addr = int(mm.group(1), 16)
                if mm.group(2):
                    entry.mem_store = int(mm.group(2), 16)
                    profile.memory_accesses.append((pc, entry.mem_addr, True))
                if mm.group(3):
                    entry.mem_load = int(mm.group(3), 16)
                    profile.memory_accesses.append((pc, entry.mem_addr, False))

            profile.entries.append(entry)
            profile.pc_exec_counts[pc] = profile.pc_exec_counts.get(pc, 0) + 1
            profile.total_cycles = max(profile.total_cycles, cycle)

        profile.total_instructions = len(profile.entries)
        return profile

    # ──────────────────────────────────────────────────────────────────
    # Format 2: Simple CSV
    # ──────────────────────────────────────────────────────────────────

    def _parse_simple(self, f) -> TraceProfile:
        """Parse simple CSV format: cycle,pc,instr."""
        profile = TraceProfile(trace_format="simple_csv")

        for line in f:
            line = line.strip()
            if not line:
                continue
            m = self.SIMPLE_RE.match(line)
            if m:
                cycle = int(m.group(1))
                pc = int(m.group(2), 16)
                instr = int(m.group(3), 16)

                entry = TraceEntry(
                    cycle=cycle,
                    pc=pc,
                    raw_instr=instr,
                    is_compressed=(instr < 0x10000),
                )
                profile.entries.append(entry)
                profile.pc_exec_counts[pc] = profile.pc_exec_counts.get(pc, 0) + 1
                profile.total_cycles = max(profile.total_cycles, cycle)

        profile.total_instructions = len(profile.entries)
        return profile

    # ──────────────────────────────────────────────────────────────────
    # Format 3: Spike commit log
    # ──────────────────────────────────────────────────────────────────

    def _parse_spike(
        self,
        path: Path,
        blocks: Optional[Dict],
        max_instructions: int,
        store_entries: bool,
        progress_every: int,
    ) -> TraceProfile:
        """
        Parse Spike -l --log-commits output.

        Spike format (pairs of lines):
          Disasm:  core 0: 0xPC (0xBITS) mnemonic operands
          Commit:  core 0: PRIV 0xPC (0xBITS) [xN 0xVAL] [mem 0xADDR 0xVAL]

        Key insight: we maintain a shadow register file. Before each
        instruction, shadow_rf[rs1] and shadow_rf[rs2] give us the
        source operand values. After each instruction, we update
        shadow_rf[rd] from the commit line's writeback.
        """
        profile = TraceProfile(trace_format="spike")
        self._reset_shadow_rf()

        stats = {
            "total_lines": 0,
            "disasm_lines": 0,
            "commit_lines": 0,
            "reg_writebacks": 0,
            "mem_accesses": 0,
            "unmatched_commits": 0,
        }

        # Build PC → (block_id, instr_idx) mapping
        pc_to_block: Dict[int, Tuple[int, int]] = {}
        if blocks:
            for bid, block in blocks.items():
                for idx, inst in enumerate(block.instructions):
                    pc = getattr(inst, "pc", None) or getattr(inst, "address", None)
                    if pc is not None:
                        pc_to_block[pc] = (bid, idx)

        # Online value trackers (O(1) memory each)
        reg_trackers: Dict[Tuple, OnlineValueTracker] = {}

        # Streaming parse
        pending = None  # Pending disassembly line
        n_instructions = 0
        cycle_counter = 0  # Synthetic cycle counter for Spike

        with open(path, "r", errors="replace") as f:
            for line in f:
                stats["total_lines"] += 1
                line = line.rstrip()
                if not line or not line.startswith("core"):
                    continue

                # ── Try disassembly line ──
                # Distinguish from commit: disasm has 0x right after ": "
                # commit has a priv digit first: ": 3 0x..."
                m_disasm = self.SPIKE_DISASM_RE.match(line)
                if m_disasm:
                    # Check it's NOT a commit line (no priv digit between : and 0x)
                    # Quick heuristic: after last ":", strip space, first char
                    after_colon = line.split(":", 2)
                    if len(after_colon) >= 3:
                        remainder = after_colon[2].strip()
                    else:
                        remainder = after_colon[-1].strip()

                    if remainder and remainder[0] == "0":
                        # Starts with 0x → disassembly line
                        stats["disasm_lines"] += 1
                        pending = {
                            "pc": int(m_disasm.group(1), 16),
                            "bits_str": m_disasm.group(2),
                            "mnemonic": m_disasm.group(3),
                            "operands": m_disasm.group(4).strip(),
                        }
                        continue

                # ── Try commit line ──
                m_commit = self.SPIKE_COMMIT_RE.match(line)
                if m_commit:
                    stats["commit_lines"] += 1
                    commit_pc = int(m_commit.group(1), 16)
                    trailing = m_commit.group(3).strip()

                    # Parse register writeback
                    rd_reg, rd_val = None, None
                    rm = self.SPIKE_REG_WB_RE.search(trailing)
                    if rm:
                        rd_reg = rm.group(1)
                        rd_val = int(rm.group(2), 16)
                        stats["reg_writebacks"] += 1

                    # Parse memory access
                    mem_addr, mem_val = None, None
                    mm = self.SPIKE_MEM_RE.search(trailing)
                    if mm:
                        mem_addr = int(mm.group(1), 16)
                        mem_val = int(mm.group(2), 16) if mm.group(2) else None
                        stats["mem_accesses"] += 1

                    # Match with pending disassembly
                    if pending and pending["pc"] == commit_pc:
                        entry = self._process_spike_instruction(
                            pending,
                            rd_reg,
                            rd_val,
                            mem_addr,
                            mem_val,
                            pc_to_block,
                            reg_trackers,
                            profile,
                            cycle_counter,
                        )

                        if store_entries and entry is not None:
                            profile.entries.append(entry)

                        pending = None
                        n_instructions += 1
                        cycle_counter += 1

                        if progress_every and n_instructions % progress_every == 0:
                            print(f"    ... {n_instructions:,} instructions parsed")

                        if max_instructions and n_instructions >= max_instructions:
                            break
                    else:
                        stats["unmatched_commits"] += 1
                    continue

        # ── Convert online trackers to register_values format ──
        for key, tracker in reg_trackers.items():
            profile.register_values[key] = tracker.to_list()

        profile.total_instructions = n_instructions
        profile.total_cycles = cycle_counter
        profile.stats = stats

        return profile

    def _process_spike_instruction(
        self,
        disasm: dict,
        rd_reg: Optional[str],
        rd_val: Optional[int],
        mem_addr: Optional[int],
        mem_val: Optional[int],
        pc_to_block: Dict[int, Tuple[int, int]],
        reg_trackers: Dict[Tuple, OnlineValueTracker],
        profile: TraceProfile,
        cycle: int,
    ) -> Optional[TraceEntry]:
        """
        Process one Spike instruction.

        1. Read source values from shadow RF (BEFORE execution)
        2. Track values for operand constancy analysis
        3. Update shadow RF with rd writeback (AFTER execution)
        """
        pc = disasm["pc"]
        mnemonic = disasm["mnemonic"]
        operands_str = disasm["operands"]
        bits_str = disasm["bits_str"]
        bits = int(bits_str, 16)

        # Instruction size
        is_compressed = (bits & 0x3) != 0x3
        _ = 2 if is_compressed else 4  # insn_size unused

        # Parse operands from disassembly text
        rd, rs1, rs2, imm = self._parse_spike_operands(mnemonic, operands_str)

        # ── Read source values from shadow RF (BEFORE) ──
        rs1_val = self._shadow_rf.get(rs1) if rs1 and rs1 != "x0" else None
        rs2_val = self._shadow_rf.get(rs2) if rs2 and rs2 != "x0" else None

        # ── Build TraceEntry ──
        entry = TraceEntry(
            cycle=cycle,
            pc=pc,
            raw_instr=bits,
            mnemonic=mnemonic,
            operands=operands_str.split(",") if operands_str else [],
            is_compressed=is_compressed,
        )

        # Register reads (from shadow RF)
        if rs1 and rs1 != "x0" and rs1_val is not None:
            entry.reg_reads[rs1] = rs1_val
        if rs2 and rs2 != "x0" and rs2_val is not None:
            entry.reg_reads[rs2] = rs2_val

        # Register writes (from commit line)
        if rd_reg and rd_val is not None:
            entry.reg_writes[rd_reg] = rd_val

        # Memory access
        is_store = mnemonic in STORE_MNEMONICS
        is_load = mnemonic in LOAD_MNEMONICS

        if mem_addr is not None:
            entry.mem_addr = mem_addr
            if is_store:
                entry.mem_store = mem_val
            elif is_load:
                entry.mem_load = mem_val
            profile.memory_accesses.append((pc, mem_addr, is_store))
        elif (is_load or is_store) and rs1_val is not None and imm is not None:
            # Reconstruct memory address from shadow RF
            reconstructed_addr = (rs1_val + imm) & 0xFFFFFFFF
            entry.mem_addr = reconstructed_addr
            profile.memory_accesses.append((pc, reconstructed_addr, is_store))

        # ── Map to basic block and track values ──
        profile.pc_exec_counts[pc] = profile.pc_exec_counts.get(pc, 0) + 1

        mapping = pc_to_block.get(pc)
        if mapping:
            bid, idx = mapping

            # Track source register values (for operand constancy)
            if rs1 and rs1 != "x0" and rs1_val is not None:
                key = (bid, idx, rs1)
                if key not in reg_trackers:
                    reg_trackers[key] = OnlineValueTracker()
                reg_trackers[key].add(rs1_val)

            if rs2 and rs2 != "x0" and rs2_val is not None:
                key = (bid, idx, rs2)
                if key not in reg_trackers:
                    reg_trackers[key] = OnlineValueTracker()
                reg_trackers[key].add(rs2_val)

        # ── Update shadow RF (AFTER execution) ──
        if rd_reg and rd_val is not None and rd_reg != "x0":
            self._shadow_rf[rd_reg] = rd_val

        return entry

    # ──────────────────────────────────────────────────────────────────
    # Spike operand parsing
    # ──────────────────────────────────────────────────────────────────

    def _parse_spike_operands(
        self,
        mnemonic: str,
        ops_str: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
        """
        Parse operands from Spike's disassembly output.

        Returns: (rd, rs1, rs2, imm) — all normalized to xN format.
        """
        rd, rs1, rs2, imm = None, None, None, None

        if not ops_str:
            return rd, rs1, rs2, imm

        ops = ops_str.strip()

        # ── Memory format: reg, offset(base) ──
        mem_match = re.match(r"(\w+),\s*(-?\w+)\((\w+)\)", ops)
        if mem_match:
            reg1 = normalize_reg(mem_match.group(1))
            offset = _parse_imm(mem_match.group(2))
            base = normalize_reg(mem_match.group(3))
            if mnemonic in STORE_MNEMONICS:
                rs2, rs1, imm = reg1, base, offset
            else:
                rd, rs1, imm = reg1, base, offset
            return rd, rs1, rs2, imm

        # ── PC-relative targets ──
        if "pc" in ops:
            before_pc = ops.split("pc")[0]
            parts = [p.strip().rstrip(",") for p in before_pc.split(",") if p.strip()]

            pc_match = re.search(r"pc\s*([+-])\s*(\d+)", ops)
            if pc_match:
                sign = 1 if pc_match.group(1) == "+" else -1
                imm = sign * int(pc_match.group(2))

            if mnemonic in {"beq", "bne", "blt", "bge", "bltu", "bgeu"}:
                if len(parts) >= 1:
                    rs1 = normalize_reg(parts[0])
                if len(parts) >= 2:
                    rs2 = normalize_reg(parts[1])
            elif mnemonic in {"c.beqz", "c.bnez"}:
                if len(parts) >= 1:
                    rs1 = normalize_reg(parts[0])
            elif mnemonic in {"jal", "c.jal"}:
                if len(parts) >= 1:
                    rd = normalize_reg(parts[0])
                if mnemonic == "c.jal" and not rd:
                    rd = "x1"
            elif mnemonic == "jalr":
                if len(parts) >= 1:
                    rd = normalize_reg(parts[0])
                if len(parts) >= 2:
                    rs1 = normalize_reg(parts[1])
            elif mnemonic == "c.j":
                pass
            elif mnemonic == "c.jr":
                if len(parts) >= 1:
                    rs1 = normalize_reg(parts[0])
            elif mnemonic == "c.jalr":
                if len(parts) >= 1:
                    rs1 = normalize_reg(parts[0])
                rd = "x1"
            return rd, rs1, rs2, imm

        # ── Split by comma ──
        parts = [p.strip() for p in ops.split(",")]

        # ── Classify by mnemonic ──

        # U-type: rd, imm
        if mnemonic in {"lui", "auipc", "c.lui", "c.li"}:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            if len(parts) >= 2:
                imm = _parse_imm(parts[1])
            if mnemonic == "c.li":
                rs1 = "x0"

        # R-type: rd, rs1, rs2
        elif mnemonic in {
            "add",
            "sub",
            "sll",
            "srl",
            "sra",
            "and",
            "or",
            "xor",
            "slt",
            "sltu",
            "mul",
            "mulh",
            "mulhsu",
            "mulhu",
            "div",
            "divu",
            "rem",
            "remu",
        }:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            if len(parts) >= 2:
                rs1 = normalize_reg(parts[1])
            if len(parts) >= 3:
                rs2 = normalize_reg(parts[2])

        # I-type: rd, rs1, imm
        elif mnemonic in {
            "addi",
            "slti",
            "sltiu",
            "andi",
            "ori",
            "xori",
            "slli",
            "srli",
            "srai",
        }:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            if len(parts) >= 2:
                rs1 = normalize_reg(parts[1])
            if len(parts) >= 3:
                imm = _parse_imm(parts[2])

        # c.mv rd, rs2 / c.add rd, rs2
        elif mnemonic in {"c.mv", "c.add"}:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
                rs1 = rd if mnemonic == "c.add" else "x0"
            if len(parts) >= 2:
                rs2 = normalize_reg(parts[1])

        # c.sub, c.xor, c.or, c.and: rd/rs1, rs2
        elif mnemonic in {"c.sub", "c.xor", "c.or", "c.and"}:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
                rs1 = rd
            if len(parts) >= 2:
                rs2 = normalize_reg(parts[1])

        # c.addi, c.slli, c.srli, c.srai, c.andi: rd/rs1, imm
        elif mnemonic in {"c.addi", "c.slli", "c.srli", "c.srai", "c.andi"}:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
                rs1 = rd
            if len(parts) >= 2:
                imm = _parse_imm(parts[1])

        # c.addi16sp sp, imm
        elif mnemonic == "c.addi16sp":
            rd = "x2"
            rs1 = "x2"
            for p in parts:
                v = _parse_imm(p)
                if v is not None:
                    imm = v
                    break

        # c.addi4spn rd, sp, imm
        elif mnemonic == "c.addi4spn":
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            rs1 = "x2"
            for p in parts[1:]:
                v = _parse_imm(p)
                if v is not None:
                    imm = v
                    break

        # c.lwsp rd, offset(sp)  — sometimes disassembled without parentheses
        elif mnemonic == "c.lwsp":
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            rs1 = "x2"  # Always sp-relative
            if len(parts) >= 2:
                imm = _parse_imm(parts[1])

        # c.swsp rs2, offset(sp)
        elif mnemonic == "c.swsp":
            if len(parts) >= 1:
                rs2 = normalize_reg(parts[0])
            rs1 = "x2"
            if len(parts) >= 2:
                imm = _parse_imm(parts[1])

        # c.lw rd, offset(rs1)
        elif mnemonic == "c.lw":
            # Should have been caught by mem_match, but fallback
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])

        # c.sw rs2, offset(rs1)
        elif mnemonic == "c.sw":
            if len(parts) >= 1:
                rs2 = normalize_reg(parts[0])

        # CSR instructions
        elif mnemonic in {"csrrw", "csrrs", "csrrc"}:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            if len(parts) >= 3:
                rs1 = normalize_reg(parts[2])
        elif mnemonic in {"csrrwi", "csrrsi", "csrrci"}:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            if len(parts) >= 3:
                imm = _parse_imm(parts[2])

        # No-operand instructions
        elif mnemonic in {
            "ecall",
            "ebreak",
            "fence",
            "fence.i",
            "mret",
            "sret",
            "uret",
            "wfi",
            "c.nop",
            "c.ebreak",
            "nop",
        }:
            pass

        # Generic fallback
        else:
            if len(parts) >= 1:
                rd = normalize_reg(parts[0])
            if len(parts) >= 2:
                rs1 = normalize_reg(parts[1])
            if len(parts) >= 3:
                r = normalize_reg(parts[2])
                if r:
                    rs2 = r
                else:
                    imm = _parse_imm(parts[2])

        return rd, rs1, rs2, imm

    # ──────────────────────────────────────────────────────────────────
    # Stall analysis
    # ──────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────
    # Quality report
    # ──────────────────────────────────────────────────────────────────

    def get_quality_report(self, profile: TraceProfile) -> str:
        """Return human-readable quality report of parsed trace."""
        lines = [
            f"  Trace Quality Report ({profile.trace_format})",
            f"    Total instructions: {profile.total_instructions:>12,}",
            f"    Unique PCs:         {len(profile.pc_exec_counts):>12,}",
            f"    Total cycles:       {profile.total_cycles:>12,}",
            f"    Memory accesses:    {len(profile.memory_accesses):>12,}",
        ]

        if profile.register_values:
            n_keys = len(profile.register_values)
            # Count how many have constant values
            n_constant = sum(1 for vals in profile.register_values.values() if len(vals) == 1 or (len(set(vals)) == 1))
            lines.append(f"    Register observations: {n_keys:>12,}")
            lines.append(f"    Constant operands:     {n_constant:>12,} ({100 * n_constant / max(1, n_keys):.1f}%)")
        else:
            lines.append(f"    Register observations: {'none':>12}")

        if profile.stats:
            s = profile.stats
            if "disasm_lines" in s:
                lines.append(f"    Spike disasm lines:    {s['disasm_lines']:>12,}")
            if "commit_lines" in s:
                lines.append(f"    Spike commit lines:    {s['commit_lines']:>12,}")
            if "unmatched_commits" in s:
                lines.append(f"    Unmatched commits:     {s['unmatched_commits']:>12,}")

        # Shadow RF state (Spike only)
        if profile.trace_format == "spike":
            non_zero = sum(1 for v in self._shadow_rf.values() if v != 0)
            lines.append(f"    Shadow RF non-zero:    {non_zero:>12}/32")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Convenience function
# ═══════════════════════════════════════════════════════════════════════════


def parse_trace(path: str, blocks: Dict | None = None) -> TraceProfile:
    """Convenience function to parse trace file."""
    parser = TracerParser()
    return parser.parse_file(Path(path), blocks=blocks)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        profile = parse_trace(sys.argv[1])

        parser = TracerParser()
        print(parser.get_quality_report(profile))

        # Show top executed PCs
        top = sorted(profile.pc_exec_counts.items(), key=lambda x: -x[1])[:10]
        print("\n  Top executed PCs:")
        for pc, count in top:
            print(f"    0x{pc:08x}: {count:,}")

        # Show register value constancy
        if profile.register_values:
            n_const = sum(1 for v in profile.register_values.values() if len(set(v)) == 1)
            n_total = len(profile.register_values)
            print(f"\n  Register constancy: {n_const}/{n_total} ({100 * n_const / max(1, n_total):.1f}% constant)")
    else:
        print("Usage: python tracer_parser.py <trace_file>")
