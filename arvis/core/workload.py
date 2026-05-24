"""Workload model.

A :class:`Workload` is *what the pipeline is specializing for*: a
set of source files plus the metadata needed to compile, run, and
verify them.  A :class:`WorkloadProfile` is the static-analysis
summary strategies use to make decisions.

Today the equivalent information is scattered across
:data:`config.BENCHMARKS` (a flat dict) and various ad-hoc
fields on :class:`pipeline.context.PipelineContext`.  Phase 2
migrates the per-benchmark dict entries to ``Workload`` instances
in ``workloads/*.py`` (or YAML).  Phase 1 just defines the shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
)

if TYPE_CHECKING:
    from arvis.core.toolchain import Toolchain


# ─── Build recipe ──────────────────────────────────────────────────


@dataclass(frozen=True)
class BuildRecipe:
    """How to build a workload from sources.

    Most workloads will use a Makefile-driven recipe today.  Future
    workloads might use Cmake or Bazel; we abstract over the verb
    so the toolchain doesn't need to know the build system.

    Attributes
    ----------
    kind:
        ``"makefile"``, ``"cmake"``, ``"manual"``, ...
    working_dir:
        Directory in which the recipe is invoked.
    targets:
        Build targets to invoke (e.g. ``("all",)``, ``("hex",)``).
    env:
        Extra environment variables to set during the build.
    """

    kind: str = "makefile"
    working_dir: Path | None = None
    targets: tuple[str, ...] = ("all",)
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedResult:
    """The reference outcome a workload should produce.

    Attributes
    ----------
    exit_status:
        ``0`` for ``EXIT_SUCCESS`` style firmware exits.
    expected_cycles:
        Optional baseline cycle count.  Reporters may flag
        regressions when actual cycles drift beyond a threshold.
    expected_output:
        Optional textual output to match.  Used by workloads that
        emit data via UART instead of a status code.
    """

    exit_status: int = 0
    expected_cycles: int | None = None
    expected_output: str | None = None


# ─── Workload profile (analysis input) ─────────────────────────────


@dataclass(frozen=True)
class WorkloadProfile:
    """Static-analysis summary of a workload.

    A profile is computed by :meth:`Workload.profile` after the
    workload has been compiled at least once (so disassembly is
    available).  Strategies consume profiles to decide their
    decisions.

    Attributes
    ----------
    instr_histogram:
        ``{mnemonic: count}`` over the disassembly.
    dynamic_counts:
        ``{mnemonic: count}`` over a *runtime* trace, when
        available.  Empty when no trace exists.
    cycle_profile:
        ``{function_name: cycles}`` from a profiling run.  Empty
        when not profiled.
    loops:
        Opaque loop records used by hwloop strategies.
    elf_paths:
        Paths of every compiled variant relevant to width
        analysis (baseline, fused-only, hwloop-only, fused+hwloop).
        Width strategies take the max of analyses across these so
        the same RTL fits every variant the pipeline will emit.
    extra:
        Free-form extension point for new strategies that need
        new analyses.
    """

    instr_histogram: Mapping[str, int] = field(default_factory=dict)
    dynamic_counts: Mapping[str, int] = field(default_factory=dict)
    cycle_profile: Mapping[str, int] = field(default_factory=dict)
    loops: tuple[Any, ...] = field(default_factory=tuple)
    elf_paths: tuple[Path, ...] = field(default_factory=tuple)
    extra: Mapping[str, object] = field(default_factory=dict)


# ─── Workload abstract base ────────────────────────────────────────


class Workload(ABC):
    """A program (or program set) the pipeline specializes for.

    Subclasses bind to concrete projects (e.g.
    ``UDWorkload``, ``Crc32Workload``, an ``EmbenchSuite`` member).
    Workloads expose source files, compile flags, and a builder; the
    pipeline does the actual compiling via the :class:`Toolchain`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier used in reports (e.g. ``"ud"``)."""

    @property
    @abstractmethod
    def sources(self) -> list[Path]:
        """All source files (C, ASM, headers) the workload needs."""

    @property
    @abstractmethod
    def cflags(self) -> list[str]:
        """Compiler flags specific to this workload.

        These are merged with the toolchain's default cflags; they
        do NOT replace them.  Use them for workload-specific
        optimisation hints (``-O2``, ``-funroll-loops``) or
        feature toggles (``-DUSE_FOO``).
        """

    @property
    @abstractmethod
    def build_recipe(self) -> BuildRecipe:
        """How to invoke the build."""

    @property
    @abstractmethod
    def expected(self) -> ExpectedResult:
        """The reference behaviour for verification."""

    @abstractmethod
    def profile(self, toolchain: Toolchain) -> WorkloadProfile:
        """Analyse the workload and return a :class:`WorkloadProfile`.

        Implementations typically (a) compile a baseline binary,
        (b) disassemble it, (c) tally instruction usage, and
        (d) detect loops.  Profiling is allowed to invoke the
        toolchain; the result should be cached by the workload to
        avoid repeated work.
        """


# ─── Workload suite (for multi-program targets) ────────────────────


@dataclass
class WorkloadSuite:
    """A collection of workloads that share a target.

    Useful for benchmark suites (Embench, MiBench) and for the
    multi-program Kyber-style tests.  The pipeline iterates the
    suite, running each workload through the same target and
    strategies.
    """

    name: str
    workloads: list[Workload] = field(default_factory=list)

    def __iter__(self) -> Iterator[Workload]:
        return iter(self.workloads)
