"""Hardware-loop address-register narrowing.

Wraps :func:`pipeline.hwloop_sweep.analyze_addr_width` as a
:class:`WidthStrategy`.  The HWLP_START / HWLP_END / HWLP_LAST
registers only need to span the binary's text section; narrowing
them saves FFs and shortens per-cycle PC-comparison CARRY chains
in the controller, aligner, and prefetch_controller.

Note that this strategy is *only meaningful when the loop strategy
selected nest_depth > 0*.  When the loop strategy didn't run (or
ran but produced ``LoopDecision(nest_depth=0)``), narrowing the
HWLP regs has no effect since they aren't instantiated.  The
applicability check captures this by verifying the target exposes
the parameter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arvis.core.strategy import WidthStrategy, WidthDecision

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


class HWLPAddrNarrowing(WidthStrategy):
    """Auto-narrow HWLP_ADDR_WIDTH to ``clog2(text_end) + margin``.

    Parameters
    ----------
    margin_bits:
        Extra bits beyond the strictly-required width.
    minimum:
        Lower bound.  ``12`` is the smallest realistic value (smaller
        text sections are unusual and we'd rather pay one extra
        bit than risk an off-by-one against compiler reshuffling).
    maximum:
        Upper bound.  At this value the strategy reports a no-op.
    """

    def __init__(
        self, *, margin_bits: int = 1, minimum: int = 12, maximum: int = 32
    ) -> None:
        self.margin_bits = margin_bits
        self.minimum = minimum
        self.maximum = maximum

    @property
    def name(self) -> str:
        return "hwlp-addr-narrowing"

    def applicable(self, workload: "Workload", target: "TargetCore") -> bool:
        return target.parameter("HWLP_ADDR_WIDTH") is not None

    def analyze(
        self,
        workload: "Workload",
        profile: "WorkloadProfile",
        target: "TargetCore",
    ) -> WidthDecision:
        from arvis.pipeline.hwloop_sweep import analyze_addr_width

        widths = []
        for elf in profile.elf_paths:
            if elf is None:
                continue
            try:
                widths.append(analyze_addr_width(str(elf), self.margin_bits))
            except Exception:
                continue

        if not widths:
            return WidthDecision(hwlp_addr_width=self.maximum)

        chosen = max(widths)
        chosen = max(self.minimum, min(self.maximum, chosen))
        return WidthDecision(hwlp_addr_width=chosen)
