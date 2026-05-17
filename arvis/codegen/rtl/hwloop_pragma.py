"""
HWLOOP Pragma Processor.

Processes ARVIS_HWLP_BEGIN/END pragmas in the original RTL files.
Three pragma types:
  - REMOVE   — always deleted (PULP-only artifacts)
  - KEEP     — deleted if hw_loop=0, kept as-is if hw_loop>0
  - <name>   — deleted if hw_loop=0, replaced with generated code if hw_loop>0

Usage:
    from arvis.codegen.rtl.hwloop_pragma import HWLoopPragmaProcessor

    proc = HWLoopPragmaProcessor(hw_loop=2)
    for sv_file in rtl_dir.glob("*.sv"):
        text = sv_file.read_text()
        text = proc.process(text)
        sv_file.write_text(text)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List

# Pragma markers
PRAGMA_BEGIN = "// ARVIS_HWLP_BEGIN:"
PRAGMA_END = "// ARVIS_HWLP_END:"

# Regex to match a pragma block (including markers)
PRAGMA_PATTERN = re.compile(
    r"^([ \t]*)// ARVIS_HWLP_BEGIN:\s*(\w+)\s*\n"
    r"(.*?)"
    r"^[ \t]*// ARVIS_HWLP_END:\s*\2\s*\n",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class PragmaStats:
    """Statistics from pragma processing."""

    file: str = ""
    removed: int = 0
    kept: int = 0
    replaced: int = 0
    unknown: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.removed + self.kept + self.replaced

    def summary(self) -> str:
        parts = []
        if self.removed:
            parts.append(f"{self.removed} removed")
        if self.kept:
            parts.append(f"{self.kept} kept")
        if self.replaced:
            parts.append(f"{self.replaced} replaced")
        if self.unknown:
            parts.append(f"{len(self.unknown)} unknown: {self.unknown}")
        return f"{self.file}: {', '.join(parts)}" if parts else f"{self.file}: no pragmas"


class HWLoopPragmaProcessor:
    """Process ARVIS_HWLP pragmas in SystemVerilog files.

    Args:
        hw_loop: Number of hardware loop levels (0 = disabled, 2-8 = active)
    """

    def __init__(self, hw_loop: int = 0, hwlp_encoding=None):
        self.hw_loop = hw_loop
        self.hwlp_encoding = hwlp_encoding
        self._generators: Dict[str, Callable[[int, str], str]] = {}
        self._register_generators()

    def _register_generators(self) -> None:
        """Register replacement generators for named pragmas."""
        # Aligner
        # self._generators["aligner_misalign"] = _gen_aligner_misalign

        # Controller
        self._generators["controller_params"] = _gen_controller_params
        self._generators["controller_ports"] = _gen_controller_ports
        self._generators["controller_logic_declaration"] = _gen_controller_signals
        self._generators["controller_target_addr_o_declaration"] = _gen_controller_target_addr
        self._generators["controller_dec_fire"] = _gen_controller_dec_fire
        self._generators["controller_decode_default_hwlp"] = _gen_controller_decode_default_hwlp
        self._generators["controller_decode_hwloop_default"] = _gen_controller_decode_hwloop_default
        self._generators["controller_flush_hwlp"] = _gen_controller_flush_hwlp
        self._generators["controller_gen_hwlp"] = _gen_controller_gen_hwlp
        self._generators["controller_decode_hwloop_entry"] = _gen_controller_decode_hwloop_entry
        self._generators["controller_decode_hwloop_exit"] = _gen_controller_decode_hwloop_exit
        self._generators["controller_decode_hwloop_defaults"] = _gen_controller_decode_hwloop_defaults
        self._generators["controller_global_innermost_dec"] = _gen_controller_innermost_dec

        # Decoder
        self._generators["decoder_output"] = _gen_decoder_output
        self._generators["decoder_hwlp_we_declaration"] = _gen_decoder_we_declaration
        self._generators["decoder_signals_init"] = _gen_decoder_signals_init
        self._generators["decoder_assert_hwlp_we_o"] = _gen_decoder_assert_hwlp_we

        # ID stage
        self._generators["id_stage_output"] = _gen_id_ports
        self._generators["id_stage_logic_declaration"] = _gen_id_wires
        self._generators["id_stage_controller_instantiation"] = _gen_id_controller_conn
        self._generators["id_stage_hw_regs_instantiation"] = _gen_id_hw_regs_instantiation
        self._generators["id_stage_regid_assign"] = _gen_id_regid_assign
        self._generators["id_stage_mux_logic"] = _gen_id_mux_logic
        self._generators["id_stage_we_masked"] = _gen_id_we_masked

        # Core
        self._generators["core_signal_declaration"] = _gen_core_signal_declaration
        self._generators["core_id_instantiation"] = _gen_core_id_instantiation
        self._generators["core_if_hwlp_connections"] = _gen_core_if_hwlp_connections

        # Aligner
        self._generators["aligner_params"] = _gen_aligner_params
        self._generators["aligner_ports"] = _gen_aligner_ports
        self._generators["aligner_pc_wrap"] = _gen_aligner_pc_wrap

        # IF stage
        self._generators["if_ports"] = _gen_if_ports
        self._generators["if_pc_mux"] = _gen_if_pc_mux
        self._generators["if_prefetch_conn"] = _gen_if_prefetch_conn
        self._generators["if_aligner_instantiation"] = _gen_if_aligner_instantiation
        self._generators["if_aligner_hwlp_connections"] = _gen_if_aligner_hwlp_connections

        # Prefetch controller
        self._generators["prefetch_ports"] = _gen_prefetch_ports
        self._generators["prefetch_proactive_wrap"] = _gen_prefetch_proactive_wrap
        self._generators["prefetch_idle_addr"] = _gen_prefetch_idle_addr
        self._generators["prefetch_fifo_cnt_masked"] = _gen_prefetch_fifo_cnt_masked
        self._generators["prefetch_idle_case_if"] = _gen_prefetch_idle_case_if
        self._generators["prefetch_controller_flush_chain"] = _gen_prefetch_controller_flush_chain
        self._generators["prefetch_controller_registers_if"] = _gen_prefetch_controller_registers_if
        self._generators["prefetch_gen_hwlp"] = _gen_prefetch_gen_hwlp

        # Prefetch buffer
        self._generators["prefetch_buf_ports"] = _gen_prefetch_buf_ports
        self._generators["prefetch_buf_conn"] = _gen_prefetch_buf_conn

        # CS registers
        self._generators["csr_params"] = _gen_csr_params
        self._generators["csr_ports"] = _gen_csr_ports
        self._generators["csr_hwlp_read"] = _gen_csr_hwlp_read

    def process(self, text: str, filename: str = "") -> tuple[str, PragmaStats]:
        """Process all ARVIS_HWLP pragmas in a file.

        Handles nested pragmas: processes innermost first, then outer ones.
        When an outer pragma deletes content (hw_loop=0), inner pragmas
        are automatically removed with it.

        Returns (processed_text, stats).
        """
        stats = PragmaStats(file=filename)

        # Process iteratively until no more pragmas remain
        # This handles nesting: inner pragmas are processed first
        max_iterations = 20  # safety limit
        for _ in range(max_iterations):
            found = False

            def _replace_match(m: re.Match) -> str:
                nonlocal found
                found = True
                indent = m.group(1)
                tag = m.group(2)
                original_content = m.group(3)

                if tag == "REMOVE":
                    stats.removed += 1
                    return ""

                if tag == "KEEP":
                    if self.hw_loop == 0:
                        stats.removed += 1
                        return ""
                    else:
                        stats.kept += 1
                        # Keep the content without pragma markers
                        return original_content

                # Named pragma — always call the generator (it decides what to emit)
                gen_fn = self._generators.get(tag)
                if gen_fn is None:
                    if self.hw_loop == 0:
                        stats.removed += 1
                        return ""
                    stats.unknown.append(tag)
                    return original_content

                replacement = gen_fn(self.hw_loop, indent, hwlp_encoding=self.hwlp_encoding)
                if replacement:
                    stats.replaced += 1
                else:
                    stats.removed += 1
                return replacement

            # Use a non-greedy pattern that matches innermost pragmas first
            # by requiring no nested BEGIN markers in the content
            inner_pattern = re.compile(
                r"^([ \t]*)// ARVIS_HWLP_BEGIN:\s*(\w+)\s*\n"
                r"((?:(?!// ARVIS_HWLP_BEGIN:).)*?)"
                r"^[ \t]*// ARVIS_HWLP_END:\s*\2\s*\n",
                re.MULTILINE | re.DOTALL,
            )
            text = inner_pattern.sub(_replace_match, text)

            if not found:
                break

        return text, stats

    def process_file(self, filepath: Path) -> PragmaStats:
        """Process pragmas in a file in-place."""
        text = filepath.read_text()
        text, stats = self.process(text, filepath.name)
        filepath.write_text(text)
        return stats

    def process_dir(self, rtl_dir: Path) -> List[PragmaStats]:
        """Process all .sv files in a directory."""
        all_stats = []
        for sv_file in sorted(rtl_dir.glob("*.sv")):
            text = sv_file.read_text()
            if PRAGMA_BEGIN not in text:
                continue
            text, stats = self.process(text, sv_file.name)
            sv_file.write_text(text)
            all_stats.append(stats)
        return all_stats


# ═══════════════════════════════════════════════════════════════════════════
# Replacement generators
# Each function takes (hw_loop: int, indent: str) and returns SystemVerilog
# ═══════════════════════════════════════════════════════════════════════════

# Removed Paulo
"""
def _gen_aligner_misalign(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}// ARVIS: handle hwloop jump-back from this state\n"
        f"{indent}if (hwlp_update_pc_i || hwlp_update_pc_q) begin\n"
        f"{indent}  pc_n = hwlp_update_pc_i ? hwlp_addr_i : hwlp_addr_q;\n"
        #f"{indent}  next_state = (hwlp_update_pc_i ? hwlp_addr_i[1] : hwlp_addr_q[1]) ? BRANCH_MISALIGNED : ALIGNED32;\n" # Remo
        f"{indent}end\n"
    )
"""

# ── Controller generators ────────────────────────────────────────────────


def _gen_controller_params(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return f"{indent}parameter HW_LOOP = 0,\n"


def _gen_controller_ports(hw_loop: int, indent: str, **kw) -> str:
    """Controller hwlp ports (only the ones NOT in KEEP blocks).

    hwlp_mask_o, hwlp_jump_o, hwlp_targ_addr_o are in separate KEEP pragmas.
    """
    if hw_loop == 0:
        return ""
    return (
        f"{indent}// ARVIS: compressed instruction awareness\n"
        f"{indent}input  logic        is_compressed_i,\n"
        f"\n"
        f"{indent}// from hwloop_regs\n"
        f"{indent}input  logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0] [31:0] hwlp_start_addr_i,\n"
        f"{indent}input  logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0] [31:0] hwlp_end_addr_i,\n"
        f"{indent}input  logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0] [31:0] hwlp_counter_i,\n"
        f"{indent}input  logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0] [31:0] hwlp_pnult_addr_i,\n"
        f"\n"
        f"{indent}// to hwloop_regs\n"
        f"{indent}output logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0]        hwlp_dec_cnt_o,\n"
    )


def _gen_controller_signals(hw_loop: int, indent: str, **kw) -> str:
    """Controller signal declarations (is_hwlp_body is in a KEEP pragma)."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}logic [HW_LOOP-1:0] hwlp_end_eq_pc;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_counter_gt_1;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_counter_eq_1;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_counter_eq_0;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_end_eq_pc_plus4;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_start_leq_pc;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_end_geq_pc;\n"
        f"\n"
        f"{indent}// Auxiliary signals to make hwlp_jump_o last only one cycle\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_end_4_id_d, hwlp_end_4_id_q;\n"
        f"\n"
        f"{indent}logic hwlp_post_branch_q, hwlp_post_branch_d;\n"
        f"\n"
        f"{indent}// ARVIS: deferred hwloop DEC — set when a conditional branch at LP_end\n"
        f"{indent}// defers the decrement until the branch resolves.\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_dec_deferred_q;\n"
    )


def _gen_controller_target_addr(hw_loop: int, indent: str, **kw) -> str:
    """Target address priority encoder (innermost active loop wins)."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}// ARVIS: target address priority encoder\n"
        f"{indent}begin\n"
        f"{indent}    hwlp_targ_addr_o = '0;\n"
        f"{indent}    for (int i = 0; i < HW_LOOP; i++)\n"
        f"{indent}        if (hwlp_end_eq_pc[i] || (hwlp_start_leq_pc[i] && hwlp_end_geq_pc[i]))\n"
        f"{indent}            hwlp_targ_addr_o = hwlp_start_addr_i[i];\n"
        f"{indent}end\n"
    )


def _gen_controller_dec_fire(hw_loop: int, indent: str, **kw) -> str:
    """hwlp_dec_cnt for csr_status in DECODE and DECODE_HWLOOP."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{indent}  if (hwlp_end_eq_pc[i]) hwlp_dec_cnt_o[i] = 1'b1;\n"
        f"{indent}end\n"
    )


def _gen_controller_decode_hwloop_defaults(hw_loop: int, indent: str, **kw) -> str:
    """ARVIS-only default assignments in DECODE_HWLOOP defaults block."""
    if hw_loop == 0:
        return ""
    return f"{indent}hwlp_post_branch_d      = hwlp_post_branch_q;\n"


def _gen_controller_decode_hwloop_entry(hw_loop: int, indent: str, **kw) -> str:
    """Counter decrement guards + branch handling at DECODE_HWLOOP entry."""
    if hw_loop == 0:
        return ""
    I = indent
    return (
        f"{I}// Counter decrement: DEC all matching loops. The aligner handles\n"
        f"{I}if (instr_valid_i && !branch_taken_ex_i && 1'b1) begin\n"
        f"{I}  for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}    if (!hwlp_end_4_id_q[i] && hwlp_end_eq_pc[i] && !hwlp_counter_eq_0[i]) begin\n"
        f"{I}      if (!branch_in_id) begin\n"
        f"{I}        hwlp_dec_cnt_o[i] = 1'b1;\n"
        f"{I}        hwlp_end_4_id_d[i] = 1'b1;\n"
        f"{I}      end else begin\n"
        f"{I}        hwlp_end_4_id_d[i] = 1'b1;\n"
        f"{I}      end\n"
        f"{I}    end\n"
        f"{I}  end\n"
        f"{I}end\n"
        f"{I}// Hold each guard while its loop's end_eq_pc is still true (stall)\n"
        f"{I}for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}  if (hwlp_end_4_id_q[i] && hwlp_end_eq_pc[i])\n"
        f"{I}    hwlp_end_4_id_d[i] = 1'b1;\n"
        f"{I}end\n"
        f"\n"
        f"{I}// ARVIS: handle taken branches inside HW loop body\n"
        f"{I}if (branch_taken_ex_i) begin\n"
        f"{I}  is_decoding_o = 1'b0;\n"
        f"{I}  pc_mux_o      = PC_BRANCH;\n"
        f"{I}  pc_set_o      = 1'b1;\n"
        f"{I}  hwlp_end_4_id_d     = '0;\n"
        f"{I}  hwlp_post_branch_d  = 1'b1;\n"
        f"{I}end\n"
        f"{I}else begin\n"
        f"{I}  // Branch was NOT taken (or no branch): commit any deferred DECs\n"
        f"{I}  for (int i = 0; i < HW_LOOP; i++)\n"
        f"{I}    if (hwlp_dec_deferred_q[i])\n"
        f"{I}      hwlp_dec_cnt_o[i] = 1'b1;\n"
    )


def _gen_controller_decode_hwloop_exit(hw_loop: int, indent: str, **kw) -> str:
    """Close the else-begin opened by controller_decode_hwloop_entry."""
    if hw_loop == 0:
        return ""
    return f"{indent}end // else: !branch_taken_ex_i\n"


def _gen_controller_decode_default_hwlp(hw_loop: int, indent: str, **kw) -> str:
    """is_hwlp_body check + dec_cnt in DECODE default case."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}if(is_hwlp_body) begin\n"
        f"{indent}  ctrl_fsm_ns  = DECODE_HWLOOP;\n"
        f"{indent}end\n"
        f"\n"
        f"{indent}if (!branch_in_id) begin\n"
        f"{indent}  for (int i = 0; i < HW_LOOP; i++)\n"
        f"{indent}    if (hwlp_end_eq_pc[i] && hwlp_counter_eq_1[i])\n"
        f"{indent}      hwlp_dec_cnt_o[i] = 1'b1;\n"
        f"{indent}end\n"
    )


def _gen_controller_decode_hwloop_default(hw_loop: int, indent: str, **kw) -> str:
    """Full DECODE_HWLOOP default case matching RTL."""
    if hw_loop == 0:
        return ""
    I = indent

    return (
        # --- Pre-clear post_branch ---
        f"{I}// Clear post_branch flag when loop end is detected,\n"
        f"{I}// even if current instruction is a branch.\n"
        f"{I}if (hwlp_end_4_id_q == '0 && hwlp_post_branch_q) begin\n"
        f"{I}  for (int i = 0; i < HW_LOOP; i++)\n"
        f"{I}    if (hwlp_end_eq_pc[i] && !hwlp_counter_eq_0[i])\n"
        f"{I}      hwlp_post_branch_d = 1'b0;\n"
        f"{I}end\n\n"
        # --- Main block ---
        f"{I}if (hwlp_end_4_id_q == '0 && !branch_in_id) begin\n"
        f"{I}  if (!hwlp_post_branch_q) begin\n"
        f"{I}    // --- Sequential path: aligner handles PC wrap, no flush ---\n"
        f"{I}    for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}        if (hwlp_end_eq_pc_plus4[i]) begin\n"
        f"{I}            if (hwlp_counter_gt_1[i])\n"
        f"{I}                ctrl_fsm_ns = DECODE_HWLOOP;\n"
        f"{I}            else\n"
        f"{I}                ctrl_fsm_ns = is_hwlp_body ? DECODE_HWLOOP : DECODE;\n"
        f"{I}        end\n"
        f"{I}    end\n"
        f"{I}    for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}        if (hwlp_end_eq_pc[i] && !hwlp_end_eq_pc_plus4[i] && !hwlp_counter_eq_0[i]) begin\n"
        f"{I}            if (hwlp_counter_gt_1[i])\n"
        f"{I}                ctrl_fsm_ns = DECODE_HWLOOP;\n"
        f"{I}            else\n"
        f"{I}                ctrl_fsm_ns = is_hwlp_body ? DECODE_HWLOOP : DECODE;\n"
        f"{I}        end\n"
        f"{I}    end\n"
        f"{I}  end else begin\n"
        f"{I}    // --- Post-branch path: pc_set_o flush ---\n"
        f"{I}    for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}        if (hwlp_end_eq_pc[i] && !hwlp_counter_eq_0[i]) begin\n"
        f"{I}            if (hwlp_counter_gt_1[i]) begin\n"
        f"{I}                hwlp_end_4_id_d[i] = 1'b1;\n"
        f"{I}                hwlp_targ_addr_o = hwlp_start_addr_i[i];\n"
        f"{I}                pc_mux_o    = PC_HWLOOP;\n"
        f"{I}                pc_set_o    = 1'b1;\n"
        f"{I}                ctrl_fsm_ns = DECODE_HWLOOP;\n"
        f"{I}            end else begin\n"
        f"{I}                ctrl_fsm_ns = is_hwlp_body ? DECODE_HWLOOP : DECODE;\n"
        f"{I}            end\n"
        f"{I}            hwlp_post_branch_d = 1'b0;\n"
        f"{I}        end\n"
        f"{I}    end\n"
        f"{I}    for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}        if (hwlp_end_eq_pc_plus4[i] && !hwlp_end_eq_pc[i]) begin\n"
        f"{I}            if (hwlp_counter_gt_1[i]) begin\n"
        f"{I}                hwlp_end_4_id_d[i] = 1'b1;\n"
        f"{I}                hwlp_targ_addr_o = hwlp_start_addr_i[i];\n"
        f"{I}                pc_mux_o    = PC_HWLOOP;\n"
        f"{I}                pc_set_o    = 1'b1;\n"
        f"{I}                ctrl_fsm_ns = DECODE_HWLOOP;\n"
        f"{I}            end else\n"
        f"{I}                ctrl_fsm_ns = is_hwlp_body ? DECODE_HWLOOP : DECODE;\n"
        f"{I}            hwlp_post_branch_d = 1'b0;\n"
        f"{I}        end\n"
        f"{I}    end\n"
        f"{I}  end\n"
        f"{I}  for (int i = 0; i < HW_LOOP; i++)\n"
        f"{I}    hwlp_dec_cnt_o[i] = hwlp_dec_cnt_o[i] | (hwlp_end_eq_pc[i] && !hwlp_counter_eq_0[i]);\n"
        f"{I}end // hwlp_end_4_id_q == 0\n"
    )


def _gen_controller_flush_hwlp(hw_loop: int, indent: str, **kw) -> str:
    """FLUSH_WB csr_status hwloop jump check."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}if (|(hwlp_end_eq_pc & ~hwlp_counter_eq_0)) begin\n"
        f"{indent}  pc_mux_o = PC_HWLOOP;\n"
        f"{indent}  pc_set_o = 1'b1;\n"
        f"{indent}end\n"
    )


def _gen_controller_innermost_dec(hw_loop: int, indent: str, **kw) -> str:
    """Generate RTL for 'innermost only' DEC guard."""
    if hw_loop == 0:
        return ""
    I = indent

    return (
        f"{I}// =========================================================================\n"
        f'{I}// Global "innermost only" DEC guard for nested HW loops\n'
        f"{I}// =========================================================================\n"
        f"{I}// When multiple loops' hwlp_end_eq_pc fires simultaneously (adjacent end\n"
        f"{I}// addresses), only the innermost (highest-index) DEC is the real end-of-loop.\n"
        f"{I}// Outer DECs are cascade artifacts — the aligner wraps to the innermost\n"
        f"{I}// loop's start, so the outer loop's end won't actually be reached.\n"
        f"{I}// When multiple loops DEC in the same cycle with different start\n"
        f"{I}// addresses, only keep the innermost.\n"
        f"{I}if (HW_LOOP) begin\n"
        f"{I}  for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{I}    if (hwlp_dec_cnt_o[i]) begin\n"
        f"{I}      for (int j = i + 1; j < HW_LOOP; j++) begin\n"
        f"{I}        // Cancel outer DEC when inner DEC fires with different start,\n"
        f"{I}        // UNLESS the inner loop is finishing (cnt==1 -> will be 0).\n"
        f"{I}        // When inner finishes, outer DEC is the real next-level end.\n"
        f"{I}        if (hwlp_dec_cnt_o[j] &&\n"
        f"{I}            hwlp_start_addr_i[j] != hwlp_start_addr_i[i])\n"
        f"{I}          hwlp_dec_cnt_o[i] = 1'b0;\n"
        f"{I}      end\n"
        f"{I}    end\n"
        f"{I}  end\n"
        f"{I}end\n"
    )


def _gen_controller_gen_hwlp(hw_loop: int, indent: str, **kw) -> str:
    """generate if(HW_LOOP)/else block with signal assignments (updated RTL)."""
    if hw_loop == 0:
        return (
            "  assign hwlp_jump_o          = 1'b0;\n"
            "  assign hwlp_end_4_id_q      = 1'b0;\n"
            "  assign hwlp_post_branch_q   = 1'b0;\n"
            "  assign hwlp_end_eq_pc       = '0;\n"
            "  assign hwlp_counter_gt_1    = '0;\n"
            "  assign hwlp_counter_eq_1    = '0;\n"
            "  assign hwlp_counter_eq_0    = '0;\n"
            "  assign hwlp_end_eq_pc_plus4 = '0;\n"
            "  assign hwlp_start_leq_pc    = '0;\n"
            "  assign hwlp_end_geq_pc      = '0;\n"
            "  assign hwlp_pc_is_end       = '0;\n"
            "  assign is_hwlp_body         = 1'b0;\n"
        )

    return (
        "generate\n"
        "  if(HW_LOOP) begin : geHW_LOOP\n"
        "    //////////////////////////////////////////////////////////////////////////////\n"
        "    // Convert hwlp_jump_o to a pulse\n"
        "    //////////////////////////////////////////////////////////////////////////////\n"
        "\n"
        "    // hwlp_jump_o should last one cycle only, as the prefetcher\n"
        "    // reacts immediately. If it last more cycles, the prefetcher\n"
        "    // goes on requesting HWLP_BEGIN more than one time (wrong!).\n"
        "    // This signal is not controlled by id_ready because otherwise,\n"
        "    // in case of stall, the jump would happen at the end of the stall.\n"
        "\n"
        "    // Disable hwlp_jump_o — use pc_set_o + PC_HWLOOP for reliable redirect.\n"
        "\n"
        "    always_ff @(posedge clk or negedge rst_n) begin\n"
        "      if(!rst_n) begin\n"
        "        hwlp_end_4_id_q     <= '0;\n"
        "        hwlp_post_branch_q  <= 1'b0;\n"
        "        hwlp_dec_deferred_q <= '0;\n"
        "      end else begin\n"
        "        hwlp_end_4_id_q     <= hwlp_end_4_id_d;\n"
        "        hwlp_post_branch_q  <= hwlp_post_branch_d;\n"
        "        // Deferred DEC: set when branch_in_id at LP_end, cleared on branch_taken or commit\n"
        "        for (int i = 0; i < HW_LOOP; i++) begin\n"
        "          if (branch_taken_ex_i)\n"
        "            hwlp_dec_deferred_q[i] <= 1'b0;\n"
        "          else if (hwlp_dec_deferred_q[i])\n"
        "            hwlp_dec_deferred_q[i] <= 1'b0;\n"
        "          else if (ctrl_fsm_cs == DECODE_HWLOOP && instr_valid_i && branch_in_id &&\n"
        "                   !hwlp_end_4_id_q[i] && hwlp_end_eq_pc[i] && !hwlp_counter_eq_0[i])\n"
        "            hwlp_dec_deferred_q[i] <= 1'b1;\n"
        "        end\n"
        "      end\n"
        "    end\n"
        "\n"
        "    // =========================================================================\n"
        "    // =========================================================================\n"
        "\n"
        "    // ARVIS: instruction size depends on whether current insn is compressed\n"
        "    logic [31:0] pc_next;  // PC of the next instruction\n"
        "    assign pc_next = pc_id_i + (is_compressed_i ? 32'd2 : 32'd4);\n"
        "\n"
        "    for (genvar i = 0; i < HW_LOOP; i++) begin : geHW_LOOP_signals\n"
        "      assign hwlp_end_eq_pc[i]       = hwlp_end_addr_i[i] == pc_next;\n"
        "      assign hwlp_end_eq_pc_plus4[i] = hwlp_pnult_addr_i[i] == pc_id_i;\n"
        "      assign hwlp_counter_gt_1[i]    = hwlp_counter_i[i] > 1;\n"
        "      assign hwlp_counter_eq_1[i]    = hwlp_counter_i[i] == 1;\n"
        "      assign hwlp_counter_eq_0[i]    = hwlp_counter_i[i] == 0;\n"
        "      // LP_START range check (supports halfword-aligned LP_START)\n"
        "      assign hwlp_start_leq_pc[i]    = hwlp_start_addr_i[i] <= pc_id_i;\n"
        "      assign hwlp_end_geq_pc[i]      = hwlp_end_addr_i[i] >= pc_next;\n"
        "      // ARVIS: detect when a branch lands exactly at LP_END (if/else skip case)\n"
        "    end\n"
        "    assign is_hwlp_body = |(hwlp_start_leq_pc & hwlp_end_geq_pc & hwlp_counter_gt_1);\n"
        "\n"
        "  end else begin : gen_no_hwlp\n"
        "\n"
        "    assign hwlp_end_4_id_q      = 1'b0;\n"
        "    assign hwlp_post_branch_q   = 1'b0;\n"
        "    assign hwlp_end_eq_pc       = '0;\n"
        "    assign hwlp_counter_gt_1    = '0;\n"
        "    assign hwlp_counter_eq_1    = '0;\n"
        "    assign hwlp_counter_eq_0    = '0;\n"
        "    assign hwlp_end_eq_pc_plus4 = '0;\n"
        "    assign hwlp_start_leq_pc    = '0;\n"
        "    assign hwlp_end_geq_pc      = '0;\n"
        "    assign is_hwlp_body         = 1'b0;\n"
        "\n"
        "  end\n"
        "endgenerate\n"
    )


# ── Decoder generators ───────────────────────────────────────────────────


def _gen_decoder_output(hw_loop: int, indent: str, **kw) -> str:
    """Decoder output ports for hwloop (simplified: 1-bit we, no mux_sels)."""
    if hw_loop == 0:
        return ""
    return f"{indent}output logic       hwlp_we_o,               // write enable for hwloop regs\n"


def _gen_decoder_we_declaration(hw_loop: int, indent: str, **kw) -> str:
    """Decoder internal hwlp_we wire (1-bit, not 3-bit)."""
    if hw_loop == 0:
        return ""
    return f"{indent}logic       hwlp_we;\n"


def _gen_decoder_signals_init(hw_loop: int, indent: str, **kw) -> str:
    """Decoder default assignments (1-bit we, no mux_sels)."""
    if hw_loop == 0:
        return ""
    return f"{indent}hwlp_we                        = 1'b0;\n"


def _gen_decoder_assert_hwlp_we(hw_loop: int, indent: str, **kw) -> str:
    """Decoder output assign with deassert gating (1-bit)."""
    if hw_loop == 0:
        return ""
    return f"{indent}assign hwlp_we_o                   = (deassert_we_i) ? 1'b0          : hwlp_we;\n"


# ── ID stage generators ─────────────────────────────────────────────────


def _gen_id_ports(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}output logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_start_o,\n"
        f"{indent}output logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_end_o,\n"
        f"{indent}output logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_cnt_o,\n"
        f"{indent}output logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_pnult_o,\n"
        f"{indent}output logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_cnt_next_o,\n"
        f"{indent}output logic [31:0]       hwlp_target_o,\n"
    )


def _gen_id_wires(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}logic [      31:0] hwlp_start;\n"
        f"{indent}logic [      31:0] hwlp_end;\n"
        f"{indent}logic [      31:0] hwlp_cnt;\n"
        f"{indent}logic [HW_LOOP_BITS-1:0] hwlp_regid;\n"
        f"{indent}logic [1:0] hwlp_pnult_off;\n"
        f"{indent}logic hwlp_we, hwlp_we_masked;\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_dec_cnt;\n"
        f"{indent}logic              hwlp_valid;\n"
    )


def _gen_id_controller_conn(hw_loop: int, indent: str, **kw) -> str:
    """Controller hwlp connections (only ports NOT in KEEP blocks).

    hwlp_mask_o is in a separate KEEP pragma in id_stage.
    """
    if hw_loop == 0:
        return ""
    return (
        f"{indent}.is_compressed_i      (is_compressed_i),\n"
        f"{indent}.hwlp_start_addr_i    (hwlp_start_o),\n"
        f"{indent}.hwlp_end_addr_i      (hwlp_end_o),\n"
        f"{indent}.hwlp_counter_i       (hwlp_cnt_o),\n"
        f"{indent}.hwlp_pnult_addr_i    (hwlp_pnult_o),\n"
        f"{indent}.hwlp_dec_cnt_o       (hwlp_dec_cnt),\n"
        f"{indent}.hwlp_targ_addr_o     (hwlp_target_o),\n"
    )


def _gen_id_hw_regs_instantiation(hw_loop: int, indent: str, **kw) -> str:
    """hwloop_regs port connections (inside generate if block)."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}// from ID (funct3-decoded hwloop data)\n"
        f"{indent}.hwlp_start_data_i(hwlp_start),\n"
        f"{indent}.hwlp_end_data_i  (hwlp_end),\n"
        f"{indent}.hwlp_cnt_data_i  (hwlp_cnt),\n"
        f"{indent}.hwlp_we_i        (hwlp_we_masked),\n"
        f"{indent}.hwlp_regid_i     (hwlp_regid),\n"
        f"{indent}.hwlp_pnult_off_i (hwlp_pnult_off),\n"
        f"{indent}.hwlp_funct3_i    (instr[14:12]),\n"
        f"\n"
        f"{indent}// from controller\n"
        f"{indent}.valid_i(hwlp_valid),\n"
        f"\n"
        f"{indent}// to hwloop controller\n"
        f"{indent}.hwlp_start_addr_o(hwlp_start_o),\n"
        f"{indent}.hwlp_end_addr_o  (hwlp_end_o),\n"
        f"{indent}.hwlp_counter_o   (hwlp_cnt_o),\n"
        f"{indent}.hwlp_counter_next_o(hwlp_cnt_next_o),\n"
        f"{indent}.hwlp_pnult_addr_o(hwlp_pnult_o),\n"
        f"\n"
        f"{indent}// from hwloop controller\n"
        f"{indent}.hwlp_dec_cnt_i(hwlp_dec_cnt)\n"
    )


def _gen_id_regid_assign(hw_loop: int, indent: str, **kw) -> str:
    """hwlp_regid and hwlp_pnult_off extraction from rd field."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}assign hwlp_regid = instr[7 +: HW_LOOP_BITS];\n"
        f"{indent}assign hwlp_pnult_off = {{1'b0, instr[7 + HW_LOOP_BITS]}};\n"
    )


def _gen_id_mux_logic(hw_loop: int, indent: str, hwlp_encoding=None, **kw) -> str:
    """Funct3-based hwloop data decode (replaces PULP muxes)."""
    if hw_loop == 0:
        return ""
    # Use dynamic funct3 values from encoding, or defaults
    if hwlp_encoding:
        bf3 = hwlp_encoding.bounds_funct3
        cf3 = hwlp_encoding.count_funct3
        sf3 = hwlp_encoding.start_funct3
        ef3 = hwlp_encoding.end_funct3
    else:
        bf3, cf3, sf3, ef3 = 0b010, 0b011, 0b100, 0b101
    return (
        f"{indent}// hwloop data sources: depends on funct3\n"
        f"{indent}always_comb begin\n"
        f"{indent}  unique case (instr[14:12])\n"
        f"{indent}    3'b{bf3:03b}: begin  // BOUNDS: compact start+end\n"
        f"{indent}      hwlp_start = pc_id_i + {{23'b0, instr[19:15], instr[11:9], 1'b0}};\n"
        f"{indent}      hwlp_end   = pc_id_i + {{19'b0, instr[31:20], 1'b0}};\n"
        f"{indent}      hwlp_cnt   = '0;\n"
        f"{indent}    end\n"
        f"{indent}    3'b{cf3:03b}: begin  // COUNT: rs3=count only\n"
        f"{indent}      hwlp_start = '0;\n"
        f"{indent}      hwlp_end   = '0;\n"
        f"{indent}      hwlp_cnt   = operand_c_fw_id;\n"
        f"{indent}    end\n"
        f"{indent}    3'b{sf3:03b}: begin  // START: PC-relative start offset\n"
        f"{indent}      hwlp_start = pc_id_i + {{19'b0, instr[31:20], 1'b0}};\n"
        f"{indent}      hwlp_end   = '0;\n"
        f"{indent}      hwlp_cnt   = '0;\n"
        f"{indent}    end\n"
        f"{indent}    3'b{ef3:03b}: begin  // END: PC-relative end offset\n"
        f"{indent}      hwlp_start = '0;\n"
        f"{indent}      hwlp_end   = pc_id_i + {{19'b0, instr[31:20], 1'b0}};\n"
        f"{indent}      hwlp_cnt   = '0;\n"
        f"{indent}    end\n"
        f"{indent}    default: begin\n"
        f"{indent}      hwlp_start = '0;\n"
        f"{indent}      hwlp_end   = '0;\n"
        f"{indent}      hwlp_cnt   = '0;\n"
        f"{indent}    end\n"
        f"{indent}  endcase\n"
        f"{indent}end\n"
    )


def _gen_id_we_masked(hw_loop: int, indent: str, **kw) -> str:
    """hwlp_we masking (1-bit version)."""
    if hw_loop == 0:
        return ""
    return f"{indent}assign hwlp_we_masked = hwlp_we & id_ready_o;\n"


# ── Core generators ──────────────────────────────────────────────────────


def _gen_core_signal_declaration(hw_loop: int, indent: str, **kw) -> str:
    """Core wire declarations for hwloop signals."""
    if hw_loop == 0:
        return ""
    I = indent

    return (
        f"{I}// Hardware loop controller signals\n"
        f"{I}localparam HWLP_W = HW_LOOP > 0 ? HW_LOOP : 1;  // minimum 1 for wire widths\n"
        f"{I}logic [HWLP_W-1:0][31:0] hwlp_start;\n"
        f"{I}logic [HWLP_W-1:0][31:0] hwlp_end;\n"
        f"{I}logic [HWLP_W-1:0][31:0] hwlp_cnt;\n"
        f"{I}logic [HWLP_W-1:0][31:0] hwlp_pnult;\n"
        f"{I}logic [31:0] hwlp_target;\n"
        f"{I}logic [HWLP_W-1:0][31:0] hwlp_cnt_next;\n"
    )


def _gen_core_id_instantiation(hw_loop: int, indent: str, **kw) -> str:
    """hwlp port connections in id_stage instantiation."""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}.hwlp_start_o(hwlp_start),\n"
        f"{indent}.hwlp_end_o  (hwlp_end),\n"
        f"{indent}.hwlp_cnt_o  (hwlp_cnt),\n"
        f"{indent}.hwlp_pnult_o(hwlp_pnult),\n"
        f"\n"
        f"{indent}.hwlp_cnt_next_o(hwlp_cnt_next),\n"
        f"{indent}.hwlp_target_o(hwlp_target),\n"
    )


# ── IF stage generators ─────────────────────────────────────────────────


def _gen_if_ports(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_start_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_end_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_counter_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_cnt_next_i,\n"
    )


def _gen_if_pc_mux(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return f"{indent}PC_HWLOOP: branch_addr_n = hwlp_target_i;\n"


def _gen_if_prefetch_conn(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}.hwlp_start_addr_i(hwlp_start_addr_i),\n"
        f"{indent}.hwlp_end_addr_i  (hwlp_end_addr_i),\n"
        f"{indent}.hwlp_counter_i   (hwlp_counter_i),\n"
        f"{indent}.hwlp_cnt_next_i  (hwlp_cnt_next_i),\n"
    )


# ── Prefetch generators ─────────────────────────────────────────────────


def _gen_prefetch_ports(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_start_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_end_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_counter_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_cnt_next_i,\n"
    )


def _gen_prefetch_fifo_cnt_masked(hw_loop: int, indent: str, **kw) -> str:
    """fifo_cnt_masked with or without hwlp_jump_i."""
    if hw_loop == 0:
        return f"{indent}assign fifo_cnt_masked = (branch_i) ? '0 : fifo_cnt_i;\n"
    return f"{indent}assign fifo_cnt_masked = (branch_i || hwlp_late_wrap_flush) ? '0 : fifo_cnt_i;\n"


def _gen_prefetch_idle_addr(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return f"{indent}trans_addr_o = trans_addr_incr;\n"
    return f"{indent}trans_addr_o = hwlp_pf_wrap ? hwlp_pf_addr : trans_addr_incr;\n"


def _gen_prefetch_idle_case_if(hw_loop: int, indent: str, **kw) -> str:
    """IDLE state branch wait condition."""
    if hw_loop == 0:
        return (
            f"{indent}if ((branch_i) && !(trans_valid_o && trans_ready_i)) begin\n"
            f"{indent}  next_state = BRANCH_WAIT;\n"
            f"{indent}end\n"
        )
    return (
        f"{indent}if ((branch_i || hwlp_late_wrap_flush) && !(trans_valid_o && trans_ready_i)) begin\n"
        f"{indent}  // Taken branch, but transaction not yet accepted by bus interface adapter.\n"
        f"{indent}  next_state = BRANCH_WAIT;\n"
        f"{indent}end\n"
    )


def _gen_prefetch_controller_flush_chain(hw_loop: int, indent: str, **kw) -> str:
    """Full flush counter if-else chain.

    When hw_loop=0: branch-only + resp fallback (no hwlp_flush_resp_delayed).
    When hw_loop>0: full 3-way chain with hwlp signals.
    """
    if hw_loop == 0:
        return (
            f"{indent}if (branch_i) begin\n"
            f"{indent}  next_flush_cnt = cnt_q;\n"
            f"{indent}  if (resp_valid_i && (cnt_q > 0)) begin\n"
            f"{indent}    next_flush_cnt = cnt_q - 1'b1;\n"
            f"{indent}  end\n"
            f"{indent}end else if (resp_valid_i && (flush_cnt_q > 0)) begin\n"
            f"{indent}  next_flush_cnt = flush_cnt_q - 1'b1;\n"
            f"{indent}end\n"
        )
    return (
        f"{indent}if (branch_i || hwlp_late_wrap_flush) begin\n"
        f"{indent}  next_flush_cnt = cnt_q;\n"
        f"{indent}  if (resp_valid_i && (cnt_q > 0)) begin\n"
        f"{indent}    next_flush_cnt = cnt_q - 1'b1;\n"
        f"{indent}  end\n"
        f"{indent}end else if (resp_valid_i && (flush_cnt_q > 0)) begin\n"
        f"{indent}  next_flush_cnt = flush_cnt_q - 1'b1;\n"
        f"{indent}end\n"
    )


def _gen_prefetch_controller_registers_if(hw_loop: int, indent: str, **kw) -> str:
    """Register update condition."""
    if hw_loop == 0:
        return (
            f"{indent}if (branch_i || (trans_valid_o && trans_ready_i)) begin\n"
            f"{indent}  trans_addr_q <= trans_addr_o;\n"
            f"{indent}end\n"
        )
    return (
        f"{indent}if (branch_i || (trans_valid_o && trans_ready_i)) begin\n"
        f"{indent}  trans_addr_q <= trans_addr_o;\n"
        f"{indent}end\n"
    )


def _gen_prefetch_gen_hwlp(hw_loop: int, indent: str, **kw) -> str:
    """Full generate if(HW_LOOP)/else block for prefetch FIFO flush logic."""
    if hw_loop == 0:
        # Only emit the gen_no_hwlp branch
        return "  // FIFO flush (no hwloop)\n  assign fifo_flush_o             = branch_i;\n"
    # Full generate with both branches
    return (
        "generate\n"
        "  if (HW_LOOP) begin : gen_hwlp\n"
        "    assign fifo_flush_o           = branch_i || hwlp_late_wrap_flush;\n"
        "  end else begin : gen_no_hwlp\n"
        "    assign fifo_flush_o             = branch_i;\n"
        "  end\n"
        "endgenerate\n"
    )


def _gen_prefetch_buf_ports(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_start_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_end_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_counter_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_cnt_next_i,\n"
    )


def _gen_prefetch_buf_conn(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}.hwlp_start_addr_i(hwlp_start_addr_i),\n"
        f"{indent}.hwlp_end_addr_i  (hwlp_end_addr_i),\n"
        f"{indent}.hwlp_counter_i   (hwlp_counter_i),\n"
        f"{indent}.hwlp_cnt_next_i  (hwlp_cnt_next_i),\n"
    )


# ── New generators for FIFO-depth-independent hwloop ────────────────────


def _gen_core_if_hwlp_connections(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}.hwlp_start_addr_i(hwlp_start),\n"
        f"{indent}.hwlp_end_addr_i  (hwlp_end),\n"
        f"{indent}.hwlp_counter_i   (hwlp_cnt),\n"
        f"{indent}.hwlp_cnt_next_i  (hwlp_cnt_next),\n"
    )


def _gen_aligner_ports(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_start_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_end_addr_i,\n"
        f"{indent}input logic [HW_LOOP-1:0][31:0] hwlp_counter_i,\n"
    )


def _gen_aligner_params(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return "module cv32e40p_aligner (\n"
    return f"module cv32e40p_aligner #(\n{indent}parameter HW_LOOP = {hw_loop}\n) (\n"


def _gen_aligner_pc_wrap(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}begin : hwlp_wrap_block\n"
        f"{indent}  logic inner_finishing;\n"
        f"{indent}  inner_finishing = 1'b0;\n"
        f"{indent}  for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{indent}    if (!inner_finishing &&\n"
        f"{indent}      hwlp_counter_i[i] > 1 &&\n"
        f"{indent}      pc_n >= hwlp_end_addr_i[i] &&\n"
        f"{indent}      pc_q >= hwlp_start_addr_i[i] && pc_q < hwlp_end_addr_i[i]) begin\n"
        f"{indent}     pc_n       = hwlp_start_addr_i[i];\n"
        f"{indent}     next_state = hwlp_start_addr_i[i][1] ? BRANCH_MISALIGNED : ALIGNED32;\n"
        f"{indent}     end\n"
        f"{indent}     if (hwlp_counter_i[i] == 1 && hwlp_end_addr_i[i] != 0 &&\n"
        f"{indent}       pc_n >= hwlp_end_addr_i[i] &&\n"
        f"{indent}       pc_q >= hwlp_start_addr_i[i] && pc_q < hwlp_end_addr_i[i])\n"
        f"{indent}       inner_finishing = 1'b1;\n"
        f"{indent}  end\n"
        f"{indent}end\n"
    )


def _gen_if_aligner_hwlp_connections(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    return (
        f"{indent}.hwlp_start_addr_i(hwlp_start_addr_i),\n"
        f"{indent}.hwlp_end_addr_i  (hwlp_end_addr_i),\n"
        f"{indent}.hwlp_counter_i   (hwlp_counter_i),\n"
    )


def _gen_if_aligner_instantiation(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return f"{indent}cv32e40p_aligner aligner_i (\n"
    return f"{indent}cv32e40p_aligner #(.HW_LOOP(HW_LOOP)) aligner_i (\n"


def _gen_prefetch_proactive_wrap(hw_loop: int, indent: str = "    ", **kw) -> str:
    """Generate RTL for proactive hwloop prefetch wrap, small loop flag, and late wrap flush."""
    if hw_loop == 0:
        return ""

    return (
        f"{indent}// Proactive hwloop prefetch wrap\n"
        f"{indent}logic hwlp_pf_wrap;\n"
        f"{indent}logic [31:0] hwlp_pf_addr;\n"
        f"{indent}always_comb begin\n"
        f"{indent}  hwlp_pf_wrap = 1'b0;\n"
        f"{indent}  hwlp_pf_addr = '0;\n"
        f"{indent}  for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{indent}    logic inside_guard;\n"
        f"{indent}    inside_guard = 1'b0;\n"
        f"{indent}    for (int j = i + 1; j < HW_LOOP; j++)\n"
        f"{indent}      if (((hwlp_small_loop_q[j]) ? hwlp_cnt_next_i[j] <= 2 : hwlp_cnt_next_i[j] <= 1) &&\n"
        f"{indent}          hwlp_start_addr_i[j] != hwlp_end_addr_i[j] &&\n"
        f"{indent}          trans_addr_q >= hwlp_start_addr_i[j] &&\n"
        f"{indent}          trans_addr_q < hwlp_end_addr_i[j])\n"
        f"{indent}        inside_guard = 1'b1;\n"
        f"{indent}    // Normal wrap: prefetcher about to cross LP_end\n"
        f"{indent}    if (!inside_guard &&\n"
        f"{indent}        ((hwlp_small_loop_q[i]) ? hwlp_cnt_next_i[i] > 2 : hwlp_cnt_next_i[i] > 1) &&\n"
        f"{indent}        {{trans_addr_incr[31:2], 2'b00}} >= hwlp_end_addr_i[i] &&\n"
        f"{indent}        trans_addr_q >= hwlp_start_addr_i[i] &&\n"
        f"{indent}        trans_addr_q < hwlp_end_addr_i[i]) begin\n"
        f"{indent}      hwlp_pf_wrap = 1'b1;\n"
        f"{indent}      hwlp_pf_addr = {{hwlp_start_addr_i[i][31:2], 2'b00}};\n"
        f"{indent}    end\n"
        f"{indent}    // Setup redirect: counter just written, prefetcher past LP_end\n"
        f"{indent}    if (!inside_guard && !hwlp_pf_wrap &&\n"
        f"{indent}        hwlp_cnt_next_i[i] > 1 && hwlp_counter_i[i] == 0 &&\n"
        f"{indent}        hwlp_end_addr_i[i] != '0 &&\n"
        f"{indent}        trans_addr_q >= hwlp_end_addr_i[i]) begin\n"
        f"{indent}      hwlp_pf_wrap = 1'b1;\n"
        f"{indent}      hwlp_pf_addr = {{hwlp_start_addr_i[i][31:2], 2'b00}};\n"
        f"{indent}    end\n"
        f"{indent}  end\n"
        f"{indent}end\n\n"
        f"{indent}// Late wrap flush: one-time FIFO flush\n"
        f"{indent}logic hwlp_late_wrap_flush;\n"
        f"{indent}always_comb begin\n"
        f"{indent}  hwlp_late_wrap_flush = 1'b0;\n"
        f"{indent}  for (int i = 0; i < HW_LOOP; i++)\n"
        f"{indent}    if (!hwlp_pf_wrap &&\n"
        f"{indent}        hwlp_cnt_next_i[i] > 1 && hwlp_counter_i[i] == 0 &&\n"
        f"{indent}        hwlp_end_addr_i[i] != '0 &&\n"
        f"{indent}        trans_addr_q >= hwlp_end_addr_i[i])\n"
        f"{indent}      hwlp_late_wrap_flush = 1'b1;\n"
        f"{indent}end\n\n"
        f"{indent}// Sticky flag: loop body fits in FIFO (small loop)\n"
        f"{indent}logic [HW_LOOP-1:0] hwlp_small_loop_q;\n"
        f"{indent}always_ff @(posedge clk or negedge rst_n)\n"
        f"{indent}  if (!rst_n) hwlp_small_loop_q <= '0;\n"
        f"{indent}  else for (int i = 0; i < HW_LOOP; i++) begin\n"
        f"{indent}    if (hwlp_counter_i[i] == 0)\n"
        f"{indent}      hwlp_small_loop_q[i] <= 1'b0;\n"
        f"{indent}    else if (hwlp_end_addr_i[i] != '0 &&\n"
        f"{indent}             (hwlp_end_addr_i[i] - hwlp_start_addr_i[i]) < (DEPTH * 4))\n"
        f"{indent}      hwlp_small_loop_q[i] <= 1'b1;\n"
        f"{indent}  end\n"
    )


# ── CS registers generators ─────────────────────────────────────────────


def _gen_csr_params(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    if hw_loop == 0:
        return ""
    return f"{indent}parameter HW_LOOP = 0,\n"


def _gen_csr_ports(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    if hw_loop == 0:
        return ""
    return (
        f"{indent}input logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_start_i,\n"
        f"{indent}input logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_end_i,\n"
        f"{indent}input logic [HW_LOOP > 0 ? HW_LOOP-1 : 0:0][31:0] hwlp_cnt_i,\n"
    )


def _gen_csr_hwlp_read(hw_loop: int, indent: str, **kw) -> str:
    if hw_loop == 0:
        return ""
    if hw_loop == 0:
        return ""
    """CSR read logic for hwloop registers."""  # TODO: Generate from working RTL if needed
    return ""
