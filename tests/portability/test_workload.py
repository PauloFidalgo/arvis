"""Tests for ``workloads.BenchmarkWorkload``."""

from __future__ import annotations

from pathlib import Path

import pytest

from arvis.core import BuildRecipe, ExpectedResult, WorkloadProfile
from arvis.workloads import BenchmarkWorkload


# ─── Repo-resident benchmark for tests ─────────────────────────────


def _find_bench_dir() -> Path:
    """Locate a benchmark dir that ships at least one ELF.

    Tests are skipped when nothing is available (e.g. on a fresh
    checkout where no benchmark has been built yet).
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    bench_root = repo_root / "targets" / "benchmarks"
    if not bench_root.exists():
        return None  # type: ignore[return-value]
    # Prefer ud (used in many other tests).
    ud = bench_root / "ud"
    if ud.exists() and any(ud.glob("*.elf")):
        return ud
    # Otherwise pick whatever has at least one ELF.
    for d in bench_root.iterdir():
        if d.is_dir() and any(d.glob("*.elf")):
            return d
    return None  # type: ignore[return-value]


@pytest.fixture
def bench_dir():
    d = _find_bench_dir()
    if d is None:
        pytest.skip("No benchmark directory with ELFs found under targets/benchmarks/")
    return d


# ─── Construction ──────────────────────────────────────────────────


class TestBenchmarkWorkloadConstruction:
    def test_default_name_from_dir(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        assert w.name == bench_dir.name

    def test_explicit_name_overrides(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir, name="custom")
        assert w.name == "custom"

    def test_default_cflags(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        assert w.cflags == ["-O2"]

    def test_explicit_cflags(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir, cflags=["-O3", "-funroll-loops"])
        assert w.cflags == ["-O3", "-funroll-loops"]

    def test_default_expected(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        assert w.expected.exit_status == 0

    def test_missing_dir_raises(self):
        with pytest.raises(FileNotFoundError):
            BenchmarkWorkload(bench_dir=Path("/nonexistent/path/to/benchmark"))


# ─── Workload contract surface ─────────────────────────────────────


class TestBenchmarkWorkloadProperties:
    def test_sources_is_list_of_path(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        sources = w.sources
        assert isinstance(sources, list)
        for p in sources:
            assert isinstance(p, Path)

    def test_build_recipe_is_makefile(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        recipe = w.build_recipe
        assert isinstance(recipe, BuildRecipe)
        assert recipe.kind == "makefile"
        assert recipe.working_dir == bench_dir
        assert "all" in recipe.targets

    def test_cflags_returns_a_copy(self, bench_dir):
        """Mutating the returned list must not affect the workload."""
        w = BenchmarkWorkload(bench_dir=bench_dir, cflags=["-O2"])
        cflags = w.cflags
        cflags.append("-DEVIL")
        assert "-DEVIL" not in w.cflags


# ─── Profile ───────────────────────────────────────────────────────


class TestBenchmarkWorkloadProfile:
    def test_profile_returns_workload_profile(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        p = w.profile(toolchain=None)  # type: ignore[arg-type]
        assert isinstance(p, WorkloadProfile)

    def test_profile_includes_elfs(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        p = w.profile(toolchain=None)  # type: ignore[arg-type]
        assert len(p.elf_paths) >= 1

    def test_profile_excludes_spike_elfs(self, bench_dir):
        """Spike ELFs are skewed for width analyses; must not be in profile."""
        w = BenchmarkWorkload(bench_dir=bench_dir)
        p = w.profile(toolchain=None)  # type: ignore[arg-type]
        for elf in p.elf_paths:
            assert "spike" not in elf.name

    def test_profile_excludes_baseline_snapshots(self, bench_dir):
        w = BenchmarkWorkload(bench_dir=bench_dir)
        p = w.profile(toolchain=None)  # type: ignore[arg-type]
        for elf in p.elf_paths:
            assert ".baseline" not in elf.name

    def test_custom_exclude_substrings(self, bench_dir):
        # Passing an empty exclusion list lets even the spike
        # ELF through (if one exists).
        w = BenchmarkWorkload(bench_dir=bench_dir, exclude_elf_substrings=())
        p = w.profile(toolchain=None)  # type: ignore[arg-type]
        # We don't assert spike is present (some bench dirs don't have it);
        # we just verify the filter is honoured by counting >= unfiltered
        # workload's elfs.
        unfiltered_count = len(p.elf_paths)
        w2 = BenchmarkWorkload(bench_dir=bench_dir)
        filtered_count = len(w2.profile(toolchain=None).elf_paths)  # type: ignore[arg-type]
        assert unfiltered_count >= filtered_count
