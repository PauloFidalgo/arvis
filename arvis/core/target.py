"""Target core abstraction.

A :class:`TargetCore` is the *thing being specialized*.  It owns:

- The source RTL location (template tree to copy from)
- The synthesizable file list
- The parameter surface (HW_LOOP, CNT_WIDTH, PC_WIDTH, etc.)
- The encoding space for custom operations
- The reset vector, memory layout, and testbench
- The "render" methods that translate target-agnostic
  :class:`core.strategy.Decision` objects into target-specific RTL
  patches.

Exactly one concrete subclass exists today (``CV32E40P``); the
interface is shaped so adding a new core (e.g. ``IbexCore``,
``KelvinCore``) is a matter of subclassing and providing the
target-specific renderings.

The Phase 1 ``TargetCore`` is *abstract only* — concrete subclasses
land in subsequent commits as the existing cv32e40p code is
factored out from ``codegen/rtl/`` into ``targets/cv32e40p/``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
)

if TYPE_CHECKING:
    from arvis.core.isa import ISADescriptor
    from arvis.core.rtl_patch import RTLPatch, RTLWorkspace
    from arvis.core.strategy import (
        Decision,
        FusionDecision,
        LoopDecision,
        PruneDecision,
        WidthDecision,
    )


# ─── Core parameter surface ────────────────────────────────────────


@dataclass(frozen=True)
class CoreParameter:
    """A single SystemVerilog parameter that ARVIS may tune.

    Attributes
    ----------
    name:
        The parameter name as it appears in the RTL (e.g.
        ``"HW_LOOP"``, ``"PC_WIDTH"``).
    default:
        Default value used when no strategy overrides it.
    minimum:
        Lower bound that any strategy may set.  ``None`` means no
        bound.
    maximum:
        Upper bound that any strategy may set.  ``None`` means no
        bound.
    description:
        Free-form human description for reports.
    """

    name: str
    default: int
    minimum: int | None = None
    maximum: int | None = None
    description: str = ""

    def clamp(self, value: int) -> int:
        """Constrain ``value`` to the parameter's legal range."""
        if self.minimum is not None and value < self.minimum:
            return self.minimum
        if self.maximum is not None and value > self.maximum:
            return self.maximum
        return value


# ─── Encoding space for custom operations ──────────────────────────


@dataclass(frozen=True)
class OpcodeSlot:
    """One fixed-width slot in an opcode space.

    A custom fused operation is assigned exactly one slot.  Slots
    carry the bit fields the target's instruction format requires
    (RISC-V R/R4-type uses opcode + funct3 + funct7 / funct2; other
    families would carry whatever fields are relevant).
    """

    opcode: int
    funct3: int | None = None
    funct2: int | None = None
    funct7: int | None = None
    label: str = ""  # human-readable, e.g. "CUSTOM_0/funct3=001"


@dataclass
class OpcodeSpace:
    """Available encoding slots for custom operations.

    The space tracks which slots are still free; ``allocate()``
    returns the next free slot or raises if exhausted.  Concrete
    targets pre-populate the space with the slots they reserve for
    custom instructions (e.g. cv32e40p uses CUSTOM_0 / CUSTOM_1 R4
    slots).

    This is mutable (slots are consumed as fused ops are assigned)
    but a single ``OpcodeSpace`` instance lives only for the
    duration of one pipeline run.
    """

    name: str
    available: list[OpcodeSlot] = field(default_factory=list)
    consumed: list[OpcodeSlot] = field(default_factory=list)

    def allocate(self) -> OpcodeSlot:
        """Return the next free slot.

        Raises :class:`RuntimeError` if no slot is free; concrete
        strategies should check :attr:`available` before allocating
        en masse so they can fail with a friendly message.
        """
        if not self.available:
            raise RuntimeError(
                f"OpcodeSpace {self.name!r} exhausted ({len(self.consumed)} slots used)"
            )
        slot = self.available.pop(0)
        self.consumed.append(slot)
        return slot

    def free_slots(self) -> int:
        return len(self.available)


# ─── The target core abstraction ───────────────────────────────────


class TargetCore(ABC):
    """A core that ARVIS can specialize.

    A subclass commits to:

    - A name and an :class:`ISADescriptor`
    - The path to the source RTL templates
    - A list of synthesizable files (relative to the RTL root)
    - The parameter surface
    - The reset vector and memory layout
    - The encoding space for custom ops
    - The testbench used by the verifier
    - Renderings: how each :class:`Decision` becomes RTL patches

    Subclasses MUST be picklable and immutable in their identity:
    instances are passed across phases and must compare equal when
    they represent the same core.

    Phase 1 keeps :meth:`render_*` methods abstract so the
    implementation can land alongside the strategy retrofits without
    forcing a flag day.  In the meantime the legacy
    ``RTLChangeSet.apply`` continues to do the work.
    """

    # ── Identity ───────────────────────────────────────────────────
    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (``"cv32e40p"``, ``"ibex"``)."""

    @property
    @abstractmethod
    def isa(self) -> ISADescriptor:
        """The standard ISA implemented by this core."""

    # ── RTL surface ────────────────────────────────────────────────
    @property
    @abstractmethod
    def rtl_root(self) -> Path:
        """Path to the directory containing the RTL templates.

        The pipeline copies a fresh tree from here at the start of
        every variant emission.  Subdirectories
        (e.g. ``rtl/template/`` for parameterised hwloop overrides)
        are target-specific.
        """

    @abstractmethod
    def synthesizable_files(self) -> list[Path]:
        """The list of files that the synthesis flow should compile.

        Returned paths are relative to :attr:`rtl_root`.
        """

    # ── Parameters ─────────────────────────────────────────────────
    @abstractmethod
    def parameters(self) -> list[CoreParameter]:
        """Tunable parameters exposed to strategies.

        Strategies look up parameters by name and clamp values
        through :meth:`CoreParameter.clamp` before recording them in
        a decision.
        """

    def parameter(self, name: str) -> CoreParameter | None:
        """Convenience lookup by name."""
        for p in self.parameters():
            if p.name == name:
                return p
        return None

    # ── Encoding ───────────────────────────────────────────────────
    @abstractmethod
    def opcode_space(self) -> OpcodeSpace:
        """A fresh, fully-populated opcode space for one pipeline run.

        Concrete implementations construct a new ``OpcodeSpace`` on
        every call so two pipeline runs don't share allocation
        state.
        """

    # ── Memory layout ──────────────────────────────────────────────
    @property
    @abstractmethod
    def reset_vector(self) -> int:
        """Address the core fetches from on reset."""

    @property
    @abstractmethod
    def memory_layout(self) -> Mapping[str, tuple[int, int]]:
        """Symbolic ``{region: (start, size)}`` map.

        Used by analyses (e.g. PC width derivation) and by the
        linker-script generator.  Regions are at minimum
        ``"text"`` and ``"data"``.
        """

    # ── Testbench wiring ───────────────────────────────────────────
    @abstractmethod
    def testbench_dir(self) -> Path:
        """Path to the testbench used by the default verifier.

        The testbench may be inside :attr:`rtl_root` (cv32e40p ships
        an ``example_tb/``) or out-of-tree.
        """

    # ── Variants ───────────────────────────────────────────────────
    @property
    def standard_variants(self) -> tuple:
        """Canonical variant set for this target.

        Concrete targets override to expose their canonical
        variant tuple (e.g. cv32e40p exposes baseline / pruned /
        fused_pruned / hwloop_pruned / all).  The default returns
        an empty tuple, which makes
        :meth:`Pipeline.run_full_verification` a no-op for
        unconfigured targets.

        The return type is intentionally weak (``tuple``) to
        avoid a forward reference to
        :class:`core.pipeline.VariantConfig`; concrete overrides
        return ``tuple[VariantConfig, ...]``.
        """
        return ()

    # ── Decision rendering ─────────────────────────────────────────
    # In Phase 1 these are stubs.  Phase 3 fills them in by moving
    # codegen/rtl/* and codegen/hwloop/* logic into target subclasses.

    def render_decision(self, decision: Decision, workspace: RTLWorkspace) -> list[RTLPatch]:
        """Translate a single decision into RTL patches.

        Default dispatch by decision type.  Subclasses override the
        per-type ``render_*`` methods; this dispatcher needs no
        change unless a new decision class is added.
        """
        # Late import to avoid a top-level cycle.
        from arvis.core.strategy import (
            FusionDecision,
            LoopDecision,
            PruneDecision,
            WidthDecision,
        )

        if isinstance(decision, PruneDecision):
            return self.render_prune_decision(decision, workspace)
        if isinstance(decision, FusionDecision):
            return self.render_fusion_decision(decision, workspace)
        if isinstance(decision, LoopDecision):
            return self.render_loop_decision(decision, workspace)
        if isinstance(decision, WidthDecision):
            return self.render_width_decision(decision, workspace)
        raise TypeError(f"Unknown decision type: {type(decision).__name__}")

    def render_prune_decision(
        self, decision: PruneDecision, workspace: RTLWorkspace
    ) -> list[RTLPatch]:
        return []

    def render_fusion_decision(
        self, decision: FusionDecision, workspace: RTLWorkspace
    ) -> list[RTLPatch]:
        return []

    def render_loop_decision(
        self, decision: LoopDecision, workspace: RTLWorkspace
    ) -> list[RTLPatch]:
        return []

    def render_width_decision(
        self, decision: WidthDecision, workspace: RTLWorkspace
    ) -> list[RTLPatch]:
        return []
