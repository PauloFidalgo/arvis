"""Add fused_imm_i[9:0] port to ALU and wire it through the pipeline.

Only modifies RTL when patterns with variable immediates are detected.
Uses regex-based insertion to add ports and wires to existing SV files.

Pipeline path:
  decoder (fused_imm_o) → id_stage wire (fused_imm) → ID/EX pipeline
  register (fused_imm_ex_o) → core.sv wire (fused_imm_ex) →
  ex_stage (fused_imm_i) → ALU (fused_imm_i)

Files modified:
  - cv32e40p_alu.sv: add input [9:0] fused_imm_i
  - cv32e40p_decoder.sv: add output [9:0] fused_imm_o + default
  - cv32e40p_ex_stage.sv: add input port + connect to ALU instance
  - cv32e40p_id_stage.sv: add wire, output port, pipeline register
  - cv32e40p_core.sv: add wire + connect id_stage→ex_stage
"""

from __future__ import annotations

import re
from pathlib import Path


def needs_fused_imm(used_ops: list) -> bool:
    """Check if any used operation references fused_imm_i in its SV expression
    or uses parametric immediate encoding (rs3_5bit).

    The latter is needed because ARVIS shared-imm-mux RTL references
    ``fused_imm_i[4:0]`` even when no individual op's sv_expression mentions
    it (the wires are computed centrally and consumed indirectly via
    ``arvis_shr`` / ``arvis_shl`` / etc.).
    """
    for op in used_ops:
        if "fused_imm_i" in getattr(op, "sv_expression", ""):
            return True
        # ARVIS Approach A: any parametric op (rs3_5bit) drives the shared
        # imm muxes which consume fused_imm_i, so the port must exist.
        imm_enc = getattr(op, "imm_encoding", {}) or {}
        if any(v == "rs3_5bit" for v in imm_enc.values()):
            return True
    return False


def _has_port_declaration(text: str, signal_name: str) -> bool:
    """Check if a signal has a proper port declaration (input/output).

    Restricted to a single line so a downstream reference to the signal
    inside the module body can't be mistaken for a port declaration on
    a different line above (the module signature uses one line per port).
    """
    return bool(
        re.search(
            rf"(?:input|output)\s+(?:logic\s+)?(?:\[[^\]]*\]\s+)?{signal_name}\b",
            text,
        )
    )


def add_fused_imm_port(rtl_dir: str, imm_width: int = 10) -> bool:
    """Add fused_imm_i port to ALU and wire through the full pipeline.

    Returns True if any file was modified.
    """
    rtl = Path(rtl_dir)
    modified = False

    # ── 1. ALU: add input port ──
    alu_path = rtl / "cv32e40p_alu.sv"
    if alu_path.exists():
        text = alu_path.read_text()
        if not _has_port_declaration(text, "fused_imm_i"):
            text = re.sub(
                r"(input\s+alu_opcode_e\s+operator_i\s*,)",
                rf"\1\n  // ARVIS: Fused immediate bus for variable immediates\n"
                rf"  input  logic [{imm_width - 1}:0] fused_imm_i,",
                text,
                count=1,
            )
            alu_path.write_text(text)
            modified = True
            print(f"  ✅ Added fused_imm_i[{imm_width - 1}:0] to cv32e40p_alu.sv")

    # ── 2. Decoder: add output port + default ──
    dec_path = rtl / "cv32e40p_decoder.sv"
    if dec_path.exists():
        text = dec_path.read_text()
        if not _has_port_declaration(text, "fused_imm_o"):
            # Add output port after alu_en_o
            text = re.sub(
                r"(output\s+logic\s+alu_en_o\s*,)",
                rf"\1\n  // ARVIS: Fused immediate output for variable immediates\n"
                rf"  output logic [{imm_width - 1}:0] fused_imm_o,",
                text,
                count=1,
            )
            # Add default assignment after illegal_insn_o = 1'b0;
            text = re.sub(
                r"(illegal_insn_o\s*=\s*1'b0;)",
                rf"\1\n    fused_imm_o    = {imm_width}'b0;",
                text,
                count=1,
            )
            dec_path.write_text(text)
            modified = True
            print(f"  ✅ Added fused_imm_o[{imm_width - 1}:0] to cv32e40p_decoder.sv")

    # ── 3. EX stage: add input port + connect to ALU and MULT instances ──
    ex_path = rtl / "cv32e40p_ex_stage.sv"
    if ex_path.exists():
        text = ex_path.read_text()
        if not _has_port_declaration(text, "fused_imm_i"):
            # Add input port
            text = re.sub(
                r"(input\s+alu_opcode_e\s+alu_operator_i\s*,)",
                rf"\1\n  // ARVIS: Fused immediate from decoder\n"
                rf"  input  logic [{imm_width - 1}:0] fused_imm_i,",
                text,
                count=1,
            )
            # Connect to ALU instance
            text = re.sub(
                r"(\.operator_i\s*\(\s*alu_operator_i\s*\)\s*,)",
                r"\1\n    .fused_imm_i  (fused_imm_i),",
                text,
                count=1,
            )
            modified = True

        # Connect to MULT instance (if not already connected)
        mult_inst_match = re.search(r"cv32e40p_mult\s+mult_i\s*\(", text)
        if mult_inst_match:
            after_inst = text[mult_inst_match.start() :]
            inst_block = after_inst[: after_inst.index(");") + 2]
            if ".fused_imm_i" not in inst_block:
                # Find the mult instance and add fused_imm_i port connection
                text = re.sub(
                    r"(cv32e40p_mult\s+mult_i\s*\([\s\S]*?\.operator_i\s*\(\s*mult_operator_i\s*\)\s*,)",
                    r"\1\n      .fused_imm_i  (fused_imm_i),",
                    text,
                    flags=re.DOTALL,
                    count=1,
                )
                modified = True

        if modified:
            ex_path.write_text(text)
            print("  ✅ Wired fused_imm through cv32e40p_ex_stage.sv (ALU + MULT)")

    # ── 3b. MULT: add input port ──
    mult_path = rtl / "cv32e40p_mult.sv"
    if mult_path.exists():
        text = mult_path.read_text()
        if not _has_port_declaration(text, "fused_imm_i"):
            text = re.sub(
                r"(input\s+mul_opcode_e\s+operator_i\s*,)",
                rf"\1\n    // ARVIS: Fused immediate bus for variable immediates\n"
                rf"    input  logic [{imm_width - 1}:0] fused_imm_i,",
                text,
                count=1,
            )
            mult_path.write_text(text)
            modified = True
            print(f"  ✅ Added fused_imm_i[{imm_width - 1}:0] to cv32e40p_mult.sv")

    # ── 4. ID stage: add wire, output port, pipeline register ──
    id_path = rtl / "cv32e40p_id_stage.sv"
    if id_path.exists():
        text = id_path.read_text()
        if not _has_port_declaration(text, "fused_imm_ex_o"):
            # 4a. Add output port after alu_en_ex_o.
            # The fused_imm_ex_o register fans out to the ALU result mux,
            # the MULT pre-compute steering and the shifter steering. On
            # FPGA that combined fanout stretches the clock-to-LUT path on
            # critical expressions. MAX_FANOUT + KEEP asks Vivado to
            # replicate the register close to each consumer group; the
            # attributes are silently ignored by ASIC synthesisers so the
            # emission is platform-neutral.
            # Build the replacement string. Use a regular f-string for the
            # attribute line (double quotes around "true" must be literal),
            # then rely on re.sub's \1 backreference for the matched group.
            keep_attr = '(* MAX_FANOUT = 50 *) (* KEEP = "true" *)'
            port_decl = (
                f"output logic [{imm_width - 1}:0]        fused_imm_ex_o,  // ARVIS: fused immediate pipeline reg"
            )
            text = re.sub(
                r"(output\s+logic\s+alu_en_ex_o\s*,)",
                lambda m: f"{m.group(1)}\n    {keep_attr}\n    {port_decl}",
                text,
                count=1,
            )
            modified = True

        # 4b. Add internal wire (only if not present)
        if "fused_imm" not in text or not re.search(r"logic\s+\[\d+:\d+\]\s+fused_imm\s*;", text):
            text = re.sub(
                r"(logic\s+alu_en;)",
                rf"\1\n  logic [{imm_width - 1}:0] fused_imm;"
                rf"  // ARVIS: fused immediate bus",
                text,
                count=1,
            )
            modified = True

        # 4c. Connect decoder output (only if not present)
        if ".fused_imm_o" not in text:
            text = re.sub(
                r"(\.alu_en_o\s*\(\s*alu_en\s*\)\s*,)",
                r"\1\n    .fused_imm_o  (fused_imm),",
                text,
                count=1,
            )
            modified = True

        # 4d. Add pipeline register reset: after alu_en_ex_o <= '0;
        if "fused_imm_ex_o" not in text or "fused_imm_ex_o         <= '0;" not in text:
            text = re.sub(
                r"(alu_en_ex_o\s*<=\s*'0;)",
                r"\1\n      fused_imm_ex_o         <= '0;",
                text,
                count=1,
            )
            modified = True

        # 4e. Add pipeline register assignment: after alu_en_ex_o <= alu_en;
        # in the "normal pipeline unstall" block
        if "fused_imm_ex_o <= fused_imm;" not in text:
            text = re.sub(
                r"(alu_en_ex_o\s*<=\s*alu_en;)",
                r"\1\n        fused_imm_ex_o <= fused_imm;",
                text,
                count=1,
            )
            modified = True

        if modified:
            id_path.write_text(text)
            print("  ✅ Added fused_imm pipeline register to cv32e40p_id_stage.sv")

    # ── 5. Core: add wire + connect id_stage→ex_stage ──
    core_path = rtl / "cv32e40p_core.sv"
    if core_path.exists():
        text = core_path.read_text()
        core_modified = False

        # 5a. Add wire declaration after alu_en_ex
        if "fused_imm_ex" not in text:
            text = re.sub(
                r"(logic\s+alu_en_ex;)",
                rf"\1\n  logic [{imm_width - 1}:0]"
                rf"         fused_imm_ex;"
                rf"  // ARVIS: fused immediate pipeline wire",
                text,
                count=1,
            )
            core_modified = True

        # 5b. Connect id_stage output: after .alu_en_ex_o(alu_en_ex),
        if ".fused_imm_ex_o" not in text:
            text = re.sub(
                r"(\.alu_en_ex_o\s*\(\s*alu_en_ex\s*\)\s*,)",
                r"\1\n      .fused_imm_ex_o     (fused_imm_ex),",
                text,
                count=1,
            )
            core_modified = True

        # 5c. Connect ex_stage input: after .alu_en_i(alu_en_ex),
        if ".fused_imm_i" not in text:
            text = re.sub(
                r"(\.alu_en_i\s*\(\s*alu_en_ex\s*\)\s*,)",
                r"\1\n      .fused_imm_i     (fused_imm_ex),",
                text,
                count=1,
            )
            core_modified = True

        if core_modified:
            core_path.write_text(text)
            modified = True
            print("  ✅ Wired fused_imm through cv32e40p_core.sv")

    return modified
