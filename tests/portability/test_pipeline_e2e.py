"""Integration tests for Phase 6.7: full pipeline with all 5 variants.

Covers:

* :meth:`Pipeline.run_full_verification` emits all 5 standard
  variants with mock verifier + synth.
* :class:`BenchmarkWorkload.hex_for_variant` resolves real hex
  paths for known benchmarks.
* :class:`RISCVGCCToolchain` constructs and exposes correct ISA.
* Reporter produces a markdown report with all 5 variant rows.
"""

from __future__ import annotations

from pathlib import Path

from arvis.core import (
    BuildRecipe,
    ExpectedResult,
    ISADescriptor,
    Pipeline,
    PipelineResult,
    Toolchain,
    Workload,
    WorkloadProfile,
)
from arvis.core.synthesis import SynthesisFlow, SynthResult
from arvis.core.verifier import SimResult, Verifier
from arvis.targets import CV32E40P
from arvis.targets.cv32e40p.variants import STANDARD_VARIANTS

# ─── Mock infrastructure ──────────────────────────────────────────


class _MockVerifier(Verifier):
    """Records calls and returns a passing SimResult."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    @property
    def name(self) -> str:
        return "mock-verifier"

    def simulate(self, rtl_dir: Path, hex_path: Path, **kw) -> SimResult:
        self.calls.append((rtl_dir, hex_path))
        return SimResult(test_passed=True, total_cycles=1000)


class _MockSynthesis(SynthesisFlow):
    """Records calls and returns a minimal SynthResult."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    @property
    def name(self) -> str:
        return "mock-synth"

    def synthesize(self, rtl_dir: Path, **kw) -> SynthResult:
        self.calls.append(rtl_dir)
        return SynthResult(area_um2=5000.0, fmax_mhz=100.0)


class _NullToolchain(Toolchain):
    @property
    def name(self) -> str:
        return "null"

    @property
    def isa(self) -> ISADescriptor:
        return ISADescriptor.rv32imc_zicsr()

    def compile(self, *a, **kw):
        raise NotImplementedError

    def assemble(self, *a, **kw):
        raise NotImplementedError

    def disassemble(self, elf):
        return ""


class _HexWorkload(Workload):
    """Workload that resolves hex_for_variant to temp files."""

    def __init__(self, tmp: Path) -> None:
        self._tmp = tmp
        self._name = "test_bench"
        # Create hex files for all variants
        (tmp / "test_bench.hex").write_text("@00000000\n00000013\n")
        (tmp / "test_bench_fused.hex").write_text("@00000000\n00000013\n")
        (tmp / "test_bench_hw1.hex").write_text("@00000000\n00000013\n")

    @property
    def name(self) -> str:
        return self._name

    @property
    def sources(self) -> list[Path]:
        return []

    @property
    def cflags(self) -> list[str]:
        return []

    @property
    def build_recipe(self) -> BuildRecipe:
        return BuildRecipe()

    @property
    def expected(self) -> ExpectedResult:
        return ExpectedResult()

    def profile(self, toolchain) -> WorkloadProfile:
        return WorkloadProfile()

    def hex_for_variant(self, variant_label: str) -> Path | None:
        mapping = {
            "baseline": self._tmp / "test_bench.hex",
            "pruned": self._tmp / "test_bench.hex",
            "fused_pruned": self._tmp / "test_bench_fused.hex",
            "hwloop_pruned": self._tmp / "test_bench_hw1.hex",
            "all": self._tmp / "test_bench_fused.hex",
        }
        p = mapping.get(variant_label)
        return p if p and p.exists() else None


# ─── Tests ────────────────────────────────────────────────────────


class TestRunFullVerification:
    """Pipeline.run_full_verification emits all 5 standard variants."""

    def test_all_five_variants_emitted(self, tmp_path: Path) -> None:
        verifier = _MockVerifier()
        synth = _MockSynthesis()
        workload = _HexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=verifier,
            synthesis=synth,
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        result = p.run_full_verification(workload)

        assert isinstance(result, PipelineResult)
        assert len(result.variants) == 5
        labels = [v.label for v in result.variants]
        assert labels == ["baseline", "pruned", "fused_pruned", "hwloop_pruned", "all"]

    def test_verifier_called_for_each_variant_with_hex(self, tmp_path: Path) -> None:
        verifier = _MockVerifier()
        workload = _HexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=verifier,
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        result = p.run_full_verification(workload)

        # All 5 variants have hex files → verifier called 5 times
        assert len(verifier.calls) == 5
        for vr in result.variants:
            assert vr.sim_result is not None
            assert vr.sim_result.test_passed is True

    def test_synthesis_called_for_each_variant(self, tmp_path: Path) -> None:
        synth = _MockSynthesis()
        workload = _HexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_MockVerifier(),
            synthesis=synth,
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        result = p.run_full_verification(workload)

        assert len(synth.calls) == 5
        for vr in result.variants:
            assert vr.synth_result is not None
            assert vr.synth_result.area_um2 == 5000.0

    def test_rtl_directories_created(self, tmp_path: Path) -> None:
        workload = _HexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_MockVerifier(),
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        result = p.run_full_verification(workload)

        for vr in result.variants:
            assert vr.rtl_dir.exists()
            # Each variant gets its own directory
            assert f"rtl_{vr.label}" in str(vr.rtl_dir)

    def test_variants_restored_after_run(self, tmp_path: Path) -> None:
        """run_full_verification restores original variants list."""
        workload = _HexWorkload(tmp_path)
        original_variants = [STANDARD_VARIANTS[0]]  # just baseline

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_MockVerifier(),
            variants=original_variants,
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        p.run_full_verification(workload)

        # Original variants list is restored
        assert p.variants == original_variants

    def test_missing_hex_skips_simulation(self, tmp_path: Path) -> None:
        """Variants without hex files skip verifier gracefully."""
        verifier = _MockVerifier()

        class _NoHexWorkload(_HexWorkload):
            def hex_for_variant(self, variant_label: str) -> Path | None:
                # Only baseline has a hex
                if variant_label == "baseline":
                    return self._tmp / "test_bench.hex"
                return None

        workload = _NoHexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=verifier,
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        result = p.run_full_verification(workload)

        # Only 1 verifier call (baseline)
        assert len(verifier.calls) == 1
        # All 5 variants still emitted
        assert len(result.variants) == 5
        # Non-baseline variants have sim_result=None
        for vr in result.variants[1:]:
            assert vr.sim_result is None


class TestBenchmarkWorkloadHexForVariant:
    """BenchmarkWorkload.hex_for_variant resolves real hex paths."""

    def test_resolves_baseline(self) -> None:
        bench = Path("targets/benchmarks/crc32")
        if not bench.exists():
            return  # skip if benchmarks not present
        from arvis.workloads.benchmark import BenchmarkWorkload

        wl = BenchmarkWorkload(bench)
        h = wl.hex_for_variant("baseline")
        assert h is not None
        assert h.name == "crc32.hex"

    def test_resolves_fused(self) -> None:
        bench = Path("targets/benchmarks/crc32")
        if not bench.exists():
            return
        from arvis.workloads.benchmark import BenchmarkWorkload

        wl = BenchmarkWorkload(bench)
        h = wl.hex_for_variant("fused_pruned")
        assert h is not None
        assert h.name == "crc32_fused.hex"

    def test_resolves_hwloop(self) -> None:
        bench = Path("targets/benchmarks/crc32")
        if not bench.exists():
            return
        from arvis.workloads.benchmark import BenchmarkWorkload

        wl = BenchmarkWorkload(bench)
        h = wl.hex_for_variant("hwloop_pruned")
        assert h is not None
        assert "hw" in h.name

    def test_pruned_same_as_baseline(self) -> None:
        bench = Path("targets/benchmarks/crc32")
        if not bench.exists():
            return
        from arvis.workloads.benchmark import BenchmarkWorkload

        wl = BenchmarkWorkload(bench)
        assert wl.hex_for_variant("pruned") == wl.hex_for_variant("baseline")

    def test_unknown_variant_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "dummy.c").write_text("int main(){}")
        from arvis.workloads.benchmark import BenchmarkWorkload

        wl = BenchmarkWorkload(tmp_path, name="dummy")
        assert wl.hex_for_variant("nonexistent_variant") is None


class TestRISCVGCCToolchain:
    """RISCVGCCToolchain constructs and exposes correct properties."""

    def test_construction_and_isa(self) -> None:
        from arvis.toolchains.riscv_gcc import RISCVGCCToolchain, ToolchainError

        try:
            tc = RISCVGCCToolchain()
        except ToolchainError:
            return  # GCC not installed; skip

        assert tc.name == "riscv32-unknown-elf-gcc"
        assert tc.isa.name == "rv32imc_zicsr"
        assert tc.isa.xlen == 32
        assert tc.supports("i")
        assert tc.supports("m")
        assert tc.supports("c")
        assert not tc.supports("v")

    def test_missing_gcc_raises(self) -> None:
        import pytest

        from arvis.toolchains.riscv_gcc import RISCVGCCToolchain, ToolchainError

        with pytest.raises(ToolchainError):
            RISCVGCCToolchain(gcc_binary="/nonexistent/riscv32-gcc")

    def test_disassemble_empty_on_missing_objdump(self, tmp_path: Path) -> None:
        from arvis.toolchains.riscv_gcc import RISCVGCCToolchain, ToolchainError

        try:
            tc = RISCVGCCToolchain()
        except ToolchainError:
            return

        # Disassemble a non-existent ELF → empty string (graceful)
        result = tc.disassemble(tmp_path / "nonexistent.elf")
        assert result == ""


class TestTargetStandardVariants:
    """run_full_verification reads variants from target.standard_variants."""

    def test_target_advertises_standard_variants(self) -> None:
        """CV32E40P advertises the 5 canonical variants."""
        from arvis.targets import CV32E40P

        target = CV32E40P()
        std = target.standard_variants
        assert len(std) == 5
        labels = {v.label for v in std}
        assert labels == {"baseline", "pruned", "fused_pruned", "hwloop_pruned", "all"}

    def test_run_full_verification_uses_target_variants(self, tmp_path: Path) -> None:
        """Pipeline reads variants from self.target, not hard-coded import."""
        workload = _HexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_MockVerifier(),
        )
        p._output_root_for_workload = lambda wl: tmp_path / "out"

        result = p.run_full_verification(workload)
        assert len(result.variants) == 5

    def test_target_with_no_variants_raises(self, tmp_path: Path) -> None:
        """Targets that don't override standard_variants raise."""

        class _BareTarget:
            name = "bare"
            standard_variants = ()
            rtl_root = tmp_path

        workload = _HexWorkload(tmp_path)
        p = Pipeline(
            target=_BareTarget(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_MockVerifier(),
        )

        import pytest

        with pytest.raises(ValueError, match="advertises no standard variants"):
            p.run_full_verification(workload)


class TestPipelineWithReporter:
    """Reporter produces markdown with all 5 variant rows."""

    def test_report_generated_with_all_variants(self, tmp_path: Path) -> None:
        from arvis.report.markdown_reporter import MarkdownReporter

        reporter = MarkdownReporter()
        workload = _HexWorkload(tmp_path)

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_MockVerifier(),
            synthesis=_MockSynthesis(),
            reporter=reporter,
        )
        out_dir = tmp_path / "out"
        p._output_root_for_workload = lambda wl: out_dir

        p.run_full_verification(workload)

        # Report file should exist
        report_path = out_dir / "report.md"
        assert report_path.exists()
        content = report_path.read_text()

        # All 5 variant labels appear in the report
        for label in ("baseline", "pruned", "fused_pruned", "hwloop_pruned", "all"):
            assert label in content
