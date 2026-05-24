"""Contract tests for :mod:`core.sweep` + ``strategies/sweep/``.

Coverage:

* ``SweepCandidate`` equality + hash + label
* ``SearchSpace`` size + names
* ``GridSearch`` exhaustive enumeration
* ``RandomSearch`` reproducibility (seeded) + dedup
* ``BayesianSearch`` end-to-end on a known parabola minimum
* ``SuccessiveHalving`` stub-grid behaviour
* ``SweepStrategy.analyze`` happy + sad paths
* ``SweepResult`` Pydantic JSON round-trip
* ``MultiParamSweep`` Cartesian product
* ``PrefetchFIFOSweep`` configuration (without invoking Verilator)
* Evaluator failure -> not-passed result, sweep continues
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from arvis.core.sweep import (
    BayesianSearch,
    GridSearch,
    RandomSearch,
    SearchSpace,
    SuccessiveHalving,
    SweepCandidate,
    SweepDecision,
    SweepResult,
    SweepStrategy,
)

# ─── SweepCandidate ────────────────────────────────────────────────


def test_candidate_equality_and_hash() -> None:
    a = SweepCandidate(overrides={"FIFO_DEPTH": 4, "HW_LOOP": 2})
    b = SweepCandidate(overrides={"FIFO_DEPTH": 4, "HW_LOOP": 2})
    c = SweepCandidate(overrides={"FIFO_DEPTH": 4, "HW_LOOP": 3})
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    assert {a, b, c} == {a, c}


def test_candidate_label_is_sorted_kv_pairs() -> None:
    cand = SweepCandidate(overrides={"HW_LOOP": 2, "FIFO_DEPTH": 4})
    # Sorted by key alphabetically.
    assert cand.label == "FIFO_DEPTH=4_HW_LOOP=2"


def test_candidate_subscript_access() -> None:
    cand = SweepCandidate(overrides={"FIFO_DEPTH": 8})
    assert cand["FIFO_DEPTH"] == 8


# ─── SearchSpace ───────────────────────────────────────────────────


def test_search_space_size_is_cartesian() -> None:
    space = SearchSpace(parameters={"a": [1, 2, 3], "b": [10, 20]})
    assert space.size == 6
    assert space.names() == ("a", "b")


def test_search_space_single_param() -> None:
    space = SearchSpace(parameters={"FIFO_DEPTH": [2, 4, 8]})
    assert space.size == 3


# ─── GridSearch ────────────────────────────────────────────────────


def test_grid_search_enumerates_full_product() -> None:
    space = SearchSpace(parameters={"a": [1, 2], "b": [10, 20]})
    cands = GridSearch().candidates(space)
    assert len(cands) == 4
    labels = {c.label for c in cands}
    assert labels == {"a=1_b=10", "a=1_b=20", "a=2_b=10", "a=2_b=20"}


def test_grid_search_tell_is_noop() -> None:
    """``GridSearch`` is non-adaptive; ``tell`` must accept any input."""
    g = GridSearch()
    g.tell(SweepCandidate(overrides={"x": 1}), 0.5)


# ─── RandomSearch ──────────────────────────────────────────────────


def test_random_search_seed_makes_run_reproducible() -> None:
    space = SearchSpace(parameters={"a": list(range(10))})
    a = RandomSearch(n_trials=5, seed=42).candidates(space)
    b = RandomSearch(n_trials=5, seed=42).candidates(space)
    assert [c.overrides for c in a] == [c.overrides for c in b]


def test_random_search_caps_at_grid_size() -> None:
    """``n_trials`` larger than the grid yields the full grid (deduplicated)."""
    space = SearchSpace(parameters={"a": [1, 2, 3]})
    cands = RandomSearch(n_trials=100, seed=42).candidates(space)
    assert len(cands) == 3


# ─── BayesianSearch ────────────────────────────────────────────────


def test_bayesian_search_finds_parabola_minimum() -> None:
    """TPE reliably finds the minimum of a discrete parabola."""
    space = SearchSpace(parameters={"FIFO_DEPTH": [2, 4, 8, 16, 32]})

    def parabola(cand: SweepCandidate) -> dict[str, float]:
        x = cand["FIFO_DEPTH"]
        return {"adp": float((x - 4) ** 2 + 1), "passed": True}

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(
        space=space,
        evaluator=parabola,
        search=BayesianSearch(n_trials=10, seed=42),
    )
    result = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert result.best is not None
    assert result.best.candidate["FIFO_DEPTH"] == 4


def test_bayesian_search_construction_without_optuna(monkeypatch: pytest.MonkeyPatch) -> None:
    """When optuna is missing, instantiation raises ImportError."""
    import sys

    # Simulate optuna being unavailable.
    monkeypatch.setitem(sys.modules, "optuna", None)
    with pytest.raises(ImportError, match="BayesianSearch requires optuna"):
        BayesianSearch(n_trials=5)


# ─── SuccessiveHalving ────────────────────────────────────────────


def test_successive_halving_falls_back_to_grid() -> None:
    """Phase 5.2 stub: behaves like a grid."""
    space = SearchSpace(parameters={"a": [1, 2, 3]})
    cands = SuccessiveHalving().candidates(space)
    assert len(cands) == 3


# ─── SweepStrategy.analyze ────────────────────────────────────────


def test_sweep_picks_lowest_cost_passing_candidate() -> None:
    space = SearchSpace(parameters={"x": [1, 2, 3, 4]})

    def linear_cost(cand: SweepCandidate) -> dict[str, float]:
        return {"adp": float(cand["x"]), "passed": True}

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(space=space, evaluator=linear_cost)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert decision.best is not None
    assert decision.best.candidate["x"] == 1
    assert decision.passed_count == 4
    assert decision.total_count == 4


def test_sweep_returns_none_when_all_fail() -> None:
    space = SearchSpace(parameters={"x": [1, 2]})

    def always_fails(cand: SweepCandidate) -> dict[str, float]:
        return {"passed": False}

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(space=space, evaluator=always_fails)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert decision.best is None
    assert decision.passed_count == 0


def test_sweep_continues_after_evaluator_exception() -> None:
    """An evaluator that raises records the failure but doesn't
    abort the sweep."""
    space = SearchSpace(parameters={"x": [1, 2, 3]})
    seen = []

    def picky(cand: SweepCandidate) -> dict[str, float]:
        seen.append(cand["x"])
        if cand["x"] == 2:
            raise RuntimeError("boom")
        return {"adp": float(cand["x"]), "passed": True}

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(space=space, evaluator=picky)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert seen == [1, 2, 3]
    assert decision.passed_count == 2
    assert decision.best is not None
    assert decision.best.candidate["x"] == 1
    # The failed result records the exception.
    failed = next(r for r in decision.all_results if not r.passed)
    assert failed.error is not None
    assert "boom" in failed.error


def test_sweep_default_cost_uses_adp_metric() -> None:
    """Without a custom cost, the framework reads ``metrics["adp"]``."""
    space = SearchSpace(parameters={"x": [1, 2]})

    def evaluator(cand: SweepCandidate) -> dict[str, float]:
        return {"adp": 100.0 - cand["x"], "passed": True}

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(space=space, evaluator=evaluator)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    # Lowest ADP = 100 - 2 = 98 at x=2.
    assert decision.best is not None
    assert decision.best.candidate["x"] == 2


def test_sweep_custom_cost_overrides_default() -> None:
    space = SearchSpace(parameters={"x": [1, 2, 3]})

    def evaluator(cand: SweepCandidate) -> dict[str, float]:
        return {"adp": 100.0, "throughput": float(cand["x"]), "passed": True}

    # Maximise throughput -> negate to keep "lower is better" convention.
    def neg_throughput(result: SweepResult) -> float:
        if not result.passed:
            return float("inf")
        return -result.metrics["throughput"]

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(space=space, evaluator=evaluator, cost=neg_throughput)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert decision.best is not None
    assert decision.best.candidate["x"] == 3


# ─── SweepResult / SweepDecision serialisation ────────────────────


def test_sweep_result_json_round_trip() -> None:
    r = SweepResult(
        candidate=SweepCandidate(overrides={"x": 1}),
        metrics={"adp": 1.5},
        passed=True,
    )
    payload = r.model_dump_json()
    assert "x" in payload
    assert "1.5" in payload


def test_sweep_decision_passed_count_excludes_failures() -> None:
    cands = [SweepCandidate(overrides={"x": i}) for i in (1, 2, 3)]
    results = (
        SweepResult(candidate=cands[0], metrics={"adp": 1.0}, passed=True),
        SweepResult(candidate=cands[1], metrics={}, passed=False, error="fail"),
        SweepResult(candidate=cands[2], metrics={"adp": 0.5}, passed=True),
    )
    decision = SweepDecision(
        best=results[2],
        all_results=results,
        parameter_names=("x",),
    )
    assert decision.passed_count == 2
    assert decision.total_count == 3


# ─── MultiParamSweep ──────────────────────────────────────────────


def test_multi_param_sweep_grids_arbitrary_parameters() -> None:
    from arvis.strategies.sweep.multi_param import MultiParamSweep

    seen_overrides = []

    def collect(cand: SweepCandidate) -> dict[str, float]:
        seen_overrides.append(dict(cand.overrides))
        return {"adp": float(cand["fifo"]) * float(cand["hw"] + 1), "passed": True}

    sweep = MultiParamSweep(
        parameters={"fifo": [2, 4], "hw": [0, 2]},
        evaluator=collect,
    )
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert decision.total_count == 4
    # Best is fifo=2, hw=0 (cost = 2*1 = 2).
    assert decision.best is not None
    assert decision.best.candidate["fifo"] == 2
    assert decision.best.candidate["hw"] == 0


# ─── PrefetchFIFOSweep configuration ─────────────────────────────


def test_prefetch_fifo_sweep_search_space() -> None:
    """The strategy's space exposes only ``FIFO_DEPTH``."""
    from arvis.strategies.sweep import PrefetchFIFOSweep

    sweep = PrefetchFIFOSweep(
        rtl_root="/nonexistent",
        output_dir="/tmp/sweep_test",
        hex_path="/nonexistent.hex",
        candidates=(2, 4, 8),
    )
    assert sweep.space.names() == ("FIFO_DEPTH",)
    assert sweep.space.size == 3
    assert sweep.name == "prefetch-fifo-sweep"


def test_prefetch_fifo_sweep_defaults_to_grid_search() -> None:
    """With no explicit ``search``, the strategy uses ``GridSearch``."""
    from arvis.strategies.sweep import PrefetchFIFOSweep

    sweep = PrefetchFIFOSweep(
        rtl_root="/nonexistent",
        output_dir="/tmp/sweep_test_2",
        hex_path="/nonexistent.hex",
    )
    assert isinstance(sweep.search, GridSearch)


# ─── HWLoopDepthSweep configuration ──────────────────────────────


def test_hwloop_depth_sweep_picks_winner_from_table() -> None:
    """Pre-computed metrics map -> sweep selects the lowest-ADP depth."""
    from arvis.strategies.sweep.hwloop_depth import HWLoopDepthSweep

    table = {
        0: {"cycles": 1000.0, "cells": 10000.0, "passed": 1.0},  # adp = 10
        2: {"cycles": 800.0, "cells": 10500.0, "passed": 1.0},  # adp = 8.4
        3: {"cycles": 750.0, "cells": 11000.0, "passed": 1.0},  # adp = 8.25
    }
    sweep = HWLoopDepthSweep(candidates_by_depth=table)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert decision.best is not None
    assert decision.best.candidate["HW_LOOP"] == 3


def test_hwloop_depth_sweep_failed_candidates_excluded() -> None:
    """Candidates with ``passed=False`` are skipped during selection."""
    from arvis.strategies.sweep.hwloop_depth import HWLoopDepthSweep

    table = {
        0: {"cycles": 1000.0, "cells": 10000.0, "passed": 1.0},
        2: {"cycles": 0.0, "cells": 0.0, "passed": 0.0},  # failed sim
        3: {"cycles": 500.0, "cells": 12000.0, "passed": 1.0},
    }
    sweep = HWLoopDepthSweep(candidates_by_depth=table)
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    assert decision.best is not None
    assert decision.best.candidate["HW_LOOP"] == 3


# ─── Evaluator infrastructure ────────────────────────────────────


def test_prefetch_evaluator_constructs_with_minimal_args() -> None:
    """The evaluator dataclass is happy with the four required args."""
    from arvis.targets.cv32e40p.sweep_evaluators import PrefetchFIFOEvaluator

    ev = PrefetchFIFOEvaluator(
        rtl_root="/nonexistent",
        output_dir="/tmp/eval_test",
        hex_path="/nonexistent.hex",
    )
    assert ev.use_cache is True
    assert ev.keep_artifacts is False
    # No scratch yet.
    ev.cleanup()  # safe no-op


def test_hwloop_evaluator_returns_failed_for_unknown_depth() -> None:
    """An unseen depth -> ``passed=0``; the framework records as not-passed."""
    from arvis.targets.cv32e40p.sweep_evaluators import HWLoopDepthEvaluator

    ev = HWLoopDepthEvaluator(candidates_by_depth={2: {"adp": 1.0, "passed": 1.0}})
    metrics = ev(SweepCandidate(overrides={"HW_LOOP": 99}))
    assert metrics["passed"] == 0.0
    assert math.isinf(metrics["adp"])


# ─── Extra edge-case coverage ────────────────────────────────────


def test_sweep_result_round_trips_via_json() -> None:
    """Full ``model_dump_json`` -> ``model_validate_json`` round-trip."""
    r = SweepResult(
        candidate=SweepCandidate(overrides={"x": 1, "y": 2}),
        metrics={"adp": 1.5, "cycles": 100.0},
        passed=True,
    )
    json_str = r.model_dump_json()
    restored = SweepResult.model_validate_json(json_str)
    assert restored.candidate.overrides == {"x": 1, "y": 2}
    assert restored.metrics["adp"] == 1.5
    assert restored.passed is True


def test_frozen_map_repr_and_eq_with_non_mapping() -> None:
    """Edge cases on the internal ``_FrozenMap`` helper."""
    cand = SweepCandidate(overrides={"a": 1})
    # __repr__ should mention the underlying data.
    assert "a" in repr(cand.overrides)
    # __eq__ with a non-mapping returns NotImplemented (i.e. False).
    assert (cand.overrides == 42) is False


def test_prefetch_fifo_cost_helper() -> None:
    """The ``_adp_cost`` helper handles failed + zero-metric cases."""
    from arvis.strategies.sweep.prefetch_fifo import _adp_cost

    failed = SweepResult(
        candidate=SweepCandidate(overrides={"FIFO_DEPTH": 4}),
        metrics={},
        passed=False,
    )
    assert _adp_cost(failed) == float("inf")

    zero_cycles = SweepResult(
        candidate=SweepCandidate(overrides={"FIFO_DEPTH": 4}),
        metrics={"cycles": 0, "cells": 100},
        passed=True,
    )
    assert _adp_cost(zero_cycles) == float("inf")

    real = SweepResult(
        candidate=SweepCandidate(overrides={"FIFO_DEPTH": 4}),
        metrics={"cycles": 1000, "cells": 10000},
        passed=True,
    )
    assert _adp_cost(real) == 1000 * 10000 / 1e9


def test_prefetch_fifo_sweep_cleanup_is_safe_to_call() -> None:
    """``cleanup()`` is safe even when no evaluator artifacts exist."""
    from arvis.strategies.sweep import PrefetchFIFOSweep

    sweep = PrefetchFIFOSweep(
        rtl_root="/nonexistent",
        output_dir="/tmp/sweep_cleanup_test",
        hex_path="/nonexistent.hex",
    )
    sweep.cleanup()  # no scratch yet; safe no-op
    sweep.cleanup()  # idempotent


def test_sweep_strategy_applicable_default_is_true() -> None:
    """The default ``applicable`` returns True for any inputs."""
    space = SearchSpace(parameters={"x": [1]})

    class _Sweep(SweepStrategy):
        pass

    sweep = _Sweep(space=space, evaluator=lambda c: {"passed": True, "adp": 1})
    assert sweep.applicable(workload=None, target=None) is True  # type: ignore[arg-type]


def test_hwloop_depth_cost_helper() -> None:
    """The ``_adp_cost`` helper in hwloop_depth handles edge cases."""
    from arvis.strategies.sweep.hwloop_depth import _adp_cost as hwloop_cost

    failed = SweepResult(
        candidate=SweepCandidate(overrides={"HW_LOOP": 2}),
        metrics={},
        passed=False,
    )
    assert hwloop_cost(failed) == float("inf")

    real = SweepResult(
        candidate=SweepCandidate(overrides={"HW_LOOP": 2}),
        metrics={"cycles": 1000, "cells": 10000},
        passed=True,
    )
    assert hwloop_cost(real) == 0.01


def test_multi_param_default_cost_helper() -> None:
    """The ``_default_cost`` helper in multi_param returns inf for failures."""
    from arvis.strategies.sweep.multi_param import _default_cost

    failed = SweepResult(
        candidate=SweepCandidate(overrides={"x": 1}),
        metrics={},
        passed=False,
    )
    assert _default_cost(failed) == float("inf")


# ─── PrefetchFIFOEvaluator concrete behaviour ────────────────────


def test_copy_rtl_with_fifo_depth_rewrites_localparam(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The helper copies an RTL tree and rewrites the FIFO_DEPTH localparam."""
    from arvis.targets.cv32e40p.sweep_evaluators import _copy_rtl_with_fifo_depth

    # Build a fake RTL tree.
    src = tmp_path / "src_rtl"
    (src / "rtl").mkdir(parents=True)
    pfb = src / "rtl" / "cv32e40p_prefetch_buffer.sv"
    pfb.write_text("module cv32e40p_prefetch_buffer;\n  localparam FIFO_DEPTH = 2;\nendmodule\n")

    dst = tmp_path / "dst_rtl"
    _copy_rtl_with_fifo_depth(str(src), str(dst), depth=8)

    new_text = (dst / "rtl" / "cv32e40p_prefetch_buffer.sv").read_text()
    assert "FIFO_DEPTH                     = 8;" in new_text
    assert "FIFO_DEPTH = 2" not in new_text


def test_prefetch_evaluator_uses_synth_cache_for_known_depth() -> None:
    """When ``use_cache=True`` and the depth is in the cached table,
    the evaluator returns the cached cell count without invoking Yosys.
    """
    from arvis.targets.cv32e40p.sweep_evaluators import (
        _FIFO_AREA_CACHE,
        PrefetchFIFOEvaluator,
    )

    ev = PrefetchFIFOEvaluator(
        rtl_root="/nonexistent",
        output_dir="/tmp/cache_test",
        hex_path="/nonexistent.hex",
    )
    # ``_synthesize`` is the unit under test.  Pass a fake rtl_dir;
    # the cache lookup should not touch the filesystem when the depth
    # is in the canonical table.
    cells = ev._synthesize(rtl_dir=Path("/nonexistent"), depth=4)
    assert cells == _FIFO_AREA_CACHE[4]


def test_prefetch_evaluator_synth_returns_zero_on_failure() -> None:
    """Unknown depth + Yosys unavailable -> ``cells=0`` (passed-through failure)."""
    from arvis.targets.cv32e40p.sweep_evaluators import PrefetchFIFOEvaluator

    ev = PrefetchFIFOEvaluator(
        rtl_root="/nonexistent",
        output_dir="/tmp/synth_fail_test",
        hex_path="/nonexistent.hex",
        use_cache=False,  # force fresh synth (which will raise)
    )
    # An unknown depth + no real Yosys -> 0
    cells = ev._synthesize(rtl_dir=Path("/nonexistent"), depth=999)
    assert cells == 0


def test_prefetch_evaluator_simulate_returns_failure_on_missing_rtl() -> None:
    """Missing RTL -> Verilator can't build -> (0, False)."""
    from arvis.targets.cv32e40p.sweep_evaluators import PrefetchFIFOEvaluator

    ev = PrefetchFIFOEvaluator(
        rtl_root="/nonexistent",
        output_dir="/tmp/sim_fail_test",
        hex_path="/nonexistent.hex",
    )
    cycles, passed = ev._simulate(
        rtl_dir=Path("/nonexistent/rtl"),
        sim_dir=Path("/tmp/sim_fail_test/sim"),
    )
    assert passed is False
    assert cycles == 0


def test_prefetch_evaluator_call_with_failing_sim(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``__call__`` on a fresh fake RTL tree records cycles=0 + passed=False."""
    from arvis.targets.cv32e40p.sweep_evaluators import PrefetchFIFOEvaluator

    # Set up a synthetic RTL tree so _copy_rtl_with_fifo_depth succeeds.
    src = tmp_path / "src_rtl"
    (src / "rtl").mkdir(parents=True)
    (src / "rtl" / "cv32e40p_prefetch_buffer.sv").write_text("localparam FIFO_DEPTH = 2;\n")

    ev = PrefetchFIFOEvaluator(
        rtl_root=str(src),
        output_dir=str(tmp_path / "out"),
        hex_path="/nonexistent.hex",
    )
    metrics = ev(SweepCandidate(overrides={"FIFO_DEPTH": 4}))
    assert metrics["passed"] == 0.0  # Verilator can't run with a fake hex
    assert metrics["cycles"] == 0.0
    # Cells should hit the cached table (depth=4 is canonical).
    assert metrics["cells"] == 39104
    # ADP is inf when cycles=0.
    assert math.isinf(metrics["adp"])

    # Cleanup is recorded; calling it removes the per-candidate dir.
    assert (tmp_path / "out" / "rtl_fifo4").exists()
    ev.cleanup()
    assert not (tmp_path / "out" / "rtl_fifo4").exists()
