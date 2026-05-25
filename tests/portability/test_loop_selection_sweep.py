"""Tests for the GA/exhaustive loop selection sweep migration.

Covers:

* :class:`GeneticSearch` — binary GA candidate generation,
  exhaustive fallback for small N, deduplication.
* :class:`LoopSelectionSweep` — construction, best_mask extraction.
* :class:`LoopSelectionEvaluator` — mock-based contract test.
* :func:`_loop_selection_via_sweep` — end-to-end with mocked
  evaluator and sim builder.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

from arvis.core.sweep import (
    GeneticSearch,
    SearchSpace,
    SweepCandidate,
)

# ─── GeneticSearch ────────────────────────────────────────────────


class TestGeneticSearch:
    """GeneticSearch generates candidates for binary on/off spaces."""

    def test_small_space_falls_back_to_exhaustive(self) -> None:
        """N ≤ 6 → exhaustive (GridSearch) fallback."""
        space = SearchSpace(parameters={"loop_0": [0, 1], "loop_1": [0, 1]})
        gs = GeneticSearch(exhaustive_threshold=6, seed=42)
        candidates = gs.candidates(space)
        # 2 binary params → 4 combinations
        assert len(candidates) == 4
        # All 4 combos present
        combos = {(c.overrides["loop_0"], c.overrides["loop_1"]) for c in candidates}
        assert combos == {(0, 0), (0, 1), (1, 0), (1, 1)}

    def test_large_space_uses_ga(self) -> None:
        """N > 6 → GA generates more candidates than exhaustive."""
        N = 8
        space = SearchSpace(parameters={f"loop_{i}": [0, 1] for i in range(N)})
        gs = GeneticSearch(
            population_size=8,
            generations=3,
            exhaustive_threshold=6,
            seed=42,
        )
        candidates = gs.candidates(space)
        # Should have GA candidates + single-bit flips, deduplicated
        # GA: 8 * 3 = 24, plus all-on, all-off, N single-on, N single-off = 2 + 16
        # Total before dedup: ~42; after dedup: fewer
        assert len(candidates) > 0
        assert len(candidates) <= 8 * 3 + 2 + 2 * N  # upper bound

    def test_candidates_are_deduplicated(self) -> None:
        """No duplicate candidates in output."""
        N = 10
        space = SearchSpace(parameters={f"loop_{i}": [0, 1] for i in range(N)})
        gs = GeneticSearch(seed=123)
        candidates = gs.candidates(space)
        keys = [tuple(c.overrides[f"loop_{i}"] for i in range(N)) for c in candidates]
        assert len(keys) == len(set(keys))

    def test_non_binary_falls_back_to_grid(self) -> None:
        """Non-binary parameters → GridSearch fallback."""
        space = SearchSpace(parameters={"depth": [2, 3, 4]})
        gs = GeneticSearch()
        candidates = gs.candidates(space)
        assert len(candidates) == 3

    def test_tell_is_noop(self) -> None:
        """tell() doesn't raise."""
        gs = GeneticSearch()
        gs.tell(SweepCandidate(overrides={"x": 1}), 42.0)


# ─── LoopSelectionSweep ───────────────────────────────────────────


class TestLoopSelectionSweep:
    """LoopSelectionSweep wraps GeneticSearch for loop on/off."""

    def test_construction(self) -> None:
        from arvis.strategies.sweep.loop_selection import LoopSelectionSweep

        def _mock_eval(c: SweepCandidate) -> dict[str, float]:
            return {"cycles": 100.0, "passed": 1.0}

        sweep = LoopSelectionSweep(
            loop_keys=["loop_A", "loop_B", "loop_C"],
            evaluator=_mock_eval,
        )
        assert sweep.name == "loop-selection-sweep"
        assert len(sweep.loop_keys) == 3

    def test_analyze_picks_lowest_cycles(self) -> None:
        from arvis.strategies.sweep.loop_selection import LoopSelectionSweep

        # 3 loops → exhaustive (2^3 = 8 candidates)
        call_count = [0]

        def _eval(c: SweepCandidate) -> dict[str, float]:
            call_count[0] += 1
            mask = [int(c.overrides.get(f"loop_{i}", 0)) for i in range(3)]
            # Best: only loop_1 on → 50 cycles
            if mask == [0, 1, 0]:
                return {"cycles": 50.0, "passed": 1.0}
            # All others: 100 cycles
            return {"cycles": 100.0, "passed": 1.0}

        sweep = LoopSelectionSweep(
            loop_keys=["A", "B", "C"],
            evaluator=_eval,
        )
        decision = sweep.analyze(None, None, None)  # type: ignore[arg-type]

        assert decision.best is not None
        assert decision.best.metrics["cycles"] == 50.0
        mask = sweep.best_mask(decision)
        assert mask == [0, 1, 0]

    def test_best_mask_defaults_to_all_on(self) -> None:
        from arvis.strategies.sweep.loop_selection import LoopSelectionSweep

        # All candidates fail → best is None → mask defaults to all-on
        def _fail_eval(c: SweepCandidate) -> dict[str, float]:
            return {"cycles": 0.0, "passed": 0.0}

        sweep = LoopSelectionSweep(
            loop_keys=["A", "B"],
            evaluator=_fail_eval,
        )
        decision = sweep.analyze(None, None, None)  # type: ignore[arg-type]
        assert decision.best is None
        mask = sweep.best_mask(decision)
        assert mask == [1, 1]


# ─── LoopSelectionEvaluator (mock-based) ──────────────────────────


class TestLoopSelectionEvaluator:
    """Contract test for LoopSelectionEvaluator with mocked subprocess."""

    def test_returns_cycles_on_success(self, tmp_path: Path) -> None:
        from arvis.targets.cv32e40p.sweep_evaluators import LoopSelectionEvaluator

        # Create a minimal assembly source
        asm = tmp_path / "test.s"
        asm.write_text(".text\n.globl _start\n_start:\n  nop\n  j _start\n")

        evaluator = LoopSelectionEvaluator(
            src_asm=asm,
            loop_keys=[],  # no loops → patch is a no-op
            hw_loop=2,
            bm_dir=tmp_path,
            sim_bin="/nonexistent/sim",
        )

        # Mock subprocess to return SUCCESS
        mock_result = MagicMock()
        mock_result.stdout = "SUCCESS after 1234 cycles\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            with patch("subprocess.call", return_value=0):
                metrics = evaluator(SweepCandidate(overrides={}))

        assert metrics["passed"] == 1.0
        assert metrics["cycles"] == 1234.0

    def test_returns_not_passed_on_timeout(self, tmp_path: Path) -> None:
        import subprocess

        from arvis.targets.cv32e40p.sweep_evaluators import LoopSelectionEvaluator

        asm = tmp_path / "test.s"
        asm.write_text(".text\n.globl _start\n_start:\n  nop\n  j _start\n")

        evaluator = LoopSelectionEvaluator(
            src_asm=asm,
            loop_keys=[],
            hw_loop=2,
            bm_dir=tmp_path,
            sim_bin="/nonexistent/sim",
        )

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sim", 120)):
            with patch("subprocess.call", return_value=0):
                metrics = evaluator(SweepCandidate(overrides={}))

        assert metrics["passed"] == 0.0


# ─── _loop_selection_via_sweep (integration) ──────────────────────


class TestLoopSelectionViaSweep:
    """End-to-end test of the sweep gateway in runner.py."""

    def test_returns_false_when_no_merged_source(self, tmp_path: Path) -> None:
        """No merged .s files → returns False (falls through to legacy)."""
        from arvis.pipeline.runner import _loop_selection_via_sweep

        class _FakeCfg:
            use_pipeline_runner = True

        class _FakeCtx:
            hwloop_candidates: ClassVar[list] = []
            hwloop_hex_path = None
            hwloop_elf_path = None
            fused_hex_path = None
            fused_elf_path = None

        result = _loop_selection_via_sweep(
            cfg=_FakeCfg(),
            ctx=_FakeCtx(),
            bm_dir=tmp_path,
            prog_name="nonexistent",
            hw_val=2,
            rtl_all="/tmp/fake_rtl",
            label_tag="test",
            encoding=None,
            image=None,
            specializer_dir=tmp_path,
            fifo_depth=2,
            build_sim_fn=lambda rtl, label: None,
        )
        assert result is False

    def test_returns_false_when_sim_build_fails(self, tmp_path: Path) -> None:
        """Sim build fails → returns False."""
        from arvis.pipeline.runner import _loop_selection_via_sweep

        # Create a merged source so the loop enters
        (tmp_path / "test_merged.s").write_text(".text\n_start:\n  nop\n  j _start\n")

        class _FakeCfg:
            use_pipeline_runner = True

        class _FakeCtx:
            hwloop_candidates: ClassVar[list] = []
            hwloop_hex_path = None
            hwloop_elf_path = None
            fused_hex_path = None
            fused_elf_path = None

        result = _loop_selection_via_sweep(
            cfg=_FakeCfg(),
            ctx=_FakeCtx(),
            bm_dir=tmp_path,
            prog_name="test",
            hw_val=2,
            rtl_all="/tmp/fake_rtl",
            label_tag="test",
            encoding=None,
            image=None,
            specializer_dir=tmp_path,
            fifo_depth=2,
            build_sim_fn=lambda rtl, label: None,  # sim build fails
        )
        assert result is False
