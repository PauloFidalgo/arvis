"""Standard variant configurations for cv32e40p.

The five variants below mirror the legacy
:func:`pipeline.runner.run_pipeline` if-tree:

- ``BASELINE``:       no decisions, original RTL.
- ``PRUNED``:         {PruneDecision}.
- ``FUSED_PRUNED``:   {PruneDecision, FusionDecision}.
- ``HWLOOP_PRUNED``:  {PruneDecision, LoopDecision}.
- ``ALL``:            {PruneDecision, FusionDecision, LoopDecision,
                       WidthDecision}.

These are simple data; users compose them into a :class:`Pipeline`
via the ``variants=`` constructor argument.  Custom variants are
just additional :class:`VariantConfig` instances -- nothing is
hard-wired into the pipeline's variant set.

Note on per-variant decisions
-----------------------------
The legacy runner.py recomputed the prune config for each variant
(baseline binary -> step 2 prune config; fused binary -> step 3
prune config; hwloop-only binary -> step 4 prune config;
fused+hwloop -> step 5 prune config).  The new model runs each
strategy ONCE; all variants receive the same decision set, filtered
by :attr:`VariantConfig.decision_kinds`.

For paper-quality cycle/area parity with the legacy path, callers
who need per-variant decisions should run multiple :class:`Pipeline`
invocations -- one per variant -- with strategies parameterised on
the right binary.  Phase 2.8 introduces that mechanism inside the
runner replacement.  Until then, the standard variants below all
share the same set of strategy outputs (the FUSED variant's
decisions, since the master changeset uses those for the ``all``
variant in the legacy code).
"""

from __future__ import annotations

from arvis.core.pipeline import VariantConfig


BASELINE = VariantConfig(
    label="baseline",
    decision_kinds=frozenset(),
    hex_source="baseline",
)
"""No decisions -- emits the original cv32e40p RTL unchanged."""


PRUNED = VariantConfig(
    label="pruned",
    decision_kinds=frozenset({"PruneDecision"}),
    hex_source="baseline",
)
"""Pruning only.  Runs the original (non-fused) binary against the
pruned RTL.  Used to attribute area savings to pruning alone."""


FUSED_PRUNED = VariantConfig(
    label="fused_pruned",
    decision_kinds=frozenset({"PruneDecision", "FusionDecision"}),
    hex_source="fused_only",
)
"""Pruning + fusion.  Runs the fused binary against the
fused+pruned RTL.  Excludes hwloop."""


HWLOOP_PRUNED = VariantConfig(
    label="hwloop_pruned",
    decision_kinds=frozenset({"PruneDecision", "LoopDecision"}),
    hex_source="hwloop_only",
)
"""Pruning + hwloop.  Runs the hwloop-only binary against the
hwloop+pruned RTL.  Excludes fusion."""


ALL = VariantConfig(
    label="all",
    decision_kinds=frozenset(
        {"PruneDecision", "FusionDecision", "LoopDecision", "WidthDecision"}
    ),
    hex_source="fused_hwloop",
)
"""Everything: pruning + fusion + hwloop + width narrowing.  Runs
the fused+hwloop binary against the fully specialised RTL."""


# Convenience: the canonical ordering of the standard set.
STANDARD_VARIANTS = (BASELINE, PRUNED, FUSED_PRUNED, HWLOOP_PRUNED, ALL)


__all__ = [
    "BASELINE",
    "PRUNED",
    "FUSED_PRUNED",
    "HWLOOP_PRUNED",
    "ALL",
    "STANDARD_VARIANTS",
]
