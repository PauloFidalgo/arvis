"""Concrete :class:`core.verifier.Verifier` implementation backed by Verilator.

This is the bridge between the abstract :class:`Verifier` interface
(``core/verifier.py``) and the legacy
:class:`simulation.verilator_runner.VerilatorRunner`.  Phase 6 wires
this class into ``Pipeline.run()`` so the orchestrator can drive
simulation without knowing anything about the underlying
Verilator runner.

The legacy ``VerilatorRunner`` API splits the work into ``build_sim``
+ ``run_sim``; this verifier composes them so a single
:meth:`simulate` call handles both, mirroring the common pattern
used by the legacy ``pipeline.verification`` module.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from arvis.core.verifier import SimResult, Verifier

logger = logging.getLogger(__name__)


@dataclass
class _SimCfg:
    """Lightweight ``cfg`` shape that ``VerilatorRunner`` expects.

    Only the four fields the runner actually reads are surfaced;
    the full ``ToolConfig`` is overkill for the verifier's needs
    and pulls in the entire pipeline package.
    """

    rtl_root: str
    verilator_bin: str = "verilator"
    verilator_timeout_cycles: int = 10_000_000
    sim_timeout_seconds: int = 600


class VerilatorVerifier(Verifier):
    """Drive Verilator simulation through the unified
    :class:`core.verifier.Verifier` API.

    Parameters
    ----------
    rtl_root:
        Path to the canonical RTL tree (used by
        ``VerilatorRunner`` to locate the testbench when the
        per-variant workspace doesn't ship its own).
    sim_timeout_seconds:
        Wall-clock timeout passed to the underlying simulator.
        ``600`` is the legacy default; tighten for fast unit
        tests, loosen for long benchmarks.
    extra_flags:
        Optional ``+G<param>=<value>`` flags passed to Verilator's
        build step.  These typically come from
        ``ctx.verilator_extra_flags`` -- the value the legacy
        pruning pass writes to drive testbench parameter
        narrowing.

    Idempotency
    -----------
    Two simulations of the same ``(rtl_dir, hex_path)`` pair
    produce identical :class:`SimResult` (modulo wall-clock
    timing, which is excluded from equality).
    """

    def __init__(
        self,
        *,
        rtl_root: str | Path,
        sim_timeout_seconds: int = 600,
        verilator_bin: str = "verilator",
        extra_flags: list[str] | None = None,
    ) -> None:
        self._rtl_root = str(rtl_root)
        self._sim_timeout_seconds = sim_timeout_seconds
        self._verilator_bin = verilator_bin
        self._extra_flags: list[str] = list(extra_flags or [])

    @property
    def name(self) -> str:
        return "verilator"

    def simulate(
        self,
        rtl_dir: Path,
        hex_path: Path,
        max_cycles: int = 10_000_000,
        firmware_args: Mapping[str, str] | None = None,
    ) -> SimResult:
        """Build the simulator + run it; return a typed :class:`SimResult`.

        Parameters
        ----------
        rtl_dir:
            Path to the per-variant ``rtl/`` directory (the
            ``output_root/rtl`` produced by
            :class:`Pipeline._emit_variant`).
        hex_path:
            Path to the workload hex file (typically a
            ``benchmark_name.hex`` produced by GCC compilation).
        max_cycles:
            ``+maxcycles`` for the simulator.
        firmware_args:
            Reserved for future ``+plusarg`` extensions; currently
            ignored.  ``None`` is the documented default.
        """
        del firmware_args  # no plusarg support yet; reserved for v2

        from arvis.simulation.verilator_runner import VerilatorRunner

        sim_cfg = _SimCfg(
            rtl_root=self._rtl_root,
            verilator_bin=self._verilator_bin,
            verilator_timeout_cycles=max_cycles,
            sim_timeout_seconds=self._sim_timeout_seconds,
        )
        runner = VerilatorRunner(sim_cfg)

        # The legacy runner expects a "core root" rather than the
        # ``rtl/`` directory itself.  ``Pipeline._emit_variant``
        # produces a workspace where ``rtl_dir`` is exactly the
        # ``rtl/`` directory; the runner navigates up one level
        # internally to find the testbench.
        rtl_dir = Path(rtl_dir)
        sim_dir = rtl_dir.parent / "verilator_sim"

        try:
            ok, sim_bin, build_log = runner.build_sim(
                rtl_dir=str(rtl_dir),
                output_dir=str(sim_dir),
                extra_flags=self._extra_flags or None,
            )
            if not ok:
                logger.warning(
                    "Verilator build failed for %s; returning failed SimResult",
                    rtl_dir,
                )
                return SimResult(
                    test_passed=False,
                    extra={"build_log": str(build_log) if build_log else ""},
                )

            run_result = runner.run_sim(
                sim_binary=sim_bin,
                firmware_hex=str(os.path.abspath(hex_path)),
                max_cycles=max_cycles,
            )
            log_path = sim_dir / "sim.log"
            try:
                log_path.write_text(run_result.stdout or "")
            except OSError:
                log_path = None  # type: ignore[assignment]

            return SimResult(
                test_passed=bool(run_result.test_passed),
                total_cycles=int(run_result.total_cycles or 0),
                timeout=False,  # legacy runner doesn't distinguish timeout vs fail
                log_path=log_path,
                extra={
                    "return_code": run_result.return_code,
                    "sim_time_seconds": run_result.sim_time_seconds,
                },
            )
        except Exception:
            logger.exception("Soft-skip: Verilator simulation raised")
            return SimResult(test_passed=False)


__all__ = ["VerilatorVerifier"]
