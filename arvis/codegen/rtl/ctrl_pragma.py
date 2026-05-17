"""
Controller Pragma Generators (ARVIS_CTRL).

Unified pragmas for the controller FSM that handle BOTH
IRQ and DBG removal in a single pragma block.

Each generator receives (irq_enabled, dbg_enabled, indent) and
outputs the correct RTL for the combination.
"""

from __future__ import annotations

from typing import Callable, Dict


def _gen_first_fetch_irq_block(irq: bool, dbg: bool, indent: str) -> str:
    """FIRST_FETCH IRQ handling block.

    IRQ=off: remove entire block
    IRQ=on, DBG=off: if (irq_req_ctrl_i) begin ... end
    IRQ=on, DBG=on: keep original (if (irq_req_ctrl_i && ~(debug...)))
    """
    if not irq:
        return ""  # Remove entire block
    if not dbg:
        # Keep IRQ block but remove debug guard from condition
        return None  # TODO: generate simplified block without debug_req_pending
    return None  # Keep original


def _gen_decode_irq_block(irq: bool, dbg: bool, indent: str) -> str:
    """DECODE state IRQ handling block (if/else chain).

    IRQ=off: remove entire if(irq_req_ctrl_i) ... else branch
    IRQ=on, DBG=off: keep but simplify condition
    IRQ=on, DBG=on: keep original
    """
    if not irq:
        return ""  # Remove: fall through to else branch
    if not dbg:
        return None  # TODO: generate simplified block
    return None  # Keep original


def _gen_decode_hwloop_irq_block(irq: bool, dbg: bool, indent: str) -> str:
    """DECODE_HWLOOP IRQ block."""
    if not irq:
        return ""
    if not dbg:
        return None
    return None


def _gen_irq_flush_elw_irq_block(irq: bool, dbg: bool, indent: str) -> str:
    """IRQ_FLUSH_ELW IRQ block (inside COREV_CLUSTER generate)."""
    if not irq:
        return ""
    if not dbg:
        return None  # TODO: simplified
    return None


def _gen_elw_exe_next_state(irq: bool, dbg: bool, indent: str) -> str:
    """ELW_EXE next state: references both debug and IRQ.

    Original: ctrl_fsm_ns = ((debug_req_pending || trigger_match_i) & ~debug_mode_q) ? DBG_FLUSH : IRQ_FLUSH_ELW;
    IRQ=off, DBG=off: ctrl_fsm_ns = DECODE; (no IRQ flush, no debug)
    IRQ=on, DBG=off: ctrl_fsm_ns = IRQ_FLUSH_ELW;
    IRQ=off, DBG=on: ctrl_fsm_ns = ((debug...) ? DBG_FLUSH : DECODE;
    IRQ=on, DBG=on: keep original
    """
    if not irq and not dbg:
        return f"{indent}ctrl_fsm_ns = DECODE;\n"
    if irq and not dbg:
        return f"{indent}ctrl_fsm_ns = IRQ_FLUSH_ELW;\n"
    if not irq and dbg:
        return f"{indent}ctrl_fsm_ns = ((debug_req_pending || trigger_match_i) & ~debug_mode_q) ? DBG_FLUSH : DECODE;\n"
    return None  # Keep original


def _gen_ctrl_wake_from_sleep(irq: bool, dbg: bool, indent: str) -> str:
    """Wake from sleep: references both IRQ and DBG.

    Original: assign wake_from_sleep_o = irq_wu_ctrl_i || debug_req_pending || debug_mode_q;
    IRQ=off, DBG=off: assign wake_from_sleep_o = 1'b0; (only WFI timer can wake)
    IRQ=on, DBG=off: assign wake_from_sleep_o = irq_wu_ctrl_i;
    IRQ=off, DBG=on: assign wake_from_sleep_o = debug_req_pending || debug_mode_q;
    IRQ=on, DBG=on: keep original
    """
    if not irq and not dbg:
        return f"{indent}assign wake_from_sleep_o = 1'b0;\n"
    if irq and not dbg:
        return f"{indent}assign wake_from_sleep_o = irq_wu_ctrl_i;\n"
    if not irq and dbg:
        return f"{indent}assign wake_from_sleep_o = debug_req_pending || debug_mode_q;\n"
    return None  # Keep original


def _gen_pkg_pc_mux(irq: bool, dbg: bool, indent: str) -> str:
    """PC mux parameters: re-encode with minimal width.

    Always present: BOOT, JUMP, BRANCH, EXCEPTION, FENCEI, MRET, URET (7)
    Optional: PC_DRET (debug), PC_HWLOOP (hwloop  always included, managed by HWLP pragma)

    Also defines PC_MUX_WIDTH so signal declarations can reference it.
    """
    values = ["PC_BOOT", "PC_FENCEI", "PC_JUMP", "PC_BRANCH", "PC_EXCEPTION", "PC_MRET", "PC_URET"]
    if dbg:
        values.append("PC_DRET")
    values.append("PC_HWLOOP")  # Always include, HWLP pragma handles removal

    width = 4  # Preserve original width to match pipeline signal declarations

    lines = []
    for i, name in enumerate(values):
        lines.append(f"{indent}parameter {name} = {width}'d{i};")
    return "\n".join(lines) + "\n"


def _gen_pkg_exc_pc_mux(irq: bool, dbg: bool, indent: str) -> str:
    """EXC_PC mux parameters: re-encode with minimal width.

    Also defines EXC_PC_MUX_WIDTH so signal declarations can reference it.
    """
    values = ["EXC_PC_EXCEPTION"]
    if irq:
        values.append("EXC_PC_IRQ")
    if dbg:
        values.append("EXC_PC_DBD")
        values.append("EXC_PC_DBE")

    width = 4  # Preserve original width to match pipeline signal declarations

    lines = []
    for i, name in enumerate(values):
        lines.append(f"{indent}parameter {name} = {width}'d{i};")
    return "\n".join(lines) + "\n"


# All controller generators: (irq_enabled, dbg_enabled, indent) -> str|None
CTRL_GENERATORS: Dict[str, Callable[[bool, bool, str], str]] = {
    "first_fetch_irq_block": _gen_first_fetch_irq_block,
    "decode_irq_block": _gen_decode_irq_block,
    "decode_hwloop_irq_block": _gen_decode_hwloop_irq_block,
    "irq_flush_elw_irq_block": _gen_irq_flush_elw_irq_block,
    "elw_exe_next_state": _gen_elw_exe_next_state,
    "ctrl_wake_from_sleep": _gen_ctrl_wake_from_sleep,
    # Package parameter generators
    "pkg_pc_mux": _gen_pkg_pc_mux,
    "pkg_exc_pc_mux": _gen_pkg_exc_pc_mux,
}
