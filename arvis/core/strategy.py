"""Optimization strategies and their typed decision results.

Strategy hierarchy
------------------

::

    OptimizationStrategy[D] (abstract)
      ├─ PruningStrategy   produces PruneDecision
      ├─ FusionStrategy    produces FusionDecision
      ├─ LoopStrategy      produces LoopDecision
      └─ WidthStrategy     produces WidthDecision

Each concrete strategy lives in ``strategies/`` and implements
:meth:`OptimizationStrategy.analyze`.  The pipeline runs strategies
in dependency order (fusion before pruning, pruning before
narrowing) and collects the resulting :class:`Decision` objects.

Decision objects are *immutable* value types.  The current contents
of each decision are minimal scaffolds that will be filled in as the
existing pipeline code is retrofitted in subsequent commits.  At
this stage of the migration their interfaces are:

- :class:`PruneDecision` — analogous to today's ``PruneConfig``.
- :class:`FusionDecision` — analogous to today's
  ``FusedOperation`` list.
- :class:`LoopDecision` — analogous to today's
  ``HWLoopCandidate`` selection plus counter/address widths.
- :class:`WidthDecision` — currently captures PC width and HWLP
  address width only.

The :meth:`Decision.render` method is the bridge between
target-agnostic strategy decisions and target-specific RTL edits.
A decision *describes* what should change; the target's ``emit_rtl``
turns that description into concrete :class:`RTLPatch` objects.  In
Phase 1 ``render`` is implemented as a no-op stub on each Decision;
Phase 3 will move the actual rendering logic out of
``codegen/rtl/`` into target-specific overrides.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Generic, List, Optional, Set, Tuple, TypeVar

# Forward references resolved at runtime to avoid circular imports.
# ``TargetCore`` lives in core.target; ``WorkloadProfile`` and
# ``Workload`` in core.workload; ``RTLPatch`` in core.rtl_patch.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile
    from arvis.core.rtl_patch import RTLPatch


# ─── Decision hierarchy ────────────────────────────────────────────


class Decision(ABC):
    """A typed result produced by an :class:`OptimizationStrategy`.

    Decisions are immutable value objects.  They carry only the data
    needed to describe what should change about the core; they do
    NOT know the target's RTL layout.  Lowering to per-file RTL
    edits is the target's job, performed via :meth:`render`.
    """

    @abstractmethod
    def render(self, target: "TargetCore") -> List["RTLPatch"]:
        """Translate this decision into a list of target-specific
        RTL patches.

        The default implementations in Phase 1 return an empty list
        (the existing ``RTLChangeSet.apply`` performs the rendering
        in-place); Phase 3 moves the rendering logic into target
        subclasses so a new core can be ported by overriding
        :meth:`TargetCore.render_decision`.
        """
        ...

    @property
    def name(self) -> str:
        """Stable identifier used in reports and logs."""
        return self.__class__.__name__


@dataclass(frozen=True)
class PruneDecision(Decision):
    """Which features to disable in the target core.

    The fields mirror today's :class:`codegen.rtl.rtl_pruning.PruneConfig`
    but in a target-agnostic form.  Strategies produce sets of
    feature names; the target's RTL emitter knows what each name
    means in terms of file edits.

    Attributes
    ----------
    removable_alu_ops:
        Names of ALU operations that can be removed (e.g.
        ``{"ALU_DIV", "ALU_REM"}``).
    removable_mul_modes:
        Names of multiplier modes that can be removed (e.g.
        ``{"MUL_DOT8", "MUL_DOT16"}``).
    removable_opcode_groups:
        Higher-level groups (e.g. ``{"PULP", "FP"}``).
    feature_flags:
        Free-form ``{name: enabled}`` toggles for misc. options
        (RF read port C, CSR labels, etc.).  Migrating strategies
        will keep these in sync with the legacy gate names so the
        target's emitter recognises them.
    used_instructions:
        Set of instructions actually observed in the workload — used
        by the target to leave their datapath enabled.
    target_payload:
        Phase 2 migration bridge.  Strategies that delegate to
        legacy code populate this with the underlying
        target-specific config object (e.g. the legacy
        :class:`codegen.rtl.rtl_pruning.PruneConfig`) so the
        target's render path can hand it directly to the legacy
        emitter without having to re-translate from the typed
        fields.  Phase 3 will remove this slot once all the
        relevant data is carried in typed Decision fields.
        Treat as opaque from outside the strategy/target pair.
    """

    removable_alu_ops: FrozenSet[str] = field(default_factory=frozenset)
    removable_mul_modes: FrozenSet[str] = field(default_factory=frozenset)
    removable_opcode_groups: FrozenSet[str] = field(default_factory=frozenset)
    feature_flags: Dict[str, bool] = field(default_factory=dict)
    used_instructions: FrozenSet[str] = field(default_factory=frozenset)
    target_payload: Optional[Any] = None

    def render(self, target: "TargetCore") -> List["RTLPatch"]:
        # Phase 1: rendering is still done by the legacy
        # RTLChangeSet.apply path. Subsequent commits move the logic
        # into TargetCore.render_decision so this becomes:
        #   return target.render_prune_decision(self)
        return []


@dataclass(frozen=True)
class FusionDecision(Decision):
    """Custom fused instructions to add to the core.

    The list of fused operations carries everything needed to (a)
    teach the compiler about the new instruction (peephole / .md /
    intrinsic), (b) generate the decoder case in RTL, and (c)
    allocate an opcode/funct3/funct7 slot.  The opcode allocation is
    finalized by the pipeline AFTER all strategies run, so concrete
    fused-op records may carry placeholder encodings until the
    pipeline's encoding pass.

    Attributes
    ----------
    fused_ops:
        Ordered list of fused operations.  Order matters because
        opcode slots are filled deterministically.
    target_payload:
        Phase 2 migration bridge -- mirrors :attr:`PruneDecision.target_payload`.
        For :class:`NGramFusion` this carries the legacy fusion
        registry / encoding info needed by the cv32e40p RTL emitter.
    """

    # We use a tuple rather than a Python list because Decision is
    # frozen.  Items are opaque "FusedOperation" records — Phase 1
    # carries them as-is via the existing dataclass; Phase 3 will
    # define a target-agnostic FusedOp type.
    fused_ops: Tuple[Any, ...] = field(default_factory=tuple)
    target_payload: Optional[Any] = None

    def render(self, target: "TargetCore") -> List["RTLPatch"]:
        return []


@dataclass(frozen=True)
class LoopDecision(Decision):
    """Hardware loop configuration and patched-loop set.

    Attributes
    ----------
    nest_depth:
        Number of hardware loop levels enabled (``0`` disables).
    counter_width:
        Width in bits of the loop-counter register
        (``HW_LOOP_CNT_WIDTH``).
    addr_width:
        Width in bits of the LP_start / LP_end / LP_last registers
        (``HWLP_ADDR_WIDTH``).
    patched_loops:
        Opaque records describing which loops were patched.  Used
        by the assembler-level patcher and by reports.
    target_payload:
        Phase 2 migration bridge -- mirrors :attr:`PruneDecision.target_payload`.
        Populated by :class:`CV32E40PHWLoop` with the legacy
        encoding / registry data needed by the cv32e40p RTL emitter.
    """

    nest_depth: int = 0
    counter_width: int = 32
    addr_width: int = 32
    patched_loops: Tuple[Any, ...] = field(default_factory=tuple)
    target_payload: Optional[Any] = None

    def render(self, target: "TargetCore") -> List["RTLPatch"]:
        return []


@dataclass(frozen=True)
class WidthDecision(Decision):
    """Datapath-width narrowing decisions.

    A single ``WidthDecision`` may describe several widths because
    they all share the same trigger (the binary's text size) and we
    want to apply them as a coordinated set.

    Attributes
    ----------
    pc_width:
        Main pipeline PC width.  ``0`` disables narrowing
        (i.e. keep the default 32).
    hwlp_addr_width:
        HW-loop register addr width (subsumes
        ``LoopDecision.addr_width`` for emission, but we keep both
        because the loop strategy may set it earlier).  Default
        ``32`` means no narrowing.
    counter_width:
        HW-loop counter register width.  Default ``32`` means no
        narrowing.
    """

    pc_width: int = 0
    hwlp_addr_width: int = 32
    counter_width: int = 32

    def render(self, target: "TargetCore") -> List["RTLPatch"]:
        return []


# ─── Strategy hierarchy ────────────────────────────────────────────

D = TypeVar("D", bound=Decision)


class OptimizationStrategy(ABC, Generic[D]):
    """Abstract base for any optimization strategy.

    A strategy is *stateless* with respect to a single pipeline run:
    its :meth:`analyze` method takes the workload, the workload's
    profile, and the target, and returns a :class:`Decision`.  No
    files are written, no RTL is emitted — strategies only *decide*.

    The pipeline calls strategies in a dependency-respecting order
    (typically fusion -> pruning -> width narrowing).  If a
    strategy is not :meth:`applicable` to the current workload (e.g.
    a hwloop strategy on a workload with no eligible loops) the
    pipeline skips it.

    Subtype protocol
    ----------------
    Concrete strategies should subclass one of the four sub-abstracts
    (:class:`PruningStrategy`, :class:`FusionStrategy`,
    :class:`LoopStrategy`, :class:`WidthStrategy`) rather than
    :class:`OptimizationStrategy` directly.  Subtype membership lets
    the pipeline order-by-role and lets reporters group strategies
    by category.
    """

    @property
    def name(self) -> str:
        """Stable identifier used in reports and logs.

        Defaults to the class name; concrete strategies may override
        for human-readable names like ``"usage-driven-pruning"``.
        """
        return self.__class__.__name__

    def applicable(self, workload: "Workload", target: "TargetCore") -> bool:
        """Return True if this strategy can run on the given inputs.

        Default: always applicable.  Subclasses override when their
        analysis is meaningful only for some workloads (e.g. a
        hwloop strategy that needs at least one eligible loop, or a
        DSP fusion strategy that needs a multiplier in the target).
        """
        return True

    @abstractmethod
    def analyze(
        self,
        workload: "Workload",
        profile: "WorkloadProfile",
        target: "TargetCore",
    ) -> D:
        """Analyse the workload and produce a typed decision.

        Implementations MUST be deterministic for a given
        ``(workload, profile, target)`` triple — the pipeline relies
        on this for caching and for the regression discipline that
        guards the migration.
        """
        ...


# ─── Sub-abstracts: one per strategy role ──────────────────────────


class PruningStrategy(OptimizationStrategy[PruneDecision]):
    """Decides which core features can be safely removed."""


class FusionStrategy(OptimizationStrategy[FusionDecision]):
    """Decides which custom fused instructions should be added."""


class LoopStrategy(OptimizationStrategy[LoopDecision]):
    """Decides whether/how to use the core's hardware-loop unit."""


class WidthStrategy(OptimizationStrategy[WidthDecision]):
    """Decides datapath-width narrowing (PC, HWLP addr, etc.)."""
