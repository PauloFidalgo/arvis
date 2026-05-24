"""Toolchain abstraction.

A :class:`Toolchain` compiles workload sources into ELF/hex
artifacts that the verifier can run.  It also provides
disassembly (used by analyses) and an extension point for teaching
the compiler about custom fused operations or hardware-loop
intrinsics.

Phase 1 ships only an interface plus a stub.  The legacy code in
:mod:`pipeline.gcc_compile` and :mod:`codegen.gcc` is treated as
the de-facto implementation; Phase 2 wraps it as a concrete
``GCCToolchain``.

The interface is shaped neutrally between GCC and LLVM, but a real
LLVM port would still need significant work: GCC's ``.md`` patterns
and LLVM's ``.td`` files are different formalisms.  We document the
LLVM port as a future task in the migration playbook
(``docs/portability.md``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:
    from arvis.core.isa import ISADescriptor
    from arvis.core.strategy import FusionDecision, LoopDecision
    from arvis.core.workload import Workload


# ─── Compiled artifact value object ────────────────────────────────


@dataclass(frozen=True)
class CompiledArtifact:
    """The output of a toolchain compile step.

    Carries everything the verifier and downstream analyses need.
    Optional fields may be ``None`` for toolchains that don't
    produce them (e.g. ``hex_path`` is RV-specific).
    """

    elf_path: Path
    hex_path: Optional[Path] = None
    asm_path: Optional[Path] = None
    map_path: Optional[Path] = None
    cflags_used: Tuple[str, ...] = field(default_factory=tuple)


# ─── Toolchain interface ───────────────────────────────────────────


class Toolchain(ABC):
    """Abstract toolchain.

    A toolchain is parameterised by the :class:`ISADescriptor` it
    targets and by any toolchain-specific knobs (Docker image,
    binary location, etc.) supplied by the concrete subclass.

    Strategies do NOT call the toolchain directly; the pipeline does.
    Strategies only register intent (e.g. "this fused op exists")
    and the pipeline lowers that into toolchain-specific actions.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (``"gcc"``, ``"llvm"``)."""

    @property
    @abstractmethod
    def isa(self) -> "ISADescriptor":
        """The ISA this toolchain instance targets."""

    # ── Compilation ────────────────────────────────────────────────
    @abstractmethod
    def compile(
        self,
        workload: "Workload",
        cflags: Sequence[str] = (),
        output_dir: Optional[Path] = None,
    ) -> CompiledArtifact:
        """Compile ``workload`` into an :class:`CompiledArtifact`.

        ``cflags`` are appended to whatever defaults the workload
        already supplies (it does NOT replace them).  ``output_dir``
        defaults to the workload's build directory.
        """

    @abstractmethod
    def assemble(
        self,
        sources: Sequence[Path],
        cflags: Sequence[str] = (),
        output: Optional[Path] = None,
    ) -> CompiledArtifact:
        """Assemble standalone ``.s`` files (no C front-end)."""

    @abstractmethod
    def disassemble(self, elf_path: Path) -> str:
        """Return objdump-style disassembly text.

        Used by analyses (instruction histogram, hwloop eligibility,
        etc.).  Must be deterministic for a given ELF.
        """

    # ── Extension points ───────────────────────────────────────────
    def supports(self, extension: str) -> bool:
        """Return True if the toolchain has the given extension
        support compiled in.

        Default falls back to the ISA descriptor; toolchains may
        override when they have features the standard ISA doesn't
        know about (e.g. a hwloop fork of GCC has ``hwloop``).
        """
        return self.isa.has(extension)

    def register_custom_ops(self, decision: "FusionDecision") -> None:
        """Teach the toolchain about new fused instructions.

        Subclasses implement by emitting peephole patterns / built-in
        patterns / intrinsics.  Default is a no-op so toolchains
        that don't yet support custom-op registration can still be
        used for the standard-ISA path.
        """
        return

    def register_hwloop(self, decision: "LoopDecision") -> None:
        """Teach the toolchain about the target's hwloop support.

        Default is a no-op for the same reason as above.
        """
        return
