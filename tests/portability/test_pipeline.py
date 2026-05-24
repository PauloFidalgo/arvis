"""Tests for ``core.pipeline.Pipeline`` and variant emission."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

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
from arvis.core.pipeline import VariantConfig, VariantResult
from arvis.core.verifier import Verifier, SimResult
from arvis.targets import CV32E40P
from arvis.targets.cv32e40p.variants import (
    BASELINE,
    PRUNED,
    FUSED_PRUNED,
    HWLOOP_PRUNED,
    ALL,
    STANDARD_VARIANTS,
)


# ─── Shared mock infrastructure ────────────────────────────────────


class _StubWorkload(Workload):
    """In-memory workload that yields whatever ELF list it's told."""

    def __init__(self, name="stub", elf_paths=()):
        self._name = name
        self._elfs = tuple(elf_paths)

    @property
    def name(self): return self._name
    @property
    def sources(self): return []
    @property
    def cflags(self): return []
    @property
    def build_recipe(self): return BuildRecipe()
    @property
    def expected(self): return ExpectedResult()
    def profile(self, toolchain): return WorkloadProfile(elf_paths=self._elfs)


class _NullToolchain(Toolchain):
    @property
    def name(self): return "null"
    @property
    def isa(self): return ISADescriptor.rv32imc_zicsr()
    def compile(self, *a, **kw): raise NotImplementedError
    def assemble(self, *a, **kw): raise NotImplementedError
    def disassemble(self, elf): return ""


class _NullVerifier(Verifier):
    @property
    def name(self): return "null"
    def simulate(self, *a, **kw): return SimResult(test_passed=False)


# ─── VariantConfig contract ────────────────────────────────────────


class TestVariantConfig:
    def test_includes(self):
        v = VariantConfig(label="x", decision_kinds=frozenset({"PruneDecision"}))
        assert v.includes("PruneDecision")
        assert not v.includes("FusionDecision")

    def test_baseline_includes_nothing(self):
        assert not BASELINE.includes("PruneDecision")
        assert not BASELINE.includes("FusionDecision")

    def test_all_variant_includes_everything(self):
        assert ALL.includes("PruneDecision")
        assert ALL.includes("FusionDecision")
        assert ALL.includes("LoopDecision")
        assert ALL.includes("WidthDecision")

    def test_pruned_excludes_fusion(self):
        assert PRUNED.includes("PruneDecision")
        assert not PRUNED.includes("FusionDecision")
        assert not PRUNED.includes("LoopDecision")


class TestStandardVariantsSet:
    def test_count_and_membership(self):
        assert len(STANDARD_VARIANTS) == 5
        labels = {v.label for v in STANDARD_VARIANTS}
        assert labels == {"baseline", "pruned", "fused_pruned",
                          "hwloop_pruned", "all"}


# ─── Pipeline.run() ────────────────────────────────────────────────


class TestPipelineDecisionsOnly:
    """When variants is empty, Pipeline.run is a strategy collector."""

    def test_empty_strategies_returns_empty_decisions(self):
        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[],
            verifier=_NullVerifier(),
        )
        result = p.run(_StubWorkload())
        assert isinstance(result, PipelineResult)
        assert result.decisions == {}
        assert result.variants == []


class TestPipelineVariantEmission:
    """When variants is non-empty, each variant produces a VariantResult."""

    def test_baseline_variant_copies_rtl_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = CV32E40P()
            workload = _StubWorkload()
            p = Pipeline(
                target=target,
                toolchain=_NullToolchain(),
                strategies=[],
                verifier=_NullVerifier(),
                variants=[BASELINE],
            )
            p._output_root_for_workload = lambda wl: Path(tmp)
            result = p.run(workload)

            assert len(result.variants) == 1
            vr = result.variants[0]
            assert vr.label == "baseline"
            assert vr.rtl_dir.exists()
            # Baseline variant copies the RTL but applies no patches.
            top = (vr.rtl_dir / "rtl/cv32e40p_top.sv").read_text()
            assert "parameter PC_WIDTH" not in top  # no width narrowing

    def test_three_variants_produce_three_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = CV32E40P()
            p = Pipeline(
                target=target,
                toolchain=_NullToolchain(),
                strategies=[],
                verifier=_NullVerifier(),
                variants=[BASELINE, PRUNED, ALL],
            )
            p._output_root_for_workload = lambda wl: Path(tmp)
            result = p.run(_StubWorkload())

            assert len(result.variants) == 3
            labels = [v.label for v in result.variants]
            assert labels == ["baseline", "pruned", "all"]
            for vr in result.variants:
                assert vr.rtl_dir.exists()
                assert vr.rtl_dir.name == f"rtl_{vr.label}"


class TestPipelineStrategyOrdering:
    """Strategies run in canonical dependency order."""

    def test_canonical_order(self):
        from arvis.strategies.fusion import NGramFusion
        from arvis.strategies.hwloop import CV32E40PHWLoop
        from arvis.strategies.pruning import UsageDrivenPruner
        from arvis.strategies.width import PCWidthNarrowing

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[
                PCWidthNarrowing(),
                UsageDrivenPruner(verbose=False),
                NGramFusion(),
                CV32E40PHWLoop(),
            ],
            verifier=_NullVerifier(),
        )
        order = [s.name for s in p.ordered_strategies()]
        # Expected: fusion -> loop -> prune -> width
        assert order == [
            "ngram-fusion",
            "cv32e40p-pulp-hwloop",
            "usage-driven-pruner",
            "pc-width-narrowing",
        ]


class TestPipelineErrorHandling:
    """Strategy and variant failures must not abort the pipeline."""

    def test_strategy_exception_recorded(self):
        from arvis.core.strategy import PruningStrategy, PruneDecision

        class FailingPruner(PruningStrategy):
            @property
            def name(self): return "failing-pruner"
            def analyze(self, *a, **kw):
                raise RuntimeError("intentional test failure")

        p = Pipeline(
            target=CV32E40P(),
            toolchain=_NullToolchain(),
            strategies=[FailingPruner()],
            verifier=_NullVerifier(),
        )
        result = p.run(_StubWorkload())
        assert "strategy_errors" in result.extra
        assert "failing-pruner" in result.extra["strategy_errors"]
        assert "RuntimeError" in result.extra["strategy_errors"]["failing-pruner"]
