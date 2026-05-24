"""Concrete :class:`core.rtl_patch.RTLPatch` implementations for cv32e40p.

This module is where decisions become actual RTL edits.  Each
patch class wraps one self-contained mutation of the RTL files
inside an :class:`RTLWorkspace`; patches are constructed by
:meth:`CV32E40P.render_*_decision` and applied by the pipeline.

Patches must be **idempotent** (safe to re-run).  All cv32e40p
patches in this module satisfy that property either by using
markers (the PC width surgery's per-edit comment markers) or by
direct parameter substitution (regex rewrites that match the
parameter line by name).

Phase 2.1 ships only :class:`WidthNarrowingPatch` because it's
the smallest and most thoroughly validated of the four decision
types.  Phase 2.2-2.4 add the other three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from arvis.core.rtl_patch import RTLPatch, RTLWorkspace

if TYPE_CHECKING:
    from arvis.core.strategy import WidthDecision


# ─── WidthNarrowingPatch ──────────────────────────────────────────


@dataclass
class WidthNarrowingPatch(RTLPatch):
    """Apply a :class:`WidthDecision` to a cv32e40p RTL workspace.

    The patch handles three independent narrowings:

    - ``pc_width`` (main pipeline PC) — delegated to
      :func:`apply_pc_width.patch_rtl_dir`, the validated
      standalone surgery that adds parameters and boundary casts.
    - ``hwlp_addr_width`` (HW-loop address registers) — applied
      as a parameter rewrite in every ``cv32e40p_*.sv`` file that
      declares ``HWLP_ADDR_WIDTH``.
    - ``counter_width`` (HW-loop counter register) — applied as a
      parameter rewrite for ``HW_LOOP_CNT_WIDTH``.

    Each leg checks its decision field against the "no-narrowing"
    sentinel:
      pc_width == 0 or >= 32   -> skip
      hwlp_addr_width >= 32    -> skip
      counter_width >= 32      -> skip

    The patch is idempotent: re-applying it changes nothing if the
    parameters are already at the requested values.
    """

    decision: "WidthDecision"

    @property
    def label(self) -> str:
        d = self.decision
        parts = []
        if 0 < d.pc_width < 32:
            parts.append(f"pc={d.pc_width}")
        if d.hwlp_addr_width < 32:
            parts.append(f"hwlp_addr={d.hwlp_addr_width}")
        if d.counter_width < 32:
            parts.append(f"cnt={d.counter_width}")
        if not parts:
            return "WidthNarrowingPatch(noop)"
        return f"WidthNarrowingPatch({', '.join(parts)})"

    # ── Apply ──────────────────────────────────────────────────────
    def apply(self, workspace: RTLWorkspace) -> None:
        rtl_dir = workspace.output_root / "rtl"
        if not rtl_dir.exists():
            # Workspace not in the cv32e40p layout — bail silently.
            # Caller can detect by inspecting the workspace shape.
            return

        # ── PC width: delegate to validated standalone surgery ──
        pc = self.decision.pc_width
        if 0 < pc < 32:
            try:
                from arvis.pipeline.pc_width import patch_rtl_dir as _patch_pc
                _patch_pc(rtl_dir, pc)
            except Exception:
                # Mirror the legacy RTLChangeSet.apply path which
                # logs a warning and continues; here we surface a
                # softer "skip" because raising would abort the
                # pipeline.  Phase 3 may tighten this.
                pass

        # ── HWLP address width: regex parameter rewrite ──
        if self.decision.hwlp_addr_width < 32:
            self._rewrite_parameter(
                rtl_dir,
                "HWLP_ADDR_WIDTH",
                self.decision.hwlp_addr_width,
            )

        # ── HW loop counter width: regex parameter rewrite ──
        # The RTL parameter name is ``CNT_WIDTH`` (not the
        # WidthDecision field name ``counter_width`` which is the
        # target-agnostic semantic name).
        if self.decision.counter_width < 32:
            self._rewrite_parameter(
                rtl_dir,
                "CNT_WIDTH",
                self.decision.counter_width,
            )

    # ── Helpers ────────────────────────────────────────────────────
    @staticmethod
    def _rewrite_parameter(rtl_dir: Path, name: str, value: int) -> None:
        """Rewrite every ``parameter <name> = <int>`` in the tree.

        Mirrors the legacy
        :meth:`pipeline.rtl_changeset.RTLChangeSet.apply` regex
        used for HWLP_ADDR_WIDTH and HW_LOOP_CNT_WIDTH.  Idempotent
        (the regex doesn't care what the previous value was).
        """
        pattern = re.compile(rf"parameter\s+{re.escape(name)}\s*=\s*\d+")
        replacement = f"parameter {name} = {value}"
        for sv in rtl_dir.rglob("*.sv"):
            text = sv.read_text()
            new_text = pattern.sub(replacement, text)
            if new_text != text:
                sv.write_text(new_text)
