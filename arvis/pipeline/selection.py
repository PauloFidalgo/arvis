"""
Phase 2: N-gram Fusion Pattern Discovery.

Analyzes all hot loops to find instruction n-gram patterns that can be
fused into single custom instructions. Deduplicates across loops and
produces ctx.all_fusions which is passed directly to GCC.

GCC decides which patterns to actually use in the compiled binary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


def run(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Execute Phase 2: N-gram fusion pattern discovery."""
    from arvis.analysis.fusion import AggregationKey, FusionAnalyzer
    from arvis.cli import print_metric, print_section

    print_section("N-GRAM FUSION PATTERN DISCOVERY")

    assert ctx.profile is not None, "Phase 1 must run before Phase 2"

    ctx.fusion_analyzer = FusionAnalyzer(ctx.blocks, ctx.profile)
    all_fusions = []

    for loop in ctx.loops:
        if loop.hotness_score < cfg.loop_hotness_threshold:
            continue
        fusions = ctx.fusion_analyzer.analyze_loop(
            loop,
            min_n=cfg.fusion_min_n,
            max_n=cfg.fusion_max_n,
            min_frequency=cfg.fusion_min_frequency,
        )
        all_fusions.extend(fusions)

    # Global deduplication across loops
    by_agg_key: dict = {}
    for c in all_fusions:
        sig = c.signature if hasattr(c, "signature") and c.signature else None
        if sig is not None:
            agg_key = AggregationKey(
                signature=sig,
                min_read_ports=c.liveness.min_read_ports,
                min_write_ports=c.liveness.min_write_ports_eliminate,
            )
        else:
            agg_key = c.pattern
        if agg_key in by_agg_key:
            existing = by_agg_key[agg_key]
            existing.frequency += c.frequency
            existing.static_instances = getattr(existing, "static_instances", 1) + getattr(c, "static_instances", 1)
            if hasattr(c, "locations") and c.locations:
                if not hasattr(existing, "locations"):
                    existing.locations = []
                existing.locations.extend(c.locations)
            has_const = (
                hasattr(c, "constant_operands") and c.constant_operands and hasattr(existing, "constant_operands")
            )
            if has_const:
                FusionAnalyzer._merge_constancy(existing.constant_operands, c.constant_operands)
            if hasattr(c, "imm_stats") and c.imm_stats and c.imm_stats.value_counts:
                if not hasattr(existing, "imm_stats") or existing.imm_stats is None:
                    existing.imm_stats = c.imm_stats
                else:
                    for pos, counts in c.imm_stats.value_counts.items():
                        if pos not in existing.imm_stats.value_counts:
                            existing.imm_stats.value_counts[pos] = dict(counts)
                        else:
                            for val, cnt in counts.items():
                                existing.imm_stats.value_counts[pos][val] = (
                                    existing.imm_stats.value_counts[pos].get(val, 0) + cnt
                                )
        else:
            by_agg_key[agg_key] = c

    # Re-compute imm_should_hardcode from merged imm_stats
    for cand in by_agg_key.values():
        if hasattr(cand, "imm_stats") and cand.imm_stats and cand.imm_stats.value_counts:
            n_insts = len(cand.pattern) if hasattr(cand, "pattern") else len(cand.instructions)
            decisions = cand.imm_stats.get_all_decisions(n_insts, threshold=1.0)
            cand.imm_should_hardcode = tuple(d[0] for d in decisions)
            cand.hardcoded_imm_values = tuple(d[1] for d in decisions)
            cand.has_hardcoded_imm = any(d[0] for d in decisions)

    ctx.all_fusions = list(by_agg_key.values())
    n_hc = sum(1 for c in ctx.all_fusions if getattr(c, "has_hardcoded_imm", False))

    print_metric("Unique fusion candidates", str(len(ctx.all_fusions)))
    print_metric("With hardcoded immediates", str(n_hc))

    # Show top patterns by frequency
    top = sorted(ctx.all_fusions, key=lambda c: c.frequency, reverse=True)[:10]
    if top:
        print()
        for i, c in enumerate(top):
            pat = " -> ".join(c.pattern)
            print(f"    {i + 1:2d}. {pat:<30s} freq={c.frequency:>10,}")
        if len(ctx.all_fusions) > 10:
            print(f"    ... and {len(ctx.all_fusions) - 10} more")
