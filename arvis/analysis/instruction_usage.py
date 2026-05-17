"""Workload instruction usage analysis for RTL pruning.

Analyzes the compiled binary to produce a ``WorkloadProfile`` that
describes exactly which hardware features the workload requires.
This profile drives the pruning pipeline: any feature NOT in the
profile can be removed from the RTL.

Usage:
    from arvis.analysis.instruction_usage import analyze_binary
    profile = analyze_binary(instructions)
    # profile.used_registers → {0, 1, 2, 5, 6, ...}
    # profile.used_csr_names → set()  (empty when the workload uses no CSRs)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .models import Instruction
from .registers import normalize_mnemonic

# CSR address → name mapping (RISC-V privileged spec)
CSR_NAMES: Dict[int, str] = {
    0x300: "mstatus",
    0x301: "misa",
    0x304: "mie",
    0x305: "mtvec",
    0x306: "mcounteren",
    0x310: "mstatush",
    0x340: "mscratch",
    0x341: "mepc",
    0x342: "mcause",
    0x343: "mtval",
    0x344: "mip",
    0xB00: "mcycle",
    0xB02: "minstret",
    0xB80: "mcycleh",
    0xB82: "minstreth",
    0xF11: "mvendorid",
    0xF12: "marchid",
    0xF13: "mimpid",
    0xF14: "mhartid",
    # Performance counters
    **{0xB03 + i: f"mhpmcounter{i + 3}" for i in range(29)},
    **{0xB83 + i: f"mhpmcounter{i + 3}h" for i in range(29)},
    **{0x323 + i: f"mhpmevent{i + 3}" for i in range(29)},
    # User-mode counter aliases
    0xC00: "cycle",
    0xC02: "instret",
    0xC80: "cycleh",
    0xC82: "instreth",
    **{0xC03 + i: f"hpmcounter{i + 3}" for i in range(29)},
    **{0xC83 + i: f"hpmcounter{i + 3}h" for i in range(29)},
    # Trigger
    0x7A0: "tselect",
    0x7A1: "tdata1",
    0x7A2: "tdata2",
    0x7A3: "tdata3",
    0x7A8: "mcontext",
    0x7AA: "scontext",
    0x7A4: "tinfo",
    # Debug
    0x7B0: "dcsr",
    0x7B1: "dpc",
    0x7B2: "dscratch0",
    0x7B3: "dscratch1",
}

# CSRs that the Verilator testbench reads internally for verification.
# These must ALWAYS be kept, even if the binary never accesses them.
VERILATOR_REQUIRED_CSRS: Set[str] = {
    "mstatus",  # trap handling (exception save/restore)
    "mepc",  # exception return address
    "mcause",  # exception cause
    "mtvec",  # trap vector
    "mie",  # interrupt enable
    "mip",  # interrupt pending
    "mhartid",  # hart ID (required by spec)
    # NOTE: mcycle/minstret NOT required — sim_main.cpp provides cycle count.
    # Only kept if firmware actually reads them via csrrs.
}

# CSR instruction mnemonics
_CSR_MNEMONICS = {"csrrw", "csrrs", "csrrc", "csrrwi", "csrrsi", "csrrci"}

# Immediate-form mnemonics (use const_int, not register)
_IMM_MNEMONICS = {
    "addi",
    "andi",
    "ori",
    "xori",
    "slli",
    "srli",
    "srai",
    "slti",
    "sltiu",
}

# Mnemonics that indicate multiply usage
_MUL_MNEMONICS = {"mul"}
_MULH_MNEMONICS = {"mulh", "mulhsu", "mulhu"}
_DIV_MNEMONICS = {"div", "divu", "rem", "remu"}


@dataclass
class WorkloadProfile:
    """Complete workload hardware usage profile for pruning decisions."""

    # ── Instruction usage ──
    used_mnemonics: Set[str] = field(default_factory=set)
    mnemonic_counts: Dict[str, int] = field(default_factory=dict)
    total_static_instructions: int = 0

    # ── Register usage ──
    used_registers: Set[int] = field(default_factory=set)
    unused_registers: Set[int] = field(default_factory=set)
    max_register_index: int = 31

    # ── CSR usage ──
    used_csr_addresses: Set[int] = field(default_factory=set)
    used_csr_names: Set[str] = field(default_factory=set)
    required_csr_names: Set[str] = field(default_factory=set)

    # ── Feature flags ──
    uses_compressed: bool = False
    used_compressed_mnemonics: Set[str] = field(default_factory=set)
    uses_mul: bool = False
    uses_mulh: bool = False
    uses_div: bool = False
    uses_float: bool = False
    uses_atomic: bool = False
    uses_interrupts: bool = True  # CONSERVATIVE default: assume yes
    has_mret: bool = False  # mret instruction found in binary

    # ── Branch/memory ──
    used_branch_types: Set[str] = field(default_factory=set)
    used_load_types: Set[str] = field(default_factory=set)
    used_store_types: Set[str] = field(default_factory=set)

    # ── Decoder ──
    # Set of (opcode_group, funct3, funct7) tuples that are used
    used_decoder_cases: Set[Tuple[str, ...]] = field(default_factory=set)

    def summary(self) -> str:
        """Human-readable summary of the workload profile."""
        lines = [
            f"WorkloadProfile: {self.total_static_instructions} instructions, "
            f"{len(self.used_mnemonics)} unique mnemonics",
            f"  Registers: {len(self.used_registers)}/32 used (unused: {sorted(self.unused_registers)})",
            f"  CSRs from binary: {sorted(self.used_csr_names) or 'none'}",
            f"  Required CSRs (Verilator): {sorted(self.required_csr_names)}",
            f"  Compressed: {'yes' if self.uses_compressed else 'no'} ({len(self.used_compressed_mnemonics)} types)",
            f"  MUL: {self.uses_mul}, MULH: {self.uses_mulh}, DIV: {self.uses_div}, FPU: {self.uses_float}",
            f"  Branches: {sorted(self.used_branch_types)}",
            f"  Loads: {sorted(self.used_load_types)}",
            f"  Stores: {sorted(self.used_store_types)}",
        ]
        return "\n".join(lines)


def analyze_binary(instructions: List[Instruction]) -> WorkloadProfile:
    """Analyze a list of decoded instructions to produce a WorkloadProfile.

    Args:
        instructions: Decoded instructions from the binary (from disassembler).

    Returns:
        WorkloadProfile with all hardware usage information.
    """
    profile = WorkloadProfile()
    profile.total_static_instructions = len(instructions)

    mnem_counter: Counter = Counter()
    all_registers: Set[int] = set()

    for inst in instructions:
        mnem = inst.mnemonic
        norm = normalize_mnemonic(mnem)
        mnem_counter[mnem] += 1
        profile.used_mnemonics.add(mnem)

        # Register tracking
        for reg in [inst.rd, inst.rs1, inst.rs2]:
            if reg and reg.startswith("x"):
                try:
                    idx = int(reg[1:])
                    all_registers.add(idx)
                except ValueError:
                    pass

        # Compressed instructions
        if mnem.startswith("c."):
            profile.uses_compressed = True
            profile.used_compressed_mnemonics.add(mnem)

        # CSR instructions
        if norm in _CSR_MNEMONICS:
            # CSR address is the immediate field
            if inst.imm is not None:
                csr_addr = inst.imm & 0xFFF
                profile.used_csr_addresses.add(csr_addr)
                csr_name = CSR_NAMES.get(csr_addr, f"csr_0x{csr_addr:03x}")
                profile.used_csr_names.add(csr_name)

        # Feature detection
        if norm in _MUL_MNEMONICS:
            profile.uses_mul = True
        if norm in _MULH_MNEMONICS:
            profile.uses_mulh = True
        if norm in _DIV_MNEMONICS:
            profile.uses_div = True
        if mnem.startswith("f") and mnem not in ("fence",):
            profile.uses_float = True
        if mnem.startswith("amo") or mnem in ("lr.w", "sc.w"):
            profile.uses_atomic = True

        # Interrupt-related instruction detection
        if norm == "mret":
            profile.has_mret = True

        # Branch/load/store categorization
        if inst.is_branch or inst.is_jump:
            profile.used_branch_types.add(norm)
        if inst.is_load:
            profile.used_load_types.add(norm)
        if inst.is_store:
            profile.used_store_types.add(norm)

    profile.mnemonic_counts = dict(mnem_counter)
    profile.used_registers = all_registers
    profile.unused_registers = set(range(32)) - all_registers
    profile.max_register_index = max(all_registers) if all_registers else 0

    # Required CSRs = Verilator needs + binary needs
    profile.required_csr_names = VERILATOR_REQUIRED_CSRS | profile.used_csr_names

    # Interrupt usage detection (conservative)
    # Only mark as NOT using interrupts when we have strong evidence:
    # - No writes to mie (interrupt enable register)
    # - No writes to mtvec (trap vector — custom handler)
    # - No mret instruction (return from interrupt handler)
    irq_csrs_accessed = bool({"mie", "mtvec"} & profile.used_csr_names)
    if not irq_csrs_accessed and not profile.has_mret:
        profile.uses_interrupts = False
    else:
        profile.uses_interrupts = True

    return profile
