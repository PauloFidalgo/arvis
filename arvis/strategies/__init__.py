"""Concrete optimization strategies.

Strategies are organised by role:

- ``strategies.width``     — datapath width narrowing
- ``strategies.pruning``   — feature removal
- ``strategies.fusion``    — custom instruction selection
- ``strategies.hwloop``    — hardware loop patching

Each role is a sub-package containing one file per concrete
strategy.  Strategies are independent of one another; the pipeline
composes them at runtime.

Phase 1 retrofits the existing ARVIS logic as default
implementations; Phase 2 swaps the legacy call sites in
``pipeline/runner.py`` to use them.
"""
