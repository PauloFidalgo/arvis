"""Hardware-loop counter-register narrowing.

Wraps :func:`pipeline.hwloop_sweep.analyze_counter_width` as a
:class:`WidthStrategy`.  The hwloop counter register holds the
remaining-iterations value; narrowing it from 32 bits to
``ceil(log2(max_count)) + margin`` saves FFs in
``cv32e40p_hwloop_regs.sv``.

Counter analysis is *not* derivable from the ELF alone — it needs
the patched assembly for each candidate hwloop, because the count
literal lives in an immediate field that may be variable, constant,
or derived from a register in the surrounding code.  This strategy
therefore needs more than the ``elf_paths`` field: it also reads
the hwloop candidates from the workload profile.

In Phase 1 the candidate list is opaque (``Tuple[Any, ...]``); the
strategy expects entries shaped like the legacy
:class:`pipeline.hwloop.HWLoopCandidate` and falls back gracefully
when the profile doesn't carry the right shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arvis.core.strategy import WidthStrategy, WidthDecision

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


class CounterWidthNarrowing(WidthStrategy):
    """Auto-narrow ``HW_LOOP_CNT_WIDTH`` based on observed loop counts.

    This strategy is best run *after* a :class:`LoopStrategy` has
    selected its candidates, because narrowing the counter to fit
    a specific candidate's max-iteration count gives the tightest
    bound.  When run in isolation (no candidates in the profile)
    it returns the conservative default of 16 bits, mirroring the
    legacy behaviour for "all dynamic counts".

    Parameters
    ----------
    margin_bits:
        Extra bits beyond the strictly-required width.
    minimum:
        Lower bound.  ``8`` because anything smaller defeats the
        purpose for typical embedded benchmarks.
    maximum:
        Upper bound; reporting at this value disables narrowing.
    """

    def __init__(
        self, *, margin_bits: int = 1, minimum: int = 8, maximum: int = 32
    ) -> None:
        self.margin_bits = margin_bits
        self.minimum = minimum
        self.maximum = maximum

    @property
    def name(self) -> str:
        return "counter-width-narrowing"

    def applicable(self, workload: "Workload", target: "TargetCore") -> bool:
        return target.parameter("CNT_WIDTH") is not None

    def analyze(
        self,
        workload: "Workload",
        profile: "WorkloadProfile",
        target: "TargetCore",
    ) -> WidthDecision:
        from arvis.pipeline.hwloop_sweep import analyze_counter_width

        # The legacy analyze_counter_width takes (asm_path, hw_loop)
        # rather than ELF.  We fish those out of the profile.loops
        # tuple; entries are expected to expose ``asm_path`` and
        # ``hw_loop`` attributes (which the legacy
        # ``HWLoopCandidate`` does).
        widths = []
        for cand in profile.loops:
            asm_path = getattr(cand, "asm_path", None)
            hw_loop = getattr(cand, "hw_loop", None)
            if asm_path is None or hw_loop is None:
                continue
            try:
                widths.append(analyze_counter_width(str(asm_path), hw_loop))
            except Exception:
                continue

        if not widths:
            # No candidates seen: conservative default matches the
            # legacy "all dynamic counts" behaviour (16 bits).
            return WidthDecision(counter_width=16)

        chosen = max(widths)
        chosen = max(self.minimum, min(self.maximum, chosen))

        # If chosen >= maximum, treat as "don't narrow" — the
        # decision's default value of 32 will signal that to the
        # renderer.  Otherwise stash the chosen width.
        if chosen >= self.maximum:
            return WidthDecision()
        return WidthDecision(counter_width=chosen)
