"""Prefetch buffer FIFO depth sweep -- legacy shim.

Phase 5 generalised the sweep mechanism into
:class:`core.sweep.SweepStrategy` and ships a
:class:`strategies.sweep.PrefetchFIFOSweep` subclass.  This
module is kept as a thin shim around that subclass so the
legacy CLI signature (used by ``pipeline/runner.py:run_pipeline``
to integrate the sweep into the per-benchmark flow) keeps
working unchanged.

Use the new API in new code::

    from arvis.strategies.sweep import PrefetchFIFOSweep
    sweep = PrefetchFIFOSweep(
        rtl_root=cfg.rtl_root,
        output_dir=cfg.output_dir,
        hex_path=baseline_hex,
        candidates=(2, 4, 8),
    )
    decision = sweep.analyze(workload, profile, target)

The shim translates the legacy return type
``Tuple[best_depth, list[FIFOResult]]`` for backwards
compatibility with reports/CSV writers that consume
``FIFOResult.adp`` directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


logger = logging.getLogger(__name__)


@dataclass
class FIFOResult:
    """Backwards-compatible result record.

    Mirrors the legacy dataclass so existing reporting code
    (``report/html_sections.py``, CSV writers) keeps working
    unchanged.
    """

    depth: int
    cycles: int = 0
    cells: int = 0
    passed: bool = False

    @property
    def adp(self) -> float:
        if self.cycles > 0 and self.cells > 0:
            return self.cycles * self.cells / 1e9
        return float("inf")


def sweep_prefetch_depth(
    cfg: ToolConfig,
    ctx: PipelineContext,
    candidates: list[int] | None = None,
) -> tuple[int, list[FIFOResult]]:
    """Sweep FIFO depths via the new :class:`SweepStrategy` framework.

    Returns ``(best_depth, results)``; ``best_depth=0`` means no
    change recommended.

    The signature is preserved for ``pipeline.runner.run_pipeline``;
    new code should call :class:`PrefetchFIFOSweep` directly.
    """
    from arvis.cli import print_warning

    if candidates is None:
        candidates = [2, 4, 8]

    print(f"\n  Prefetch FIFO sweep: depths {candidates}")

    hex_path = _find_baseline_hex(cfg)
    if not hex_path:
        print_warning("No baseline hex -- defaulting to 4")
        return 4, []

    sweep_dir = os.path.join(cfg.output_dir, "prefetch_sweep")
    os.makedirs(sweep_dir, exist_ok=True)

    # ── Delegate to the generalised framework ────────────────────
    from arvis.strategies.sweep import PrefetchFIFOSweep

    sweep = PrefetchFIFOSweep(
        rtl_root=cfg.rtl_root,
        output_dir=sweep_dir,
        hex_path=hex_path,
        candidates=candidates,
        sim_timeout_seconds=cfg.sim_timeout_seconds,
        verilator_timeout_cycles=cfg.verilator_timeout_cycles,
    )
    decision = sweep.analyze(workload=None, profile=None, target=None)  # type: ignore[arg-type]
    sweep.cleanup()

    # ── Translate SweepDecision back to legacy FIFOResult list ──
    results: list[FIFOResult] = []
    for r in decision.all_results:
        depth = int(r.candidate["FIFO_DEPTH"])
        cycles = int(r.metrics.get("cycles", 0))
        cells = int(r.metrics.get("cells", 0))
        results.append(FIFOResult(depth=depth, cycles=cycles, cells=cells, passed=r.passed))

    # Print the legacy table format so existing log scrapers keep working.
    _print_legacy_table(candidates, results, decision)

    if decision.best is None:
        print_warning("No valid results -- no change")
        return 0, results

    best_depth = int(decision.best.candidate["FIFO_DEPTH"])
    base_depth = candidates[0]
    base = next((r for r in results if r.depth == base_depth), None)
    if base is None or base.adp == float("inf"):
        return best_depth, results

    best_result = next(r for r in results if r.depth == best_depth)
    if best_result.adp < base.adp:
        delta_cycles = base.cycles - best_result.cycles
        delta_cells = best_result.cells - base.cells
        delta_pct = (base.adp - best_result.adp) / base.adp * 100
        print(
            f"\n  -> FIFO={best_depth}: {delta_cycles:,} fewer cycles, "
            f"+{delta_cells:,} cells, {delta_pct:.1f}% better ADP"
        )
        return best_depth, results
    print("\n  -> No ADP improvement -- keeping default")
    return 0, results


# ─── Internals: table printing + hex discovery ────────────────────


def _print_legacy_table(
    candidates: list[int],
    results: list[FIFOResult],
    decision,
) -> None:
    """Reproduce the legacy log output so reports/scrapers don't break."""
    base_depth = candidates[0]
    base = next((r for r in results if r.depth == base_depth and r.adp != float("inf")), None)
    best = (
        next(
            (r for r in results if r.depth == int(decision.best.candidate["FIFO_DEPTH"])),
            None,
        )
        if decision.best
        else None
    )

    print(f"\n  {'Depth':>5s}  {'Cycles':>12s}  {'Cells':>8s}  {'ADP':>10s}  {'vs base':>8s}")
    print(f"  {'─' * 5}  {'─' * 12}  {'─' * 8}  {'─' * 10}  {'─' * 8}")
    for r in results:
        if not r.passed:
            print(f"  {r.depth:>5d}  {'FAIL':>12s}")
            continue
        delta = (r.adp - base.adp) / base.adp * 100 if base and base.adp > 0 else 0
        marker = " ◀ best" if best and r.depth == best.depth else ""
        print(
            f"  {r.depth:>5d}  {r.cycles:>12,}  {r.cells:>8,}  "
            f"{r.adp:>10.2f}  {delta:>+7.1f}%{marker}"
        )


def _find_baseline_hex(cfg: ToolConfig) -> str:
    from arvis.config import BENCHMARKS

    bm = BENCHMARKS.get(cfg.benchmark_name, {})
    hex_name = bm.get("hex", "")
    if hex_name:
        for candidate in (
            os.path.join(cfg.benchmark_dir, hex_name + ".baseline"),
            os.path.join(cfg.benchmark_dir, hex_name),
        ):
            if os.path.exists(candidate):
                return candidate
    return ""
