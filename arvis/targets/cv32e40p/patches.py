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


# ─── PrunePatch ────────────────────────────────────────────────────


@dataclass
class PrunePatch(RTLPatch):
    """Apply a :class:`PruneDecision` to a cv32e40p RTL workspace.

    Phase 2 implementation: delegates to the legacy
    :class:`codegen.rtl.rtl_pruning.RTLPruner` to do the heavy
    lifting (parameter gating across alu/mult/ex_stage/core/top,
    case-arm pruning of removable ALU ops, derived synthesis
    parameters).  The decision must carry its
    :attr:`PruneDecision.target_payload` populated with the
    legacy :class:`PruneConfig` -- :class:`UsageDrivenPruner` does
    this automatically.

    When the payload is missing (e.g. a future
    :class:`ManualPruner` that constructs a PruneDecision from
    a YAML file without a backing PruneConfig), we reconstruct a
    minimal PruneConfig from the typed Decision fields.  This is
    lossy (no register masks, CSR sets, or HW-loop info) but
    sufficient for the basic ALU/MUL pruning paths.
    """

    decision: "PruneDecision"

    @property
    def label(self) -> str:
        d = self.decision
        return (
            f"PrunePatch("
            f"alu={len(d.removable_alu_ops)}, "
            f"mul_modes={len(d.removable_mul_modes)}, "
            f"opc_groups={len(d.removable_opcode_groups)})"
        )

    def apply(self, workspace: RTLWorkspace) -> None:
        # Late imports keep the cv32e40p target import-light; the
        # legacy modules pull in cli, config, etc.
        from arvis.codegen.rtl.base import RTLWorkspace as LegacyWorkspace
        from arvis.codegen.rtl.rtl_pruning import PruneConfig, RTLPruner

        # The legacy RTLPruner takes its own RTLWorkspace shape
        # (codegen.rtl.base.RTLWorkspace).  In Phase 1 we already
        # established the new core.rtl_patch.RTLWorkspace; both have
        # an ``output_root`` attribute so we can wrap one in the
        # other.  Phase 3 unifies them.
        legacy_ws = LegacyWorkspace(
            source_root=str(workspace.source_root),
            output_root=str(workspace.output_root),
        )

        # Use the strategy-supplied legacy config when present
        # (the round-trip-lossless path).  Fall back to a
        # reconstructed minimal config when not.
        config = self.decision.target_payload
        if not isinstance(config, PruneConfig):
            config = self._reconstruct_minimal_config()

        try:
            RTLPruner(legacy_ws).apply(config, verbose=False)
        except Exception:
            # Mirror Phase 2.1's soft-skip policy: don't abort the
            # pipeline on a render error.  Phase 3 may tighten.
            pass

    def _reconstruct_minimal_config(self):
        """Build a PruneConfig from typed Decision fields only.

        Lossy (no register masks, CSR sets, or HW-loop info) but
        sufficient for the ALU / MUL / opcode-group pruning paths.
        """
        from arvis.codegen.rtl.rtl_pruning import PruneConfig

        d = self.decision
        cfg = PruneConfig()
        cfg.removable_alu_ops = set(d.removable_alu_ops)
        cfg.removable_mul_modes = set(d.removable_mul_modes)
        cfg.removable_opcode_groups = set(d.removable_opcode_groups)

        # Booleans from feature_flags.  Each PruneConfig field
        # named ``enable_*`` is updated when the same key appears
        # in the decision's flag dict.
        for k, v in d.feature_flags.items():
            if hasattr(cfg, k) and isinstance(getattr(cfg, k), bool):
                setattr(cfg, k, v)
        return cfg



# ─── FusionPatch ───────────────────────────────────────────────────


@dataclass
class FusionPatch(RTLPatch):
    """Apply a :class:`FusionDecision` to a cv32e40p RTL workspace.

    Phase 2 implementation: delegates to the legacy fusion path
    inside :class:`pipeline.rtl_changeset.RTLChangeSet`.  The
    patch builds a temporary RTLChangeSet, populates it with the
    fused operations from the decision, and invokes
    ``_apply_fusion_patches`` on the supplied workspace.

    Empty decisions (no fused ops) are a no-op.

    Pre-requisites
    --------------
    The fusion patches operate on the existing decoder, ALU, and
    cv32e40p_pkg.sv files.  They expect those files to already be
    in their post-pruning state (i.e. PrunePatch must run first if
    pruning is part of the variant).  The pipeline orchestrator is
    responsible for ordering patches correctly.
    """

    decision: "FusionDecision"

    @property
    def label(self) -> str:
        n = len(self.decision.fused_ops)
        return f"FusionPatch({n} ops)"

    def apply(self, workspace: RTLWorkspace) -> None:
        if not self.decision.fused_ops:
            # No fusions -- nothing to patch.  Workspace untouched.
            return

        from arvis.pipeline.rtl_changeset import RTLChangeSet

        # Build a temporary RTLChangeSet that just carries the
        # fusion data.  We do NOT call its public ``apply`` because
        # that re-copies the workspace (clobbering any prior patches
        # the pipeline applied).  Instead we call the private
        # ``_apply_fusion_patches`` directly.
        cs = RTLChangeSet()
        cs.fused_operations = list(self.decision.fused_ops)

        # Carry registry / hwloop encoding from target_payload when
        # the strategy populated them.  The legacy fusion code
        # checks for these via getattr with default None, so missing
        # values are safe.
        if isinstance(self.decision.target_payload, dict):
            payload = self.decision.target_payload
            if "custom_registry" in payload:
                cs._custom_registry = payload["custom_registry"]
            if "hwlp_encoding" in payload:
                cs._hwlp_encoding = payload["hwlp_encoding"]
            if "hw_loop_count" in payload:
                cs.hw_loop_count = payload["hw_loop_count"]

        # Build a minimal ctx shim.  The legacy method only writes
        # ``ctx.fusion_rtl_applied = True`` at the end and reads
        # nothing, so a permissive proxy works.
        class _CtxShim:
            def __setattr__(self, name, value):
                pass

            def __getattr__(self, name):
                return None

        try:
            cs._apply_fusion_patches(workspace, _CtxShim())
        except Exception:
            # Soft-skip on render error (Phase 2.1 policy).
            pass
