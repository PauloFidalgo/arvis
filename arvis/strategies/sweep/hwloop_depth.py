"""HW_LOOP nest-depth sweep.

Wraps the legacy
:func:`pipeline.runner._sweep_hwloop_candidates` flow in the
generalised :class:`core.sweep.SweepStrategy` API.  The legacy
flow generates candidate ELFs (one per nest depth) via Docker /
GCC, then evaluates each via Verilator + Yosys; this sweep
strategy consumes those pre-computed metrics and selects the
winner via the standard ADP cost.

The dual-compile remains in ``runner.py`` because it's deeply
intertwined with the prune/fuse pipeline state.  A future
iteration could move that work into a sweep evaluator that
invokes the Toolchain directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from arvis.core.sweep import (
    GridSearch,
    SearchSpace,
    SearchStrategy,
    SweepResult,
    SweepStrategy,
)


def _adp_cost(result: SweepResult) -> float:
    if not result.passed:
        return float("inf")
    cycles = result.metrics.get("cycles", 0)
    cells = result.metrics.get("cells", 0)
    if cycles <= 0 or cells <= 0:
        return float("inf")
    return cycles * cells / 1e9


@dataclass(init=False)
class HWLoopDepthSweep(SweepStrategy):
    """Sweep the HW_LOOP nest depth.

    Parameters
    ----------
    candidates_by_depth:
        Pre-computed metrics dict, keyed by depth.  Format::

            {
                0: {"cycles": 100, "cells": 38800, "passed": True},
                2: {"cycles": 80,  "cells": 39000, "passed": True},
                3: {"cycles": 75,  "cells": 39200, "passed": True},
            }

        Populate this from the legacy
        ``_sweep_hwloop_candidates`` results before invoking
        ``analyze``.
    """

    def __init__(
        self,
        *,
        candidates_by_depth: dict[int, dict[str, float]],
        search: SearchStrategy | None = None,
    ) -> None:
        from arvis.targets.cv32e40p.sweep_evaluators import HWLoopDepthEvaluator

        evaluator = HWLoopDepthEvaluator(candidates_by_depth=candidates_by_depth)
        depths = sorted(candidates_by_depth.keys())
        super().__init__(
            space=SearchSpace(parameters={"HW_LOOP": depths}),
            evaluator=evaluator,
            cost=_adp_cost,
            search=search or GridSearch(),
            name_override="hwloop-depth-sweep",
        )


__all__ = ["HWLoopDepthSweep"]
