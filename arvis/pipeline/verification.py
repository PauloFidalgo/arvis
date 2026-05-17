"""
Phase 7: RTL Verification.

Runs Yosys synthesis for area estimation and Verilator simulation
to verify functional correctness of the specialized RTL.

Three simulation configs:
  - Baseline:      original RTL + original hex
  - Fused:         fused RTL (no pruning) + fused hex
  - Fused+Pruned:  pruned+fused RTL + fused hex
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


# ═══════════════════════════════════════════════════════════════════════════
# Synthesis
# ═══════════════════════════════════════════════════════════════════════════


def run_synthesis(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Run Yosys synthesis: baseline vs current RTL output.

    Called once after the final RTL is ready (pruned+fused or pruned-only).
    """
    from arvis.cli import print_info, print_section, print_warning
    from arvis.synthesis.yosys_synth import YosysSynthesizer

    yosys = YosysSynthesizer()
    if not yosys.available:
        print_warning("Yosys not found — skipping synthesis area estimation")
        return

    print_section("SYNTHESIS AREA ESTIMATION (Yosys)")
    print_info(f"Yosys version: {yosys.version}")

    ctx.synth_comparison = yosys.compare(
        baseline_rtl=os.path.join(cfg.rtl_root, "rtl"),
        pruned_rtl=os.path.join(ctx.rtl_output_dir, "rtl"),
        prune_config=ctx.prune_config,
    )


def run_synth_step(cfg: "ToolConfig", ctx: "PipelineContext", *, label: str, prune_config=None):
    """Run Yosys synthesis for a named step, storing result on ctx.

    Returns the SynthComparison result (or None on failure).
    """
    from arvis.cli import print_section, print_warning
    from arvis.synthesis.yosys_synth import YosysSynthesizer

    yosys = YosysSynthesizer()
    if not yosys.available:
        print_warning(f"Yosys not found — skipping {label} synthesis")
        return
    if not ctx.rtl_output_dir:
        print_warning(f"No RTL output — skipping {label} synthesis")
        return

    print_section(f"SYNTHESIS — {label.upper()}")

    cached_baseline = None
    if ctx.synth_comparison and ctx.synth_comparison.baseline.success:
        cached_baseline = ctx.synth_comparison.baseline

    pc = prune_config if prune_config is not None else ctx.prune_config

    result = yosys.compare(
        baseline_rtl=os.path.join(cfg.rtl_root, "rtl"),
        pruned_rtl=os.path.join(ctx.rtl_output_dir, "rtl"),
        prune_config=pc,
        baseline_stats=cached_baseline,
    )

    if label == "fused_pruned":
        ctx.synth_fused_only = result
    elif label == "hwloop_pruned":
        ctx.synth_hwloop_pruned = result
    elif label == "hwloop_pruned_best_perf":
        ctx.synth_hwloop_pruned_best_perf = result
    elif label == "all":
        ctx.synth_all = result
    elif label == "all_best_perf":
        ctx.synth_all_best_perf = result

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Verilator helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_sim_cfg(cfg: "ToolConfig"):
    """Create a lightweight config object for VerilatorRunner."""

    @dataclass
    class _SimCfg:
        rtl_root: str = cfg.rtl_root
        verilator_bin: str = "verilator"
        verilator_timeout_cycles: int = cfg.verilator_timeout_cycles
        sim_timeout_seconds: int = cfg.sim_timeout_seconds

    return _SimCfg()


def _find_original_hex(cfg: "ToolConfig", ctx=None) -> Optional[str]:
    """Find the original benchmark hex for baseline comparison."""
    from arvis.config import BENCHMARKS

    bm = BENCHMARKS.get(cfg.benchmark_name)
    if bm and bm.get("hex"):
        hex_name = bm["hex"]
        bench_dir = bm["dir"]

        saved = os.path.join(bench_dir, hex_name + ".baseline")
        if os.path.exists(saved):
            return saved

        candidate = os.path.join(bench_dir, hex_name)
        if os.path.exists(candidate):
            return candidate

    for fname in sorted(os.listdir(cfg.benchmark_dir)):
        if fname.endswith(".hex.baseline"):
            return os.path.join(cfg.benchmark_dir, fname)
    for fname in sorted(os.listdir(cfg.benchmark_dir)):
        if fname.endswith(".hex"):
            return os.path.join(cfg.benchmark_dir, fname)

    return None


def _find_fused_hex(cfg: "ToolConfig", ctx: "PipelineContext") -> Optional[str]:
    """Find the fused hex (from GCC compile or asm-patch)."""
    if ctx.fused_hex_path and os.path.exists(ctx.fused_hex_path):
        return ctx.fused_hex_path
    if ctx.firmware_patched and ctx.patched_hex_path and os.path.exists(ctx.patched_hex_path):
        return ctx.patched_hex_path
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Individual simulation runners
# ═══════════════════════════════════════════════════════════════════════════


def run_post_pruning_check(  # noqa: C901
    cfg: "ToolConfig",
    ctx: "PipelineContext",
    *,
    label: str = "pruned",
    use_original_hex: bool = False,
    explicit_hex: str | None = None,
) -> None:
    """Run Verilator simulation on current RTL output.

    Args:
        label: Result storage key and display label
        use_original_hex: If True, force original hex (not fused)
        explicit_hex: If set, use this hex path directly (overrides other logic)

    Results stored in ctx based on label:
      - "pruned"          → ctx.sim_pruned
      - "fused_orig_hex"  → ctx.sim_fused_orig_hex
      - "fused_pruned"    → ctx.sim_fused (reuse for final table)
      - other             → ctx.sim_pruned
    """
    from arvis.cli import print_error, print_info, print_step, print_success, print_warning
    from arvis.simulation.verilator_runner import VerilatorRunner

    if not ctx.rtl_output_dir:
        print_warning(f"No RTL output — skipping {label} check")
        return

    if not cfg.verilator_bin:
        print_warning(f"Verilator not found — skipping {label} check")
        return

    if explicit_hex and os.path.exists(explicit_hex):
        hex_path = explicit_hex
    elif use_original_hex:
        hex_path = _find_original_hex(cfg, ctx)
    else:
        hex_path = _find_fused_hex(cfg, ctx)
        if not hex_path:
            hex_path = _find_original_hex(cfg, ctx)
    if not hex_path:
        print_warning(f"No hex file found — skipping {label} check")
        return

    tag = label.upper()
    sim_dir = os.path.join(cfg.output_dir, f"sim_{label}")

    print_step(tag, "Building RTL...")
    print_info(f"RTL: {ctx.rtl_output_dir}/rtl")
    print_info(f"HEX: {hex_path}")

    # Only pass tb-level parameters (not internal ENABLE_* params)
    _TB_PARAMS = {"COREV_PULP", "FPU", "NUM_MHPMCOUNTERS", "HW_LOOP", "COREV_CLUSTER", "ZFINX"}
    all_flags = getattr(ctx, "verilator_extra_flags", [])
    extra_flags = [f for f in all_flags if any(f.startswith(f"-G{p}=") for p in _TB_PARAMS)]
    runner = VerilatorRunner(_make_sim_cfg(cfg))
    ok, sim_bin, build_out = runner.build_sim(
        rtl_dir=os.path.join(ctx.rtl_output_dir, "rtl"),
        output_dir=sim_dir,
        extra_flags=extra_flags,
    )

    if not ok:
        print_error(f"Verilator BUILD FAILED on {label} RTL!")
        for line in build_out.strip().splitlines()[-10:]:
            print(f"     {line}")
        return

    print_step(tag, "Running simulation...")
    result = runner.run_sim(sim_bin, os.path.abspath(hex_path))

    # Store result based on label
    if label == "pruned":
        ctx.sim_pruned = result
    elif label == "fused_pruned":
        ctx.sim_fused = result
    elif label == "fused":
        ctx.sim_fused = result
    elif label == "hwloop_pruned":
        ctx.sim_hwloop_pruned = result
    elif label == "hwloop_pruned_best_perf":
        ctx.sim_hwloop_pruned_best_perf = result
    elif label == "all":
        ctx.sim_all = result
    elif label == "all_best_perf":
        ctx.sim_all_best_perf = result
    elif label == "fused_orig_hex":
        pass
    else:
        pass  # don't overwrite ctx.sim_pruned for arbitrary labels

    if result.test_passed is True:
        print_success(f"{tag} core PASSED ({result.total_cycles:,} cycles)")
    elif result.test_passed is False:
        print_error(f"{tag} core FAILED ({result.total_cycles:,} cycles)")
    else:
        cycles_info = f" ({result.total_cycles:,} cycles)" if result.total_cycles else ""
        print_warning(f"{tag} result inconclusive{cycles_info}")
        # Print last lines of sim output to help diagnose (timeout vs crash)
        sim_out = (result.stdout + result.stderr).strip()
        if sim_out:
            for line in sim_out.splitlines()[-5:]:
                print(f"    {line}")

    return result


def run_baseline_sim(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Run Verilator simulation on original RTL with original hex.

    This is the reference point for the final summary table.
    """
    from arvis.cli import print_error, print_info, print_step, print_success, print_warning
    from arvis.simulation.verilator_runner import VerilatorRunner

    if not cfg.verilator_bin:
        print_warning("Verilator not found — skipping baseline sim")
        return

    if not os.path.exists(os.path.join(cfg.rtl_root, "example_tb")):
        print_warning("No example_tb — skipping baseline sim")
        return

    hex_path = _find_original_hex(cfg, ctx)
    if not hex_path:
        print_warning("No original hex — skipping baseline sim")
        return

    sim_dir = os.path.join(cfg.output_dir, "sim_baseline")

    print_step("BASELINE", "Building original RTL...")
    print_info(f"RTL: {cfg.rtl_root}/rtl")
    print_info(f"HEX: {hex_path}")

    runner = VerilatorRunner(_make_sim_cfg(cfg))
    ok, sim_bin, build_out = runner.build_sim(
        rtl_dir=os.path.join(cfg.rtl_root, "rtl"),
        output_dir=sim_dir,
    )

    if not ok:
        print_error("Verilator BUILD FAILED on baseline RTL!")
        for line in build_out.strip().splitlines()[-10:]:
            print(f"     {line}")
        return

    print_step("BASELINE", "Running simulation...")
    result = runner.run_sim(sim_bin, os.path.abspath(hex_path))
    ctx.sim_baseline = result

    if result.test_passed is True:
        print_success(f"BASELINE PASSED ({result.total_cycles:,} cycles)")
    elif result.test_passed is False:
        print_error(f"BASELINE FAILED ({result.total_cycles:,} cycles)")
    else:
        print_warning(f"BASELINE inconclusive ({result.total_cycles:,} cycles)")


# ═══════════════════════════════════════════════════════════════════════════
# Final summary table
# ═══════════════════════════════════════════════════════════════════════════


def print_final_summary(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Print final results table. Delegated to report.summary."""
    from arvis.report.summary import print_final_summary as _impl

    _impl(cfg, ctx)
