"""Concrete sweep evaluators for the cv32e40p target.

A sweep evaluator is a callable that takes a
:class:`SweepCandidate` (a parameter-override map) and returns a
metrics dict.  The framework
(:class:`core.sweep.SweepStrategy`) calls the evaluator once per
candidate, collects results, and picks the best.

These evaluators are target-specific because they:

1. Build modified RTL (translate ``{"FIFO_DEPTH": 4}`` into the
   right localparam rewrite in
   ``rtl/cv32e40p_prefetch_buffer.sv``).
2. Run cycle-accurate simulation via the bundled Verilator
   runner.
3. Run technology-independent synthesis via the bundled Yosys
   wrapper.

A different target (Ibex, Vex, ...) ships its own evaluator
file alongside its target package.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arvis.core.sweep import SweepCandidate


logger = logging.getLogger(__name__)


# ─── Synthesis area cache ─────────────────────────────────────────


# Yosys area numbers for the cv32e40p prefetch FIFO depth sweep.
# These are the canonical results from the original sweep on the
# baseline cv32e40p RTL; we cache them so casual sweeps don't have
# to re-run synthesis (it takes minutes per depth).  Pass
# ``use_cache=False`` to the evaluator to force a fresh synthesis.
_FIFO_AREA_CACHE: dict[int, int] = {
    2: 38870,
    4: 39104,
    8: 39796,
}


# ─── Prefetch FIFO evaluator ──────────────────────────────────────


@dataclass
class PrefetchFIFOEvaluator:
    """Evaluate one ``FIFO_DEPTH`` candidate.

    Parameters
    ----------
    rtl_root:
        Path to the cv32e40p RTL tree (the source-of-truth, not
        the per-variant copy).
    output_dir:
        Where per-candidate artifacts live (one subdirectory
        per candidate).
    hex_path:
        Pre-built workload hex used by the simulator.
    sim_timeout_seconds:
        Verilator simulation wall-clock timeout.
    verilator_timeout_cycles:
        Cycle-budget for the simulator (``+maxcycles``).
    use_cache:
        When ``True`` (default), use the canonical cached Yosys
        area for the depth instead of re-running synthesis.
    """

    rtl_root: str
    output_dir: str
    hex_path: str
    sim_timeout_seconds: int = 300
    verilator_timeout_cycles: int = 10_000_000
    use_cache: bool = True

    # ── Cleanup hook ─────────────────────────────────────────────
    keep_artifacts: bool = False
    """If True, leave per-candidate RTL + sim_dir on disk for
    debugging.  Default is False -- the directories get a
    ``shutil.rmtree`` after the metrics are collected.
    """

    _scratch: list[Path] = field(default_factory=list, repr=False)

    def __call__(self, candidate: SweepCandidate) -> dict[str, float]:
        """Evaluate one candidate, returning metrics.

        Returned keys:
          ``cycles``      Verilator's ``$cycles_total`` (0 on fail)
          ``cells``       Yosys cell count (0 on fail)
          ``adp``         cycles * cells / 1e9
          ``passed``      bool: did Verilator complete successfully
        """
        depth = int(candidate["FIFO_DEPTH"])

        rtl_dir = Path(self.output_dir) / f"rtl_fifo{depth}"
        sim_dir = Path(self.output_dir) / f"sim_fifo{depth}"
        if not self.keep_artifacts:
            self._scratch.append(rtl_dir)

        _copy_rtl_with_fifo_depth(self.rtl_root, str(rtl_dir), depth)

        cycles, passed = self._simulate(rtl_dir, sim_dir)
        cells = self._synthesize(rtl_dir, depth)

        adp = (cycles * cells / 1e9) if (cycles > 0 and cells > 0) else float("inf")
        return {
            "cycles": float(cycles),
            "cells": float(cells),
            "adp": adp,
            "passed": float(passed),
        }

    def cleanup(self) -> None:
        """Remove every per-candidate scratch directory we created."""
        if self.keep_artifacts:
            return
        for d in self._scratch:
            shutil.rmtree(d, ignore_errors=True)
        self._scratch.clear()

    # ── Internals ────────────────────────────────────────────────

    def _simulate(self, rtl_dir: Path, sim_dir: Path) -> tuple[int, bool]:
        """Build Verilator + run; return ``(cycles, passed)``."""
        try:
            from arvis.simulation.verilator_runner import VerilatorRunner

            runner = VerilatorRunner(self._sim_cfg())
            ok, sim_bin, _ = runner.build_sim(
                rtl_dir=str(rtl_dir / "rtl"),
                output_dir=str(sim_dir),
            )
            if not ok:
                return 0, False
            sim_result = runner.run_sim(sim_bin, os.path.abspath(self.hex_path))
            if not sim_result.test_passed:
                return 0, False
            return sim_result.total_cycles, True
        except Exception:
            logger.warning(
                "FIFO sweep: Verilator failed for depth=%s",
                rtl_dir.name,
                exc_info=True,
            )
            return 0, False

    def _synthesize(self, rtl_dir: Path, depth: int) -> int:
        """Yosys cell count for the candidate.  Uses the canonical
        cache when ``use_cache=True`` and the depth has a known
        value; falls back to a fresh Yosys invocation otherwise.
        """
        if self.use_cache and depth in _FIFO_AREA_CACHE:
            return _FIFO_AREA_CACHE[depth]

        try:
            from arvis.synthesis.yosys_synth import YosysSynthesizer

            synth = YosysSynthesizer()
            r = synth.synthesize(str(rtl_dir / "rtl"), label=f"fifo{depth}")
            if r and r.success:
                return r.cells
        except Exception:
            logger.warning("FIFO sweep: Yosys failed for depth=%d", depth, exc_info=True)
        return 0

    def _sim_cfg(self) -> Any:
        """Build the lightweight ``cfg`` shape ``VerilatorRunner``
        expects.  We inline a tiny dataclass rather than reaching
        for the full ``ToolConfig`` -- the simulator only reads
        four fields."""

        @dataclass
        class _SimCfg:
            rtl_root: str = self.rtl_root
            verilator_bin: str = "verilator"
            verilator_timeout_cycles: int = self.verilator_timeout_cycles
            sim_timeout_seconds: int = self.sim_timeout_seconds

        return _SimCfg()


# ─── HW_LOOP nest-depth evaluator ─────────────────────────────────


@dataclass
class HWLoopDepthEvaluator:
    """Evaluate one ``HW_LOOP`` candidate.

    The HW_LOOP sweep is fundamentally different from FIFO: each
    candidate produces a different fused-with-hwloop ELF (via the
    Docker GCC build), and the chosen depth dictates how many
    loop levels the hardware supports.  This evaluator wraps the
    legacy :func:`pipeline.runner._sweep_hwloop_candidates` flow
    so the new sweep API can reuse it without reimplementing the
    candidate generation + ELF dual-compile machinery.

    Phase 5 ships this as a thin shim that returns metrics for a
    pre-computed candidate; the legacy candidate-generation step
    still happens inside ``runner.py``.  A future iteration can
    move that into the sweep itself.
    """

    candidates_by_depth: dict[int, dict[str, float]] = field(default_factory=dict)
    """Pre-computed metrics indexed by HW_LOOP depth.  The legacy
    ``_sweep_hwloop_candidates`` populates this table during its
    candidate-evaluation loop; the sweep then just looks up by
    candidate.
    """

    def __call__(self, candidate: SweepCandidate) -> dict[str, float]:
        depth = int(candidate["HW_LOOP"])
        metrics = self.candidates_by_depth.get(depth)
        if metrics is None:
            return {"passed": 0.0, "adp": float("inf")}
        return metrics


# ─── Helpers ──────────────────────────────────────────────────────


def _copy_rtl_with_fifo_depth(rtl_root: str, output_dir: str, depth: int) -> None:
    """Copy the cv32e40p RTL tree and rewrite the prefetch
    ``localparam FIFO_DEPTH``.  Idempotent (re-copies on each call).
    """
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    shutil.copytree(rtl_root, output_dir)

    pfb = Path(output_dir) / "rtl" / "cv32e40p_prefetch_buffer.sv"
    if pfb.exists():
        text = pfb.read_text()
        text = re.sub(
            r"localparam FIFO_DEPTH\s*=\s*\d+;",
            f"localparam FIFO_DEPTH                     = {depth};",
            text,
        )
        pfb.write_text(text)


__all__ = [
    "HWLoopDepthEvaluator",
    "PrefetchFIFOEvaluator",
]
