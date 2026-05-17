"""
RTL Pruning for cv32e40p via parameter-based gating.

Instead of regex-based code removal (which breaks Verilator), this module
adds new parameters and `generate if` blocks to gate unused features.
This is the same pattern CV32E40P already uses for COREV_PULP, FPU, etc.

Approach:
  1. Add ENABLE_DIV parameter to cv32e40p_alu.sv, wrap divider in generate-if
  2. Propagate parameters up through cv32e40p_core.sv and cv32e40p_top.sv
  3. At Verilator build time, set -GENABLE_DIV=0 etc. to disable features

Features that can be gated:
  - ENABLE_DIV: divider (cv32e40p_alu_div) — ~1,111 cells saved
  - ENABLE_MUL: entire multiplier — ~6,632 cells saved
  - ENABLE_MULH: mulh state machine — ~47 cells saved
  - ENABLE_DOT_MUL: dot product hardware — ~247 cells saved
  - ENABLE_MSU: MSU subtract path — ~247 cells saved
  - ENABLE_POPCNT: population count unit — ~164 cells saved
  - ENABLE_FF1: find-first-one unit — ~387 cells saved
  - ENABLE_REGFILE_WR_B: register file write port B — ~1,113 cells saved
  - ENABLE_REGFILE_RD_C: register file read port C — ~1,984 cells saved
  - DEBUG_TRIGGER_EN: debug trigger registers — ~3 cells saved
  - NUM_MHPMCOUNTERS: HPM counters — ~818 cells saved (1->0)
  - USED_REGS_MASK: per-register disable — ~64 FFs per unused register
  - Already existing: COREV_PULP=0 disables PULP extensions
  - Already existing: FPU=0 disables floating point
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .base import RTLWorkspace

# ======================================================================
# CSR Storage Map -- maps CSR names to register names in cs_registers.sv
# ======================================================================

_CSR_STORAGE_MAP: Dict[str, List[str]] = {
    "mscratch": ["mscratch_q"],
    "mtval": ["mtval_q"],
    "dscratch0": ["dscratch0_q"],
    "dscratch1": ["dscratch1_q"],
    "dcsr": ["dcsr_q"],
    "depc": ["depc_q"],
    "mcounteren": ["mcounteren_q"],
}

# ======================================================================
# PULP-only immediate mux labels in cv32e40p_id_stage.sv
# ======================================================================

_PULP_ONLY_IMM_LABELS = [
    "IMMB_S2",
    "IMMB_BI",
    "IMMB_S3",
    "IMMB_VS",
    "IMMB_VU",
    "IMMB_SHUF",
    "IMMB_CLIP",
]

# ======================================================================
# Pruning Configuration
# ======================================================================


@dataclass
class PruneConfig:
    """Configuration for RTL pruning based on workload analysis."""

    removable_alu_ops: Set[str] = field(default_factory=set)

    enable_div: bool = True
    enable_mul: bool = True
    enable_mul_h: bool = True
    enable_sleep: bool = True
    enable_debug: bool = True
    enable_hpm: bool = True

    enable_dot_mul: bool = True
    enable_msu: bool = True
    enable_popcnt: bool = True
    enable_ff1: bool = True
    enable_regfile_wr_b: bool = True
    enable_regfile_rd_c: bool = True
    enable_interrupts: bool = True  # Conservative default: keep interrupt path

    corev_pulp: int = 0
    fpu: int = 0
    num_mhpmcounters: int = 1

    debug_trigger_en: int = 1

    used_regs_mask: int = 0xFFFFFFFF
    unused_registers: List[int] = field(default_factory=list)

    required_csrs: Set[str] = field(default_factory=set)
    removable_csr_storage: Set[str] = field(default_factory=set)

    mul_modes_used: Set[str] = field(default_factory=set)
    mul_modes_removable: Set[str] = field(default_factory=set)

    # Derived fields for pipeline reporting
    max_used_register: int = 31
    removable_mul_modes: Set[str] = field(default_factory=set)
    removable_csr_labels: Set[str] = field(default_factory=set)

    removable_opcode_groups: Set[str] = field(default_factory=set)
    enable_compressed: bool = True

    # ── HW Loop support ──
    hw_loop: int = 0  # 0=disabled, 2-8=number of nested HW loop levels
    hw_loop_cnt_width: int = 32  # counter register width

    _OPCODE_MAP = {
        "jal": "OPCODE_JAL",
        "jalr": "OPCODE_JALR",
        "beq": "OPCODE_BRANCH",
        "bne": "OPCODE_BRANCH",
        "blt": "OPCODE_BRANCH",
        "bge": "OPCODE_BRANCH",
        "bltu": "OPCODE_BRANCH",
        "bgeu": "OPCODE_BRANCH",
        "sb": "OPCODE_STORE",
        "sh": "OPCODE_STORE",
        "sw": "OPCODE_STORE",
        "lb": "OPCODE_LOAD",
        "lh": "OPCODE_LOAD",
        "lw": "OPCODE_LOAD",
        "lbu": "OPCODE_LOAD",
        "lhu": "OPCODE_LOAD",
        "lr.w": "OPCODE_AMO",
        "sc.w": "OPCODE_AMO",
        "amoswap.w": "OPCODE_AMO",
        "amoadd.w": "OPCODE_AMO",
        "amoand.w": "OPCODE_AMO",
        "amoor.w": "OPCODE_AMO",
        "amoxor.w": "OPCODE_AMO",
        "amomax.w": "OPCODE_AMO",
        "amomin.w": "OPCODE_AMO",
        "lui": "OPCODE_LUI",
        "auipc": "OPCODE_AUIPC",
        "addi": "OPCODE_OPIMM",
        "slti": "OPCODE_OPIMM",
        "sltiu": "OPCODE_OPIMM",
        "xori": "OPCODE_OPIMM",
        "ori": "OPCODE_OPIMM",
        "andi": "OPCODE_OPIMM",
        "slli": "OPCODE_OPIMM",
        "srli": "OPCODE_OPIMM",
        "srai": "OPCODE_OPIMM",
        "add": "OPCODE_OP",
        "sub": "OPCODE_OP",
        "sll": "OPCODE_OP",
        "slt": "OPCODE_OP",
        "sltu": "OPCODE_OP",
        "xor": "OPCODE_OP",
        "srl": "OPCODE_OP",
        "sra": "OPCODE_OP",
        "or": "OPCODE_OP",
        "and": "OPCODE_OP",
        "mul": "OPCODE_OP",
        "mulh": "OPCODE_OP",
        "mulhsu": "OPCODE_OP",
        "mulhu": "OPCODE_OP",
        "div": "OPCODE_OP",
        "divu": "OPCODE_OP",
        "rem": "OPCODE_OP",
        "remu": "OPCODE_OP",
        "fence": "OPCODE_FENCE",
        "fence.i": "OPCODE_FENCE",
        "ecall": "OPCODE_SYSTEM",
        "ebreak": "OPCODE_SYSTEM",
        "wfi": "OPCODE_SYSTEM",
        "csrrw": "OPCODE_SYSTEM",
        "csrrs": "OPCODE_SYSTEM",
        "csrrc": "OPCODE_SYSTEM",
        "csrrwi": "OPCODE_SYSTEM",
        "csrrsi": "OPCODE_SYSTEM",
        "csrrci": "OPCODE_SYSTEM",
        "mret": "OPCODE_SYSTEM",
        "dret": "OPCODE_SYSTEM",
    }

    _ALL_OPCODE_GROUPS = {
        "OPCODE_JAL",
        "OPCODE_JALR",
        "OPCODE_BRANCH",
        "OPCODE_STORE",
        "OPCODE_LOAD",
        "OPCODE_AMO",
        "OPCODE_LUI",
        "OPCODE_AUIPC",
        "OPCODE_OPIMM",
        "OPCODE_OP",
        "OPCODE_FENCE",
        "OPCODE_SYSTEM",
        "OPCODE_OP_FP",
        "OPCODE_OP_FMADD",
        "OPCODE_OP_FMSUB",
        "OPCODE_OP_FNMSUB",
        "OPCODE_OP_FNMADD",
        "OPCODE_STORE_FP",
        "OPCODE_LOAD_FP",
        "OPCODE_CUSTOM_0",
        "OPCODE_CUSTOM_1",
        "OPCODE_CUSTOM_2",
        "OPCODE_CUSTOM_3",
    }

    @classmethod
    def from_analysis(
        cls,
        alu_removable_ops: Set[str],
        used_instructions: Set[str],
        used_csrs: Optional[Set[str]] = None,
        unused_registers: Optional[List[int]] = None,
        required_csrs: Optional[Set[str]] = None,
        mul_modes_used: Optional[Set[str]] = None,
        mul_modes_removable: Optional[Set[str]] = None,
        # Pipeline-style parameters (alternative API)
        used_registers: Optional[Set[int]] = None,
        mul_usage: object = None,
        workload_profile: object = None,
    ) -> "PruneConfig":
        """Build config from workload analysis results.

        Accepts either direct parameters (unused_registers, required_csrs, etc.)
        or higher-level objects from the pipeline (used_registers, mul_usage,
        workload_profile).
        """
        config = cls(removable_alu_ops=set(alu_removable_ops))

        div_insns = {"div", "divu", "rem", "remu"}
        config.enable_div = bool(div_insns & used_instructions)

        mul_insns = {"mul", "mulh", "mulhsu", "mulhu"}
        config.enable_mul = bool(mul_insns & used_instructions)

        mulh_insns = {"mulh", "mulhsu"}
        config.enable_mul_h = bool(mulh_insns & used_instructions)

        config.enable_sleep = "wfi" in used_instructions
        config.enable_debug = "ebreak" in used_instructions

        # Interrupt detection (from workload profile)
        if workload_profile is not None:
            wp_interrupts = getattr(workload_profile, "uses_interrupts", True)
            config.enable_interrupts = wp_interrupts
        config.debug_trigger_en = 1 if config.enable_debug else 0

        config.corev_pulp = 0
        config.fpu = 0

        # Auto-detect PULP-only features when COREV_PULP=0
        if config.corev_pulp == 0:
            config.enable_dot_mul = False
            config.enable_msu = False
            config.enable_popcnt = False
            config.enable_ff1 = False
            config.enable_regfile_wr_b = False
        else:
            config.enable_dot_mul = True
            config.enable_msu = True
            config.enable_popcnt = True
            config.enable_ff1 = True
            config.enable_regfile_wr_b = True

        # Read port C: only needed for FPU (R4 fused) or PULP
        if config.fpu == 0 and config.corev_pulp == 0:
            config.enable_regfile_rd_c = False
        else:
            config.enable_regfile_rd_c = True

        # Multiplier mode analysis — from direct params or MulUsageResult
        if mul_usage is not None:
            config.mul_modes_used = set(getattr(mul_usage, "used_modes", set()))
            config.mul_modes_removable = set(getattr(mul_usage, "removable_modes", set()))
            config.removable_mul_modes = config.mul_modes_removable
            if getattr(mul_usage, "can_remove_dot_hardware", False):
                config.enable_dot_mul = False
            if getattr(mul_usage, "can_remove_mulh_fsm", False):
                config.enable_mul_h = False
            if "MUL_MSU32" in config.mul_modes_removable:
                config.enable_msu = False
        if mul_modes_used is not None:
            config.mul_modes_used = set(mul_modes_used)
        if mul_modes_removable is not None:
            config.mul_modes_removable = set(mul_modes_removable)
            config.removable_mul_modes = config.mul_modes_removable
            if "MUL_DOT8" in mul_modes_removable or "MUL_DOT16" in mul_modes_removable:
                config.enable_dot_mul = False
            if "MUL_MSU32" in mul_modes_removable:
                config.enable_msu = False

        # WorkloadProfile integration
        if workload_profile is not None:
            wp = workload_profile
            if used_registers is None:
                used_registers = getattr(wp, "used_registers", None)
            if required_csrs is None:
                required_csrs = getattr(wp, "required_csr_names", None)
            # CSR labels removable from workload profile
            wp_csr_names: set = getattr(wp, "used_csr_names", set())
            if not wp_csr_names:
                used_csrs = set()

        # CSR/HPM analysis
        if used_csrs is not None and len(used_csrs) == 0:
            config.num_mhpmcounters = 0
            config.enable_hpm = False
        else:
            config.num_mhpmcounters = 0
            config.enable_hpm = False
            if used_csrs:
                hpm_csrs = {c for c in used_csrs if c.startswith("mhpm") or c.startswith("hpm")}
                if hpm_csrs:
                    config.num_mhpmcounters = 1
                    config.enable_hpm = True

        # Required CSRs
        if required_csrs is not None:
            config.required_csrs = set(required_csrs)

        # CSR storage pruning
        if config.required_csrs:
            for csr_name in _CSR_STORAGE_MAP:
                if csr_name not in config.required_csrs:
                    config.removable_csr_storage.add(csr_name)

        # CSR case label pruning (workload-driven)
        # All CSR addresses in the read mux of cs_registers.sv
        _ALL_CSR_LABELS = {
            "CSR_MSTATUS",
            "CSR_MISA",
            "CSR_MIE",
            "CSR_MTVEC",
            "CSR_MSCRATCH",
            "CSR_MEPC",
            "CSR_MCAUSE",
            "CSR_MIP",
            "CSR_MTVAL",
            "CSR_MHARTID",
            "CSR_MVENDORID",
            "CSR_MARCHID",
            "CSR_MIMPID",
            "CSR_MCOUNTEREN",
            "CSR_MCOUNTINHIBIT",
            "CSR_MCYCLE",
            "CSR_MCYCLEH",
            "CSR_MINSTRET",
            "CSR_MINSTRETH",
            # User mode CSRs
            "CSR_USTATUS",
            "CSR_UTVEC",
            "CSR_UEPC",
            "CSR_UCAUSE",
            "CSR_UHARTID",
            "CSR_PRIVLV",
            "CSR_ZFINX",
        }
        if config.required_csrs:
            # Convert analysis names (lowercase) to RTL labels (CSR_ + uppercase)
            required_labels = {f"CSR_{name.upper()}" for name in config.required_csrs}
            config.removable_csr_labels = _ALL_CSR_LABELS - required_labels

        # Unused registers — from direct param or used_registers set
        if unused_registers is not None:
            config.unused_registers = list(unused_registers)
            mask = 0xFFFFFFFF
            for reg in unused_registers:
                if 0 <= reg <= 31:
                    mask &= ~(1 << reg)
            config.used_regs_mask = mask
        elif used_registers is not None:
            all_regs = set(range(32))
            unused = sorted(all_regs - used_registers)
            config.unused_registers = unused
            config.max_used_register = max(used_registers) if used_registers else 31
            mask = 0xFFFFFFFF
            for reg in unused:
                if 0 <= reg <= 31:
                    mask &= ~(1 << reg)
            config.used_regs_mask = mask

        # Compressed instructions
        compressed_insns = {i for i in used_instructions if i.startswith("c.")}
        _COMPRESSED_PSEUDOS = {"j", "jr", "mv", "li", "ret", "nop", "bnez", "beqz"}
        compressed_pseudos = _COMPRESSED_PSEUDOS & used_instructions
        config.enable_compressed = bool(compressed_insns or compressed_pseudos)

        # Determine which decoder opcode groups are used
        used_groups: Set[str] = set()
        _BASE_MAP = {
            "c.add": "add",
            "c.mv": "add",
            "c.li": "addi",
            "c.lui": "lui",
            "c.addi": "addi",
            "c.addi4spn": "addi",
            "c.addi16sp": "addi",
            "c.slli": "slli",
            "c.srli": "srli",
            "c.srai": "srai",
            "c.andi": "andi",
            "c.sub": "sub",
            "c.xor": "xor",
            "c.or": "or",
            "c.and": "and",
            "c.lw": "lw",
            "c.lwsp": "lw",
            "c.sw": "sw",
            "c.swsp": "sw",
            "c.beqz": "beq",
            "c.bnez": "bne",
            "c.j": "jal",
            "c.jal": "jal",
            "c.jr": "jalr",
            "c.jalr": "jalr",
            "c.ebreak": "ebreak",
        }
        for insn in used_instructions:
            base_insn = insn
            if insn.startswith("c."):
                base_insn = _BASE_MAP.get(insn, insn.replace("c.", ""))
            group = cls._OPCODE_MAP.get(base_insn)
            if group:
                used_groups.add(group)

        already_gated = {
            "OPCODE_OP_FP",
            "OPCODE_OP_FMADD",
            "OPCODE_OP_FMSUB",
            "OPCODE_OP_FNMSUB",
            "OPCODE_OP_FNMADD",
            "OPCODE_STORE_FP",
            "OPCODE_LOAD_FP",
            "OPCODE_CUSTOM_0",
            "OPCODE_CUSTOM_1",
            "OPCODE_CUSTOM_2",
            "OPCODE_CUSTOM_3",
        }
        config.removable_opcode_groups = cls._ALL_OPCODE_GROUPS - used_groups - already_gated

        return config

    def synthesis_parameters(self) -> Dict[str, int]:
        """Return parameters to pass to synthesis/Verilator."""
        params = {
            "COREV_PULP": self.corev_pulp,
            "FPU": self.fpu,
            "NUM_MHPMCOUNTERS": self.num_mhpmcounters,
            "ENABLE_DIV": 1 if self.enable_div else 0,
            "ENABLE_MUL": 1 if self.enable_mul else 0,
            "ENABLE_MULH": 1 if self.enable_mul_h else 0,
            "ENABLE_SLEEP": 1 if self.enable_sleep else 0,
            "ENABLE_DOT_MUL": 1 if self.enable_dot_mul else 0,
            "ENABLE_MSU": 1 if self.enable_msu else 0,
            "ENABLE_POPCNT": 1 if self.enable_popcnt else 0,
            "ENABLE_FF1": 1 if self.enable_ff1 else 0,
            "ENABLE_REGFILE_WR_B": 1 if self.enable_regfile_wr_b else 0,
            "DEBUG_TRIGGER_EN": self.debug_trigger_en,
            "USED_REGS_MASK": self.used_regs_mask,
        }
        if not self.enable_mul:
            params["ENABLE_MULH"] = 0
            params["ENABLE_DOT_MUL"] = 0
            params["ENABLE_MSU"] = 0
        # HW_LOOP parameter: number of nested hardware loop levels (0=disabled)
        params["HW_LOOP"] = self.hw_loop
        if self.hw_loop > 0 and self.hw_loop_cnt_width < 32:
            params["CNT_WIDTH"] = self.hw_loop_cnt_width
        return params

    def verilator_flags(self) -> List[str]:
        """Return -G flags for Verilator."""
        return [f"-G{k}={v}" for k, v in self.synthesis_parameters().items()]


# ======================================================================
# Pruning Statistics
# ======================================================================


@dataclass
class PruneStats:
    """Statistics from a pruning pass."""

    file: str
    features_added: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class PruneReport:
    """Report from all pruning operations."""

    config: PruneConfig
    file_stats: List[PruneStats] = field(default_factory=list)

    @property
    def total_features(self) -> int:
        return sum(len(s.features_added) for s in self.file_stats)

    def summary(self) -> str:
        lines: List[str] = []
        cfg = self.config
        params = cfg.synthesis_parameters()

        lines.append("")
        lines.append("=" * 58)
        lines.append("            RTL Pruning Report")
        lines.append("=" * 58)

        # Synthesis parameters
        lines.append("")
        lines.append("-- Synthesis Parameters --")
        for k, v in params.items():
            if k == "USED_REGS_MASK":
                lines.append(f"  {k:<25}= 0x{v:08X}")
            else:
                lines.append(f"  {k:<25}= {v}")

        # Feature decisions
        lines.append("")
        lines.append("-- Feature Decisions --")

        def _dec(name: str, enabled: bool, keep: str, rm: str) -> str:
            mark = "KEEP" if enabled else "REMOVE"
            reason = keep if enabled else rm
            return f"  {name:<30} {mark:<10} ({reason})"

        lines.append(_dec("Divider (ENABLE_DIV)", cfg.enable_div, "div/rem", "no div/rem"))
        lines.append(_dec("Multiplier (ENABLE_MUL)", cfg.enable_mul, "mul/mulh", "no mul"))
        lines.append(_dec("MULH FSM (ENABLE_MULH)", cfg.enable_mul_h, "mulh/mulhsu", "no mulh"))
        lines.append(_dec("Sleep unit (ENABLE_SLEEP)", cfg.enable_sleep, "wfi", "no wfi"))
        lines.append(
            _dec(
                "Dot product (DOT_MUL)",
                cfg.enable_dot_mul,
                "SIMD",
                "PULP",
            )
        )
        lines.append(
            _dec(
                "MSU path (ENABLE_MSU)",
                cfg.enable_msu,
                "MAC",
                "PULP",
            )
        )
        lines.append(_dec("Popcnt (ENABLE_POPCNT)", cfg.enable_popcnt, "PULP", "PULP"))
        lines.append(_dec("FF1 (ENABLE_FF1)", cfg.enable_ff1, "PULP", "PULP"))
        lines.append(
            _dec(
                "Regfile write port B",
                cfg.enable_regfile_wr_b,
                "PULP post-inc",
                "PULP post-inc",
            )
        )
        lines.append(_dec("Debug triggers", cfg.enable_debug, "ebreak", "ebreak"))
        lines.append(_dec("HPM counters", cfg.enable_hpm, "CSR access", "CSR access"))

        # Register file
        if cfg.unused_registers:
            lines.append("")
            lines.append("-- Register File --")
            n = len(cfg.unused_registers)
            lines.append(f"  Used:    {32 - n}/32 registers")
            lines.append(f"  Unused:  x{cfg.unused_registers}")
            lines.append(f"  Savings: {n} x 32 = {n * 32} FFs disabled")

        # ALU ops
        if cfg.removable_alu_ops:
            lines.append("")
            lines.append(f"-- ALU Operations ({len(cfg.removable_alu_ops)} removable) --")
            ops = sorted(cfg.removable_alu_ops)
            for i in range(0, len(ops), 4):
                chunk = ", ".join(ops[i : i + 4])
                lines.append(f"    {chunk}")

        # Decoder opcode groups
        if cfg.removable_opcode_groups:
            lines.append("")
            lines.append("-- Decoder Opcode Groups Removed --")
            for g in sorted(cfg.removable_opcode_groups):
                lines.append(f"  x {g}")

        # CSR pruning
        lines.append("")
        lines.append("-- CSR Pruning --")
        lines.append(f"  NUM_MHPMCOUNTERS: {cfg.num_mhpmcounters}")
        if cfg.removable_csr_storage:
            lines.append(f"  Removable CSR storage: {sorted(cfg.removable_csr_storage)}")
        if cfg.required_csrs:
            lines.append(f"  Required CSRs: {sorted(cfg.required_csrs)}")

        # RTL modifications
        lines.append("")
        lines.append("-- RTL Modifications --")
        for s in self.file_stats:
            if s.features_added:
                lines.append(f"  {s.file}:")
                for feat in s.features_added:
                    lines.append(f"    + {feat}")

        # Verilator flags
        lines.append("")
        lines.append("-- Verilator Flags --")
        lines.append(f"  {' '.join(cfg.verilator_flags())}")

        return "\n".join(lines)


# ======================================================================
# Parameter insertion helpers
# ======================================================================


def _add_parameter_to_module(
    text: str,
    module_name: str,
    param_name: str,
    param_value: str,
) -> Tuple[str, bool]:
    """Add a parameter to a module's parameter list."""
    if re.search(rf"\b{param_name}\b", text):
        return text, False

    pattern = rf"(module\s+{module_name}\b.*?#\s*\()"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        insert_pos = match.end()
        text = text[:insert_pos] + f"\n    parameter {param_name} = {param_value}," + text[insert_pos:]
        return text, True

    pattern2 = rf"(module\s+{module_name}\b[^(]*?)(\s*\()"
    match2 = re.search(pattern2, text, re.DOTALL)
    if match2:
        text = text[: match2.end(1)] + f"\n#(\n    parameter {param_name} = {param_value}\n) " + text[match2.start(2) :]
        return text, True

    return text, False


def _wrap_in_generate_if(
    text: str,
    start_marker: str,
    end_marker: str,
    condition: str,
    else_body: str,
) -> Tuple[str, bool]:
    """Wrap a block of code between markers in a generate-if."""
    guard_marker = f"// ARVIS_GATED: {condition}"
    if guard_marker in text:
        return text, False

    lines = text.split("\n")
    result = []
    in_block = False
    start_idx = -1
    end_idx = -1

    for i, line in enumerate(lines):
        if start_marker in line and not in_block:
            start_idx = i
            in_block = True
        if end_marker in line and in_block:
            end_idx = i
            break

    if start_idx < 0 or end_idx < 0:
        return text, False

    indent = "  "
    for i, line in enumerate(lines):
        if i == start_idx:
            result.append(f"{indent}generate {guard_marker}")
            gen_label = condition.replace("==", "_eq_").replace(" ", "_")
            result.append(f"{indent}if ({condition}) begin : gen_{gen_label}")
        if start_idx <= i <= end_idx:
            result.append(f"  {line}")
        else:
            result.append(line)
        if i == end_idx:
            gen_no_label = condition.replace("==", "_eq_").replace(" ", "_")
            result.append(f"{indent}end else begin : gen_no_{gen_no_label}")
            for else_line in else_body.strip().split("\n"):
                result.append(f"  {indent}{else_line}")
            result.append(f"{indent}end")
            result.append(f"{indent}endgenerate")

    return "\n".join(result), True


# ======================================================================
# Per-file modifications
# ======================================================================


def _gate_divider_in_alu(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Add ENABLE_DIV parameter to cv32e40p_alu.sv."""
    stats = PruneStats(file="rtl/cv32e40p_alu.sv")

    text, added = _add_parameter_to_module(text, "cv32e40p_alu", "ENABLE_DIV", "1")
    if added:
        stats.features_added.append("Added parameter ENABLE_DIV to cv32e40p_alu")

    div_else = """\
    // Divider disabled: tie off outputs
    assign result_div = '0;
    assign div_ready = 1'b1;"""

    text, wrapped = _wrap_in_generate_if(
        text,
        start_marker="cv32e40p_alu_div alu_div_i",
        end_marker=");",
        condition="ENABLE_DIV",
        else_body=div_else,
    )
    if wrapped:
        stats.features_added.append("Wrapped divider in generate if (ENABLE_DIV)")

    return text, stats


def _propagate_enable_div_to_top(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Add parameters to cv32e40p_top.sv."""
    stats = PruneStats(file="rtl/cv32e40p_top.sv")

    for param in ["ENABLE_DIV", "ENABLE_MULH", "ENABLE_SLEEP", "ENABLE_MUL"]:
        text, added = _add_parameter_to_module(text, "cv32e40p_top", param, "1")
        if added:
            stats.features_added.append(f"Added parameter {param} to cv32e40p_top")

    return text, stats


def _gate_mulh_in_mult(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Add ENABLE_MULH parameter to cv32e40p_mult.sv."""
    stats = PruneStats(file="rtl/cv32e40p_mult.sv")

    text, added = _add_parameter_to_module(text, "cv32e40p_mult", "ENABLE_MULH", "1")
    if added:
        stats.features_added.append("Added parameter ENABLE_MULH to cv32e40p_mult")

    # Gate the MUL_H FSM entry + tie mulh_active_o when disabled
    mulh_guard = "// ARVIS_GATED: ENABLE_MULH"
    if mulh_guard not in text:
        # 1. Gate FSM entry
        old = "if ((operator_i == MUL_H) && enable_i) begin"
        new = f"if (ENABLE_MULH && (operator_i == MUL_H) && enable_i) begin {mulh_guard}"
        if old in text:
            text = text.replace(old, new, 1)

        # 2. Gate mulh_active_o output to force 0
        # This eliminates 6 mux selectors + state/carry FFs
        old_active = "mulh_active_o    = 1'b1;"
        new_active = "mulh_active_o    = ENABLE_MULH ? 1'b1 : 1'b0; // ARVIS_GATED: ENABLE_MULH_ACTIVE"
        if old_active in text:
            text = text.replace(old_active, new_active, 1)

        stats.features_added.append("Gated MUL_H FSM + mulh_active_o (ENABLE_MULH)")

    return text, stats


def _gate_mulh_in_ex_stage(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Add ENABLE_MULH parameter to cv32e40p_ex_stage.sv."""
    stats = PruneStats(file="rtl/cv32e40p_ex_stage.sv")
    text, added = _add_parameter_to_module(text, "cv32e40p_ex_stage", "ENABLE_MULH", "1")
    if added:
        stats.features_added.append("Added parameter ENABLE_MULH to cv32e40p_ex_stage")
    return text, stats


def _gate_sleep_in_core(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Add ENABLE_SLEEP and other parameters to cv32e40p_core.sv."""
    stats = PruneStats(file="rtl/cv32e40p_core.sv")

    for param in ["ENABLE_DIV", "ENABLE_MUL", "ENABLE_MULH", "ENABLE_SLEEP"]:
        text, added = _add_parameter_to_module(text, "cv32e40p_core", param, "1")
        if added:
            stats.features_added.append(f"Added parameter {param} to cv32e40p_core")

    sleep_else = """\
    // Sleep disabled: always awake
    assign clock_en   = 1'b1;
    assign core_sleep_o = 1'b0;
    assign core_busy_o = 1'b1;"""

    text, wrapped = _wrap_in_generate_if(
        text,
        start_marker="cv32e40p_sleep_unit #(",
        end_marker=");",
        condition="ENABLE_SLEEP",
        else_body=sleep_else,
    )
    if wrapped:
        stats.features_added.append("Wrapped sleep_unit in generate if (ENABLE_SLEEP)")

    return text, stats


# ======================================================================
# Case-statement pruning
# ======================================================================


def _prune_alu_case_entries(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Remove unused ALU operation case entries from cv32e40p_alu.sv."""
    stats = PruneStats(file="rtl/cv32e40p_alu.sv")
    removable = config.removable_alu_ops
    if not removable:
        return text, stats

    removed_count = 0
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        case_match = re.match(r"^(\s+)((?:ALU_\w+(?:\s*,\s*)?)+)\s*:\s*(.*)", line)
        if case_match:
            indent = case_match.group(1)
            ops_str = case_match.group(2)
            rest = case_match.group(3)
            ops = [op.strip() for op in ops_str.split(",") if op.strip()]
            all_removable = all(op in removable for op in ops)
            some_removable = any(op in removable for op in ops)

            if all_removable:
                result.append(f"{indent}// PRUNED: {ops_str}")
                if "begin" in rest:
                    depth = 1
                    i += 1
                    while i < len(lines) and depth > 0:
                        s = lines[i].strip()
                        if "begin" in s:
                            depth += 1
                        if s == "end" or s.startswith("end "):
                            depth -= 1
                        i += 1
                    removed_count += len(ops)
                    continue
                else:
                    removed_count += len(ops)
                    i += 1
                    continue
            elif some_removable:
                kept_ops = [op for op in ops if op not in removable]
                removed_ops = [op for op in ops if op in removable]
                new_label = ", ".join(kept_ops)
                result.append(f"{indent}{new_label}: {rest}")
                removed_count += len(removed_ops)
                i += 1
                continue

        result.append(line)
        i += 1

    if removed_count > 0:
        stats.features_added.append(f"Pruned {removed_count} unused ALU case entries from result mux and comparator")
    return "\n".join(result), stats


def _prune_decoder_opcode_blocks(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Remove entire OPCODE_X case blocks from cv32e40p_decoder.sv."""
    stats = PruneStats(file="rtl/cv32e40p_decoder.sv")
    removable = config.removable_opcode_groups
    if not removable:
        return text, stats

    lines = text.split("\n")
    result = []
    removed_count = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        matched_opcode = None

        for opcode in removable:
            if stripped.startswith(f"{opcode}:") and "begin" in stripped:
                matched_opcode = opcode
                break
            if stripped.startswith(f"{opcode},") or stripped == f"{opcode},":
                j = i
                label_lines = []
                while j < len(lines):
                    ls = lines[j].strip()
                    label_lines.append(ls)
                    if ": begin" in ls or ":begin" in ls:
                        break
                    j += 1
                all_labels = " ".join(label_lines)
                labels = [
                    lb.strip().rstrip(",").rstrip(":").strip()
                    for lb in re.split(r"[,:]", all_labels)
                    if lb.strip().startswith("OPCODE_")
                ]
                if labels and all(lb in removable for lb in labels):
                    matched_opcode = labels[0]
                break

        if matched_opcode:
            indent = line[: len(line) - len(line.lstrip())]
            depth = 0
            j = i
            while j < len(lines):
                s = lines[j].strip()
                depth += s.count("begin") - s.count("end")
                depth += s.count("endcase")
                if depth <= 0 and j > i:
                    break
                j += 1
            result.append(f"{indent}// PRUNED: {matched_opcode} block removed (unused by workload)")
            result.append(f"{indent}// {lines[i].strip()}")
            removed_count += 1
            i = j + 1
            continue

        result.append(line)
        i += 1

    if removed_count > 0:
        stats.features_added.append(f"Pruned {removed_count} unused OPCODE case blocks from decoder")
    return "\n".join(result), stats


def _gate_compressed_decoder(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Gate compressed decoder in cv32e40p_if_stage.sv."""
    stats = PruneStats(file="rtl/cv32e40p_if_stage.sv")
    if config.enable_compressed:
        return text, stats

    text, added = _add_parameter_to_module(text, "cv32e40p_if_stage", "ENABLE_COMPRESSED", "1")
    if added:
        stats.features_added.append("Added parameter ENABLE_COMPRESSED")

    comp_else = """\
    // Compressed disabled: pass through 32-bit instructions only
    assign instr_decompressed = instr_aligned;
    assign is_compressed = 1'b0;
    assign illegal_c_insn = 1'b0;"""

    text, wrapped = _wrap_in_generate_if(
        text,
        start_marker="cv32e40p_compressed_decoder",
        end_marker=");",
        condition="ENABLE_COMPRESSED",
        else_body=comp_else,
    )
    if wrapped:
        stats.features_added.append("Wrapped compressed decoder in generate if (ENABLE_COMPRESSED)")
    return text, stats


# ======================================================================
# New pruning functions (session features)
# ======================================================================


def _prune_pulp_immediate_mux(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Comment out PULP-only immediate mux entries in cv32e40p_id_stage.sv.

    When COREV_PULP=0, these mux entries are never selected:
    IMMB_S2, IMMB_BI, IMMB_S3, IMMB_VS, IMMB_VU, IMMB_SHUF, IMMB_CLIP
    """
    stats = PruneStats(file="rtl/cv32e40p_id_stage.sv")
    if config.corev_pulp != 0:
        return text, stats

    removed_count = 0
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        matched = False

        for label in _PULP_ONLY_IMM_LABELS:
            if stripped.startswith(f"{label}:") or stripped.startswith(f"{label},"):
                indent = line[: len(line) - len(line.lstrip())]
                result.append(f"{indent}// PRUNED (PULP-only): {stripped}")
                if "begin" in stripped:
                    depth = 1
                    i += 1
                    while i < len(lines) and depth > 0:
                        s = lines[i].strip()
                        if "begin" in s:
                            depth += 1
                        if s == "end" or s.startswith("end "):
                            depth -= 1
                        i += 1
                else:
                    i += 1
                removed_count += 1
                matched = True
                break

        if not matched:
            result.append(line)
            i += 1

    if removed_count > 0:
        stats.features_added.append(
            f"Pruned {removed_count} PULP-only immediate mux entries ({'/'.join(_PULP_ONLY_IMM_LABELS)})"
        )
    return "\n".join(result), stats


def _prune_csr_storage(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Remove unused CSR storage registers from cv32e40p_cs_registers.sv.

    For each CSR in removable_csr_storage, find the register assignment
    pattern (e.g. mscratch_q <= mscratch_n) and comment it out, replacing
    the flip-flop with a constant zero.
    """
    stats = PruneStats(file="rtl/cv32e40p_cs_registers.sv")
    if not config.removable_csr_storage:
        return text, stats

    pruned_regs = []
    for csr_name in sorted(config.removable_csr_storage):
        reg_names = _CSR_STORAGE_MAP.get(csr_name, [])
        for reg_name in reg_names:
            # Comment out the FF assignment: reg_q <= reg_n;
            pattern = rf"^(\s+)({re.escape(reg_name)}\s*<=\s*\S+;)"
            replacement = rf"\1// PRUNED: \2\n\1{reg_name} <= '0;  // CSR storage removed"
            new_text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
            if count > 0:
                text = new_text
                pruned_regs.append(reg_name)

    if pruned_regs:
        regs_str = ", ".join(pruned_regs)
        ffs = len(pruned_regs) * 32
        stats.features_added.append(f"Pruned CSR storage: {regs_str} (~{ffs} FFs saved)")
    return text, stats


def _gate_regfile_write_port_b(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Gate write port B in cv32e40p_register_file_ff.sv.

    Write port B is only used for PULP post-increment load/store.
    Gate the we_b_dec decoder and the write-back paths.
    """
    stats = PruneStats(file="rtl/cv32e40p_register_file_ff.sv")

    text, added = _add_parameter_to_module(text, "cv32e40p_register_file", "ENABLE_REGFILE_WR_B", "1")
    if added:
        stats.features_added.append("Added parameter ENABLE_REGFILE_WR_B")

    guard = "// ARVIS_GATED: ENABLE_REGFILE_WR_B"
    if guard not in text:
        # Gate we_b_dec: line 105
        old = "(waddr_b == gidx) ? we_b_i : 1'b0;"
        new = f"ENABLE_REGFILE_WR_B ? ((waddr_b == gidx) ? we_b_i : 1'b0) : 1'b0; {guard}"
        if old in text:
            text = text.replace(old, new, 1)
            stats.features_added.append("Gated write port B decoder with ENABLE_REGFILE_WR_B")

    return text, stats


def _gate_regfile_read_port_c(text: str, config: PruneConfig) -> Tuple[str, PruneStats]:
    """Add ENABLE_REGFILE_RD_C parameter to cv32e40p_register_file_ff.sv.

    Read port C is only used for FPU R4-type fused instructions or PULP.
    Gating it removes a 32:1 x 32-bit mux + forwarding chain.
    """
    stats = PruneStats(file="rtl/cv32e40p_register_file_ff.sv")

    text, added = _add_parameter_to_module(text, "cv32e40p_register_file", "ENABLE_REGFILE_RD_C", "1")
    if added:
        stats.features_added.append("Added parameter ENABLE_REGFILE_RD_C")

    # Gate read port C output
    guard = "// ARVIS_GATED: ENABLE_REGFILE_RD_C"
    if guard not in text:
        # Find the rdata_c assignment and wrap it
        pattern = r"(assign\s+rdata_c_o\s*=\s*)([^;]+)(;)"
        match = re.search(pattern, text)
        if match:
            orig_expr = match.group(2)
            new_expr = f"ENABLE_REGFILE_RD_C ? ({orig_expr}) : '0"
            text = text.replace(
                match.group(0),
                f"{match.group(1)}{new_expr}{match.group(3)} {guard}",
                1,
            )
            stats.features_added.append(
                "Gated read port C with ENABLE_REGFILE_RD_C (32:1 x 32-bit mux + forwarding chain eliminated)"
            )

    return text, stats


# ======================================================================
# Main RTLPruner class
# ======================================================================


class RTLPruner:
    """Add parameter-based feature gates to CV32E40P RTL."""

    _MODIFICATIONS = {
        "rtl/cv32e40p_alu.sv": _gate_divider_in_alu,
        "rtl/cv32e40p_mult.sv": _gate_mulh_in_mult,
        "rtl/cv32e40p_ex_stage.sv": _gate_mulh_in_ex_stage,
        "rtl/cv32e40p_core.sv": _gate_sleep_in_core,
        "rtl/cv32e40p_top.sv": _propagate_enable_div_to_top,
    }

    # Note: mult case pruning removed — DOT/MSU handled by parameters
    _CASE_PRUNING = {
        "rtl/cv32e40p_alu.sv": _prune_alu_case_entries,
    }

    def __init__(self, workspace: RTLWorkspace):
        self.ws = workspace

    def apply(self, config: PruneConfig, *, verbose: bool = True) -> PruneReport:
        """Apply parameter-based gating to the workspace RTL."""
        report = PruneReport(config=config)
        _print = print if verbose else (lambda *a, **k: None)

        params = config.synthesis_parameters()
        _print("\n  RTL Parameter Gating:")
        _print(
            f"    ENABLE_DIV={params['ENABLE_DIV']} "
            f"ENABLE_MULH={params['ENABLE_MULH']} "
            f"ENABLE_SLEEP={params['ENABLE_SLEEP']}"
        )
        _print(
            f"    COREV_PULP={params['COREV_PULP']} FPU={params['FPU']} NUM_MHPMCOUNTERS={params['NUM_MHPMCOUNTERS']}"
        )

        # Pass 1: Primary parameter gating
        for rel_path, modify_fn in self._MODIFICATIONS.items():
            full_path = self.ws.output_root / rel_path
            if not full_path.exists():
                continue
            text = full_path.read_text()
            modified_text, stats = modify_fn(text, config)
            if stats.features_added:
                full_path.write_text(modified_text)
                _print(f"    + {rel_path}:")
                for feat in stats.features_added:
                    _print(f"        {feat}")
            report.file_stats.append(stats)

        # Pass 2: Decoder opcode block pruning
        if config.removable_opcode_groups:
            n_grp = len(config.removable_opcode_groups)
            _print(f"\n  Decoder pruning ({n_grp} unused opcode groups):")
            dec_path = self.ws.output_root / "rtl/cv32e40p_decoder.sv"
            if dec_path.exists():
                text = dec_path.read_text()
                modified_text, stats = _prune_decoder_opcode_blocks(text, config)
                if stats.features_added:
                    dec_path.write_text(modified_text)
                    _print("    + rtl/cv32e40p_decoder.sv:")
                    for feat in stats.features_added:
                        _print(f"        {feat}")
                report.file_stats.append(stats)

        # Pass 3: Compressed decoder + aligner gating
        # Always add the parameter so it can be toggled
        if_path = self.ws.output_root / "rtl/cv32e40p_if_stage.sv"
        if if_path.exists():
            text = if_path.read_text()
            # Force config to add parameter even if compressed
            orig_compressed = config.enable_compressed
            config.enable_compressed = False
            modified_text, stats = _gate_compressed_decoder(text, config)
            config.enable_compressed = orig_compressed
            if stats.features_added:
                if_path.write_text(modified_text)
                if not orig_compressed:
                    _print("\n  Compressed instruction support: REMOVING")
                _print("    + rtl/cv32e40p_if_stage.sv:")
                for feat in stats.features_added:
                    _print(f"        {feat}")
            report.file_stats.append(stats)

        # Pass 4: Structural gating of multiplier (ENABLE_MUL)
        # Always add the generate-if so the parameter works
        ex_path = self.ws.output_root / "rtl/cv32e40p_ex_stage.sv"
        if ex_path.exists():
            text = ex_path.read_text()
            mul_else = """\
    // Multiplier disabled: tie off outputs
    assign mult_result = '0;
    assign mult_multicycle_o = 1'b0;"""
            text, _ = _add_parameter_to_module(
                text,
                "cv32e40p_ex_stage",
                "ENABLE_MUL",
                "1",
            )
            text, wrapped = _wrap_in_generate_if(
                text,
                start_marker="cv32e40p_mult mult_i",
                end_marker=");",
                condition="ENABLE_MUL",
                else_body=mul_else,
            )
            if wrapped:
                ex_path.write_text(text)
                stats = PruneStats(file="rtl/cv32e40p_ex_stage.sv")
                stats.features_added.append("Wrapped multiplier (ENABLE_MUL)")
                report.file_stats.append(stats)
                _print("    + rtl/cv32e40p_ex_stage.sv:")
                _print("        Wrapped multiplier (ENABLE_MUL)")

        # Pass 5-7: Gate int_controller, hwloop_regs, apu_disp in core
        core_path = self.ws.output_root / "rtl/cv32e40p_core.sv"
        if core_path.exists():
            text = core_path.read_text()
            modified = False

            int_else = """\
    assign irq_req_ctrl = 1'b0;
    assign irq_id_ctrl  = '0;
    assign irq_sec_ctrl = 1'b0;"""
            text, w = _wrap_in_generate_if(
                text,
                "cv32e40p_int_controller",
                ");",
                "COREV_PULP",
                int_else,
            )
            if w:
                modified = True
                s = PruneStats(file="rtl/cv32e40p_core.sv")
                s.features_added.append("Wrapped int_controller in generate if (COREV_PULP)")
                report.file_stats.append(s)

            # hwloop_regs wrapping now handled by ARVIS_HWLP pragmas

            apu_else = """\
    assign apu_req_o      = 1'b0;
    assign apu_operands_o = '0;
    assign apu_op_o       = '0;
    assign apu_flags_o    = '0;
    assign apu_busy_o     = 1'b0;"""
            text, w = _wrap_in_generate_if(
                text,
                "cv32e40p_apu_disp apu_disp_i",
                ");",
                "FPU",
                apu_else,
            )
            if w:
                modified = True
                s = PruneStats(file="rtl/cv32e40p_core.sv")
                s.features_added.append("Wrapped apu_disp in generate if (FPU)")
                report.file_stats.append(s)

            if modified:
                core_path.write_text(text)

        # Pass 8: Gate ff_one and popcnt in ALU (PULP bit-ops only)
        alu_path = self.ws.output_root / "rtl/cv32e40p_alu.sv"
        if alu_path.exists():
            text = alu_path.read_text()
            # Add all needed parameters to ALU module
            for p in ["COREV_PULP", "ENABLE_POPCNT", "ENABLE_FF1"]:
                val = "0" if p == "COREV_PULP" else "1"
                text, _ = _add_parameter_to_module(
                    text,
                    "cv32e40p_alu",
                    p,
                    val,
                )
            alu_modified = False

            popcnt_else = """\
    assign cnt_result = '0;"""
            text, w = _wrap_in_generate_if(
                text,
                "cv32e40p_popcnt popcnt_i",
                ");",
                "ENABLE_POPCNT",
                popcnt_else,
            )
            if w:
                alu_modified = True
                s = PruneStats(file="rtl/cv32e40p_alu.sv")
                s.features_added.append("Wrapped popcnt (ENABLE_POPCNT)")
                report.file_stats.append(s)
                _print("    + rtl/cv32e40p_alu.sv:")
                _print("        Wrapped popcnt (ENABLE_POPCNT)")

            ff1_else = """\
    assign ff_input  = '0;
    assign ff_no_one = 1'b1;
    assign ff_result = '0;"""
            text, w = _wrap_in_generate_if(
                text,
                "cv32e40p_ff_one ff_one_i",
                ");",
                "ENABLE_FF1",
                ff1_else,
            )
            if w:
                alu_modified = True
                s = PruneStats(file="rtl/cv32e40p_alu.sv")
                s.features_added.append("Wrapped ff_one (ENABLE_FF1)")
                report.file_stats.append(s)
                _print("        Wrapped ff_one (ENABLE_FF1)")

            if alu_modified:
                alu_path.write_text(text)

        # Pass 9: Gate aligner (only needed for compressed instructions)
        if not config.enable_compressed:
            if_path = self.ws.output_root / "rtl/cv32e40p_if_stage.sv"
            if if_path.exists():
                text = if_path.read_text()
                aligner_else = """\
    assign instr_aligned = instr_i;
    assign instr_valid   = instr_valid_i;
    assign align_ready   = 1'b1;"""
                text, w = _wrap_in_generate_if(
                    text,
                    "cv32e40p_aligner aligner_i",
                    ");",
                    "ENABLE_COMPRESSED",
                    aligner_else,
                )
                if w:
                    if_path.write_text(text)
                    s = PruneStats(file="rtl/cv32e40p_if_stage.sv")
                    s.features_added.append("Wrapped aligner in generate if (ENABLE_COMPRESSED)")
                    report.file_stats.append(s)

        # Pass 10: Register file gating (USED_REGS_MASK, write port B, read port C)
        regfile_path = self.ws.output_root / "rtl/cv32e40p_register_file_ff.sv"
        if regfile_path.exists():
            text = regfile_path.read_text()
            regfile_modified = False

            # Add USED_REGS_MASK parameter and gate write enables
            text, added = _add_parameter_to_module(text, "cv32e40p_register_file", "USED_REGS_MASK", "32'hFFFFFFFF")
            if added:
                mask_hex = f"32'h{config.used_regs_mask:08X}"
                n_disabled = len(config.unused_registers)
                s = PruneStats(file="rtl/cv32e40p_register_file_ff.sv")
                s.features_added.append(
                    f"USED_REGS_MASK={mask_hex} ({n_disabled} regs disabled, saves {n_disabled * 32} FFs)"
                )
                report.file_stats.append(s)
                regfile_modified = True

            # Gate we_a_dec with USED_REGS_MASK
            mask_guard = "// ARVIS_GATED: USED_REGS_MASK"
            if mask_guard not in text:
                old_a = "(waddr_a == gidx) ? we_a_i : 1'b0;"
                new_a = f"(USED_REGS_MASK[gidx] && (waddr_a == gidx)) ? we_a_i : 1'b0; {mask_guard}"
                if old_a in text:
                    text = text.replace(old_a, new_a, 1)
                    regfile_modified = True

                # Also gate the FF write-back with mask
                ff_guard = "// ARVIS_GATED: USED_REGS_MASK_FF"
                old_ff = (
                    "if (we_b_dec[i] == 1'b1) mem[i] <= wdata_b_i;\n"
                    "          else if (we_a_dec[i] == 1'b1) "
                    "mem[i] <= wdata_a_i;"
                )
                new_ff = (
                    f"if (USED_REGS_MASK[i]) begin {ff_guard}\n"
                    "            if (we_b_dec[i] == 1'b1) "
                    "mem[i] <= wdata_b_i;\n"
                    "            else if (we_a_dec[i] == 1'b1) "
                    "mem[i] <= wdata_a_i;\n"
                    "          end"
                )
                if old_ff in text:
                    text = text.replace(old_ff, new_ff, 1)

                s2 = PruneStats(file="rtl/cv32e40p_register_file_ff.sv")
                s2.features_added.append("Gated we_a_dec + FF write-back with USED_REGS_MASK")
                report.file_stats.append(s2)
                regfile_modified = True

            # Gate write port B
            text, wb_stats = _gate_regfile_write_port_b(text, config)
            if wb_stats.features_added:
                report.file_stats.append(wb_stats)
                regfile_modified = True
                _print("    + rtl/cv32e40p_register_file_ff.sv:")
                for f in wb_stats.features_added:
                    _print(f"        {f}")

            # Gate read port C
            text, rc_stats = _gate_regfile_read_port_c(text, config)
            if rc_stats.features_added:
                report.file_stats.append(rc_stats)
                regfile_modified = True
                for f in rc_stats.features_added:
                    _print(f"        {f}")

            if regfile_modified:
                regfile_path.write_text(text)

        # Pass 11: PULP immediate mux pruning in id_stage
        if config.corev_pulp == 0:
            id_path = self.ws.output_root / "rtl/cv32e40p_id_stage.sv"
            if id_path.exists():
                text = id_path.read_text()
                modified_text, stats = _prune_pulp_immediate_mux(text, config)
                if stats.features_added:
                    id_path.write_text(modified_text)
                    _print("    + rtl/cv32e40p_id_stage.sv:")
                    for feat in stats.features_added:
                        _print(f"        {feat}")
                report.file_stats.append(stats)

        # Pass 12: Gate dot product, MSU, and MULH in mult with
        # ternary expressions (generate-if is too fragile for
        # cv32e40p_mult.sv due to complex variable scoping).
        mult_path = self.ws.output_root / "rtl/cv32e40p_mult.sv"
        if mult_path.exists():
            text = mult_path.read_text()
            mult_modified = False

            for p in ["ENABLE_DOT_MUL", "ENABLE_MSU"]:
                text, added = _add_parameter_to_module(
                    text,
                    "cv32e40p_mult",
                    p,
                    "1",
                )
                if added:
                    mult_modified = True

            # Gate MSU: int_is_msu drives the subtract path
            msu_guard = "// ARVIS_GATED: ENABLE_MSU"
            if msu_guard not in text:
                old_msu = "(operator_i == MUL_MSU32);"
                new_msu = f"(ENABLE_MSU && (operator_i == MUL_MSU32)); {msu_guard}"
                if old_msu in text:
                    text = text.replace(old_msu, new_msu, 1)
                    mult_modified = True
                    s = PruneStats(file="rtl/cv32e40p_mult.sv")
                    s.features_added.append("Gated MSU path with ENABLE_MSU")
                    report.file_stats.append(s)

            # Gate dot product results with ENABLE_DOT_MUL ternary
            dot_guard = "// ARVIS_GATED: ENABLE_DOT_MUL"
            if dot_guard not in text:
                # Gate dot_char_result assignment
                dot_replacements = [
                    (
                        "assign dot_char_result = $signed(",
                        "assign dot_char_result = ENABLE_DOT_MUL ? $signed(",
                    ),
                    (
                        "assign dot_short_result = $signed(",
                        "assign dot_short_result = ENABLE_DOT_MUL ? $signed(",
                    ),
                ]
                dot_count = 0
                for old_prefix, new_prefix in dot_replacements:
                    if old_prefix in text:
                        text = text.replace(old_prefix, new_prefix, 1)
                        dot_count += 1

                # Close the ternary: find the ); after each
                # result computation and add : '0
                if dot_count > 0:
                    # For dot_char_result: ends with dot_op_c_i\n  );
                    text = text.replace(
                        "dot_op_c_i\n  );",
                        f"dot_op_c_i\n  ) : '0; {dot_guard}",
                        1,
                    )
                    # For dot_short_result: ends with accumulator\n  );
                    text = text.replace(
                        "accumulator\n  );",
                        f"accumulator\n  ) : '0; {dot_guard}",
                        1,
                    )
                    mult_modified = True
                    s = PruneStats(file="rtl/cv32e40p_mult.sv")
                    s.features_added.append("Gated dot product results (ENABLE_DOT_MUL)")
                    report.file_stats.append(s)

            if mult_modified:
                mult_path.write_text(text)

        # Pass 12b: Patch DEBUG_TRIGGER_EN localparam value
        # directly in cv32e40p_core.sv source code
        core_path2 = self.ws.output_root / "rtl/cv32e40p_core.sv"
        if core_path2.exists():
            text = core_path2.read_text()
            dbg_guard = "// ARVIS_PATCHED: DEBUG_TRIGGER_EN"
            if dbg_guard not in text:
                old = "localparam DEBUG_TRIGGER_EN = 1;"
                if old in text and not config.enable_debug:
                    text = text.replace(
                        old,
                        f"localparam DEBUG_TRIGGER_EN = 0; {dbg_guard}",
                        1,
                    )
                    core_path2.write_text(text)
                    s = PruneStats(file="rtl/cv32e40p_core.sv")
                    s.features_added.append("Patched DEBUG_TRIGGER_EN = 0")
                    report.file_stats.append(s)

        total = report.total_features
        if total > 0:
            _print(f"\n  {total} parameter gates added")
        else:
            _print("\n  No modifications needed (parameters already present)")

        # Pass 13: pyslang AST-based case-item pruning
        try:
            from .pyslang_pruning import apply_pyslang_pruning

            _print("\n  pyslang AST-based case pruning:")
            pyslang_stats = apply_pyslang_pruning(
                output_root=self.ws.output_root,
                removable_alu_ops=config.removable_alu_ops,
                enable_hpm=config.enable_hpm,
                enable_debug=config.enable_debug,
                corev_pulp=config.corev_pulp,
                removable_csr_labels=config.removable_csr_labels,
            )
            for pstats in pyslang_stats:
                if pstats.items_removed > 0:
                    _print(
                        f"    + {pstats.file}: removed "
                        f"{pstats.items_removed}/{pstats.items_total} "
                        f"case items ({len(pstats.labels_removed)} labels)"
                    )
                    s = PruneStats(file=pstats.file)
                    s.features_added.append(
                        f"pyslang: removed {pstats.items_removed} case items "
                        f"({len(pstats.labels_removed)} labels) - "
                        f"{pstats.description}"
                    )
                    report.file_stats.append(s)
                else:
                    _print(f"    = {pstats.file}: no removable case items")
        except ImportError:
            _print("\n  pyslang not available - skipping AST-based pruning")
        except Exception as e:
            _print(f"\n  pyslang pruning error (non-fatal): {e}")

        # Pass 14: CSR storage pruning
        if config.removable_csr_storage:
            _print("\n  CSR storage pruning:")
            cs_path = self.ws.output_root / "rtl/cv32e40p_cs_registers.sv"
            if cs_path.exists():
                text = cs_path.read_text()
                modified_text, stats = _prune_csr_storage(text, config)
                if stats.features_added:
                    cs_path.write_text(modified_text)
                    _print("    + rtl/cv32e40p_cs_registers.sv:")
                    for feat in stats.features_added:
                        _print(f"        {feat}")
                report.file_stats.append(stats)

        return report

    def generate_specialized_decoder(
        self,
        used_instructions: Set[str],
        custom_instructions: Optional[List] = None,
    ) -> PruneStats:
        """Replace the decoder with a generated one containing only used instructions."""
        from arvis.analysis.isa_db import ISA_DB, get_used_instructions
        from arvis.codegen.rtl.decoder_gen import generate_decoder

        stats = PruneStats(file="rtl/cv32e40p_decoder.sv")

        _COMPRESSED_MAP = {
            "c.add": "add",
            "c.mv": "add",
            "c.li": "addi",
            "c.lui": "lui",
            "c.addi": "addi",
            "c.addi4spn": "addi",
            "c.addi16sp": "addi",
            "c.slli": "slli",
            "c.srli": "srli",
            "c.srai": "srai",
            "c.andi": "andi",
            "c.sub": "sub",
            "c.xor": "xor",
            "c.or": "or",
            "c.and": "and",
            "c.lw": "lw",
            "c.lwsp": "lw",
            "c.sw": "sw",
            "c.swsp": "sw",
            "c.beqz": "beq",
            "c.bnez": "bne",
            "c.j": "jal",
            "c.jal": "jal",
            "c.jr": "jalr",
            "c.jalr": "jalr",
            "c.ebreak": "ebreak",
            "c.nop": "addi",
        }

        _PSEUDO_MAP = {
            "ret": "jalr",
            "nop": "addi",
            "mv": "add",
            "li": "addi",
            "not": "xori",
            "neg": "sub",
            "j": "jal",
            "jr": "jalr",
            "seqz": "sltiu",
            "snez": "sltu",
            "blez": "bge",
            "bgez": "bge",
            "bltz": "blt",
            "bgtz": "blt",
            "bnez": "bne",
            "beqz": "beq",
            "csrw": "csrrw",
            "csrr": "csrrs",
            "zext.b": "andi",
        }

        base_mnemonics: Set[str] = set()
        for insn in used_instructions:
            if insn.startswith("c."):
                mapped = _COMPRESSED_MAP.get(insn)
                if mapped:
                    base_mnemonics.add(mapped)
                else:
                    base_mnemonics.add(insn.replace("c.", ""))
            elif insn in _PSEUDO_MAP:
                base_mnemonics.add(_PSEUDO_MAP[insn])
            elif insn in ("fence", "fence.i"):
                base_mnemonics.add("fence")
                base_mnemonics.add("fence_i")
            else:
                base_mnemonics.add(insn)

        # Always include essential system instructions
        for essential in ["ecall", "mret", "ebreak", "wfi", "fence", "fence_i"]:
            base_mnemonics.add(essential)

        subset = get_used_instructions(base_mnemonics)

        original_dec = Path(self.ws.source_root) / "rtl/cv32e40p_decoder.sv"
        sv_code = generate_decoder(
            subset,
            custom_instructions=custom_instructions,
            original_decoder_path=str(original_dec),
        )

        dec_path = self.ws.output_root / "rtl/cv32e40p_decoder.sv"
        dec_path.write_text(sv_code)

        n_total = len(ISA_DB)
        n_kept = len(subset) + (len(custom_instructions) if custom_instructions else 0)
        n_removed = n_total - len(subset)

        stats.features_added.append(
            f"Generated specialized decoder: {n_kept} instructions (removed {n_removed} unused from {n_total} total)"
        )
        stats.features_added.append(f"Kept: {', '.join(sorted(subset.keys()))}")
        return stats
