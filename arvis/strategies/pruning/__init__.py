"""Pruning strategies.

A :class:`PruningStrategy` decides which features of the target
core can be removed for the workload at hand.  Currently:

- :class:`UsageDrivenPruner` — analyse the compiled binary's
  instruction set and disable features that go unused.

Future strategies in this package may include:
- ``BudgetPruner`` — remove until area is under a target.
- ``CycleDrivenPruner`` — keep features whose removal would
  regress cycles by more than a threshold.
- ``ManualPruner`` — read decisions from a YAML overlay.
"""

from arvis.strategies.pruning.usage_driven import UsageDrivenPruner

__all__ = ["UsageDrivenPruner"]
