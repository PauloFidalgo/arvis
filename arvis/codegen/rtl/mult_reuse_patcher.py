"""Patch cv32e40p_mult.sv to reuse the existing multiplier for pre_compute fusions.

For patterns like or+mul, xor+mul, sub+mul, shift+mul:
  - Pre-compute the ALU op on mult inputs (cheap: few LUTs)
  - Feed pre-computed result as first operand to EXISTING 32x32 multiplier
  - Use op_c_i as the second multiply operand (swapped from op_b_i)
  - Set accumulator to 0 (pure multiply, no MAC)

This avoids synthesizing a SECOND 32x32 multiplier (~1500 LUTs).

Files modified:
  - cv32e40p_mult.sv only (all changes are MULT-internal)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def needs_mult_pre_compute(used_ops: list) -> bool:
    """Check if any used operation needs mult pre-compute steering."""
    for op in used_ops:
        if getattr(op, "execution_unit", "") == "mult":
            if getattr(op, "mult_pattern", "") == "pre_compute":
                return True
    return False


def _get_pre_compute_ops(used_ops: list) -> list:
    """Return MULT ops that need pre_compute input steering."""
    return [
        op
        for op in used_ops
        if getattr(op, "execution_unit", "") == "mult" and getattr(op, "mult_pattern", "") == "pre_compute"
    ]


def generate_mult_steering(pre_compute_ops: list) -> str:
    """Generate the always_comb block for mult pre-compute input steering.

    For each pre_compute op, emits a case arm that:
      - Sets fused_mult_pre_a to the pre-computed value (e.g., op_a_i | op_b_i)
      - Swaps op_b_i → op_c_i for the multiply second operand
      - Sets accumulator to 0 (pure multiply, no MAC)
    """
    if not pre_compute_ops:
        return ""

    lines = []
    lines.append("  // Fused mult pre-compute steering (ARVIS auto-generated)")
    lines.append("  always_comb begin")
    lines.append("    // Defaults: no pre-compute steering")
    lines.append("    fused_mult_pre_sel = 1'b0;")
    lines.append("    fused_mult_pre_a   = op_a_i;")
    lines.append("    fused_mult_b       = op_b_i;")
    lines.append("    fused_mult_acc     = op_c_i;")
    lines.append("")
    lines.append("    unique case (operator_i)")

    for op in pre_compute_ops:
        # Extract the pre-computation expression from the SV expression
        # The SV expr is like: ((op_a_i | op_b_i) * op_c_i)
        # We need the part before *, and the part after *
        pre_expr, mult_second = _decompose_pre_compute(op)
        lines.append(f"      {op.mult_opcode_sv}: begin // {op.description}")
        lines.append("        fused_mult_pre_sel = 1'b1;")
        lines.append(f"        fused_mult_pre_a   = {pre_expr};")
        lines.append(f"        fused_mult_b       = {mult_second};")
        lines.append("        fused_mult_acc     = 32'b0;")
        lines.append("      end")

    lines.append("      default: ;")
    lines.append("    endcase")
    lines.append("  end")
    return "\n".join(lines)


def _decompose_pre_compute(op) -> tuple:
    """Extract pre-compute expression and multiply second operand.

    Given SV expression like: ((operand_a_i | operand_b_i) * operand_c_i)
    Returns: ("(op_a_i | op_b_i)", "op_c_i")
    """
    sv = op.sv_expression
    # Translate to mult port names
    sv = sv.replace("operand_a_i", "op_a_i")
    sv = sv.replace("operand_b_i", "op_b_i")
    sv = sv.replace("operand_c_i", "op_c_i")

    # Find the outermost multiply: (EXPR * OPERAND)
    # Try to match: ((...) * op_X_i) pattern
    m = re.match(r"\((.+)\s*\*\s*(op_[abc]_i)\)", sv)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Try reverse: (op_X_i * (...))
    m = re.match(r"\((op_[abc]_i)\s*\*\s*(.+)\)", sv)
    if m:
        return m.group(2).strip(), m.group(1).strip()

    # Fallback: can't decompose — return raw
    print(f"  ⚠️  Cannot decompose pre_compute SV: {sv}")
    return "op_a_i", "op_b_i"


def add_mult_pre_compute_wiring(rtl_dir: str, steering_sv: str) -> bool:
    """Patch cv32e40p_mult.sv to add pre-compute input steering.

    Adds wires, steering logic, and modifies int_op_a_msu + int_result
    to use steered values when fused_mult_pre_sel is active.
    """
    mult_path = Path(rtl_dir) / "cv32e40p_mult.sv"
    if not mult_path.exists():
        print("  ⚠️  cv32e40p_mult.sv not found — cannot add mult pre-compute")
        return False

    text = mult_path.read_text()

    # If steering already exists, update the pre-compute expressions
    if "fused_mult_pre_sel" in text:
        # Replace the entire steering always_comb block
        old_block = re.search(
            r"  // Fused mult pre-compute steering \(ARVIS auto-generated\)\n  always_comb begin.*?endcase\n  end",
            text,
            re.DOTALL,
        )
        if old_block:
            # Build new steering block
            new_block = steering_sv
            text = text[: old_block.start()] + new_block + text[old_block.end() :]
            mult_path.write_text(text)
            print("  ✅ Updated mult pre-compute steering (replaced existing)")
            return True
        else:
            print("  ⚠️  Could not find steering block to replace")
            return False

    modified = False

    # ── Step 1: Add wire declarations + steering logic before int_op_a_msu ──
    wire_decls = (
        "\n"
        "  // ── Fused pre-compute mult input steering (ARVIS auto-generated) ──\n"
        "  logic        fused_mult_pre_sel;  // 1 = pre-compute active\n"
        "  logic [31:0] fused_mult_pre_a;    // pre-computed first multiply operand\n"
        "  logic [31:0] fused_mult_b;        // second multiply operand (swapped)\n"
        "  logic [31:0] fused_mult_acc;      // accumulator (0 for pure multiply)\n"
        "\n" + steering_sv + "\n"
    )

    # Insert before: "  // 32x32 = 32-bit multiplier"
    marker = "  // 32x32 = 32-bit multiplier"
    idx = text.find(marker)
    if idx < 0:
        # Try alternative marker
        marker = "  logic [31:0] int_op_a_msu;"
        idx = text.find(marker)
    if idx < 0:
        print("  ⚠️  Cannot find insertion point in cv32e40p_mult.sv")
        return False

    text = text[:idx] + wire_decls + "\n" + text[idx:]
    modified = True

    # ── Step 2: Modify int_op_a_msu to use pre-computed value ──
    # Original: assign int_op_a_msu = op_a_i ^ {32{int_is_msu}};
    # New:      assign int_op_a_msu = fused_mult_pre_a ^ {32{int_is_msu}};
    # ARVIS FPGA: we consume fused_mult_pre_a directly instead of selecting
    # through a sel-mux. The steering always_comb assigns fused_mult_pre_a =
    # op_a_i in its default branch, so this is functionally identical and
    # removes one LUT level immediately before the DSP48 A input.
    text = text.replace(
        "assign int_op_a_msu = op_a_i ^ {32{int_is_msu}};",
        "assign int_op_a_msu = fused_mult_pre_a ^ {32{int_is_msu}};",
        1,
    )

    # ── Step 3: Modify int_result to use steered op_b and accumulator ──
    # ARVIS FPGA: use fused_mult_acc / fused_mult_b directly rather than muxing
    # through fused_mult_pre_sel. The steering always_comb defaults these to
    # op_c_i / op_b_i so the mux is functionally redundant; eliminating it
    # removes one LUT level before the DSP48 C and B inputs.
    #
    # Original:
    #   assign int_result = $signed(op_c_i) + $signed(int_op_b_msu) +
    #                       $signed(int_op_a_msu) * $signed(op_b_i);
    #
    # New:
    #   assign int_result = $signed(fused_mult_acc) +
    #                       $signed(int_op_b_msu) +
    #                       $signed(int_op_a_msu) * $signed(fused_mult_b);

    # Replace the int_result expression
    # Match the multi-line assign
    old_int_result = re.search(
        r"(assign int_result = \$signed\(\n\s*)(op_c_i)(\n\s*\).*?\$signed\(\n\s*)(op_b_i)(\n\s*\);)",
        text,
        re.DOTALL,
    )
    if old_int_result:
        text = (
            text[: old_int_result.start(2)]
            + "fused_mult_acc"
            + text[old_int_result.end(2) : old_int_result.start(4)]
            + "fused_mult_b"
            + text[old_int_result.end(4) :]
        )
        modified = True
    else:
        # Try simpler single-line pattern
        text = re.sub(
            r"assign int_result = \$signed\(\s*op_c_i\s*\)",
            "assign int_result = $signed(\n      fused_mult_acc\n  )",
            text,
            count=1,
        )
        text = re.sub(
            r"\$signed\(\s*int_op_a_msu\s*\)\s*\*\s*\$signed\(\s*op_b_i\s*\)",
            "$signed(\n      int_op_a_msu\n  ) * $signed(\n      fused_mult_b\n  )",
            text,
            count=1,
        )
        modified = True

    if modified:
        mult_path.write_text(text)
        print("  ✅ Added mult pre-compute steering to cv32e40p_mult.sv")

    return modified
