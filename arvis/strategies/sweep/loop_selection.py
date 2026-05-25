"""Per-loop on/off selection via the sweep framework.

Replaces the legacy ``_select_beneficial_loops`` GA in
``pipeline/hwloop.py``.  The search space is one binary parameter
per eligible loop; the evaluator assembles the patched source with
the selected subset and measures cycles via Verilator.

The :class:`GeneticSearch` strategy handles the 3-tier logic:

* N = 1 → trivial (single candidate)
* N ≤ 6 → exhaustive (2^N via GridSearch fallback)
* N > 6 → GA + single-bit refinement

Usage
-----
::

    from arvis.strategies.sweep.loop_selection import LoopSelectionSweep

    sweep = LoopSelectionSweep(
        loop_keys=loop_keys,
        evaluator=evaluator,  # LoopSelectionEvaluator instance
    )
    decision = sweep.analyze(workload, profile, target)
    best_mask = decision.best.candidate.overrides  # {"loop_0": 1, "loop_1": 0, ...}
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arvis.core.sweep import (
    GeneticSearch,
    SearchSpace,
    SearchStrategy,
    SweepCandidate,
    SweepResult,
    SweepStrategy,
)


def _cycles_cost(result: SweepResult) -> float:
    """Lower cycles = better.  Failed evals get infinite cost."""
    if not result.passed:
        return float("inf")
    cycles = result.metrics.get("cycles", float("inf"))
    return float(cycles)


@dataclass(init=False)
class LoopSelectionSweep(SweepStrategy):
    """Binary on/off sweep over individual loops.

    Parameters
    ----------
    loop_keys:
        Ordered list of loop identifiers (typically
        ``(start_label, start_line, back_branch_insn)`` tuples
        cast to strings for parameter naming).
    evaluator:
        Callable that takes a :class:`SweepCandidate` and returns
        metrics dict with at least ``{"cycles": int, "passed": bool}``.
    search:
        Override the search strategy.  Default is
        :class:`GeneticSearch` which auto-selects exhaustive for
        N ≤ 6.
    seed:
        RNG seed for reproducibility.
    """

    def __init__(
        self,
        *,
        loop_keys: list[str],
        evaluator: Callable[[SweepCandidate], dict[str, float]],
        search: SearchStrategy | None = None,
        seed: int | None = None,
    ) -> None:
        N = len(loop_keys)
        # Parameter names: "loop_0", "loop_1", ...
        params = {f"loop_{i}": [0, 1] for i in range(N)}
        space = SearchSpace(parameters=params)

        if search is None:
            search = GeneticSearch(
                population_size=min(16, N),
                generations=10,
                elite=4,
                mutation_rate=0.1,
                exhaustive_threshold=6,
                seed=seed,
            )

        super().__init__(
            space=space,
            evaluator=evaluator,
            cost=_cycles_cost,
            search=search,
            name_override="loop-selection-sweep",
        )
        self._loop_keys = loop_keys

    @property
    def loop_keys(self) -> list[str]:
        """The ordered loop identifiers this sweep operates over."""
        return self._loop_keys

    def best_mask(self, decision) -> list[int]:
        """Extract the binary mask from a SweepDecision.

        Returns a list of 0/1 values in the same order as
        ``loop_keys``.
        """
        if decision.best is None:
            return [1] * len(self._loop_keys)
        overrides = decision.best.candidate.overrides
        return [int(overrides.get(f"loop_{i}", 1)) for i in range(len(self._loop_keys))]


__all__ = ["LoopSelectionSweep"]
