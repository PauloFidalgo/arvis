#!/usr/bin/env python3
"""
CV32E40P Workload Specialization Tool.

Thin CLI entry point that orchestrates the pipeline phases:
  1. Profiling    — Disassemble, CFG, loops, trace, memory, ALU usage
  2. Selection    — Fusion/accelerator/SPM candidate selection (ILP)
  3. Pruning      — Compute PruneConfig (no file writes)
  4. Fusion RTL   — Generate GCC patterns, compile, filter to used ops
  5. RTL Apply    — RTLChangeSet: atomic copy + prune + fuse
  6. Verification — Yosys synthesis + Verilator simulation
  7. Reporting    — HTML, text, CSV reports

Phase selection via ``--phases`` / ``--until``:
  --phases pruning            Run only up to pruning (analysis → selection → pruning → verify)
  --phases fusion             Run up to fusion (includes pruning + fusion RTL + GCC)
  --phases all                Run everything (default)

Hardware resource flags:
  --keep-rf-read-c            Keep 3rd register file read port (needed for R4 fused insns)
  --prune-rf-read-c           Prune 3rd read port (safe when no fusion)
  --keep-rf-write-b           Keep 2nd register file write port (needed for dual-write)
  --prune-rf-write-b          Prune 2nd write port (safe when no dual-ALU)

When not specified, defaults are inferred from the phase selection:
  - pruning-only:  prune both (no R4 instructions, no dual-write)
  - fusion:        keep read-c (R4 encoding needs rs3), prune write-b
  - all:           keep both (future accelerators may need dual-write)
"""

import argparse
import sys

from arvis.config import BENCHMARKS, ToolConfig
from toolchain import check_prerequisites

# ── Phase definitions ──
# Ordered list of phase names for --until selection
PHASE_ORDER = [
    "analysis",  # Phase 1+2: profiling + selection
    "pruning",  # Phase 3: RTL pruning + verification
    "fusion",  # Phase 5+6: fusion RTL + GCC compile
    "verification",  # Phase 7: post-fusion verification
]

PHASE_ALIASES = {
    "all": PHASE_ORDER,
    "analyze": ["analysis"],
    "prune": ["analysis", "pruning"],
    "fuse": ["analysis", "pruning", "fusion"],
    "hwloop": ["analysis", "pruning"],
}


def _resolve_phases(phases_arg: str) -> set:
    """Resolve a --phases argument into a set of phase names.

    Supports:
      - 'all' → all phases
      - 'pruning' → analysis + pruning (--until style)
      - 'analysis,pruning,fusion' → explicit list
      - Aliases: 'prune' → analysis+pruning, 'fuse' → through fusion
    """
    phases_arg = phases_arg.strip().lower()

    # Check aliases first
    if phases_arg in PHASE_ALIASES:
        return set(PHASE_ALIASES[phases_arg])

    # Check if it's a single phase name (--until style)
    if phases_arg in PHASE_ORDER:
        idx = PHASE_ORDER.index(phases_arg)
        return set(PHASE_ORDER[: idx + 1])

    # Comma-separated list
    requested = {p.strip() for p in phases_arg.split(",")}
    unknown = requested - set(PHASE_ORDER) - set(PHASE_ALIASES)
    if unknown:
        print(f"  ❌ Unknown phases: {unknown}")
        print(f"     Available: {', '.join(PHASE_ORDER)}")
        print(f"     Aliases:   {', '.join(PHASE_ALIASES)}")
        sys.exit(1)

    # Expand any aliases in the list
    expanded = set()
    for p in requested:
        if p in PHASE_ALIASES:
            expanded.update(PHASE_ALIASES[p])
        else:
            expanded.add(p)

    # Ensure dependencies: pruning needs analysis, fusion needs pruning, etc.
    if "pruning" in expanded:
        expanded.add("analysis")
    if "fusion" in expanded:
        expanded.add("analysis")
        expanded.add("pruning")
    if "verification" in expanded and "fusion" in expanded:
        pass  # verification after fusion is explicit
    return expanded


def parse_args() -> ToolConfig:
    """Parse CLI arguments and return a resolved ToolConfig."""
    parser = argparse.ArgumentParser(
        description="CV32E40P Workload Specializer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phase selection examples:
  %(prog)s --phases pruning        Analysis + pruning + verification
  %(prog)s --phases fusion         Analysis + pruning + fusion + GCC
  %(prog)s --phases all            Full pipeline (default)
  %(prog)s --until pruning         Same as --phases pruning

Hardware resource examples:
  %(prog)s --prune-rf-read-c       Remove 3rd read port (saves area)
  %(prog)s --keep-rf-read-c        Keep 3rd read port (needed for R4)
  %(prog)s --prune-rf-write-b      Remove 2nd write port (saves area)
  %(prog)s --keep-rf-write-b       Keep 2nd write port (dual-ALU)

Combined:
  %(prog)s -b kyber --phases pruning --prune-rf-read-c --prune-rf-write-b
""",
    )

    # ── Benchmark selection ──
    parser.add_argument(
        "--benchmark",
        "-b",
        choices=list(BENCHMARKS.keys()),
        default=None,
        help="Select benchmark (kyber, conv2d, minimal, stress, divheavy, mulheavy)",
    )

    # ── Phase selection ──
    phase_group = parser.add_mutually_exclusive_group()
    phase_group.add_argument(
        "--phases",
        default="all",
        help=(
            "Pipeline phases to run. "
            "Options: all, analysis, pruning, fusion, verification, reporting. "
            "Comma-separated or single name (implies --until). "
            "Aliases: prune, fuse, analyze. Default: all"
        ),
    )
    phase_group.add_argument(
        "--until",
        choices=PHASE_ORDER,
        default=None,
        help="Run pipeline up to and including this phase",
    )

    # ── Hardware resource parameters ──
    rf_read_group = parser.add_mutually_exclusive_group()
    rf_read_group.add_argument(
        "--keep-rf-read-c",
        action="store_true",
        default=None,
        help="Keep 3rd RF read port (operand_c/rs3). Needed for R4 fused insns.",
    )
    rf_read_group.add_argument(
        "--prune-rf-read-c",
        action="store_true",
        default=None,
        help="Prune 3rd register file read port. Safe when no fusion/R4 instructions are used.",
    )

    rf_write_group = parser.add_mutually_exclusive_group()
    rf_write_group.add_argument(
        "--keep-rf-write-b",
        action="store_true",
        default=None,
        help="Keep 2nd register file write port. Needed for dual-write ALU extensions.",
    )
    rf_write_group.add_argument(
        "--prune-rf-write-b",
        action="store_true",
        default=None,
        help="Prune 2nd register file write port. Safe when no dual-write extensions are used.",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Keep RISC-V debug infrastructure (JTAG, breakpoints, single-step). Default: pruned.",
    )
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        default=False,
        help="(Deprecated, use --ga) Alias for --ga.",
    )
    parser.add_argument(
        "--ga",
        action="store_true",
        default=False,
        help="GA-based HW loop optimization: genetic algorithm + greedy refinement on the 'All' solution.",
    )

    # ── Phase 2.8: portability gateway ──
    parser.add_argument(
        "--use-portability",
        action="store_true",
        default=False,
        help=(
            "EXPERIMENTAL: route RTL emission through the portable "
            "Pipeline path (targets/cv32e40p/portability_shim) "
            "instead of the legacy RTLChangeSet.apply.  Verified "
            "byte-equivalent on the 5 standard variants for the "
            "ud benchmark; production adoption blocked on full "
            "21-benchmark sweep (Phase 2.8e)."
        ),
    )

    args = parser.parse_args()

    # ── Build config ──
    if args.benchmark:
        cfg = ToolConfig.from_benchmark(args.benchmark)
    else:
        cfg = ToolConfig()

    # ── Resolve phases ──
    if args.until:
        cfg.enabled_phases = _resolve_phases(args.until)
    else:
        cfg.enabled_phases = _resolve_phases(args.phases)

    # ── Resolve HW resource parameters ──
    # Smart defaults based on phase selection
    has_fusion = "fusion" in cfg.enabled_phases

    if args.keep_rf_read_c:
        cfg.prune_rf_read_c = False
    elif args.prune_rf_read_c:
        cfg.prune_rf_read_c = True
    else:
        # Auto: prune read-c only when NOT doing fusion
        cfg.prune_rf_read_c = not has_fusion

    if args.keep_rf_write_b:
        cfg.prune_rf_write_b = False
    elif args.prune_rf_write_b:
        cfg.prune_rf_write_b = True
    else:
        # Auto: prune write-b unless future dual-ALU phases are enabled
        cfg.prune_rf_write_b = True

    # Debug infrastructure
    cfg.enable_debug = args.debug
    cfg.exhaustive_hwloop = args.exhaustive or args.ga

    # Phase 2.8 portability gateway: propagate the flag onto cfg
    # AND set the env var so library code (rtl_changeset) can pick
    # it up without a chain of cfg-passing changes.
    cfg.use_portability = bool(args.use_portability)
    if cfg.use_portability:
        import os as _os

        _os.environ["ARVIS_USE_PORTABILITY"] = "1"

    return cfg


def main():
    from arvis.cli import print_banner, print_metric

    print_banner()

    # ── CLI + Config ──
    cfg = parse_args()
    print_metric("Benchmark", f"{cfg.benchmark_name}")
    print_metric("Source", cfg.benchmark_dir)
    print_metric("Output", cfg.output_dir)
    print_metric("Phases", ", ".join(sorted(cfg.enabled_phases)))
    print_metric("RF read port C", "PRUNE" if cfg.prune_rf_read_c else "KEEP")
    print_metric("RF write port B", "PRUNE" if cfg.prune_rf_write_b else "KEEP")
    print()

    # ── Prerequisites (toolchain, build, trace) ──
    use_trace = check_prerequisites(cfg)

    # ── Pipeline context ──
    from arvis.pipeline.context import PipelineContext

    ctx = PipelineContext(use_trace=use_trace)

    # ── Run pipeline ──
    if cfg.is_suite:
        from arvis.pipeline.multi_runner import run_suite_pipeline
        run_suite_pipeline(cfg, ctx)
    else:
        from arvis.pipeline.runner import run_pipeline
        run_pipeline(cfg, ctx)


if __name__ == "__main__":
    main()
