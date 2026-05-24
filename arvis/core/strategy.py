"""Strategy and Decision contracts.

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

Decision hierarchy
------------------

Decisions are **Pydantic v2 BaseModel** subclasses (Phase 4.3
upgrade from plain ``@dataclass(frozen=True)``).  This buys:

* free runtime validation -- malformed inputs (e.g. negative
  ``used_regs_mask``) raise ``pydantic.ValidationError`` at
  construction;
* free JSON / YAML serialisation via
  :meth:`Decision.model_dump_json` and
  :meth:`Decision.model_validate_json`;
* explicit immutability via ``model_config = ConfigDict(
  frozen=True)``;
* a stable, machine-readable schema that can be exported with
  :meth:`Decision.model_json_schema`.

The model_config below establishes the project-wide invariants:

* ``frozen=True``         immutability after construction
* ``extra="forbid"``      reject unknown fields (typos surface)
* ``arbitrary_types_allowed=True``
                           lets us carry opaque legacy records
                           (``FusedOperation``,
                           ``HWLoopCandidate``) inside
                           :attr:`FusionDecision.fused_ops` /
                           :attr:`LoopDecision.patched_loops`
                           until Phase 5 produces typed
                           replacements.

The :meth:`Decision.render` method is the bridge between
target-agnostic strategy decisions and target-specific RTL edits.
A decision *describes* what should change; the target's
``render_*_decision`` turns that description into concrete
:class:`RTLPatch` objects.  Decision.render returns ``[]`` because
the actual rendering is performed by
:meth:`TargetCore.render_prune_decision` (and friends).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from arvis.core.rtl_patch import RTLPatch
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


logger = logging.getLogger(__name__)


# ─── Decision hierarchy ────────────────────────────────────────────


class Decision(BaseModel):
    """A typed result produced by an :class:`OptimizationStrategy`.

    Decisions are immutable Pydantic models (see ``model_config``
    below).  They carry only the data needed to describe what
    should change about the core; they do NOT know the target's
    RTL layout.  Lowering to per-file RTL edits is the target's
    job, performed via :meth:`render` (which delegates to
    ``TargetCore.render_*_decision``).
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
        # ``populate_by_name`` is off by default; we don't use
        # field aliases, so every Decision field name is its
        # canonical identifier in serialised output too.
    )

    def render(self, target: TargetCore) -> list[RTLPatch]:
        """Translate this decision into a list of target-specific
        RTL patches.

        The default implementation returns an empty list; concrete
        targets override ``TargetCore.render_<kind>_decision`` to
        produce the actual patches.  This base method exists only
        as a documented escape hatch for any caller that already
        holds a Decision and wants a target-agnostic noop.
        """
        return []

    @property
    def name(self) -> str:
        """Stable identifier used in reports and logs."""
        return self.__class__.__name__


class PruneDecision(Decision):
    """Which features to disable in the target core.

    The fields mirror today's
    :class:`codegen.rtl.rtl_pruning.PruneConfig` but in a
    target-agnostic form.  Strategies produce sets of feature names;
    the target's RTL emitter knows what each name means in terms of
    file edits.

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
        Set of instructions actually observed in the workload --
        used by the target to leave their datapath enabled.
    unused_registers:
        Registers (x0..x31 -> 0..31) that the workload never writes.
        Used by the target to gate the register-file write enables.
    used_regs_mask:
        Bitmask version of :attr:`unused_registers` for direct use
        in synthesis-parameter expressions.  Bit ``i`` set means
        register x``i`` IS used.  Validated to be non-negative
        (Phase 4.3).
    removable_csr_labels:
        CSR identifiers that can be pruned from the decoder.
    removable_csr_storage:
        CSR storage cells that can be removed (a tighter subset of
        :attr:`removable_csr_labels` covering only those whose
        backing storage isn't shared with another live CSR).
    target_overlay:
        Free-form ``{name: int}`` map for target-wide configuration
        knobs that the target emitter reads from the prune decision
        (e.g. ``corev_pulp``, ``fpu``, ``num_mhpmcounters``,
        ``debug_trigger_en``).  These are not "decisions" in the
        strict sense -- they're target capabilities the strategy
        observed and propagated.
    """

    removable_alu_ops: frozenset[str] = Field(default_factory=frozenset)
    removable_mul_modes: frozenset[str] = Field(default_factory=frozenset)
    removable_opcode_groups: frozenset[str] = Field(default_factory=frozenset)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    used_instructions: frozenset[str] = Field(default_factory=frozenset)
    unused_registers: tuple[int, ...] = Field(default_factory=tuple)
    used_regs_mask: int = 0xFFFFFFFF
    removable_csr_labels: frozenset[str] = Field(default_factory=frozenset)
    removable_csr_storage: frozenset[str] = Field(default_factory=frozenset)
    target_overlay: dict[str, int] = Field(default_factory=dict)

    @field_validator("used_regs_mask")
    @classmethod
    def _used_regs_mask_non_negative(cls, v: int) -> int:
        """The mask is a non-negative bitfield; negative values
        would be a sign-extension bug in the upstream analysis.
        """
        if v < 0:
            msg = (
                f"used_regs_mask must be non-negative, got {v}; "
                "this usually means an upstream signed-int promotion."
            )
            raise ValueError(msg)
        return v

    @field_validator("unused_registers")
    @classmethod
    def _registers_in_range(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        """RV32 has 32 architectural integer registers (x0..x31)."""
        for r in v:
            if not 0 <= r < 32:
                msg = f"unused_registers entries must be in [0, 32), got {r}"
                raise ValueError(msg)
        return v


class FusionDecision(Decision):
    """Custom fused instructions to add to the core.

    The list of fused operations carries everything needed to (a)
    teach the compiler about the new instruction (peephole / .md /
    intrinsic), (b) generate the decoder case in RTL, and (c)
    allocate an opcode/funct3/funct7 slot.  The opcode allocation is
    finalised by the pipeline AFTER all strategies run, so concrete
    fused-op records may carry placeholder encodings until the
    pipeline's encoding pass.

    Attributes
    ----------
    fused_ops:
        Ordered tuple of fused operations.  Order matters because
        opcode slots are filled deterministically.
    """

    # Items are opaque legacy ``FusedOperation`` records; the
    # ``arbitrary_types_allowed=True`` model_config setting lets
    # Pydantic accept them without instance-level validation.
    fused_ops: tuple[Any, ...] = Field(default_factory=tuple)


class LoopDecision(Decision):
    """Hardware loop configuration and patched-loop set.

    Attributes
    ----------
    nest_depth:
        Number of hardware loop levels enabled (``0`` disables).
        Must be non-negative.
    counter_width:
        Width in bits of the loop-counter register
        (``HW_LOOP_CNT_WIDTH``).  Must be in [1, 32].
    addr_width:
        Width in bits of the LP_start / LP_end / LP_last registers
        (``HWLP_ADDR_WIDTH``).  Must be in [1, 32].
    patched_loops:
        Opaque records describing which loops were patched.  Used
        by the assembler-level patcher and by reports.
    """

    nest_depth: int = 0
    counter_width: int = 32
    addr_width: int = 32
    patched_loops: tuple[Any, ...] = Field(default_factory=tuple)

    @field_validator("nest_depth")
    @classmethod
    def _nest_depth_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"nest_depth must be non-negative, got {v}")
        return v

    @field_validator("counter_width", "addr_width")
    @classmethod
    def _width_in_range(cls, v: int) -> int:
        if not 1 <= v <= 32:
            raise ValueError(f"hwloop width must be in [1, 32], got {v}")
        return v


class WidthDecision(Decision):
    """Datapath-width narrowing decisions.

    A single ``WidthDecision`` may describe several widths because
    they all share the same trigger (the binary's text size) and we
    want to apply them as a coordinated set.

    Attributes
    ----------
    pc_width:
        Main pipeline PC width.  ``0`` disables narrowing
        (i.e. keep the default 32).  Otherwise must be in [1, 32].
    hwlp_addr_width:
        HW-loop register addr width (subsumes
        ``LoopDecision.addr_width`` for emission, but we keep both
        because the loop strategy may set it earlier).  Must be in
        [1, 32]; ``32`` means no narrowing.
    counter_width:
        HW-loop counter register width.  Must be in [1, 32];
        ``32`` means no narrowing.
    fifo_depth:
        Prefetch buffer FIFO depth tuning.  ``0`` means leave the
        template default in place; positive values rewrite the
        ``localparam FIFO_DEPTH`` in
        ``cv32e40p_prefetch_buffer.sv``.  Determined by the
        bottleneck analysis stage, not by a width strategy proper
        -- but kept here because it shares the same "single
        integer that rewrites a parameter" emission path.
    """

    pc_width: int = 0
    hwlp_addr_width: int = 32
    counter_width: int = 32
    fifo_depth: int = 0

    @field_validator("pc_width")
    @classmethod
    def _pc_width_in_range(cls, v: int) -> int:
        # ``0`` is the documented "disable" sentinel; otherwise
        # must be a sensible bus width.
        if v != 0 and not 1 <= v <= 32:
            raise ValueError(f"pc_width must be 0 (disabled) or in [1, 32], got {v}")
        return v

    @field_validator("hwlp_addr_width", "counter_width")
    @classmethod
    def _hwloop_width_in_range(cls, v: int) -> int:
        if not 1 <= v <= 32:
            raise ValueError(f"hwloop width must be in [1, 32], got {v}")
        return v

    @field_validator("fifo_depth")
    @classmethod
    def _fifo_depth_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"fifo_depth must be non-negative, got {v}")
        return v


# ─── Strategy hierarchy ────────────────────────────────────────────


# PEP 695 type parameter syntax (Python 3.12+).  ``D`` is bound by
# :class:`Decision` so subclasses can override
# :meth:`OptimizationStrategy.analyze` with their concrete decision
# type (e.g. ``PruneDecision``) and keep the inherited contract.


class OptimizationStrategy[D: Decision](ABC):
    """Abstract base for any optimization strategy.

    A strategy is *stateless* with respect to a single pipeline run:
    its :meth:`analyze` method takes the workload, the workload's
    profile, and the target, and returns a :class:`Decision`.  No
    files are written, no RTL is emitted -- strategies only *decide*.

    The pipeline calls strategies in a dependency-respecting order
    (typically fusion -> pruning -> width narrowing).  If a strategy
    is not :meth:`applicable` to the current workload (e.g. a
    hwloop strategy on a workload with no eligible loops) the
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

    def applicable(self, workload: Workload, target: TargetCore) -> bool:
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
        workload: Workload,
        profile: WorkloadProfile,
        target: TargetCore,
    ) -> D:
        """Produce a typed :class:`Decision` for the given workload.

        The strategy is responsible for any auxiliary I/O it needs
        (objdump, GCC, etc.) but must not mutate the workload, the
        profile, the target, or any RTL.
        """
        ...


class PruningStrategy(OptimizationStrategy[PruneDecision]):
    """Marker subclass: produces a :class:`PruneDecision`."""


class FusionStrategy(OptimizationStrategy[FusionDecision]):
    """Marker subclass: produces a :class:`FusionDecision`."""


class LoopStrategy(OptimizationStrategy[LoopDecision]):
    """Marker subclass: produces a :class:`LoopDecision`."""


class WidthStrategy(OptimizationStrategy[WidthDecision]):
    """Marker subclass: produces a :class:`WidthDecision`."""


__all__ = [
    "Decision",
    "FusionDecision",
    "FusionStrategy",
    "LoopDecision",
    "LoopStrategy",
    "OptimizationStrategy",
    "PruneDecision",
    "PruningStrategy",
    "WidthDecision",
    "WidthStrategy",
]
