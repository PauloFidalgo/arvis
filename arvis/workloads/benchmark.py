"""Single-benchmark workload wrapping the ``targets/benchmarks/<name>/`` layout.

Each ARVIS benchmark lives in a directory containing:

- One or more ``.c`` / ``.h`` source files (the program under
  test).
- Optional ``.s`` assembly fragments.
- A ``Makefile`` (the canonical build recipe).
- Optionally one or more pre-built ELF / hex / asm artifacts
  from earlier pipeline runs.

:class:`BenchmarkWorkload` exposes that directory as a
:class:`core.workload.Workload`.  Profiling collects the
disassembly of every cv32e40p-runnable ELF under the directory;
the resulting :class:`WorkloadProfile` carries:

- ``elf_paths``: every cv32e40p-runnable ELF (excludes spike,
  baseline snapshots, etc.).
- ``instr_histogram``: instruction-count map across the fused
  ELF when one exists, else the baseline ELF.

Profiling does **not** invoke the toolchain to (re)compile.  This
is deliberate: the workload represents the input to the pipeline,
not its build state.  When the pipeline needs a fresh build it
calls :meth:`Toolchain.compile` directly.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import (
    TYPE_CHECKING,
)

from arvis.core.workload import (
    BuildRecipe,
    ExpectedResult,
    Workload,
    WorkloadProfile,
)

if TYPE_CHECKING:
    from arvis.core.toolchain import Toolchain


# ─── Default exclusion list ────────────────────────────────────────


DEFAULT_EXCLUDED_ELF_SUBSTRINGS: tuple[str, ...] = (
    "spike",  # spike ISA simulator builds (different memory layout)
    ".baseline",  # snapshots of an earlier baseline build
    "_baseline_",  # alternate baseline naming
)


# ─── BenchmarkWorkload ─────────────────────────────────────────────


class BenchmarkWorkload(Workload):
    """A single ARVIS benchmark, addressed by directory.

    Parameters
    ----------
    bench_dir:
        Path to ``targets/benchmarks/<name>/``.
    name:
        Stable identifier (used in reports and as the suffix of
        the per-workload output directory).  Defaults to the
        benchmark directory's basename.
    cflags:
        Extra compiler flags merged with whatever the toolchain
        defaults to.  Common values: ``["-O2"]``, ``["-Os",
        "-funroll-loops"]``.  Defaults to ``["-O2"]``.
    expected:
        Reference behaviour for verification.  Defaults to
        :class:`ExpectedResult` with ``exit_status=0`` (canonical
        EXIT_SUCCESS).
    exclude_elf_substrings:
        Substrings used to filter out ELFs that aren't relevant
        to the cv32e40p testbench.  Defaults to
        :data:`DEFAULT_EXCLUDED_ELF_SUBSTRINGS`.
    objdump_binary:
        ``riscv*-objdump`` binary used by :meth:`profile` for
        disassembly.  Defaults to ``"riscv32-unknown-elf-objdump"``;
        falls back to ``"riscv64-unknown-elf-objdump"`` when the
        first is missing.
    """

    def __init__(
        self,
        bench_dir: Path,
        *,
        name: str | None = None,
        cflags: Sequence[str] | None = None,
        expected: ExpectedResult | None = None,
        exclude_elf_substrings: tuple[str, ...] = DEFAULT_EXCLUDED_ELF_SUBSTRINGS,
        objdump_binary: str = "riscv32-unknown-elf-objdump",
    ) -> None:
        bench_dir = Path(bench_dir)
        if not bench_dir.exists():
            raise FileNotFoundError(f"Benchmark directory not found: {bench_dir}")
        self._bench_dir = bench_dir
        self._name = name if name is not None else bench_dir.name
        self._cflags = list(cflags) if cflags is not None else ["-O2"]
        self._expected = expected if expected is not None else ExpectedResult()
        self.exclude_elf_substrings = exclude_elf_substrings
        self.objdump_binary = objdump_binary

    # ── core.Workload contract ─────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def bench_dir(self) -> Path:
        return self._bench_dir

    @property
    def cflags(self) -> list[str]:
        return list(self._cflags)

    @property
    def expected(self) -> ExpectedResult:
        return self._expected

    @property
    def sources(self) -> list[Path]:
        """Every ``.c`` / ``.s`` / ``.S`` file in the benchmark directory."""
        return sorted(
            list(self._bench_dir.glob("*.c"))
            + list(self._bench_dir.glob("*.s"))
            + list(self._bench_dir.glob("*.S"))
        )

    @property
    def build_recipe(self) -> BuildRecipe:
        """Makefile-driven build, defaulting to ``make all``."""
        return BuildRecipe(
            kind="makefile",
            working_dir=self._bench_dir,
            targets=("all",),
        )

    # ── Profile ────────────────────────────────────────────────────
    def profile(self, toolchain: Toolchain) -> WorkloadProfile:
        """Collect ELFs and (best-effort) instruction histogram.

        ``toolchain`` is part of the abstract contract but unused
        in the default implementation -- profiling works off
        already-built artifacts.  When future strategies need
        toolchain-driven reprofiling (e.g. for cycle profiles),
        override this method.
        """
        elfs = self._collect_elfs()
        histogram = self._compute_histogram(elfs)
        return WorkloadProfile(
            elf_paths=elfs,
            instr_histogram=histogram,
        )

    # ── Helpers ────────────────────────────────────────────────────
    def _collect_elfs(self) -> tuple[Path, ...]:
        """Sorted, deduplicated tuple of in-scope ELFs."""
        candidates = sorted(self._bench_dir.glob("*.elf"))
        kept: list[Path] = []
        seen: set[str] = set()
        for elf in candidates:
            if any(s in elf.name for s in self.exclude_elf_substrings):
                continue
            real = str(elf.resolve())
            if real in seen:
                continue
            seen.add(real)
            kept.append(elf)
        return tuple(kept)

    def _compute_histogram(self, elfs: Sequence[Path]) -> dict[str, int]:
        """Disassemble the most representative ELF and tally
        mnemonics.

        Picks the fused ELF when present (since strategies
        downstream of fusion want post-fusion stats); otherwise
        the first ELF.  Returns ``{}`` when no objdump is
        available or no ELF is in scope.
        """
        if not elfs:
            return {}
        fused = next(
            (e for e in elfs if "fused" in e.name and "baseline" not in e.name),
            None,
        )
        chosen = fused if fused is not None else elfs[0]
        objdump = self._resolve_objdump()
        if objdump is None:
            return {}
        try:
            out = subprocess.check_output(
                [objdump, "-d", str(chosen)],
                text=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return {}

        counter: Counter[str] = Counter()
        for line in out.splitlines():
            # objdump disassembly lines look like:
            #     80000010:   00102023            sw      zero,0(zero)
            # Take the first whitespace-separated token after
            # the bytes column.  We just want a coarse histogram
            # for downstream analyses.
            parts = line.split()
            if len(parts) < 4:
                continue
            mnemonic = parts[2]
            # Filter out junk lines (header rows, empty mnemonics).
            if not mnemonic or not mnemonic[0].isalpha() or mnemonic.endswith(":"):
                continue
            counter[mnemonic] += 1
        return dict(counter)

    def _resolve_objdump(self) -> str | None:
        """Return a callable objdump binary path or None."""
        if shutil.which(self.objdump_binary):
            return self.objdump_binary
        # Fall back to riscv64-unknown-elf-objdump (also handles RV32 ELFs).
        for alt in ("riscv64-unknown-elf-objdump", "riscv-none-elf-objdump"):
            if shutil.which(alt):
                return alt
        return None
