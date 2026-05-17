"""Section builders for the ARVIS HTML report."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


def _h(t) -> str:
    return html.escape(str(t))


def _fmt(n: int) -> str:
    return f"{n:,}"


def _pct(p: float, t: float) -> str:
    return f"{p / t * 100:.1f}%" if t else "—"


def _section(icon, title, body, collapsed=False, id_attr=""):
    c = " collapsed" if collapsed else ""
    h = " hidden" if collapsed else ""
    ida = f' id="{id_attr}"' if id_attr else ""
    return (
        f'<div class="section"{ida}><div class="section-header{c}">'
        f'<h2><span class="icon">{icon}</span> {_h(title)}</h2>'
        f'<span class="chevron">▼</span></div>'
        f'<div class="section-body{h}">{body}</div></div>'
    )


def _hero(v, label, cls="accent", count=None, suffix="", decimals=0):
    cd = ""
    if count is not None:
        cd = f' data-count="{count}" data-suffix="{suffix}" data-decimals="{decimals}"'
    return f'<div class="hero-card {cls}"><div class="value"{cd}>{v}</div><div class="label">{_h(label)}</div></div>'


def _bar(label, value, mx, text, color="var(--accent)"):
    w = min(value / mx * 100, 100) if mx > 0 else 0
    return (
        f'<div class="bar-row"><div class="bar-label">{_h(label)}</div>'
        f'<div class="bar-track"><div class="bar-fill" data-w="{w:.1f}%" style="width:0;background:{color}"></div>'
        f'<div class="bar-value">{text}</div></div></div>'
    )


def _badge(t, c="accent"):
    return f'<span class="badge badge-{c}">{_h(t)}</span>'


def _donut(segments, center_big, center_small):
    """CSS conic-gradient donut chart."""
    stops, legend, offset = [], [], 0
    for label, val, pct, color in segments:
        end = offset + pct * 3.6
        stops.append(f"{color} {offset:.1f}deg {end:.1f}deg")
        legend.append(
            f'<div class="legend-item"><div class="legend-dot" style="background:{color}"></div>{_h(label)}: {_fmt(val)} ({pct:.1f}%)</div>'
        )
        offset = end
    grad = f"conic-gradient({', '.join(stops)})"
    return (
        f'<div class="donut-wrap"><div class="donut" style="background:{grad}">'
        f'<div style="position:absolute;inset:25%;border-radius:50%;background:var(--surface)"></div>'
        f'<div class="donut-center"><div class="big">{center_big}</div><div class="small">{center_small}</div></div>'
        f'</div><div class="donut-legend">{"".join(legend)}</div></div>'
    )


def build_hero(cfg: "ToolConfig", ctx: "PipelineContext") -> str:
    cards = []
    b = ctx.sim_baseline.total_cycles if ctx.sim_baseline and ctx.sim_baseline.total_cycles > 0 else 0
    sims = [
        ("Pruned", ctx.sim_pruned),
        ("Fused+Pruned", ctx.sim_fused),
        ("HWLoop+Pruned", ctx.sim_hwloop_pruned),
        ("All", ctx.sim_all),
        ("All(perf)", ctx.sim_all_best_perf),
    ]
    bc, bn = b, "Baseline"
    for n, s in sims:
        if s and s.test_passed and 0 < s.total_cycles < bc:
            bc, bn = s.total_cycles, n
    if b > 0 and bc < b:
        spd = b / bc
        red = (1 - bc / b) * 100
        cards.append(_hero(f"{spd:.2f}×", f"Speedup ({bn})", "green", spd, "×", 2))
        cards.append(_hero(f"{red:.1f}%", "Cycle Reduction", "green", red, "%", 1))
    elif b > 0:
        cards.append(_hero("1.00×", "Speedup", "accent"))
    if b > 0:
        cards.append(_hero(_fmt(b), "Baseline Cycles", "accent", b))
    sy = ctx.synth_comparison
    if sy and sy.baseline.success and sy.pruned.success:
        cards.append(_hero(f"{sy.cell_savings_pct:.1f}%", "Cell Savings", "purple", sy.cell_savings_pct, "%", 1))
    gcc = ctx.gcc_compile_result
    if gcc and gcc.total_fused_count > 0:
        cards.append(_hero(str(gcc.unique_patterns), "Custom Instructions", "cyan", gcc.unique_patterns))
    if ctx.hwloop_candidates:
        v = [c for c in ctx.hwloop_candidates if c.fused_hex or c.plain_hex]
        if v:
            best = max(v, key=lambda c: c.loops_patched)
            cards.append(_hero(str(best.loops_patched), "HW Loops Patched", "yellow", best.loops_patched))
    cards.append(_hero(str(len(cfg.enabled_phases)), "Pipeline Phases", "accent", len(cfg.enabled_phases)))
    return '<div class="hero">' + "".join(cards) + "</div>"


def build_pipeline_flow(ctx: "PipelineContext") -> str:
    steps = [
        ("1", "Baseline", ctx.sim_baseline),
        ("2", "Pruned", ctx.sim_pruned),
        ("3", "Fused", ctx.sim_fused),
        ("4", "HWLoop", ctx.sim_hwloop_pruned),
        ("5", "All", ctx.sim_all),
    ]
    parts = []
    for i, (num, label, sim) in enumerate(steps):
        if i > 0:
            ac = " active" if sim and sim.test_passed else ""
            parts.append(f'<div class="pipe-arrow{ac}"></div>')
        if sim is None:
            cls = ""
        elif sim.test_passed:
            cls = " pass"
        else:
            cls = " fail"
        icon = "✓" if sim and sim.test_passed else ("✗" if sim and sim.test_passed is False else num)
        parts.append(
            f'<div class="pipe-step"><div class="pipe-dot{cls}">{icon}</div><div class="pipe-label">{label}</div></div>'
        )
    return '<div class="pipeline-flow">' + "".join(parts) + "</div>"


def build_verification(cfg, ctx):
    def _sy(s):
        return s.pruned if s else None

    columns = [
        (
            "Baseline",
            ctx.sim_baseline,
            ctx.synth_comparison.baseline if ctx.synth_comparison else None,
        ),
        ("Pruned", ctx.sim_pruned, _sy(ctx.synth_comparison)),
        ("Fused+Pruned", ctx.sim_fused, _sy(ctx.synth_fused_only)),
        ("HWLoop+Pruned", ctx.sim_hwloop_pruned, _sy(ctx.synth_hwloop_pruned)),
        ("All", ctx.sim_all, _sy(ctx.synth_all)),
    ]
    if ctx.sim_all_best_perf is not None:
        columns.append(("All(perf)", ctx.sim_all_best_perf, _sy(ctx.synth_all_best_perf)))
    cols = [(n, s, sy) for n, s, sy in columns if s is not None]
    if not cols:
        return "<p>No verification data.</p>"
    b = ctx.sim_baseline.total_cycles if ctx.sim_baseline and ctx.sim_baseline.total_cycles > 0 else 0
    hdr = "<tr><th>Metric</th>" + "".join(f"<th class='right'>{_h(n)}</th>" for n, _, _ in cols) + "</tr>"
    rows = []

    # Status
    def st(s):
        if s is None:
            return "—"
        if s.test_passed is True:
            return '<span class="pass">✓ PASS</span>'
        if s.test_passed is False:
            return '<span class="fail">✗ FAIL</span>'
        return "?"

    rows.append("<tr><td>Status</td>" + "".join(f"<td class='right'>{st(s)}</td>" for _, s, _ in cols) + "</tr>")
    # Cycles
    cv = [s.total_cycles if s and s.total_cycles > 0 else 0 for _, s, _ in cols]
    bi = cv.index(min(v for v in cv if v > 0)) if any(v > 0 for v in cv) else -1
    r = "<tr><td>Cycles</td>"
    for i, (_, s, _) in enumerate(cols):
        v = _fmt(s.total_cycles) if s and s.total_cycles > 0 else "—"
        c = ' class="right best"' if i == bi else ' class="right mono"'
        r += f"<td{c}>{v}</td>"
    rows.append(r + "</tr>")
    # Speedup, reduction
    for label, fn in [
        (
            "Speedup",
            lambda s: f"{b / s.total_cycles:.3f}×" if s and s.total_cycles > 0 and b > 0 else "—",
        ),
        (
            "Cycle Reduction",
            lambda s: f"{(1 - s.total_cycles / b) * 100:+.1f}%" if s and s.total_cycles > 0 and b > 0 else "—",
        ),
    ]:
        rows.append(
            f"<tr><td>{label}</td>" + "".join(f'<td class="right mono">{fn(s)}</td>' for _, s, _ in cols) + "</tr>"
        )
    # Cells
    ce = [sy.cells if sy and sy.success else 0 for _, _, sy in cols]
    if any(ce):
        bci = ce.index(min(v for v in ce if v > 0)) if any(v > 0 for v in ce) else -1
        r = "<tr><td>Yosys Cells</td>"
        for i, (_, _, sy) in enumerate(cols):
            v = _fmt(sy.cells) if sy and sy.success else "—"
            c = ' class="right best"' if i == bci else ' class="right mono"'
            r += f"<td{c}>{v}</td>"
        rows.append(r + "</tr>")
        bc0 = ce[0] if ce[0] > 0 else 0
        if bc0:
            rows.append(
                "<tr><td>Cell Δ</td>"
                + "".join(
                    f'<td class="right mono">{(sy.cells - bc0) / bc0 * 100:+.1f}%</td>'
                    if sy and sy.success
                    else '<td class="right">—</td>'
                    for _, _, sy in cols
                )
                + "</tr>"
            )
    # ADP
    r = "<tr><td>ADP (×10⁹)</td>"
    for _, s, sy in cols:
        if s and s.total_cycles > 0 and sy and sy.success and sy.cells > 0:
            r += f'<td class="right mono">{s.total_cycles * sy.cells / 1e9:.2f}</td>'
        else:
            r += '<td class="right">—</td>'
    rows.append(r + "</tr>")
    return f"<table>{hdr}{''.join(rows)}</table>"


def build_bottleneck(ctx):
    bn = getattr(ctx, "bottleneck", None)
    if not bn:
        return ""
    tc = bn.total_cycles or 1
    items = [
        ("Base (1/insn)", bn.base_cycles, "var(--accent)"),
        ("Fetch stalls", bn.fetch_stalls, "var(--yellow)"),
        ("Branch penalties", bn.branch_penalty, "var(--purple)"),
        ("Load-use stalls", bn.load_use_stalls, "var(--red)"),
        ("Multi-cycle ops", bn.multicycle_extra, "var(--cyan)"),
    ]
    # Donut
    [(l, v, v / tc * 100, c.replace("var(", "").replace(")", "")) for l, v, c in items if v > 0]
    color_map = {
        "--accent": "#58a6ff",
        "--yellow": "#d29922",
        "--purple": "#bc8cff",
        "--red": "#f85149",
        "--cyan": "#39d2c0",
    }
    segs2 = [
        (l, v, v / tc * 100, color_map.get(c.replace("var(", "").replace(")", ""), c)) for l, v, c in items if v > 0
    ]
    donut = _donut(segs2, f"{bn.cpi:.2f}", "CPI")
    bars = "".join(_bar(l, v, tc, f"{_fmt(v)} ({_pct(v, tc)})", c) for l, v, c in items)
    info = f'<p style="margin-bottom:16px">Total cycles: <strong>{_fmt(bn.total_cycles)}</strong> &nbsp;|&nbsp; Instructions: <strong>{_fmt(bn.total_instructions)}</strong></p>'
    recs = ""
    if bn.recommendations:
        recs = '<div style="margin-top:20px"><h3 style="font-size:0.95rem;margin-bottom:10px">Recommendations</h3>'
        for i, (_, _, d) in enumerate(bn.recommendations):
            recs += (
                f'<div class="rec-item"><div class="rec-rank">{i + 1}</div><div class="rec-text">{_h(d)}</div></div>'
            )
        recs += "</div>"
    return _section("📊", "Bottleneck Analysis", info + donut + bars + recs, id_attr="bottleneck")


def build_profiling(ctx):
    p = []
    p.append(
        f"<p>Basic blocks: <strong>{len(ctx.blocks)}</strong> &nbsp;|&nbsp; Loops: <strong>{len(ctx.loops)}</strong></p>"
    )
    if ctx.profile:
        p.append(f"<p>Dynamic instructions: <strong>{_fmt(ctx.profile.total_instructions)}</strong></p>")
    if ctx.alu_usage:
        p.append(
            f"<p>ALU ops used: <strong>{len(ctx.alu_usage.used_ops)}</strong> &nbsp;|&nbsp; Removable: <strong>{len(ctx.alu_usage.removable_ops)}</strong></p>"
        )
    if ctx.mem_analysis and ctx.mem_analysis.total_accesses > 0:
        ma = ctx.mem_analysis
        p.append(
            f"<p>Memory accesses: <strong>{_fmt(ma.total_accesses)}</strong> &nbsp;|&nbsp; Locality: <strong>{ma.locality_score:.2f}</strong></p>"
        )
    if ctx.loops:
        top = ctx.loops[:10]
        p.append('<h3 style="font-size:0.92rem;margin:18px 0 10px">Hottest Loops</h3>')
        p.append(
            '<table><tr><th>#</th><th>Header</th><th class="right">Blocks</th><th class="right">Hotness</th><th class="right">Trip Count</th></tr>'
        )
        for i, lp in enumerate(top):
            h = f"{lp.hotness_score * 100:.2f}%"
            p.append(
                f'<tr><td>{i + 1}</td><td class="mono">0x{lp.header_block_id:x}</td><td class="right">{len(lp.body_block_ids)}</td><td class="right mono">{h}</td><td class="right mono">{lp.trip_count_estimate:,}</td></tr>'
            )
        p.append("</table>")
    return _section("🔍", "Workload Profiling", "".join(p), id_attr="profiling")


def build_fusion(ctx):
    p = []
    if not ctx.all_fusions and not (ctx.gcc_compile_result and ctx.gcc_compile_result.used_instructions):
        return ""
    if ctx.all_fusions:
        p.append(f"<p>Candidates discovered: <strong>{len(ctx.all_fusions)}</strong></p>")
        top = sorted(ctx.all_fusions, key=lambda c: c.frequency, reverse=True)[:15]
        if top:
            p.append('<h3 style="font-size:0.92rem;margin:18px 0 10px">Top Patterns</h3>')
            mx = top[0].frequency
            for c in top:
                pat = " → ".join(c.pattern)
                w = c.frequency / mx * 100 if mx else 0
                hc = f" {_badge('HC', 'yellow')}" if getattr(c, "has_hardcoded_imm", False) else ""
                p.append(
                    f'<div class="pattern-item"><div style="display:flex;justify-content:space-between;align-items:center"><div class="pattern-ops">{_h(pat)}{hc}</div><div class="mono" style="color:var(--text2)">{_fmt(c.frequency)}</div></div>'
                    f'<div style="margin-top:6px;height:4px;background:var(--surface);border-radius:2px;overflow:hidden"><div style="height:100%;width:{w:.0f}%;background:var(--cyan);border-radius:2px"></div></div>'
                    f'<div class="pattern-meta"><span>{len(c.pattern)}-gram</span><span>{c.liveness.min_read_ports}R / {c.liveness.min_write_ports_eliminate}W</span></div></div>'
                )
    gcc = ctx.gcc_compile_result
    if gcc and gcc.used_instructions:
        p.append('<h3 style="font-size:0.92rem;margin:18px 0 10px">In Binary</h3>')
        p.append(
            f"<p>Uses: <strong>{_fmt(gcc.total_fused_count)}</strong> &nbsp;|&nbsp; Unique: <strong>{gcc.unique_patterns}</strong></p>"
        )
        p.append('<table><tr><th>Pattern</th><th>Mnemonics</th><th class="right">Count</th><th>Enc</th></tr>')
        for ui in sorted(gcc.used_instructions, key=lambda u: u.count, reverse=True):
            pn = _h(ui.pattern_name) if ui.pattern_name else f"0x{ui.opcode:02x}/{ui.funct3}/{ui.funct7}"
            mn = " → ".join(ui.mnemonics) if ui.mnemonics else "—"
            tp = _badge("R4", "purple") if ui.is_r4 else _badge("R", "accent")
            p.append(
                f'<tr><td class="mono">{pn}</td><td class="mono">{_h(mn)}</td><td class="right mono">{_fmt(ui.count)}</td><td>{tp}</td></tr>'
            )
        p.append("</table>")
    return _section("⚡", "Instruction Fusion", "".join(p), id_attr="fusion")


def build_pruning(ctx):
    pc = ctx.prune_config
    if not pc:
        return ""
    p = []
    decs = [
        ("Divider", pc.enable_div, "div/rem", "no div/rem"),
        ("Multiplier", pc.enable_mul, "mul", "no mul"),
        ("MULH FSM", pc.enable_mul_h, "mulh", "no mulh"),
        ("Sleep", pc.enable_sleep, "wfi", "no wfi"),
        ("Debug", pc.enable_debug, "ebreak", "no ebreak"),
        ("HPM", pc.enable_hpm, "CSR", "no CSR"),
        ("Dot product", pc.enable_dot_mul, "SIMD", "PULP=0"),
        ("MSU", pc.enable_msu, "MAC", "PULP=0"),
        ("Popcnt", pc.enable_popcnt, "PULP", "PULP=0"),
        ("FF1", pc.enable_ff1, "PULP", "PULP=0"),
        ("RF write B", pc.enable_regfile_wr_b, "dual-write", "unused"),
        ("RF read C", pc.enable_regfile_rd_c, "R4/FPU", "unused"),
        ("Compressed", pc.enable_compressed, "C.*", "no C.*"),
        ("Interrupts", pc.enable_interrupts, "ISR", "no ISR"),
    ]
    p.append('<div class="decision-grid">')
    for nm, en, kr, rr in decs:
        c = "keep" if en else "remove"
        st = "KEEP" if en else "REMOVE"
        rs = kr if en else rr
        p.append(
            f'<div class="decision-card {c}"><div class="name">{_h(nm)}</div><div class="status">{st}</div><div class="reason">{_h(rs)}</div></div>'
        )
    p.append("</div>")
    # Register heatmap
    if pc.unused_registers is not None:
        unused = set(pc.unused_registers)
        n = len(unused)
        p.append(
            f'<h3 style="font-size:0.92rem;margin:18px 0 10px">Register File — {32 - n}/32 used ({n * 32} FFs saved)</h3>'
        )
        p.append('<div class="reg-grid">')
        for i in range(32):
            c = "reg-unused" if i in unused else "reg-used"
            p.append(f'<div class="reg-cell {c}" title="x{i}">x{i}</div>')
        p.append("</div>")
    if pc.removable_alu_ops:
        p.append(
            f'<h3 style="font-size:0.92rem;margin:18px 0 10px">Removable ALU Ops ({len(pc.removable_alu_ops)})</h3>'
        )
        p.append('<div style="display:flex;flex-wrap:wrap;gap:6px">')
        for op in sorted(pc.removable_alu_ops):
            p.append(_badge(op, "red"))
        p.append("</div>")
    if pc.removable_opcode_groups:
        p.append('<h3 style="font-size:0.92rem;margin:18px 0 10px">Removable Decoder Groups</h3>')
        p.append('<div style="display:flex;flex-wrap:wrap;gap:6px">')
        for g in sorted(pc.removable_opcode_groups):
            p.append(_badge(g, "red"))
        p.append("</div>")
    if pc.removable_mul_modes:
        p.append('<h3 style="font-size:0.92rem;margin:18px 0 10px">Removable MUL Modes</h3>')
        p.append('<div style="display:flex;flex-wrap:wrap;gap:6px">')
        for m in sorted(pc.removable_mul_modes):
            p.append(_badge(m, "red"))
        p.append("</div>")
    return _section("✂️", "RTL Pruning", "".join(p), id_attr="pruning")


def build_hwloop(ctx):
    if not ctx.hwloop_candidates:
        return ""
    p = []
    cands = ctx.hwloop_candidates
    p.append(f"<p>Candidates: <strong>{len(cands)}</strong></p>")
    mx_loops = max((c.loops_patched for c in cands), default=1) or 1
    p.append(
        '<table class="sweep-table"><tr><th>HW_LOOP</th><th>Loops Patched</th><th></th><th>Fused</th><th>Plain</th></tr>'
    )
    for c in cands:
        w = c.loops_patched / mx_loops * 100
        fh = '<span class="pass">✓</span>' if c.fused_hex else '<span class="fail">✗</span>'
        ph = '<span class="pass">✓</span>' if c.plain_hex else '<span class="fail">✗</span>'
        p.append(
            f'<tr><td class="mono" style="font-weight:700">{c.hw_loop}</td><td class="right">{c.loops_patched}</td>'
            f'<td class="sweep-bar-cell"><div class="sweep-bar-bg" data-w="{w:.0f}%" style="width:0;background:var(--yellow)"></div></td>'
            f"<td>{fh}</td><td>{ph}</td></tr>"
        )
    p.append("</table>")
    return _section("🔄", "Hardware Loops", "".join(p), id_attr="hwloop")


def build_synthesis(ctx):
    sy = ctx.synth_comparison
    if not sy or not sy.baseline.success:
        return ""
    p = []
    b, pr = sy.baseline, sy.pruned
    p.append(
        '<table><tr><th>Metric</th><th class="right">Baseline</th><th class="right">Pruned</th><th class="right">Savings</th></tr>'
    )
    p.append(
        f'<tr><td>Cells</td><td class="right mono">{_fmt(b.cells)}</td><td class="right mono">{_fmt(pr.cells)}</td><td class="right mono">{sy.cell_savings:+,} ({sy.cell_savings_pct:+.1f}%)</td></tr>'
    )
    p.append(
        f'<tr><td>Wires</td><td class="right mono">{_fmt(b.wires)}</td><td class="right mono">{_fmt(pr.wires)}</td><td class="right mono">{sy.wire_savings:+,}</td></tr>'
    )
    p.append(
        f'<tr><td>Wire bits</td><td class="right mono">{_fmt(b.wire_bits)}</td><td class="right mono">{_fmt(pr.wire_bits)}</td><td class="right mono">{sy.wire_bit_savings:+,}</td></tr>'
    )
    p.append("</table>")
    if b.cell_breakdown or pr.cell_breakdown:
        types = sorted(
            set(b.cell_breakdown) | set(pr.cell_breakdown),
            key=lambda t: b.cell_breakdown.get(t, 0),
            reverse=True,
        )[:12]
        p.append('<h3 style="font-size:0.92rem;margin:18px 0 10px">Cell Breakdown</h3>')
        p.append(
            '<table><tr><th>Type</th><th class="right">Baseline</th><th class="right">Pruned</th><th class="right">Δ</th></tr>'
        )
        for t in types:
            bv, pv = b.cell_breakdown.get(t, 0), pr.cell_breakdown.get(t, 0)
            p.append(
                f'<tr><td class="mono">{_h(t)}</td><td class="right mono">{_fmt(bv)}</td><td class="right mono">{_fmt(pv)}</td><td class="right mono">{pv - bv:+,}</td></tr>'
            )
        p.append("</table>")
    return _section("🏭", "Synthesis", "".join(p), id_attr="synthesis")


def build_config(cfg):
    p = [
        f"<p>Benchmark: <strong>{_h(cfg.benchmark_name)}</strong></p>",
        f"<p>Source: <code>{_h(cfg.benchmark_dir)}</code></p>",
        f"<p>Output: <code>{_h(cfg.output_dir)}</code></p>",
        f"<p>Phases: <strong>{', '.join(sorted(cfg.enabled_phases))}</strong></p>",
        f"<p>RF read C: <strong>{'PRUNE' if cfg.prune_rf_read_c else 'KEEP'}</strong> &nbsp;|&nbsp; RF write B: <strong>{'PRUNE' if cfg.prune_rf_write_b else 'KEEP'}</strong></p>",
    ]
    return _section("⚙️", "Configuration", "".join(p), collapsed=True, id_attr="config")
