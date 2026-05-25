"""
Pipeline runner — orchestrates all phases of the ARVIS specialization pipeline.

Extracted from main.py to separate CLI parsing from pipeline logic.
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


def run_pipeline(cfg: ToolConfig, ctx: PipelineContext) -> None:
    """Run the ARVIS specialization pipeline according to cfg.enabled_phases."""
    from arvis.pipeline.rtl_changeset import RTLChangeSet

    changeset = RTLChangeSet()

    # ── Phase 1+2: Analysis (Profiling + Selection) ──
    if "analysis" in cfg.enabled_phases:
        _run_analysis(cfg, ctx, changeset)

    # ── Phase 5: Fusion (RTL gen + GCC compile + filter) ──
    if "fusion" in cfg.enabled_phases:
        _run_fusion(cfg, ctx, changeset)

    # ── Phase 5b: HC Assembly Patching (patch fused .s with HC immediates) ──
    # BYPASSED: HC patterns now included in merged .md for GCC to handle
    # if "fusion" in cfg.enabled_phases and ctx.fused_elf_path:
    #     from pipeline.hc_asm_patch import run_hc_patch
    #     run_hc_patch(cfg, ctx, changeset)

    # Save fused-only hex before hwloop overwrites it
    fused_only_hex = ctx.fused_hex_path
    fused_only_elf = ctx.fused_elf_path

    # ── Phase 4: HW Loop (dual-compile → merge → patch → reassemble) ──
    if "fusion" in cfg.enabled_phases:
        from arvis.pipeline import hwloop

        hwloop.run(cfg, ctx, changeset)

    # ── Phase 3: Pruning (compute PruneConfig from FINAL binary) ──
    prune_config_orig, all_used_orig = None, set()
    prune_config_hwonly, all_used_hwonly = None, set()
    if "pruning" in cfg.enabled_phases:
        prune_config_orig, all_used_orig, prune_config_hwonly, all_used_hwonly = _run_pruning(
            cfg, ctx, changeset
        )

    # ── PC pipeline narrowing analysis ──
    # Compute optimal PC_WIDTH from the widest binary the pipeline will
    # run.  The same RTL must accommodate every variant (baseline,
    # fused-only, hwloop-only, fused+hwloop), so we take the max across
    # whichever ELFs are available at this point.  This sets one
    # ``changeset.pc_width`` value that flows into every sub-changeset
    # (cs2/cs3/cs4/cs5) inside _run_verification.
    changeset.pc_width = _compute_pc_width(ctx, fused_only_elf)

    # ── Verification ──
    if "pruning" in cfg.enabled_phases:
        # Phase 7 gateway: when --use-pipeline-runner is set, route the
        # 5-variant emission + sim + synth through Pipeline.run_full_verification
        # instead of the legacy _run_verification if-tree.
        if cfg.use_pipeline_runner:
            _run_verification_via_pipeline(
                cfg,
                ctx,
                changeset,
                prune_config_orig,
                all_used_orig,
                prune_config_hwonly=prune_config_hwonly,
                all_used_hwonly=all_used_hwonly,
                fused_only_hex=fused_only_hex,
                fused_only_elf=fused_only_elf,
            )
        else:
            _run_verification(
                cfg,
                ctx,
                changeset,
                prune_config_orig,
                all_used_orig,
                prune_config_hwonly=prune_config_hwonly,
                all_used_hwonly=all_used_hwonly,
                fused_only_hex=fused_only_hex,
                fused_only_elf=fused_only_elf,
            )

    # ── HTML Report ──
    # Stash final changeset state onto ctx for the report to read.
    ctx.final_hw_loop_count = changeset.hw_loop_count  # type: ignore[attr-defined]
    ctx.final_prefetch_fifo_depth = changeset.prefetch_fifo_depth  # type: ignore[attr-defined]
    ctx.final_pc_width = getattr(changeset, "pc_width", None)  # type: ignore[attr-defined]
    ctx.final_changeset = changeset  # type: ignore[attr-defined]

    from arvis.cli import print_success
    from arvis.report.html_report import generate_html_report

    report_path = generate_html_report(cfg, ctx)
    ctx.report_path = report_path
    print_success(f"HTML report: {report_path}")


def _build_docker_baseline(cfg: ToolConfig, ctx: PipelineContext) -> None:
    """Build baseline ELFs/hex with Docker GCC (no fused, no hwloop).

    Produces {prog}_baseline.elf, .hex, .s — never overwrites fusion outputs.
    The Makefile builds {prog}.elf/.hex/.s, which we rename to *_baseline.* after.
    """
    import subprocess

    from arvis.cli import print_step, print_success, print_warning
    from arvis.config import BENCHMARKS

    bm = BENCHMARKS.get(cfg.benchmark_name, {})
    if not bm.get("dir"):
        return

    bm_dir = Path(bm["dir"]).resolve()
    makefile = bm_dir / "Makefile"
    if not makefile.exists():
        return

    specializer_dir = Path(__file__).resolve().parent.parent

    image = "riscv-gcc-prebuilt"
    ret = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=10)
    if ret.returncode != 0:
        from arvis.pipeline.hwloop import HWLOOP_ONLY_IMAGE

        image = HWLOOP_ONLY_IMAGE
        ret = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=10)
        if ret.returncode != 0:
            image = "custom-riscv-gcc-merged"
            ret = subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=10
            )
            if ret.returncode != 0:
                print_warning("No Docker GCC image available — using system GCC baseline")
                return

    print_step("0/5", "Building Docker baseline (same compiler for all)")

    prog_name = cfg.benchmark_name.replace("embench_", "")
    _RISCV_PREFIX = "/opt/riscv"

    # Save fused .s if it exists (fusion step output)
    fused_s = bm_dir / f"{prog_name}.s"
    fused_s_backup = None
    if fused_s.exists():
        fused_s_backup = bm_dir / f".{prog_name}.s.fused_backup"
        import shutil as _sh

        _sh.copy2(str(fused_s), str(fused_s_backup))

    # Clean and rebuild everything with baseline Docker GCC
    make_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{specializer_dir}:/work",
        "-w",
        f"/work/{bm_dir.relative_to(specializer_dir)}",
        image,
        "make",
        "-f",
        "Makefile",
        "clean",
        "all",
        f"PREFIX={_RISCV_PREFIX}/bin/riscv32-unknown-elf-",
        f"CC={_RISCV_PREFIX}/bin/riscv32-unknown-elf-gcc",
        f"OBJCOPY={_RISCV_PREFIX}/bin/riscv32-unknown-elf-objcopy",
    ]

    ret = subprocess.run(make_cmd, capture_output=True, text=True, timeout=120)
    if ret.returncode != 0 and "lgcc" in ret.stderr:
        # Retry with custom-riscv-gcc-merged which has libgcc
        print_warning("Build failed (missing libgcc) — retrying with merged image")
        make_cmd[5] = "custom-riscv-gcc-merged"
        ret = subprocess.run(make_cmd, capture_output=True, text=True, timeout=120)
    if ret.returncode != 0:
        print_warning("Docker baseline build failed — rebuilding with system GCC")
        if fused_s_backup and fused_s_backup.exists():
            import shutil as _sh2

            _sh2.copy2(str(fused_s_backup), str(fused_s))
            fused_s_backup.unlink()
        # Rebuild with system GCC so the elf exists for profiling
        subprocess.run(
            ["make", "-C", str(bm_dir), "clean", "all"], capture_output=True, timeout=120
        )
        return

    # Rename make outputs to baseline-specific names
    import shutil as _sh3

    for ext in [".elf", ".hex", ".s"]:
        src = bm_dir / f"{prog_name}{ext}"
        dst = bm_dir / f"{prog_name}{ext}.baseline"
        if src.exists():
            if dst.exists():
                dst.unlink()
            _sh3.copy2(str(src), str(dst))

    # Restore fused .s
    if fused_s_backup and fused_s_backup.exists():
        if fused_s.exists():
            fused_s.unlink()
        _sh3.copy2(str(fused_s_backup), str(fused_s))
        fused_s_backup.unlink()

    # Also set .baseline for verilator_elf and hex if they differ from prog_name
    hex_name = bm.get("hex", "")
    elf_name = bm.get("verilator_elf", "")
    spike_elf = bm.get("elf", "")
    for name in [hex_name, elf_name, spike_elf]:
        if not name or name == f"{prog_name}.elf" or name == f"{prog_name}.hex":
            continue
        src = bm_dir / name
        dst = bm_dir / (name + ".baseline")
        if src.exists():
            if dst.exists():
                dst.unlink()
            _sh3.copy2(str(src), str(dst))

    print_success("Docker baseline built → *_baseline.{elf,hex,s}")


def _run_analysis(cfg: ToolConfig, ctx: PipelineContext, changeset) -> None:
    from arvis.pipeline import profiling, selection

    # Build baseline with Docker GCC (same compiler as fused/hwloop)
    # This ensures all binaries come from the same compiler for fair comparison
    _build_docker_baseline(cfg, ctx)

    profiling.run(cfg, ctx)
    selection.run(cfg, ctx)

    if ctx.profile is not None:
        from arvis.analysis.bottleneck import analyze_bottleneck, print_bottleneck_report
        from arvis.cli import print_section

        print_section("BOTTLENECK ANALYSIS")
        baseline_cycles = getattr(ctx, "baseline_cycles", 0)
        bottleneck = analyze_bottleneck(ctx.blocks, ctx.profile, baseline_cycles)
        print_bottleneck_report(bottleneck)
        ctx.bottleneck = bottleneck  # type: ignore[attr-defined]

        if bottleneck.fetch_stalls > bottleneck.total_cycles * 0.05:
            from arvis.pipeline.prefetch_sweep import sweep_prefetch_depth

            best_depth, fifo_results = sweep_prefetch_depth(cfg, ctx)
            # Stash for the HTML report (Phase 9 observability).
            ctx.fifo_sweep_results = fifo_results  # type: ignore[attr-defined]
            ctx.fifo_chosen_depth = best_depth  # type: ignore[attr-defined]
            if best_depth > 0:
                changeset.prefetch_fifo_depth = best_depth


def _run_fusion(cfg: ToolConfig, ctx: PipelineContext, changeset) -> None:
    from arvis.pipeline import fusion_rtl, gcc_compile

    _tmp_rtl = tempfile.mkdtemp(prefix="fusion_tmp_")
    ctx.rtl_output_dir = _tmp_rtl
    fusion_rtl.run(cfg, ctx)
    shutil.rmtree(_tmp_rtl, ignore_errors=True)
    ctx.rtl_output_dir = ""

    gcc_compile.run(cfg, ctx)

    if ctx.gcc_compile_result and ctx.gcc_compile_result.used_instructions:
        fused_ops = fusion_rtl.compute_filtered_ops(cfg, ctx)
        if fused_ops:
            changeset.add_fused_operations(fused_ops)
            ctx._rtl_changeset_fused_ops = fused_ops  # type: ignore[attr-defined]


def _compute_pc_width(ctx: PipelineContext, fused_only_elf=None) -> int:
    """Compute the optimal PC_WIDTH for whichever benchmark ELFs exist.

    Mirrors :func:`pipeline.hwloop_sweep.analyze_addr_width` but for the
    main PC pipeline rather than the hwloop registers.  We take the max
    over every variant the pipeline will run (baseline, fused-only,
    hwloop-only, fused+hwloop) so the same RTL accommodates all of them.

    Returns 0 when no ELF is available, which signals "don't narrow"
    to :class:`RTLChangeSet`.
    """
    try:
        from apply_pc_width import analyze_pc_width
    except ImportError:
        return 0  # apply_pc_width.py not on PYTHONPATH; skip narrowing

    candidates = [
        ctx.fused_elf_path,
        ctx.hwloop_elf_path,
        ctx.hwloop_only_elf_path,
        fused_only_elf,
    ]
    seen: set[str] = set()
    width = 0
    for elf in candidates:
        if not elf:
            continue
        elf_str = str(elf)
        if elf_str in seen or not Path(elf_str).exists():
            continue
        seen.add(elf_str)
        try:
            w = analyze_pc_width(elf_str)
            if w > width:
                width = w
        except Exception:
            continue
    return width


def _run_pruning(cfg: ToolConfig, ctx: PipelineContext, changeset) -> tuple:
    from arvis.cli import print_info
    from arvis.pipeline.pruning import compute_prune_config

    has_fusion = "fusion" in cfg.enabled_phases and changeset.fused_operations

    if has_fusion:
        saved_fused_elf = ctx.fused_elf_path
        saved_fused_hex = ctx.fused_hex_path

        # If hwloop ran, ctx.fused_elf_path points at the fused+hwloop
        # binary, but the FUSED_PRUNED variant runs the fused-ONLY binary.
        # Use the saved fused-only path for the FUSED prune config so we
        # don't mark instructions removable that are only present in
        # fused-only code (e.g. a plain `xor` outside fusion that becomes
        # a hwloop body when hwloop is active).
        fused_only_elf = getattr(ctx, "fused_only_elf_path", None)
        fused_only_hex = getattr(ctx, "fused_only_hex_path", None)

        # Step 2 prune config: based on original baseline binary
        ctx.fused_elf_path = None
        ctx.fused_hex_path = None
        print_info("Computing prune config for BASELINE binary (step 2)...")
        prune_config_baseline, all_used_baseline = compute_prune_config(cfg, ctx, verbose=False)

        # Step 4 prune config: based on hwloop-only binary
        hwonly_elf = getattr(ctx, "hwloop_only_elf_path", None)
        if hwonly_elf and os.path.exists(str(hwonly_elf)):
            ctx.fused_elf_path = str(hwonly_elf)
            print_info("Computing prune config for HWLOOP-ONLY binary (step 4)...")
            prune_config_hwonly, all_used_hwonly = compute_prune_config(cfg, ctx, verbose=False)
        else:
            prune_config_hwonly, all_used_hwonly = prune_config_baseline, all_used_baseline

        # Use the fused-only binary for the FUSED prune config when
        # available, otherwise fall back to whatever was saved (which may
        # be the fused+hwloop binary if hwloop didn't separate them).
        if fused_only_elf and os.path.exists(str(fused_only_elf)):
            ctx.fused_elf_path = str(fused_only_elf)
            ctx.fused_hex_path = str(fused_only_hex) if fused_only_hex else saved_fused_hex
            print_info("Computing prune config for FUSED binary (step 3+, fused-only)...")
        else:
            ctx.fused_elf_path = saved_fused_elf
            ctx.fused_hex_path = saved_fused_hex
            print_info("Computing prune config for FUSED binary (step 3+)...")

        prune_config_fused, all_used_fused = compute_prune_config(cfg, ctx)
        # Restore so downstream consumers see the binary that actually
        # runs after hwloop merging.
        ctx.fused_elf_path = saved_fused_elf
        ctx.fused_hex_path = saved_fused_hex
        changeset.add_prune_config(prune_config_fused, all_used_fused)
        return prune_config_baseline, all_used_baseline, prune_config_hwonly, all_used_hwonly
    else:
        prune_config_orig, all_used_orig = compute_prune_config(cfg, ctx)
        changeset.add_prune_config(prune_config_orig, all_used_orig)
        return prune_config_orig, all_used_orig, prune_config_orig, all_used_orig


def _loop_selection_via_sweep(
    *,
    cfg,
    ctx,
    bm_dir: Path,
    prog_name: str,
    hw_val: int,
    rtl_all: str,
    label_tag: str,
    encoding,
    image: str | None,
    specializer_dir: Path,
    fifo_depth: int,
    build_sim_fn,
) -> bool:
    """Run per-loop selection via the LoopSelectionSweep framework.

    Returns True if the sweep produced a result and updated ctx;
    False if it couldn't run (caller falls through to legacy GA).
    """
    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.cli import print_info, print_section
    from arvis.pipeline.hwloop import _assemble_patched
    from arvis.strategies.sweep.loop_selection import LoopSelectionSweep
    from arvis.targets.cv32e40p.sweep_evaluators import LoopSelectionEvaluator

    tag = label_tag.replace(" ", "_").lower()
    best_hex: str | None = None
    best_elf: str | None = None
    best_cycles = float("inf")

    for variant, src_name in [
        ("nounroll", f"{prog_name}_merged.s"),
        ("unroll", f"{prog_name}_merged_unroll.s"),
    ]:
        fused_src = bm_dir / src_name
        if not fused_src.exists():
            continue

        print_section(f"LOOP SELECTION SWEEP — All ({label_tag}, {variant})")

        # Build sim binary from the sweep RTL
        sim_bin = build_sim_fn(rtl_all, f"sweep_sel_{tag}_{variant}")
        if not sim_bin:
            continue

        # Detect eligible loops
        det = AsmLoopDetector(str(fused_src), fifo_depth=fifo_depth)
        det.find_all_loops()

        # Apply cached valid_loops filter if available
        _cand_for_variant = None
        for _c in ctx.hwloop_candidates:
            if getattr(_c, "fused_src", None) == str(fused_src):
                _cand_for_variant = _c
                break
        _cached_valid = (
            getattr(_cand_for_variant, "_valid_loops", None) if _cand_for_variant else None
        )
        if _cached_valid is not None:
            for loop in det.all_loops:
                k = (loop.start_label, loop.start_line, loop.back_branch_insn)
                if k not in _cached_valid:
                    loop.hw_eligible = False

        eligible = [loop for loop in det.all_loops if loop.hw_eligible]
        if not eligible:
            continue

        loop_keys = [
            (loop.start_label, loop.start_line, loop.back_branch_insn) for loop in eligible
        ]
        N = len(loop_keys)
        print_info(f"  {N} eligible loops → sweep via {'exhaustive' if N <= 6 else 'GA'}")

        # Build evaluator and sweep
        evaluator = LoopSelectionEvaluator(
            src_asm=fused_src,
            loop_keys=loop_keys,
            hw_loop=hw_val,
            bm_dir=bm_dir,
            sim_bin=sim_bin,
            image=image,
            specializer_dir=specializer_dir,
            encoding=encoding,
            fifo_depth=fifo_depth,
        )
        sweep = LoopSelectionSweep(
            loop_keys=[str(k) for k in loop_keys],
            evaluator=evaluator,
        )

        # Run the sweep (analyze ignores workload/profile/target for sweeps)
        decision = sweep.analyze(None, None, None)  # type: ignore[arg-type]

        if decision.best is None:
            print_info(f"  Sweep found no passing candidates for {variant}")
            continue

        sweep_cycles = decision.best.metrics.get("cycles", float("inf"))
        mask = sweep.best_mask(decision)
        n_on = sum(mask)
        print_info(
            f"  ✓ Sweep result: {int(sweep_cycles):,} cycles "
            f"({n_on}/{N} loops, {len(decision.all_results)} evals)"
        )

        if sweep_cycles < best_cycles:
            # Build the final patched assembly with the winning mask
            from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator

            asm_text = fused_src.read_text()
            det_final = AsmLoopDetector(str(fused_src), fifo_depth=fifo_depth)
            det_final.find_all_loops()
            enabled_keys = set(loop_keys[i] for i in range(N) if mask[i])
            for loop in det_final.all_loops:
                k = (loop.start_label, loop.start_line, loop.back_branch_insn)
                loop.hw_eligible = k in enabled_keys
            gen = HWLoopGenerator(det_final, hw_loop=hw_val, encoding=encoding)
            gen.generate()
            patcher = AsmPatcher(gen, asm_text)
            patched_path = bm_dir / f"{prog_name}_hw{hw_val}_sweep_sel_{tag}_{variant}.s"
            patched_path.write_text(patcher.patch())

            sel_elf = f"{prog_name}_hw{hw_val}_sweep_sel_{tag}_{variant}.elf"
            sel_hex = f"{prog_name}_hw{hw_val}_sweep_sel_{tag}_{variant}.hex"
            ok = _assemble_patched(
                specializer_dir, bm_dir, patched_path, sel_elf, sel_hex, image=image
            )
            if ok:
                best_cycles = sweep_cycles
                best_hex = str(bm_dir / sel_hex)
                best_elf = str(bm_dir / sel_elf)
                print_info(f"  ✓ {variant}: {int(best_cycles):,} cycles ← new best")

    if best_hex:
        ctx.hwloop_hex_path = best_hex
        ctx.hwloop_elf_path = best_elf
        ctx.fused_hex_path = best_hex
        ctx.fused_elf_path = best_elf
        return True

    return False


def _run_verification_via_pipeline(
    cfg: ToolConfig,
    ctx: PipelineContext,
    changeset,
    prune_config_orig,
    all_used_orig: set,
    *,
    prune_config_hwonly=None,
    all_used_hwonly: set | None = None,
    fused_only_hex: str | None = None,
    fused_only_elf: str | None = None,
) -> None:
    """Phase 7 entry: run the 5-variant verification via Pipeline.run_full_verification.

    Replaces the legacy :func:`_run_verification` if-tree with the
    unified portable path.  Binary-gen phases (analysis / fusion /
    hwloop / pruning) still run upstream in the legacy code; this
    function consumes their pre-built hex files via
    :class:`BenchmarkWorkload.hex_for_variant`.

    The Pipeline is built fresh per call from:

    * The :class:`CV32E40P` target (carries ``standard_variants``).
    * The same strategy set as the legacy path: fusion, hwloop,
      pruning, width.  Strategies are constructed in-place from
      whatever state the binary-gen phases left in ``ctx`` /
      ``changeset``.
    * The current concrete :class:`VerilatorVerifier` and
      :class:`YosysSynthesisFlow` (skipped when their tools are
      unavailable).
    * The :class:`MarkdownReporter` for the ``report.md`` artifact.

    The legacy ``runner.py`` HTML reporter still runs after this
    function returns; the markdown report is an additional
    artifact produced by the new path.

    Args:
        Same as :func:`_run_verification`.

    Side effects:
        Writes ``rtl_<variant>/`` directories under
        ``cfg.output_dir`` and a ``report.md`` summarising
        sim/synth results.  Mutates ``ctx`` to record the
        per-variant results so the legacy HTML reporter can pick
        them up.
    """
    from arvis.cli import print_section, print_success
    from arvis.core.pipeline import Pipeline
    from arvis.report.markdown_reporter import MarkdownReporter
    from arvis.simulation.verifier import VerilatorVerifier
    from arvis.strategies.fusion import NGramFusion
    from arvis.strategies.hwloop import CV32E40PHWLoop
    from arvis.strategies.pruning import UsageDrivenPruner
    from arvis.strategies.width import PCWidthNarrowing
    from arvis.synthesis.yosys_flow import YosysSynthesisFlow
    from arvis.targets import CV32E40P
    from arvis.toolchains import RISCVGCCToolchain, ToolchainError
    from arvis.workloads.benchmark import BenchmarkWorkload

    print_section("VERIFICATION via Pipeline.run_full_verification (Phase 7)")

    # Build the workload pointing at the pre-populated bench dir.
    workload = BenchmarkWorkload(bench_dir=Path(cfg.benchmark_dir))

    # Target carries standard_variants so Pipeline drives the
    # canonical 5-variant set.
    target = CV32E40P()

    # Toolchain is best-effort: when GCC isn't installed (CI
    # without RISC-V toolchain), fall back to a null toolchain so
    # construction doesn't fail.
    try:
        toolchain = RISCVGCCToolchain()
    except ToolchainError:
        from arvis.core.isa import ISADescriptor
        from arvis.core.toolchain import Toolchain

        class _NullToolchain(Toolchain):
            @property
            def name(self) -> str:
                return "null"

            @property
            def isa(self) -> ISADescriptor:
                return ISADescriptor(name="rv32imc", xlen=32, standard_extensions=("i", "m", "c"))

            def compile(self, *a, **kw):
                raise NotImplementedError

            def assemble(self, *a, **kw):
                raise NotImplementedError

            def disassemble(self, elf):
                return ""

        toolchain = _NullToolchain()

    # Construct strategies; they're cheap to build and the analyze()
    # calls inside Pipeline.run() short-circuit when applicable() returns
    # False.
    strategies = [
        NGramFusion(),
        CV32E40PHWLoop(),
        UsageDrivenPruner(verbose=False),
        PCWidthNarrowing(),
    ]

    verifier = VerilatorVerifier(rtl_root=str(target.rtl_root))
    synthesis = YosysSynthesisFlow()
    reporter = MarkdownReporter()

    pipeline = Pipeline(
        target=target,
        toolchain=toolchain,
        strategies=strategies,
        verifier=verifier,
        synthesis=synthesis,
        reporter=reporter,
    )
    # Redirect output to cfg.output_dir so artifacts land where the
    # legacy code expects them.
    out_root = Path(cfg.output_dir)
    pipeline._output_root_for_workload = lambda wl: out_root  # type: ignore[method-assign]

    # Phase 9: resolve --variants / --variant CLI flags.  When
    # neither was specified, fall back to the target's full
    # standard set via run_full_verification().
    from arvis.pipeline.variant_resolver import VariantResolutionError, resolve_variants

    try:
        custom_variants = resolve_variants(
            names=getattr(cfg, "variants_arg", None),
            inline_specs=getattr(cfg, "variant_arg", None),
            target=target,
        )
    except VariantResolutionError as exc:
        from arvis.cli import print_error

        print_error(f"Variant resolution failed: {exc}")
        raise

    if custom_variants is None:
        # No flags → emit the target's full standard set.
        result = pipeline.run_full_verification(workload)
    else:
        # Custom set → bypass run_full_verification and use Pipeline.run
        # directly with the resolved list.
        labels = ", ".join(v.label for v in custom_variants)
        print_section(f"VERIFICATION variants: {labels}")
        pipeline.variants = custom_variants
        result = pipeline.run(workload)

    # Stash per-variant results on ctx so the HTML reporter can
    # pick them up.  Also expose for downstream tools.
    ctx.pipeline_result = result

    print_success(
        f"Pipeline.run_full_verification: {len(result.variants)} variants emitted "
        f"-> {out_root / 'report.md'}"
    )


def _run_verification(
    cfg: ToolConfig,
    ctx: PipelineContext,
    changeset,
    prune_config_orig,
    all_used_orig: set,
    *,
    prune_config_hwonly=None,
    all_used_hwonly: set = None,
    fused_only_hex: str | None = None,
    fused_only_elf: str | None = None,
) -> None:
    """5-step verification:
    1. Baseline        — original RTL + original hex
    2. Pruned          — pruned RTL + original hex
    3. Fused+Pruned    — fused+pruned RTL + fused hex (no hwloop)
    4. HWLoop+Pruned   — hwloop+pruned RTL + hwloop-only hex (no fused)
    5. All             — fused+hwloop+pruned RTL + fused+hwloop hex
    """
    import subprocess

    from arvis.cli import print_section
    from arvis.pipeline import verification
    from arvis.pipeline.rtl_changeset import RTLChangeSet
    from arvis.pipeline.step_config import RTLConfig, SimCache

    has_fusion = "fusion" in cfg.enabled_phases and changeset.fused_operations
    has_hwloop = ctx.hwloop_hex_path is not None
    has_hwloop_only = ctx.hwloop_only_hex_path is not None

    # ── Sim cache: build once per (RTL, hw_loop), reuse everywhere ──
    sim_cache = SimCache()

    # Track RTL dirs after each step's apply() so exhaustive search can reuse
    rtl_dirs: dict[str, str] = {}  # step_name → rtl_dir path

    # ── Step 1: Baseline ──
    print_section("STEP 1: BASELINE (original RTL + original hex)")
    verification.run_baseline_sim(cfg, ctx)

    # ── Step 2: Pruned-only ──
    print_section("STEP 2: PRUNED (pruned RTL + original hex)")
    changeset_prune = RTLChangeSet()
    if prune_config_orig is not None:
        changeset_prune.add_prune_config(copy.deepcopy(prune_config_orig), set(all_used_orig))
    else:
        changeset_prune.add_prune_config(
            copy.deepcopy(changeset.prune_config), set(changeset.used_instructions)
        )
    changeset_prune.pc_width = changeset.pc_width
    changeset_prune._apply_label = "pruned"
    changeset_prune.apply(cfg, ctx, verbose=False)
    verification.run_synthesis(cfg, ctx)
    verification.run_post_pruning_check(cfg, ctx, label="pruned", use_original_hex=True)

    # ── Step 3: Fused+Pruned (no hwloop) ──
    if has_fusion:
        print_section("STEP 3: FUSED+PRUNED (fused+pruned RTL + fused hex)")
        cs3 = RTLChangeSet()
        cs3.hw_loop_count = 0
        cs3.prefetch_fifo_depth = changeset.prefetch_fifo_depth
        cs3.pc_width = changeset.pc_width
        cs3.add_fused_operations(changeset.fused_operations)
        cs3.add_prune_config(
            copy.deepcopy(changeset.prune_config), set(changeset.used_instructions)
        )
        cs3._apply_label = "fused_pruned"
        cs3.apply(cfg, ctx)
        step3_hex = fused_only_hex or ctx.fused_hex_path
        ctx.hex_fused = step3_hex
        verification.run_synth_step(cfg, ctx, label="fused_pruned", prune_config=cs3.prune_config)
        verification.run_post_pruning_check(cfg, ctx, label="fused_pruned", explicit_hex=step3_hex)

    # ── HW_LOOP sweep: pick best ADP and best cycles ──
    sweep_winners = None
    if has_hwloop_only or has_hwloop:
        sweep_winners = _sweep_hwloop_candidates(
            cfg, ctx, changeset, prune_config_orig, all_used_orig
        )

    def _set_candidate(hw_val, specific_cand=None):
        """Point changeset + ctx at a specific HW_LOOP candidate."""
        changeset.hw_loop_count = hw_val
        cand = specific_cand
        if not cand:
            for c in ctx.hwloop_candidates:
                if c.hw_loop == hw_val:
                    cand = c
                    break
        if cand:
            if cand.fused_hex:
                ctx.hwloop_hex_path = cand.fused_hex
                ctx.hwloop_elf_path = cand.fused_elf
            if cand.plain_hex:
                ctx.hwloop_only_hex_path = cand.plain_hex
                ctx.hwloop_only_elf_path = cand.plain_elf
            return True
        return False

    def _run_step4(label_suffix=""):
        tag = f"hwloop_pruned{label_suffix}"
        hw = changeset.hw_loop_count
        print_section(f"STEP 4{label_suffix.upper()}: HWLOOP+PRUNED (HW_LOOP={hw})")
        cs4 = RTLChangeSet()
        cs4.hw_loop_count = hw
        cs4.hw_loop_cnt_width = changeset.hw_loop_cnt_width
        cs4.hw_loop_addr_width = changeset.hw_loop_addr_width
        cs4.prefetch_fifo_depth = changeset.prefetch_fifo_depth
        cs4.pc_width = changeset.pc_width
        if prune_config_hwonly is not None:
            cs4.add_prune_config(copy.deepcopy(prune_config_hwonly), set(all_used_hwonly))
        elif prune_config_orig is not None:
            cs4.add_prune_config(copy.deepcopy(prune_config_orig), set(all_used_orig))
        else:
            cs4.add_prune_config(
                copy.deepcopy(changeset.prune_config), set(changeset.used_instructions)
            )
        cs4._apply_label = tag
        cs4.apply(cfg, ctx)
        rtl_dirs[tag] = os.path.join(ctx.rtl_output_dir, "rtl")

        # Collect ALL plain hex variants (nounroll + unroll) for this hw_loop depth
        plain_hexes = []
        for c in ctx.hwloop_candidates:
            if c.hw_loop == hw and c.plain_hex:
                plain_hexes.append((c.plain_hex, c.plain_elf))
        if not plain_hexes:
            plain_hexes = [(ctx.hwloop_only_hex_path, ctx.hwloop_only_elf_path)]

        verification.run_synth_step(cfg, ctx, label=tag, prune_config=cs4.prune_config)

        # Run all variants, pick best
        best_sim = None
        best_hex = None
        best_elf = None
        for hex_path, elf_path in plain_hexes:
            if not hex_path:
                continue
            sim_r = verification.run_post_pruning_check(cfg, ctx, label=tag, explicit_hex=hex_path)
            if sim_r and sim_r.test_passed:
                if best_sim is None or sim_r.total_cycles < best_sim.total_cycles:
                    best_sim = sim_r
                    best_hex = hex_path
                    best_elf = elf_path

        if best_sim:
            ctx.hwloop_only_hex_path = best_hex
            ctx.hwloop_only_elf_path = best_elf
            step4_hex = best_hex
            ctx.hex_hwloop_pruned = step4_hex
            setattr(ctx, f"sim_{tag}", best_sim)
            from arvis.cli import print_info

            print_info(f"  {tag}: best variant {best_sim.total_cycles:,} cycles")
        else:
            step4_hex = plain_hexes[0][0] if plain_hexes else ctx.hwloop_only_hex_path
            ctx.hex_hwloop_pruned = step4_hex
            sim4 = None
        # Retry with pre-validation if no variant passed — try each candidate's plain_src
        if not best_sim:
            from arvis.cli import print_info
            from arvis.pipeline.hwloop import _patch_asm, _prevalidate_loops

            _bm_dir = Path(cfg.benchmark_dir).resolve()
            _prog = cfg.benchmark_name.replace("embench_", "")
            _enc_plain = getattr(ctx, "_hwlp_enc_plain", None)
            _rtl_dir = rtl_dirs.get(tag)
            if _rtl_dir:
                print_info(f"  {tag}: FAIL/TIMEOUT — running per-loop pre-validation")
                from arvis.pipeline.verification import _make_sim_cfg
                from arvis.simulation.verilator_runner import VerilatorRunner

                _sim_dir = os.path.join(cfg.output_dir, f"sim_preval_{tag}")
                _vr = VerilatorRunner(_make_sim_cfg(cfg))
                _TB = {"COREV_PULP", "FPU", "NUM_MHPMCOUNTERS", "HW_LOOP", "COREV_CLUSTER", "ZFINX"}
                _fl = [
                    f
                    for f in getattr(ctx, "verilator_extra_flags", [])
                    if any(f.startswith(f"-G{p}=") for p in _TB)
                ]
                _ok, _sim_bin, _ = _vr.build_sim(
                    rtl_dir=_rtl_dir, output_dir=_sim_dir, extra_flags=_fl
                )
                if _ok and _sim_bin:
                    _best_val_cycles = float("inf")
                    for _c in ctx.hwloop_candidates:
                        if _c.hw_loop != hw or not _c.plain_src:
                            continue
                        _psrc = Path(_c.plain_src)
                        if not _psrc.exists():
                            continue
                        print_info(f"  Trying {_psrc.name}...")
                        _valid = _prevalidate_loops(
                            _psrc,
                            hw,
                            _bm_dir,
                            _sim_bin,
                            "link_cv32e40p.ld",
                            "crt0_cv32e40p.S",
                            encoding=_enc_plain,
                            image=None,
                            specializer_dir=Path(__file__).resolve().parent.parent,
                            fifo_depth=changeset.prefetch_fifo_depth or 2,
                        )
                        if not _valid:
                            continue
                        _out = f"{_prog}_hw{hw}_{tag}_{_psrc.stem}_validated.s"
                        _patched, _ = _patch_asm(
                            _psrc,
                            _out,
                            hw,
                            _bm_dir,
                            encoding=_enc_plain,
                            valid_loops=_valid,
                            fifo_depth=changeset.prefetch_fifo_depth or 2,
                        )
                        if not _patched:
                            continue
                        _elf = f"{_prog}_hw{hw}_{tag}_{_psrc.stem}_validated.elf"
                        _hex = f"{_prog}_hw{hw}_{tag}_{_psrc.stem}_validated.hex"
                        _ok2 = (
                            subprocess.call(
                                [
                                    "riscv32-unknown-elf-gcc",
                                    "-march=rv32imc_zicsr",
                                    "-mabi=ilp32",
                                    "-static",
                                    "-nostdlib",
                                    "-nostartfiles",
                                    "-T",
                                    "link_cv32e40p.ld",
                                    "-Wl,--gc-sections",
                                    "-o",
                                    _elf,
                                    "crt0_cv32e40p.S",
                                    str(_patched),
                                ],
                                timeout=30,
                                cwd=str(_bm_dir),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            == 0
                        )
                        if _ok2:
                            subprocess.call(
                                [
                                    "riscv32-unknown-elf-objcopy",
                                    "-O",
                                    "verilog",
                                    str(_bm_dir / _elf),
                                    str(_bm_dir / _hex),
                                ],
                                timeout=10,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                        if _ok2:
                            _new_hex = str(_bm_dir / _hex)
                            sim_v = verification.run_post_pruning_check(
                                cfg,
                                ctx,
                                label=f"{tag}_{_psrc.stem}_validated",
                                explicit_hex=_new_hex,
                            )
                            if (
                                sim_v
                                and sim_v.test_passed
                                and sim_v.total_cycles < _best_val_cycles
                            ):
                                _best_val_cycles = sim_v.total_cycles
                                ctx.hwloop_only_hex_path = _new_hex
                                ctx.hwloop_only_elf_path = str(_bm_dir / _elf)
                                setattr(ctx, f"sim_{tag}", sim_v)
                                print_info(
                                    f"  {tag}: PASS ({sim_v.total_cycles:,} cycles, {len(_valid)} loops, {_psrc.stem})"
                                )
            ctx.hex_hwloop_pruned = ctx.hwloop_only_hex_path

    def _run_step5(label_suffix=""):
        tag = f"all{label_suffix}"
        hw = changeset.hw_loop_count
        print_section(f"STEP 5{label_suffix.upper()}: ALL (HW_LOOP={hw}, fused+hwloop+pruned)")

        # Compute prune config from the actual fused+hwloop ELF that will run
        hwloop_elf = ctx.hwloop_elf_path
        if hwloop_elf and os.path.exists(str(hwloop_elf)):
            from arvis.pipeline.pruning import compute_prune_config

            saved = ctx.fused_elf_path
            ctx.fused_elf_path = str(hwloop_elf)
            pc_all, used_all = compute_prune_config(cfg, ctx, verbose=False)
            ctx.fused_elf_path = saved
            changeset.add_prune_config(pc_all, used_all)

        changeset._apply_label = tag
        changeset.apply(cfg, ctx)
        rtl_dirs[tag] = os.path.join(ctx.rtl_output_dir, "rtl")
        step5_hex = ctx.hwloop_hex_path
        if label_suffix:
            ctx.hex_all_perf = step5_hex
        else:
            ctx.hex_all = step5_hex
        verification.run_synth_step(cfg, ctx, label=tag, prune_config=changeset.prune_config)
        sim5 = verification.run_post_pruning_check(cfg, ctx, label=tag, explicit_hex=step5_hex)
        # Retry with pre-validation if failed
        if not (sim5 and sim5.test_passed):
            from arvis.cli import print_info
            from arvis.pipeline.hwloop import (
                FUSED_IMAGE,
                _assemble_patched,
                _patch_asm,
                _prevalidate_loops,
            )

            _bm_dir = Path(cfg.benchmark_dir).resolve()
            _prog = cfg.benchmark_name.replace("embench_", "")
            _enc_fused = getattr(ctx, "_hwlp_enc_fused", None)
            _fused_merged = _bm_dir / f"{_prog}_merged.s"
            _rtl_dir = rtl_dirs.get(tag)
            if _fused_merged.exists() and _rtl_dir:
                print_info(f"  {tag}: FAIL/TIMEOUT — running per-loop pre-validation")
                from arvis.pipeline.verification import _make_sim_cfg
                from arvis.simulation.verilator_runner import VerilatorRunner

                _sim_dir = os.path.join(cfg.output_dir, f"sim_preval_{tag}")
                _vr = VerilatorRunner(_make_sim_cfg(cfg))
                _TB = {"COREV_PULP", "FPU", "NUM_MHPMCOUNTERS", "HW_LOOP", "COREV_CLUSTER", "ZFINX"}
                _fl = [
                    f
                    for f in getattr(ctx, "verilator_extra_flags", [])
                    if any(f.startswith(f"-G{p}=") for p in _TB)
                ]
                _ok, _sim_bin, _ = _vr.build_sim(
                    rtl_dir=_rtl_dir, output_dir=_sim_dir, extra_flags=_fl
                )
                if _ok and _sim_bin:
                    _valid = _prevalidate_loops(
                        _fused_merged,
                        hw,
                        _bm_dir,
                        _sim_bin,
                        "link_cv32e40p.ld",
                        "crt0_cv32e40p.S",
                        encoding=_enc_fused,
                        image=FUSED_IMAGE,
                        specializer_dir=Path(__file__).resolve().parent.parent,
                        fifo_depth=changeset.prefetch_fifo_depth or 2,
                    )
                    if _valid:
                        _out = f"{_prog}_hw{hw}_{tag}_validated.s"
                        _patched, _ = _patch_asm(
                            _fused_merged,
                            _out,
                            hw,
                            _bm_dir,
                            encoding=_enc_fused,
                            valid_loops=_valid,
                            fifo_depth=changeset.prefetch_fifo_depth or 2,
                        )
                        if _patched:
                            _elf = f"{_prog}_hw{hw}_{tag}_validated.elf"
                            _hex = f"{_prog}_hw{hw}_{tag}_validated.hex"
                            _ok2 = _assemble_patched(
                                Path(__file__).resolve().parent.parent,
                                _bm_dir,
                                _patched,
                                _elf,
                                _hex,
                                image=FUSED_IMAGE,
                            )
                            if _ok2:
                                ctx.hwloop_hex_path = str(_bm_dir / _hex)
                                ctx.hwloop_elf_path = str(_bm_dir / _elf)
                                step5_hex = ctx.hwloop_hex_path
                                sim5b = verification.run_post_pruning_check(
                                    cfg, ctx, label=f"{tag}_validated", explicit_hex=step5_hex
                                )
                                if sim5b and sim5b.test_passed:
                                    print_info(
                                        f"  {tag}: PASS after pre-validation ({sim5b.total_cycles:,} cycles, {len(_valid)} loops)"
                                    )
                                    setattr(ctx, f"sim_{tag}", sim5b)
            if label_suffix:
                ctx.hex_all_perf = ctx.hwloop_hex_path
            else:
                ctx.hex_all = ctx.hwloop_hex_path

    def _build_sim_from_rtl(rtl_dir: str, sim_label: str) -> str | None:
        """Build Verilator sim from an explicit RTL directory."""
        from arvis.cli import print_error, print_info

        if not os.path.exists(rtl_dir):
            print_error(f"RTL dir not found: {rtl_dir}")
            return None
        from arvis.pipeline.verification import _make_sim_cfg
        from arvis.simulation.verilator_runner import VerilatorRunner

        sc = sim_cache.get_or_create(
            RTLConfig(name=sim_label, has_fused=False, has_hwloop=False, rtl_dir=rtl_dir),
            changeset.hw_loop_count,
        )
        if sc.sim_bin:
            print_info(f"  Using cached sim: {sc.sim_bin}")
            return sc.sim_bin
        sc.resolve_sim_dir(cfg.output_dir)
        print_info(f"  Building sim from {rtl_dir}...")
        if os.path.exists(sc.sim_dir):
            import shutil as _sh

            _sh.rmtree(sc.sim_dir, ignore_errors=True)
        runner_v = VerilatorRunner(_make_sim_cfg(cfg))
        _TB = {"COREV_PULP", "FPU", "NUM_MHPMCOUNTERS", "HW_LOOP", "COREV_CLUSTER", "ZFINX"}
        flags = [
            f
            for f in getattr(ctx, "verilator_extra_flags", [])
            if any(f.startswith(f"-G{p}=") for p in _TB)
        ]
        ok, sim_bin, _ = runner_v.build_sim(
            rtl_dir=rtl_dir, output_dir=sc.sim_dir, extra_flags=flags
        )
        if ok:
            sc.sim_bin = sim_bin
            print_info(f"  Sim built: {sim_bin}")
            return sim_bin
        print_error(f"  Sim build FAILED for {rtl_dir}")
        return None

    def _run_exhaustive_selection(
        label_tag, *, rtl_hwonly: str | None = None, rtl_all: str | None = None
    ):
        """Run per-loop selection. Requires explicit RTL dirs.

        Args:
            rtl_hwonly: RTL dir for HWLoop+Pruned exhaustive (no fused).
            rtl_all:    RTL dir for All exhaustive (fused+hwloop).
        """
        if not cfg.exhaustive_hwloop:
            return
        has_hl = ctx.hwloop_hex_path is not None
        has_hl_only = ctx.hwloop_only_hex_path is not None
        if not has_hl and not has_hl_only:
            print_section("GA SKIPPED — no hwloop hex available")
            return

        from arvis.config import BENCHMARKS
        from arvis.pipeline.hwloop import (
            FUSED_IMAGE,
            _assemble_patched,
            _select_beneficial_loops,
        )

        bm_info = BENCHMARKS.get(cfg.benchmark_name, {})
        bm_dir = Path(cfg.benchmark_dir).resolve()
        specializer_dir = Path(__file__).resolve().parent.parent
        prog_name = cfg.benchmark_name.replace("embench_", "")
        hw_val = changeset.hw_loop_count
        link_script = "link_cv32e40p.ld"
        crt0_file = "crt0_cv32e40p.S"
        extra_ld = bm_info.get("extra_ldflags", "").split() or None
        tag = label_tag.replace(" ", "_").lower()
        hwlp_enc = getattr(changeset, "_hwlp_encoding", None) or getattr(
            ctx, "_hwlp_encoding", None
        )
        hwlp_enc_fused = getattr(ctx, "_hwlp_enc_fused", hwlp_enc)
        hwlp_enc_plain = getattr(ctx, "_hwlp_enc_plain", hwlp_enc)

        # ── Sweep-framework path (Phase 6.7) ──────────────────────
        if cfg.use_pipeline_runner and has_fusion and has_hl and rtl_all:
            _result = _loop_selection_via_sweep(
                cfg=cfg,
                ctx=ctx,
                bm_dir=bm_dir,
                prog_name=prog_name,
                hw_val=hw_val,
                rtl_all=rtl_all,
                label_tag=label_tag,
                encoding=hwlp_enc_fused,
                image=FUSED_IMAGE,
                specializer_dir=specializer_dir,
                fifo_depth=changeset.prefetch_fifo_depth or 2,
                build_sim_fn=_build_sim_from_rtl,
            )
            if _result:
                return

        # GA optimization on "All" (fused+hwloop+pruned) — uses sweep RTL
        # Run on BOTH nounroll and unroll merged sources, pick best
        if has_fusion and has_hl and rtl_all:
            best_ga_hex = None
            best_ga_elf = None
            best_ga_cycles = float("inf")

            for variant, src_name in [
                ("nounroll", f"{prog_name}_merged.s"),
                ("unroll", f"{prog_name}_merged_unroll.s"),
            ]:
                fused_src = bm_dir / src_name
                if not fused_src.exists():
                    continue
                print_section(f"EXHAUSTIVE LOOP SELECTION — All ({label_tag}, {variant})")
                sim_bin = _build_sim_from_rtl(rtl_all, f"exhaust_all_{tag}_{variant}")
                if not sim_bin:
                    continue

                # Find candidate for this variant to pass merge info
                _cand_for_variant = None
                for _c in ctx.hwloop_candidates:
                    if getattr(_c, "fused_src", None) == str(fused_src):
                        _cand_for_variant = _c
                        break

                # Pass cached valid loops from sweep prevalidation if available
                _cached_valid = (
                    getattr(_cand_for_variant, "_valid_loops", None) if _cand_for_variant else None
                )

                result = _select_beneficial_loops(
                    fused_src,
                    hw_val,
                    bm_dir,
                    sim_bin,
                    link_script,
                    crt0_file,
                    f"{prog_name}_hw{hw_val}_fused_sel_{tag}_{variant}.s",
                    image=FUSED_IMAGE,
                    specializer_dir=specializer_dir,
                    encoding=hwlp_enc_fused,
                    valid_loops=_cached_valid,
                    fifo_depth=changeset.prefetch_fifo_depth or 2,
                )
                if result[0]:
                    sel_elf = f"{prog_name}_hw{hw_val}_sel_{tag}_{variant}.elf"
                    sel_hex = f"{prog_name}_hw{hw_val}_sel_{tag}_{variant}.hex"
                    ok = _assemble_patched(
                        specializer_dir, bm_dir, result[0], sel_elf, sel_hex, image=FUSED_IMAGE
                    )
                    if ok:
                        # Measure cycles using the SAME sim the GA used (sweep RTL)
                        import re as _re_ga

                        _ga_r = subprocess.run(
                            [sim_bin, f"+firmware={bm_dir / sel_hex}", "+maxcycles=20000000"],
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        _ga_cycles = None
                        for _gl in _ga_r.stdout.splitlines():
                            _gm = _re_ga.search(r"after (\d+) cycles", _gl)
                            if _gm and "SUCCESS" in _gl:
                                _ga_cycles = int(_gm.group(1))
                                break
                        if _ga_cycles and _ga_cycles < best_ga_cycles:
                            # Test merged function benefit before accepting
                            from arvis.pipeline.hwloop import _test_merged_functions

                            if _cand_for_variant and getattr(
                                _cand_for_variant, "_replaced_fns", None
                            ):
                                optimized = _test_merged_functions(
                                    _cand_for_variant,
                                    bm_dir,
                                    sim_bin,
                                    _ga_cycles,
                                    hw_val,
                                    encoding=hwlp_enc_fused,
                                    image=FUSED_IMAGE,
                                    specializer_dir=specializer_dir,
                                )
                                if optimized:
                                    opt_elf = f"{prog_name}_hw{hw_val}_opt_{tag}_{variant}.elf"
                                    opt_hex = f"{prog_name}_hw{hw_val}_opt_{tag}_{variant}.hex"
                                    ok2 = _assemble_patched(
                                        specializer_dir,
                                        bm_dir,
                                        optimized,
                                        opt_elf,
                                        opt_hex,
                                        image=FUSED_IMAGE,
                                    )
                                    if ok2:
                                        _opt_r = subprocess.run(
                                            [
                                                sim_bin,
                                                f"+firmware={bm_dir / opt_hex}",
                                                "+maxcycles=20000000",
                                            ],
                                            capture_output=True,
                                            text=True,
                                            timeout=120,
                                        )
                                        for _ol in _opt_r.stdout.splitlines():
                                            _om = _re_ga.search(r"after (\d+) cycles", _ol)
                                            if _om and "SUCCESS" in _ol:
                                                _opt_cycles = int(_om.group(1))
                                                if _opt_cycles < _ga_cycles:
                                                    _ga_cycles = _opt_cycles
                                                    sel_hex = opt_hex
                                                    sel_elf = opt_elf
                                                break

                            best_ga_cycles = _ga_cycles
                            best_ga_hex = str(bm_dir / sel_hex)
                            best_ga_elf = str(bm_dir / sel_elf)
                            from arvis.cli import print_info

                            print_info(f"  GA {variant}: {best_ga_cycles:,} cycles ← new best")

            if best_ga_hex:
                ctx.hwloop_hex_path = best_ga_hex
                ctx.hwloop_elf_path = best_ga_elf
                ctx.fused_hex_path = best_ga_hex
                ctx.fused_elf_path = best_ga_elf

    if sweep_winners and sweep_winners.best_adp > 0:
        _set_candidate(
            sweep_winners.best_adp, getattr(sweep_winners.best_adp_result, "_candidate", None)
        )
        has_hwloop = ctx.hwloop_hex_path is not None
        has_hwloop_only = ctx.hwloop_only_hex_path is not None

        # Build step 4 RTL first (hwloop_pruned — no fused, all loops patched)
        hw_adp = sweep_winners.best_adp
        if has_hwloop_only:
            _run_step4()

        # GA optimization only on "All" (fused+hwloop+pruned) — the final solution
        # Use the sweep RTL for best_adp — same RTL as the final All step
        sweep_rtl = getattr(sweep_winners.best_adp_result, "_rtl_dir", None)
        if not sweep_rtl:
            sweep_rtl = os.path.join(cfg.output_dir, f"rtl_hwloop_sweep_{hw_adp}_0", "rtl")
        if has_fusion and has_hwloop and sweep_rtl:
            _run_exhaustive_selection("best_adp", rtl_hwonly=None, rtl_all=sweep_rtl)

        # Run step 5 with optimized hex
        if has_fusion and has_hwloop:
            _run_step5()

        # If best cycles is different, also run step 4/5 for it (no GA — only best_adp gets GA)
        if not sweep_winners.same and sweep_winners.best_cycles > 0:
            hw_perf = sweep_winners.best_cycles
            _set_candidate(hw_perf, getattr(sweep_winners.best_cycles_result, "_candidate", None))
            suffix = "_best_perf"
            if ctx.hwloop_only_hex_path:
                _run_step4(suffix)
            # GA for best_perf too
            sweep_rtl_perf = getattr(sweep_winners.best_cycles_result, "_rtl_dir", None)
            if not sweep_rtl_perf:
                sweep_rtl_perf = os.path.join(
                    cfg.output_dir, f"rtl_hwloop_sweep_{hw_perf}_0", "rtl"
                )
            if has_fusion and ctx.hwloop_hex_path:
                _run_exhaustive_selection("best_perf", rtl_hwonly=None, rtl_all=sweep_rtl_perf)
            if has_fusion and ctx.hwloop_hex_path:
                _run_step5(suffix)
    else:
        # No sweep or no valid results — run with whatever hwloop.py set
        if has_hwloop_only:
            _run_step4()
        # GA optimization only on "All"
        sweep_rtl_default = None
        sweep_dirs = getattr(ctx, "_sweep_rtl_dirs", {})
        if sweep_dirs:
            sweep_rtl_default = next(iter(sweep_dirs.values()), None)
        if has_fusion and has_hwloop and sweep_rtl_default:
            _run_exhaustive_selection("default", rtl_hwonly=None, rtl_all=sweep_rtl_default)
        if has_fusion and has_hwloop:
            _run_step5()

    verification.print_final_summary(cfg, ctx)


def _sweep_hwloop_candidates(
    cfg: ToolConfig,
    ctx: PipelineContext,
    changeset,
    prune_config_orig,
    all_used_orig: set,
):
    """Evaluate each HW_LOOP candidate exactly like step 5 (All).

    For each candidate: set hw_loop_count on changeset, point ctx at
    candidate hex, apply full changeset, sim + synth.

    Phase 5 refactor
    ----------------
    The selection logic now goes through the unified
    :class:`strategies.sweep.HWLoopDepthSweep`: we pre-compute
    per-candidate metrics in this function (the dual-compile +
    sim + synth + retry paths are deeply target-specific and
    stay here), then hand the resulting metrics table to the
    sweep, which selects the lowest-ADP depth via the same code
    path used by the prefetch FIFO sweep.

    The legacy two-winner output (``best_adp`` + ``best_cycles``)
    is reconstructed from ``SweepDecision.all_results`` so the
    runner's downstream code keeps working unchanged.

    Returns SweepWinners (best ADP + best cycles), or None if no
    candidates.
    """
    from arvis.cli import print_info, print_section
    from arvis.pipeline.hwloop_sweep import SweepWinners

    candidates = ctx.hwloop_candidates
    if len(candidates) <= 1:
        if candidates:
            hw = candidates[0].hw_loop
            print_info(f"Single HW_LOOP candidate: {hw}")
            return SweepWinners(best_adp=hw, best_cycles=hw, same=True)
        return None

    print_section("HW_LOOP SWEEP (evaluating candidates with pruned RTL)")
    results = []
    saved_hw = changeset.hw_loop_count
    saved_hex, saved_elf = ctx.fused_hex_path, ctx.fused_elf_path
    # Protect results from earlier steps — sweep labels fall through to these
    saved_sim_pruned = ctx.sim_pruned
    saved_synth = ctx.synth_comparison

    # Phase 6 gateway: when ``--use-pipeline-runner`` is set, the
    # per-candidate sim+synth runs through
    # :class:`HWLoopVariantEvaluator`.  When unset (the default),
    # the legacy inline loop runs unchanged.
    use_pipeline_runner = os.environ.get("ARVIS_USE_PIPELINE_RUNNER") == "1" or bool(
        getattr(cfg, "use_pipeline_runner", False)
    )

    if use_pipeline_runner:
        print_info(
            "[pipeline-runner] HW_LOOP sweep using HWLoopVariantEvaluator + Pipeline._emit_variant"
        )
        results = _evaluate_hwloop_via_pipeline(
            cfg, ctx, changeset, candidates, prune_config_orig, all_used_orig
        )
    else:
        results = _evaluate_hwloop_via_legacy(
            cfg, ctx, changeset, candidates, prune_config_orig, all_used_orig
        )

    # Restore
    changeset.hw_loop_count = saved_hw
    ctx.fused_hex_path, ctx.fused_elf_path = saved_hex, saved_elf
    ctx.sim_pruned = saved_sim_pruned
    ctx.synth_comparison = saved_synth

    # Phase 5: hand off to the unified sweep framework for selection.
    # The dual-compile + per-candidate sim + synth above produced
    # the metrics table; the framework now picks the winner.
    winners = _select_hwloop_winners(results)

    # Stash full sweep results on ctx for HTML report observability (Phase 9).
    ctx.hwloop_sweep_results = results  # type: ignore[attr-defined]
    ctx.hwloop_winners = winners  # type: ignore[attr-defined]

    return winners


def _evaluate_hwloop_via_pipeline(
    cfg, ctx, changeset, candidates, prune_config_orig, all_used_orig
):
    """Phase 6 gateway: evaluate HW_LOOP candidates via the
    unified :class:`HWLoopVariantEvaluator` + ``Pipeline._emit_variant``.

    Differs from the legacy inline loop in that:
      * RTL emission goes through ``Pipeline._emit_variant``.
      * Simulation goes through :class:`VerilatorVerifier`.
      * Synthesis goes through :class:`YosysSynthesisFlow`.

    The per-candidate dual-compile (Docker / GCC ELF generation)
    is upstream of this; the candidate is expected to carry its
    own ``fused_hex`` / ``fused_elf`` paths.
    """
    from arvis.cli import print_info

    # Build the Pipeline + verifier + synth wiring once.
    from arvis.core.pipeline import Pipeline
    from arvis.core.sweep import SweepCandidate
    from arvis.pipeline.hwloop_sweep import HWLoopSweepResult
    from arvis.simulation.verifier import VerilatorVerifier
    from arvis.synthesis.yosys_flow import YosysSynthesisFlow
    from arvis.targets import CV32E40P
    from arvis.targets.cv32e40p.sweep_evaluators import HWLoopVariantEvaluator

    target = CV32E40P()
    verifier = VerilatorVerifier(
        rtl_root=cfg.rtl_root,
        sim_timeout_seconds=cfg.sim_timeout_seconds,
        verilator_bin="verilator",
        extra_flags=getattr(ctx, "verilator_extra_flags", None),
    )
    synth = YosysSynthesisFlow()
    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[],
        verifier=verifier,
        synthesis=synth,
        variants=[],
    )
    sweep_dir = Path(cfg.output_dir) / "hwloop_sweep_via_pipeline"
    sweep_dir.mkdir(parents=True, exist_ok=True)

    # Map candidate -> hex.  The legacy candidate stores the
    # path on cand.fused_hex.
    cand_by_depth = {c.hw_loop: c for c in candidates}

    def hex_for(sweep_cand: SweepCandidate) -> str:
        depth = int(sweep_cand["HW_LOOP"])
        legacy_cand = cand_by_depth.get(depth)
        if legacy_cand is None or not legacy_cand.fused_hex:
            raise FileNotFoundError(f"no hex for HW_LOOP={depth}")
        return str(legacy_cand.fused_hex)

    evaluator = HWLoopVariantEvaluator(
        target=target,
        pipeline=pipeline,
        output_dir=sweep_dir,
        hex_path_for=hex_for,
        base_decisions={},  # Phase 6.1: no Prune/Fusion injection yet
    )

    results = []
    for cand in candidates:
        if not cand.fused_hex:
            results.append(
                HWLoopSweepResult(hw_loop=cand.hw_loop, loops_patched=cand.loops_patched)
            )
            continue

        sweep_cand = SweepCandidate(overrides={"HW_LOOP": cand.hw_loop})
        metrics = evaluator(sweep_cand)

        r = HWLoopSweepResult(hw_loop=cand.hw_loop, loops_patched=cand.loops_patched)
        r.cycles = int(metrics.get("cycles", 0))
        r.cells = int(metrics.get("cells", 0))
        r.passed = bool(metrics.get("passed", 0))
        r._candidate = cand  # type: ignore[attr-defined]  # store for downstream consumers

        status = f"{r.cycles:,} cycles, {r.cells:,} cells" if r.passed else "FAIL"
        print_info(f"HW_LOOP={cand.hw_loop}: {cand.loops_patched} loops, {status}")
        results.append(r)

    return results


def _evaluate_hwloop_via_legacy(cfg, ctx, changeset, candidates, prune_config_orig, all_used_orig):
    """Legacy per-candidate evaluation loop (the body of
    ``_sweep_hwloop_candidates`` before Phase 6).

    Kept verbatim here so that ``--use-pipeline-runner=False`` (the
    default) reproduces the production behaviour the 21-benchmark
    sweep depends on.
    """
    from arvis.cli import print_info
    from arvis.pipeline import verification
    from arvis.pipeline.hwloop_sweep import HWLoopSweepResult

    results = []

    for cand_idx, cand in enumerate(candidates):
        if not cand.fused_hex:
            results.append(
                HWLoopSweepResult(hw_loop=cand.hw_loop, loops_patched=cand.loops_patched)
            )
            continue

        # Exactly like step 5: swap hw_loop + hex on the SAME changeset, apply
        changeset.hw_loop_count = cand.hw_loop
        ctx.fused_hex_path = cand.fused_hex
        ctx.fused_elf_path = cand.fused_elf

        # Recompute prune config for THIS candidate's ELF
        if cand.fused_elf and os.path.exists(cand.fused_elf):
            from arvis.pipeline.pruning import compute_prune_config

            _saved_elf = ctx.fused_elf_path
            ctx.fused_elf_path = cand.fused_elf
            pc, used = compute_prune_config(cfg, ctx, verbose=False)
            ctx.fused_elf_path = _saved_elf
            changeset.add_prune_config(pc, used)
        else:
            changeset.add_prune_config(copy.deepcopy(prune_config_orig), set(all_used_orig))

        changeset._apply_label = f"hwloop_sweep_{cand.hw_loop}_{cand_idx}"
        changeset.apply(cfg, ctx, verbose=False)

        # Record RTL dir for reuse by exhaustive search
        sweep_rtl_dir = os.path.join(ctx.rtl_output_dir, "rtl")
        if not hasattr(ctx, "_sweep_rtl_dirs"):
            ctx._sweep_rtl_dirs = {}
        ctx._sweep_rtl_dirs[f"hwloop_sweep_{cand.hw_loop}_{cand_idx}"] = sweep_rtl_dir

        r = HWLoopSweepResult(hw_loop=cand.hw_loop, loops_patched=cand.loops_patched)
        r._rtl_dir = sweep_rtl_dir
        r._candidate = cand  # store for GA reuse
        label = f"hwloop_sweep_{cand.hw_loop}_{cand_idx}"

        synth = verification.run_synth_step(
            cfg, ctx, label=label, prune_config=changeset.prune_config
        )
        if synth and hasattr(synth, "pruned") and synth.pruned.success:
            r.cells = synth.pruned.cells

        sim_result = verification.run_post_pruning_check(cfg, ctx, label=label)
        if sim_result and sim_result.test_passed:
            r.cycles = sim_result.total_cycles
            r.passed = True
        else:
            # Retry with pre-validation
            from arvis.config import BENCHMARKS
            from arvis.pipeline.hwloop import (
                FUSED_IMAGE,
                _assemble_patched,
                _patch_asm,
                _prevalidate_loops,
            )

            bm_info = BENCHMARKS.get(cfg.benchmark_name, {})
            bm_dir_sw = Path(cfg.benchmark_dir).resolve()
            prog = cfg.benchmark_name.replace("embench_", "")
            enc_fused = getattr(ctx, "_hwlp_enc_fused", None)
            sim_bin = None
            if sweep_rtl_dir:
                from arvis.pipeline.verification import _make_sim_cfg
                from arvis.simulation.verilator_runner import VerilatorRunner

                _sim_dir = os.path.join(cfg.output_dir, f"sim_preval_{label}")
                vr = VerilatorRunner(_make_sim_cfg(cfg))
                _TB = {"COREV_PULP", "FPU", "NUM_MHPMCOUNTERS", "HW_LOOP", "COREV_CLUSTER", "ZFINX"}
                _fl = [
                    f
                    for f in getattr(ctx, "verilator_extra_flags", [])
                    if any(f.startswith(f"-G{p}=") for p in _TB)
                ]
                ok, sim_bin, _ = vr.build_sim(
                    rtl_dir=sweep_rtl_dir, output_dir=_sim_dir, extra_flags=_fl
                )
                if not ok:
                    sim_bin = None
            fused_merged = (
                Path(cand.fused_src) if cand.fused_src else bm_dir_sw / f"{prog}_merged.s"
            )
            if sim_bin and fused_merged.exists():
                print_info(f"  {label}: FAIL — running per-loop pre-validation")
                _baseline_src = (
                    Path(cand._standard_src) if getattr(cand, "_standard_src", None) else None
                )
                valid = _prevalidate_loops(
                    fused_merged,
                    cand.hw_loop,
                    bm_dir_sw,
                    sim_bin,
                    "link_cv32e40p.ld",
                    "crt0_cv32e40p.S",
                    encoding=enc_fused,
                    image=FUSED_IMAGE,
                    specializer_dir=Path(__file__).resolve().parent.parent,
                    baseline_src=_baseline_src,
                )
                cand._valid_loops = valid  # cache for GA reuse
                if valid:
                    out_name = f"{prog}_hw{cand.hw_loop}_{label}_validated.s"
                    patched, _ = _patch_asm(
                        fused_merged,
                        out_name,
                        cand.hw_loop,
                        bm_dir_sw,
                        encoding=enc_fused,
                        valid_loops=valid,
                    )
                    if patched:
                        elf_n = f"{prog}_hw{cand.hw_loop}_{label}_validated.elf"
                        hex_n = f"{prog}_hw{cand.hw_loop}_{label}_validated.hex"
                        ok = _assemble_patched(
                            Path(__file__).resolve().parent.parent,
                            bm_dir_sw,
                            patched,
                            elf_n,
                            hex_n,
                            image=FUSED_IMAGE,
                        )
                        if ok:
                            new_hex = str(bm_dir_sw / hex_n)
                            sim2 = verification.run_post_pruning_check(
                                cfg, ctx, label=f"{label}_validated", explicit_hex=new_hex
                            )
                            if sim2 and sim2.test_passed:
                                r.cycles = sim2.total_cycles
                                r.passed = True
                                cand.fused_hex = new_hex
                                cand.fused_elf = str(bm_dir_sw / elf_n)
                                print_info(
                                    f"  {label}: PASS after pre-validation ({r.cycles:,} cycles, {len(valid)} loops)"
                                )

        results.append(r)
        status = f"{r.cycles:,} cycles, {r.cells:,} cells" if r.passed else "FAIL"
        print_info(f"HW_LOOP={cand.hw_loop}: {cand.loops_patched} loops, {status}")

    return results


def _select_hwloop_winners(results):
    """Translate per-candidate :class:`HWLoopSweepResult` records
    into a :class:`SweepWinners` (best ADP + best cycles).

    Phase 5 refactor: routes selection through
    :class:`strategies.sweep.HWLoopDepthSweep` so the same code
    path handles both the prefetch FIFO sweep and the HW_LOOP
    sweep.  ``SweepDecision.best`` gives the ADP winner; the
    cycles winner is a separate scan over ``all_results``.
    """
    from arvis.cli import print_warning
    from arvis.pipeline.hwloop_sweep import SweepWinners
    from arvis.strategies.sweep import HWLoopDepthSweep

    # Build the metrics table the sweep evaluator expects.  We
    # filter to passing candidates because non-passing entries
    # are recorded as `passed=0` for the framework to skip during
    # selection (and printed as ``FAIL`` in the legacy table).
    candidates_by_depth: dict[int, dict[str, float]] = {}
    for r in results:
        candidates_by_depth[r.hw_loop] = {
            "cycles": float(r.cycles),
            "cells": float(r.cells),
            "passed": 1.0 if r.passed else 0.0,
        }

    if not candidates_by_depth:
        print_warning("No HW_LOOP candidates evaluated")
        return SweepWinners()

    sweep = HWLoopDepthSweep(candidates_by_depth=candidates_by_depth)
    decision = sweep.analyze(
        workload=None,  # type: ignore[arg-type]  # legacy table-lookup evaluator ignores
        profile=None,  # type: ignore[arg-type]   # legacy table-lookup evaluator ignores
        target=None,  # type: ignore[arg-type]    # legacy table-lookup evaluator ignores
    )

    winners = SweepWinners()

    # Best by ADP comes from the framework's selection.
    if decision.best is not None:
        adp_depth = int(decision.best.candidate["HW_LOOP"])
        winners.best_adp = adp_depth
        # Find the matching legacy result record.
        adp_result = next((r for r in results if r.hw_loop == adp_depth and r.passed), None)
        winners.best_adp_result = adp_result

        # Best by cycles is a separate scan: the framework's cost
        # function is ADP, but we surface the cycles winner too.
        passing = [r for r in results if r.passed and r.cells > 0]
        if passing:
            by_cycles = min(passing, key=lambda r: r.cycles)
            winners.best_cycles = by_cycles.hw_loop
            winners.best_cycles_result = by_cycles
            winners.same = winners.best_adp == winners.best_cycles
    else:
        print_warning("No valid HW_LOOP results — disabling")

    # Print the legacy table format (mirrors hwloop_sweep.pick_best
    # output so log scrapers and reports keep working).
    _print_hwloop_sweep_table(results, winners)

    return winners


def _print_hwloop_sweep_table(results, winners):
    """Reproduce the legacy HW_LOOP sweep table format."""
    print(f"\n  {'HW_LOOP':>7s}  {'Loops':>5s}  {'Cycles':>12s}  {'Cells':>8s}  {'ADP':>10s}")
    print(f"  {'─' * 7}  {'─' * 5}  {'─' * 12}  {'─' * 8}  {'─' * 10}")
    for r in results:
        if not r.passed:
            print(f"  {r.hw_loop:>7d}  {r.loops_patched:>5d}  {'FAIL':>12s}")
            continue
        markers = []
        if r.hw_loop == winners.best_adp:
            markers.append("best ADP")
        if r.hw_loop == winners.best_cycles:
            markers.append("best cycles")
        tag = f" ◀ {', '.join(markers)}" if markers else ""
        print(
            f"  {r.hw_loop:>7d}  {r.loops_patched:>5d}  {r.cycles:>12,}  "
            f"{r.cells:>8,}  {r.adp:>10.2f}{tag}"
        )

    if winners.best_adp == 0:
        return
    if winners.same:
        print(f"\n  → HW_LOOP={winners.best_adp}: best ADP and best cycles")
    else:
        print(
            f"\n  → HW_LOOP={winners.best_adp} (best ADP), "
            f"HW_LOOP={winners.best_cycles} (best cycles)"
        )
