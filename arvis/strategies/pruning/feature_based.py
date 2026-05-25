"""Target-agnostic pruner driven by declarative feature surfaces.

The :class:`FeatureBasedPruner` consults
:attr:`TargetCore.features` and produces a :class:`PruneDecision`
carrying the names of features that should be removed for the
given workload.  No target-specific code lives in this strategy;
all target knowledge is in the target's :class:`CoreFeature`
list.

This complements (and will eventually replace) the existing
:class:`UsageDrivenPruner`, which encodes cv32e40p-specific
field semantics in :class:`PruneDecision`.

Usage
-----
::

    pruner = FeatureBasedPruner(overrides={"keep_branch_predictor": True})
    decision = pruner.analyze(workload, profile, target)
    # decision.unused_features = frozenset({"div_unit", "compressed", ...})

The decision is consumed by :class:`FeatureRemovalPatch` which
dispatches each feature's :class:`RemovalAction` /
:class:`CustomAction` items to the generic primitives in
:mod:`core.rtl_primitives`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from arvis.core.feature import resolve_removable
from arvis.core.strategy import PruneDecision, PruningStrategy

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile

logger = logging.getLogger(__name__)


class FeatureBasedPruner(PruningStrategy):
    """Walk ``target.features`` and report which ones are unused.

    Parameters
    ----------
    overrides:
        Operator overrides forwarded to each feature's
        ``keep_when`` callback.  Maps feature-name (or generic
        flag name) to a value the callback understands.  Example::

            overrides = {
                "keep_branch_predictor": True,   # CLI: --keep-branch-predictor
                "keep_div_unit": False,
            }

    verbose:
        When ``True``, logs the per-feature decision at INFO level.
        ``False`` (default) keeps logs quiet for batch runs.

    Notes
    -----
    The strategy is :meth:`applicable` to any target that
    advertises a non-empty ``features`` tuple.  Targets whose
    feature surface isn't yet declarative fall back to other
    pruners (e.g. :class:`UsageDrivenPruner`).
    """

    def __init__(
        self,
        *,
        overrides: dict[str, Any] | None = None,
        verbose: bool = False,
    ) -> None:
        self.overrides = overrides or {}
        self.verbose = verbose

    @property
    def name(self) -> str:
        return "feature-based-pruner"

    def applicable(self, workload: Workload, target: TargetCore) -> bool:
        # Only run when the target advertises features.  Targets
        # that haven't migrated yet are pruned by the legacy
        # UsageDrivenPruner instead.
        return bool(getattr(target, "features", ()))

    def analyze(
        self,
        workload: Workload,
        profile: WorkloadProfile,
        target: TargetCore,
    ) -> PruneDecision:
        del workload  # unused — analysis is profile + target only

        features = target.features
        unused, conflicts = resolve_removable(features, profile, self.overrides)

        for conflict in conflicts:
            logger.warning("FeatureBasedPruner: %s", conflict)

        if self.verbose:
            for feature in features:
                status = "REMOVE" if feature.name in unused else "keep"
                logger.info(
                    "  %-30s %s — %s",
                    feature.name,
                    status,
                    feature.description,
                )

        return PruneDecision(unused_features=frozenset(unused))


__all__ = ["FeatureBasedPruner"]
