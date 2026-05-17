"""
Analyzes which multiplier modes are actually used by the firmware.

The CV32E40P multiplier (cv32e40p_mult.sv) supports multiple modes:
  - MUL_MAC32: 32×32 MAC (used by ``mul``)
  - MUL_MSU32: 32×32 MSU (PULP-only: p.mac / p.msu)
  - MUL_I:     Short integer multiply (PULP-only: p.mul*)
  - MUL_IR:    Short integer multiply with rounding (PULP-only)
  - MUL_DOT8:  8-bit dot product (PULP-only: pv.dotsp.b etc.)
  - MUL_DOT16: 16-bit dot product (PULP-only: pv.dotsp.h etc.)
  - MUL_H:     mulh state machine (used by ``mulh``, ``mulhsu``, ``mulhu``)

When COREV_PULP=0 (standard RV32IM), only MUL_MAC32 and MUL_H are
reachable from the decoder. This module identifies which modes are
actually exercised by the workload so the rest can be pruned.

Pruning opportunities:
  - Entire dot product hardware (char/short multipliers, accumulators)
  - MUL_H multi-cycle state machine (if no mulh/mulhsu/mulhu)
  - MUL_MSU32 subtract path
  - MUL_I/MUL_IR short-multiply rounding logic
  - CLPX (complex number) datapath in dot multiplier
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set

from .models import Instruction

# Instruction mnemonic → multiplier mode mapping
MNEMONIC_TO_MUL_MODE: Dict[str, str] = {
    # RV32M standard
    "mul": "MUL_MAC32",
    "mulh": "MUL_H",
    "mulhsu": "MUL_H",
    "mulhu": "MUL_H",
    # PULP multiply-accumulate (COREV_PULP=1 only)
    "p.mac": "MUL_MAC32",
    "p.msu": "MUL_MSU32",
    # PULP short multiplies
    "p.muls": "MUL_I",
    "p.mulhhs": "MUL_I",
    "p.mulsN": "MUL_I",
    "p.mulhhsN": "MUL_I",
    "p.mulsRN": "MUL_IR",
    "p.mulhhsRN": "MUL_IR",
    "p.mulu": "MUL_I",
    "p.mulhhu": "MUL_I",
    "p.muluN": "MUL_I",
    "p.mulhhuN": "MUL_I",
    "p.muluRN": "MUL_IR",
    "p.mulhhuRN": "MUL_IR",
    "p.macsN": "MUL_I",
    "p.machhsN": "MUL_I",
    "p.macsRN": "MUL_IR",
    "p.machhsRN": "MUL_IR",
    "p.macuN": "MUL_I",
    "p.machhuN": "MUL_I",
    "p.macuRN": "MUL_IR",
    "p.machhuRN": "MUL_IR",
    # PULP dot products (8-bit)
    "pv.dotsp.b": "MUL_DOT8",
    "pv.dotup.b": "MUL_DOT8",
    "pv.dotusp.b": "MUL_DOT8",
    "pv.sdotsp.b": "MUL_DOT8",
    "pv.sdotup.b": "MUL_DOT8",
    "pv.sdotusp.b": "MUL_DOT8",
    # PULP dot products (16-bit)
    "pv.dotsp.h": "MUL_DOT16",
    "pv.dotup.h": "MUL_DOT16",
    "pv.dotusp.h": "MUL_DOT16",
    "pv.sdotsp.h": "MUL_DOT16",
    "pv.sdotup.h": "MUL_DOT16",
    "pv.sdotusp.h": "MUL_DOT16",
    # PULP complex (uses DOT16 path)
    "pv.cplxmul.r": "MUL_DOT16",
    "pv.cplxmul.i": "MUL_DOT16",
}

# All multiplier modes defined in cv32e40p_pkg.sv
ALL_MUL_MODES: Set[str] = {
    "MUL_MAC32",
    "MUL_MSU32",
    "MUL_I",
    "MUL_IR",
    "MUL_DOT8",
    "MUL_DOT16",
    "MUL_H",
}

# Modes that are only reachable when COREV_PULP=1
PULP_ONLY_MODES: Set[str] = {
    "MUL_MSU32",
    "MUL_I",
    "MUL_IR",
    "MUL_DOT8",
    "MUL_DOT16",
}


@dataclass
class MulUsageResult:
    """Result of multiplier mode usage analysis."""

    used_modes: Set[str] = field(default_factory=set)
    unused_modes: Set[str] = field(default_factory=set)
    removable_modes: Set[str] = field(default_factory=set)

    # Derived feature flags
    uses_mul: bool = False
    uses_mulh: bool = False
    uses_dot8: bool = False
    uses_dot16: bool = False
    uses_dot_any: bool = False
    uses_clpx: bool = False
    uses_short_mul: bool = False
    uses_msu: bool = False

    # Hardware blocks that can be removed
    can_remove_dot_hardware: bool = False
    can_remove_mulh_fsm: bool = False
    can_remove_short_mul: bool = False
    can_remove_clpx: bool = False
    can_remove_msu_path: bool = False

    def summary(self) -> str:
        lines = [
            "Multiplier Usage Analysis",
            f"  Used modes:     {sorted(self.used_modes)}",
            f"  Removable modes: {sorted(self.removable_modes)}",
            f"  MUL: {self.uses_mul}, MULH: {self.uses_mulh}",
            f"  DOT8: {self.uses_dot8}, DOT16: {self.uses_dot16}",
            f"  CLPX: {self.uses_clpx}, Short: {self.uses_short_mul}",
            "",
            "  Removable hardware:",
        ]
        if self.can_remove_dot_hardware:
            lines.append("    - Dot product datapath (char + short multipliers)")
        if self.can_remove_mulh_fsm:
            lines.append("    - MULH multi-cycle FSM")
        if self.can_remove_short_mul:
            lines.append("    - Short multiply rounding logic")
        if self.can_remove_clpx:
            lines.append("    - Complex number (CLPX) datapath")
        if self.can_remove_msu_path:
            lines.append("    - MSU subtract path")
        if not any(
            [
                self.can_remove_dot_hardware,
                self.can_remove_mulh_fsm,
                self.can_remove_short_mul,
                self.can_remove_clpx,
                self.can_remove_msu_path,
            ]
        ):
            lines.append("    (none)")
        return "\n".join(lines)


def analyze_mul_usage(
    instructions: List[Instruction],
    corev_pulp: int = 0,
) -> MulUsageResult:
    """Analyze which multiplier modes are used by the firmware.

    Parameters
    ----------
    instructions : list[Instruction]
        Decoded instructions from the binary.
    corev_pulp : int
        Value of the COREV_PULP parameter (0 or 1).

    Returns
    -------
    MulUsageResult
    """
    result = MulUsageResult()

    # Collect used modes
    for inst in instructions:
        mnem = inst.mnemonic.lower()
        mode = MNEMONIC_TO_MUL_MODE.get(mnem)
        if mode:
            result.used_modes.add(mode)

        # Detect CLPX usage (complex number multiply)
        if "cplxmul" in mnem:
            result.uses_clpx = True

    # When COREV_PULP=0, PULP-only modes are unreachable regardless
    if corev_pulp == 0:
        result.used_modes -= PULP_ONLY_MODES

    # Compute unused and removable
    result.unused_modes = ALL_MUL_MODES - result.used_modes
    result.removable_modes = set(result.unused_modes)

    # Set feature flags
    result.uses_mul = "MUL_MAC32" in result.used_modes
    result.uses_mulh = "MUL_H" in result.used_modes
    result.uses_dot8 = "MUL_DOT8" in result.used_modes
    result.uses_dot16 = "MUL_DOT16" in result.used_modes
    result.uses_dot_any = result.uses_dot8 or result.uses_dot16
    result.uses_short_mul = bool({"MUL_I", "MUL_IR"} & result.used_modes)
    result.uses_msu = "MUL_MSU32" in result.used_modes

    # Determine which hardware blocks can be removed
    result.can_remove_dot_hardware = not result.uses_dot_any
    result.can_remove_mulh_fsm = not result.uses_mulh
    result.can_remove_short_mul = not result.uses_short_mul
    result.can_remove_clpx = not result.uses_clpx
    result.can_remove_msu_path = not result.uses_msu

    return result
