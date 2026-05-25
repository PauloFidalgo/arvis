"""Declarative feature surface for target-agnostic pruning.

Phase 8 introduces a declarative model that lets each target
describe its prunable features as data instead of as code.

Vocabulary
----------

A :class:`CoreFeature` is one removable/configurable feature of
a core (the divider, the branch predictor, the compressed
decoder, ...).  It bundles:

* ``usage_predicate`` — when is this feature actually USED by
  the workload?  Returns ``True`` if the feature must stay.
* ``keep_when`` — user/operator override (``--keep-foo`` flag).
* ``removal_actions`` — declarative *or* custom actions to
  apply when the feature is removed.
* ``requires`` / ``incompatible_with`` — dependency edges.

A :class:`RemovalAction` is one declarative RTL transformation
parameterised by ``kind`` (an :class:`ActionKind` enum) plus
target/file/value triples.  The framework dispatches each kind
to a generic primitive in :mod:`core.rtl_primitives`.

A :class:`CustomAction` is the escape hatch: a callable that
receives the workspace and the decision, free to do anything
(typically used for mux retargeting, generate-block surgery, or
target-specific structural rewrites the declarative kinds can't
express).

Why declarative?
----------------

Adding a new core to ARVIS becomes "write a list of features".
The :class:`FeatureBasedPruner` strategy and the
:class:`FeatureRemovalPatch` consume the same declarative spec
for any target — no per-target pruner classes needed.

Example::

    DIV_FEATURE = CoreFeature(
        name="div_unit",
        description="ALU divider (DIV/DIVU/REM/REMU)",
        usage_predicate=lambda p: p.uses_any(("div", "divu", "rem", "remu")),
        removal_actions=(
            RemovalAction(
                kind=ActionKind.SET_PARAM,
                file="rtl/cv32e40p_top.sv",
                target="ENABLE_DIV",
                value=0,
            ),
        ),
    )
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arvis.core.rtl_patch import RTLWorkspace
    from arvis.core.strategy import PruneDecision
    from arvis.core.workload import WorkloadProfile


class ActionKind(Enum):
    """The declarative RTL-transformation kinds the framework supports.

    Each kind maps to one generic primitive in :mod:`core.rtl_primitives`.
    Targets that need a transformation outside this set should use a
    :class:`CustomAction` instead.
    """

    SET_PARAM = "set_param"
    """Set a SystemVerilog parameter to a literal value.

    ``target`` = parameter name (e.g. ``"ENABLE_DIV"``).
    ``value``  = the literal value (int, str, or SV expression).
    Idempotent.
    """

    SHRINK_PARAM = "shrink_param"
    """Narrow a SystemVerilog parameter (e.g. ``PC_WIDTH``).

    ``target`` = parameter name.
    ``value``  = the new width.  May be a callable
    ``(profile) -> int`` for derived widths.
    """

    REMOVE_PRAGMA_BLOCK = "remove_pragma_block"
    """Remove everything between ``// pragma X_BEGIN`` and
    ``// pragma X_END`` markers.

    ``target`` = pragma label (e.g. ``"PULP_ONLY_IMM"``).
    ``value``  unused.
    """

    REMOVE_OPCODE_CASE = "remove_opcode_case"
    """Remove a ``case`` arm in a decoder.

    ``target`` = opcode constant (e.g. ``"OPCODE_AMO"``).
    ``value``  unused.
    """

    REMOVE_SIGNAL = "remove_signal"
    """Remove a signal declaration and all its uses.

    ``target`` = signal name or glob (``"div_*"``).
    ``value``  unused.
    """

    REMOVE_INSTANCE = "remove_instance"
    """Remove a module instantiation block.

    ``target`` = instance name (e.g. ``"u_branch_predictor"``).
    ``value``  unused.
    """

    REPLACE_PATTERN = "replace_pattern"
    """Regex-based replacement.

    ``target`` = regex pattern (Python ``re`` syntax).
    ``value``  = replacement string.
    """

    AST_REMOVE_CASE_ITEMS = "ast_remove_case_items"
    """Remove case-arm items via pyslang AST manipulation.

    Generalises the legacy ``codegen/rtl/pyslang_pruning.py``
    routines (``prune_alu_cases``, ``prune_mult_cases``,
    ``prune_csr_cases``) into one declarative kind.

    ``target`` = the case-selector signal name (e.g.
    ``"operator_i"`` for the ALU's ``case (operator_i)``,
    ``"csr_addr"`` for the CSR mux).
    ``value``  = iterable of label names to remove (set, tuple,
    or list).  An item is removed iff ALL its labels are in
    this set; multi-label items with mixed
    used/unused labels are kept (partial-label removal would
    require deeper AST surgery and isn't supported).

    Why AST instead of regex?  Case items are highly variable:
    single-label, comma-separated multi-label, multi-line
    begin/end blocks, mixed comments — pyslang parses the
    structure so removal is precise and won't mangle adjacent
    arms.
    """


# ─── Action shapes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RemovalAction:
    """One declarative RTL transformation.

    Parameters
    ----------
    kind:
        Which generic primitive to invoke.
    file:
        Path relative to the target's ``rtl_root``.
    target:
        Identifier the primitive operates on (parameter name,
        signal pattern, regex, ...).  Semantics depend on
        ``kind``.
    value:
        Action-specific payload.  ``None`` for kinds that don't
        need a value.  May be a callable ``(profile) -> Any`` for
        derived values (e.g. shrink-param widths).
    condition:
        Optional Python expression evaluated against the
        decision's ``feature_flags``.  When present and false,
        the action is skipped.  Useful for actions that depend
        on whether other features are also being removed.

    Frozen to keep features hashable for caching.
    """

    kind: ActionKind
    file: str
    target: str
    value: Any = None
    condition: str | None = None


@dataclass(frozen=True)
class CustomAction:
    """Escape hatch: a callable RTL transformation.

    Used when no :class:`ActionKind` cleanly expresses the
    transformation (typical: mux retargeting, generate-block
    surgery, target-specific structural rewrites).

    The callable receives:

    * ``workspace``: the :class:`RTLWorkspace` to mutate
    * ``decision``:  the :class:`PruneDecision` driving this run

    Implementations live next to the feature definition in the
    target's directory — they don't leak into shared code.

    Parameters
    ----------
    name:
        Stable identifier for logging.
    apply:
        The callable.

    Frozen + the callable is captured; equality on
    :class:`CustomAction` is reference-based (frozen dataclasses
    of unhashable fields delegate ``__hash__`` to ``id``).
    """

    name: str
    apply: Callable[[RTLWorkspace, PruneDecision], None]


# Union type for the ``removal_actions`` field of CoreFeature.
Action = RemovalAction | CustomAction


# ─── CoreFeature ───────────────────────────────────────────────────


@dataclass(frozen=True)
class CoreFeature:
    """A removable/configurable feature of a target core.

    Parameters
    ----------
    name:
        Stable identifier (``"div_unit"``, ``"branch_predictor"``,
        ``"compressed"``, ...).  Used as a dict key in
        :class:`PruneDecision.feature_flags` and in dependency
        edges.
    description:
        Human-readable description.  Surfaces in reports.
    usage_predicate:
        ``(WorkloadProfile) -> bool``.  Returns ``True`` when
        the workload USES this feature (so it must stay).
        ``False`` means the feature is unused → removable.
    keep_when:
        Optional ``(WorkloadProfile, dict) -> bool``.  When
        present and returns ``True``, the feature stays even
        if ``usage_predicate`` says it's unused.  The ``dict``
        carries operator overrides (e.g. CLI flags
        ``{"keep_branch_predictor": True}``).
    removal_actions:
        Tuple of :class:`RemovalAction` / :class:`CustomAction`.
        Applied in order when the feature is removed.
    requires:
        Names of other features that MUST also be removed when
        this one is removed (cascade).  Useful for storage that
        becomes dead when its consumer is dropped.
    incompatible_with:
        Names of features that MUST stay if this one is
        removed.  Detected as conflicts at decision time.

    Frozen + tuple-typed actions to keep features hashable.
    """

    name: str
    description: str
    usage_predicate: Callable[[WorkloadProfile], bool]
    removal_actions: tuple[Action, ...] = ()
    keep_when: Callable[[WorkloadProfile, dict[str, Any]], bool] | None = None
    requires: tuple[str, ...] = ()
    incompatible_with: tuple[str, ...] = ()

    # Optional metadata for richer reports
    estimated_area_pct: float | None = None
    """Hint: how much core area this feature represents.  Used
    only for reporter ranking; the framework doesn't read it."""

    def is_unused(
        self,
        profile: WorkloadProfile,
        overrides: dict[str, Any] | None = None,
    ) -> bool:
        """Return True iff the feature should be removed.

        A feature is removed when:
        1. ``usage_predicate(profile)`` returns False, AND
        2. ``keep_when(profile, overrides)`` (if set) returns False.
        """
        if self.usage_predicate(profile):
            return False
        if self.keep_when is not None:
            return not self.keep_when(profile, overrides or {})
        return True


# ─── Dependency resolution ─────────────────────────────────────────


def resolve_removable(
    features: tuple[CoreFeature, ...] | list[CoreFeature],
    profile: WorkloadProfile,
    overrides: dict[str, Any] | None = None,
) -> tuple[set[str], list[str]]:
    """Compute the set of feature names to remove.

    Walks ``features``, applies ``is_unused`` to each, then
    propagates ``requires`` (cascade-remove) and detects
    ``incompatible_with`` conflicts.

    Returns
    -------
    (to_remove, conflicts)
        ``to_remove`` — names of features to remove (closed under
        ``requires``).
        ``conflicts`` — human-readable strings describing any
        ``incompatible_with`` violations encountered.

    Conflicts don't raise; the caller decides whether to
    abort or warn.  This keeps the strategy non-aborting (a
    pipeline invariant: strategies never crash the pipeline).
    """
    overrides = overrides or {}
    by_name = {f.name: f for f in features}
    to_remove: set[str] = set()
    conflicts: list[str] = []

    # Phase 1: direct unused-feature detection.
    for feature in features:
        if feature.is_unused(profile, overrides):
            to_remove.add(feature.name)

    # Phase 2: cascade via ``requires`` until fixed point.
    changed = True
    while changed:
        changed = False
        for name in list(to_remove):
            feature = by_name.get(name)
            if feature is None:
                continue
            for req in feature.requires:
                if req not in to_remove and req in by_name:
                    to_remove.add(req)
                    changed = True

    # Phase 3: ``incompatible_with`` detection.
    for name in to_remove:
        feature = by_name.get(name)
        if feature is None:
            continue
        for incompat in feature.incompatible_with:
            if incompat in to_remove:
                conflicts.append(
                    f"{name!r} marked incompatible with {incompat!r} "
                    f"but both are scheduled for removal"
                )

    return to_remove, conflicts


__all__ = [
    "Action",
    "ActionKind",
    "CoreFeature",
    "CustomAction",
    "RemovalAction",
    "resolve_removable",
]
