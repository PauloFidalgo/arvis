#!/usr/bin/env python3
"""Apply PC_WIDTH narrowing to a single cv32e40p RTL tree.

USAGE
-----

    python3 apply_pc_width.py <rtl_root> <pc_width>

``<rtl_root>`` is a directory containing the cv32e40p .sv files
(top-level ``rtl/`` folder of any variant -- e.g.
``output/ud_specialized/rtl_pruned/rtl``).

``<pc_width>`` is the desired PC pipeline width in bits
(e.g. 13 for ud, 16 for kyber_all).

The script edits the SV files in place to:
  * Add ``parameter PC_WIDTH = <pc_width>`` to every relevant module.
  * Narrow pipeline-internal PC signals to ``[PC_WIDTH-1:0]``
    (pc_if/pc_id/pc_q/pc_n/pc_plus2/4/branch_addr_n/exc_pc/
    trans_addr_*/aligned_branch_addr/mepc_q/n/uepc_q/n/exception_pc).
  * Add zero-extends/truncations at every 32-bit boundary
    (OBI bus, regfile, CSR read/write, immediate adders for AUIPC and
    branch targets, BOOT_ADDR, jump_target_*).

Re-running on an already-patched tree is a no-op (idempotent).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple


# ─── Edit catalogue ──────────────────────────────────────────────────


def edits_for_top_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(\s+parameter HWLP_ADDR_WIDTH = \d+,\n)",
         r"\1    parameter PC_WIDTH = 32,\n"),
        ("      .COREV_PULP      (COREV_PULP),\n"
         "      .HW_LOOP         (HW_LOOP),\n"
         "      .COREV_CLUSTER   (COREV_CLUSTER),\n",
         "      .COREV_PULP      (COREV_PULP),\n"
         "      .HW_LOOP         (HW_LOOP),\n"
         "      .PC_WIDTH        (PC_WIDTH),\n"
         "      .COREV_CLUSTER   (COREV_CLUSTER),\n"),
    ]


def edits_for_core_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(    parameter HWLP_ADDR_WIDTH = \d+,(?:\s*//[^\n]*)?\n)",
         r"\1    parameter PC_WIDTH = 32,  // Program counter pipeline width (narrowed when binary fits)\n"),
        ("  logic [31:0] pc_if;  // Program counter in IF stage\n"
         "  logic [31:0] pc_id;  // Program counter in ID stage\n",
         "  logic [PC_WIDTH-1:0] pc_if;  // Program counter in IF stage\n"
         "  logic [PC_WIDTH-1:0] pc_id;  // Program counter in ID stage\n"),
        ("  logic [31:0] mepc, uepc;\n",
         "  logic [PC_WIDTH-1:0] mepc, uepc;\n"),
        # if_stage_i instantiation, two shapes
        ("  cv32e40p_if_stage #(\n"
         "      .HW_LOOP    (HW_LOOP),\n"
         "      .PULP_OBI   (PULP_OBI),\n",
         "  cv32e40p_if_stage #(\n"
         "      .HW_LOOP    (HW_LOOP),\n"
         "      .PC_WIDTH   (PC_WIDTH),\n"
         "      .HWLP_ADDR_WIDTH(HWLP_ADDR_WIDTH),\n"
         "      .PULP_OBI   (PULP_OBI),\n"),
        ("  cv32e40p_if_stage #(\n"
         "      .HW_LOOP    (HW_LOOP),\n"
         "      .HWLP_ADDR_WIDTH(HWLP_ADDR_WIDTH),\n"
         "      .PULP_OBI   (PULP_OBI),\n",
         "  cv32e40p_if_stage #(\n"
         "      .HW_LOOP    (HW_LOOP),\n"
         "      .PC_WIDTH   (PC_WIDTH),\n"
         "      .HWLP_ADDR_WIDTH(HWLP_ADDR_WIDTH),\n"
         "      .PULP_OBI   (PULP_OBI),\n"),
        # id_stage_i and cs_registers_i bindings
        ("      .COREV_PULP      (COREV_PULP),\n"
         "      .HW_LOOP         (HW_LOOP),\n"
         "      .COREV_CLUSTER   (COREV_CLUSTER),\n",
         "      .COREV_PULP      (COREV_PULP),\n"
         "      .HW_LOOP         (HW_LOOP),\n"
         "      .PC_WIDTH        (PC_WIDTH),\n"
         "      .COREV_CLUSTER   (COREV_CLUSTER),\n"),
        ("      .HW_LOOP         (HW_LOOP),\n"
         "      .A_EXTENSION     (A_EXTENSION),\n",
         "      .HW_LOOP         (HW_LOOP),\n"
         "      .PC_WIDTH        (PC_WIDTH),\n"
         "      .A_EXTENSION     (A_EXTENSION),\n"),
    ]


def edits_for_if_stage_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(    parameter HWLP_ADDR_WIDTH = \d+,(?:\s*//[^\n]*)?\n)",
         r"\1    parameter PC_WIDTH = 32,  // Program counter pipeline width\n"),
        ("    output logic [31:0] pc_if_o,\n"
         "    output logic [31:0] pc_id_o,\n",
         "    output logic [PC_WIDTH-1:0] pc_if_o,\n"
         "    output logic [PC_WIDTH-1:0] pc_id_o,\n"),
        ("    input logic [31:0] mepc_i,  // address used to restore PC when the interrupt/exception is served\n"
         "    input logic [31:0] uepc_i,  // address used to restore PC when the interrupt/exception is served\n",
         "    input logic [PC_WIDTH-1:0] mepc_i,  // address used to restore PC when the interrupt/exception is served\n"
         "    input logic [PC_WIDTH-1:0] uepc_i,  // address used to restore PC when the interrupt/exception is served\n"),
        ("  logic        branch_req;\n"
         "  logic [31:0] branch_addr_n;\n",
         "  logic        branch_req;\n"
         "  logic [PC_WIDTH-1:0] branch_addr_n;\n"),
        ("  logic [31:0] exc_pc;\n",
         "  logic [PC_WIDTH-1:0] exc_pc;\n"),
        # exc_pc assignment: truncate trap_base
        ("    unique case (exc_pc_mux_i)\n"
         "      EXC_PC_EXCEPTION:\n"
         "      exc_pc = {trap_base_addr, 8'h0};  //1.10 all the exceptions go to base address\n"
         "      default: exc_pc = {trap_base_addr, 8'h0};\n"
         "    endcase",
         "    unique case (exc_pc_mux_i)\n"
         "      EXC_PC_EXCEPTION:\n"
         "      exc_pc = {trap_base_addr, 8'h0}[PC_WIDTH-1:0];  //1.10 all the exceptions go to base address\n"
         "      default: exc_pc = {trap_base_addr, 8'h0}[PC_WIDTH-1:0];\n"
         "    endcase"),
        # branch_addr_n mux: truncate boot_addr_i / jump_target_*_i, narrow PC_FENCEI add
        ("  // fetch address selection\n"
         "  always_comb begin\n"
         "    // Default assign PC_BOOT (should be overwritten in below case)\n"
         "    branch_addr_n = {boot_addr_i[31:2], 2'b0};\n"
         "\n"
         "    unique case (pc_mux_i)\n"
         "      PC_BOOT: branch_addr_n = {boot_addr_i[31:2], 2'b0};\n"
         "      PC_JUMP: branch_addr_n = jump_target_id_i;\n"
         "      PC_BRANCH: branch_addr_n = jump_target_ex_i;\n"
         "      PC_EXCEPTION: branch_addr_n = exc_pc;  // set PC to exception handler\n"
         "      PC_MRET: branch_addr_n = mepc_i;  // PC is restored when returning from IRQ/exception\n"
         "      PC_URET: branch_addr_n = uepc_i;  // PC is restored when returning from IRQ/exception\n"
         "      PC_FENCEI: branch_addr_n = pc_id_o + 4;  // jump to next instr forces prefetch buffer reload\n"
         "      default: ;\n"
         "    endcase\n"
         "  end",
         "  // fetch address selection\n"
         "  always_comb begin\n"
         "    // Sources that come in 32-bit (boot_addr_i, jump_target_*_i)\n"
         "    // are truncated to PC_WIDTH; the binary text fits by\n"
         "    // construction (analyze_addr_width() in the pipeline).\n"
         "    branch_addr_n = {boot_addr_i[31:2], 2'b0}[PC_WIDTH-1:0];\n"
         "\n"
         "    unique case (pc_mux_i)\n"
         "      PC_BOOT: branch_addr_n = {boot_addr_i[31:2], 2'b0}[PC_WIDTH-1:0];\n"
         "      PC_JUMP: branch_addr_n = jump_target_id_i[PC_WIDTH-1:0];\n"
         "      PC_BRANCH: branch_addr_n = jump_target_ex_i[PC_WIDTH-1:0];\n"
         "      PC_EXCEPTION: branch_addr_n = exc_pc;\n"
         "      PC_MRET: branch_addr_n = mepc_i;\n"
         "      PC_URET: branch_addr_n = uepc_i;\n"
         "      PC_FENCEI: branch_addr_n = pc_id_o + PC_WIDTH'(4);\n"
         "      default: ;\n"
         "    endcase\n"
         "  end"),
        # prefetch_buffer instantiation: parameter binding + branch_addr index
        (r"RE:(\.HWLP_ADDR_WIDTH\(HWLP_ADDR_WIDTH\))\n(  \) prefetch_buffer_i \()",
         r"\1,\n      .PC_WIDTH  (PC_WIDTH)\n\2"),
        ("      .branch_addr_i({branch_addr_n[31:1], 1'b0}),\n",
         "      .branch_addr_i({branch_addr_n[PC_WIDTH-1:1], 1'b0}),\n"),
        # aligner instantiation
        ("  cv32e40p_aligner aligner_i (",
         "  cv32e40p_aligner #(\n      .PC_WIDTH(PC_WIDTH)\n  ) aligner_i ("),
        ("      .branch_addr_i   ({branch_addr_n[31:1], 1'b0}),\n",
         "      .branch_addr_i   ({branch_addr_n[PC_WIDTH-1:1], 1'b0}),\n"),
    ]


def edits_for_aligner_sv() -> List[Tuple[str, str]]:
    return [
        # Module-decl shape A: bare ``module cv32e40p_aligner (`` (no params)
        ("module cv32e40p_aligner (",
         "module cv32e40p_aligner #(\n    parameter PC_WIDTH = 32\n) ("),
        # Module-decl shape B: existing param block ending in HWLP_ADDR_WIDTH = N
        (r"RE:(module cv32e40p_aligner #\(\n[^)]*parameter HWLP_ADDR_WIDTH = \d+)\n(\) \()",
         r"\1,\nparameter PC_WIDTH = 32\n\2"),
        ("    input logic [31:0] branch_addr_i,\n"
         "    input logic        branch_i,  // Asserted if we are branching/jumping now\n"
         "    output logic [31:0] pc_o\n",
         "    input logic [PC_WIDTH-1:0] branch_addr_i,\n"
         "    input logic        branch_i,  // Asserted if we are branching/jumping now\n"
         "    output logic [PC_WIDTH-1:0] pc_o\n"),
        ("  logic [15:0] r_instr_h;\n"
         "  logic [31:0] pc_q, pc_n;\n"
         "  logic update_state;\n"
         "  logic [31:0] pc_plus4, pc_plus2;\n"
         "  logic aligner_ready_q;\n"
         "  assign pc_o     = pc_q;\n"
         "\n"
         "  assign pc_plus2 = pc_q + 2;\n"
         "  assign pc_plus4 = pc_q + 4;\n",
         "  logic [15:0] r_instr_h;\n"
         "  logic [PC_WIDTH-1:0] pc_q, pc_n;\n"
         "  logic update_state;\n"
         "  logic [PC_WIDTH-1:0] pc_plus4, pc_plus2;\n"
         "  logic aligner_ready_q;\n"
         "  assign pc_o     = pc_q;\n"
         "\n"
         "  assign pc_plus2 = pc_q + PC_WIDTH'(2);\n"
         "  assign pc_plus4 = pc_q + PC_WIDTH'(4);\n"),
    ]


def edits_for_prefetch_buffer_sv() -> List[Tuple[str, str]]:
    return [
        # Add PC_WIDTH after HWLP_ADDR_WIDTH (last param, no trailing comma)
        (r"RE:(    parameter HWLP_ADDR_WIDTH = \d+)\n",
         r"\1,\n    parameter PC_WIDTH = 32  // Program counter pipeline width\n"),
        ("    input logic [31:0] branch_addr_i,\n",
         "    input logic [PC_WIDTH-1:0] branch_addr_i,\n"),
        # Add PC_WIDTH binding to prefetch_controller instantiation
        (r"RE:(\.HWLP_ADDR_WIDTH\(HWLP_ADDR_WIDTH\))\n(  \) prefetch_controller_i \()",
         r"\1,\n      .PC_WIDTH  (PC_WIDTH)\n\2"),
    ]


def edits_for_prefetch_controller_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(    parameter HWLP_ADDR_WIDTH = \d+,(?:\s*//[^\n]*)?\n)(    parameter DEPTH)",
         r"\1    parameter PC_WIDTH = 32,  // Program counter pipeline width\n\2"),
        ("    input  logic [31:0] branch_addr_i,  // Taken branch address (only valid when branch_i = 1)\n",
         "    input  logic [PC_WIDTH-1:0] branch_addr_i,  // Taken branch address (only valid when branch_i = 1)\n"),
        # trans_addr_q/incr/aligned_branch declarations + add trans_addr_int
        ("  // Transaction address\n"
         "  logic [31:0] trans_addr_q, trans_addr_incr;\n"
         "\n"
         "  // Word-aligned branch target address\n"
         "  logic [31:0] aligned_branch_addr;  // Word aligned branch target address\n",
         "  // Transaction address\n"
         "  logic [PC_WIDTH-1:0] trans_addr_q, trans_addr_incr;\n"
         "  // Internal narrow trans_addr; zero-extended to 32-bit for the OBI bus boundary at the bottom of the module.\n"
         "  logic [PC_WIDTH-1:0] trans_addr_int;\n"
         "\n"
         "  // Word-aligned branch target address\n"
         "  logic [PC_WIDTH-1:0] aligned_branch_addr;  // Word aligned branch target address\n"),
        # aligned_branch_addr / trans_addr_incr assignments
        ("  // Prefetcher will only perform word fetches\n"
         "  assign aligned_branch_addr = {branch_addr_i[31:2], 2'b00};\n"
         "\n"
         "  // Increment address (always word fetch)\n"
         "  assign trans_addr_incr = {trans_addr_q[31:2], 2'b00} + 32'd4;\n",
         "  // Prefetcher will only perform word fetches\n"
         "  assign aligned_branch_addr = {branch_addr_i[PC_WIDTH-1:2], 2'b00};\n"
         "\n"
         "  // Increment address (always word fetch)\n"
         "  assign trans_addr_incr = {trans_addr_q[PC_WIDTH-1:2], 2'b00} + PC_WIDTH'(4);\n"),
        # FSM combinational: route trans_addr through trans_addr_int
        ("  // FSM (state_q, next_state) to control OBI A channel signals.\n"
         "  always_comb begin\n"
         "    next_state   = state_q;\n"
         "    trans_addr_o = trans_addr_q;\n",
         "  // FSM (state_q, next_state) to control OBI A channel signals.\n"
         "  always_comb begin\n"
         "    next_state   = state_q;\n"
         "    trans_addr_int = trans_addr_q;\n"),
        ("            trans_addr_o = aligned_branch_addr;\n"
         "          end \n"
         "          else begin\n"
         "            trans_addr_o = trans_addr_incr;\n",
         "            trans_addr_int = aligned_branch_addr;\n"
         "          end \n"
         "          else begin\n"
         "            trans_addr_int = trans_addr_incr;\n"),
        ("        trans_addr_o = branch_i ? aligned_branch_addr : trans_addr_q;\n",
         "        trans_addr_int = branch_i ? aligned_branch_addr : trans_addr_q;\n"),
        # always_ff: trans_addr_q <= trans_addr_int + zero-extend trans_addr_o at bus boundary
        ("      if (branch_i || (trans_valid_o && trans_ready_i)) begin\n"
         "        trans_addr_q <= trans_addr_o;\n"
         "      end\n"
         "    end\n"
         "  end\n"
         "\n"
         "endmodule  // cv32e40p_prefetch_controller\n",
         "      if (branch_i || (trans_valid_o && trans_ready_i)) begin\n"
         "        trans_addr_q <= trans_addr_int;\n"
         "      end\n"
         "    end\n"
         "  end\n"
         "\n"
         "  // Zero-extend internal narrow trans_addr to the 32-bit OBI bus width.\n"
         "  assign trans_addr_o = {{(32 - PC_WIDTH){1'b0}}, trans_addr_int};\n"
         "\n"
         "endmodule  // cv32e40p_prefetch_controller\n"),
    ]


def edits_for_id_stage_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(    parameter HWLP_ADDR_WIDTH = \d+,(?:\s*//[^\n]*)?\n)",
         r"\1    parameter PC_WIDTH = 32,\n"),
        ("    input logic [31:0] pc_id_i,\n",
         "    input logic [PC_WIDTH-1:0] pc_id_i,\n"),
        ("    output logic [31:0] pc_ex_o,\n",
         "    output logic [PC_WIDTH-1:0] pc_ex_o,\n"),
        # JT_JAL / JT_COND adders zero-extend pc_id_i
        ("  always_comb begin : jump_target_mux\n"
         "    unique case (ctrl_transfer_target_mux_sel)\n"
         "      JT_JAL:  jump_target = pc_id_i + imm_uj_type;\n"
         "      JT_COND: jump_target = pc_id_i + imm_sb_type;\n",
         "  always_comb begin : jump_target_mux\n"
         "    unique case (ctrl_transfer_target_mux_sel)\n"
         "      // pc_id_i is PC_WIDTH-bit; zero-extend before adding to 32-bit immediates.\n"
         "      JT_JAL:  jump_target = {{(32-PC_WIDTH){1'b0}}, pc_id_i} + imm_uj_type;\n"
         "      JT_COND: jump_target = {{(32-PC_WIDTH){1'b0}}, pc_id_i} + imm_sb_type;\n"),
        # AUIPC operand a
        ("      OP_A_CURRPC:      alu_operand_a = pc_id_i;\n",
         "      OP_A_CURRPC:      alu_operand_a = {{(32-PC_WIDTH){1'b0}}, pc_id_i};\n"),
        # controller_i instantiation
        ("      .COREV_CLUSTER(COREV_CLUSTER),\n"
         "      .HW_LOOP   (HW_LOOP),\n"
         "      .FPU          (FPU)\n"
         "  ) controller_i (\n",
         "      .COREV_CLUSTER(COREV_CLUSTER),\n"
         "      .HW_LOOP   (HW_LOOP),\n"
         "      .PC_WIDTH  (PC_WIDTH),\n"
         "      .FPU          (FPU)\n"
         "  ) controller_i (\n"),
    ]


def edits_for_controller_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(  parameter HWLP_ADDR_WIDTH = \d+,(?:\s*//[^\n]*)?\n)",
         r"\1  parameter PC_WIDTH = 32,\n"),
        ("  input  logic [31:0]       pc_id_i,\n",
         "  input  logic [PC_WIDTH-1:0]       pc_id_i,\n"),
    ]


def edits_for_cs_registers_sv() -> List[Tuple[str, str]]:
    return [
        (r"RE:(    parameter HW_LOOP\s+= \d+,\n)(    parameter APU)",
         r"\1    parameter PC_WIDTH          = 32,\n\2"),
        ("    output logic [31:0] mepc_o,\n"
         "    output logic [31:0] uepc_o,\n",
         "    output logic [PC_WIDTH-1:0] mepc_o,\n"
         "    output logic [PC_WIDTH-1:0] uepc_o,\n"),
        ("    input logic [31:0] pc_if_i,\n"
         "    input logic [31:0] pc_id_i,\n"
         "    input logic [31:0] pc_ex_i,\n",
         "    input logic [PC_WIDTH-1:0] pc_if_i,\n"
         "    input logic [PC_WIDTH-1:0] pc_id_i,\n"
         "    input logic [PC_WIDTH-1:0] pc_ex_i,\n"),
        ("  // Interrupt control signals\n"
         "  logic [31:0] mepc_q, mepc_n;\n"
         "  logic [31:0] uepc_q, uepc_n;\n",
         "  // Interrupt control signals\n"
         "  logic [PC_WIDTH-1:0] mepc_q, mepc_n;\n"
         "  logic [PC_WIDTH-1:0] uepc_q, uepc_n;\n"),
        ("  logic [31:0] exception_pc;\n",
         "  logic [PC_WIDTH-1:0] exception_pc;\n"),
        # CSR write to mepc: narrow (replace_all -- M-mode + U-mode blocks)
        ("          mepc_n = csr_wdata_int & ~32'b1;  // force 16-bit alignment\n",
         "          mepc_n = csr_wdata_int[PC_WIDTH-1:0] & ~PC_WIDTH'(1);  // force 16-bit alignment, narrowed to PC_WIDTH\n"),
    ]


EDITS_BY_FILE = {
    "cv32e40p_top.sv":               edits_for_top_sv,
    "cv32e40p_core.sv":              edits_for_core_sv,
    "cv32e40p_if_stage.sv":          edits_for_if_stage_sv,
    "cv32e40p_aligner.sv":           edits_for_aligner_sv,
    "cv32e40p_prefetch_buffer.sv":   edits_for_prefetch_buffer_sv,
    "cv32e40p_prefetch_controller.sv": edits_for_prefetch_controller_sv,
    "cv32e40p_id_stage.sv":          edits_for_id_stage_sv,
    "cv32e40p_controller.sv":        edits_for_controller_sv,
    "cv32e40p_cs_registers.sv":      edits_for_cs_registers_sv,
}


# ─── Engine ──────────────────────────────────────────────────────────


def _extract_marker(find: str, replace: str) -> str:
    """Return a short distinctive 'PC_WIDTH-bearing' fragment that
    appears in the replacement but NOT in the find pattern.  Used as
    a per-edit idempotency skip marker.

    The literal width value is stripped (any digit sequence after
    '=' becomes 'N') so a 32-vs-13 mismatch doesn't defeat the marker
    after ``set_pc_width_value`` rewrites the parameter literal.
    """
    repl = re.sub(r"\\[1-9]", "", replace)
    repl = repl.replace("\\n", "\n").replace("\\t", "\t")
    find_lines = set(find.split("\n")) if not find.startswith("RE:") else set()
    for line in repl.split("\n"):
        if "PC_WIDTH" not in line or not line.strip() or line in find_lines:
            continue
        return re.sub(r"=\s*\d+", "= N", line.strip())
    return ""


def _apply_edits(path: Path, edits: List[Tuple[str, str]]) -> Tuple[int, int]:
    text = path.read_text()
    text_norm = re.sub(r"=\s*\d+", "= N", text)
    applied = 0
    skipped = 0
    for find_str, replace_str in edits:
        marker = _extract_marker(find_str, replace_str)
        if marker and marker in text_norm:
            skipped += 1
            continue
        if find_str.startswith("RE:"):
            new_text, n = re.subn(find_str[3:], replace_str, text,
                                  count=0, flags=re.MULTILINE | re.DOTALL)
            if n == 0:
                skipped += 1
                continue
            text = new_text
            applied += 1
            continue
        if find_str not in text:
            skipped += 1
            continue
        # mepc_n CSR write occurs in M-mode and U-mode blocks; replace all
        if "mepc_n = csr_wdata_int & ~32'b1" in find_str:
            text = text.replace(find_str, replace_str)
        else:
            text = text.replace(find_str, replace_str, 1)
        applied += 1
    path.write_text(text)
    return applied, skipped


def _cleanup_corruption(rtl_dir: Path) -> int:
    """Collapse duplicate ``parameter PC_WIDTH = N,`` lines and
    duplicate ``.PC_WIDTH(PC_WIDTH),`` / ``.HWLP_ADDR_WIDTH(HWLP_ADDR_WIDTH),``
    instance bindings that previous broken iterations may have left."""
    fixed = 0
    for sv in rtl_dir.glob("cv32e40p_*.sv"):
        text = sv.read_text()
        original = text
        text = re.sub(
            r"((?:\s+parameter PC_WIDTH = \d+,(?:\s*//[^\n]*)?\n)){2,}",
            lambda m: m.group(0).split('\n')[0] + '\n', text)
        text = re.sub(r"(\s+\.PC_WIDTH\s*\(PC_WIDTH\),\n)\1+", r"\1", text)
        text = re.sub(r"(\s+\.HWLP_ADDR_WIDTH\s*\(HWLP_ADDR_WIDTH\),\n)\1+",
                      r"\1", text)
        text = re.sub(
            r"(\s+\.PC_WIDTH\s*\(PC_WIDTH\),\n\s+\.HWLP_ADDR_WIDTH\s*\(HWLP_ADDR_WIDTH\),\n)\1+",
            r"\1", text)
        if text != original:
            sv.write_text(text)
            fixed += 1
    return fixed


def analyze_pc_width(elf_path: str, margin_bits: int = 1) -> int:
    """Determine minimum PC_WIDTH for a compiled binary.

    The main pipeline PC pipeline (pc_if/pc_id/pc_q/branch_addr_n/mepc/
    uepc) only needs to hold addresses inside the binary's
    executable region. Narrowing it saves FFs throughout the pipeline
    (PC stages in IF/ID/EX, aligner pc_q, prefetch buffer FIFO, the
    PC-relative adders for branches and AUIPC, and CSR storage for
    mepc/uepc) and shortens the PC-comparison CARRY chains in the
    aligner and prefetch_controller.

    Identical strategy to ``analyze_addr_width``: read the ELF, find
    the highest LMA + size among executable sections, return
    ``clog2(max_addr + 1) + margin``. The margin guards against
    relocation rounding or compiler tweaks that produce slightly
    higher addresses on a re-build.

    Args:
        elf_path: Path to a compiled ELF that targets the same memory
            layout as the synthesis target.
        margin_bits: Extra bits beyond the strictly-required width.

    Returns:
        Optimal PC_WIDTH (minimum 8 — needed for the reset vector
        BOOT_ADDR=0x80 in the standard testbench, maximum 32 — disables
        narrowing).
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ["riscv32-unknown-elf-objdump", "-h", elf_path],
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return 32  # conservative fallback if objdump is missing

    max_end = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            size = int(parts[2], 16)
            lma = int(parts[3], 16)
        except ValueError:
            continue
        name = parts[1]
        if not (name.startswith(".text") or name in (".init", ".fini", ".plt")):
            continue
        end = lma + size
        if end > max_end:
            max_end = end

    if max_end <= 0:
        return 32

    width = max_end.bit_length() + margin_bits
    return max(8, min(32, width))


def _set_pc_width_value(rtl_dir: Path, pc_width: int) -> None:
    """Override ``parameter PC_WIDTH = ...,`` in cv32e40p_top.sv.
    Submodule defaults stay at 32 since the value flows down via
    parameter bindings."""
    top_sv = rtl_dir / "cv32e40p_top.sv"
    if not top_sv.exists():
        return
    text = top_sv.read_text()
    new_text = re.sub(r"parameter PC_WIDTH = \d+,",
                      f"parameter PC_WIDTH = {pc_width},", text, count=1)
    if new_text != text:
        top_sv.write_text(new_text)


def patch_rtl_dir(rtl_dir: Path, pc_width: int) -> None:
    if not rtl_dir.is_dir():
        sys.exit(f"ERROR: {rtl_dir} is not a directory")
    cleaned = _cleanup_corruption(rtl_dir)
    if cleaned:
        print(f"  cleaned duplicates in {cleaned} files")
    for sv_name, edit_fn in EDITS_BY_FILE.items():
        path = rtl_dir / sv_name
        if not path.exists():
            print(f"  {sv_name}: skipped (file missing)")
            continue
        applied, skipped = _apply_edits(path, edit_fn())
        if applied:
            print(f"  {sv_name}: {applied} edits applied, {skipped} skipped")
    _set_pc_width_value(rtl_dir, pc_width)
    print(f"  PC_WIDTH = {pc_width} set in cv32e40p_top.sv")


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("USAGE: apply_pc_width.py <rtl_root> <pc_width>", file=sys.stderr)
        print("", file=sys.stderr)
        print("  <rtl_root>  Path to a cv32e40p RTL directory (containing", file=sys.stderr)
        print("              cv32e40p_top.sv, cv32e40p_core.sv, ...).", file=sys.stderr)
        print("  <pc_width>  Desired PC width in bits (e.g. 13, 16).", file=sys.stderr)
        print("", file=sys.stderr)
        print("Example:", file=sys.stderr)
        print("  apply_pc_width.py output/ud_specialized/rtl_pruned/rtl 13", file=sys.stderr)
        return 2

    rtl_root = Path(argv[0])
    try:
        pc_width = int(argv[1])
    except ValueError:
        print(f"ERROR: pc_width must be an integer, got {argv[1]!r}",
              file=sys.stderr)
        return 1
    if not (8 <= pc_width <= 32):
        print(f"ERROR: pc_width {pc_width} out of range [8, 32]",
              file=sys.stderr)
        return 1

    print(f"Applying PC_WIDTH={pc_width} to {rtl_root}")
    patch_rtl_dir(rtl_root, pc_width)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
