"""Contract tests for the Phase 6.5 :class:`MarkdownReporter`.

Covers:

* ``name`` property
* ``emit`` produces a deterministic Markdown file
* Output is reproducible across runs (byte-identical for the
  same :class:`PipelineResult`)
* Each section (header / decisions / variants / extra) renders
  predictably
* Edge cases: empty decisions, no variants, no sim/synth results
"""

from __future__ import annotations

from arvis.core.pipeline import PipelineResult, VariantResult
from arvis.core.strategy import (
    FusionDecision,
    LoopDecision,
    PruneDecision,
    WidthDecision,
)
from arvis.core.synthesis import SynthResult
from arvis.core.verifier import SimResult
from arvis.report.markdown_reporter import MarkdownReporter

# ─── Basic API ───────────────────────────────────────────────────


def test_markdown_reporter_name() -> None:
    assert MarkdownReporter().name == "markdown"


def test_markdown_reporter_emits_into_existing_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``emit`` writes ``report.md`` and returns its path."""
    result = PipelineResult(workload_name="ud", target_name="cv32e40p")
    md_path = MarkdownReporter().emit(result, tmp_path)
    assert md_path == tmp_path / "report.md"
    assert md_path.exists()


def test_markdown_reporter_creates_missing_output_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``emit`` mkdirs the output path on demand."""
    out = tmp_path / "deep" / "nested" / "subdir"
    result = PipelineResult(workload_name="ud", target_name="cv32e40p")
    md_path = MarkdownReporter().emit(result, out)
    assert md_path.exists()


# ─── Header section ───────────────────────────────────────────────


def test_header_includes_workload_target_counts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = PipelineResult(
        workload_name="kyber",
        target_name="cv32e40p",
        decisions={"X": PruneDecision()},
        variants=[
            VariantResult(label="baseline", rtl_dir=tmp_path),
            VariantResult(label="all", rtl_dir=tmp_path),
        ],
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()

    assert "# ARVIS Pipeline Report" in md
    assert "**Workload**: `kyber`" in md
    assert "**Target**: `cv32e40p`" in md
    assert "**Decisions**: 1" in md
    assert "**Variants**: 2" in md


# ─── Decisions section ────────────────────────────────────────────


def test_decisions_section_handles_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = PipelineResult(workload_name="ud", target_name="cv32e40p")
    md = MarkdownReporter().emit(result, tmp_path).read_text()
    assert "## Decisions" in md
    assert "_(none)_" in md


def test_decisions_section_renders_pydantic_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Pydantic decisions render every field via ``model_dump``."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        decisions={
            "WidthDecision": WidthDecision(pc_width=14, hwlp_addr_width=14, counter_width=12),
        },
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()

    assert "### WidthDecision" in md
    assert "**pc_width**: `14`" in md
    assert "**hwlp_addr_width**: `14`" in md
    assert "**counter_width**: `12`" in md


def test_decisions_section_renders_frozenset_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """frozenset values render as ``[a, b, c]`` in sorted order."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        decisions={
            "PruneDecision": PruneDecision(
                removable_alu_ops=frozenset({"ALU_DIV", "ALU_REM"}),
            ),
        },
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()
    assert "[ALU_DIV, ALU_REM]" in md


# ─── Variants section ─────────────────────────────────────────────


def test_variants_section_handles_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = PipelineResult(workload_name="ud", target_name="cv32e40p")
    md = MarkdownReporter().emit(result, tmp_path).read_text()
    assert "## Variants" in md
    assert "_(none emitted)_" in md


def test_variants_section_renders_full_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A passing sim + cell count -> ADP shown."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        variants=[
            VariantResult(
                label="all",
                rtl_dir=tmp_path / "rtl_all",
                hex_path=tmp_path / "all.hex",
                sim_result=SimResult(test_passed=True, total_cycles=850000),
                synth_result=SynthResult(cell_count=39200),
            ),
        ],
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()

    assert "| `all` |" in md
    assert "✓ pass" in md
    assert "850,000" in md
    assert "39,200" in md
    # ADP = 850000 * 39200 / 1e9 = 33.32
    assert "33.32" in md


def test_variants_section_handles_failed_sim(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``passed=False`` -> ✗ fail status; ADP is —."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        variants=[
            VariantResult(
                label="hwloop",
                rtl_dir=tmp_path / "rtl_hwloop",
                sim_result=SimResult(test_passed=False),
                synth_result=SynthResult(cell_count=39000),
            ),
        ],
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()

    assert "| `hwloop` |" in md
    assert "✗ fail" in md
    assert "39,000" in md


def test_variants_section_handles_no_sim_no_synth(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """When sim and synth aren't run, the row shows em-dashes."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        variants=[
            VariantResult(label="rtl_only", rtl_dir=tmp_path / "rtl_only"),
        ],
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()
    assert "| `rtl_only` |" in md
    # The em-dash indicates "no data".
    assert " — " in md


# ─── Reproducibility ──────────────────────────────────────────────


def test_emit_is_byte_reproducible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Two runs with the same PipelineResult produce byte-identical output."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        decisions={
            "WidthDecision": WidthDecision(pc_width=14),
            "PruneDecision": PruneDecision(),
        },
        variants=[
            VariantResult(
                label="baseline",
                rtl_dir=tmp_path / "rtl_baseline",
                sim_result=SimResult(test_passed=True, total_cycles=1000000),
                synth_result=SynthResult(cell_count=38800),
            ),
        ],
    )

    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"
    md_a = MarkdownReporter().emit(result, out_a)
    md_b = MarkdownReporter().emit(result, out_b)

    assert md_a.read_bytes() == md_b.read_bytes()


def test_decisions_render_in_alphabetical_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Section order is deterministic regardless of insertion order."""
    decisions = {
        "WidthDecision": WidthDecision(pc_width=14),
        "PruneDecision": PruneDecision(),
        "FusionDecision": FusionDecision(),
        "LoopDecision": LoopDecision(),
    }
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        decisions=decisions,
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()

    # Expect alphabetical order: Fusion, Loop, Prune, Width.
    fusion_pos = md.index("### FusionDecision")
    loop_pos = md.index("### LoopDecision")
    prune_pos = md.index("### PruneDecision")
    width_pos = md.index("### WidthDecision")
    assert fusion_pos < loop_pos < prune_pos < width_pos


# ─── Extra section ────────────────────────────────────────────────


def test_extra_section_appears_when_present(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``result.extra`` is rendered as a bullet list."""
    result = PipelineResult(
        workload_name="ud",
        target_name="cv32e40p",
        extra={"sweep_winner": "HW_LOOP=2", "synthesis_warning": "missing constraint"},
    )
    md = MarkdownReporter().emit(result, tmp_path).read_text()

    assert "## Extra" in md
    assert "**sweep_winner**" in md
    assert "**synthesis_warning**" in md


def test_extra_section_omitted_when_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """No extra entries -> no Extra section in output."""
    result = PipelineResult(workload_name="ud", target_name="cv32e40p")
    md = MarkdownReporter().emit(result, tmp_path).read_text()
    assert "## Extra" not in md


# ─── Value formatter helper ───────────────────────────────────────


def test_value_formatter_handles_long_lists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A long list collapses to a summary form."""
    from arvis.report.markdown_reporter import _format_value

    short = ["a", "b", "c"]
    assert _format_value(short) == "[a, b, c]"

    # Build a list whose stringification exceeds the threshold.
    long_list = [f"item_{i:03d}" for i in range(50)]
    rendered = _format_value(long_list)
    assert "50 items" in rendered
    assert "item_000" in rendered
    assert "item_049" in rendered


def test_value_formatter_handles_dicts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Small dicts render inline; large dicts collapse."""
    from arvis.report.markdown_reporter import _format_value

    assert _format_value({}) == "{}"
    rendered = _format_value({"a": 1, "b": 2})
    # Sorted keys.
    assert rendered.startswith("{a=")
    assert "b=" in rendered

    large = {f"key_{i:03d}": i for i in range(20)}
    summary = _format_value(large)
    assert "20 entries" in summary


def test_value_formatter_handles_bools_and_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from arvis.report.markdown_reporter import _format_value

    assert _format_value(True) == "true"
    assert _format_value(False) == "false"
    assert _format_value(None) == "_null_"
