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
                print("  [changeset] Conflict resolved: kept regfile read port C for HWLOOP R4-type")

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

        # ── Step 1: Fresh copy from original RTL ──
        label = getattr(self, "_apply_label", None)
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
                    f"localparam FIFO_DEPTH                     = {self.prefetch_fifo_depth}; // ARVIS: auto-tuned from bottleneck analysis",  # noqa: E501
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

            gcc_result = getattr(ctx, "gcc_compile_result", None)
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
        has_real_pruning = self.prune_config is not None and len(self.prune_config.removable_alu_ops) > 0
        self._apply_hwloop_pragmas(ws, ctx, verbose=verbose)

        # Replace hwloop_regs with ARVIS template when HW_LOOP > 0
        if self.hw_loop_count > 0:
            import shutil

            custom_hwlp = Path(cfg.rtl_root) / "rtl" / "template" / "cv32e40p_hwloop_regs.sv"
            target_hwlp = ws.output_root / "rtl" / "cv32e40p_hwloop_regs.sv"
            if custom_hwlp.exists():
                shutil.copy2(str(custom_hwlp), str(target_hwlp))
                # Patch funct3 values to match dynamic encoding
                enc = getattr(self, "_hwlp_encoding", None)
                if enc:
                    # Replace pragma block with correct funct3 values
                    text = target_hwlp.read_text()
                    import re as _re_hwlp

                    replacement = (
                        f"  assign hwlp_we_start     = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.bounds_funct3:03b} || hwlp_funct3_i == 3'b{enc.start_funct3:03b});\n"  # noqa: E501
                        f"  assign hwlp_we_end       = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.bounds_funct3:03b} || hwlp_funct3_i == 3'b{enc.end_funct3:03b});\n"  # noqa: E501
                        f"  assign hwlp_we_start_end = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.bounds_funct3:03b});\n"
                        f"  assign hwlp_we_cnt       = hwlp_we_i && (hwlp_funct3_i == 3'b{enc.count_funct3:03b});\n"
                    )
                    text = _re_hwlp.sub(
                        r"// ARVIS_HWLP_BEGIN: hwlp_regs_we\n.*?// ARVIS_HWLP_END: hwlp_regs_we",
                        f"// ARVIS_HWLP_BEGIN: hwlp_regs_we\n{replacement}  // ARVIS_HWLP_END: hwlp_regs_we",
                        text,
                        flags=_re_hwlp.DOTALL,
                    )
                    target_hwlp.write_text(text)
                    _print_info(
                        f"HWLOOP regs: bounds=f3={enc.bounds_funct3}, count=f3={enc.count_funct3}, start=f3={enc.start_funct3}, end=f3={enc.end_funct3}"  # noqa: E501
                    )
                    # Verify
                    if f"3'b{enc.bounds_funct3:03b}" not in target_hwlp.read_text():
                        print("  ⚠ WARNING: hwloop_regs patching FAILED")
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
        has_hwlp = self.hw_loop_count > 0 and hasattr(self, "_custom_registry")
        if self.fused_operations or has_hwlp:
            self._apply_fusion_patches(ws, ctx)
        else:
            _print_info("No fused operations — skipping fusion patches")

        # ── Step 5: Encoding optimization (after ALL other modifications) ──
        if has_real_pruning:
            self._apply_encoding_optimization(ws, verbose=verbose)

        n_fused = len(self.fused_operations)
        has_prune = self.prune_config is not None and (
            len(self.prune_config.removable_alu_ops) > 0 or len(self.prune_config.removable_opcode_groups) > 0
        )
        tag = f"{n_fused} fused ops"
        if self.hw_loop_count > 0:
            tag += f" + hwloop({self.hw_loop_count})"
        if has_prune:
            tag += " + pruning"
        if self.hw_loop_count > 0:
            tag += f" + hwloop({self.hw_loop_count})"

        _print_success(f"RTL changeset applied ({tag})")

    def _apply_hwloop_pragmas(self, ws: "RTLWorkspace", ctx: "PipelineContext", *, verbose: bool = True) -> None:
        """Process ARVIS_HWLP pragmas in the copied RTL.

        This runs AFTER pruning and decoder generation so that:
          - PULP hwloop artifacts are stripped (hw_loop=0)
          - Our custom hwloop is inserted (hw_loop>0)
          - The decoder already has ARVIS_FUSED pragmas ready
        """
        from arvis.cli import print_info
        from arvis.codegen.rtl.hwloop_pragma import HWLoopPragmaProcessor

        _print_info = print_info if verbose else (lambda *a, **k: None)

        rtl_dir = ws.output_root / "rtl"
        hw_loop = self.hw_loop_count

        proc = HWLoopPragmaProcessor(hw_loop=hw_loop, hwlp_encoding=getattr(self, "_hwlp_encoding", None))
        all_stats = proc.process_dir(rtl_dir)

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
            _print_info(f"HWLOOP pragmas: {', '.join(parts)} (HW_LOOP={hw_loop})")

        # Log any unknown pragmas
        for s in all_stats:
            if s.unknown:
                from arvis.cli import print_warning

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
        """Process ARVIS pragmas for a single feature (DBG, PULP, IRQ, etc.)."""
        from arvis.cli import print_info
        from arvis.codegen.rtl.rtl_pragma import RTLPragmaProcessor

        _print_info = print_info if verbose else (lambda *a, **k: None)

        rtl_dir = ws.output_root / "rtl"
        proc = RTLPragmaProcessor()
        proc.add_feature(feature, enabled=enabled, level=level, generators=generators)
        all_stats = proc.process_dir(rtl_dir)

        for sub in extra_dirs or []:
            d = ws.output_root / sub
            if d.exists():
                all_stats.extend(proc.process_dir(d))

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

    def _apply_debug_pragmas(self, ws: "RTLWorkspace", cfg: "ToolConfig", *, verbose: bool = True) -> None:
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

    def _apply_pulp_pragmas(self, ws: "RTLWorkspace", cfg: "ToolConfig", *, verbose: bool = True) -> None:
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

    def _apply_ctrl_pragmas(self, ws: "RTLWorkspace", cfg: "ToolConfig", *, verbose: bool = True) -> None:
        """Process ARVIS_CTRL pragmas in the controller.

        These are unified pragmas that receive BOTH irq and dbg flags
        to generate the correct RTL for the combination.
        """
        import re

        from arvis.cli import print_info

        _print_info = print_info if verbose else (lambda *a, **k: None)
        from arvis.codegen.rtl.ctrl_pragma import CTRL_GENERATORS

        rtl_dir = ws.output_root / "rtl"
        ctrl_path = rtl_dir / "cv32e40p_controller.sv"
        if not ctrl_path.exists():
            return

        enable_irq = True
        enable_dbg = cfg.enable_debug
        if self.prune_config is not None:
            enable_irq = self.prune_config.enable_interrupts

        text = ctrl_path.read_text()
        total_processed = 0

        # Process ARVIS_CTRL_BEGIN/END blocks
        pattern = re.compile(
            r"(\s*)// ARVIS_CTRL_BEGIN: (\w+)\n(.*?)// ARVIS_CTRL_END: \2",
            re.DOTALL,
        )

        def replacer(match):
            nonlocal total_processed
            indent = match.group(1)
            name = match.group(2)
            match.group(3)

            gen = CTRL_GENERATORS.get(name)
            if gen is None:
                return match.group(0)  # Unknown, keep

            result = gen(enable_irq, enable_dbg, indent)
            total_processed += 1

            if result is None:
                return match.group(0)  # Keep original
            if result == "":
                return ""  # Remove entire block
            # Replace with generated content
            return f"{indent}{result.rstrip()}\n"

        new_text = pattern.sub(replacer, text)

        if new_text != text:
            ctrl_path.write_text(new_text)
            irq_mode = "KEEP" if enable_irq else "PRUNE"
            dbg_mode = "KEEP" if enable_dbg else "PRUNE"
            _print_info(f"CTRL pragmas: {total_processed} processed (IRQ={irq_mode}, DBG={dbg_mode})")

    def _apply_pulp_pragmas_on_dir(self, ws: "RTLWorkspace", cfg: "ToolConfig", target_dir: Path) -> None:
        """Process ARVIS_PULP pragmas on a specific directory (e.g. include/)."""
        from arvis.codegen.rtl.pulp_pragma import PULP_GENERATORS
        from arvis.codegen.rtl.rtl_pragma import RTLPragmaProcessor

        corev_pulp = 0
        if self.prune_config is not None:
            corev_pulp = self.prune_config.corev_pulp

        proc = RTLPragmaProcessor()
        proc.add_feature("PULP", enabled=(corev_pulp > 0), level=corev_pulp, generators=PULP_GENERATORS)
        proc.process_dir(target_dir)

    def _apply_ctrl_pragmas_on_dir(self, ws: "RTLWorkspace", cfg: "ToolConfig", target_dir: Path) -> None:
        """Process ARVIS_CTRL pragmas on a specific directory (e.g. include/).

        Uses the same CTRL generators as the controller, applied to pkg files.
        """
        import re

        from arvis.codegen.rtl.ctrl_pragma import CTRL_GENERATORS

        enable_irq = True
        enable_dbg = cfg.enable_debug
        if self.prune_config is not None:
            enable_irq = self.prune_config.enable_interrupts

        for sv_file in sorted(target_dir.glob("*.sv")):
            text = sv_file.read_text()
            original = text

            pattern = re.compile(
                r"(\s*)// ARVIS_CTRL_BEGIN: (\w+)\n(.*?)// ARVIS_CTRL_END: \2",
                re.DOTALL,
            )

            def replacer(match):
                indent = match.group(1)
                name = match.group(2)
                gen = CTRL_GENERATORS.get(name)
                if gen is None:
                    return match.group(0)
                result = gen(enable_irq, enable_dbg, indent)
                if result is None:
                    return match.group(0)
                if result == "":
                    return ""
                return f"{indent}{result.rstrip()}\n"

            text = pattern.sub(replacer, text)
            if text != original:
                sv_file.write_text(text)

    def _apply_dce_cleanup(self, ws: "RTLWorkspace", *, verbose: bool = True) -> None:
        """Run dead code elimination cleanup on processed RTL.

        After all pruning passes (parameters, pragmas, case removal),
        scan for orphaned signal declarations and assignments that are
        no longer referenced. Remove them iteratively.
        """
        from arvis.cli import print_info

        _print_info = print_info if verbose else (lambda *a, **k: None)

        rtl_dir = ws.output_root / "rtl"

        try:
            from arvis.codegen.rtl.pyslang_dce import run_dce_on_directory

            all_stats = run_dce_on_directory(rtl_dir)

            total_signals = sum(len(s.signals_removed) for s in all_stats)
            total_lines = sum(s.lines_removed for s in all_stats)

            if total_signals > 0:
                _print_info(
                    f"DCE cleanup: removed {total_signals} dead signals, "
                    f"{total_lines} lines across {len(all_stats)} files"
                )
                for s in all_stats:
                    if s.signals_removed:
                        _print_info(f"  {s.file}: {len(s.signals_removed)} signals ({s.iterations} iterations)")
        except Exception as e:
            from arvis.cli import print_warning

            print_warning(f"DCE cleanup skipped: {e}")

    def _apply_fusion_patches(self, ws: "RTLWorkspace", ctx: "PipelineContext") -> None:
        """Insert fused instruction RTL into the (already pruned) workspace.

        Patches between ARVIS_FUSED_BEGIN/END pragmas in:
          - cv32e40p_pkg.sv (ALU enum entries)
          - cv32e40p_decoder.sv (CUSTOM opcode case blocks)
          - cv32e40p_alu.sv (result_mux expressions)

        Then applies post-fusion fixes:
          - fused_imm_i port (for variable-immediate patterns)
          - SVA assertion disable (CUSTOM_0 legality)
          - ALU_OP_WIDTH expansion (if enum values > 127)
        """
        from arvis.codegen.rtl.isa_fusion.alu_single_cycle import RTLGenerator

        rtl_dir = ws.output_root / "rtl"

        # Verify pragma files exist
        pkg_path = rtl_dir / "include" / "cv32e40p_pkg.sv"
        dec_path = rtl_dir / "cv32e40p_decoder.sv"
        alu_path = rtl_dir / "cv32e40p_alu.sv"

        for path, name in [
            (pkg_path, "pkg"),
            (dec_path, "decoder"),
            (alu_path, "alu"),
        ]:
            if not path.exists():
                print(f"  ❌ {name} file not found: {path}")
                return

        # Clear any existing pragma content (from previous runs)
        self._clear_fused_pragmas(rtl_dir)

        # Create generator and register all operations
        gen = RTLGenerator(str(rtl_dir))
        gen.add_existing()  # Parse baseline ALU values (non-fused)

        # Deduplicate ops by name before registering
        seen_names = set()
        for op in self.fused_operations:
            if op.name in seen_names:
                continue
            seen_names.add(op.name)
            gen.fused_ops.append(op)
            gen.allocator.register(op)

        # Write patches between pragmas
        # Inject hwloop decoder entries into the same custom opcode blocks
        registry = getattr(self, "_custom_registry", None)
        if registry:
            gen._hwloop_registry_entries = registry.hwloop_instructions
            if registry.hwloop_instructions:
                from arvis.cli import print_info

                for e in registry.hwloop_instructions:
                    print_info(f"  HWLoop decoder entry: {e.name} → 0x{e.opcode:02x} f3={e.funct3} f2={e.funct2}")
        gen.write()

        # ── Post-fusion fix 1: fused_imm_i port ──
        from arvis.codegen.rtl.fused_imm_patcher import add_fused_imm_port, needs_fused_imm

        # Check both op.sv_expression AND the generated ALU file for fused_imm_i
        alu_file = ws.output_root / "rtl" / "cv32e40p_alu.sv"
        needs_imm = needs_fused_imm(self.fused_operations)
        if not needs_imm and alu_file.exists():
            needs_imm = "fused_imm_i" in alu_file.read_text()
        if needs_imm:
            add_fused_imm_port(str(rtl_dir), imm_width=10)

        # ── Post-fusion fix 2: Adder reuse wiring ──
        from arvis.codegen.rtl.adder_reuse_patcher import add_adder_reuse_wiring, needs_adder_reuse

        if needs_adder_reuse(self.fused_operations):
            steering_sv = gen.generate_alu_adder_steering()
            add_adder_reuse_wiring(str(rtl_dir), steering_sv)

        # ── Post-fusion fix 2b: Mult pre-compute input steering ──
        from arvis.codegen.rtl.mult_reuse_patcher import (
            _get_pre_compute_ops,
            add_mult_pre_compute_wiring,
            generate_mult_steering,
            needs_mult_pre_compute,
        )

        if needs_mult_pre_compute(self.fused_operations):
            pre_ops = _get_pre_compute_ops(self.fused_operations)
            steering_sv = generate_mult_steering(pre_ops)
            add_mult_pre_compute_wiring(str(rtl_dir), steering_sv)

        # ── Post-fusion fix 3: Disable CUSTOM_0 SVA assertion ──
        from arvis.codegen.rtl.fusion_patches import disable_custom0_sva

        disable_custom0_sva(rtl_dir)

        # ── Post-fusion fix 4: Update ALU_OP_WIDTH if needed ──
        from arvis.codegen.rtl.fusion_patches import update_alu_op_width

        update_alu_op_width(rtl_dir, gen)

        # Update context
        ctx.fusion_rtl_applied = True

    def _apply_encoding_optimization(self, ws: "RTLWorkspace", *, verbose: bool = True) -> None:
        """Optimize enum encodings by removing unused members and reducing bit widths.

        This runs AFTER all pruning, pragma processing, DCE, and fusion so
        it can scan the final RTL to determine which enum members survived.

        Targets:
          - alu_opcode_e: remove unused ALU ops, reduce ALU_OP_WIDTH
          - mul_opcode_e: remove unused MUL modes, reduce MUL_OP_WIDTH
          - ctrl_state_e: remove unused FSM states (debug, hwloop), reduce width
          - mult_state_e: remove unused multiplier states, reduce width
        """
        from arvis.cli import print_info

        _print_info = print_info if verbose else (lambda *a, **k: None)

        try:
            from arvis.analysis.enum_usage import analyze_enum_usage
            from arvis.codegen.rtl.encoding_optimizer import apply_encoding_optimization

            # Build forced_unused from pruning decisions.
            # The PruneConfig already knows which ALU ops and MUL modes are
            # removable. These may still appear as dead references in
            # id_stage.sv comparisons (e.g. alu_operator != ALU_BEXT) but
            # were removed from the decoder and ALU case statements.
            forced_unused = {}
            if self.prune_config is not None:
                # Note: alu_opcode_e is excluded — it has bit-extraction
                # constraints and complex fused instruction interactions.
                # ALU width is managed by the fusion RTL generator instead.
                if self.prune_config.removable_mul_modes:
                    forced_unused["mul_opcode_e"] = set(self.prune_config.removable_mul_modes)

            if verbose:
                print("\n  Encoding optimization (enum width reduction):")
            report = analyze_enum_usage(ws.output_root)

            result = apply_encoding_optimization(
                ws.output_root,
                usage_report=report,
                forced_unused=forced_unused if forced_unused else None,
                verbose=verbose,
            )

            if result.total_bits_saved > 0:
                _print_info(
                    f"Encoding optimization: {result.total_bits_saved} bits saved across {result.files_modified} files"
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
        except Exception as e:
            from arvis.cli import print_warning

            print_warning(f"Encoding optimization skipped: {e}")

    @staticmethod
    def _clear_fused_pragmas(rtl_dir: Path) -> None:
        """Clear content between ARVIS_FUSED_BEGIN/END pragmas."""
        import re

        for filename in [
            "include/cv32e40p_pkg.sv",
            "cv32e40p_decoder.sv",
            "cv32e40p_alu.sv",
        ]:
            fpath = rtl_dir / filename
            if not fpath.exists():
                continue
            text = fpath.read_text()
            text = re.sub(
                r"(// ARVIS_FUSED_BEGIN: \w+\n).*?(// ARVIS_FUSED_END: \w+)",
                r"\1\2",
                text,
                flags=re.DOTALL,
            )
            fpath.write_text(text)
