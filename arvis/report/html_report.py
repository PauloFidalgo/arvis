"""HTML report generator for the ARVIS specialization pipeline.

Produces a single self-contained HTML file with all analysis results.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext

from arvis.report.html_sections import (
    _h,
    _section,
    build_bottleneck,
    build_config,
    build_fusion,
    build_hero,
    build_hwloop,
    build_pipeline_flow,
    build_profiling,
    build_pruning,
    build_synthesis,
    build_verification,
)
from arvis.report.html_template import CSS, JS


def generate_html_report(cfg: "ToolConfig", ctx: "PipelineContext") -> str:
    """Generate the full HTML report and write it to disk. Returns path."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build nav links
    nav_items = [
        ("✅", "Verification", "#verification"),
        ("📊", "Bottleneck", "#bottleneck"),
        ("🔍", "Profiling", "#profiling"),
        ("⚡", "Fusion", "#fusion"),
        ("✂️", "Pruning", "#pruning"),
        ("🔄", "HW Loops", "#hwloop"),
        ("🏭", "Synthesis", "#synthesis"),
        ("⚙️", "Config", "#config"),
    ]
    nav_links = "".join(f'<a class="nav-link" href="{href}">{icon} {label}</a>' for icon, label, href in nav_items)
    nav = f'<nav class="nav"><div class="nav-brand">ARVIS</div><div class="nav-links">{nav_links}</div></nav>'

    hero = build_hero(cfg, ctx)
    flow = build_pipeline_flow(ctx)
    verif = _section("✅", "Verification Summary", build_verification(cfg, ctx), id_attr="verification")
    bottleneck = build_bottleneck(ctx)
    profiling = build_profiling(ctx)
    fusion = build_fusion(ctx)
    pruning = build_pruning(ctx)
    hwloop = build_hwloop(ctx)
    synthesis = build_synthesis(ctx)
    config = build_config(cfg)

    body = hero + flow + verif + bottleneck + profiling + fusion + pruning + hwloop + synthesis + config
    body = "".join(
        s
        for s in [
            hero,
            flow,
            verif,
            bottleneck,
            profiling,
            fusion,
            pruning,
            hwloop,
            synthesis,
            config,
        ]
        if s
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARVIS — {_h(cfg.benchmark_name)}</title>
<style>{CSS}</style></head><body>
{nav}
<div class="container">
<div class="header">
<h1>ARVIS Specialization Report</h1>
<div class="subtitle">{_h(cfg.benchmark_name)} — CV32E40P Workload Specializer</div>
<div class="timestamp">Generated {now}</div>
</div>
{body}
<div class="footer">ARVIS — Automated RISC-V Intelligent Specialization</div>
</div>
<script>{JS}</script>
</body></html>"""

    out_path = os.path.join(cfg.output_dir, "report.html")
    os.makedirs(cfg.output_dir, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html_doc)
    return out_path
