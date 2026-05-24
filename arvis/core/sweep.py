"""Generalised hyperparameter-tuning framework.

A single sweep abstraction that subsumes today's three ad-hoc
sweeps (prefetch FIFO depth, HW_LOOP nest depth, CNT_WIDTH) and
generalises to multi-parameter Cartesian products and adaptive
Bayesian optimisation.

Vocabulary
----------

A :class:`SweepCandidate` is one point in the design space:
a mapping from parameter name to candidate value (e.g.
``{"FIFO_DEPTH": 4, "HW_LOOP": 2}``).

A :class:`SearchSpace` defines the parameter dimensions and their
valid candidate sets.

A :class:`SearchStrategy` enumerates :class:`SweepCandidate`
instances from a :class:`SearchSpace`.  Implementations:

* :class:`GridSearch`         exhaustive Cartesian product
* :class:`RandomSearch`       n_trials random samples (with a seed)
* :class:`BayesianSearch`     Optuna TPE-based adaptive search
* :class:`SuccessiveHalving`  budget-aware: keep the top-K after
                              each round, double the budget per
                              candidate

An *evaluator* is a callable that takes a :class:`SweepCandidate`
and returns a metrics dict.  The metrics dict must include
``passed: bool``; everything else is up to the evaluator (the
prefetch sweep returns ``cycles`` and ``cells``; a future cache
sweep might return ``hit_rate`` and ``area``).

A :class:`SweepStrategy` ties it all together: search strategy +
evaluator + cost function.  ``analyze()`` runs the search,
collects :class:`SweepResult` instances, and returns a
:class:`SweepDecision` carrying the winner.

Why this shape
--------------

* **Target-agnostic.**  ``core/sweep.py`` knows nothing about
  RTL or simulation.  Concrete evaluators live in target packages
  (``targets/cv32e40p/sweep_evaluators.py``) and translate
  candidates into per-target work (build RTL, run Verilator, run
  Yosys).
* **Composable.**  A multi-parameter sweep is just a
  :class:`SearchSpace` with multiple keys.  Single-parameter is
  the degenerate case.
* **Search-strategy-pluggable.**  Grid for small spaces, Bayesian
  for large ones, halving for expensive evaluations.
* **Reproducible.**  Random/Bayesian searches accept a seed.
* **Persistable.**  :class:`SweepResult` and :class:`SweepDecision`
  serialise to JSON via Pydantic, so a sweep result can be cached
  and replayed without re-running the evaluator.
"""

from __future__ import annotations

import itertools
import logging
import random
from abc import ABC
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from arvis.core.strategy import Decision, OptimizationStrategy

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


logger = logging.getLogger(__name__)


# ─── SweepCandidate ────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepCandidate:
    """One point in the design space.

    The ``overrides`` map is intentionally typed as
    ``Mapping[str, Any]`` rather than ``Mapping[str, int]`` because
    a future sweep may carry non-numeric parameters (e.g.
    ``{"branch_predictor": "gshare"}``).

    Equality + hash are derived from the overrides so candidates
    can live in sets and act as dict keys for memoisation.
    """

    overrides: Mapping[str, Any]

    def __post_init__(self) -> None:
        # Freeze the overrides to a tuple-of-pairs so the dataclass
        # remains hashable when ``overrides`` is a regular dict.
        # (Dataclasses frozen=True doesn't help if the field
        # contains a mutable type.)
        if not isinstance(self.overrides, _FrozenMap):
            object.__setattr__(self, "overrides", _FrozenMap(self.overrides))

    @property
    def label(self) -> str:
        """Human-readable identifier (e.g. ``"FIFO_DEPTH=4_HW_LOOP=2"``)."""
        return "_".join(f"{k}={v}" for k, v in sorted(self.overrides.items()))

    def __getitem__(self, key: str) -> Any:
        return self.overrides[key]


class _FrozenMap(Mapping[str, Any]):
    """Hashable, immutable view over a dict."""

    __slots__ = ("_data", "_hash")

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data: dict[str, Any] = dict(data)
        self._hash: int | None = None

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(tuple(sorted(self._data.items())))
        return self._hash

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"


# ─── SearchSpace ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchSpace:
    """The set of parameters to sweep, with discrete candidate values.

    Continuous parameters are represented as a discrete grid
    (e.g. ``hidden_dim=[64, 128, 256]``).  This is a deliberate
    simplification: hardware sweeps are almost always discrete
    (FIFO depths must be powers of two; nest depths are
    integers; cache sizes pick from a fixed list).
    """

    parameters: Mapping[str, Sequence[Any]]

    @property
    def size(self) -> int:
        """Total number of points in the Cartesian product."""
        n = 1
        for values in self.parameters.values():
            n *= len(values)
        return n

    def names(self) -> tuple[str, ...]:
        return tuple(self.parameters.keys())


# ─── SweepResult + SweepDecision ──────────────────────────────────


class SweepResult(BaseModel):
    """One evaluated candidate.

    Pydantic v2 ``BaseModel`` so reports / caching can serialise
    via ``model_dump_json``.  Note that ``candidate`` carries a
    :class:`SweepCandidate`, which is a frozen dataclass; we
    enable ``arbitrary_types_allowed`` to accept it, and we add a
    field serialiser that converts it to a plain dict for JSON
    output (the dataclass is reconstructable from the dict).
    """

    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
    )

    candidate: SweepCandidate
    metrics: dict[str, float] = Field(default_factory=dict)
    passed: bool = False
    error: str | None = None

    @field_serializer("candidate")
    def _serialize_candidate(self, candidate: SweepCandidate) -> dict[str, Any]:
        return {"overrides": dict(candidate.overrides)}

    @field_validator("candidate", mode="before")
    @classmethod
    def _coerce_candidate(cls, v: object) -> object:
        """Accept plain dicts (from JSON) or SweepCandidate."""
        if isinstance(v, SweepCandidate):
            return v
        if isinstance(v, dict) and "overrides" in v:
            return SweepCandidate(overrides=v["overrides"])
        return v

    @property
    def label(self) -> str:
        return self.candidate.label


class SweepDecision(Decision):
    """The winner of a sweep + the full result table.

    Attributes
    ----------
    best:
        The lowest-cost ``passed`` :class:`SweepResult`.  ``None``
        when no candidate passed (e.g. every variant failed
        simulation).  Consumers should treat ``None`` as "fall
        back to the default value".
    all_results:
        Every evaluated candidate, in evaluation order.  Useful
        for reports / charts.
    parameter_names:
        Names of the parameters that were swept.  Mirrors
        ``SearchSpace.names()`` at the time the sweep ran.
    """

    best: SweepResult | None = None
    all_results: tuple[SweepResult, ...] = ()
    parameter_names: tuple[str, ...] = ()

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.all_results if r.passed)

    @property
    def total_count(self) -> int:
        return len(self.all_results)


# ─── SearchStrategy hierarchy ─────────────────────────────────────


class SearchStrategy(Protocol):
    """How to enumerate candidates from a :class:`SearchSpace`."""

    def candidates(
        self, space: SearchSpace
    ) -> Iterable[SweepCandidate]:  # pragma: no cover - protocol
        ...

    def tell(self, candidate: SweepCandidate, score: float) -> None:  # pragma: no cover - protocol
        """Optional adaptive feedback hook (Bayesian only).

        Default-implementing classes may ignore this; only
        :class:`BayesianSearch` and similar adaptive searches use
        it to update their posterior.
        """
        ...


@dataclass
class GridSearch:
    """Exhaustive Cartesian product.

    The simplest search strategy: enumerate every combination of
    parameter values.  Time is ``O(prod(|values_i|))``; use only
    for small spaces (the existing FIFO sweep is 3 candidates).
    """

    def candidates(self, space: SearchSpace) -> list[SweepCandidate]:
        names = list(space.parameters.keys())
        value_lists = [list(space.parameters[n]) for n in names]
        return [
            SweepCandidate(overrides=dict(zip(names, combo, strict=True)))
            for combo in itertools.product(*value_lists)
        ]

    def tell(self, candidate: SweepCandidate, score: float) -> None:
        # Non-adaptive; ignore feedback.
        del candidate, score


@dataclass
class RandomSearch:
    """Uniform random sampling.

    Useful when a grid is too large but the parameter space is
    well-understood and uniform sampling suffices.  Reproducible
    via ``seed``.
    """

    n_trials: int = 20
    seed: int | None = None

    def candidates(self, space: SearchSpace) -> list[SweepCandidate]:
        rng = random.Random(self.seed)
        names = list(space.parameters.keys())
        value_lists = [list(space.parameters[n]) for n in names]

        # Avoid duplicates when n_trials approaches the grid size.
        max_unique = 1
        for v in value_lists:
            max_unique *= len(v)
        n = min(self.n_trials, max_unique)

        seen: set[SweepCandidate] = set()
        out: list[SweepCandidate] = []
        # Bound the number of attempts to prevent infinite loops in
        # pathological random orderings.
        attempts = 0
        max_attempts = n * 50
        while len(out) < n and attempts < max_attempts:
            attempts += 1
            picks = [rng.choice(v) for v in value_lists]
            cand = SweepCandidate(overrides=dict(zip(names, picks, strict=True)))
            if cand in seen:
                continue
            seen.add(cand)
            out.append(cand)
        return out

    def tell(self, candidate: SweepCandidate, score: float) -> None:
        del candidate, score


@dataclass
class BayesianSearch:
    """Adaptive search via Optuna's TPE sampler.

    TPE (Tree-structured Parzen Estimator) maintains a posterior
    over the cost function and proposes the next candidate that
    maximises Expected Improvement.  Use for large discrete spaces
    where evaluation is expensive (e.g. Verilator + synthesis).

    Optuna is an optional dependency; importing this class without
    optuna installed is fine but instantiating it raises
    :class:`ImportError`.
    """

    n_trials: int = 20
    seed: int | None = None

    def __post_init__(self) -> None:
        try:
            import optuna  # noqa: F401  (imported lazily in candidates())
        except ImportError as exc:
            raise ImportError(
                "BayesianSearch requires optuna; install with "
                "`uv add --optional sweep optuna` or "
                "`pip install optuna`."
            ) from exc

    def candidates(self, space: SearchSpace) -> Iterable[SweepCandidate]:
        """Yield candidates one at a time, suspending on each yield
        for the caller to ``tell()`` the score before proposing
        the next."""
        import optuna

        # Suppress Optuna's per-trial INFO chatter; we log via our
        # own structured logger.
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        sampler = optuna.samplers.TPESampler(seed=self.seed)
        study = optuna.create_study(direction="minimize", sampler=sampler)

        names = list(space.parameters.keys())
        value_lists = [list(space.parameters[n]) for n in names]

        for _ in range(self.n_trials):
            trial = study.ask()
            picks = []
            for n, vs in zip(names, value_lists, strict=True):
                # Use ``suggest_categorical`` for discrete spaces
                # so Optuna treats the values as fixed choices.
                picks.append(trial.suggest_categorical(n, vs))
            cand = SweepCandidate(overrides=dict(zip(names, picks, strict=True)))

            self._pending_trial = trial
            self._study = study
            yield cand

    def tell(self, candidate: SweepCandidate, score: float) -> None:
        """Feed back the cost so TPE can update its posterior."""
        del candidate
        if not hasattr(self, "_pending_trial") or not hasattr(self, "_study"):
            return
        self._study.tell(self._pending_trial, score)


@dataclass
class SuccessiveHalving:
    """Budget-aware halving (Successive Halving Algorithm, SHA).

    Round 1: evaluate every candidate with budget ``b``.  Keep the
    top ``ceil(N / reduction)`` by cost; double the budget; repeat
    until one survivor.

    Useful when each evaluation has a tunable cost (e.g. number of
    simulation cycles, dataset size).  The evaluator must accept
    an optional ``budget`` argument; the ``budget`` field on this
    class is the *initial* per-candidate budget.

    Phase 5.2 ships a stub implementation: it just performs an
    exhaustive grid evaluation.  The full halving logic is left
    for a future iteration; the API is in place so callers can
    swap it in without changing call sites.
    """

    initial_budget: int = 100
    reduction_factor: int = 2

    def candidates(self, space: SearchSpace) -> list[SweepCandidate]:
        # Phase 5.2 fallback: exhaustive grid.  Real halving comes
        # in a follow-up commit; this keeps the public API stable.
        return GridSearch().candidates(space)

    def tell(self, candidate: SweepCandidate, score: float) -> None:
        del candidate, score


# ─── SweepStrategy ────────────────────────────────────────────────


# Default cost: assume metrics["adp"] (area * delay product); fall
# back to inf when missing.
def _default_cost(result: SweepResult) -> float:
    return result.metrics.get("adp", float("inf"))


@dataclass
class SweepStrategy(OptimizationStrategy[SweepDecision], ABC):
    """Hyperparameter tuning over a :class:`SearchSpace`.

    Subclasses (or callers) provide:

    * ``space``       what to sweep (single or multi-parameter)
    * ``evaluator``   how to measure one candidate
    * ``cost``        objective function (lower is better)
    * ``search``      enumeration strategy (Grid / Random /
                      Bayesian / SuccessiveHalving)

    The framework runs the loop, captures all results, and
    selects the winner.  The :meth:`analyze` return value is a
    :class:`SweepDecision`; the rendering layer can extract the
    best-candidate overrides and merge them with other
    decisions.
    """

    space: SearchSpace
    evaluator: Callable[[SweepCandidate], dict[str, float]]
    cost: Callable[[SweepResult], float] = _default_cost
    search: SearchStrategy = field(default_factory=GridSearch)
    name_override: str | None = None

    # Keep the OptimizationStrategy contract.
    @property
    def name(self) -> str:
        return self.name_override or self.__class__.__name__

    def applicable(self, workload: Workload, target: TargetCore) -> bool:
        """Default: always applicable.  Subclasses may override
        when their parameters require specific target capabilities
        (e.g. ``HW_LOOP`` sweep requires the target to expose a
        ``HW_LOOP`` parameter).
        """
        del workload, target
        return True

    def analyze(
        self,
        workload: Workload,
        profile: WorkloadProfile,
        target: TargetCore,
    ) -> SweepDecision:
        """Run the sweep.

        For each candidate from :attr:`search`:
          1. Call :attr:`evaluator(candidate)` to get metrics.
          2. Wrap the metrics in a :class:`SweepResult`.
          3. Feed the cost back to the search via ``tell`` (for
             adaptive strategies).

        Then pick the lowest-cost passing candidate as ``best``.
        """
        del workload, profile, target  # subclasses may use; default ignores

        results: list[SweepResult] = []
        for cand in self.search.candidates(self.space):
            try:
                metrics = self.evaluator(cand)
                passed = bool(metrics.get("passed", True))
                result = SweepResult(candidate=cand, metrics=metrics, passed=passed)
            except Exception as exc:
                logger.warning(
                    "Sweep evaluator failed for %s; recording as not-passed",
                    cand.label,
                    exc_info=True,
                )
                result = SweepResult(
                    candidate=cand,
                    metrics={},
                    passed=False,
                    error=str(exc),
                )

            results.append(result)
            score = self.cost(result) if result.passed else float("inf")
            self.search.tell(cand, score)

        passing = [r for r in results if r.passed]
        best = min(passing, key=self.cost) if passing else None
        return SweepDecision(
            best=best,
            all_results=tuple(results),
            parameter_names=self.space.names(),
        )


__all__ = [
    "BayesianSearch",
    "GridSearch",
    "RandomSearch",
    "SearchSpace",
    "SearchStrategy",
    "SuccessiveHalving",
    "SweepCandidate",
    "SweepDecision",
    "SweepResult",
    "SweepStrategy",
]
