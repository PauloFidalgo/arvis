"""
RTL post-fusion patches: SVA assertion disable and ALU_OP_WIDTH update.

These operate on RTL files after fusion patches have been applied.
Moved from pipeline/fusion_rtl.py to keep pipeline modules focused on orchestration.
"""

from __future__ import annotations

import re
from pathlib import Path

from arvis.cli import print_info, print_success, print_warning


def disable_custom0_sva(rtl_dir: Path) -> None:
    """Disable the SVA assertion that marks OPCODE_CUSTOM_0 as always illegal.

    The original cv32e40p_id_stage.sv asserts that CUSTOM_0 instructions
    are illegal when COREV_PULP=0. This must be disabled for fused instructions.
    """
    id_stage_path = rtl_dir / "cv32e40p_id_stage.sv"
    if not id_stage_path.exists():
        print_warning("cv32e40p_id_stage.sv not found — cannot disable CUSTOM_0 SVA")
        return

    text = id_stage_path.read_text()

    # Match the full assertion block containing OPCODE_CUSTOM_0
    pattern = re.compile(
        r"(\s*)(assert property\s*\(\s*\n?"
        r"\s*@\(posedge clk\) disable iff \(!rst_n\)\s*"
        r"\(\(instr\[6:0\] == OPCODE_CUSTOM_0\).*?"
        r"\|-> \(illegal_insn_dec == 'b1\)\);)",
        re.DOTALL,
    )

    match = pattern.search(text)
    if match:
        original_assertion = match.group(2)
        commented_lines = [f"// ARVIS_FUSED: {line}" for line in original_assertion.splitlines()]
        text = text[: match.start()] + "\n" + "\n".join(commented_lines) + text[match.end() :]
        id_stage_path.write_text(text)
        print_success("Disabled CUSTOM_0 SVA assertion in cv32e40p_id_stage.sv")
        return

    # Fallback: comment out individual OPCODE_CUSTOM lines in assert blocks
    lines = text.splitlines()
    modified = False
    in_custom_assert = False
    result_lines = []
    for i, line in enumerate(lines):
        if "OPCODE_CUSTOM_0" in line and "assert" not in line:
            for j in range(max(0, i - 5), i):
                if "assert property" in lines[j]:
                    in_custom_assert = True
                    break
        if in_custom_assert and any(
            kw in line
            for kw in [
                "OPCODE_CUSTOM_0",
                "OPCODE_CUSTOM_1",
                "OPCODE_CUSTOM_2",
                "OPCODE_CUSTOM_3",
                "|-> (illegal_insn_dec",
            ]
        ):
            result_lines.append(f"// ARVIS_FUSED: {line}")
            modified = True
            if "|-> (illegal_insn_dec" in line:
                in_custom_assert = False
        else:
            result_lines.append(line)

    if modified:
        id_stage_path.write_text("\n".join(result_lines))
        print_success("Disabled CUSTOM_0 SVA assertion in cv32e40p_id_stage.sv")
    else:
        print_info("No OPCODE_CUSTOM_0 SVA assertion found to disable")


def update_alu_op_width(rtl_dir: Path, gen: object) -> None:
    """Update ALU_OP_WIDTH if fused ops need more than 7 bits.

    The baseline cv32e40p uses ALU_OP_WIDTH = 7. If fused operations
    require values > 127, this increases to 8 bits and pads all literals.
    """
    pkg_path = rtl_dir / "include" / "cv32e40p_pkg.sv"
    if not pkg_path.exists():
        return

    pkg_text = pkg_path.read_text()
    all_vals = []
    for m in re.finditer(r"ALU_\w+\s*=\s*(\d+)'b([01]+)", pkg_text):
        width = int(m.group(1))
        val = int(m.group(2), 2)
        all_vals.append((width, val))

    if not all_vals:
        return

    max_width = max(w for w, _ in all_vals)
    max_value = max(v for _, v in all_vals)
    needed_width = max(7, max_value.bit_length())

    if needed_width <= max_width:
        return

    print(
        f"  Updating ALU_OP_WIDTH: {max_width} → {needed_width} "
        f"(max value = {max_value} = 0b{max_value:0{needed_width}b})"
    )

    pkg_text = re.sub(
        r"parameter ALU_OP_WIDTH = \d+;",
        f"parameter ALU_OP_WIDTH = {needed_width};",
        pkg_text,
    )

    def _fix_alu_literal(m: re.Match) -> str:
        name = m.group(1)
        bits = m.group(3)
        if len(bits) < needed_width:
            bits = bits.zfill(needed_width)
        return f"ALU_{name} = {needed_width}'b{bits}"

    pkg_text = re.sub(r"ALU_(\w+)\s*=\s*(\d+)'b([01]+)", _fix_alu_literal, pkg_text)
    pkg_path.write_text(pkg_text)
    print_success(f"Updated {pkg_path}")
