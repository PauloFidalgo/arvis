"""Verifier abstraction.

A :class:`Verifier` runs an emitted RTL variant against a hex file
and returns a :class:`SimResult`.  The default implementation that
ships with ARVIS today is :class:`VerilatorVerifier`; alternative
backends (Icarus, ModelSim, FPGA) plug in by subclassing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class SimResult:
    """Outcome of one simulation run.

    Attributes
    ----------
    test_passed:
        True if the simulation reached a successful exit
        (typically ``EXIT_SUCCESS``).
    total_cycles:
        Total cycles consumed by the run.  ``-1`` when the
        simulator could not measure it.
    timeout:
        True if the run hit the ``+maxcycles`` cap.
    log_path:
        Path to the simulator's stdout log, if persisted.
    extra:
        Free-form ``{name: value}`` for tool-specific metrics
        (e.g. ``{"icache_hits": 12345}``).
    """

    test_passed: bool
    total_cycles: int = -1
    timeout: bool = False
    log_path: Optional[Path] = None
    extra: Mapping[str, object] = field(default_factory=dict)


class Verifier(ABC):
    """Abstract verifier.

    A verifier takes a workspace (already populated with the
    target's RTL plus any patches the pipeline has applied) and a
    hex file, and runs them against the target's testbench.

    Verifiers MUST be stateless across calls: two simulations of
    the same ``(workspace, hex)`` pair must produce the same
    :class:`SimResult` (modulo timing variability the verifier
    explicitly opts into).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (``"verilator"``, ``"icarus"``)."""

    @abstractmethod
    def simulate(
        self,
        rtl_dir: Path,
        hex_path: Path,
        max_cycles: int = 10_000_000,
        firmware_args: Mapping[str, str] = {},
    ) -> SimResult:
        """Run a simulation and return its result.

        Parameters
        ----------
        rtl_dir:
            Root of the RTL workspace.  The verifier knows how to
            locate the testbench inside this tree (typically by
            convention, e.g. ``example_tb/core/verilator/``).
        hex_path:
            Path to the program to load.
        max_cycles:
            Cap on simulation length.  Reaching the cap should
            return ``timeout=True`` rather than raising.
        firmware_args:
            Free-form args passed to the firmware (mirrors today's
            ``+name=value`` plusargs).
        """
