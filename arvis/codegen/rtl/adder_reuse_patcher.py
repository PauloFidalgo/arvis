"""Patch cv32e40p_alu.sv to reuse the existing partitioned adder for fused ops.

Only modifies RTL when fused operations with hw_strategy="reuse_adder" are
detected.  Uses regex-based insertion to:
  1. Declare fused_adder_* wires
  2. Insert the steering always_comb block (operator_i case → fused inputs)
  3. Refactor baseline adder_op_a/b assigns into a mux with fused override

This is a post-fusion patcher, called after pragma patching (result_mux etc).
It follows the same pattern as fused_imm_patcher.py.

Files modified:
  - cv32e40p_alu.sv only (all changes are ALU-internal)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def needs_adder_reuse(used_ops: list) -> bool:
    """Check if any used operation has hw_strategy='reuse_adder'."""
    for op in used_ops:
        if getattr(op, "hw_strategy", "inline") == "reuse_adder":
            return True
    return False


def add_adder_reuse_wiring(rtl_dir: str, steering_sv: str) -> bool:
    """Patch cv32e40p_alu.sv to reuse the existing adder for fused ops.

    Args:
        rtl_dir: Path to the RTL directory (already copied to output).
        steering_sv: The generated always_comb block from
                     RTLGenerator.generate_alu_adder_steering().

    Returns True if the file was modified.
    """
    alu_path = Path(rtl_dir) / "cv32e40p_alu.sv"
    if not alu_path.exists():
        print("  ⚠️  cv32e40p_alu.sv not found — cannot add adder reuse wiring")
        return False

    text = alu_path.read_text()

    # Guard: don't patch twice
    if "fused_adder_sel" in text:
        print("  ℹ️  Adder reuse wiring already present — skipping")
        return False

    modified = False

    # ── Step 1: Add wire declarations after the alu_signals pragma ──
    # Insert after the ARVIS_FUSED_END: alu_signals line
    wire_decls = (
        "\n"
        "  // ── Fused-instruction adder reuse wires (ARVIS auto-generated) ──\n"
        "  logic        fused_adder_sel;        // 1 = fused op steers the adder\n"
        "  logic [31:0] fused_adder_op_a;       // fused adder operand A\n"
        "  logic [31:0] fused_adder_op_b;       // fused adder operand B\n"
        "  logic        fused_adder_b_negate;   // fused adder subtraction control\n"
        "\n"
        "  // ── Fused adder steering logic (ARVIS auto-generated) ──\n" + steering_sv + "\n"
    )

    # Insert after the alu_signals pragma end marker
    marker = "// ARVIS_FUSED_END: alu_signals"
    idx = text.find(marker)
    if idx < 0:
        print("  ⚠️  ARVIS_FUSED_END: alu_signals not found — cannot insert wires")
        return False

    end_of_line = text.index("\n", idx) + 1
    text = text[:end_of_line] + wire_decls + text[end_of_line:]
    modified = True

    # ── Step 2: Refactor adder_op_b_negate assign ──
    # Change:   assign adder_op_b_negate = (operator_i == ALU_SUB) || ...
    # To:       assign baseline_adder_op_b_negate = ...
    #           assign adder_op_b_negate = fused_adder_sel ? fused_adder_b_negate : baseline_...

    # 2a. Rename adder_op_b_negate assign to baseline_
    old_negate = re.search(
        r"(\n  assign adder_op_b_negate = )(.*?)(;\n)",
        text,
        re.DOTALL,
    )
    if old_negate:
        # Add baseline wire declaration + rename + add mux
        baseline_decl = "\n  // Baseline adder controls (before fused override mux)\n"
        baseline_decl += "  logic        baseline_adder_op_b_negate;\n"
        baseline_decl += "  logic [31:0] baseline_adder_op_a;\n"
        baseline_decl += "  logic [31:0] baseline_adder_op_b;\n"

        # Insert baseline declarations before the adder_op_b_negate assign
        negate_start = old_negate.start()
        text = text[:negate_start] + baseline_decl + text[negate_start:]

        # Now rename the assign (accounting for the inserted text offset)
        text = text.replace(
            "\n  assign adder_op_b_negate = ",
            "\n  assign baseline_adder_op_b_negate = ",
            1,
        )
        modified = True

    # 2b. Rename adder_op_a assign to baseline_
    text = text.replace(
        "\n  // prepare operand a\n  assign adder_op_a = ",
        "\n  // prepare operand a (baseline)\n  assign baseline_adder_op_a = ",
        1,
    )

    # 2c. Rename adder_op_b assign to baseline_
    text = text.replace(
        "\n  // prepare operand b\n  assign adder_op_b = ",
        "\n  // prepare operand b (baseline)\n  assign baseline_adder_op_b = ",
        1,
    )

    # Also fix: baseline_adder_op_b uses adder_op_b_negate → baseline_adder_op_b_negate
    text = text.replace(
        "assign baseline_adder_op_b = adder_op_b_negate",
        "assign baseline_adder_op_b = baseline_adder_op_b_negate",
        1,
    )

    # 2d. Add the fused override mux assigns AFTER the baseline_adder_op_b assign
    mux_code = (
        "\n"
        "  // Fused override mux: when fused_adder_sel is active, use fused operands\n"
        "  // instead of baseline. Synthesis eliminates the mux when fused_adder_sel=0.\n"
        "  // NOTE: baseline_adder_op_b already includes bitwise-NOT for subtraction,\n"
        "  // but fused_adder_op_b is raw — we must negate it here when fused_adder_b_negate=1.\n"
        "  assign adder_op_b_negate = fused_adder_sel ? fused_adder_b_negate : baseline_adder_op_b_negate;\n"
        "  assign adder_op_a        = fused_adder_sel ? fused_adder_op_a     : baseline_adder_op_a;\n"
        "  assign adder_op_b        = fused_adder_sel ? (fused_adder_b_negate ? ~fused_adder_op_b : fused_adder_op_b) : baseline_adder_op_b;\n"
    )

    # Find the end of the baseline_adder_op_b assign and insert mux after it
    # The baseline_adder_op_b assign ends with "operand_b_i;\n"
    m = re.search(
        r"(assign baseline_adder_op_b = .*?;\n)",
        text,
        re.DOTALL,
    )
    if m:
        insert_pos = m.end()
        text = text[:insert_pos] + mux_code + text[insert_pos:]
        modified = True

    if modified:
        alu_path.write_text(text)
        print("  ✅ Added adder reuse wiring to cv32e40p_alu.sv")

    return modified
