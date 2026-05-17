"""
Decoder Generator for cv32e40p.

Takes a set of used instructions (from the ISA DB) plus optional custom
instructions and generates a clean ``cv32e40p_decoder.sv`` with a
hierarchical ``unique case`` structure:

    opcode → funct3 → funct7/funct7_6

Only the instructions actually used by the target firmware are included;
everything else falls through to ``illegal_insn_o = 1'b1``.

Usage
-----
::

    from arvis.analysis.isa_db import ISA_DB, InstructionEntry, OPCODE_CUSTOM_0
    from arvis.codegen.rtl.decoder_gen import generate_decoder

    # Select subset
    used = {k: v for k, v in ISA_DB.items()
            if k in {'add', 'sub', 'addi', 'lw', 'sw', 'beq', 'jal', 'lui'}}

    # Optionally add custom instructions
    custom = [InstructionEntry(
        name="xori_and", opcode=OPCODE_CUSTOM_0, funct3=0b000, funct7=0b0000001,
        insn_type="R", extension="CUSTOM",
        signals={"alu_operator_o": "ALU_XORI_AND", "regfile_alu_we": 1,
                 "rega_used_o": 1, "regb_used_o": 1},
    )]

    sv_code = generate_decoder(used, custom_instructions=custom)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from arvis.analysis.isa_db import (
    DEFAULT_SIGNALS,
    OPCODE_AUIPC,
    OPCODE_BRANCH,
    OPCODE_CUSTOM_0,
    OPCODE_CUSTOM_1,
    OPCODE_CUSTOM_2,
    OPCODE_CUSTOM_3,
    OPCODE_FENCE,
    OPCODE_JAL,
    OPCODE_JALR,
    OPCODE_LOAD,
    OPCODE_LUI,
    OPCODE_NAMES,
    OPCODE_OP,
    OPCODE_OPIMM,
    OPCODE_STORE,
    OPCODE_SYSTEM,
    InstructionEntry,
)

# ───────────────────────────────────────────────────────────────────
# Value formatting helpers
# ───────────────────────────────────────────────────────────────────


def _sv_val(val: Any) -> str:
    """Format a Python value as a SystemVerilog literal."""
    if isinstance(val, int):
        return f"1'b{val}" if val in (0, 1) else str(val)
    return str(val)


def _indent(text: str, level: int) -> str:
    """Indent each line of *text* by *level* × 2 spaces."""
    prefix = "  " * level
    return "\n".join(prefix + line if line.strip() else "" for line in text.split("\n"))


# ───────────────────────────────────────────────────────────────────
# Signal assignment block generation
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────
# Case hierarchy builders
# ───────────────────────────────────────────────────────────────────


def _group_by_funct3(
    entries: List[InstructionEntry],
) -> Dict[Optional[int], List[InstructionEntry]]:
    """Group instruction entries by funct3 value."""
    grouped: Dict[Optional[int], List[InstructionEntry]] = defaultdict(list)
    for e in entries:
        grouped[e.funct3].append(e)
    return dict(grouped)


def _group_by_funct7(
    entries: List[InstructionEntry],
) -> Dict[Optional[int], List[InstructionEntry]]:
    """Group by full funct7 (bits [31:25])."""
    grouped: Dict[Optional[int], List[InstructionEntry]] = defaultdict(list)
    for e in entries:
        grouped[e.funct7].append(e)
    return dict(grouped)


# ───────────────────────────────────────────────────────────────────
# Opcode-specific emitters
# ───────────────────────────────────────────────────────────────────


def _emit_opcode_simple(opcode_name: str, entries: List[InstructionEntry]) -> str:
    """Emit for opcodes with a single instruction (LUI, AUIPC, JAL)."""
    assert len(entries) == 1
    e = entries[0]
    lines = [f"{opcode_name}: begin  // {e.description}"]
    for sig, val in sorted(e.signals.items()):
        if val != DEFAULT_SIGNALS.get(sig):
            lines.append(f"  {sig:<30s} = {_sv_val(val)};")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_with_funct3(
    opcode_name: str,
    entries: List[InstructionEntry],
    comment: str = "",
) -> str:
    """Emit for opcodes that sub-decode by funct3 only."""
    lines = [f"{opcode_name}: begin{f'  // {comment}' if comment else ''}"]
    by_f3 = _group_by_funct3(entries)

    # Check if there are entries without funct3 (shouldn't happen here)
    if None in by_f3:
        # Single entry without funct3 distinction
        for e in by_f3[None]:
            for sig, val in sorted(e.signals.items()):
                if val != DEFAULT_SIGNALS.get(sig):
                    lines.append(f"  {sig:<30s} = {_sv_val(val)};")
    else:
        lines.append("  unique case (instr_rdata_i[14:12])")
        for f3 in sorted(k for k in by_f3 if k is not None):
            f3_entries = by_f3[f3]
            if len(f3_entries) == 1:
                e = f3_entries[0]
                # Check if needs further funct7 decode
                if e.funct7 is not None:
                    # There might be multiple entries for same funct3 with different funct7
                    # Handle this case in _emit_opcode_opimm
                    lines.append(f"    3'b{f3:03b}: begin  // {e.name}")
                    for sig, val in sorted(e.signals.items()):
                        if val != DEFAULT_SIGNALS.get(sig):
                            lines.append(f"      {sig:<30s} = {_sv_val(val)};")
                    lines.append("    end")
                else:
                    lines.append(f"    3'b{f3:03b}: begin  // {e.name}")
                    for sig, val in sorted(e.signals.items()):
                        if val != DEFAULT_SIGNALS.get(sig):
                            lines.append(f"      {sig:<30s} = {_sv_val(val)};")
                    lines.append("    end")
            else:
                # Multiple entries for same funct3 (e.g., srli/srai)
                lines.append(f"    3'b{f3:03b}: begin")
                _emit_funct7_subcase(lines, f3_entries, indent=6)
                lines.append("    end")
        lines.append("    default: illegal_insn_o = 1'b1;")
        lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_funct7_subcase(
    lines: List[str],
    entries: List[InstructionEntry],
    indent: int = 6,
) -> None:
    """Emit a sub-case on funct7 (bits [31:25]) inside a funct3 case."""
    pfx = " " * indent
    by_f7 = _group_by_funct7(entries)
    if None in by_f7 and len(by_f7) == 1:
        # No funct7 needed, just emit directly
        for e in by_f7[None]:
            lines.append(f"{pfx}// {e.name}")
            for sig, val in sorted(e.signals.items()):
                if val != DEFAULT_SIGNALS.get(sig):
                    lines.append(f"{pfx}{sig:<30s} = {_sv_val(val)};")
        return

    lines.append(f"{pfx}unique case (instr_rdata_i[31:25])")
    for f7 in sorted(k for k in by_f7 if k is not None):
        for e in by_f7[f7]:
            lines.append(f"{pfx}  7'b{f7:07b}: begin  // {e.name}")
            for sig, val in sorted(e.signals.items()):
                if val != DEFAULT_SIGNALS.get(sig):
                    lines.append(f"{pfx}    {sig:<30s} = {_sv_val(val)};")
            lines.append(f"{pfx}  end")
    if None in by_f7:
        for e in by_f7[None]:
            lines.append(f"{pfx}  default: begin  // {e.name}")
            for sig, val in sorted(e.signals.items()):
                if val != DEFAULT_SIGNALS.get(sig):
                    lines.append(f"{pfx}    {sig:<30s} = {_sv_val(val)};")
            lines.append(f"{pfx}  end")
    else:
        lines.append(f"{pfx}  default: illegal_insn_o = 1'b1;")
    lines.append(f"{pfx}endcase")


def _emit_opcode_opimm(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_OPIMM with funct3 → optional funct7 sub-decode."""
    lines = ["OPCODE_OPIMM: begin // Register-Immediate ALU Operations"]

    # Common signals for all OPIMM instructions
    lines.append("  alu_op_b_mux_sel_o           = OP_B_IMM;")
    lines.append("  imm_b_mux_sel_o              = IMMB_I;")
    lines.append("  regfile_alu_we               = 1'b1;")
    lines.append("  rega_used_o                  = 1'b1;")
    lines.append("")
    lines.append("  unique case (instr_rdata_i[14:12])")

    by_f3 = _group_by_funct3(entries)
    for f3 in sorted(k for k in by_f3 if k is not None):
        f3_entries = by_f3[f3]
        if len(f3_entries) == 1 and f3_entries[0].funct7 is None:
            e = f3_entries[0]
            lines.append(f"    3'b{f3:03b}: alu_operator_o = {e.signals['alu_operator_o']};  // {e.name}")
        elif len(f3_entries) == 1:
            e = f3_entries[0]
            lines.append(f"    3'b{f3:03b}: begin  // {e.name}")
            lines.append(f"      alu_operator_o = {e.signals['alu_operator_o']};")
            if e.funct7 is not None:
                lines.append(f"      if (instr_rdata_i[31:25] != 7'b{e.funct7:07b})")
                lines.append("        illegal_insn_o = 1'b1;")
            lines.append("    end")
        else:
            # Multiple entries for same funct3 (e.g., srli/srai at funct3=101)
            lines.append(f"    3'b{f3:03b}: begin")
            has_f7 = any(e.funct7 is not None for e in f3_entries)
            if has_f7:
                f7_entries = [e for e in f3_entries if e.funct7 is not None]
                for idx, e in enumerate(f7_entries):
                    kw = "if" if idx == 0 else "else if"
                    lines.append(f"      {kw} (instr_rdata_i[31:25] == 7'b{e.funct7:07b})")
                    lines.append(f"        alu_operator_o = {e.signals['alu_operator_o']};  // {e.name}")
                # Add else for illegal
                lines.append("      else")
                lines.append("        illegal_insn_o = 1'b1;")
            lines.append("    end")

    lines.append("    default: illegal_insn_o = 1'b1;")
    lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_op(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_OP with {funct7_6, funct3} combined case key."""
    lines = ["OPCODE_OP: begin  // Register-Register ALU / Mul / Div"]
    lines.append("  regfile_alu_we               = 1'b1;")
    lines.append("  rega_used_o                  = 1'b1;")
    lines.append("")
    lines.append("  unique case ({instr_rdata_i[30:25], instr_rdata_i[14:12]})")

    for e in sorted(entries, key=lambda x: ((x.funct7_6 or 0) << 3) | (x.funct3 or 0)):
        f76 = e.funct7_6 if e.funct7_6 is not None else 0
        f3 = e.funct3 if e.funct3 is not None else 0
        key = f"{{6'b{f76:06b}, 3'b{f3:03b}}}"

        # Collect non-common signals (remove regfile_alu_we, rega_used_o since hoisted)
        delta = {}
        for sig, val in e.signals.items():
            if sig in ("regfile_alu_we", "rega_used_o") and val == 1:
                continue
            if val != DEFAULT_SIGNALS.get(sig):
                delta[sig] = val

        if not delta or (len(delta) == 1 and "alu_operator_o" in delta):
            # Simple case: only ALU op differs
            if "alu_operator_o" in delta:
                lines.append(f"    {key}: alu_operator_o = {delta['alu_operator_o']};  // {e.name}")
            else:
                lines.append(f"    {key}: ;  // {e.name} (defaults)")
        else:
            lines.append(f"    {key}: begin  // {e.name}")
            for sig, val in sorted(delta.items()):
                lines.append(f"      {sig:<30s} = {_sv_val(val)};")
            lines.append("    end")

    lines.append("    default: illegal_insn_o = 1'b1;")
    lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_branch(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_BRANCH with funct3 case for ALU operator."""
    lines = ["OPCODE_BRANCH: begin  // Conditional Branches"]
    lines.append("  ctrl_transfer_target_mux_sel_o = JT_COND;")
    lines.append("  ctrl_transfer_insn             = BRANCH_COND;")
    lines.append("  alu_op_c_mux_sel_o             = OP_C_JT;")
    lines.append("  rega_used_o                    = 1'b1;")
    lines.append("  regb_used_o                    = 1'b1;")
    lines.append("")
    lines.append("  unique case (instr_rdata_i[14:12])")
    for e in sorted(entries, key=lambda x: x.funct3 or 0):
        f3 = e.funct3 if e.funct3 is not None else 0
        lines.append(f"    3'b{f3:03b}: alu_operator_o = {e.signals['alu_operator_o']};  // {e.name}")
    lines.append("    default: illegal_insn_o = 1'b1;")
    lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_load(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_LOAD with funct3 case for data type/sign extension."""
    lines = ["OPCODE_LOAD: begin  // Load"]
    lines.append("  data_req                       = 1'b1;")
    lines.append("  regfile_mem_we                 = 1'b1;")
    lines.append("  rega_used_o                    = 1'b1;")
    lines.append("  alu_operator_o                 = ALU_ADD;")
    lines.append("  alu_op_b_mux_sel_o             = OP_B_IMM;")
    lines.append("  imm_b_mux_sel_o                = IMMB_I;")
    lines.append("")
    lines.append("  // sign/zero extension")
    lines.append("  data_sign_extension_o = {1'b0,~instr_rdata_i[14]};")
    lines.append("")
    lines.append("  unique case (instr_rdata_i[14:12])")

    # Group by data_type_o
    for e in sorted(entries, key=lambda x: x.funct3 or 0):
        f3 = e.funct3 if e.funct3 is not None else 0
        dt = e.signals.get("data_type_o", "2'b00")
        lines.append(f"    3'b{f3:03b}: data_type_o = {dt};  // {e.name}")

    lines.append("    default: illegal_insn_o = 1'b1;")
    lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_store(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_STORE with funct3 case for data type."""
    lines = ["OPCODE_STORE: begin  // Store"]
    lines.append("  data_req                       = 1'b1;")
    lines.append("  data_we_o                      = 1'b1;")
    lines.append("  rega_used_o                    = 1'b1;")
    lines.append("  regb_used_o                    = 1'b1;")
    lines.append("  alu_operator_o                 = ALU_ADD;")
    lines.append("  alu_op_c_mux_sel_o             = OP_C_REGB_OR_FWD;")
    lines.append("  imm_b_mux_sel_o                = IMMB_S;")
    lines.append("  alu_op_b_mux_sel_o             = OP_B_IMM;")
    lines.append("")
    lines.append("  unique case (instr_rdata_i[14:12])")
    for e in sorted(entries, key=lambda x: x.funct3 or 0):
        f3 = e.funct3 if e.funct3 is not None else 0
        dt = e.signals.get("data_type_o", "2'b00")
        lines.append(f"    3'b{f3:03b}: data_type_o = {dt};  // {e.name}")
    lines.append("    default: begin")
    lines.append("      illegal_insn_o = 1'b1;")
    lines.append("      data_req       = 1'b0;")
    lines.append("      data_we_o      = 1'b0;")
    lines.append("    end")
    lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_system(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_SYSTEM handling both non-CSR and CSR instructions."""
    csr_entries = [e for e in entries if e.funct3 is not None and e.funct3 != 0]
    non_csr = [e for e in entries if e.funct3 is None or e.funct3 == 0]

    lines = ["OPCODE_SYSTEM: begin"]
    lines.append("  if (instr_rdata_i[14:12] == 3'b000) begin")
    lines.append("    // non CSR related SYSTEM instructions")
    lines.append("    if ({instr_rdata_i[19:15], instr_rdata_i[11:7]} == '0) begin")
    lines.append("      unique case (instr_rdata_i[31:20])")

    for e in non_csr:
        if e.name == "ecall":
            lines.append("        12'h000: ecall_insn_o = 1'b1;  // ECALL")
        elif e.name == "ebreak":
            lines.append("        12'h001: ebrk_insn_o = 1'b1;  // EBREAK")
        elif e.name == "mret":
            lines.append("        12'h302: begin  // MRET")
            lines.append("          mret_insn_o = 1'b1;")
            lines.append("          mret_dec_o  = 1'b1;")
            lines.append("        end")
        elif e.name == "wfi":
            lines.append("        12'h105: wfi_o = 1'b1;  // WFI")

    lines.append("        default: illegal_insn_o = 1'b1;")
    lines.append("      endcase")
    lines.append("    end else illegal_insn_o = 1'b1;")
    lines.append("  end")

    if csr_entries:
        lines.append("  else begin")
        lines.append("    // CSR instructions")
        lines.append("    csr_access_o                 = 1'b1;")
        lines.append("    regfile_alu_we               = 1'b1;")
        lines.append("    alu_op_b_mux_sel_o           = OP_B_IMM;")
        lines.append("    imm_a_mux_sel_o              = IMMA_Z;")
        lines.append("    imm_b_mux_sel_o              = IMMB_I;")
        lines.append("")
        lines.append("    if (instr_rdata_i[14] == 1'b1) begin")
        lines.append("      alu_op_a_mux_sel_o = OP_A_IMM;")
        lines.append("    end else begin")
        lines.append("      rega_used_o        = 1'b1;")
        lines.append("      alu_op_a_mux_sel_o = OP_A_REGA_OR_FWD;")
        lines.append("    end")
        lines.append("")
        lines.append("    unique case (instr_rdata_i[13:12])")
        lines.append("      2'b01:   csr_op = CSR_OP_WRITE;")
        lines.append("      2'b10:   csr_op = instr_rdata_i[19:15] == 5'b0 ? CSR_OP_READ : CSR_OP_SET;")
        lines.append("      2'b11:   csr_op = instr_rdata_i[19:15] == 5'b0 ? CSR_OP_READ : CSR_OP_CLEAR;")
        lines.append("      default: csr_illegal = 1'b1;")
        lines.append("    endcase")
        lines.append("")
        lines.append("    illegal_insn_o = csr_illegal;")
        lines.append("  end")

    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_fence(entries: List[InstructionEntry]) -> str:
    """Emit OPCODE_FENCE."""
    lines = ["OPCODE_FENCE: begin"]
    lines.append("  unique case (instr_rdata_i[14:12])")
    for e in sorted(entries, key=lambda x: x.funct3 or 0):
        f3 = e.funct3 if e.funct3 is not None else 0
        lines.append(f"    3'b{f3:03b}: fencei_insn_o = 1'b1;  // {e.name}")
    lines.append("    default: illegal_insn_o = 1'b1;")
    lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


def _emit_opcode_custom(
    opcode_name: str,
    entries: List[InstructionEntry],
) -> str:
    """Emit a CUSTOM opcode block with funct3 → funct7 hierarchy."""
    lines = [f"{opcode_name}: begin"]
    by_f3 = _group_by_funct3(entries)

    if len(by_f3) == 1 and None in by_f3:
        # Single custom instruction, no sub-decode needed
        for e in by_f3[None]:
            for sig, val in sorted(e.signals.items()):
                if val != DEFAULT_SIGNALS.get(sig):
                    lines.append(f"  {sig:<30s} = {_sv_val(val)};  // {e.name}")
    else:
        lines.append("  unique case (instr_rdata_i[14:12])")
        for f3 in sorted(k for k in by_f3 if k is not None):
            f3_entries = by_f3[f3]
            by_f7 = _group_by_funct7(f3_entries)
            has_f7 = any(e.funct7 is not None for e in f3_entries)

            if not has_f7 and len(f3_entries) == 1:
                e = f3_entries[0]
                lines.append(f"    3'b{f3:03b}: begin  // {e.name}")
                for sig, val in sorted(e.signals.items()):
                    if val != DEFAULT_SIGNALS.get(sig):
                        lines.append(f"      {sig:<30s} = {_sv_val(val)};")
                lines.append("    end")
            elif has_f7:
                is_r4 = any(e.is_r4 for e in f3_entries)
                if is_r4:
                    # R4-type: decode on funct2 (bits [26:25])
                    lines.append(f"    3'b{f3:03b}: begin")
                    lines.append("      unique case (instr_rdata_i[26:25])")
                    for f7_val in sorted(k for k in by_f7 if k is not None):
                        f2_val = f7_val & 0x3
                        for e in by_f7[f7_val]:
                            lines.append(f"        2'b{f2_val:02b}: begin // {e.name}")
                            for sig, val in sorted(e.signals.items()):
                                if val != DEFAULT_SIGNALS.get(sig):
                                    lines.append(f"          {sig:<30s} = {_sv_val(val)};")
                            lines.append("        end")
                    lines.append("        default: illegal_insn_o = 1'b1;")
                    lines.append("      endcase")
                    lines.append("    end")
                else:
                    # R-type: decode on funct7 (bits [31:25])
                    lines.append(f"    3'b{f3:03b}: begin")
                    lines.append("      unique case (instr_rdata_i[31:25])")
                    for f7_val in sorted(k for k in by_f7 if k is not None):
                        for e in by_f7[f7_val]:
                            lines.append(f"        7'b{f7_val:07b}: begin  // {e.name}")
                            for sig, val in sorted(e.signals.items()):
                                if val != DEFAULT_SIGNALS.get(sig):
                                    lines.append(f"          {sig:<30s} = {_sv_val(val)};")
                            lines.append("        end")
                    lines.append("        default: illegal_insn_o = 1'b1;")
                    lines.append("      endcase")
                    lines.append("    end")
            else:
                for e in f3_entries:
                    lines.append(f"    3'b{f3:03b}: begin  // {e.name}")
                    for sig, val in sorted(e.signals.items()):
                        if val != DEFAULT_SIGNALS.get(sig):
                            lines.append(f"      {sig:<30s} = {_sv_val(val)};")
                    lines.append("    end")
        lines.append("    default: illegal_insn_o = 1'b1;")
        lines.append("  endcase")
    lines.append("end")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────
# Opcode dispatch table
# ───────────────────────────────────────────────────────────────────

# Maps opcode value → specialised emitter function


def _emit_opcode_block(opcode: int, entries: List[InstructionEntry]) -> str:
    """Dispatch to the appropriate emitter for a given opcode."""
    name = OPCODE_NAMES.get(opcode, f"7'h{opcode:02x}")

    if opcode == OPCODE_LUI:
        return _emit_opcode_simple(name, entries)
    elif opcode == OPCODE_AUIPC:
        return _emit_opcode_simple(name, entries)
    elif opcode == OPCODE_JAL:
        return _emit_opcode_simple(name, entries)
    elif opcode == OPCODE_JALR:
        return _emit_opcode_with_funct3(name, entries, "Jump and Link Register")
    elif opcode == OPCODE_BRANCH:
        return _emit_opcode_branch(entries)
    elif opcode == OPCODE_LOAD:
        return _emit_opcode_load(entries)
    elif opcode == OPCODE_STORE:
        return _emit_opcode_store(entries)
    elif opcode == OPCODE_OPIMM:
        return _emit_opcode_opimm(entries)
    elif opcode == OPCODE_OP:
        return _emit_opcode_op(entries)
    elif opcode == OPCODE_FENCE:
        return _emit_opcode_fence(entries)
    elif opcode == OPCODE_SYSTEM:
        return _emit_opcode_system(entries)
    elif opcode in (OPCODE_CUSTOM_0, OPCODE_CUSTOM_1, OPCODE_CUSTOM_2, OPCODE_CUSTOM_3):
        return _emit_opcode_custom(name, entries)
    else:
        # Generic fallback
        return _emit_opcode_with_funct3(name, entries)


# ───────────────────────────────────────────────────────────────────
# Default block emitter
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────
# Deassert-we block emitter
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────
# Main generator — template-based approach
# ───────────────────────────────────────────────────────────────────


def _generate_case_body(
    used_instructions: Dict[str, InstructionEntry],
    custom_instructions: Optional[List[InstructionEntry]] = None,
) -> str:
    """Generate just the case blocks for the opcode switch."""
    db = dict(used_instructions)
    if custom_instructions:
        for entry in custom_instructions:
            db[entry.name] = entry

    by_opcode: Dict[int, List[InstructionEntry]] = defaultdict(list)
    for entry in db.values():
        by_opcode[entry.opcode].append(entry)

    case_blocks: List[str] = []
    for opcode in sorted(by_opcode.keys()):
        entries = by_opcode[opcode]
        block = _emit_opcode_block(opcode, entries)
        indented = _indent(block, 3)
        case_blocks.append(indented)

    return "\n\n".join(case_blocks)


def generate_decoder(
    used_instructions: Dict[str, InstructionEntry],
    custom_instructions: Optional[List[InstructionEntry]] = None,
    original_decoder_path: Optional[str] = None,
) -> str:
    """
    Generate a specialized ``cv32e40p_decoder.sv`` by replacing only
    the case body in the original decoder, preserving all signal
    declarations, default assignments, and deassert logic.

    Parameters
    ----------
    used_instructions : dict[str, InstructionEntry]
        The subset of ISA_DB entries to include.
    custom_instructions : list[InstructionEntry], optional
        Additional custom fused instructions.
    original_decoder_path : str, optional
        Path to original cv32e40p_decoder.sv. If provided, uses the
        template-based approach (recommended). Otherwise generates
        from scratch (may miss signals).

    Returns
    -------
    str
        Complete SystemVerilog source for the decoder module.
    """
    db = dict(used_instructions)
    if custom_instructions:
        for entry in custom_instructions:
            db[entry.name] = entry

    n_insn = len(db)
    insn_list = ", ".join(sorted(db.keys()))

    # Template-based: replace case body in original file
    if original_decoder_path:
        import re

        original = open(original_decoder_path).read()

        # Remove SYSTEM instructions from the generated set — the original
        # SYSTEM block has complex CSR address validation that must be preserved.
        from arvis.analysis.isa_db import OPCODE_SYSTEM as _OPCODE_SYSTEM

        non_system = {k: v for k, v in used_instructions.items() if v.opcode != _OPCODE_SYSTEM}
        case_body = _generate_case_body(non_system, custom_instructions)

        # Extract the original OPCODE_SYSTEM block from the template
        sys_match = re.search(r"(\n\s*)(OPCODE_SYSTEM:\s*begin\b)", original)
        if sys_match:
            sys_indent = sys_match.group(1)
            sys_start = sys_match.start() + len(sys_indent)
            # Find matching end by counting begin/end nesting
            # Start at depth=1 because we're inside the opening 'begin'
            sys_depth = 1
            sys_end = sys_match.end()
            for line in original[sys_match.end() :].split("\n"):
                sys_end += len(line) + 1
                sys_depth += line.count("begin") - line.count("end")
                sys_depth += line.count("endcase")
                if sys_depth <= 0:
                    break
            original_system = original[sys_start:sys_end].rstrip()
            case_body += "\n\n      " + original_system

        # Find the unique case block boundaries
        # Pattern: "unique case (instr_rdata_i[6:0])" ... "endcase"
        # We need to find the OUTER case (opcode level), not nested ones
        case_start_pattern = r"(\s*unique case \(instr_rdata_i\[6:0\]\))"
        case_start_match = re.search(case_start_pattern, original)
        if not case_start_match:
            raise ValueError("Could not find 'unique case (instr_rdata_i[6:0])' in original decoder")

        start_pos = case_start_match.start()
        search_from = case_start_match.end()

        # Find the end of the outer case by looking for the
        # "illegal_c_insn_i" check that immediately follows.
        # This is more reliable than counting case/endcase nesting.
        illegal_c_pattern = re.search(r"\n(\s*//.*\n)*\s*if\s*\(illegal_c_insn_i\)", original[search_from:])
        if illegal_c_pattern:
            # The endcase is just before the illegal_c_insn_i block
            # Walk backward from illegal_c_insn_i to find 'endcase'
            region_before = original[search_from : search_from + illegal_c_pattern.start()]
            last_endcase = region_before.rfind("endcase")
            if last_endcase >= 0:
                end_pos = search_from + last_endcase + len("endcase")
            else:
                end_pos = search_from + illegal_c_pattern.start()
        else:
            # Fallback: find the last endcase before 'end' of always_comb
            end_block = original.find("\n  end\n", search_from)
            if end_block > 0:
                region = original[search_from:end_block]
                last_ec = region.rfind("endcase")
                end_pos = search_from + last_ec + len("endcase") if last_ec >= 0 else end_block
            else:
                end_pos = len(original)

        # Build the replacement
        header_comment = f"    // AUTO-SPECIALIZED: {n_insn} instructions\n    // {insn_list}\n"
        new_case = (
            f"    unique case (instr_rdata_i[6:0])\n\n"
            f"{case_body}\n\n"
            f"      // ARVIS_FUSED_BEGIN: decoder_cases\n"
            f"      // ARVIS_FUSED_END: decoder_cases\n\n"
            f"      default: illegal_insn_o = 1'b1;\n"
            f"    endcase"
        )

        result = original[:start_pos] + header_comment + new_case + original[end_pos:]
        return result

    # Fallback: generate from scratch (less reliable)
    raise ValueError(
        "original_decoder_path is required. The template-based approach "
        "preserves all signal declarations and default assignments from "
        "the original decoder, ensuring correctness."
    )


# ───────────────────────────────────────────────────────────────────
# CLI entry point (for testing)
# ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    from arvis.analysis.isa_db import ISA_DB

    sv = generate_decoder(ISA_DB)
    if len(sys.argv) > 1:
        outpath = sys.argv[1]
        with open(outpath, "w") as f:
            f.write(sv)
        print(f"Wrote decoder to {outpath}")
    else:
        print(sv)
