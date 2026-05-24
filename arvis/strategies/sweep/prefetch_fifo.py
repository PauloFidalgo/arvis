"""Prefetch-buffer FIFO depth sweep.

Refactored from the legacy
:func:`pipeline.prefetch_sweep.sweep_prefetch_depth` into the
generalised :class:`core.sweep.SweepStrategy` framework.

Public surface::

    sweep = PrefetchFIFOSweep(
        candidates=(2, 4, 8),
        rtl_root=cfg.rtl_root,
        output_dir=cfg.output_dir,
        hex_path=baseline_hex,
        search=GridSearch(),  # or RandomSearch / BayesianSearch
    )
    decision = sweep.analyze(workload, profile, target)
    best_depth = decision.best.candidate["FIFO_DEPTH"] if decision.best else 0
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arvis.core.sweep import (
    GridSearch,
    SearchSpace,
    SearchStrategy,
    SweepResult,
    SweepStrategy,
)


def _adp_cost(result: SweepResult) -> float:
    """ADP = cycles * cells.  Lower is better.

    Failed runs return ``+inf`` so they're never selected.
    """
    if not result.passed:
        return float("inf")
    cycles = result.metrics.get("cycles", 0)
    cells = result.metrics.get("cells", 0)
    if cycles <= 0 or cells <= 0:
        return float("inf")
    return cycles * cells / 1e9


@dataclass(init=False)
class PrefetchFIFOSweep(SweepStrategy):
    """Sweep ``FIFO_DEPTH`` across a small set of candidates.

    The default candidate set ``(2, 4, 8)`` matches the legacy
    sweep.  Pass a custom set for non-power-of-two cores or when
    you want to evaluate a wider grid.
    """

    def __init__(
        self,
        *,
        rtl_root: str,
        output_dir: str,
        hex_path: str,
        candidates: Sequence[int] = (2, 4, 8),
        search: SearchStrategy | None = None,
        sim_timeout_seconds: int = 300,
        verilator_timeout_cycles: int = 10_000_000,
        use_synth_cache: bool = True,
        keep_artifacts: bool = False,
    ) -> None:
        from arvis.targets.cv32e40p.sweep_evaluators import PrefetchFIFOEvaluator

        evaluator = PrefetchFIFOEvaluator(
            rtl_root=rtl_root,
            output_dir=output_dir,
            hex_path=hex_path,
            sim_timeout_seconds=sim_timeout_seconds,
            verilator_timeout_cycles=verilator_timeout_cycles,
            use_cache=use_synth_cache,
            keep_artifacts=keep_artifacts,
        )
        super().__init__(
            space=SearchSpace(parameters={"FIFO_DEPTH": list(candidates)}),
            evaluator=evaluator,
            cost=_adp_cost,
            search=search or GridSearch(),
            name_override="prefetch-fifo-sweep",
        )
        self._evaluator = evaluator

    def cleanup(self) -> None:
        """Remove any per-candidate scratch directories the
        evaluator created.  Safe to call multiple times.
        """
        self._evaluator.cleanup()


__all__ = ["PrefetchFIFOSweep"]
