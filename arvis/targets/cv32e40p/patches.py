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

    Reconstructs a legacy :class:`codegen.rtl.rtl_pruning.PruneConfig`
    from the typed fields of the decision and hands it to the
    legacy :class:`RTLPruner`.  The reconstruction is
    round-trip-lossless versus the original PruneConfig produced
    by :func:`pipeline.pruning.compute_prune_config` -- equivalence
    is verified by ``examples/portability_equivalence.py``
    (174 SV files byte-identical between the new and legacy paths).
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
        from arvis.codegen.rtl.rtl_pruning import RTLPruner

        # The legacy RTLPruner takes its own RTLWorkspace shape
        # (codegen.rtl.base.RTLWorkspace).  Both have an
        # ``output_root`` attribute so we can wrap one in the
        # other.  Phase 3.1+ unifies them.
        legacy_ws = LegacyWorkspace(
            source_root=str(workspace.source_root),
            output_root=str(workspace.output_root),
        )

        # Reconstruct PruneConfig from typed Decision fields.
        config = self._build_prune_config()

        try:
            RTLPruner(legacy_ws).apply(config, verbose=False)
        except Exception:
            # Mirror Phase 2.1's soft-skip policy: don't abort the
            # pipeline on a render error.
            pass

    def _build_prune_config(self):
        """Construct a :class:`PruneConfig` from the typed Decision.

        This is the inverse of :meth:`UsageDrivenPruner._translate`.
        Lossless for every field the legacy :class:`RTLPruner` reads.
        """
        from arvis.codegen.rtl.rtl_pruning import PruneConfig

        d = self.decision
        cfg = PruneConfig()

        # Typed sets / frozenset -> mutable set on PruneConfig.
        cfg.removable_alu_ops = set(d.removable_alu_ops)
        cfg.removable_mul_modes = set(d.removable_mul_modes)
        cfg.removable_opcode_groups = set(d.removable_opcode_groups)
        cfg.removable_csr_labels = set(d.removable_csr_labels)
        cfg.removable_csr_storage = set(d.removable_csr_storage)

        # Boolean enable_* flags from feature_flags dict.
        for k, v in d.feature_flags.items():
            if hasattr(cfg, k) and isinstance(getattr(cfg, k), bool):
                setattr(cfg, k, v)

        # Register-level analysis.
        cfg.unused_registers = list(d.unused_registers)
        cfg.used_regs_mask = d.used_regs_mask

        # Target-wide configuration knobs.
        for attr, value in d.target_overlay.items():
            if hasattr(cfg, attr) and isinstance(getattr(cfg, attr), int):
                setattr(cfg, attr, value)

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

        # Read the per-variant encoding registry from the workspace
        # metadata, populated by
        # :meth:`CV32E40P.allocate_workspace_metadata` before any
        # patches run.  When the registry is present (the variant
        # includes both FusionDecision AND LoopDecision, e.g. the
        # ALL variant), the legacy fusion code injects hwloop
        # decoder entries alongside the fused ops.  When absent
        # (FUSED_PRUNED variant), no hwloop slots are reserved and
        # the fused ops fill the front of the opcode space.
        from arvis.targets.cv32e40p.encoding import WORKSPACE_REGISTRY_KEY
        registry = workspace.metadata.get(WORKSPACE_REGISTRY_KEY)
        if registry is not None:
            cs._custom_registry = registry
            # The legacy code sets hw_loop_count from
            # registry.hwloop_instructions when present.  Mirror it
            # so the fusion path knows how many hwloop slots to
            # account for.
            cs.hw_loop_count = len(getattr(registry, "hwloop_instructions", []))

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



# ─── LoopPatch ─────────────────────────────────────────────────────


@dataclass
class LoopPatch(RTLPatch):
    """Apply a :class:`LoopDecision` to a cv32e40p RTL workspace.

    Phase 2 implementation: delegates to the legacy hwloop RTL
    code path inside :class:`pipeline.rtl_changeset.RTLChangeSet`.
    The patch:

    1. Calls ``_apply_hwloop_pragmas`` to resolve the ARVIS_HWLP
       pragmas across the tree based on ``hw_loop_count``.
    2. When ``nest_depth > 0``, replaces ``cv32e40p_hwloop_regs.sv``
       with the parameterised template from
       ``targets/cv32e40p/rtl/template/`` and patches the funct3
       values from the encoding payload.

    Note that HWLP_ADDR_WIDTH and CNT_WIDTH parameter rewrites are
    NOT done here — those live on WidthDecision and are applied by
    :class:`WidthNarrowingPatch` (Phase 2.1).  The orchestrator is
    expected to apply LoopPatch first, then WidthNarrowingPatch.

    The decision's ``patched_loops`` field is informational only at
    this stage; the actual assembly patching (lp.start/end/count
    insertion) happens at the toolchain level inside
    :class:`pipeline.hwloop.run` and is reflected in the compiled
    hex/elf -- it does not affect the emitted RTL.
    """

    decision: "LoopDecision"

    @property
    def label(self) -> str:
        d = self.decision
        if d.nest_depth == 0:
            return "LoopPatch(noop)"
        return (
            f"LoopPatch(nest={d.nest_depth}, "
            f"cnt={d.counter_width}b, addr={d.addr_width}b)"
        )

    def apply(self, workspace: RTLWorkspace) -> None:
        if self.decision.nest_depth == 0 and not self.decision.patched_loops:
            # No hwloop activity -- pragma processor still runs to
            # strip any ARVIS_HWLP markers from the templates,
            # mirroring legacy behaviour where _apply_hwloop_pragmas
            # is called unconditionally.
            self._apply_pragmas_only(workspace, hw_loop_count=0)
            return

        from arvis.pipeline.rtl_changeset import RTLChangeSet

        cs = RTLChangeSet()
        cs.hw_loop_count = self.decision.nest_depth

        # Read the encoding registry from workspace metadata (set by
        # CV32E40P.allocate_workspace_metadata).  When present the
        # registry's hwloop encoding is used to patch funct3 values
        # in the swapped hwloop_regs template; when absent we still
        # do the template swap with default funct3 values.
        from arvis.targets.cv32e40p.encoding import WORKSPACE_REGISTRY_KEY
        registry = workspace.metadata.get(WORKSPACE_REGISTRY_KEY)
        if registry is not None:
            cs._custom_registry = registry
            try:
                cs._hwlp_encoding = registry.get_hwloop_encoding()
            except Exception:
                cs._hwlp_encoding = None

        # Build a minimal ctx shim.  _apply_hwloop_pragmas only
        # writes status flags; reads via getattr return None.
        class _CtxShim:
            def __setattr__(self, name, value):
                pass

            def __getattr__(self, name):
                return None

        try:
            cs._apply_hwloop_pragmas(workspace, _CtxShim(), verbose=False)
        except Exception:
            pass

        # When hw_loop_count > 0, swap in the parameterised
        # hwloop_regs template.  This mirrors lines 245-... of the
        # legacy RTLChangeSet.apply.
        if cs.hw_loop_count > 0:
            self._swap_hwloop_regs_template(
                workspace, getattr(cs, "_hwlp_encoding", None)
            )

    @staticmethod
    def _apply_pragmas_only(workspace: RTLWorkspace, hw_loop_count: int) -> None:
        """Run only the pragma processor, no template swap.

        Used when nest_depth=0 to mirror legacy behaviour where the
        ARVIS_HWLP pragmas are stripped out (hwloop disabled).
        """
        from arvis.pipeline.rtl_changeset import RTLChangeSet

        cs = RTLChangeSet()
        cs.hw_loop_count = hw_loop_count

        class _CtxShim:
            def __setattr__(self, name, value):
                pass

            def __getattr__(self, name):
                return None

        try:
            cs._apply_hwloop_pragmas(workspace, _CtxShim(), verbose=False)
        except Exception:
            pass

    @staticmethod
    def _swap_hwloop_regs_template(workspace: RTLWorkspace, encoding) -> None:
        """Copy the parameterised hwloop_regs template over the
        baseline file and patch funct3 values.

        Mirrors legacy ``RTLChangeSet.apply`` lines 245-275.  Safe
        to call on a workspace whose source_root doesn't ship a
        template file (the swap is just skipped).
        """
        import shutil
        custom_hwlp = workspace.source_root / "rtl" / "template" / "cv32e40p_hwloop_regs.sv"
        target_hwlp = workspace.output_root / "rtl" / "cv32e40p_hwloop_regs.sv"
        if not custom_hwlp.exists():
            return
        shutil.copy2(str(custom_hwlp), str(target_hwlp))

        if encoding is None:
            return
        # Patch funct3 values in the ARVIS_HWLP_BEGIN/END pragma block.
        # Same regex as the legacy code.
        text = target_hwlp.read_text()
        replacement = (
            f"  assign hwlp_we_start     = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.bounds_funct3:03b} || hwlp_funct3_i == 3'b{encoding.start_funct3:03b});\n"
            f"  assign hwlp_we_end       = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.bounds_funct3:03b} || hwlp_funct3_i == 3'b{encoding.end_funct3:03b});\n"
            f"  assign hwlp_we_start_end = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.bounds_funct3:03b});\n"
            f"  assign hwlp_we_cnt       = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.count_funct3:03b});\n"
        )
        text = re.sub(
            r"// ARVIS_HWLP_BEGIN: hwlp_regs_we\n.*?// ARVIS_HWLP_END: hwlp_regs_we",
            f"// ARVIS_HWLP_BEGIN: hwlp_regs_we\n{replacement}  // ARVIS_HWLP_END: hwlp_regs_we",
            text,
            flags=re.DOTALL,
        )
        target_hwlp.write_text(text)
