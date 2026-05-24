"""Synthesis flow abstraction.

A :class:`SynthesisFlow` runs logic synthesis on an emitted RTL
variant and returns area / timing / power estimates.  Today the
only implementation is :class:`YosysFlow`; LibreLane and Vivado are
documented future targets.

Synthesis is *optional* — a pipeline may run with no synthesis
flow attached, in which case the produced :class:`SynthResult` is
empty and reports omit area numbers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class SynthResult:
    """Outcome of one synthesis run.

    Fields are optional because not every flow reports every metric
    (Yosys with Nangate45 reports area + cell count; LibreLane adds
    Fmax + power).
    """

    area_um2: Optional[float] = None
    cell_count: Optional[int] = None
    fmax_mhz: Optional[float] = None
    power_mw: Optional[float] = None
    technology: str = ""
    log_path: Optional[Path] = None
    extra: Mapping[str, object] = field(default_factory=dict)


class SynthesisFlow(ABC):
    """Abstract synthesis flow.

    Like :class:`Verifier`, a synthesis flow MUST be stateless
    across invocations: two runs on the same RTL tree with the
    same technology must produce the same numbers (modulo any
    explicitly-modelled noise).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (``"yosys"``, ``"librelane"``)."""

    @abstractmethod
    def synthesize(
        self,
        rtl_dir: Path,
        technology: str,
        constraints: Optional[Path] = None,
    ) -> SynthResult:
        """Run synthesis and return area/timing/power.

        Parameters
        ----------
        rtl_dir:
            Root of the RTL workspace to synthesize.
        technology:
            Technology library name (e.g. ``"nangate45"``,
            ``"sky130"``, ``"asap7"``).
        constraints:
            Optional path to a constraints file (SDC, etc.).
        """
