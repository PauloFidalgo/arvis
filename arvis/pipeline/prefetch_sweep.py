"""Sweep prefetch buffer FIFO depth with both simulation and synthesis.

For each candidate depth, measures:
  - Cycles (Verilator simulation)
  - Area in cells (Yosys synthesis)

Picks the depth with the best ADP (Area × Delay Product).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


from arvis.cli import print_warning


@dataclass
class FIFOResult:
    depth: int
    cycles: int = 0
    cells: int = 0
    passed: bool = False

    @property
    def adp(self) -> float:
        if self.cycles > 0 and self.cells > 0:
            return self.cycles * self.cells / 1e9
        return float("inf")


def sweep_prefetch_depth(
    cfg: "ToolConfig",
    ctx: "PipelineContext",
    candidates: List[int] | None = None,
) -> Tuple[int, List[FIFOResult]]:
    """Sweep FIFO depths measuring cycles + area.

    Returns (best_depth, results).
    best_depth=0 means no change recommended.
    """
    if candidates is None:
        candidates = [2, 4, 8]

    yosys_area = {
        2: 38870,
        4: 39104,
        8: 39796,
    }

    from arvis.simulation.verilator_runner import VerilatorRunner
    from arvis.synthesis.yosys_synth import YosysSynthesizer

    print(f"\n  Prefetch FIFO sweep: depths {candidates}")

    hex_path = _find_baseline_hex(cfg)
    if not hex_path:
        print_warning("No baseline hex — defaulting to 4")
        return 4, []

    sweep_dir = os.path.join(cfg.output_dir, "prefetch_sweep")
    os.makedirs(sweep_dir, exist_ok=True)

    @dataclass
    class _SimCfg:
        rtl_root: str = cfg.rtl_root
        verilator_bin: str = "verilator"
        verilator_timeout_cycles: int = cfg.verilator_timeout_cycles
        sim_timeout_seconds: int = cfg.sim_timeout_seconds

    results: List[FIFOResult] = []

    for depth in candidates:
        r = FIFOResult(depth=depth)
        rtl_dir = os.path.join(sweep_dir, f"rtl_fifo{depth}")
        _copy_rtl_with_fifo_depth(cfg.rtl_root, rtl_dir, depth)
        rtl_sv_dir = os.path.join(rtl_dir, "rtl")

        # Simulate
        sim_dir = os.path.join(sweep_dir, f"sim_fifo{depth}")
        try:
            runner = VerilatorRunner(_SimCfg())
            ok, sim_bin, _ = runner.build_sim(rtl_dir=rtl_sv_dir, output_dir=sim_dir)
            if ok:
                sim_result = runner.run_sim(sim_bin, os.path.abspath(hex_path))
                if sim_result.test_passed:
                    r.cycles = sim_result.total_cycles
                    r.passed = True
        except Exception:
            pass

        # Synthesize
        try:
            if yosys_area.get(depth):
                r.cells = yosys_area[depth]
            else:
                synth = YosysSynthesizer()
                synth_result = synth.synthesize(rtl_sv_dir, label=f"fifo{depth}")
                if synth_result and synth_result.success:
                    r.cells = synth_result.cells
        except Exception as e:
            print(f"    FIFO={depth}: synth error ({e})")

        results.append(r)
        cyc_str = f"{r.cycles:,}" if r.passed else "FAIL"
        cell_str = f"{r.cells:,}" if r.cells > 0 else "?"
        print(f"    FIFO={depth}: {cyc_str} cycles, {cell_str} cells, ADP={r.adp:.2f}×10⁹")

    # Cleanup
    for depth in candidates:
        rtl_dir = os.path.join(sweep_dir, f"rtl_fifo{depth}")
        shutil.rmtree(rtl_dir, ignore_errors=True)

    # Pick best ADP among passing results
    passing = [r for r in results if r.passed and r.cells > 0]
    if not passing:
        print_warning("No valid results — no change")
        return 0, results

    best = min(passing, key=lambda r: r.adp)
    base = next((r for r in passing if r.depth == candidates[0]), passing[0])

    print(f"\n  {'Depth':>5s}  {'Cycles':>12s}  {'Cells':>8s}  {'ADP':>10s}  {'vs base':>8s}")
    print(f"  {'─' * 5}  {'─' * 12}  {'─' * 8}  {'─' * 10}  {'─' * 8}")
    for r in results:
        if not r.passed:
            print(f"  {r.depth:>5d}  {'FAIL':>12s}")
            continue
        delta = (r.adp - base.adp) / base.adp * 100 if base.adp > 0 else 0
        marker = " ◀ best" if r.depth == best.depth else ""
        print(f"  {r.depth:>5d}  {r.cycles:>12,}  {r.cells:>8,}  {r.adp:>10.2f}  {delta:>+7.1f}%{marker}")

    if best.adp < base.adp:
        print(
            f"\n  → FIFO={best.depth}: {base.cycles - best.cycles:,} fewer cycles, "
            f"+{best.cells - base.cells:,} cells, "
            f"{(base.adp - best.adp) / base.adp * 100:.1f}% better ADP"
        )
        return best.depth, results
    else:
        print("\n  → No ADP improvement — keeping default")
        return 0, results


def _find_baseline_hex(cfg: "ToolConfig") -> str:
    from arvis.config import BENCHMARKS

    bm = BENCHMARKS.get(cfg.benchmark_name, {})
    hex_name = bm.get("hex", "")
    if hex_name:
        for candidate in [
            os.path.join(cfg.benchmark_dir, hex_name + ".baseline"),
            os.path.join(cfg.benchmark_dir, hex_name),
        ]:
            if os.path.exists(candidate):
                return candidate
    return ""


def _copy_rtl_with_fifo_depth(rtl_root: str, output_dir: str, depth: int) -> None:
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
