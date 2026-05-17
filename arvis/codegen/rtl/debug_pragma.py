"""
Debug Pragma Generators.

Generators for ARVIS_DBG_BEGIN/END pragmas.
When debug disabled: removes debug logic or simplifies expressions.
When debug enabled: keeps original code.
"""

from __future__ import annotations

from typing import Callable, Dict


def _gen_sleep_core_sleep_elw(level: int, indent: str) -> str:
    """core_sleep_o in sleep_unit PULP cluster path — remove debug check."""
    if level == 0:
        return (
            f"{indent}// Sleep only in response to cv.elw once no longer busy\n"
            f"{indent}assign core_sleep_o = p_elw_busy_d && !core_busy_q;\n"
        )
    return (
        f"{indent}// Sleep only in response to cv.elw once no longer busy (but not during debug)\n"
        f"{indent}assign core_sleep_o = p_elw_busy_d && !core_busy_q && !debug_p_elw_no_sleep_i;\n"
    )


def _gen_cs_regs_if_debug(level: int, indent: str) -> str:
    """Exception PC save: debug saves to depc, normal saves to mepc."""
    if level == 0:
        return f"{indent}mepc_n = exception_pc;\n"
    return f"{indent}if (debug_csr_save_i) depc_n = exception_pc;\n{indent}else mepc_n = exception_pc;\n"


def _gen_csr_irq_enable(level: int, indent: str) -> str:
    """IRQ enable: remove dcsr single-step check when no debug."""
    if level == 0:
        return f"{indent}assign m_irq_enable_o = mstatus_q.mie;\n{indent}assign u_irq_enable_o = mstatus_q.uie;\n"
    return (
        f"{indent}assign m_irq_enable_o = mstatus_q.mie && !(dcsr_q.step && !dcsr_q.stepie);\n"
        f"{indent}assign u_irq_enable_o = mstatus_q.uie && !(dcsr_q.step && !dcsr_q.stepie);\n"
    )


# ── Controller generators ─────────────────────────────────────────────


def _gen_ctrl_irq_guard(level: int, indent: str) -> str:
    """IRQ check with debug guard: ~(debug_req_pending || debug_mode_q)."""
    if level == 0:
        return f"{indent}if (irq_req_ctrl_i) begin\n"
    return f"{indent}if (irq_req_ctrl_i && ~(debug_req_pending || debug_mode_q)) begin\n"


def _gen_ctrl_csr_save_cause(level: int, indent: str) -> str:
    """csr_save_cause_o = !debug_mode_q -> 1'b1 when no debug."""
    if level == 0:
        return f"{indent}csr_save_cause_o  = 1'b1;\n"
    return f"{indent}csr_save_cause_o  = !debug_mode_q;\n"


def _gen_ctrl_decode_debug_irq(level: int, indent: str) -> str:
    """DECODE preamble: debug check then IRQ check."""
    if level == 0:
        return f"{indent}if (irq_req_ctrl_i)\n{indent}  begin\n"
    return (
        f"{indent}if ( (debug_req_pending || trigger_match_i) & ~debug_mode_q )\n"
        f"{indent}  begin\n"
        f"{indent}    //Serving the debug\n"
        f"{indent}    is_decoding_o     = HW_LOOP ? 1'b0 : 1'b1;\n"
        f"{indent}    halt_if_o         = 1'b1;\n"
        f"{indent}    halt_id_o         = 1'b1;\n"
        f"{indent}    ctrl_fsm_ns       = DBG_FLUSH;\n"
        f"{indent}    debug_req_entry_n = 1'b1;\n"
        f"{indent}  end\n"
        f"{indent}else if (irq_req_ctrl_i && ~debug_mode_q)\n"
        f"{indent}  begin\n"
    )


def _gen_ctrl_exc_pc_mux(level: int, indent: str) -> str:
    """exc_pc_mux_o = debug_mode_q ? EXC_PC_DBE : EXC_PC_EXCEPTION."""
    if level == 0:
        return f"{indent}exc_pc_mux_o          = EXC_PC_EXCEPTION;\n"
    return f"{indent}exc_pc_mux_o          = debug_mode_q ? EXC_PC_DBE : EXC_PC_EXCEPTION;\n"


def _gen_ctrl_restore_not_debug(level: int, indent: str) -> str:
    """csr_restore_*_id_o = !debug_mode_q -> 1'b1."""
    if level == 0:
        return f"{indent}= 1'b1;\n"
    return f"{indent}= !debug_mode_q;\n"


def _gen_ctrl_pc_mux_mret(level: int, indent: str) -> str:
    """pc_mux_o = debug_mode_q ? PC_EXCEPTION : PC_MRET."""
    if level == 0:
        return f"{indent}pc_mux_o              = PC_MRET;\n{indent}pc_set_o              = 1'b1;\n"
    return (
        f"{indent}pc_mux_o              = debug_mode_q ? PC_EXCEPTION : PC_MRET;\n"
        f"{indent}pc_set_o              = 1'b1;\n"
        f"{indent}exc_pc_mux_o          = EXC_PC_DBE;\n"
    )


def _gen_ctrl_pc_mux_uret(level: int, indent: str) -> str:
    """pc_mux_o = debug_mode_q ? PC_EXCEPTION : PC_URET."""
    if level == 0:
        return f"{indent}pc_mux_o              = PC_URET;\n{indent}pc_set_o              = 1'b1;\n"
    return (
        f"{indent}pc_mux_o              = debug_mode_q ? PC_EXCEPTION : PC_URET;\n"
        f"{indent}pc_set_o              = 1'b1;\n"
        f"{indent}exc_pc_mux_o          = EXC_PC_DBE;\n"
    )


def _gen_ctrl_elw_exe_debug(level: int, indent: str) -> str:
    """ELW_EXE debug ternary for next state."""
    if level == 0:
        return f"{indent}ctrl_fsm_ns = IRQ_FLUSH_ELW;\n"
    return (
        f"{indent}ctrl_fsm_ns = ((debug_req_pending || trigger_match_i) & ~debug_mode_q) ? DBG_FLUSH : IRQ_FLUSH_ELW;\n"
    )


def _gen_ctrl_wake_from_sleep(level: int, indent: str) -> str:
    """wake_from_sleep_o without debug terms."""
    if level == 0:
        return f"{indent}assign wake_from_sleep_o = irq_wu_ctrl_i;\n"
    return f"{indent}assign wake_from_sleep_o = irq_wu_ctrl_i || debug_req_pending || debug_mode_q;\n"


def _gen_ctrl_xret_case_label(level: int, indent: str) -> str:
    """Case label: mret | uret | dret → remove dret when no debug."""
    if level == 0:
        return f"{indent}mret_insn_i | uret_insn_i: begin\n"
    return f"{indent}mret_insn_i | uret_insn_i | dret_insn_i: begin\n"


def _gen_controller_wfi_gating(level: int, indent: str) -> str:
    """wfi_active gating: with debug, gate off WFI during debug scenarios."""
    if level == 0:
        return f"{indent}assign wfi_active = wfi_i;\n"
    return f"{indent}assign wfi_active = wfi_i & ~debug_wfi_no_sleep_o;\n"


# ── All debug generators ──────────────────────────────────────────────

DBG_GENERATORS: Dict[str, Callable] = {
    "sleep_core_sleep_elw": _gen_sleep_core_sleep_elw,
    "cs_regs_if_debug": _gen_cs_regs_if_debug,
    "csr_irq_enable": _gen_csr_irq_enable,
    # Controller
    "ctrl_irq_guard": _gen_ctrl_irq_guard,
    "ctrl_csr_save_cause": _gen_ctrl_csr_save_cause,
    "ctrl_decode_debug_irq": _gen_ctrl_decode_debug_irq,
    "ctrl_exc_pc_mux": _gen_ctrl_exc_pc_mux,
    "ctrl_restore_not_debug": _gen_ctrl_restore_not_debug,
    "ctrl_pc_mux_mret": _gen_ctrl_pc_mux_mret,
    "ctrl_pc_mux_uret": _gen_ctrl_pc_mux_uret,
    "ctrl_elw_exe_debug": _gen_ctrl_elw_exe_debug,
    "ctrl_wake_from_sleep": _gen_ctrl_wake_from_sleep,
    "controller_wfi_gating": _gen_controller_wfi_gating,
    "ctrl_xret_case_label": _gen_ctrl_xret_case_label,
}
