"""
ISA Database for cv32e40p decoder.

Contains per-instruction control signal assignments extracted from
cv32e40p_decoder.sv.  Each instruction records only the signals that
*differ* from the decoder's default values, keeping the DB compact
and the generated decoder clean.

Organisation
============
ISA_DB : dict[str, InstructionEntry]
    Keyed by canonical instruction name (e.g. ``'add'``, ``'lw'``).
    Each value holds encoding fields and the signal delta.

DEFAULT_SIGNALS : dict[str, str | int]
    The default assignments that appear at the top of the decoder's
    ``always_comb`` block.  The decoder generator emits these as-is,
    then layers per-instruction overrides on top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# ───────────────────────────────────────────────────────────────────
# Default signal values (top of always_comb in cv32e40p_decoder.sv)
# ───────────────────────────────────────────────────────────────────

DEFAULT_SIGNALS: Dict[str, Any] = {
    "ctrl_transfer_insn": "BRANCH_NONE",
    "ctrl_transfer_target_mux_sel_o": "JT_JAL",
    "alu_en": 1,
    "alu_operator_o": "ALU_SLTU",
    "alu_op_a_mux_sel_o": "OP_A_REGA_OR_FWD",
    "alu_op_b_mux_sel_o": "OP_B_REGB_OR_FWD",
    "alu_op_c_mux_sel_o": "OP_C_REGC_OR_FWD",
    "alu_vec_o": 0,
    "alu_vec_mode_o": "VEC_MODE32",
    "scalar_replication_o": 0,
    "scalar_replication_c_o": 0,
    "regc_mux_o": "REGC_ZERO",
    "imm_a_mux_sel_o": "IMMA_ZERO",
    "imm_b_mux_sel_o": "IMMB_I",
    "mult_int_en": 0,
    "mult_dot_en": 0,
    "mult_operator_o": "MUL_I",
    "mult_imm_mux_o": "MIMM_ZERO",
    "mult_signed_mode_o": "2'b00",
    "mult_sel_subword_o": 0,
    "mult_dot_signed_o": "2'b00",
    "apu_en": 0,
    "regfile_mem_we": 0,
    "regfile_alu_we": 0,
    "regfile_alu_waddr_sel_o": 1,
    "prepost_useincr_o": 1,
    "hwlp_we": "3'b0",
    "csr_access_o": 0,
    "csr_status_o": 0,
    "csr_op": "CSR_OP_READ",
    "data_we_o": 0,
    "data_type_o": "2'b00",
    "data_sign_extension_o": "2'b00",
    "data_reg_offset_o": "2'b00",
    "data_req": 0,
    "data_load_event_o": 0,
    "atop_o": "6'b000000",
    "illegal_insn_o": 0,
    "ebrk_insn_o": 0,
    "ecall_insn_o": 0,
    "wfi_o": 0,
    "fencei_insn_o": 0,
    "mret_insn_o": 0,
    "uret_insn_o": 0,
    "dret_insn_o": 0,
    "mret_dec_o": 0,
    "uret_dec_o": 0,
    "dret_dec_o": 0,
    "rega_used_o": 0,
    "regb_used_o": 0,
    "regc_used_o": 0,
    "reg_fp_a_o": 0,
    "reg_fp_b_o": 0,
    "reg_fp_c_o": 0,
    "reg_fp_d_o": 0,
    "bmask_a_mux_o": "BMASK_A_ZERO",
    "bmask_b_mux_o": "BMASK_B_ZERO",
    "alu_bmask_a_mux_sel_o": "BMASK_A_IMM",
    "alu_bmask_b_mux_sel_o": "BMASK_B_IMM",
    "is_clpx_o": 0,
    "is_subrot_o": 0,
}

# Signals that go through deassert_we muxing (internal → output)
DEASSERT_SIGNALS = {
    "alu_en": "alu_en_o",
    "mult_int_en": "mult_int_en_o",
    "mult_dot_en": "mult_dot_en_o",
    "apu_en": "apu_en_o",
    "regfile_mem_we": "regfile_mem_we_o",
    "regfile_alu_we": "regfile_alu_we_o",
    "data_req": "data_req_o",
    "hwlp_we": "hwlp_we_o",
    "csr_op": "csr_op_o",
    "ctrl_transfer_insn": "ctrl_transfer_insn_in_id_o",
}


# ───────────────────────────────────────────────────────────────────
# Instruction entry
# ───────────────────────────────────────────────────────────────────


@dataclass
class InstructionEntry:
    """One row in the ISA database."""

    name: str
    opcode: int  # bits [6:0]
    funct3: Optional[int] = None  # bits [14:12]
    funct7: Optional[int] = None  # bits [31:25]
    funct7_6: Optional[int] = None  # bits [30:25] for OPCODE_OP
    funct6: Optional[int] = None  # bits [31:26] for some PULP insns
    is_r4: bool = False  # R4-type: funct2 in bits [26:25] instead of funct7 [31:25]
    insn_type: str = "R"
    extension: str = "RV32I"
    description: str = ""
    signals: Dict[str, Any] = field(default_factory=dict)


# ───────────────────────────────────────────────────────────────────
# Opcode constants (matching cv32e40p_pkg.sv)
# ───────────────────────────────────────────────────────────────────

OPCODE_LUI = 0x37
OPCODE_AUIPC = 0x17
OPCODE_JAL = 0x6F
OPCODE_JALR = 0x67
OPCODE_BRANCH = 0x63
OPCODE_LOAD = 0x03
OPCODE_STORE = 0x23
OPCODE_OPIMM = 0x13
OPCODE_OP = 0x33
OPCODE_FENCE = 0x0F
OPCODE_SYSTEM = 0x73
OPCODE_AMO = 0x2F
OPCODE_CUSTOM_0 = 0x0B
OPCODE_CUSTOM_1 = 0x2B
OPCODE_CUSTOM_2 = 0x5B
OPCODE_CUSTOM_3 = 0x7B
OPCODE_LOAD_FP = 0x07
OPCODE_STORE_FP = 0x27
OPCODE_OP_FP = 0x53
OPCODE_OP_FMADD = 0x43
OPCODE_OP_FMSUB = 0x47
OPCODE_OP_FNMSUB = 0x4B
OPCODE_OP_FNMADD = 0x4F


# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────


def _e(name, opcode, *, f3=None, f7=None, f7_6=None, f6=None, itype="R", ext="RV32I", desc="", **sigs):
    """Shorthand to build an InstructionEntry."""
    return InstructionEntry(
        name=name,
        opcode=opcode,
        funct3=f3,
        funct7=f7,
        funct7_6=f7_6,
        funct6=f6,
        insn_type=itype,
        extension=ext,
        description=desc,
        signals=sigs,
    )


# ───────────────────────────────────────────────────────────────────
# RV32I
# ───────────────────────────────────────────────────────────────────


def _build_rv32i() -> Dict[str, InstructionEntry]:
    db: Dict[str, InstructionEntry] = {}

    # U-type
    db["lui"] = _e(
        "lui",
        OPCODE_LUI,
        itype="U",
        desc="Load Upper Immediate",
        alu_op_a_mux_sel_o="OP_A_IMM",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_a_mux_sel_o="IMMA_ZERO",
        imm_b_mux_sel_o="IMMB_U",
        alu_operator_o="ALU_ADD",
        regfile_alu_we=1,
    )

    db["auipc"] = _e(
        "auipc",
        OPCODE_AUIPC,
        itype="U",
        desc="Add Upper Imm to PC",
        alu_op_a_mux_sel_o="OP_A_CURRPC",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_b_mux_sel_o="IMMB_U",
        alu_operator_o="ALU_ADD",
        regfile_alu_we=1,
    )

    # J-type
    db["jal"] = _e(
        "jal",
        OPCODE_JAL,
        itype="J",
        desc="Jump and Link",
        ctrl_transfer_target_mux_sel_o="JT_JAL",
        ctrl_transfer_insn="BRANCH_JAL",
        alu_op_a_mux_sel_o="OP_A_CURRPC",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_b_mux_sel_o="IMMB_PCINCR",
        alu_operator_o="ALU_ADD",
        regfile_alu_we=1,
    )

    db["jalr"] = _e(
        "jalr",
        OPCODE_JALR,
        f3=0b000,
        itype="I",
        desc="Jump and Link Register",
        ctrl_transfer_target_mux_sel_o="JT_JALR",
        ctrl_transfer_insn="BRANCH_JALR",
        alu_op_a_mux_sel_o="OP_A_CURRPC",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_b_mux_sel_o="IMMB_PCINCR",
        alu_operator_o="ALU_ADD",
        regfile_alu_we=1,
        rega_used_o=1,
    )

    # B-type branches
    for nm, f3, aop in [
        ("beq", 0b000, "ALU_EQ"),
        ("bne", 0b001, "ALU_NE"),
        ("blt", 0b100, "ALU_LTS"),
        ("bge", 0b101, "ALU_GES"),
        ("bltu", 0b110, "ALU_LTU"),
        ("bgeu", 0b111, "ALU_GEU"),
    ]:
        db[nm] = _e(
            nm,
            OPCODE_BRANCH,
            f3=f3,
            itype="B",
            desc=f"Branch {nm}",
            ctrl_transfer_target_mux_sel_o="JT_COND",
            ctrl_transfer_insn="BRANCH_COND",
            alu_op_c_mux_sel_o="OP_C_JT",
            rega_used_o=1,
            regb_used_o=1,
            alu_operator_o=aop,
        )

    # Loads
    for nm, f3, dt, se in [
        ("lb", 0b000, "2'b10", "2'b01"),
        ("lh", 0b001, "2'b01", "2'b01"),
        ("lw", 0b010, "2'b00", "2'b00"),
        ("lbu", 0b100, "2'b10", "2'b00"),
        ("lhu", 0b101, "2'b01", "2'b00"),
    ]:
        db[nm] = _e(
            nm,
            OPCODE_LOAD,
            f3=f3,
            itype="I",
            desc=f"Load {nm}",
            data_req=1,
            regfile_mem_we=1,
            rega_used_o=1,
            alu_operator_o="ALU_ADD",
            alu_op_b_mux_sel_o="OP_B_IMM",
            imm_b_mux_sel_o="IMMB_I",
            data_type_o=dt,
            data_sign_extension_o=se,
        )

    # Stores
    for nm, f3, dt in [("sb", 0b000, "2'b10"), ("sh", 0b001, "2'b01"), ("sw", 0b010, "2'b00")]:
        db[nm] = _e(
            nm,
            OPCODE_STORE,
            f3=f3,
            itype="S",
            desc=f"Store {nm}",
            data_req=1,
            data_we_o=1,
            rega_used_o=1,
            regb_used_o=1,
            alu_operator_o="ALU_ADD",
            alu_op_c_mux_sel_o="OP_C_REGB_OR_FWD",
            imm_b_mux_sel_o="IMMB_S",
            alu_op_b_mux_sel_o="OP_B_IMM",
            data_type_o=dt,
        )

    # OP-IMM (simple)
    for nm, f3, aop in [
        ("addi", 0b000, "ALU_ADD"),
        ("slti", 0b010, "ALU_SLTS"),
        ("sltiu", 0b011, "ALU_SLTU"),
        ("xori", 0b100, "ALU_XOR"),
        ("ori", 0b110, "ALU_OR"),
        ("andi", 0b111, "ALU_AND"),
    ]:
        db[nm] = _e(
            nm,
            OPCODE_OPIMM,
            f3=f3,
            itype="I",
            desc=f"Imm {nm}",
            alu_op_b_mux_sel_o="OP_B_IMM",
            imm_b_mux_sel_o="IMMB_I",
            regfile_alu_we=1,
            rega_used_o=1,
            alu_operator_o=aop,
        )

    # OP-IMM shifts (need funct7)
    db["slli"] = _e(
        "slli",
        OPCODE_OPIMM,
        f3=0b001,
        f7=0b0000000,
        itype="I",
        desc="Shift Left Logical Imm",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_b_mux_sel_o="IMMB_I",
        regfile_alu_we=1,
        rega_used_o=1,
        alu_operator_o="ALU_SLL",
    )
    db["srli"] = _e(
        "srli",
        OPCODE_OPIMM,
        f3=0b101,
        f7=0b0000000,
        itype="I",
        desc="Shift Right Logical Imm",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_b_mux_sel_o="IMMB_I",
        regfile_alu_we=1,
        rega_used_o=1,
        alu_operator_o="ALU_SRL",
    )
    db["srai"] = _e(
        "srai",
        OPCODE_OPIMM,
        f3=0b101,
        f7=0b0100000,
        itype="I",
        desc="Shift Right Arith Imm",
        alu_op_b_mux_sel_o="OP_B_IMM",
        imm_b_mux_sel_o="IMMB_I",
        regfile_alu_we=1,
        rega_used_o=1,
        alu_operator_o="ALU_SRA",
    )

    # OP register-register (RV32I ALU)
    for nm, f76, f3, aop in [
        ("add", 0b000000, 0b000, "ALU_ADD"),
        ("sub", 0b100000, 0b000, "ALU_SUB"),
        ("sll", 0b000000, 0b001, "ALU_SLL"),
        ("slt", 0b000000, 0b010, "ALU_SLTS"),
        ("sltu", 0b000000, 0b011, "ALU_SLTU"),
        ("xor", 0b000000, 0b100, "ALU_XOR"),
        ("srl", 0b000000, 0b101, "ALU_SRL"),
        ("sra", 0b100000, 0b101, "ALU_SRA"),
        ("or", 0b000000, 0b110, "ALU_OR"),
        ("and", 0b000000, 0b111, "ALU_AND"),
    ]:
        db[nm] = _e(
            nm,
            OPCODE_OP,
            f3=f3,
            f7_6=f76,
            itype="R",
            desc=f"Reg {nm}",
            regfile_alu_we=1,
            rega_used_o=1,
            regb_used_o=1,
            alu_operator_o=aop,
        )

    # FENCE
    db["fence"] = _e("fence", OPCODE_FENCE, f3=0b000, itype="I", desc="FENCE", fencei_insn_o=1)
    db["fence_i"] = _e("fence_i", OPCODE_FENCE, f3=0b001, itype="I", desc="FENCE.I", fencei_insn_o=1)

    # SYSTEM non-CSR
    db["ecall"] = _e("ecall", OPCODE_SYSTEM, itype="SYSTEM", desc="Environment Call", ecall_insn_o=1)
    db["ebreak"] = _e("ebreak", OPCODE_SYSTEM, itype="SYSTEM", desc="Breakpoint", ebrk_insn_o=1)
    db["mret"] = _e("mret", OPCODE_SYSTEM, itype="SYSTEM", desc="Machine Return", mret_insn_o=1, mret_dec_o=1)
    db["wfi"] = _e("wfi", OPCODE_SYSTEM, itype="SYSTEM", desc="Wait for Interrupt", wfi_o=1)

    # CSR instructions
    for nm, f3, op_a, csr_op_val in [
        ("csrrw", 0b001, "OP_A_REGA_OR_FWD", "CSR_OP_WRITE"),
        ("csrrs", 0b010, "OP_A_REGA_OR_FWD", "CSR_OP_SET"),
        ("csrrc", 0b011, "OP_A_REGA_OR_FWD", "CSR_OP_CLEAR"),
        ("csrrwi", 0b101, "OP_A_IMM", "CSR_OP_WRITE"),
        ("csrrsi", 0b110, "OP_A_IMM", "CSR_OP_SET"),
        ("csrrci", 0b111, "OP_A_IMM", "CSR_OP_CLEAR"),
    ]:
        sigs: Dict[str, Any] = {
            "csr_access_o": 1,
            "regfile_alu_we": 1,
            "alu_op_b_mux_sel_o": "OP_B_IMM",
            "imm_a_mux_sel_o": "IMMA_Z",
            "imm_b_mux_sel_o": "IMMB_I",
            "alu_op_a_mux_sel_o": op_a,
            "csr_op": csr_op_val,
        }
        if op_a == "OP_A_REGA_OR_FWD":
            sigs["rega_used_o"] = 1
        db[nm] = InstructionEntry(
            name=nm,
            opcode=OPCODE_SYSTEM,
            funct3=f3,
            insn_type="I",
            extension="SYSTEM",
            description=f"CSR {nm}",
            signals=sigs,
        )

    return db


# ───────────────────────────────────────────────────────────────────
# RV32M
# ───────────────────────────────────────────────────────────────────


def _build_rv32m() -> Dict[str, InstructionEntry]:
    db: Dict[str, InstructionEntry] = {}

    db["mul"] = _e(
        "mul",
        OPCODE_OP,
        f3=0b000,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Multiply",
        alu_en=0,
        mult_int_en=1,
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        mult_operator_o="MUL_MAC32",
        regc_mux_o="REGC_ZERO",
    )

    db["mulh"] = _e(
        "mulh",
        OPCODE_OP,
        f3=0b001,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Multiply High Signed",
        alu_en=0,
        mult_int_en=1,
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        regc_used_o=1,
        regc_mux_o="REGC_ZERO",
        mult_signed_mode_o="2'b11",
        mult_operator_o="MUL_H",
    )

    db["mulhsu"] = _e(
        "mulhsu",
        OPCODE_OP,
        f3=0b010,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Multiply High Signed-Unsigned",
        alu_en=0,
        mult_int_en=1,
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        regc_used_o=1,
        regc_mux_o="REGC_ZERO",
        mult_signed_mode_o="2'b01",
        mult_operator_o="MUL_H",
    )

    db["mulhu"] = _e(
        "mulhu",
        OPCODE_OP,
        f3=0b011,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Multiply High Unsigned",
        alu_en=0,
        mult_int_en=1,
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        regc_used_o=1,
        regc_mux_o="REGC_ZERO",
        mult_signed_mode_o="2'b00",
        mult_operator_o="MUL_H",
    )

    db["div"] = _e(
        "div",
        OPCODE_OP,
        f3=0b100,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Divide Signed",
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        alu_op_a_mux_sel_o="OP_A_REGB_OR_FWD",
        alu_op_b_mux_sel_o="OP_B_REGA_OR_FWD",
        alu_operator_o="ALU_DIV",
    )

    db["divu"] = _e(
        "divu",
        OPCODE_OP,
        f3=0b101,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Divide Unsigned",
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        alu_op_a_mux_sel_o="OP_A_REGB_OR_FWD",
        alu_op_b_mux_sel_o="OP_B_REGA_OR_FWD",
        alu_operator_o="ALU_DIVU",
    )

    db["rem"] = _e(
        "rem",
        OPCODE_OP,
        f3=0b110,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Remainder Signed",
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        alu_op_a_mux_sel_o="OP_A_REGB_OR_FWD",
        alu_op_b_mux_sel_o="OP_B_REGA_OR_FWD",
        alu_operator_o="ALU_REM",
    )

    db["remu"] = _e(
        "remu",
        OPCODE_OP,
        f3=0b111,
        f7_6=0b000001,
        itype="R",
        ext="RV32M",
        desc="Remainder Unsigned",
        regfile_alu_we=1,
        rega_used_o=1,
        regb_used_o=1,
        alu_op_a_mux_sel_o="OP_A_REGB_OR_FWD",
        alu_op_b_mux_sel_o="OP_B_REGA_OR_FWD",
        alu_operator_o="ALU_REMU",
    )

    return db


# ───────────────────────────────────────────────────────────────────
# Complete ISA DB
# ───────────────────────────────────────────────────────────────────


def build_isa_db(
    include_rv32m: bool = True,
    custom_instructions: Optional[List[InstructionEntry]] = None,
) -> Dict[str, InstructionEntry]:
    """
    Build the full ISA database.

    Parameters
    ----------
    include_rv32m : bool
        Include RV32M multiply/divide instructions.
    custom_instructions : list[InstructionEntry], optional
        Additional custom instructions to merge.

    Returns
    -------
    dict[str, InstructionEntry]
    """
    db = _build_rv32i()
    if include_rv32m:
        db.update(_build_rv32m())
    if custom_instructions:
        for entry in custom_instructions:
            db[entry.name] = entry
    return db


# Eagerly build the default DB for convenient import
ISA_DB: Dict[str, InstructionEntry] = build_isa_db()


# ───────────────────────────────────────────────────────────────────
# Query helpers
# ───────────────────────────────────────────────────────────────────


def get_used_instructions(
    used_mnemonics: Set[str],
    db: Optional[Dict[str, InstructionEntry]] = None,
) -> Dict[str, InstructionEntry]:
    """Filter the DB to only instructions whose mnemonics appear in the set."""
    if db is None:
        db = ISA_DB
    return {k: v for k, v in db.items() if k in used_mnemonics}


# ───────────────────────────────────────────────────────────────────
# Opcode name mapping (for readable RTL)
# ───────────────────────────────────────────────────────────────────

OPCODE_NAMES: Dict[int, str] = {
    OPCODE_LUI: "OPCODE_LUI",
    OPCODE_AUIPC: "OPCODE_AUIPC",
    OPCODE_JAL: "OPCODE_JAL",
    OPCODE_JALR: "OPCODE_JALR",
    OPCODE_BRANCH: "OPCODE_BRANCH",
    OPCODE_LOAD: "OPCODE_LOAD",
    OPCODE_STORE: "OPCODE_STORE",
    OPCODE_OPIMM: "OPCODE_OPIMM",
    OPCODE_OP: "OPCODE_OP",
    OPCODE_FENCE: "OPCODE_FENCE",
    OPCODE_SYSTEM: "OPCODE_SYSTEM",
    OPCODE_AMO: "OPCODE_AMO",
    OPCODE_CUSTOM_0: "OPCODE_CUSTOM_0",
    OPCODE_CUSTOM_1: "OPCODE_CUSTOM_1",
    OPCODE_CUSTOM_2: "OPCODE_CUSTOM_2",
    OPCODE_CUSTOM_3: "OPCODE_CUSTOM_3",
    OPCODE_LOAD_FP: "OPCODE_LOAD_FP",
    OPCODE_STORE_FP: "OPCODE_STORE_FP",
    OPCODE_OP_FP: "OPCODE_OP_FP",
    OPCODE_OP_FMADD: "OPCODE_OP_FMADD",
    OPCODE_OP_FMSUB: "OPCODE_OP_FMSUB",
    OPCODE_OP_FNMSUB: "OPCODE_OP_FNMSUB",
    OPCODE_OP_FNMADD: "OPCODE_OP_FNMADD",
}
