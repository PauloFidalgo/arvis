"""Width-narrowing strategies.

Each strategy in this package produces a :class:`WidthDecision`
that captures one or more datapath widths to narrow.  Currently:

- :class:`PCWidthNarrowing` — main pipeline PC
- :class:`HWLPAddrNarrowing` — hardware-loop address registers
- :class:`CounterWidthNarrowing` — hardware-loop counter register

The first acts on the binary's executable section size; the other
two act on hardware-loop-specific quantities.  All three share the
same shape: read a quantity from the workload profile, pick the
smallest power-of-two-ish width that fits, return a Decision.
"""

from arvis.strategies.width.pc_width import PCWidthNarrowing
from arvis.strategies.width.hwlp_addr import HWLPAddrNarrowing
from arvis.strategies.width.counter_width import CounterWidthNarrowing

__all__ = [
    "PCWidthNarrowing",
    "HWLPAddrNarrowing",
    "CounterWidthNarrowing",
]
