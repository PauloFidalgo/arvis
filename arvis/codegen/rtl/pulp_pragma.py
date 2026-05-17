"""
PULP Pragma Generators.

Generators for ARVIS_PULP_BEGIN/END pragmas.
When COREV_PULP=0: removes PULP ISA extension compute paths.
When COREV_PULP>0: keeps original code.
"""

from __future__ import annotations

from typing import Callable, Dict


def _gen_alu_adder_negate(level: int, indent: str) -> str:
    """Adder negate: remove PULP SUBR/SUBU/SUBUR/subrot terms."""
    if level == 0:
        return f"{indent}assign adder_op_b_negate = (operator_i == ALU_SUB);\n"
    return (
        f"{indent}assign adder_op_b_negate = (operator_i == ALU_SUB) || (operator_i == ALU_SUBR) ||\n"
        f"{indent}                           (operator_i == ALU_SUBU) || (operator_i == ALU_SUBUR) || is_subrot_i;\n"
    )


def _gen_alu_adder_op_a(level: int, indent: str) -> str:
    """Adder operand A: remove ABS and subrot paths."""
    if level == 0:
        return f"{indent}assign adder_op_a = operand_a_i;\n"
    return (
        f"{indent}assign adder_op_a = (operator_i == ALU_ABS) ? operand_a_neg : (is_subrot_i ? {{\n"
        f"{indent}  operand_b_i[15:0], operand_a_i[31:16]\n"
        f"{indent}}} : operand_a_i);\n"
    )


def _gen_alu_adder_op_b(level: int, indent: str) -> str:
    """Adder operand B: remove subrot path."""
    if level == 0:
        return f"{indent}assign adder_op_b = adder_op_b_negate ? operand_b_neg : operand_b_i;\n"
    return (
        f"{indent}assign adder_op_b = adder_op_b_negate ? (is_subrot_i ? ~{{\n"
        f"{indent}  operand_a_i[15:0], operand_b_i[31:16]\n"
        f"{indent}}} : operand_b_neg) : operand_b_i;\n"
    )


def _gen_alu_round_value(level: int, indent: str) -> str:
    """Adder round value: always 0 without PULP normalization."""
    if level == 0:
        return f"{indent}assign adder_round_value = '0;\n"
    return (
        f"{indent}assign adder_round_value  = ((operator_i == ALU_ADDR) || (operator_i == ALU_SUBR) ||\n"
        f"{indent}                             (operator_i == ALU_ADDUR) || (operator_i == ALU_SUBUR)) ?\n"
        f"{indent}                              {{1'b0, bmask[31:1]}} : '0;\n"
    )


def _gen_alu_shift_left(level: int, indent: str) -> str:
    """Shift left condition: remove PULP BINS/BREV terms."""
    if level == 0:
        return (
            f"{indent}assign shift_left = (operator_i == ALU_SLL) ||\n"
            f"{indent}                    (operator_i == ALU_FL1) || (operator_i == ALU_CLB) ||\n"
            f"{indent}                    (operator_i == ALU_DIV) || (operator_i == ALU_DIVU) ||\n"
            f"{indent}                    (operator_i == ALU_REM) || (operator_i == ALU_REMU);\n"
        )
    return (
        f"{indent}assign shift_left = (operator_i == ALU_SLL) || (operator_i == ALU_BINS) ||\n"
        f"{indent}                    (operator_i == ALU_FL1) || (operator_i == ALU_CLB)  ||\n"
        f"{indent}                    (operator_i == ALU_DIV) || (operator_i == ALU_DIVU) ||\n"
        f"{indent}                    (operator_i == ALU_REM) || (operator_i == ALU_REMU) ||\n"
        f"{indent}                    (operator_i == ALU_BREV);\n"
    )


def _gen_alu_shift_use_round(level: int, indent: str) -> str:
    """Shift use round: remove PULP ADDR/SUBR/ADDU/SUBU/ADDUR/SUBUR."""
    if level == 0:
        return f"{indent}assign shift_use_round = (operator_i == ALU_ADD) || (operator_i == ALU_SUB);\n"
    return (
        f"{indent}assign shift_use_round = (operator_i == ALU_ADD)   || (operator_i == ALU_SUB)   ||\n"
        f"{indent}                         (operator_i == ALU_ADDR)  || (operator_i == ALU_SUBR)  ||\n"
        f"{indent}                         (operator_i == ALU_ADDU)  || (operator_i == ALU_SUBU)  ||\n"
        f"{indent}                         (operator_i == ALU_ADDUR) || (operator_i == ALU_SUBUR);\n"
    )


def _gen_alu_shift_arithmetic(level: int, indent: str) -> str:
    """Shift arithmetic: remove PULP BEXT/ADDR/SUBR."""
    if level == 0:
        return (
            f"{indent}assign shift_arithmetic = (operator_i == ALU_SRA) ||\n"
            f"{indent}                          (operator_i == ALU_ADD) || (operator_i == ALU_SUB);\n"
        )
    return (
        f"{indent}assign shift_arithmetic = (operator_i == ALU_SRA)  || (operator_i == ALU_BEXT) ||\n"
        f"{indent}                          (operator_i == ALU_ADD)  || (operator_i == ALU_SUB)  ||\n"
        f"{indent}                          (operator_i == ALU_ADDR) || (operator_i == ALU_SUBR);\n"
    )


def _gen_alu_shift_amt_norm(level: int, indent: str) -> str:
    """Shift amount normalization: remove clpx path."""
    if level == 0:
        return f"{indent}assign shift_amt_norm = '0;\n"
    return (
        f"{indent}assign shift_amt_norm = is_clpx_i ? {{clpx_shift_ex, clpx_shift_ex}} : {{4{{3'b000, bmask_b_i}}}};\n"
    )


def _gen_alu_shift_ror(level: int, indent: str) -> str:
    """Shift op_a_32: remove ROR path."""
    if level == 0:
        return (
            f"{indent}assign shift_op_a_32 = $signed(\n"
            f"{indent}    {{{{32{{shift_arithmetic & shift_op_a[31]}}}}, shift_op_a}}\n"
            f"{indent});\n"
        )
    return (
        f"{indent}assign shift_op_a_32 = (operator_i == ALU_ROR) ? {{\n"
        f"{indent}      shift_op_a, shift_op_a\n"
        f"{indent}    }} : $signed(\n"
        f"{indent}        {{{{32{{shift_arithmetic & shift_op_a[31]}}}}, shift_op_a}}\n"
        f"{indent}    );\n"
    )


def _gen_alu_cmp_signed_pulp(level: int, indent: str) -> str:
    """Comparator cmp_signed: remove PULP entries, keep ALU_SLTS."""
    if level == 0:
        # Only keep ALU_SLTS (standard RV32I), remove PULP entries
        return f"{indent}ALU_SLTS: begin\n"
    return (
        f"{indent}ALU_SLTS,\n"
        f"{indent}ALU_SLETS,\n"
        f"{indent}ALU_MIN,\n"
        f"{indent}ALU_MAX,\n"
        f"{indent}ALU_ABS,\n"
        f"{indent}ALU_CLIP,\n"
        f"{indent}ALU_CLIPU: begin\n"
    )


def _gen_alu_adder_carry_condition(level: int, indent: str) -> str:
    """Adder carry condition: remove ABS/CLIP when no PULP."""
    if level == 0:
        return f"{indent}if (adder_op_b_negate) begin\n"
    return f"{indent}if (adder_op_b_negate || (operator_i == ALU_ABS || operator_i == ALU_CLIP)) begin\n"


def _gen_pkg_vec_mode(level: int, indent: str) -> str:
    """VEC_MODE parameters: remove VEC_MODE16/VEC_MODE8 when PULP=0."""
    if level == 0:
        return f"{indent}parameter VEC_MODE32 = 2'b00;\n"
    return (
        f"{indent}parameter VEC_MODE32 = 2'b00;\n"
        f"{indent}parameter VEC_MODE16 = 2'b10;\n"
        f"{indent}parameter VEC_MODE8 = 2'b11;\n"
    )


def _gen_pkg_vec_mode_b(level: int, indent: str) -> str:
    """Operand B vec replication: simplify when PULP=0 (no vector modes)."""
    if level == 0:
        return (
            f"{indent}always_comb begin\n"
            f"{indent}  operand_b_vec    = operand_b;\n"
            f"{indent}  imm_shuffle_type = imm_shuffleh_type;\n"
            f"{indent}end\n"
        )
    return None  # Keep original


def _gen_pkg_vec_mode_c(level: int, indent: str) -> str:
    """Operand C vec replication: simplify when PULP=0 (no vector modes)."""
    if level == 0:
        return f"{indent}always_comb begin\n{indent}  operand_c_vec = operand_c;\n{indent}end\n"
    return None  # Keep original


def _gen_pkg_immb(level: int, indent: str) -> str:
    """IMMB parameters: keep only I/S/U/PCINCR when PULP=0.

    Preserves original 4-bit width to avoid signal width mismatches
    across the pipeline (decoder, id_stage, ex_stage all use [3:0]).
    """
    base_values = ["IMMB_I", "IMMB_S", "IMMB_U", "IMMB_PCINCR"]
    pulp_values = ["IMMB_S2", "IMMB_S3", "IMMB_VS", "IMMB_VU", "IMMB_SHUF", "IMMB_CLIP", "IMMB_BI"]

    values = base_values + (pulp_values if level > 0 else [])
    # Always use 4-bit width to match pipeline signal declarations
    width = 4

    lines = []
    for i, name in enumerate(values):
        lines.append(f"{indent}parameter {name} = {width}'b{i:0{width}b};")
    return "\n".join(lines) + "\n"


# ── All PULP generators ──────────────────────────────────────────────

PULP_GENERATORS: Dict[str, Callable] = {
    "alu_adder_negate": _gen_alu_adder_negate,
    "alu_adder_op_a": _gen_alu_adder_op_a,
    "alu_adder_op_b": _gen_alu_adder_op_b,
    "alu_round_value": _gen_alu_round_value,
    "alu_shift_left": _gen_alu_shift_left,
    "alu_shift_use_round": _gen_alu_shift_use_round,
    "alu_shift_arithmetic": _gen_alu_shift_arithmetic,
    "alu_shift_amt_norm": _gen_alu_shift_amt_norm,
    "alu_shift_ror": _gen_alu_shift_ror,
    "alu_cmp_signed_pulp": _gen_alu_cmp_signed_pulp,
    "alu_adder_carry_condition": _gen_alu_adder_carry_condition,
    # MUL generators
    "mul_short_round": lambda level, indent: (
        f"{indent}assign short_round_tmp = '0;\n{indent}assign short_round = '0;\n" if level == 0 else None
    ),
    "mul_short_muxes": lambda level, indent: (
        f"{indent}assign short_imm = mulh_imm;\n{indent}assign short_subword = mulh_subword;\n" if level == 0 else None
    ),
    "mul_result_pulp_short": lambda level, indent: (
        f"{indent}MUL_H: result_o = short_result[31:0];\n" if level == 0 else None
    ),
    "alu_cmp_signed_vec": lambda level, indent: f"{indent}cmp_signed[3:0] = 4'b1000;\n" if level == 0 else None,
    # ALU vec generators
    "alu_shift_amt_left_vec": lambda level, indent: (
        f"{indent}assign shift_amt_left = shift_amt;\n" if level == 0 else None
    ),
    "alu_shift_right_vec": lambda level, indent: (
        f"{indent}assign shift_right_result = shift_op_a_32 >> shift_amt_int[4:0];\n" if level == 0 else None
    ),
    # Package parameter generators
    "pkg_vec_mode": _gen_pkg_vec_mode,
    "pkg_vec_mode_b": _gen_pkg_vec_mode_b,
    "pkg_vec_mode_c": _gen_pkg_vec_mode_c,
    "pkg_immb": _gen_pkg_immb,
}
