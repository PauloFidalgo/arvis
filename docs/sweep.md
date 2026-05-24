# Hyperparameter Sweeps in ARVIS

ARVIS Phase 5 generalises design-space exploration into a single
`SweepStrategy` framework.  Three production sweeps ship with the
framework, and adding a new one is a 30-line subclass.

## Vocabulary

| Term | Type | Role |
|---|---|---|
| `SweepCandidate` | frozen dataclass | one point in the design space, e.g. `{"FIFO_DEPTH": 4, "HW_LOOP": 2}` |
| `SearchSpace` | dataclass | the parameters and their candidate values |
| `SearchStrategy` | Protocol | enumerates candidates from a space; ships in 4 flavours |
| `SweepResult` | Pydantic model | one evaluated candidate (metrics + passed/failed) |
| `SweepDecision` | Pydantic Decision | the winner + every result, JSON-serialisable |
| `SweepStrategy` | abstract `OptimizationStrategy` | ties it all together |

## Search strategies

```python
from core.sweep import (
    GridSearch,        # exhaustive Cartesian product
    RandomSearch,      # n_trials uniform random samples (seeded)
    BayesianSearch,    # Optuna TPE-based adaptive search
    SuccessiveHalving, # Phase 5.2 stub; full halving in a follow-up
)
```

| Strategy | When to use | Cost |
|---|---|---|
| `GridSearch` | small spaces (≤30 candidates) | exhaustive: `O(prod(\|values_i\|))` |
| `RandomSearch(n_trials, seed)` | large spaces, want reproducibility | linear in `n_trials` |
| `BayesianSearch(n_trials, seed)` | expensive evaluator + adaptive search useful | linear in `n_trials`, smarter trials |
| `SuccessiveHalving` | budgeted multi-fidelity (Phase 5.2 = grid stub) | exponentially shrinks survivor set |

Bayesian search requires `optuna`; it's an optional extra:

```bash
uv add --optional sweep optuna
# or:
pip install 'cv32e40p-specializer[sweep]'
```

`BayesianSearch` raises a clear `ImportError` at construction if
optuna is missing.

## Built-in sweeps

### Single-parameter prefetch FIFO sweep

```python
from strategies.sweep import PrefetchFIFOSweep
from core.sweep import GridSearch

sweep = PrefetchFIFOSweep(
    rtl_root=cfg.rtl_root,
    output_dir=cfg.output_dir,
    hex_path=baseline_hex,
    candidates=(2, 4, 8),
    search=GridSearch(),
)
decision = sweep.analyze(workload, profile, target)

if decision.best is not None:
    best_depth = decision.best.candidate["FIFO_DEPTH"]
    print(f"Best FIFO depth: {best_depth}")
    print(f"  cycles: {decision.best.metrics['cycles']:,}")
    print(f"  cells:  {decision.best.metrics['cells']:,}")
    print(f"  ADP:    {decision.best.metrics['adp']:.2f}")
```

This is exactly what `pipeline.prefetch_sweep.sweep_prefetch_depth`
does internally; the legacy function survives as a thin shim
preserving the original `(best_depth, FIFOResult[])` return type
for compatibility with reporting code.

### Single-parameter HW_LOOP nest depth sweep

```python
from strategies.sweep import HWLoopDepthSweep

# Pre-compute candidate metrics via the legacy
# _sweep_hwloop_candidates flow, then feed them to the sweep.
table = {
    0: {"cycles": 1000.0, "cells": 38800.0, "passed": 1.0},
    2: {"cycles": 800.0,  "cells": 39000.0, "passed": 1.0},
    3: {"cycles": 750.0,  "cells": 39200.0, "passed": 1.0},
}

sweep = HWLoopDepthSweep(candidates_by_depth=table)
decision = sweep.analyze(workload, profile, target)
```

The HW_LOOP dual-compile machinery (Docker / GCC) still lives in
`pipeline/runner.py` because it's deeply tied to the prune/fuse
pipeline state.  A future phase can move it into a self-contained
evaluator.

### Multi-parameter sweep with Bayesian optimisation

The general-purpose API:

```python
from strategies.sweep import MultiParamSweep
from core.sweep import BayesianSearch, SweepCandidate

def my_evaluator(cand: SweepCandidate) -> dict[str, float]:
    """Build RTL with these overrides; simulate; synthesise; score."""
    fifo = cand["FIFO_DEPTH"]
    hw   = cand["HW_LOOP"]
    cache = cand["DCACHE_KB"]
    # ... build, sim, synth ...
    return {
        "cycles": cycles,
        "cells":  cells,
        "adp":    cycles * cells / 1e9,
        "passed": True,
    }

sweep = MultiParamSweep(
    parameters={
        "FIFO_DEPTH": [2, 4, 8, 16],
        "HW_LOOP":    [0, 2, 3, 4],
        "DCACHE_KB":  [4, 8, 16, 32],
    },
    evaluator=my_evaluator,
    search=BayesianSearch(n_trials=30, seed=42),
)
decision = sweep.analyze(workload, profile, target)
```

The full Cartesian product is 4×4×4 = 64 points; Bayesian search
typically finds the minimum in 20-30 evaluations.

### Custom cost functions

The framework's default cost is `metrics["adp"]` (lower is
better).  Override for custom objectives:

```python
def maximise_throughput(result: SweepResult) -> float:
    if not result.passed:
        return float("inf")
    # Negate to keep "lower is better" convention.
    return -result.metrics["throughput"]

sweep = MultiParamSweep(
    parameters=...,
    evaluator=...,
    cost=maximise_throughput,
)
```

For Pareto-front problems with multiple objectives, evaluate the
sweep with several cost functions and post-process the
`decision.all_results` table.

## Adding a new sweep

Five steps:

1. Define what to sweep -- the parameter set and candidate values:

   ```python
   space = SearchSpace(parameters={"BRANCH_PRED_DEPTH": [4, 8, 16, 32]})
   ```

2. Write an evaluator -- a callable that takes a
   `SweepCandidate` and returns a metrics dict:

   ```python
   def branch_pred_evaluator(cand: SweepCandidate) -> dict[str, float]:
       depth = cand["BRANCH_PRED_DEPTH"]
       # Build RTL with that depth, simulate, synthesise.
       return {"cycles": ..., "cells": ..., "adp": ..., "passed": True}
   ```

   Conventionally the evaluator lives in
   `targets/<core>/sweep_evaluators.py`.

3. Pick a cost function (defaults to `metrics["adp"]`).

4. Pick a search strategy (default `GridSearch`).

5. Subclass `SweepStrategy` and wire it together:

   ```python
   class BranchPredictorSweep(SweepStrategy):
       def __init__(self, *, rtl_root: str, output_dir: str, hex_path: str):
           super().__init__(
               space=SearchSpace(parameters={"BRANCH_PRED_DEPTH": [4, 8, 16, 32]}),
               evaluator=BranchPredEvaluator(rtl_root, output_dir, hex_path),
               cost=_adp_cost,
               search=GridSearch(),
               name_override="branch-pred-sweep",
           )
   ```

That's it -- the framework runs the loop, captures all results,
and selects the winner.

## Persistence

`SweepResult` and `SweepDecision` are Pydantic v2 models, so a
sweep result is JSON-serialisable for free:

```python
import json
decision = sweep.analyze(...)

# Dump to JSON.
payload = decision.model_dump_json()

# Replay later (skips the expensive sweep).
restored = SweepDecision.model_validate_json(payload)
print(f"Best: {restored.best.candidate['FIFO_DEPTH']}")
```

This is the foundation for caching sweep results across runs --
recommend dropping the JSON next to the workload's analysis
artifacts and reading it back when present.

## Testing

The Phase 5 test suite (``tests/portability/test_sweep.py``)
covers:

* `SweepCandidate` equality + hash + label
* `SearchSpace` size + names
* `GridSearch` exhaustive enumeration
* `RandomSearch` reproducibility (seeded) + dedup
* `BayesianSearch` end-to-end on a known parabola minimum
* `SweepStrategy.analyze` happy + sad paths (evaluator
  exceptions become `passed=False` results)
* `SweepResult` JSON round-trip
* `MultiParamSweep` Cartesian product
* `PrefetchFIFOEvaluator` synth cache + simulate-fail behaviour
* HWLoop sweep selection from a pre-computed table

Coverage is held to 90% across the new packages, including the
sweep code.

## See also

* `core/sweep.py` -- framework
* `strategies/sweep/` -- concrete sweep classes
* `targets/cv32e40p/sweep_evaluators.py` -- cv32e40p-specific
  evaluators
* `pipeline/prefetch_sweep.py` -- legacy shim around
  `PrefetchFIFOSweep`
