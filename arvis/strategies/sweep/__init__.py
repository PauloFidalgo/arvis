"""Generalised hyperparameter-sweep strategies.

Three sweep strategies ship in Phase 5:

* :class:`PrefetchFIFOSweep`   single-parameter sweep of
                                ``FIFO_DEPTH``
* :class:`HWLoopDepthSweep`    single-parameter sweep of
                                ``HW_LOOP``
* :class:`MultiParamSweep`     multi-parameter sweep with
                                user-supplied search space and
                                evaluator

Adding a new sweep is a 30-line subclass: provide the parameter
set, an evaluator, optionally a custom cost function, and pick a
search strategy.
"""

from arvis.strategies.sweep.hwloop_depth import HWLoopDepthSweep
from arvis.strategies.sweep.loop_selection import LoopSelectionSweep
from arvis.strategies.sweep.multi_param import MultiParamSweep
from arvis.strategies.sweep.prefetch_fifo import PrefetchFIFOSweep

__all__ = ["HWLoopDepthSweep", "LoopSelectionSweep", "MultiParamSweep", "PrefetchFIFOSweep"]
