"""Main-pipeline PC width narrowing.

Wraps :func:`apply_pc_width.analyze_pc_width` (the validated standalone
analyzer) as a :class:`WidthStrategy`.  The strategy reads every
ELF in the workload profile, picks the smallest width that holds
the largest binary's text section, and returns a
:class:`WidthDecision` carrying that ``pc_width``.

The actual RTL surgery (rewriting parameters, narrowing internal
PC signals, inserting boundary casts) still happens through the
legacy :class:`pipeline.rtl_changeset.RTLChangeSet` path during
Phase 1.  Phase 3 will move that logic into
``targets/cv32e40p/render.py`` so the strategy's
:meth:`WidthDecision.render` actually produces patches.

This is the smallest-scope retrofit and serves as the walking
skeleton for all subsequent strategy ports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arvis.core.strategy import WidthStrategy, WidthDecision

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


class PCWidthNarrowing(WidthStrategy):
    """Auto-narrow the main pipeline PC to ``clog2(text_end) + margin``.

    Parameters
    ----------
    margin_bits:
        Extra bits beyond the strictly-required width.  Guards
        against re-build address shifts.  Default ``1`` matches the
        validated value in the legacy ``analyze_pc_width``.
    minimum:
        Lower bound.  ``8`` is the smallest realistic value because
        the cv32e40p reset vector ``BOOT_ADDR=0x80`` already needs
        8 bits.
    maximum:
        Upper bound; setting at this value disables narrowing.
    """

    def __init__(
        self, *, margin_bits: int = 1, minimum: int = 8, maximum: int = 32
    ) -> None:
        self.margin_bits = margin_bits
        self.minimum = minimum
        self.maximum = maximum

    @property
    def name(self) -> str:
        return "pc-width-narrowing"

    def applicable(self, workload: "Workload", target: "TargetCore") -> bool:
        """Only run if the target exposes a ``PC_WIDTH`` parameter.

        Targets that don't have a parameterised PC width (e.g. a
        future ibex port that hard-codes 32-bit PC throughout) skip
        this strategy automatically.
        """
        return target.parameter("PC_WIDTH") is not None

    def analyze(
        self,
        workload: "Workload",
        profile: "WorkloadProfile",
        target: "TargetCore",
    ) -> WidthDecision:
        # Late import: keeps core/ free of dependencies on
        # apply_pc_width.py, which is repo-root rather than a
        # standard package.  When apply_pc_width moves into
        # ``targets/cv32e40p/`` this import path will follow.
        from arvis.pipeline.pc_width import analyze_pc_width

        widths = []
        for elf in profile.elf_paths:
            if elf is None:
                continue
            try:
                widths.append(analyze_pc_width(str(elf), self.margin_bits))
            except Exception:
                # A missing or unreadable ELF should not abort the
                # whole strategy run; skip it and rely on whatever
                # other variants are present.  If every variant
                # fails, the resulting width is 0 (= disabled),
                # which preserves correctness.
                continue

        if not widths:
            return WidthDecision(pc_width=0)

        chosen = max(widths)
        # Clamp to the strategy's configured range.  The target
        # parameter's own min/max can clamp further at emit time.
        chosen = max(self.minimum, min(self.maximum, chosen))

        # If we picked the maximum (i.e. couldn't narrow), report
        # ``0`` so the renderer treats it as a no-op.  This matches
        # the convention used by ``RTLChangeSet.pc_width``.
        pc_width = 0 if chosen >= self.maximum else chosen

        return WidthDecision(pc_width=pc_width)
