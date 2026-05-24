"""Tests for the Phase 5 HW_LOOP sweep integration in runner.py.

The full ``_sweep_hwloop_candidates`` flow needs Docker + GCC +
Verilator + Yosys, so we test the two extracted helpers
(``_select_hwloop_winners`` and ``_print_hwloop_sweep_table``)
in isolation with synthetic ``HWLoopSweepResult`` instances.

Behavioural contract: the new framework-driven selection
returns the same ``SweepWinners`` shape as the legacy
``pick_best`` helper in :mod:`pipeline.hwloop_sweep`, including:

  * best by ADP comes from the framework (cost = cycles * cells)
  * best by cycles is a separate scan over passing results
  * ``same`` is True iff both winners point at the same depth
  * an empty / all-failed result list returns
    ``SweepWinners(best_adp=0, best_cycles=0)``

Cross-repo support: this file works on both the private
``tese`` layout (``pipeline.hwloop_sweep``) and the public
``arvis-public`` layout (``arvis.pipeline.hwloop_sweep``) by
trying both import paths via :func:`_import_legacy`.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest


def _import_legacy() -> tuple:
    """Return ``(HWLoopSweepResult, SweepWinners, pick_best,
    _select_hwloop_winners, _print_hwloop_sweep_table)``.

    Tries the bare ``pipeline.*`` import first (private tese
    layout); falls back to ``arvis.pipeline.*`` (public
    arvis-public layout).  Skips the test if neither is
    importable.
    """
    try:
        from pipeline.hwloop_sweep import (
            HWLoopSweepResult,
            SweepWinners,
            pick_best,
        )
        from pipeline.runner import (
            _print_hwloop_sweep_table,
            _select_hwloop_winners,
        )
    except ImportError:
        try:
            from arvis.pipeline.hwloop_sweep import (  # type: ignore[import-not-found, no-redef]
                HWLoopSweepResult,
                SweepWinners,
                pick_best,
            )
            from arvis.pipeline.runner import (  # type: ignore[import-not-found, no-redef]
                _print_hwloop_sweep_table,
                _select_hwloop_winners,
            )
        except ImportError:
            pytest.skip("legacy pipeline modules not available")
    return (
        HWLoopSweepResult,
        SweepWinners,
        pick_best,
        _select_hwloop_winners,
        _print_hwloop_sweep_table,
    )


def _make_result(hw_loop: int, cycles: int, cells: int, passed: bool = True):
    """Build a synthetic ``HWLoopSweepResult``."""
    HWLoopSweepResult = _import_legacy()[0]
    r = HWLoopSweepResult(hw_loop=hw_loop, loops_patched=1)
    r.cycles = cycles
    r.cells = cells
    r.passed = passed
    return r


def test_select_winners_picks_best_adp_and_cycles_separately():
    """When ADP-best != cycles-best, both winners are surfaced."""
    _, _, _, _select_hwloop_winners, _ = _import_legacy()

    # Three candidates, all passing:
    #   hw=2: 1000 cycles, 10000 cells -> ADP = 10
    #   hw=3:  900 cycles, 10500 cells -> ADP = 9.45  (best ADP)
    #   hw=4:  800 cycles, 12000 cells -> ADP = 9.6   (best cycles, more cells)
    results = [
        _make_result(hw_loop=2, cycles=1000, cells=10000),
        _make_result(hw_loop=3, cycles=900, cells=10500),
        _make_result(hw_loop=4, cycles=800, cells=12000),
    ]

    sink = io.StringIO()
    with redirect_stdout(sink):
        winners = _select_hwloop_winners(results)

    assert winners.best_adp == 3
    assert winners.best_cycles == 4
    assert winners.same is False
    assert winners.best_adp_result is results[1]
    assert winners.best_cycles_result is results[2]


def test_select_winners_same_when_one_candidate_dominates():
    """When the same depth is best by both metrics, ``same=True``."""
    _, _, _, _select_hwloop_winners, _ = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=1000, cells=10000),
        _make_result(hw_loop=3, cycles=800, cells=9500),  # best both
        _make_result(hw_loop=4, cycles=900, cells=11000),
    ]

    sink = io.StringIO()
    with redirect_stdout(sink):
        winners = _select_hwloop_winners(results)

    assert winners.best_adp == 3
    assert winners.best_cycles == 3
    assert winners.same is True


def test_select_winners_excludes_failed_candidates():
    """``passed=False`` candidates are skipped during selection."""
    _, _, _, _select_hwloop_winners, _ = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=1000, cells=10000, passed=True),
        _make_result(hw_loop=3, cycles=0, cells=0, passed=False),  # failed
        _make_result(hw_loop=4, cycles=500, cells=12000, passed=True),
    ]

    sink = io.StringIO()
    with redirect_stdout(sink):
        winners = _select_hwloop_winners(results)

    # hw=3 is excluded; hw=4 wins both ADP and cycles.
    assert winners.best_adp == 4
    assert winners.best_cycles == 4


def test_select_winners_empty_results_returns_zero_winners():
    """An empty list -> ``SweepWinners(best_adp=0, best_cycles=0)``."""
    _, _, _, _select_hwloop_winners, _ = _import_legacy()

    sink = io.StringIO()
    with redirect_stdout(sink):
        winners = _select_hwloop_winners([])

    assert winners.best_adp == 0
    assert winners.best_cycles == 0


def test_select_winners_all_failed_returns_zero_winners():
    """When every candidate failed, the framework reports no winner."""
    _, _, _, _select_hwloop_winners, _ = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=0, cells=0, passed=False),
        _make_result(hw_loop=3, cycles=0, cells=0, passed=False),
    ]

    sink = io.StringIO()
    with redirect_stdout(sink):
        winners = _select_hwloop_winners(results)

    assert winners.best_adp == 0
    assert winners.best_cycles == 0


def test_select_winners_matches_legacy_pick_best():
    """Behavioural equivalence: the new selection picks the same
    ADP winner as the legacy ``pipeline.hwloop_sweep.pick_best``.
    """
    _, _, pick_best, _select_hwloop_winners, _ = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=1500, cells=10000),
        _make_result(hw_loop=3, cycles=1000, cells=11000),
        _make_result(hw_loop=4, cycles=800, cells=13000),
    ]

    sink = io.StringIO()
    with redirect_stdout(sink):
        legacy_winners, _ = pick_best(
            [
                _make_result(r.hw_loop, r.cycles, r.cells, r.passed)
                for r in results
            ]
        )
    sink2 = io.StringIO()
    with redirect_stdout(sink2):
        new_winners = _select_hwloop_winners(results)

    assert new_winners.best_adp == legacy_winners.best_adp
    assert new_winners.best_cycles == legacy_winners.best_cycles
    assert new_winners.same == legacy_winners.same


def test_print_hwloop_sweep_table_includes_markers():
    """The table prints ``◀ best ADP`` / ``◀ best cycles`` markers."""
    _, SweepWinners, _, _, _print_hwloop_sweep_table = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=1000, cells=10000),
        _make_result(hw_loop=3, cycles=800, cells=9500),
    ]
    winners = SweepWinners(best_adp=3, best_cycles=3, same=True)
    winners.best_adp_result = results[1]
    winners.best_cycles_result = results[1]

    sink = io.StringIO()
    with redirect_stdout(sink):
        _print_hwloop_sweep_table(results, winners)
    output = sink.getvalue()

    assert "best ADP" in output
    assert "best cycles" in output
    assert "1,000" in output
    assert "800" in output


def test_print_hwloop_sweep_table_marks_failures():
    """Failed candidates print a ``FAIL`` row with no metrics."""
    _, SweepWinners, _, _, _print_hwloop_sweep_table = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=1000, cells=10000),
        _make_result(hw_loop=3, cycles=0, cells=0, passed=False),
    ]
    winners = SweepWinners(best_adp=2, best_cycles=2, same=True)
    winners.best_adp_result = results[0]
    winners.best_cycles_result = results[0]

    sink = io.StringIO()
    with redirect_stdout(sink):
        _print_hwloop_sweep_table(results, winners)
    output = sink.getvalue()

    assert "FAIL" in output


def test_print_hwloop_sweep_table_separate_winners_message():
    """When ADP and cycles winners differ, both appear in the summary."""
    _, SweepWinners, _, _, _print_hwloop_sweep_table = _import_legacy()

    results = [
        _make_result(hw_loop=2, cycles=1000, cells=10000),
        _make_result(hw_loop=3, cycles=900, cells=10500),
        _make_result(hw_loop=4, cycles=800, cells=12000),
    ]
    winners = SweepWinners(best_adp=3, best_cycles=4, same=False)
    winners.best_adp_result = results[1]
    winners.best_cycles_result = results[2]

    sink = io.StringIO()
    with redirect_stdout(sink):
        _print_hwloop_sweep_table(results, winners)
    output = sink.getvalue()

    assert "HW_LOOP=3" in output
    assert "HW_LOOP=4" in output
    assert "best ADP" in output
    assert "best cycles" in output
