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


# ─── Phase 6: Pipeline-driven HW_LOOP evaluator ───────────────────


@dataclass
class HWLoopVariantEvaluator:
    """Evaluate one HW_LOOP candidate via the unified Pipeline path.

    Replaces the per-candidate sim+synth body of
    :func:`pipeline.runner._sweep_hwloop_candidates` with a single
    call that goes through the new
    :class:`core.verifier.Verifier` /
    :class:`core.synthesis.SynthesisFlow` interfaces.

    The candidate is expected to carry its own pre-built hex/elf
    paths (the dual-compile is still upstream of this evaluator,
    inside ``runner.py``).  The evaluator's job is to:

    1. Build the per-candidate workspace via
       :class:`Pipeline._emit_variant` so the RTL reflects the
       chosen ``HW_LOOP`` depth.
    2. Run :class:`VerilatorVerifier` to measure cycles.
    3. Run :class:`YosysSynthesisFlow` to measure cells.
    4. Return the standard ``{cycles, cells, adp, passed}`` dict
       the sweep framework expects.

    Parameters
    ----------
    target:
        :class:`CV32E40P` instance (or compatible) -- used by
        :class:`Pipeline._emit_variant` to allocate workspaces.
    pipeline:
        Pre-built :class:`core.pipeline.Pipeline` carrying the
        target + verifier + synthesis flow.  Tests may pass a
        mock pipeline; the production runner builds one from its
        :class:`ToolConfig`.
    output_dir:
        Where per-candidate workspace artifacts live.  One
        ``rtl_hwloop_eval_<depth>/`` subdirectory per candidate.
    hex_path_for:
        Callable mapping ``(candidate) -> str`` returning the hex
        file to simulate.  The runner provides this so the
        evaluator doesn't have to know where ``cand.fused_hex``
        is stored.
    base_decisions:
        Optional dict of pre-built decisions (Prune, Fusion) that
        the variant should also carry.  The HW_LOOP sweep usually
        runs after pruning + fusion so the per-candidate variant
        is the legacy "ALL with depth=N" variant.
    """

    target: object  # CV32E40P; typed as object to avoid circular import
    pipeline: object  # core.pipeline.Pipeline
    output_dir: Path
    hex_path_for: object  # Callable[[SweepCandidate], str]
    base_decisions: dict[str, object] = field(default_factory=dict)

    def __call__(self, candidate: SweepCandidate) -> dict[str, float]:
        from arvis.core.pipeline import VariantConfig
        from arvis.core.strategy import LoopDecision

        depth = int(candidate["HW_LOOP"])

        # Build a LoopDecision for this candidate; merge with the
        # base decisions (prune + fusion).
        decisions_by_kind: dict[str, list[object]] = {
            kind: [d] for kind, d in self.base_decisions.items()
        }
        decisions_by_kind["LoopDecision"] = [
            LoopDecision(
                nest_depth=depth,
                counter_width=12,
                addr_width=14,
            )
        ]

        # The variant config carries every active decision kind.
        variant = VariantConfig(
            label=f"hwloop_eval_{depth}",
            decision_kinds=frozenset(decisions_by_kind.keys()),
        )

        try:
            # ``Pipeline._emit_variant`` is a private helper; calling
            # it here is intentional -- the evaluator is the canonical
            # consumer of one-variant emission.
            self.pipeline._output_root_for_workload = (  # type: ignore[attr-defined]
                lambda wl: self.output_dir
            )
            variant_result = self.pipeline._emit_variant(  # type: ignore[attr-defined]
                variant,
                decisions_by_kind,
                workload=None,
            )
            rtl_dir = variant_result.rtl_dir
        except Exception:
            logger.exception(
                "HWLoopVariantEvaluator: emit_variant failed for depth=%d",
                depth,
            )
            return {
                "cycles": 0.0,
                "cells": 0.0,
                "adp": float("inf"),
                "passed": 0.0,
            }

        # Resolve the hex path for this candidate.
        try:
            hex_path = self.hex_path_for(candidate)  # type: ignore[operator]
        except Exception:
            logger.exception(
                "HWLoopVariantEvaluator: hex_path_for failed for depth=%d",
                depth,
            )
            return {
                "cycles": 0.0,
                "cells": 0.0,
                "adp": float("inf"),
                "passed": 0.0,
            }

        # Simulate.
        verifier = self.pipeline.verifier  # type: ignore[attr-defined]
        sim_result = verifier.simulate(rtl_dir=rtl_dir, hex_path=Path(hex_path))
        cycles = sim_result.total_cycles if sim_result.test_passed else 0
        passed = bool(sim_result.test_passed)

        # Synthesize.
        synth = self.pipeline.synthesis  # type: ignore[attr-defined]
        cells = 0
        if synth is not None:
            synth_result = synth.synthesize(rtl_dir=rtl_dir)
            if synth_result.cell_count is not None:
                cells = int(synth_result.cell_count)

        adp = (cycles * cells / 1e9) if (cycles > 0 and cells > 0) else float("inf")
        return {
            "cycles": float(cycles),
            "cells": float(cells),
            "adp": adp,
            "passed": float(passed),
        }


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


# ─── Phase 6.7: Per-loop selection evaluator ──────────────────────


@dataclass
class LoopSelectionEvaluator:
    """Evaluate one loop on/off mask via assemble + simulate.

    Each :class:`SweepCandidate` carries ``loop_0``, ``loop_1``,
    ... parameters (0 or 1).  The evaluator:

    1. Builds the binary mask from the candidate overrides.
    2. Patches the source assembly with only the enabled loops.
    3. Assembles via Docker or native GCC.
    4. Simulates via the provided sim binary.
    5. Returns ``{"cycles": int, "passed": bool}``.

    Parameters
    ----------
    src_asm:
        Path to the merged assembly source.
    loop_keys:
        Ordered list of ``(start_label, start_line, back_branch_insn)``
        tuples identifying each eligible loop.
    hw_loop:
        HW_LOOP nest depth (2, 3, 4, or 6).
    bm_dir:
        Benchmark directory (working dir for assembly).
    sim_bin:
        Path to the Verilator sim binary.
    link_script:
        Linker script filename (relative to bm_dir).
    crt0:
        CRT0 filename (relative to bm_dir).
    image:
        Docker image for assembly (None = native GCC).
    specializer_dir:
        Path to the specializer root (for Docker builds).
    encoding:
        HW_LOOP encoding object (passed to HWLoopGenerator).
    fifo_depth:
        Prefetch FIFO depth for loop detection.
    """

    src_asm: Path
    loop_keys: list
    hw_loop: int
    bm_dir: Path
    sim_bin: str
    link_script: str = "link_cv32e40p.ld"
    crt0: str = "crt0_cv32e40p.S"
    image: str | None = None
    specializer_dir: Path | None = None
    encoding: object = None
    fifo_depth: int | None = None
    extra_ldflags: list | None = None

    def __call__(self, candidate: SweepCandidate) -> dict[str, float]:
        """Evaluate one loop mask candidate."""
        import subprocess as _sp

        from arvis.analysis.hwloop import AsmLoopDetector
        from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator

        N = len(self.loop_keys)
        mask = [int(candidate.overrides.get(f"loop_{i}", 1)) for i in range(N)]

        # Patch assembly with selected loops
        asm_text = self.src_asm.read_text()
        det = AsmLoopDetector(str(self.src_asm), fifo_depth=self.fifo_depth)
        det.find_all_loops()

        enabled = set(self.loop_keys[i] for i, b in enumerate(mask) if b)
        for loop in det.all_loops:
            k = (loop.start_label, loop.start_line, loop.back_branch_insn)
            loop.hw_eligible = k in enabled

        gen = HWLoopGenerator(det, hw_loop=self.hw_loop, encoding=self.encoding)
        gen.generate()
        patcher = AsmPatcher(gen, asm_text)
        patched = patcher.patch()

        # Write temp files
        tmp_s = self.bm_dir / "_sweep_loop_sel.s"
        tmp_elf = self.bm_dir / "_sweep_loop_sel.elf"
        tmp_hex = self.bm_dir / "_sweep_loop_sel.hex"
        tmp_s.write_text(patched)

        # Assemble
        if self.image and self.specializer_dir:
            from arvis.pipeline.hwloop import _assemble_patched

            ok = _assemble_patched(
                self.specializer_dir,
                self.bm_dir,
                tmp_s,
                "_sweep_loop_sel.elf",
                "_sweep_loop_sel.hex",
                image=self.image,
            )
            if not ok:
                return {"cycles": 0.0, "passed": 0.0}
        else:
            ret = _sp.call(
                [
                    "riscv32-unknown-elf-gcc",
                    "-march=rv32imc_zicsr",
                    "-mabi=ilp32",
                    "-static",
                    "-nostdlib",
                    "-nostartfiles",
                    "-T",
                    self.link_script,
                    "-Wl,--gc-sections",
                    "-o",
                    str(tmp_elf),
                    self.crt0,
                    str(tmp_s),
                    *(self.extra_ldflags or []),
                ],
                timeout=30,
                cwd=str(self.bm_dir),
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            if ret != 0:
                return {"cycles": 0.0, "passed": 0.0}
            _sp.call(
                [
                    "riscv32-unknown-elf-objcopy",
                    "-O",
                    "verilog",
                    str(tmp_elf),
                    str(tmp_hex),
                ],
                timeout=10,
                cwd=str(self.bm_dir),
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )

        # Simulate
        try:
            result = _sp.run(
                [self.sim_bin, f"+firmware={tmp_hex}", "+maxcycles=20000000"],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (_sp.TimeoutExpired, FileNotFoundError):
            return {"cycles": 0.0, "passed": 0.0}

        for line in result.stdout.splitlines():
            if "SUCCESS" in line:
                import re as _re

                m = _re.search(r"after (\d+) cycles", line)
                if m:
                    return {"cycles": float(m.group(1)), "passed": 1.0}

        return {"cycles": 0.0, "passed": 0.0}


__all__ = [
    "HWLoopDepthEvaluator",
    "HWLoopVariantEvaluator",
    "LoopSelectionEvaluator",
    "PrefetchFIFOEvaluator",
]
