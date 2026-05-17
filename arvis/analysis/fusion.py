"""N-gram instruction fusion analysis with data-flow aware patterns."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from .liveness import LivenessAnalyzer
from .models import (
    BasicBlock,
    DynamicProfile,
    FusionCandidate,
    Instruction,
    InstructionType,
    Loop,
)
from .registers import COMMUTATIVE as COMMUTATIVE
from .registers import COMPRESSED_TO_BASE as COMPRESSED_TO_BASE
from .registers import normalize_mnemonic as normalize_mnemonic

# COMPRESSED_TO_BASE, normalize_mnemonic, and COMMUTATIVE are imported
# from .registers (the single source of truth for RISC-V ISA constants).
# COMPRESSED_TO_BASE is re-exported here for backwards compatibility with
# existing code that imports it from analysis.fusion.

# ══════════════════════════════════════════════════════════════════════════════
# CV32E40P Latency Classification (from RTL analysis)
# ══════════════════════════════════════════════════════════════════════════════
# Based on cv32e40p_alu.sv and cv32e40p_mult.sv:
#
# SINGLE-CYCLE ALU (combinational, no stall):
#   - Logic: and, or, xor, andi, ori, xori
#   - Arithmetic: add, sub, addi (and variants addr, subr, etc.)
#   - Shifts: sll, srl, sra, slli, srli, srai, ror
#   - Comparisons: slt, sltu, slti, sltiu, eq, ne, lt, ge, etc.
#   - Bit manipulation: bext, bins, bclr, bset, brev
#   - Min/Max/Clip: min, max, abs, clip
#   - Bit counting: ff1, fl1, clb, cnt (popcnt)
#   - Shuffle/Pack: shuf, pcklo, pckhi, ext, ins
#
# SINGLE-CYCLE MUL (MUL_MAC32, MUL_MSU32 - 32x32 combinational):
#   - mul (lower 32 bits)
#
# MULTI-CYCLE MUL (MUL_H - 4 cycles via FSM):
#   IDLE -> STEP0 -> STEP1 -> STEP2 -> FINISH
#   - mulh, mulhsu, mulhu
#
# MULTI-CYCLE DIV (iterative via cv32e40p_alu_div):
#   - div, divu, rem, remu (~34 cycles)
#
# MEMORY (LSU, 1+ cycles depending on memory latency):
#   - lb, lbu, lh, lhu, lw, sb, sh, sw
#
# CV32E40P Pipeline: 4 stages (IF, ID, EX, WB)
#   - Ideal throughput: 1 instruction/cycle
#   - EX->EX forwarding: ALU result available next cycle without stall
#   - Load-use hazard: 1 cycle stall (data available end of WB, needed at EX)
# ══════════════════════════════════════════════════════════════════════════════

# Single-cycle ALU operations
SINGLE_CYCLE_ALU = {
    # Logic
    "and",
    "or",
    "xor",
    "andi",
    "ori",
    "xori",
    # Arithmetic
    "add",
    "sub",
    "addi",
    # Shifts
    "sll",
    "srl",
    "sra",
    "slli",
    "srli",
    "srai",
    # Comparisons
    "slt",
    "sltu",
    "slti",
    "sltiu",
    # Upper immediate
    "lui",
    "auipc",
    # Compressed equivalents (expand to single-cycle)
    "c.add",
    "c.sub",
    "c.and",
    "c.or",
    "c.xor",
    "c.addi",
    "c.slli",
    "c.srli",
    "c.srai",
    "c.andi",
    "c.li",
    "c.lui",
    "c.mv",
    "c.addi16sp",
    "c.addi4spn",
}


# Non-commutative operations where operand order matters
NON_COMMUTATIVE = {
    "sub",
    "sll",
    "srl",
    "sra",
    "slli",
    "srli",
    "srai",
    "div",
    "divu",
    "rem",
    "remu",
    "slt",
    "sltu",
    "slti",
    "sltiu",
}

# Single-cycle MUL (32-bit result only)
SINGLE_CYCLE_MUL = {"mul"}

# Multi-cycle MUL (4 cycles for high 32 bits)
MULTI_CYCLE_MUL = {"mulh", "mulhsu", "mulhu"}
MULH_LATENCY = 4  # IDLE->STEP0->STEP1->STEP2->FINISH

# Multi-cycle DIV (~34 cycles iterative)
MULTI_CYCLE_DIV = {"div", "divu", "rem", "remu"}
DIV_LATENCY = 34  # Approximate, depends on operands

# Memory operations (latency depends on memory subsystem)
MEMORY_LOAD = {"lb", "lbu", "lh", "lhu", "lw", "c.lw", "c.lwsp"}
MEMORY_STORE = {"sb", "sh", "sw", "c.sw", "c.swsp"}
MEMORY_LATENCY = 2  # Typical: 1 cycle address + 1 cycle data

# Branch fusion control
ALLOW_BRANCH_FUSION = False  # Set True only if handling compare+branch fusion


@dataclass
class OperandConstancy:
    """Tracks if an operand has constant value across executions."""

    is_constant: bool = False
    constant_value: Optional[int] = None
    unique_values: Set[int] = field(default_factory=set)
    occurrence_count: int = 0

    @property
    def variance(self) -> float:
        """0.0 = constant, 1.0 = all different values."""
        if self.occurrence_count <= 1:
            return 0.0
        return (len(self.unique_values) - 1) / (self.occurrence_count - 1)

    def should_hardcode(self, threshold: float = 0.1) -> bool:
        """Hardcode if variance is below threshold (mostly constant)."""
        return self.variance <= threshold and self.is_constant


@dataclass
class ImmediateStats:
    """Track immediate value distribution for a pattern across occurrences."""

    # position_in_ngram -> {value: count}
    value_counts: Dict[int, Dict[int, int]] = field(default_factory=dict)

    def add_observation(self, position: int, value: int):
        if position not in self.value_counts:
            self.value_counts[position] = defaultdict(int)
        self.value_counts[position][value] += 1

    def should_hardcode(self, position: int, threshold: float = 1.0) -> Tuple[bool, Optional[int]]:
        """Decide whether to hardcode the immediate at *position*.

        Returns ``(True, value)`` only when **all** occurrences use the
        exact same immediate (``threshold=1.0``).  A lower threshold
        would produce wrong results for the minority of instances that
        use a different value — and because the fused R-type encoding
        has no immediate field, there is no fallback.
        """
        if position not in self.value_counts:
            return False, None
        counts = self.value_counts[position]
        total = sum(counts.values())
        if total == 0:
            return False, None
        most_common_val = max(counts, key=counts.get)  # type: ignore[arg-type]
        ratio = counts[most_common_val] / total
        if ratio >= threshold:
            return True, most_common_val
        return False, None

    def get_all_decisions(self, n_insts: int, threshold: float = 1.0) -> List[Tuple[bool, Optional[int]]]:
        """Return hardcode decision for each instruction position."""
        return [self.should_hardcode(i, threshold) for i in range(n_insts)]


@dataclass
class ChainEdge:
    """A data dependency edge within an n-gram.

    Represents: instruction `producer_idx` writes a register that
    instruction `consumer_idx` reads at operand position `consumer_slot`.

    consumer_slot: 0 = rs1 (first operand), 1 = rs2 (second operand)
    """

    producer_idx: int
    consumer_idx: int
    consumer_slot: int  # 0=rs1, 1=rs2


@dataclass
class NgramSignature:
    """
    Rich signature for n-gram differentiation.

    Two n-grams are equivalent iff they have the same signature.
    This captures:
    - Instruction sequence (mnemonics)
    - Data-flow pattern (which rd feeds which rs1/rs2)
    - Which positions have immediates (but NOT the immediate values,
      so that add+xor with imm=5 and add+xor with imm=7 hash to the same bucket)
    - Number of unique external input/output registers
    - Chain topology: explicit dependency edges between instructions

    The chain_edges field is critical for 3-gram GIMPLE matching and
    Verilog generation. It captures:
    - Linear chains: 0→1→2 (each op feeds the next)
    - Fan-out: 0→1, 0→2 (one op feeds two subsequent ops)
    - Mixed: 0→1, 1→2, 0→2 (shared subtree)

    NOTE: n_external_inputs and n_external_outputs are computed from the
    window alone (static data-flow). They do NOT reflect liveness-aware
    port requirements. Use the companion AggregationKey for grouping
    candidates that also differ in liveness-derived write port counts.
    """

    mnemonics: Tuple[str, ...]
    # For each instruction: tuple of (operand_position, binding_type, source_idx_or_None)
    # Normalized: commutative ops have sorted bindings
    bindings: Tuple[Tuple[Tuple[int, str, Optional[int]], ...], ...]
    # Which instruction positions have immediates (True/False per position)
    imm_positions: Tuple[bool, ...]
    # Number of unique external input registers
    n_external_inputs: int
    # Number of unique external output registers
    n_external_outputs: int
    # Chain topology: explicit dependency edges between instructions
    # Each edge: (producer_idx, consumer_idx, consumer_slot)
    # consumer_slot: 0=rs1 (operand a), 1=rs2 (operand b)
    chain_edges: Tuple[ChainEdge, ...] = ()

    def __hash__(self):
        # Hash WITHOUT immediate values — groups patterns with different immediates
        return hash((self.mnemonics, self.bindings, self.imm_positions))

    def __eq__(self, other):
        if not isinstance(other, NgramSignature):
            return False
        return (
            self.mnemonics == other.mnemonics
            and self.bindings == other.bindings
            and self.imm_positions == other.imm_positions
        )

    @property
    def is_linear_chain(self) -> bool:
        """True if dependencies form a simple linear chain: 0→1→2→...

        Each instruction depends on exactly the previous one.
        """
        for edge in self.chain_edges:
            if edge.producer_idx != edge.consumer_idx - 1:
                return False
        return len(self.chain_edges) == len(self.mnemonics) - 1

    @property
    def has_fan_out(self) -> bool:
        """True if any instruction feeds multiple consumers."""
        producers = [e.producer_idx for e in self.chain_edges]
        return len(producers) != len(set(producers))

    @property
    def has_register_reuse(self) -> bool:
        """True if an external input is used by multiple instructions.

        E.g., add(rs1, rs2) → sub(chain, rs2) — rs2 is reused.
        This is important because it means fewer external register
        ports are needed (GCC match_dup).
        """
        # Check if any EXT_IN binding appears at multiple instruction positions
        ext_regs: Dict[Optional[int], int] = {}  # Not used for signature but for analysis
        for inst_bindings in self.bindings:
            for pos, btype, src_idx in inst_bindings:
                if btype == "EXT_IN":
                    ext_regs[src_idx] = ext_regs.get(src_idx, 0) + 1
        # External inputs are identified by their src_idx=None but we need
        # to check from the actual register names, which aren't in the signature.
        # For signature-level analysis, check if n_external_inputs < total ext bindings
        n_ext_bindings = sum(1 for binds in self.bindings for _, btype, _ in binds if btype == "EXT_IN")
        return n_ext_bindings > self.n_external_inputs

    @property
    def has_mul(self) -> bool:
        """True if any instruction is a multiply — may be too slow for single-cycle 3-gram."""
        return "mul" in self.mnemonics


class AggregationKey(NamedTuple):
    """
    Compound key for candidate aggregation that distinguishes patterns
    that have the same data-flow signature but different liveness-derived
    register file port requirements.

    This is critical because the same mnemonic sequence with the same
    chaining structure (e.g., xori → and) may appear in two contexts:
    - Context A: the intermediate register is dead after the n-gram → 1 write port
    - Context B: the intermediate register is live after → 2 write ports

    These require DIFFERENT hardware implementations (single-write vs dual-write)
    and must NOT be merged into a single candidate.

    Similarly, read port count can vary if the same pattern appears in
    contexts where different input registers are or are not live-in.
    """

    signature: NgramSignature
    min_read_ports: int
    min_write_ports: int


@dataclass
class PatternLocation:
    """A specific location where a pattern occurs in the code."""

    block_id: int
    start_idx: int
    start_addr: int  # Address of first instruction
    end_addr: int  # Address of last instruction
    frequency: int  # Execution count at this location
    # Per-instruction register assignments at THIS specific location.
    # List of (rd, rs1, rs2, imm) tuples — one per instruction in the n-gram.
    # These are the ACTUAL register names (e.g. 'x24', 'x17') used here,
    # which may differ from other locations of the same pattern.
    instr_regs: List[Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]] = field(default_factory=list)
    # Convenience: the external I/O registers for encoding the fused instruction.
    # Populated from liveness analysis of this specific window.
    ext_rd: Optional[str] = None  # Final output register (for R-type rd field)
    ext_rs1: Optional[str] = None  # First external input (for rs1 field)
    ext_rs2: Optional[str] = None  # Second external input (for rs2 field)
    ext_rs3: Optional[str] = None  # Third external input (for R4-type rs3 field)
    # Total size in bytes of the original instructions (for NOP padding)
    original_size_bytes: int = 0


@dataclass
class EnhancedFusionCandidate(FusionCandidate):
    """Extended fusion candidate with rich signature."""

    signature: Optional[NgramSignature] = None
    instruction_type: InstructionType = InstructionType.COMPUTE_ONLY
    # Immediate handling
    has_hardcoded_imm: bool = False
    hardcoded_imm_values: Tuple[Optional[int], ...] = ()
    imm_should_hardcode: Tuple[bool, ...] = ()  # Per-instruction decision
    # Constant register detection (from dynamic analysis)
    constant_operands: Dict[Tuple[int, int], OperandConstancy] = field(default_factory=dict)
    # ImmediateStats aggregated across all occurrences
    imm_stats: Optional[ImmediateStats] = None
    # Static code locations where this pattern appears (not dynamic exec count)
    static_instances: int = 0
    # All locations where this pattern occurs
    locations: List[PatternLocation] = field(default_factory=list)


class FusionAnalyzer:
    """
    Analyzes instruction streams for fusion opportunities on cv32e40p.

    CV32E40P is a 4-stage in-order pipeline (IF, ID, EX, WB).
    Ideal throughput: 1 instruction/cycle.
    EX->EX forwarding eliminates most ALU->ALU stalls.
    """

    BASELINE_READ = 3  # operand_a, operand_b, operand_c already exist
    BASELINE_WRITE = 1  # regfile_alu_we + regfile_mem_we paths
    AREA_PER_READ_PORT = 0.02
    AREA_PER_WRITE_PORT = 0.03

    def __init__(self, blocks: Dict[int, BasicBlock], profile: DynamicProfile):
        self.blocks = blocks
        self.profile = profile
        self.liveness = LivenessAnalyzer(blocks)

    # ──────────────────────────────────────────────────────────────────────
    #  Signature computation
    # ──────────────────────────────────────────────────────────────────────

    def _compute_signature(self, window: List[Instruction]) -> NgramSignature:
        """
        Compute rich signature for an n-gram window.

        Tracks:
        - Which instruction's rd feeds into subsequent rs1/rs2 (chaining)
        - Operand positions for non-commutative ops
        - Normalizes operand order for commutative ops so that
          add x1,x2,x3 and add x1,x3,x2 produce the same signature
        - Tracks which positions have immediates (but not values)

        Compressed mnemonics (c.add, c.slli, …) are normalized to their
        base-ISA equivalents (add, slli, …) so that windows containing a
        mix of compressed and non-compressed instructions produce the
        SAME signature and get aggregated into one fusion candidate.
        This matches the CV32E40P hardware where the compressed decoder
        decompresses before the main decoder ever sees the instruction.
        """
        mnemonics = tuple(normalize_mnemonic(i.mnemonic) for i in window)

        # Track which register was last written by which instruction
        last_writer: Dict[str, int] = {}  # reg -> instruction index

        bindings_list = []
        imm_positions = []
        external_inputs: Set[str] = set()
        external_outputs: Set[str] = set()

        for idx, inst in enumerate(window):
            raw_bindings: List[Tuple[int, str, Optional[int]]] = []

            # Analyze rs1
            if inst.rs1 and inst.rs1 != "x0":
                if inst.rs1 in last_writer:
                    binding = (0, "CHAIN", last_writer[inst.rs1])
                else:
                    binding = (0, "EXT_IN", None)  # type: ignore[assignment]
                    external_inputs.add(inst.rs1)
                raw_bindings.append(binding)

            # Analyze rs2
            if inst.rs2 and inst.rs2 != "x0":
                if inst.rs2 in last_writer:
                    binding = (1, "CHAIN", last_writer[inst.rs2])
                else:
                    binding = (1, "EXT_IN", None)  # type: ignore[assignment]
                    external_inputs.add(inst.rs2)
                raw_bindings.append(binding)

            # Normalize for commutative operations:
            # Sort bindings so (EXT_IN, None) and (CHAIN, 0) always appear
            # in the same canonical order regardless of rs1/rs2 assignment
            norm_mnem = normalize_mnemonic(inst.mnemonic)
            if norm_mnem in COMMUTATIVE and len(raw_bindings) == 2:
                raw_bindings.sort(key=lambda b: (b[1], b[2] if b[2] is not None else -1))
                # Reassign canonical positions after sorting
                raw_bindings = [(i, b[1], b[2]) for i, b in enumerate(raw_bindings)]

            # Track rd for chaining
            if inst.rd and inst.rd != "x0":
                last_writer[inst.rd] = idx

            # Record whether this instruction has an immediate
            imm_positions.append(inst.imm is not None)

            bindings_list.append(tuple(raw_bindings))

        # Determine external outputs (registers written that are NOT
        # overwritten by a later instruction in the window)
        for idx, inst in enumerate(window):
            if inst.rd and inst.rd != "x0":
                overwritten = any(w.rd == inst.rd for w in window[idx + 1 :])
                if not overwritten:
                    external_outputs.add(inst.rd)

        # Build chain edges from bindings
        chain_edges: List[ChainEdge] = []
        for consumer_idx, inst_bindings in enumerate(bindings_list):
            for pos, btype, src_idx in inst_bindings:
                if btype == "CHAIN" and src_idx is not None:
                    chain_edges.append(
                        ChainEdge(
                            producer_idx=src_idx,
                            consumer_idx=consumer_idx,
                            consumer_slot=pos,
                        )
                    )

        return NgramSignature(
            mnemonics=mnemonics,
            bindings=tuple(bindings_list),
            imm_positions=tuple(imm_positions),
            n_external_inputs=len(external_inputs),
            n_external_outputs=len(external_outputs),
            chain_edges=tuple(chain_edges),
        )

    # ──────────────────────────────────────────────────────────────────────
    #  Validation helpers
    # ──────────────────────────────────────────────────────────────────────

    def _has_valid_chain(self, window: List[Instruction]) -> bool:
        """Check if there's at least one data dependency chain in the window."""
        written: Set[str] = set()
        for inst in window:
            if inst.rs1 and inst.rs1 in written:
                return True
            if inst.rs2 and inst.rs2 in written:
                return True
            if inst.rd and inst.rd != "x0":
                written.add(inst.rd)
        return False

    def _classify_instruction_type(self, window: List[Instruction]) -> InstructionType:
        """
        Classify fused instruction type based on cv32e40p RTL.

        Categories:
        - COMPUTE_ONLY: All single-cycle ALU ops
        - LOAD_COMPUTE: Has load + ALU ops
        - COMPUTE_STORE: Has ALU ops + store
        - LOAD_COMPUTE_STORE: Has both load and store (single memory port!)
        - MULTI_CYCLE: Contains mulh/div (cannot reduce to single cycle)
        """
        has_load = any(i.is_load for i in window)
        has_store = any(i.is_store for i in window)
        has_mulh = any(i.mnemonic in MULTI_CYCLE_MUL for i in window)
        has_div = any(i.mnemonic in MULTI_CYCLE_DIV for i in window)

        if has_div or has_mulh:
            return InstructionType.MULTI_CYCLE
        if has_load and has_store:
            return InstructionType.LOAD_COMPUTE_STORE
        if has_load:
            return InstructionType.LOAD_COMPUTE
        if has_store:
            return InstructionType.COMPUTE_STORE
        return InstructionType.COMPUTE_ONLY

    # ──────────────────────────────────────────────────────────────────────
    #  Operand constancy analysis (from dynamic profile)
    # ──────────────────────────────────────────────────────────────────────

    def _analyze_operand_constancy(
        self, bid: int, start: int, window: List[Instruction]
    ) -> Dict[Tuple[int, int], OperandConstancy]:
        """
        Analyze if operands have constant values across executions.

        Uses dynamic profile data to detect registers that always hold
        the same value when this n-gram executes.

        Returns: Dict[(instr_idx, operand_pos)] -> OperandConstancy
                 operand_pos: 0=rs1, 1=rs2
        """
        result: Dict[Tuple[int, int], OperandConstancy] = {}

        for idx, inst in enumerate(window):
            global_idx = start + idx

            for pos, reg in enumerate([inst.rs1, inst.rs2]):
                if reg and reg != "x0":
                    key = (bid, global_idx, reg)
                    values = self.profile.register_values.get(key, [])
                    constancy = OperandConstancy(
                        occurrence_count=len(values),
                        unique_values=set(values) if values else set(),
                    )
                    if values and len(set(values)) == 1:
                        constancy.is_constant = True
                        constancy.constant_value = values[0]
                    result[(idx, pos)] = constancy

        return result

    # ──────────────────────────────────────────────────────────────────────
    #  Pipeline hazard modelling (cv32e40p specific)
    # ──────────────────────────────────────────────────────────────────────

    def _hazard_stalls(self, insts: List[Instruction]) -> int:
        """
        Compute stall cycles in the cv32e40p 4-stage pipeline.

        cv32e40p forwarding:
        - ALU -> ALU:    0 stalls (EX->EX forwarding)
        - ALU -> branch: 0 stalls (EX->EX forwarding)
        - MUL -> ALU:    0 stalls (single-cycle mul, EX->EX forwarding)
        - Load -> use:   1 stall  (data available end of WB, needed at start of EX)
        - MULH -> use:   (MULH_LATENCY - 1) stalls
        - DIV  -> use:   (DIV_LATENCY - 1) stalls
        """
        stalls = 0
        # Track when each register result becomes available (cycle number)
        # A result is "available" when it can be forwarded to the next EX stage
        available: Dict[str, int] = {}

        for i, inst in enumerate(insts):
            current_cycle = i + stalls

            # Check if any source operand is not yet available
            for reg in inst.uses:
                if reg in available and available[reg] > current_cycle:
                    extra_stall = available[reg] - current_cycle
                    stalls += extra_stall
                    current_cycle += extra_stall

            # Set availability of rd based on instruction type
            for rd in inst.defs:
                if rd and rd != "x0":
                    if inst.is_load:
                        # Load: data available after MEM stage = current + 2
                        # (EX computes address, MEM reads data, available at WB)
                        available[rd] = current_cycle + 2
                    elif inst.mnemonic in MULTI_CYCLE_MUL:
                        available[rd] = current_cycle + MULH_LATENCY
                    elif inst.mnemonic in MULTI_CYCLE_DIV:
                        available[rd] = current_cycle + DIV_LATENCY
                    else:
                        # Single-cycle ALU/MUL: result forwarded from EX,
                        # available for next instruction's EX stage
                        available[rd] = current_cycle + 1

        return stalls

    # ──────────────────────────────────────────────────────────────────────
    #  Fused instruction latency estimation
    # ──────────────────────────────────────────────────────────────────────

    def _fused_latency(self, insts: List[Instruction]) -> int:
        """
        Compute fused instruction latency based on cv32e40p RTL.

        Rules:
        - DIV present:  DIV_LATENCY (iterative, dominates everything)
        - MULH present: MULH_LATENCY (4-cycle FSM)
        - Load + Store: MEMORY_LATENCY + 1 (single memory port, 2 accesses)
        - Load or Store: MEMORY_LATENCY
        - Pure ALU (<=3 chained ops): 1 cycle (combinational cascade fits)
        - Pure ALU (4+ chained ops):  2 cycles (critical path too long)
        - MUL + 2+ ALU ops: 2 cycles (multiplier critical path + ALU chain)
        """
        has_div = any(i.mnemonic in MULTI_CYCLE_DIV for i in insts)
        has_mulh = any(i.mnemonic in MULTI_CYCLE_MUL for i in insts)
        has_load = any(i.is_load for i in insts)
        has_store = any(i.is_store for i in insts)
        has_mul = any(i.mnemonic in SINGLE_CYCLE_MUL for i in insts)

        if has_div:
            return DIV_LATENCY
        if has_mulh:
            return MULH_LATENCY

        if has_load and has_store:
            return MEMORY_LATENCY + 1  # Single memory port, need 2 accesses
        if has_load or has_store:
            return MEMORY_LATENCY

        # Pure compute: chain combinationally
        n_compute = len(insts)

        # Multiplier has larger critical path than ALU
        if has_mul and n_compute > 2:
            return 2  # mul + 2+ ALU ops unlikely to fit in 1 clock period

        # Conservative timing estimate for chained ALU ops
        # Up to 3 chained ALU ops: ~1 cycle (adder + logic + mux fits)
        # 4+ chained: likely needs 2 cycles (will need synthesis to confirm)
        if n_compute <= 3:
            return 1
        else:
            return 2

    # ──────────────────────────────────────────────────────────────────────
    #  Encoding feasibility
    # ──────────────────────────────────────────────────────────────────────

    def _encoding(self, nr: int, nw: int) -> str:
        """Determine instruction encoding type based on port requirements."""
        if nr <= 2 and nw <= 1:
            return "R-type (10 func bits)"
        if nr <= 3 and nw <= 1:
            return "R4-type (5 func bits)"
        if nr <= 2 and nw <= 2:
            return "R-type dual-write (5 func bits)"
        if nr <= 3 and nw <= 2:
            return "R4-type dual-write (2 func bits)"
        return f"Needs {nr}R/{nw}W — RF extension required"

    # ──────────────────────────────────────────────────────────────────────
    #  Selection score for ranking
    # ──────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────
    #  Sub-pattern detection
    # ──────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────
    #  Main analysis entry point
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _merge_constancy(
        existing_ops: Dict[Tuple[int, int], OperandConstancy],
        new_ops: Dict[Tuple[int, int], OperandConstancy],
    ) -> None:
        """
        Merge operand constancy observations from a new instance into
        an existing aggregate.

        After merging, `is_constant` is only True if ALL instances agreed
        on the same value. `unique_values` is the union (capped to avoid
        memory blowup). `occurrence_count` is the sum.
        """
        for key, new_c in new_ops.items():
            if key not in existing_ops:
                # First time seeing this operand position — copy it
                existing_ops[key] = OperandConstancy(
                    is_constant=new_c.is_constant,
                    constant_value=new_c.constant_value,
                    unique_values=set(new_c.unique_values),
                    occurrence_count=new_c.occurrence_count,
                )
            else:
                ex = existing_ops[key]
                # Merge counts
                ex.occurrence_count += new_c.occurrence_count
                # Merge unique values (cap at 64 to avoid memory explosion)
                if len(ex.unique_values) < 64:
                    ex.unique_values |= new_c.unique_values
                # Update constancy: constant only if both are constant
                # with the same value
                if ex.is_constant and new_c.is_constant:
                    if ex.constant_value != new_c.constant_value:
                        ex.is_constant = False
                        ex.constant_value = None
                elif not new_c.is_constant:
                    ex.is_constant = False
                    ex.constant_value = None
                # If existing was not constant, it stays not constant

    def _collect_branch_targets(self) -> Set[int]:
        """Collect all addresses that are targets of branches or jumps.

        Any instruction at one of these addresses could be reached via a
        control-flow transfer.  Such instructions MUST NOT appear at
        position > 0 inside an n-gram, because doing so would mean a
        branch could jump into the *middle* of the fused instruction,
        leading to incorrect execution.

        We conservatively include:
        - Explicit branch/jump target addresses from the instruction operands
        - The start address of every basic block (by definition, the entry
          point of a block is reachable from some predecessor)
        """
        targets: Set[int] = set()

        for block in self.blocks.values():
            # Every block start is a potential branch target
            if block.instructions:
                targets.add(block.instructions[0].address)

            # Also collect explicit targets from branch/jump instructions
            for inst in block.instructions:
                if inst.is_branch or inst.is_jump:
                    # The target address is usually the immediate operand
                    # interpreted as an absolute address, or can be inferred
                    # from the successor blocks.
                    if inst.imm is not None:
                        # For PC-relative branches: target = PC + imm
                        # For JAL: target = PC + imm
                        # For JALR: target is register-based (indirect), we
                        # handle this via block-start heuristic above.
                        target_addr = inst.address + inst.imm
                        targets.add(target_addr)

            # Add successor block start addresses
            for succ_id in block.successors:
                succ = self.blocks.get(succ_id)
                if succ and succ.instructions:
                    targets.add(succ.instructions[0].address)

        return targets

    def analyze_loop(
        self,
        loop: Loop,
        min_n: int = 2,
        max_n: int = 4,
        min_frequency: int = 10,
        require_chain: bool = True,
    ) -> List[EnhancedFusionCandidate]:
        """
        Analyze loop for fusion candidates with rich pattern detection.

        Candidates are aggregated by AggregationKey which includes BOTH
        the data-flow signature AND the liveness-derived port requirements.
        This ensures that the same mnemonic pattern appearing in different
        liveness contexts (e.g., 1 write port vs 2 write ports) produces
        separate candidates, since they require different hardware.

        IMPORTANT: Instructions at position > 0 in the n-gram window must
        NOT be branch targets.  If a branch can jump to the middle of a
        fused instruction, the processor would execute corrupted code.
        We pre-compute all branch target addresses and reject any window
        where a non-first instruction sits at a branch target address.

        Args:
            loop: Loop to analyze
            min_n: Minimum n-gram size
            max_n: Maximum n-gram size
            min_frequency: Minimum block execution count to consider
            require_chain: If True, only consider n-grams with data dependencies
        """
        # Pre-compute all branch target addresses for the safety check
        branch_targets = self._collect_branch_targets()

        # Group by AggregationKey = (signature, read_ports, write_ports)
        # This separates candidates that share the same data-flow pattern
        # but differ in liveness context (different port requirements).
        by_agg_key: Dict[AggregationKey, EnhancedFusionCandidate] = {}
        # Track immediate value distributions per aggregation key
        imm_stats_by_key: Dict[AggregationKey, ImmediateStats] = {}

        for bid in sorted(loop.body_block_ids):
            block = self.blocks[bid]
            freq = self.profile.block_exec_counts.get(bid, 0)
            if freq < min_frequency:
                continue

            n_insts = len(block.instructions)
            for n in range(min_n, min(max_n + 1, n_insts + 1)):
                for start in range(n_insts - n + 1):
                    end = start + n
                    window = block.instructions[start:end]

                    # ── Control flow filtering ──
                    # Never allow branches/jumps in the middle of the window
                    if any(i.is_branch or i.is_jump for i in window[:-1]):
                        continue
                    # Last instruction may be a branch only for 2-grams
                    # (compare + branch fusion) and only if enabled
                    if window[-1].is_branch or window[-1].is_jump:
                        if not ALLOW_BRANCH_FUSION:
                            continue
                        if n > 2:
                            continue

                    # CRITICAL: Non-first instructions must NOT be branch
                    # targets. A branch jumping to the middle of a fused
                    # instruction would execute corrupted code.
                    if any(inst.address in branch_targets for inst in window[1:]):
                        continue

                    # Require data dependency chain
                    if require_chain and not self._has_valid_chain(window):
                        continue

                    # Compute rich signature (with commutative normalization)
                    sig = self._compute_signature(window)

                    # Skip if too many external I/O (won't fit in any encoding)
                    if sig.n_external_inputs > 4 or sig.n_external_outputs > 2:
                        continue

                    # ── Liveness and savings ──
                    # Liveness is context-dependent: the same pattern at
                    # different positions may have different live-out sets,
                    # leading to different read/write port requirements.
                    lv = self.liveness.analyze_ngram(bid, start, end)
                    orig = n + self._hazard_stalls(window)
                    fused = self._fused_latency(window)
                    savings = orig - fused
                    if savings <= 0:
                        continue

                    nr = lv.min_read_ports
                    nw = lv.min_write_ports_eliminate
                    fits = nr <= self.BASELINE_READ and nw <= self.BASELINE_WRITE
                    enc = self._encoding(nr, nw)
                    inst_type = self._classify_instruction_type(window)

                    # ── Build compound aggregation key ──
                    # Two instances with the same signature but different
                    # port requirements (due to different liveness contexts)
                    # will produce DIFFERENT keys → separate candidates.
                    agg_key = AggregationKey(
                        signature=sig,
                        min_read_ports=nr,
                        min_write_ports=nw,
                    )

                    # ── Immediate statistics ──
                    # Accumulate immediate values per aggregation key
                    if agg_key not in imm_stats_by_key:
                        imm_stats_by_key[agg_key] = ImmediateStats()
                    for i_idx, inst in enumerate(window):
                        if inst.imm is not None:
                            imm_stats_by_key[agg_key].add_observation(i_idx, inst.imm)

                    has_imm = any(inst.imm is not None for inst in window)

                    # ── Operand constancy (dynamic profile) ──
                    const_ops = self._analyze_operand_constancy(bid, start, window)

                    # ── Extract per-location register info ──
                    # Record the actual rd/rs1/rs2/imm for each instruction
                    # in THIS window so the patcher knows which registers
                    # to encode at each location.
                    instr_regs = [(inst.rd, inst.rs1, inst.rs2, inst.imm) for inst in window]
                    original_size_bytes = sum(getattr(inst, "size", 4) for inst in window)

                    # Determine the external I/O register names for
                    # the fused instruction encoding at this location.
                    # External inputs: used but not defined by an earlier
                    # instruction in the window.
                    # External output: last rd not overwritten.
                    loc_ext_inputs = []
                    loc_defined = set()
                    for inst in window:
                        for reg in [inst.rs1, inst.rs2]:
                            if reg and reg != "x0" and reg not in loc_defined:
                                loc_ext_inputs.append(reg)
                        if inst.rd and inst.rd != "x0":
                            loc_defined.add(inst.rd)

                    loc_ext_rd = None
                    for inst in reversed(window):
                        if inst.rd and inst.rd != "x0":
                            overwritten = any(w.rd == inst.rd for w in window[window.index(inst) + 1 :])
                            if not overwritten:
                                loc_ext_rd = inst.rd
                                break

                    # Use loc_ext_inputs with duplicates preserved - the fused
                    # instruction may need the same register in multiple slots
                    # (e.g., slli a4,a5,2 followed by add a4,a4,a5 needs rs1=a5
                    # and rs2=a5 for the R-type encoding)
                    loc = PatternLocation(
                        block_id=bid,
                        start_idx=start,
                        start_addr=window[0].address,
                        end_addr=window[-1].address,
                        frequency=freq,
                        instr_regs=instr_regs,
                        ext_rd=loc_ext_rd,
                        ext_rs1=loc_ext_inputs[0] if len(loc_ext_inputs) > 0 else None,
                        ext_rs2=loc_ext_inputs[1] if len(loc_ext_inputs) > 1 else None,
                        ext_rs3=loc_ext_inputs[2] if len(loc_ext_inputs) > 2 else None,
                        original_size_bytes=original_size_bytes,
                    )

                    # ── Build candidate ──
                    cand = EnhancedFusionCandidate(
                        instructions=window,
                        pattern=sig.mnemonics,
                        frequency=freq,
                        liveness=lv,
                        block_id=bid,
                        start_idx=start,
                        cycle_savings=savings,
                        fused_latency=fused,
                        fits_baseline_rf=fits,
                        extra_read_ports=max(0, nr - self.BASELINE_READ),
                        extra_write_ports=max(0, nw - self.BASELINE_WRITE),
                        encoding_type=enc,
                        signature=sig,
                        instruction_type=inst_type,
                        has_hardcoded_imm=has_imm,
                        hardcoded_imm_values=tuple(inst.imm for inst in window),
                        imm_should_hardcode=(),  # Filled after aggregation
                        constant_operands=const_ops,
                        imm_stats=None,  # Filled after aggregation
                        static_instances=1,
                        locations=[loc],
                    )

                    # ── Aggregate by compound key ──
                    if agg_key in by_agg_key:
                        existing = by_agg_key[agg_key]
                        existing.frequency += freq
                        existing.static_instances += 1
                        # Add this location (with its own register info)
                        existing.locations.append(loc)
                        # Merge operand constancy across all instances
                        self._merge_constancy(existing.constant_operands, const_ops)
                        # Keep the best savings data
                        if savings > existing.cycle_savings:
                            existing.cycle_savings = savings
                            existing.liveness = lv
                            existing.fused_latency = fused
                    else:
                        by_agg_key[agg_key] = cand

        # ── Post-aggregation: resolve immediate hardcode decisions ──
        for agg_key, cand in by_agg_key.items():
            stats = imm_stats_by_key.get(agg_key)
            if stats is not None:
                cand.imm_stats = stats
                n = len(cand.instructions)
                decisions = stats.get_all_decisions(n, threshold=1.0)
                cand.imm_should_hardcode = tuple(d[0] for d in decisions)
                cand.hardcoded_imm_values = tuple(d[1] for d in decisions)
                cand.has_hardcoded_imm = any(d[0] for d in decisions)

        candidates = list(by_agg_key.values())
        candidates.sort(key=lambda c: c.impact, reverse=True)
        return candidates

    # ──────────────────────────────────────────────────────────────────────
    #  Optimal n-gram selection
    # ──────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────
    #  Scoring (for detailed reports)
    # ──────────────────────────────────────────────────────────────────────

    # ──────────────────────────────────────────────────────────────────────
    #  Speedup estimation (pipelined throughput model)
    # ──────────────────────────────────────────────────────────────────────
