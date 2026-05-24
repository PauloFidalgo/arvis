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

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arvis.core.rtl_patch import RTLPatch, RTLWorkspace

if TYPE_CHECKING:
    from arvis.codegen.rtl.rtl_pruning import PruneConfig
    from arvis.core.strategy import (
        FusionDecision,
        LoopDecision,
        PruneDecision,
        WidthDecision,
    )


logger = logging.getLogger(__name__)


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

    decision: WidthDecision

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
                logger.warning(
                    "Soft-skip: apply_pc_width.patch_rtl_dir raised "
                    "for pc_width=%d; leaving PC width unchanged",
                    pc,
                    exc_info=True,
                )

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

        # ── Prefetch FIFO depth: localparam rewrite in
        #    cv32e40p_prefetch_buffer.sv.  Mirrors legacy
        #    RTLChangeSet.apply lines 169-181.
        if self.decision.fifo_depth > 0:
            pfb = rtl_dir / "cv32e40p_prefetch_buffer.sv"
            if pfb.exists():
                text = pfb.read_text()
                new_text = re.sub(
                    r"localparam FIFO_DEPTH\s*=\s*\d+;",
                    (
                        f"localparam FIFO_DEPTH                     = "
                        f"{self.decision.fifo_depth}; "
                        f"// ARVIS: auto-tuned from bottleneck analysis"
                    ),
                    text,
                )
                if new_text != text:
                    pfb.write_text(new_text)

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

    decision: PruneDecision

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
        # Late imports keep the cv32e40p target import-light.
        from arvis.codegen.rtl.base import RTLWorkspace as LegacyWorkspace
        from arvis.codegen.rtl.rtl_pruning import RTLPruner

        # The legacy RTLPruner takes its own RTLWorkspace shape
        # (codegen.rtl.base.RTLWorkspace).  Both have an
        # ``output_root`` attribute so we can wrap one in the
        # other.
        legacy_ws = LegacyWorkspace(
            source_root=str(workspace.source_root),
            output_root=str(workspace.output_root),
        )

        # Reconstruct PruneConfig from typed Decision fields.
        config = self._build_prune_config()

        try:
            # Step 1: Pruning parameter gating + opcode case
            # pruning.  Mirrors legacy
            # RTLChangeSet.apply line 187 (Step 2 in the legacy
            # comments).
            pruner = RTLPruner(legacy_ws)
            pruner.apply(config, verbose=False)

            # Step 2: Generate specialized decoder.  Mirrors legacy
            # line 222 (Step 3).  Note: this REGENERATES decoder.sv
            # from a template, OVERWRITING the opcode-block pruning
            # that pruner.apply did in Step 1.  The legacy code does
            # this in the same order intentionally -- the regen
            # produces a decoder for the workload's actual
            # used_instructions; opcode-group removability is a
            # weaker signal that's superseded by the per-instruction
            # specialization.
            try:
                pruner.generate_specialized_decoder(
                    used_instructions=set(self.decision.used_instructions),
                    custom_instructions=None,
                )
            except Exception:
                logger.warning(
                    "Decoder regeneration failed; proceeding with whatever case-pruning produced",
                    exc_info=True,
                )

            # Step 3: Hwloop pragma processor (with the variant's
            # actual hw_loop_count, stashed in workspace metadata
            # by allocate_workspace_metadata).  Mirrors legacy
            # RTLChangeSet.apply line 244.  Runs AFTER the decoder
            # regen so the regen-introduced markers get stripped at
            # the right count.
            from arvis.targets.cv32e40p.encoding import WORKSPACE_REGISTRY_KEY
            from arvis.targets.cv32e40p.passes import (
                apply_ctrl_pragmas,
                apply_ctrl_pragmas_on_dir,
                apply_dce_cleanup,
                apply_debug_pragmas,
                apply_encoding_optimization,
                apply_hwloop_pragmas,
                apply_irq_pragmas,
                apply_pulp_pragmas,
                apply_pulp_pragmas_on_dir,
            )

            hw_loop_count = workspace.metadata.get("cv32e40p_hw_loop_count", 0)
            registry_meta = workspace.metadata.get(WORKSPACE_REGISTRY_KEY)
            hwlp_encoding = None
            if registry_meta is not None and hw_loop_count > 0:
                try:
                    hwlp_encoding = registry_meta.get_hwloop_encoding()
                except Exception:
                    logger.debug(
                        "Registry lacks an hwloop encoding; falling back to template defaults",
                        exc_info=True,
                    )
                    hwlp_encoding = None

            apply_hwloop_pragmas(
                workspace,
                hw_loop_count=hw_loop_count,
                hwlp_encoding=hwlp_encoding,
            )

            # Step 4: Pragma processors (debug/pulp/irq/ctrl) -- the
            # legacy code runs these whenever has_real_pruning is
            # True.  Order is canonical: debug first, then pulp,
            # irq, ctrl (matches RTLChangeSet.apply lines 305-309).
            #
            # Flag derivation note
            # --------------------
            # ``enable_debug`` here is the CLI-level user flag
            # (analogous to ``cfg.enable_debug``), NOT the
            # analysis-derived ``prune_config.enable_debug`` (which
            # records "ebreak is used, must keep debug").  The
            # workspace stores the CLI value under the well-known
            # key ``cv32e40p_enable_debug``; emit_via_portability
            # populates it from the changeset's owning ``cfg``.
            # When absent (e.g. direct ``Pipeline._emit_variant``
            # invocation in tests) we default to ``True`` -- the
            # canonical "keep debug" stance that matches the test
            # infrastructure's cfg-shim.
            enable_debug = bool(workspace.metadata.get("cv32e40p_enable_debug", True))
            corev_pulp = int(getattr(config, "corev_pulp", 0) or 0)
            enable_interrupts = bool(getattr(config, "enable_interrupts", True))

            apply_debug_pragmas(workspace, enable_debug=enable_debug)
            apply_pulp_pragmas(workspace, corev_pulp=corev_pulp)
            apply_irq_pragmas(workspace, enable_interrupts=enable_interrupts)
            apply_ctrl_pragmas(
                workspace,
                enable_interrupts=enable_interrupts,
                enable_debug=enable_debug,
            )

            # Also process the include directory for pkg-level
            # pragmas.
            include_dir = Path(legacy_ws.output_root) / "rtl" / "include"
            if include_dir.exists():
                apply_pulp_pragmas_on_dir(workspace, include_dir, corev_pulp=corev_pulp)
                apply_ctrl_pragmas_on_dir(
                    workspace,
                    include_dir,
                    enable_interrupts=enable_interrupts,
                    enable_debug=enable_debug,
                )

            # Step 5: Dead code elimination cleanup (after pragmas).
            apply_dce_cleanup(workspace)

            # Step 6: Encoding optimization (enum width reduction).
            apply_encoding_optimization(
                workspace,
                removable_mul_modes=config.removable_mul_modes,
                verbose=False,
            )
        except Exception:
            logger.warning(
                "Soft-skip: PrunePatch render error; the variant's "
                "RTL is left in whatever partial state was reached. "
                "This usually means the template tree lacks expected "
                "pragma markers (common when porting to a "
                "non-cv32e40p fork).",
                exc_info=True,
            )

    def _build_prune_config(self) -> PruneConfig:
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

    decision: FusionDecision

    @property
    def label(self) -> str:
        n = len(self.decision.fused_ops)
        return f"FusionPatch({n} ops)"

    def apply(self, workspace: RTLWorkspace) -> None:
        if not self.decision.fused_ops:
            # No fusions -- nothing to patch.  Workspace untouched.
            return

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
        from arvis.targets.cv32e40p.passes import apply_fusion_patches

        registry = workspace.metadata.get(WORKSPACE_REGISTRY_KEY)
        hw_loop_count = (
            len(getattr(registry, "hwloop_instructions", [])) if registry is not None else 0
        )

        try:
            ok = apply_fusion_patches(
                workspace,
                fused_operations=list(self.decision.fused_ops),
                registry=registry,
                hw_loop_count=hw_loop_count,
            )
            if ok:
                # Signal to LoopPatch that fusion patches have run;
                # it will skip its own hwloop decoder injection
                # because apply_fusion_patches already did it.
                workspace.metadata["cv32e40p_fusion_patch_ran"] = True
        except Exception:
            logger.warning("Soft-skip: FusionPatch render error", exc_info=True)


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

    decision: LoopDecision

    @property
    def label(self) -> str:
        d = self.decision
        if d.nest_depth == 0:
            return "LoopPatch(noop)"
        return f"LoopPatch(nest={d.nest_depth}, cnt={d.counter_width}b, addr={d.addr_width}b)"

    def apply(self, workspace: RTLWorkspace) -> None:
        # Hwloop pragma processing was already done by
        # CV32E40P.allocate_workspace_metadata BEFORE any patches
        # ran (with the variant's actual nest_depth).  This patch
        # only does the template swap + parameter rewrites + hwloop
        # decoder-case injection, all gated on nest_depth > 0.
        if self.decision.nest_depth == 0:
            return

        # Read the encoding registry that allocate_workspace_metadata
        # populated.
        from arvis.targets.cv32e40p.encoding import WORKSPACE_REGISTRY_KEY

        registry = workspace.metadata.get(WORKSPACE_REGISTRY_KEY)
        encoding = None
        if registry is not None:
            try:
                encoding = registry.get_hwloop_encoding()
            except Exception:
                encoding = None

        # Swap in the parameterised hwloop_regs template (mirrors
        # legacy RTLChangeSet.apply lines 245-...).
        self._swap_hwloop_regs_template(workspace, encoding)

        # Rewrite HW_LOOP, CNT_WIDTH, HWLP_ADDR_WIDTH parameters
        # across every .sv file in the workspace (including the
        # testbench).  Mirrors legacy RTLChangeSet.apply lines
        # 285-303.  These are the loop's own parameters; the
        # width strategy still owns PC_WIDTH but the hwloop-
        # specific widths belong here.
        self._rewrite_loop_parameters(
            workspace,
            hw_loop=self.decision.nest_depth,
            cnt_width=self.decision.counter_width,
            addr_width=self.decision.addr_width,
        )

        # Inject hwloop decoder cases into OPCODE_CUSTOM_0 (and
        # potentially CUSTOM_1 etc. for nest_depth > 2).  Mirrors
        # legacy ``RTLChangeSet.apply`` line 322-326:
        #
        #     has_hwlp = self.hw_loop_count > 0 and hasattr(self, '_custom_registry')
        #     if self.fused_operations or has_hwlp:
        #         self._apply_fusion_patches(ws, ctx)
        #
        # That call site bundles fusion AND hwloop decoder
        # emission.  When the variant has FusionDecision (and
        # FusionPatch ran before this), the fusion patcher
        # already injected hwloop entries -- so we only need to
        # do it here if FusionPatch wasn't going to run.  We
        # detect that by checking workspace.metadata for a flag
        # FusionPatch sets.
        if not workspace.metadata.get("cv32e40p_fusion_patch_ran"):
            self._inject_hwloop_decoder_cases(workspace, registry)

    @staticmethod
    def _inject_hwloop_decoder_cases(workspace: RTLWorkspace, registry: Any) -> None:
        """Inject hwloop OPCODE_CUSTOM_* decoder cases via the
        canonical fusion pass with empty ``fused_operations``.

        Mirrors the legacy RTLChangeSet.apply branch where
        ``has_hwlp`` is True but ``fused_operations`` is empty.
        Delegates to :func:`targets.cv32e40p.passes.apply_fusion_patches`,
        which is the single source of truth for fusion-style
        patching across both the legacy and portable code paths.
        """
        if registry is None:
            return

        from arvis.targets.cv32e40p.passes import apply_fusion_patches

        try:
            hwlp_encoding = registry.get_hwloop_encoding()
        except Exception:
            hwlp_encoding = None

        try:
            apply_fusion_patches(
                workspace,
                fused_operations=(),  # explicit: no fused ops
                registry=registry,
                hwlp_encoding=hwlp_encoding,
                hw_loop_count=len(getattr(registry, "hwloop_instructions", [])),
            )
        except Exception:
            logger.warning(
                "Soft-skip: hwloop decoder injection failed; the "
                "template tree probably lacks ARVIS_FUSED_BEGIN "
                "markers (cv32e40p portability concern).",
                exc_info=True,
            )

    @staticmethod
    def _rewrite_loop_parameters(
        workspace: RTLWorkspace,
        *,
        hw_loop: int,
        cnt_width: int,
        addr_width: int,
    ) -> None:
        """Rewrite HW_LOOP / CNT_WIDTH / HWLP_ADDR_WIDTH parameters
        across every ``.sv`` file in the workspace.

        Mirrors the legacy RTLChangeSet.apply behaviour.  The
        ``parameter HW_LOOP`` rewrite always runs when nest_depth>0;
        CNT_WIDTH and HWLP_ADDR_WIDTH only when they're below the
        no-narrowing default of 32 (otherwise the rewrite is a
        no-op, but writing identical text would still touch file
        timestamps).
        """
        import re as _re

        for sv_file in workspace.output_root.rglob("*.sv"):
            text = sv_file.read_text()
            new_text = _re.sub(
                r"parameter\s+HW_LOOP\s*=\s*\d+",
                f"parameter HW_LOOP = {hw_loop}",
                text,
            )
            if cnt_width < 32:
                new_text = _re.sub(
                    r"parameter\s+CNT_WIDTH\s*=\s*\d+",
                    f"parameter CNT_WIDTH = {cnt_width}",
                    new_text,
                )
            if addr_width < 32:
                new_text = _re.sub(
                    r"parameter\s+HWLP_ADDR_WIDTH\s*=\s*\d+",
                    f"parameter HWLP_ADDR_WIDTH = {addr_width}",
                    new_text,
                )
            if new_text != text:
                sv_file.write_text(new_text)

    @staticmethod
    def _swap_hwloop_regs_template(workspace: RTLWorkspace, encoding: Any) -> None:
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
        # Same regex as the legacy code.  The SystemVerilog
        # template lines are intentionally long; we suppress
        # E501 for legibility.
        text = target_hwlp.read_text()
        replacement = (
            f"  assign hwlp_we_start     = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.bounds_funct3:03b} || hwlp_funct3_i == 3'b{encoding.start_funct3:03b});\n"  # noqa: E501
            f"  assign hwlp_we_end       = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.bounds_funct3:03b} || hwlp_funct3_i == 3'b{encoding.end_funct3:03b});\n"  # noqa: E501
            f"  assign hwlp_we_start_end = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.bounds_funct3:03b});\n"  # noqa: E501
            f"  assign hwlp_we_cnt       = hwlp_we_i && (hwlp_funct3_i == 3'b{encoding.count_funct3:03b});\n"  # noqa: E501
        )
        text = re.sub(
            r"// ARVIS_HWLP_BEGIN: hwlp_regs_we\n.*?// ARVIS_HWLP_END: hwlp_regs_we",
            f"// ARVIS_HWLP_BEGIN: hwlp_regs_we\n{replacement}  // ARVIS_HWLP_END: hwlp_regs_we",
            text,
            flags=re.DOTALL,
        )
        target_hwlp.write_text(text)
