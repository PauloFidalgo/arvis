"""Hardware-loop strategies.

A :class:`LoopStrategy` decides whether (and how) to use the
target's hardware-loop unit.  Currently:

- :class:`CV32E40PHWLoop` — the PULP-style ``lp.start`` /
  ``lp.end`` / ``lp.count`` instructions implemented by
  cv32e40p (and its merged hwloop GCC fork).

Future strategies may include:
- ``RVVLoop`` — RISC-V vector-extension loops.
- ``LLVMSWP`` — software pipelining (no hardware-loop unit).
- ``NoLoopOpt`` — baseline (skip the strategy entirely).
"""

from arvis.strategies.hwloop.cv32e40p_pulp import CV32E40PHWLoop

__all__ = ["CV32E40PHWLoop"]
