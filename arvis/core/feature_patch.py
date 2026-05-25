"""Generic patch that applies declarative feature removals.

The :class:`FeatureRemovalPatch` is the target-agnostic
counterpart to target-specific patches like
:class:`PrunePatch`.  It consumes a :class:`PruneDecision` whose
``unused_features`` field names the features to remove, looks
each one up in the target's ``features`` tuple, and dispatches
its :class:`RemovalAction` and :class:`CustomAction` items.

This patch is what makes "add a new core in 30 lines" possible:
a target's :meth:`render_prune_decision` simply returns
``[FeatureRemovalPatch(decision, target)]`` and the framework
handles the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from arvis.core.feature import CustomAction, RemovalAction
from arvis.core.rtl_patch import RTLPatch
from arvis.core.rtl_primitives import dispatch

if TYPE_CHECKING:
    from arvis.core.rtl_patch import RTLWorkspace
    from arvis.core.strategy import PruneDecision
    from arvis.core.target import TargetCore

logger = logging.getLogger(__name__)


@dataclass
class FeatureRemovalPatch(RTLPatch):
    """Apply declarative feature removals to a workspace.

    Parameters
    ----------
    decision:
        The :class:`PruneDecision` whose ``unused_features``
        field drives the work.  Other fields are ignored —
        feature-based pruning is a complete alternative to the
        legacy field-based path.
    target:
        The :class:`TargetCore` whose ``features`` tuple is
        consulted.  The patch holds a reference rather than
        copying the features so it works correctly even when a
        target lazily computes its feature surface.

    The patch silently no-ops when ``unused_features`` is empty
    or the target has no ``features`` (Patch-level
    invariant: never crash the pipeline).
    """

    decision: PruneDecision
    target: TargetCore

    @property
    def label(self) -> str:
        n = len(self.decision.unused_features)
        return f"FeatureRemovalPatch({n} features)"

    def apply(self, workspace: RTLWorkspace) -> None:
        unused = self.decision.unused_features
        if not unused:
            return

        features = getattr(self.target, "features", ())
        if not features:
            logger.warning(
                "FeatureRemovalPatch: target %r has no declarative "
                "features but decision lists %d unused features",
                getattr(self.target, "name", "?"),
                len(unused),
            )
            return

        by_name = {f.name: f for f in features}

        actions_applied = 0
        actions_failed = 0

        for fname in unused:
            feature = by_name.get(fname)
            if feature is None:
                logger.warning(
                    "FeatureRemovalPatch: unknown feature %r (not in target.features)",
                    fname,
                )
                continue

            for action in feature.removal_actions:
                ok = self._apply_action(action, workspace, feature.name)
                if ok:
                    actions_applied += 1
                else:
                    actions_failed += 1

        logger.info(
            "FeatureRemovalPatch: %d/%d actions applied across %d features",
            actions_applied,
            actions_applied + actions_failed,
            len(unused),
        )

    def _apply_action(
        self,
        action: object,
        workspace: RTLWorkspace,
        feature_name: str,
    ) -> bool:
        """Apply one action; route between RemovalAction and CustomAction."""
        if isinstance(action, RemovalAction):
            # Optional condition: skip when false
            if action.condition is not None:
                env = dict(self.decision.feature_flags)
                try:
                    if not eval(action.condition, {"__builtins__": {}}, env):
                        logger.debug(
                            "FeatureRemovalPatch[%s]: skipping action (condition %r false)",
                            feature_name,
                            action.condition,
                        )
                        return True  # not a failure: condition simply gated it
                except Exception:
                    logger.exception(
                        "FeatureRemovalPatch[%s]: condition %r raised",
                        feature_name,
                        action.condition,
                    )
                    return False

            # Resolve callable values (e.g. SHRINK_PARAM with a
            # profile-derived width).  The framework currently
            # supports literal values; callable resolution is a
            # future extension.
            value = action.value

            try:
                return dispatch(workspace, action.kind, action.file, action.target, value)
            except Exception:
                logger.exception(
                    "FeatureRemovalPatch[%s]: dispatch raised for %s",
                    feature_name,
                    action.kind,
                )
                return False

        if isinstance(action, CustomAction):
            try:
                action.apply(workspace, self.decision)
            except Exception:
                logger.exception(
                    "FeatureRemovalPatch[%s]: custom action %r raised",
                    feature_name,
                    action.name,
                )
                return False
            return True

        logger.warning(
            "FeatureRemovalPatch[%s]: unknown action type %s",
            feature_name,
            type(action).__name__,
        )
        return False


__all__ = ["FeatureRemovalPatch"]
