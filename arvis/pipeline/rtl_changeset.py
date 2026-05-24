"""
RTL Change Set — single atomic application of all RTL modifications.

Collects pruning decisions and fusion operations from independent
pipeline phases, resolves conflicts, then applies everything in one
shot to a fresh copy of the original RTL tree.

Order of application:
  1. Copy original RTL tree to output directory
  2. Apply parameter gating (generate-if blocks, case pruning)
  3. Generate specialized decoder (with fused instruction support)
  4. Insert fusion pragma patches (pkg enums, decoder cases, ALU mux)
  5. Post-fusion fixes (fused_imm port, SVA assertion, ALU_OP_WIDTH)

This eliminates the ordering bugs where pruning would overwrite fusion
patches or fusion would patch stale (non-pruned) RTL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set

if TYPE_CHECKING:
    from arvis.codegen.rtl.base import RTLWorkspace
    from arvis.codegen.rtl.isa_fusion.alu_single_cycle import FusedOperation
    from arvis.codegen.rtl.rtl_pruning import PruneConfig
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


@dataclass
class RTLChangeSet:
    """Accumulates all RTL changes and applies them atomically.

    Pipeline phases populate this object without touching files:
      - Pruning phase sets ``prune_config`` and ``used_instructions``
      - Fusion phase sets ``fused_operations``

    Then ``apply()`` does the single atomic write.
    """

    # ── From pruning phase ──
    prune_config: Optional["PruneConfig"] = None
    used_instructions: Set[str] = field(default_factory=set)

    # ── From fusion phase ──
    fused_operations: List["FusedOperation"] = field(default_factory=list)

    # ── From HW loop phase ──
    hw_loop_count: int = 0  # 0=disabled, 2-8=number of nested loop levels
    hw_loop_cnt_width: int = 32  # counter register width (auto-tuned from benchmark)
    hw_loop_addr_width: int = 32  # LP_start/end/last register width (auto-tuned to fit binary text)

    # ── From PC narrowing analysis ──
    pc_width: int = 0  # 0=disabled (no narrowing), 8-32=narrow main PC pipeline to N bits

    # ── From bottleneck analysis ──
    prefetch_fifo_depth: int = 0  # 0=don't change, 2-8=set FIFO_DEPTH

    # ── Resolved state (populated by resolve()) ──
    _resolved: bool = False

    def add_prune_config(self, config: "PruneConfig", used_instructions: Set[str]) -> None:
        """Register pruning decisions (no file I/O)."""
        self.prune_config = config
        self.used_instructions = set(used_instructions)
        self._resolved = False

    def add_fused_operations(self, ops: List["FusedOperation"]) -> None:
        """Register fused operations to insert into RTL (no file I/O)."""
        self.fused_operations = list(ops)
        self._resolved = False

    def resolve(self) -> None:
        """Resolve conflicts between pruning and fusion.

        Key conflict: pruning builds a used_instructions set that
        determines the specialized decoder. Fused instructions use
        CUSTOM_0/CUSTOM_2 opcode spaces that must NOT be pruned.
        Additionally, R4-type fused instructions need the 3rd register
        file read port (regfile_rd_c), which pruning might disable.

        This method merges the requirements so both phases are consistent.
        """
        if self._resolved:
            return

        if self.prune_config is None:
            self._resolved = True
            return

        # ── 1. Ensure CUSTOM opcode groups are not pruned ──
        if self.fused_operations:
            # Remove CUSTOM opcodes from removable set
            custom_groups = {
                "OPCODE_CUSTOM_0",
                "OPCODE_CUSTOM_1",
                "OPCODE_CUSTOM_2",
                "OPCODE_CUSTOM_3",
            }
            removed = self.prune_config.removable_opcode_groups & custom_groups
            if removed:
                self.prune_config.removable_opcode_groups -= custom_groups
                print(f"  [changeset] Conflict resolved: kept {sorted(removed)} for fused ops")

        # ── 2. Keep OPCODE_CUSTOM_3 for hwloop setup instructions ──
        if self.hw_loop_count > 0:
            hwloop_groups = {"OPCODE_CUSTOM_3"}
            removed_hwlp = self.prune_config.removable_opcode_groups & hwloop_groups
            if removed_hwlp:
                self.prune_config.removable_opcode_groups -= hwloop_groups
                print("  [changeset] Conflict resolved: kept OPCODE_CUSTOM_3 for hwloop setup")
            # Set HW_LOOP on prune_config so synthesis_parameters() includes it
            self.prune_config.hw_loop = self.hw_loop_count
            # CUSTOM_3 SETUP is R4-type → needs read port C
            if not self.prune_config.enable_regfile_rd_c:
                self.prune_config.enable_regfile_rd_c = True
                print(
                    "  [changeset] Conflict resolved: kept regfile read port C for HWLOOP R4-type"
                )

        # ── 3. Ensure read port C is enabled if any R4-type fusions ──
        has_r4 = any(op.n_inputs >= 3 for op in self.fused_operations)
        if has_r4 and not self.prune_config.enable_regfile_rd_c:
            self.prune_config.enable_regfile_rd_c = True
            print("  [changeset] Conflict resolved: kept regfile read port C for R4-type fusions")

        self._resolved = True

    def apply(self, cfg: "ToolConfig", ctx: "PipelineContext", *, verbose: bool = True) -> None:
        """Apply ALL RTL changes atomically.

        This is the ONLY place in the pipeline that touches RTL files.

        Steps:
          1. Fresh copy of original RTL
          2. Parameter gating (pruner.apply)
          3. Specialized decoder generation (with custom instructions)
          4. Fusion pragma patches (pkg enums + decoder cases + ALU mux)
          5. Post-fusion fixes (fused_imm, SVA, ALU_OP_WIDTH)
        """
        from arvis.cli import print_info, print_success, print_warning
        from arvis.codegen.rtl.base import RTLWorkspace
        from arvis.codegen.rtl.rtl_pruning import RTLPruner

        _print_info = print_info if verbose else (lambda *a, **k: None)
        _print_warning = print_warning if verbose else (lambda *a, **k: None)
        _print_success = print_success if verbose else (lambda *a, **k: None)

        # Ensure conflicts are resolved
        self.resolve()

        # Keep prune_config.hw_loop in sync with hw_loop_count (resolve() only runs once)
        if self.prune_config is not None and self.hw_loop_count > 0:
            self.prune_config.hw_loop = self.hw_loop_count
            self.prune_config.hw_loop_cnt_width = self.hw_loop_cnt_width
            self.prune_config.hw_loop_addr_width = self.hw_loop_addr_width

        # ── Phase 2.8 portability dispatch ──────────────────────
        # When the user opts in (--use-portability flag, or
        # ARVIS_USE_PORTABILITY=1 env var), route this entire
        # apply() to the new portable Pipeline path.  Equivalence
        # with the legacy code is verified for the five standard
        # cv32e40p variants by examples/portability_equivalence.py
        # (174 SystemVerilog files byte-identical for each).
        try:
            from arvis.targets.cv32e40p.portability_shim import (
                emit_via_portability,
                is_enabled as _portability_enabled,
            )

            if _portability_enabled(cfg):
                _print_info(
                    "[portability] routing apply() through "
                    "targets/cv32e40p/portability_shim.emit_via_portability"
                )
                emit_via_portability(self, cfg, ctx, verbose=verbose)
                return
        except ImportError:
            # Portability layer not available -- fall through to
            # legacy.  This keeps the runner usable in stripped-down
            # environments that don't ship the new packages.
            pass

        # ── Step 1: Fresh copy from original RTL ──
        label = getattr(self, '_apply_label', None)
        if label:
            ctx.rtl_output_dir = os.path.join(cfg.output_dir, f"rtl_{label}")
        else:
            ctx.rtl_output_dir = os.path.join(cfg.output_dir, "rtl_modified")
        ws = RTLWorkspace(cfg.rtl_root, ctx.rtl_output_dir)
        ws.copy(verbose=verbose)

        # ── Step 1b: Prefetch buffer tuning ──
        if self.prefetch_fifo_depth > 0:
            import re as _re

            pfb = ws.output_root / "rtl" / "cv32e40p_prefetch_buffer.sv"
            if pfb.exists():
                text = pfb.read_text()
                text = _re.sub(
                    r"localparam FIFO_DEPTH\s*=\s*\d+;",
                    f"localparam FIFO_DEPTH                     = {self.prefetch_fifo_depth}; // ARVIS: auto-tuned from bottleneck analysis",
                    text,
                )
                pfb.write_text(text)
                _print_info(f"Prefetch FIFO depth: {self.prefetch_fifo_depth} (auto-tuned)")

        # ── Step 2: Apply pruning parameter gating ──
        if self.prune_config is not None:
            pruner = RTLPruner(ws)
            ctx.prune_report = pruner.apply(self.prune_config, verbose=verbose)

            n_gates = len(ctx.prune_report.file_stats)
            n_alu_removed = len(self.prune_config.removable_alu_ops)
            n_opcode_removed = len(self.prune_config.removable_opcode_groups)
            _print_info(
                f"Parameter gating: {n_gates} files modified, "
                f"{n_alu_removed} ALU ops removed, "
                f"{n_opcode_removed} opcode groups removed"
            )

            # ── Step 3: Generate specialized decoder ──
            n_insns = len(self.used_instructions)

            # Build unified custom instruction registry
            from arvis.pipeline.custom_insn_registry import build_registry_from_used_instructions
            gcc_result = getattr(ctx, 'gcc_compile_result', None)
            # Only use fused slot offset when we actually have fused operations
            next_slot = gcc_result.next_r4_slot if (gcc_result and self.fused_operations) else 0
            registry = build_registry_from_used_instructions(
                hw_loop_count=self.hw_loop_count,
                next_r4_slot=next_slot,
            )
            ctx._custom_registry = registry
            self._custom_registry = registry

            # Store hwloop encoding for pragma system and assembly patcher
            if self.hw_loop_count > 0:
                enc = registry.get_hwloop_encoding()
                self._hwlp_encoding = enc
                ctx._hwlp_encoding = enc

            # Generate decoder WITHOUT custom instructions
            # (fused + hwloop are injected by _apply_fusion_patches using the registry)
            dec_stats = pruner.generate_specialized_decoder(
                used_instructions=self.used_instructions,
                custom_instructions=None,
            )
            ctx.prune_report.file_stats.append(dec_stats)
            n_hwlp = len(registry.hwloop_instructions) if registry else 0
            tag = f"{n_insns} mnemonics retained"
            if n_hwlp:
                tag += f" + {n_hwlp} hwloop instructions"
            _print_info(f"Specialized decoder: {tag}")

            ctx.verilator_extra_flags = self.prune_config.verilator_flags()
        else:
            _print_warning("No prune config — skipping parameter gating")

        # ── Step 3b: Apply pragma processing (AFTER decoder gen) ──
        # Only apply feature pragmas when we have a real prune config
        # (not the minimal fusion-only config)
        has_real_pruning = (
            self.prune_config is not None and len(self.prune_config.removable_alu_ops) > 0
        )
        self._apply_hwloop_pragmas(ws, ctx, verbose=verbose)

        # Replace hwloop_regs with ARVIS template when HW_LOOP > 0
        if self.hw_loop_count > 0:
            import shutil

            custom_hwlp = Path(cfg.rtl_root) / "rtl" / "template" / "cv32e40p_hwloop_regs.sv"
            target_hwlp = ws.output_root / "rtl" / "cv32e40p_hwloop_regs.sv"
            if custom_hwlp.exists():
                shutil.copy2(str(custom_hwlp), str(target_hwlp))
                # Patch funct3 values to match dynamic encoding
                enc = getattr(self, '_hwlp_encoding', None)
                if enc:
                    # Replace pragma block with correct funct3 values
                    text = target_hwlp.read_text()
                    import re as _re_hwlp
                    replacement = (
                        f"  assign hwlp_we_start     = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.bounds_funct3:03b} || hwlp_funct3_i == 3'b{enc.start_funct3:03b});\n"
                        f"  assign hwlp_we_end       = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.bounds_funct3:03b} || hwlp_funct3_i == 3'b{enc.end_funct3:03b});\n"
                        f"  assign hwlp_we_start_end = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.bounds_funct3:03b});\n"
                        f"  assign hwlp_we_cnt       = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.count_funct3:03b});\n"
                    )
                    text = _re_hwlp.sub(
                        r'// ARVIS_HWLP_BEGIN: hwlp_regs_we\n.*?// ARVIS_HWLP_END: hwlp_regs_we',
                        f'// ARVIS_HWLP_BEGIN: hwlp_regs_we\n{replacement}  // ARVIS_HWLP_END: hwlp_regs_we',
                        text, flags=_re_hwlp.DOTALL
                    )
                    target_hwlp.write_text(text)
                    _print_info(f"HWLOOP regs: bounds=f3={enc.bounds_funct3}, count=f3={enc.count_funct3}, start=f3={enc.start_funct3}, end=f3={enc.end_funct3}")
                    # Verify
                    if f"3'b{enc.bounds_funct3:03b}" not in target_hwlp.read_text():
                        print(f"  ⚠ WARNING: hwloop_regs patching FAILED")
                else:
                    _print_warning("HWLOOP regs: no encoding available, using template defaults")
                _print_info("HWLOOP: replaced hwloop_regs with ARVIS template")

            # Set HW_LOOP and CNT_WIDTH parameters in all .sv files
            import re as _re

            for sv_file in ws.output_root.rglob("*.sv"):
                text = sv_file.read_text()
                new_text = _re.sub(
                    r"parameter\s+HW_LOOP\s*=\s*\d+",
                    f"parameter HW_LOOP = {self.hw_loop_count}",
                    text,
                )
                if self.hw_loop_cnt_width < 32:
                    new_text = _re.sub(
                        r"parameter\s+CNT_WIDTH\s*=\s*\d+",
                        f"parameter CNT_WIDTH = {self.hw_loop_cnt_width}",
                        new_text,
                    )
                if self.hw_loop_addr_width < 32:
                    new_text = _re.sub(
                        r"parameter\s+HWLP_ADDR_WIDTH\s*=\s*\d+",
                        f"parameter HWLP_ADDR_WIDTH = {self.hw_loop_addr_width}",
                        new_text,
                    )
                if new_text != text:
                    sv_file.write_text(new_text)

        if has_real_pruning:
            self._apply_debug_pragmas(ws, cfg, verbose=verbose)
            self._apply_pulp_pragmas(ws, cfg, verbose=verbose)
            self._apply_irq_pragmas(ws, verbose=verbose)
            self._apply_ctrl_pragmas(ws, cfg, verbose=verbose)

            # Also process include directory for pkg pragmas
            include_dir = ws.output_root / "rtl" / "include"
            if include_dir.exists():
                self._apply_pulp_pragmas_on_dir(ws, cfg, include_dir)
                self._apply_ctrl_pragmas_on_dir(ws, cfg, include_dir)

        # ── Step 3d: Dead Code Elimination cleanup (after all pruning) ──
        if has_real_pruning:
            self._apply_dce_cleanup(ws, verbose=verbose)

        # ── Step 4: Apply fusion patches (also injects hwloop decoder entries) ──
        has_hwlp = self.hw_loop_count > 0 and hasattr(self, '_custom_registry')
        if self.fused_operations or has_hwlp:
            self._apply_fusion_patches(ws, ctx)
        else:
            _print_info("No fused operations — skipping fusion patches")

        # ── Step 5: Encoding optimization (after ALL other modifications) ──
        if has_real_pruning:
            self._apply_encoding_optimization(ws, verbose=verbose)

        # ── Step 6: PC pipeline narrowing (last — operates on the final
        #           emitted tree, after pruning/decoder/fusion/encoding) ──
        # apply_pc_width.patch_rtl_dir is idempotent and safe to call on
        # any cv32e40p RTL tree. It adds ``parameter PC_WIDTH = N`` to
        # every relevant module, narrows internal PC signals to
        # ``[PC_WIDTH-1:0]``, and inserts zero-extends/truncations at
        # the OBI/regfile/CSR boundaries. Only runs when explicitly
        # requested (pc_width > 0).
        if self.pc_width > 0 and self.pc_width < 32:
            try:
                from arvis.pipeline.pc_width import patch_rtl_dir as _patch_pc
                rtl_dir = ws.output_root / "rtl"
                _patch_pc(rtl_dir, self.pc_width)
                _print_info(f"PC narrowing: PC_WIDTH = {self.pc_width} bits")
            except Exception as e:
                from arvis.cli import print_warning
                print_warning(f"PC narrowing skipped: {e}")

        n_fused = len(self.fused_operations)
        has_prune = self.prune_config is not None and (
            len(self.prune_config.removable_alu_ops) > 0
            or len(self.prune_config.removable_opcode_groups) > 0
        )
        tag = f"{n_fused} fused ops"
        if self.hw_loop_count > 0:
            tag += f" + hwloop({self.hw_loop_count})"
        if has_prune:
            tag += " + pruning"
        if self.hw_loop_count > 0:
            tag += f" + hwloop({self.hw_loop_count})"

        _print_success(f"RTL changeset applied ({tag})")

    def _apply_hwloop_pragmas(
        self, ws: "RTLWorkspace", ctx: "PipelineContext", *, verbose: bool = True
    ) -> None:
        """Process ARVIS_HWLP pragmas in the copied RTL.

        Thin shim around
        :func:`targets.cv32e40p.passes.apply_hwloop_pragmas`; kept
        for backwards compatibility with downstream call sites that
        still hold an :class:`RTLChangeSet` reference.

        This runs AFTER pruning and decoder generation so that:
          - PULP hwloop artifacts are stripped (hw_loop=0)
          - Our custom hwloop is inserted (hw_loop>0)
          - The decoder already has ARVIS_FUSED pragmas ready
        """
        from arvis.cli import print_info, print_warning
        from arvis.targets.cv32e40p.passes import apply_hwloop_pragmas

        _print_info = print_info if verbose else (lambda *a, **k: None)

        all_stats = apply_hwloop_pragmas(
            ws,
            hw_loop_count=self.hw_loop_count,
            hwlp_encoding=getattr(self, "_hwlp_encoding", None),
            verbose=verbose,
        )

        total_removed = sum(s.removed for s in all_stats)
        total_kept = sum(s.kept for s in all_stats)
        total_replaced = sum(s.replaced for s in all_stats)

        if verbose and (total_removed + total_kept + total_replaced > 0):
            parts = []
            if total_removed:
                parts.append(f"{total_removed} removed")
            if total_kept:
                parts.append(f"{total_kept} kept")
            if total_replaced:
                parts.append(f"{total_replaced} replaced")
            _print_info(
                f"HWLOOP pragmas: {', '.join(parts)} (HW_LOOP={self.hw_loop_count})"
            )

        for s in all_stats:
            if s.unknown:
                print_warning(f"Unknown HWLP pragmas in {s.file}: {s.unknown}")

    def _apply_feature_pragmas(
        self,
        ws: "RTLWorkspace",
        feature: str,
        enabled: bool,
        level: int,
        generators: dict,
        label: str,
        *,
        extra_dirs: list | None = None,
        verbose: bool = True,
    ) -> None:
        """Process ARVIS pragmas for a single feature (DBG, PULP, IRQ, etc.).

        Thin shim around
        :func:`targets.cv32e40p.passes.apply_feature_pragmas`.
        """
        from arvis.cli import print_info
        from arvis.targets.cv32e40p.passes import apply_feature_pragmas

        _print_info = print_info if verbose else (lambda *a, **k: None)

        all_stats = apply_feature_pragmas(
            ws,
            feature=feature,
            enabled=enabled,
            level=level,
            generators=generators,
            extra_dirs=extra_dirs,
        )

        total_removed = sum(s.removed for s in all_stats)
        total_kept = sum(s.kept for s in all_stats)
        total_replaced = sum(s.replaced for s in all_stats)

        if total_removed + total_kept + total_replaced > 0:
            parts = []
            if total_removed:
                parts.append(f"{total_removed} removed")
            if total_kept:
                parts.append(f"{total_kept} kept")
            if total_replaced:
                parts.append(f"{total_replaced} replaced")
            mode = "KEEP" if enabled else "PRUNE"
            _print_info(f"{feature} pragmas: {', '.join(parts)} ({label}={mode})")

    def _apply_debug_pragmas(
        self, ws: "RTLWorkspace", cfg: "ToolConfig", *, verbose: bool = True
    ) -> None:
        from arvis.codegen.rtl.debug_pragma import DBG_GENERATORS

        self._apply_feature_pragmas(
            ws,
            "DBG",
            cfg.enable_debug,
            1 if cfg.enable_debug else 0,
            DBG_GENERATORS,
            "debug",
            extra_dirs=["example_tb/core", "example_tb/core/verilator"],
            verbose=verbose,
        )

    def _apply_pulp_pragmas(
        self, ws: "RTLWorkspace", cfg: "ToolConfig", *, verbose: bool = True
    ) -> None:
        from arvis.codegen.rtl.pulp_pragma import PULP_GENERATORS

        corev_pulp = self.prune_config.corev_pulp if self.prune_config else 0
        self._apply_feature_pragmas(
            ws,
            "PULP",
            corev_pulp > 0,
            corev_pulp,
            PULP_GENERATORS,
            "COREV_PULP",
            verbose=verbose,
        )

    def _apply_irq_pragmas(self, ws: "RTLWorkspace", *, verbose: bool = True) -> None:
        from arvis.codegen.rtl.irq_pragma import IRQ_GENERATORS

        enable_irq = self.prune_config.enable_interrupts if self.prune_config else True
        self._apply_feature_pragmas(
            ws,
            "IRQ",
            enable_irq,
            1 if enable_irq else 0,
            IRQ_GENERATORS,
            "interrupts",
            extra_dirs=["example_tb/core", "example_tb/core/verilator"],
            verbose=verbose,
        )

    def _apply_ctrl_pragmas(
        self, ws: "RTLWorkspace", cfg: "ToolConfig", *, verbose: bool = True
    ) -> None:
        """Process ARVIS_CTRL pragmas in the controller.

        Thin shim around :func:`targets.cv32e40p.passes.apply_ctrl_pragmas`.
        """
        from arvis.cli import print_info
        from arvis.targets.cv32e40p.passes import apply_ctrl_pragmas

        _print_info = print_info if verbose else (lambda *a, **k: None)

        enable_irq = (
            self.prune_config.enable_interrupts
            if self.prune_config is not None
            else True
        )
        enable_dbg = bool(getattr(cfg, "enable_debug", False))

        n = apply_ctrl_pragmas(
            ws,
            enable_interrupts=enable_irq,
            enable_debug=enable_dbg,
        )

        if n > 0:
            irq_mode = "KEEP" if enable_irq else "PRUNE"
            dbg_mode = "KEEP" if enable_dbg else "PRUNE"
            _print_info(
                f"CTRL pragmas: {n} processed (IRQ={irq_mode}, DBG={dbg_mode})"
            )

    def _apply_pulp_pragmas_on_dir(
        self, ws: "RTLWorkspace", cfg: "ToolConfig", target_dir: Path
    ) -> None:
        """Process ARVIS_PULP pragmas on a specific directory (e.g. include/).

        Thin shim around :func:`targets.cv32e40p.passes.apply_pulp_pragmas_on_dir`.
        """
        from arvis.targets.cv32e40p.passes import apply_pulp_pragmas_on_dir

        corev_pulp = (
            self.prune_config.corev_pulp if self.prune_config is not None else 0
        )
        apply_pulp_pragmas_on_dir(ws, target_dir, corev_pulp=corev_pulp)

    def _apply_ctrl_pragmas_on_dir(
        self, ws: "RTLWorkspace", cfg: "ToolConfig", target_dir: Path
    ) -> None:
        """Process ARVIS_CTRL pragmas on a specific directory (e.g. include/).

        Thin shim around :func:`targets.cv32e40p.passes.apply_ctrl_pragmas_on_dir`.
        """
        from arvis.targets.cv32e40p.passes import apply_ctrl_pragmas_on_dir

        enable_irq = (
            self.prune_config.enable_interrupts
            if self.prune_config is not None
            else True
        )
        enable_dbg = bool(getattr(cfg, "enable_debug", False))
        apply_ctrl_pragmas_on_dir(
            ws,
            target_dir,
            enable_interrupts=enable_irq,
            enable_debug=enable_dbg,
        )

    def _apply_dce_cleanup(self, ws: "RTLWorkspace", *, verbose: bool = True) -> None:
        """Run dead code elimination cleanup on processed RTL.

        Thin shim around :func:`targets.cv32e40p.passes.apply_dce_cleanup`.
        """
        from arvis.cli import print_info
        from arvis.targets.cv32e40p.passes import apply_dce_cleanup

        _print_info = print_info if verbose else (lambda *a, **k: None)

        all_stats = apply_dce_cleanup(ws)

        total_signals = sum(len(s.signals_removed) for s in all_stats)
        total_lines = sum(s.lines_removed for s in all_stats)

        if total_signals > 0:
            _print_info(
                f"DCE cleanup: removed {total_signals} dead signals, "
                f"{total_lines} lines across {len(all_stats)} files"
            )
            for s in all_stats:
                if s.signals_removed:
                    _print_info(
                        f"  {s.file}: {len(s.signals_removed)} signals "
                        f"({s.iterations} iterations)"
                    )

    def _apply_fusion_patches(self, ws: "RTLWorkspace", ctx: "PipelineContext") -> None:
        """Insert fused instruction RTL into the (already pruned) workspace.

        Thin shim around
        :func:`targets.cv32e40p.passes.apply_fusion_patches`; kept
        for backwards compatibility with downstream call sites that
        still pass an :class:`RTLChangeSet`.

        Patches between ARVIS_FUSED_BEGIN/END pragmas in:
          - cv32e40p_pkg.sv (ALU enum entries)
          - cv32e40p_decoder.sv (CUSTOM opcode case blocks)
          - cv32e40p_alu.sv (result_mux expressions)

        Then applies post-fusion fixes:
          - fused_imm_i port (for variable-immediate patterns)
          - SVA assertion disable (CUSTOM_0 legality)
          - ALU_OP_WIDTH expansion (if enum values > 127)
        """
        from arvis.targets.cv32e40p.passes import apply_fusion_patches

        def _on_applied():
            ctx.fusion_rtl_applied = True

        apply_fusion_patches(
            ws,
            fused_operations=self.fused_operations,
            registry=getattr(self, "_custom_registry", None),
            hwlp_encoding=getattr(self, "_hwlp_encoding", None),
            hw_loop_count=self.hw_loop_count,
            fusion_applied_callback=_on_applied,
        )

    def _apply_encoding_optimization(self, ws: "RTLWorkspace", *, verbose: bool = True) -> None:
        """Optimize enum encodings by removing unused members and reducing bit widths.

        Thin shim around
        :func:`targets.cv32e40p.passes.apply_encoding_optimization`.

        This runs AFTER all pruning, pragma processing, DCE, and fusion so
        it can scan the final RTL to determine which enum members survived.

        Targets:
          - alu_opcode_e: remove unused ALU ops, reduce ALU_OP_WIDTH
          - mul_opcode_e: remove unused MUL modes, reduce MUL_OP_WIDTH
          - ctrl_state_e: remove unused FSM states (debug, hwloop), reduce width
          - mult_state_e: remove unused multiplier states, reduce width
        """
        from arvis.cli import print_info
        from arvis.targets.cv32e40p.passes import apply_encoding_optimization

        _print_info = print_info if verbose else (lambda *a, **k: None)

        if verbose:
            print("\n  Encoding optimization (enum width reduction):")

        removable_mul_modes = (
            self.prune_config.removable_mul_modes
            if self.prune_config is not None
            else ()
        )

        result = apply_encoding_optimization(
            ws,
            removable_mul_modes=removable_mul_modes,
            verbose=verbose,
        )
        if result is None:
            return

        if result.total_bits_saved > 0:
            _print_info(
                f"Encoding optimization: {result.total_bits_saved} bits saved "
                f"across {result.files_modified} files"
            )
            for r in result.reencodings:
                if not r.skipped and r.changes:
                    _print_info(
                        f"  {r.enum_name}: {r.old_width}b -> {r.new_width}b "
                        f"({len(r.kept_members)} kept, "
                        f"{len(r.removed_members)} removed)"
                    )
        else:
            _print_info("Encoding optimization: no reductions possible")

    @staticmethod
    def _clear_fused_pragmas(rtl_dir: Path) -> None:
        """Clear content between ARVIS_FUSED_BEGIN/END pragmas."""
        from arvis.targets.cv32e40p.passes import clear_fused_pragmas

        clear_fused_pragmas(rtl_dir)


def _hwloop_instruction_entries(hw_loop: int, next_r4_slot: int = 0):
    """Create InstructionEntry objects for hwloop instructions.
    
    Places hwloop instructions at the next available R4 encoding slots
    after all fused instructions, using the same opcode space.
    """
    from arvis.analysis.isa_db import InstructionEntry
    from arvis.codegen.gcc.peephole_gen import _r4_enc

    slot = next_r4_slot
    
    def _next():
        nonlocal slot
        opcode, funct3, funct2 = _r4_enc(slot)
        slot += 1
        return opcode, funct3

    bounds_opc, bounds_f3 = _next()
    count_opc, count_f3 = _next()

    entries = [
        InstructionEntry(
            name="hwloop.bounds",
            opcode=bounds_opc,
            funct3=bounds_f3,
            signals={"hwlp_we": "1'b1"},
        ),
        InstructionEntry(
            name="hwloop.count",
            opcode=count_opc,
            funct3=count_f3,
            signals={"hwlp_we": "1'b1", "regc_used_o": "1'b1", "regc_mux_o": "REGC_S4"},
        ),
    ]
    if hw_loop > 2:
        start_opc, start_f3 = _next()
        end_opc, end_f3 = _next()
        entries.extend([
            InstructionEntry(
                name="hwloop.start",
                opcode=start_opc,
                funct3=start_f3,
                signals={"hwlp_we": "1'b1"},
            ),
            InstructionEntry(
                name="hwloop.end",
                opcode=end_opc,
                funct3=end_f3,
                signals={"hwlp_we": "1'b1"},
            ),
        ])
    return entries, slot
