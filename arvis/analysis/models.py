"""Shared data structures used across all analysis passes."""

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple

# Re-export from canonical location for backward compatibility
from .registers import ABI_REG as ABI_REG
from .registers import REG_ABI as REG_ABI
from .registers import normalize_reg as normalize_reg

# ── Instruction ──


@dataclass
class Instruction:
    address: int
    raw: int
    mnemonic: str
    operands_raw: str
    rd: Optional[str] = None
    rs1: Optional[str] = None
    rs2: Optional[str] = None
    imm: Optional[int] = None
    size: int = 4
    branch_target_addr: Optional[int] = None

    @property
    def is_branch(self) -> bool:
        return self.mnemonic in {"beq", "bne", "blt", "bge", "bltu", "bgeu", "c.beqz", "c.bnez"}

    @property
    def is_jump(self) -> bool:
        return self.mnemonic in {"jal", "jalr", "c.j", "c.jal", "c.jr", "c.jalr"}

    @property
    def is_unconditional_jump(self) -> bool:
        if self.mnemonic in ("c.j",):
            return True
        if self.mnemonic == "jal" and (self.rd is None or self.rd == "x0"):
            return True
        return False

    @property
    def is_call(self) -> bool:
        return (self.mnemonic in ("jal", "c.jal") and self.rd == "x1") or (
            self.mnemonic in ("jalr", "c.jalr") and self.rd == "x1"
        )

    @property
    def is_ret(self) -> bool:
        if self.mnemonic == "ret":
            return True
        if self.mnemonic == "jalr" and self.rs1 == "x1" and (self.rd == "x0" or self.rd is None):
            return True
        if self.mnemonic == "c.jr" and self.rs1 == "x1":
            return True
        return False

    @property
    def is_terminator(self) -> bool:
        return self.is_branch or self.is_jump or self.is_ret

    @property
    def is_load(self) -> bool:
        return self.mnemonic in {"lb", "lbu", "lh", "lhu", "lw", "c.lw", "c.lwsp", "flw"}

    @property
    def is_store(self) -> bool:
        return self.mnemonic in {"sb", "sh", "sw", "c.sw", "c.swsp", "fsw"}

    @property
    def is_mul(self) -> bool:
        return self.mnemonic in {"mul", "mulh", "mulhsu", "mulhu"}

    @property
    def is_div(self) -> bool:
        return self.mnemonic in {"div", "divu", "rem", "remu"}

    @property
    def defs(self) -> Set[str]:
        if self.rd and self.rd != "x0":
            return {self.rd}
        return set()

    @property
    def uses(self) -> Set[str]:
        regs = set()
        if self.rs1 and self.rs1 != "x0":
            regs.add(self.rs1)
        if self.rs2 and self.rs2 != "x0":
            regs.add(self.rs2)
        return regs

    def __repr__(self):
        return f"0x{self.address:08x}: {self.mnemonic} {self.operands_raw}"


# ── Basic Block ──


@dataclass
class BasicBlock:
    id: int
    start_addr: int
    end_addr: int
    instructions: List[Instruction] = field(default_factory=list)
    successors: List[int] = field(default_factory=list)
    predecessors: List[int] = field(default_factory=list)
    execution_count: int = 0

    @property
    def size(self) -> int:
        return len(self.instructions)


# ── Loop ──


@dataclass
class Loop:
    header_block_id: int
    body_block_ids: Set[int]
    back_edge: Tuple[int, int]
    exit_block_ids: Set[int]
    nesting_depth: int = 0
    parent_loop: Optional["Loop"] = None
    child_loops: List["Loop"] = field(default_factory=list)
    trip_count_estimate: int = 0
    total_dynamic_instructions: int = 0
    hotness_score: float = 0.0


# ── Dynamic Profile ──


@dataclass
class DynamicProfile:
    block_exec_counts: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    instruction_exec_counts: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    total_instructions: int = 0
    # Register values observed at each instruction execution
    # Key: (block_id, instr_idx, reg_name), Value: list of observed values
    register_values: Dict[Tuple[int, int, str], List[int]] = field(default_factory=lambda: defaultdict(list))


# ── Liveness ──


class RegisterStatus(Enum):
    EXTERNAL_INPUT = auto()
    EXTERNAL_OUTPUT = auto()
    INTERNAL_ONLY = auto()
    INPUT_AND_OUTPUT = auto()


@dataclass
class LivenessResult:
    classifications: Dict[str, RegisterStatus] = field(default_factory=dict)
    must_read: Set[str] = field(default_factory=set)
    must_write: Set[str] = field(default_factory=set)
    truly_eliminable: Set[str] = field(default_factory=set)
    all_defs: Set[str] = field(default_factory=set)

    @property
    def min_read_ports(self) -> int:
        return len(self.must_read)

    @property
    def min_write_ports_eliminate(self) -> int:
        return len(self.must_write)

    @property
    def min_write_ports_keep_all(self) -> int:
        return len(self.all_defs)


# ── Accelerator Assessment ──


@dataclass
class AcceleratorAssessment:
    loop: Loop
    overall_score: float = 0.0
    area_cost: float = 0.0
    hotness: float = 0.0
    regularity: float = 0.0
    memory_pattern: float = 0.0
    memory_pattern_type: str = ""
    compute_intensity: float = 0.0
    io_complexity: float = 0.0
    trip_count_score: float = 0.0
    live_in_regs: Set[str] = field(default_factory=set)
    live_out_regs: Set[str] = field(default_factory=set)
    dominant_ops: List[str] = field(default_factory=list)
    recommended: bool = False
    recommendation: str = ""


# ── Fusion Candidate ──


@dataclass
class FusionCandidate:
    instructions: List[Instruction]
    pattern: Tuple[str, ...]
    frequency: int
    liveness: LivenessResult
    block_id: int = 0
    start_idx: int = 0
    cycle_savings: int = 0
    fused_latency: int = 1
    fits_baseline_rf: bool = True
    extra_read_ports: int = 0
    extra_write_ports: int = 0
    encoding_type: str = ""

    @property
    def impact(self) -> int:
        return self.frequency * self.cycle_savings


# ── Custom Instruction Spec (for codegen) ──


class InstructionType(Enum):
    COMPUTE_ONLY = auto()
    LOAD_COMPUTE = auto()
    COMPUTE_STORE = auto()
    LOAD_COMPUTE_STORE = auto()
    MULTI_CYCLE = auto()


# ── RTL Patch ──


# ═══════════════════════════════════════════════════════════════════════════
# NEW: Data Flow Graph and Accelerator Candidate models for unified ILP
# ═══════════════════════════════════════════════════════════════════════════
