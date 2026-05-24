"""Contract tests for the fusion and hwloop strategies' fallback paths.

These strategies normally require Docker + GCC for real analysis;
we test the *fallback* paths that return empty decisions when
inputs are missing.  This covers the bulk of the strategy code
without standing up the toolchain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arvis.core.strategy import FusionDecision, LoopDecision
from arvis.core.workload import (
    BuildRecipe,
    ExpectedResult,
    Workload,
    WorkloadProfile,
)
from arvis.strategies.fusion.ngram import NGramFusion
from arvis.strategies.hwloop.cv32e40p_pulp import CV32E40PHWLoop
from arvis.targets import CV32E40P


class _StubWorkload(Workload):
    """Minimal workload satisfying the abstract contract."""

    @property
    def name(self) -> str:
        return "stub"

    @property
    def sources(self) -> list[Path]:
        return []

    @property
    def cflags(self) -> list[str]:
        return []

    @property
    def build_recipe(self) -> BuildRecipe:
        return BuildRecipe(name="stub", source_files=())

    @property
    def expected(self) -> ExpectedResult:
        return ExpectedResult(stdout="")

    def profile(self, toolchain: object) -> WorkloadProfile:  # type: ignore[override]
        return WorkloadProfile()


@pytest.fixture
def target() -> CV32E40P:
    return CV32E40P()


# ─── NGramFusion ──────────────────────────────────────────────────


def test_ngram_fusion_applicable_to_cv32e40p(target: CV32E40P) -> None:
    """Targets with free R4 opcode slots are fusable."""
    s = NGramFusion()
    assert s.applicable(workload=_StubWorkload(), target=target) is True


def test_ngram_fusion_not_applicable_to_stub() -> None:
    """A target with no opcode_space attribute returns False."""

    class _StubTarget:
        pass

    s = NGramFusion()
    assert s.applicable(workload=_StubWorkload(), target=_StubTarget()) is False  # type: ignore[arg-type]


def test_ngram_fusion_returns_empty_when_no_fused_elf(
    target: CV32E40P,
) -> None:
    """No ``*_fused.elf`` in profile -> empty FusionDecision."""
    s = NGramFusion()
    profile = WorkloadProfile()  # no elf_paths
    decision = s.analyze(
        workload=_StubWorkload(),
        profile=profile,
        target=target,
    )
    assert isinstance(decision, FusionDecision)
    assert decision.fused_ops == ()


def test_ngram_fusion_skips_baseline_elf(target: CV32E40P) -> None:
    """``baseline`` is filtered out from the fused-elf candidates."""
    bench_root = Path(__file__).resolve().parent.parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        pytest.skip("no benchmarks dir")
    elfs = list(bench_root.rglob("*_baseline.elf"))
    if not elfs:
        pytest.skip("no *_baseline.elf available")

    profile = WorkloadProfile(elf_paths=(elfs[0],))
    decision = NGramFusion().analyze(
        workload=_StubWorkload(),
        profile=profile,
        target=target,
    )
    # baseline alone -> no fused candidate -> empty decision
    assert decision.fused_ops == ()


def test_ngram_fusion_name() -> None:
    assert NGramFusion().name == "ngram-fusion"


# ─── CV32E40PHWLoop ───────────────────────────────────────────────


def test_hwloop_strategy_applicable_to_cv32e40p(target: CV32E40P) -> None:
    """Targets with the HW_LOOP parameter are loop-strategy targets."""
    s = CV32E40PHWLoop()
    assert s.applicable(workload=_StubWorkload(), target=target) is True


def test_hwloop_strategy_not_applicable_to_stub() -> None:
    """A target without HW_LOOP returns False."""

    class _StubTarget:
        def parameter(self, name: str) -> None:
            return None

    assert (
        CV32E40PHWLoop().applicable(workload=_StubWorkload(), target=_StubTarget())  # type: ignore[arg-type]
        is False
    )


def test_hwloop_strategy_returns_empty_on_failure(target: CV32E40P) -> None:
    """When the legacy ``hwloop.run`` raises (no Docker, no GCC),
    the strategy returns an empty :class:`LoopDecision`.

    The fixture profile carries no asm/elf paths, so the legacy
    invocation will fail at the first I/O.  We just assert the
    type contract.
    """
    s = CV32E40PHWLoop()
    decision = s.analyze(
        workload=_StubWorkload(),
        profile=WorkloadProfile(),
        target=target,
    )
    assert isinstance(decision, LoopDecision)
    # Empty decision is the documented "no candidates" outcome.
    assert decision.nest_depth == 0


def test_hwloop_strategy_name() -> None:
    assert "hwloop" in CV32E40PHWLoop().name.lower()
