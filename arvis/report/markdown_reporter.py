"""Markdown reporter for :class:`PipelineResult`.

Writes a structured Markdown file summarising:

* The workload and target.
* Every :class:`Decision` produced by the strategies (with key
  fields surfaced).
* Per-variant emission status, simulation cycles, synthesis
  cells, and ADP.
* Any ``extra`` fields recorded by strategies / variants.

Design choices
--------------

* **Pure Python.**  No Jinja2, no external templating.  The
  abstract :class:`Reporter` interface guarantees the output is
  deterministic; mixing in a templater would break that.
* **Stable section ordering.**  Tests compare report contents
  byte-for-byte across runs; the section layout is fixed by the
  module, not derived from the result dict's iteration order.
* **No external links.**  Reports are self-contained so they can
  be diffed in a shell without rendering.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arvis.core.reporter import Reporter

if TYPE_CHECKING:
    from arvis.core.pipeline import PipelineResult, VariantResult
    from arvis.core.strategy import Decision

logger = logging.getLogger(__name__)


class MarkdownReporter(Reporter):
    """Render a :class:`PipelineResult` to a single Markdown file.

    The output filename is ``report.md`` inside ``out_path``.

    Idempotency
    -----------
    Two runs with the same :class:`PipelineResult` produce
    byte-identical output.  Timestamps are recorded once at
    construction and reused; ``elapsed_seconds`` and similar
    runtime measurements are filtered out of the report.
    """

    @property
    def name(self) -> str:
        return "markdown"

    def emit(self, result: PipelineResult, out_path: Path) -> Path:
        """Write ``out_path/report.md`` and return its path."""
        out_path = Path(out_path)
        out_path.mkdir(parents=True, exist_ok=True)
        md_path = out_path / "report.md"

        sections: list[str] = []
        sections.append(self._header(result))
        sections.append(self._decisions(result))
        sections.append(self._variants(result))
        if result.extra:
            sections.append(self._extra(result))

        md_path.write_text("\n\n".join(sections) + "\n")
        logger.info("MarkdownReporter: wrote %s", md_path)
        return md_path

    # ── Section builders ─────────────────────────────────────────

    @staticmethod
    def _header(result: PipelineResult) -> str:
        return (
            f"# ARVIS Pipeline Report\n\n"
            f"- **Workload**: `{result.workload_name}`\n"
            f"- **Target**: `{result.target_name}`\n"
            f"- **Decisions**: {len(result.decisions)}\n"
            f"- **Variants**: {len(result.variants)}"
        )

    def _decisions(self, result: PipelineResult) -> str:
        if not result.decisions:
            return "## Decisions\n\n_(none)_"

        lines = ["## Decisions", ""]
        # Sort by name for deterministic output.
        for name in sorted(result.decisions.keys()):
            decision = result.decisions[name]
            lines.append(f"### {name}")
            lines.append("")
            lines.extend(self._format_decision(decision))
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _format_decision(decision: Decision) -> list[str]:
        """Render a decision as a Markdown bullet list of its
        non-private, non-empty fields."""
        # Pydantic models expose ``model_dump()``; dataclasses
        # expose ``__dict__``.  Use the most informative one.
        try:
            payload = decision.model_dump()  # type: ignore[attr-defined]
        except AttributeError:
            payload = {k: v for k, v in vars(decision).items() if not k.startswith("_")}

        out: list[str] = []
        for key in sorted(payload.keys()):
            value = payload[key]
            rendered = _format_value(value)
            out.append(f"- **{key}**: {rendered}")
        return out

    def _variants(self, result: PipelineResult) -> str:
        if not result.variants:
            return "## Variants\n\n_(none emitted)_"

        # Build a Markdown table with one row per variant.
        # Columns: label, RTL dir, hex, sim status, cycles, cells, ADP.
        lines = [
            "## Variants",
            "",
            (
                "| Variant | RTL dir | Hex | Sim | Cycles | Cells | ADP |\n"
                "|---|---|---|---|---|---|---|"
            ),
        ]
        for v in result.variants:
            lines.append(self._variant_row(v))
        return "\n".join(lines)

    @staticmethod
    def _variant_row(v: VariantResult) -> str:
        sim = v.sim_result
        synth = v.synth_result
        cycles = "—"
        cells = "—"
        adp = "—"
        sim_status = "—"
        if sim is not None:
            sim_status = "✓ pass" if sim.test_passed else "✗ fail"
            cycles_val = sim.total_cycles
            if cycles_val is not None and cycles_val >= 0:
                cycles = f"{cycles_val:,}"
        if synth is not None and synth.cell_count is not None:
            cells = f"{synth.cell_count:,}"
        # ADP only computable when both numeric.
        if (
            sim is not None
            and synth is not None
            and sim.test_passed
            and synth.cell_count is not None
            and sim.total_cycles
            and sim.total_cycles > 0
        ):
            adp = f"{sim.total_cycles * synth.cell_count / 1e9:.2f}"

        rtl_str = str(v.rtl_dir.name) if v.rtl_dir else "—"
        hex_str = str(v.hex_path.name) if v.hex_path else "—"
        return f"| `{v.label}` | `{rtl_str}` | `{hex_str}` | {sim_status} | {cycles} | {cells} | {adp} |"

    @staticmethod
    def _extra(result: PipelineResult) -> str:
        lines = ["## Extra", ""]
        for key in sorted(result.extra.keys()):
            value = result.extra[key]
            rendered = _format_value(value)
            lines.append(f"- **{key}**: {rendered}")
        return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────


def _format_value(value: Any, *, max_inline_len: int = 80) -> str:
    """Render a Decision field value for Markdown.

    Lists / sets / tuples become inline ``[a, b, c]`` when short,
    or a wrapped ``[…]`` indicator when long.  Dicts go through
    a sorted ``{k: v, ...}`` rendering.  Other types fall through
    to ``repr``.
    """
    if isinstance(value, (frozenset, set)):
        items = sorted(str(x) for x in value)
        return _inline_list(items, max_inline_len)
    if isinstance(value, (list, tuple)):
        items = [str(x) for x in value]
        return _inline_list(items, max_inline_len)
    if isinstance(value, dict):
        if not value:
            return "{}"
        kv = ", ".join(f"{k}={_format_value(v)}" for k, v in sorted(value.items()))
        if len(kv) > max_inline_len:
            return f"{{{len(value)} entries}}"
        return f"{{{kv}}}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "_null_"
    return f"`{value!r}`"


def _inline_list(items: Iterable[str], max_inline_len: int) -> str:
    items_list = list(items)
    if not items_list:
        return "[]"
    rendered = "[" + ", ".join(items_list) + "]"
    if len(rendered) <= max_inline_len:
        return rendered
    return f"[{len(items_list)} items: {items_list[0]}, …, {items_list[-1]}]"


__all__ = ["MarkdownReporter"]
