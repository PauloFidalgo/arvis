"""Multi-parameter Cartesian/Bayesian sweep.

The most general sweep: pass a parameter dictionary, an
evaluator, and a search strategy.  The framework runs the loop
and picks the winner.

Example::

    sweep = MultiParamSweep(
        parameters={"FIFO_DEPTH": [2, 4, 8], "HW_LOOP": [0, 2, 3, 4]},
        evaluator=my_evaluator,
        search=BayesianSearch(n_trials=20, seed=42),
    )
    decision = sweep.analyze(workload, profile, target)

This is what production ML autoschedulers (TVM AutoTVM, Halide
autoscheduler) use under the hood, scaled down to the discrete
parameter sets typical of hardware tuning.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from arvis.core.sweep import (
    GridSearch,
    SearchSpace,
    SearchStrategy,
    SweepCandidate,
    SweepResult,
    SweepStrategy,
)


def _default_cost(result: SweepResult) -> float:
    if not result.passed:
        return float("inf")
    return result.metrics.get("adp", float("inf"))


@dataclass(init=False)
class MultiParamSweep(SweepStrategy):
    """Sweep an arbitrary parameter set.

    Parameters
    ----------
    parameters:
        ``{name: [candidate_values, ...]}`` dictionary.  Every
        combination is exposed to the search strategy.
    evaluator:
        Callable taking a :class:`SweepCandidate` and returning a
        metrics dict (must include ``passed``).
    cost:
        Optional cost callable; default is
        ``metrics["adp"]``.  Lower is better.
    search:
        Optional :class:`SearchStrategy`.  Defaults to
        :class:`GridSearch`; pass :class:`BayesianSearch` or
        :class:`RandomSearch` for adaptive / sampled search.
    name:
        Optional name for reports / logs.
    """

    def __init__(
        self,
        *,
        parameters: Mapping[str, Sequence[object]],
        evaluator: Callable[[SweepCandidate], dict[str, float]],
        cost: Callable[[SweepResult], float] = _default_cost,
        search: SearchStrategy | None = None,
        name: str = "multi-param-sweep",
    ) -> None:
        super().__init__(
            space=SearchSpace(parameters=parameters),
            evaluator=evaluator,
            cost=cost,
            search=search or GridSearch(),
            name_override=name,
        )


__all__ = ["MultiParamSweep"]
