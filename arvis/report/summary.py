"""
Final results summary table — ARVIS terminal output.

Columns: Baseline | Pruned | Fused+Pruned | HWLoop+Pruned | All
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


def print_final_summary(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    from arvis.cli import Colors, print_section

    C = Colors
    print_section(f"FINAL RESULTS — {cfg.benchmark_name.upper()}")

    # Column definitions: (short_name, sim, synth_result)
    def _synth(s):
        return s.pruned if s else None

    # Pick best HWLoop+Pruned (best_adp vs best_perf)
    hwloop_pruned_sim = ctx.sim_hwloop_pruned
    hwloop_pruned_synth = _synth(ctx.synth_hwloop_pruned)
    if ctx.sim_hwloop_pruned_best_perf is not None:
        adp_cycles = ctx.sim_hwloop_pruned.total_cycles if ctx.sim_hwloop_pruned else float("inf")
        perf_cycles = ctx.sim_hwloop_pruned_best_perf.total_cycles if ctx.sim_hwloop_pruned_best_perf else float("inf")
        if perf_cycles < adp_cycles:
            hwloop_pruned_sim = ctx.sim_hwloop_pruned_best_perf
            hwloop_pruned_synth = _synth(ctx.synth_hwloop_pruned_best_perf)

    # Pick best All (best_adp vs best_perf)
    all_sim = ctx.sim_all
    all_synth = _synth(ctx.synth_all)
    all_perf_sim = ctx.sim_all_best_perf
    all_perf_synth = _synth(ctx.synth_all_best_perf)

    columns = [
        (
            "Baseline",
            ctx.sim_baseline,
            ctx.synth_comparison.baseline if ctx.synth_comparison else None,
        ),
        ("Pruned", ctx.sim_pruned, _synth(ctx.synth_comparison)),
        ("Fused+Pruned", ctx.sim_fused, _synth(ctx.synth_fused_only)),
        ("HWLoop+Pruned", hwloop_pruned_sim, hwloop_pruned_synth),
        ("All", all_sim, all_synth),
    ]
    # Show All(perf) if it exists and is different
    if all_perf_sim is not None:
        adp_c = all_sim.total_cycles if all_sim else float("inf")
        perf_c = all_perf_sim.total_cycles if all_perf_sim else float("inf")
        if perf_c != adp_c:
            columns.append(("All(perf)", all_perf_sim, all_perf_synth))
    # Only show columns with data
    cols = [(n, s, sy) for n, s, sy in columns if s is not None]
    if not cols:
        cols = columns[:3]

    names = [c[0] for c in cols]
    sims = [c[1] for c in cols]
    synths = [c[2] for c in cols]
    n = len(cols)

    W_L, W_C = 22, 14

    def _hdr():
        h = f"  {C.BOLD}{'Metric':<{W_L}}{C.END}"
        for nm in names:
            h += f" {C.BOLD}{nm[:W_C]:>{W_C}}{C.END}"
        return h

    def _sep():
        return f"  {C.DIM}{'─' * W_L}{(' ' + '─' * W_C) * n}{C.END}"

    def _row(label, vals, best_idx=-1):
        r = f"  {C.CYAN}{label:<{W_L}}{C.END}"
        for i, v in enumerate(vals):
            if i == best_idx:
                fmt = f"{C.GREEN}{C.BOLD}{v}{C.END}"
                pad = W_C + len(C.GREEN) + len(C.BOLD) + len(C.END)
            else:
                fmt = v
                pad = W_C
            r += f" {fmt:>{pad}}"
        return r

    print(_hdr())
    print(_sep())

    # Status
    def _st(s):
        if s is None:
            return f"{C.DIM}—{C.END}"
        if s.test_passed is True:
            return f"{C.GREEN}PASS{C.END}"
        if s.test_passed is False:
            return f"{C.RED}FAIL{C.END}"
        return f"{C.YELLOW}?{C.END}"

    line = f"  {C.CYAN}{'Verilator result':<{W_L}}{C.END}"
    for s in sims:
        line += f" {_st(s):>{W_C + len(C.GREEN) + len(C.END)}}"
    print(line)

    # Cycles
    b_cyc = ctx.sim_baseline.total_cycles if ctx.sim_baseline and ctx.sim_baseline.total_cycles > 0 else 0

    def _cyc(s):
        return f"{s.total_cycles:,}" if s and s.total_cycles > 0 else "—"

    cv = [s.total_cycles if s and s.total_cycles > 0 else float("inf") for s in sims]
    best = cv.index(min(cv)) if min(cv) < float("inf") else -1
    print(_row("Cycles", [_cyc(s) for s in sims], best_idx=best))

    def _spd(s):
        if not s or s.total_cycles == 0 or b_cyc == 0:
            return "—"
        return f"{b_cyc / s.total_cycles:.3f}x"

    def _red(s):
        if not s or s.total_cycles == 0 or b_cyc == 0:
            return "—"
        return f"{(1 - s.total_cycles / b_cyc) * 100:+.1f}%"

    print(_row("Speedup", [_spd(s) for s in sims]))
    print(_row("Cycle reduction", [_red(s) for s in sims]))

    # Synthesis
    def _cells(sy):
        return sy.cells if sy and sy.success else None

    col_cells = [_cells(sy) for sy in synths]
    base_cells = col_cells[0] if col_cells else None

    if base_cells:
        print(_sep())

        def _c(c):
            return f"{c:,}" if c else "—"

        def _cv(c):
            if not c or not base_cells:
                return "—"
            return f"{(c - base_cells) / base_cells * 100:+.1f}%"

        ccv = [c if c else float("inf") for c in col_cells]
        bc = ccv.index(min(ccv)) if min(ccv) < float("inf") else -1
        print(_row("Yosys cells", [_c(c) for c in col_cells], best_idx=bc))
        print(_row("Cell variation", [_cv(c) for c in col_cells]))

        # ADP
        def _adp(s, c):
            if not s or s.total_cycles == 0 or not c:
                return "—"
            return f"{s.total_cycles * c / 1e9:.2f}"

        def _adpv(s, c):
            if not s or s.total_cycles == 0 or not c:
                return float("inf")
            return s.total_cycles * c

        av = [_adpv(s, c) for s, c in zip(sims, col_cells)]
        ba = av.index(min(av)) if min(av) < float("inf") else -1
        print(_sep())
        print(_row("ADP (×10⁹)", [_adp(s, c) for s, c in zip(sims, col_cells)], best_idx=ba))

    print()

    # Verdict
    all_pass = all(s.test_passed is True for s in sims if s is not None)
    if all_pass and any(s is not None for s in sims):
        best_sim, best_name = None, ""
        for nm, s, _ in reversed(cols):
            if s and s.test_passed and s.total_cycles > 0:
                if best_sim is None or s.total_cycles < best_sim.total_cycles:
                    best_sim, best_name = s, nm
        if best_sim and b_cyc > 0:
            spd = b_cyc / best_sim.total_cycles
            sav = (1 - best_sim.total_cycles / b_cyc) * 100
            print(f"  {C.GREEN}{C.BOLD}✅ VERIFICATION PASSED: {cfg.benchmark_name}{C.END}")
            print(f"  {C.CYAN}   Best: {best_name} — {spd:.3f}x{C.END} ({C.BOLD}{sav:.1f}%{C.END} reduction)")
        else:
            print(f"  {C.GREEN}{C.BOLD}✅ VERIFICATION PASSED: {cfg.benchmark_name}{C.END}")
    else:
        fails = [nm for nm, s, _ in cols if s and s.test_passed is False]
        if fails:
            print(f"  {C.RED}{C.BOLD}❌ VERIFICATION FAILED: {', '.join(fails)}{C.END}")
        else:
            print(f"  {C.YELLOW}{C.BOLD}⚠️  VERIFICATION INCOMPLETE{C.END}")
