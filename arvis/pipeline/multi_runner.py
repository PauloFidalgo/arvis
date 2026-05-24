"""
Multi-program (suite) pipeline runner.

Analyzes each program independently, merges results, generates a single
specialized RTL that serves all programs in the suite.
"""

from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_suite_pipeline(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Run the full specialization pipeline for a multi-program suite."""
    from arvis.cli import print_section
    from arvis.pipeline.rtl_changeset import RTLChangeSet

    os.makedirs(cfg.output_dir, exist_ok=True)
    programs = cfg.suite_programs

    print_section(f"MULTI-PROGRAM SUITE: {cfg.benchmark_name} ({len(programs)} programs)")

    changeset = RTLChangeSet()

    # ── Phase 1+2: Per-program analysis ──
    if "analysis" in cfg.enabled_phases:
        _run_multi_analysis(cfg, ctx)

    # ── Phase 5: Fusion (multi-program static passes → merge → Docker) ──
    if "fusion" in cfg.enabled_phases:
        _run_multi_fusion(cfg, ctx, changeset)

    # ── Phase 4: HW Loop (per-program LTO compile → patch → assemble) ──
    if "fusion" in cfg.enabled_phases:
        _run_multi_hwloop(cfg, ctx, changeset)

    # ── Phase 3: Pruning (union of all used instructions) ──
    if "pruning" in cfg.enabled_phases:
        _run_multi_pruning(cfg, ctx, changeset)

    # ── Verification: simulate each program on the single RTL ──
    if "pruning" in cfg.enabled_phases:
        _run_multi_verification(cfg, ctx, changeset)

    # ── Report ──
    print_section("SUITE RESULTS")
    _print_suite_summary(cfg, ctx)


# ---------------------------------------------------------------------------
# Phase 1+2: Per-program analysis
# ---------------------------------------------------------------------------


def _run_multi_analysis(cfg: "ToolConfig", ctx: "PipelineContext") -> Dict[str, "PipelineContext"]:
    """Run profiling + selection for each program, merge into ctx."""
    from arvis.cli import print_metric, print_section
    from arvis.pipeline import profiling, selection
    from arvis.pipeline.context import PipelineContext as Ctx

    per_program: Dict[str, Ctx] = {}

    for prog in cfg.suite_programs:
        name = prog["name"]
        print_section(f"ANALYZING: {name}")
        child_cfg = cfg.for_program(prog)
        child_ctx = Ctx()
        child_ctx.use_trace = os.path.exists(child_cfg.trace_path)
        profiling.run(child_cfg, child_ctx)
        selection.run(child_cfg, child_ctx)
        per_program[name] = child_ctx

    # Merge fusions: union, sum frequencies
    print_section("MERGING ANALYSIS RESULTS")
    fusion_map: dict = {}
    for name, pctx in per_program.items():
        for f in pctx.all_fusions:
            key = f.signature
            if key in fusion_map:
                fusion_map[key].frequency += f.frequency
            else:
                fusion_map[key] = copy.deepcopy(f)
    ctx.all_fusions = sorted(fusion_map.values(), key=lambda c: c.frequency, reverse=True)

    # Merge ALU usage
    merged_counts: Dict[str, int] = {}
    for pctx in per_program.values():
        if pctx.alu_usage:
            for mnem, cnt in pctx.alu_usage.instruction_counts.items():
                merged_counts[mnem] = merged_counts.get(mnem, 0) + cnt
    for pctx in per_program.values():
        if pctx.alu_usage:
            ctx.alu_usage = copy.deepcopy(pctx.alu_usage)
            ctx.alu_usage.instruction_counts = merged_counts
            break

    print_metric("Programs analyzed", str(len(per_program)))
    print_metric("Unique fusion candidates", str(len(ctx.all_fusions)))
    if ctx.alu_usage:
        print_metric("Union of mnemonics", str(len(ctx.alu_usage.instruction_counts)))

    # Store per-program contexts for later use
    ctx._per_program = per_program  # type: ignore[attr-defined]
    return per_program


# ---------------------------------------------------------------------------
# Phase 5: Multi-program fusion
# ---------------------------------------------------------------------------


def _run_multi_fusion(cfg: "ToolConfig", ctx: "PipelineContext", changeset) -> None:
    """Run static fusion passes for each program, merge, build one Docker."""
    import subprocess

    from arvis.cli import print_error, print_info, print_section, print_success, print_warning
    from arvis.pipeline import fusion_rtl
    from arvis.pipeline.gcc_compile import (
        MERGED_IMAGE,
        STATIC_PASSES,
        GCCCompileResult,
        UsedCustomInstruction,
        _build_custom_gcc,
        _docker_image_exists,
        _get_specializer_dir,
        _inspect_binary_with_md,
        _used_to_encoding_tuples,
    )

    print_section("MULTI-PROGRAM FUSION: Static passes")

    specializer_dir = _get_specializer_dir(cfg)
    result = GCCCompileResult()
    all_pass_used: List[Tuple[str, str, List[UsedCustomInstruction]]] = []

    for md_filename, image_name, label, description in STATIC_PASSES:
        md_path = os.path.join(specializer_dir, "tools", md_filename)
        if not os.path.exists(md_path):
            print_warning(f"  {label} .md not found — skipping")
            continue

        print(f"\n  ══ {label}: {description} ══")

        # Ensure Docker image exists
        if not _docker_image_exists(image_name):
            print(f"  Building '{image_name}'...")
            ok = _build_custom_gcc(cfg, Path(md_path), image_name=image_name)
            if not ok:
                print_warning(f"{label} Docker build failed — skipping")
                continue
        else:
            print_success(f"'{image_name}' exists")

        # Compile ALL embench programs at once via make
        abs_spec = os.path.abspath(specializer_dir)
        bm_rel = cfg.benchmark_dir  # e.g. targets/benchmarks/embench
        _RISCV = "/opt/riscv"

        # Preserve original (non-fused) hex/elf before Docker overwrites them
        for prog in cfg.suite_programs:
            for ext in ("elf", "hex"):
                orig = os.path.join(cfg.benchmark_dir, prog[ext])
                saved = orig + ".baseline"
                if os.path.exists(orig) and not os.path.exists(saved):
                    shutil.copy2(orig, saved)

        compile_cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{abs_spec}:/work",
            "-w",
            f"/work/{bm_rel}",
            image_name,
            "make",
            "-j4",
            "clean",
            "all",
            f"CC={_RISCV}/bin/riscv32-unknown-elf-gcc",
            f"OBJCOPY={_RISCV}/bin/riscv32-unknown-elf-objcopy",
            "EXTRA_CFLAGS=-mcustom-fused",
        ]

        print(f"  Compiling all programs with {image_name}...")
        ret = subprocess.call(compile_cmd, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ret != 0:
            print_warning(f"  {label}: make failed (exit {ret})")
            continue

        # Inspect each program's ELF
        merged_used: Dict[str, UsedCustomInstruction] = {}
        for prog in cfg.suite_programs:
            name = prog["name"]
            elf_path = os.path.join(cfg.benchmark_dir, prog["elf"])
            if not os.path.exists(elf_path):
                continue

            used = _inspect_binary_with_md(cfg, elf_path, md_path, image=image_name)
            n_inst = sum(u.count for u in used)
            if used:
                print_info(f"  {name}: {len(used)} patterns, {n_inst} instances")

            for u in used:
                key = u.pattern_name or f"{u.opcode}_{u.funct3}_{u.funct7}"
                if key in merged_used:
                    merged_used[key].count += u.count
                else:
                    merged_used[key] = copy.deepcopy(u)

        pass_used = sorted(merged_used.values(), key=lambda x: -x.count)
        all_pass_used.append((label, md_path, pass_used))

        n = len(pass_used)
        t = sum(u.count for u in pass_used)
        print(f"  {label} total: {n} unique patterns, {t} instances")

    total_found = sum(len(u) for _, _, u in all_pass_used)
    total_instances = sum(sum(x.count for x in u) for _, _, u in all_pass_used)
    print(f"\n  Pass summary: {total_found} unique patterns, {total_instances} instances")

    if total_found == 0:
        print_warning("No fusion patterns found — skipping Docker merge")
        ctx.gcc_compile_result = result
        return

    # ── HC patterns from merged profiling ──
    hc_candidates = getattr(ctx, "all_fusions", None) or []
    n_with_hc = sum(1 for c in hc_candidates if getattr(c, "has_hardcoded_imm", False))
    print(f"\n  HC candidates: {len(hc_candidates)} ({n_with_hc} with hardcoded imm)")

    # ── Merge all passes into one .md ──
    print("\n  ══ MERGE: Combining used patterns ══")
    from arvis.codegen.gcc.peephole_gen import generate_merged_md

    pass_encodings = []
    for label, md_path, used in all_pass_used:
        pass_encodings.append((md_path, _used_to_encoding_tuples(used)))

    merged_md_path = str(Path(cfg.output_dir) / "custom-fused.md")
    content, n_merged = generate_merged_md(
        pass_used_list=pass_encodings,
        hc_candidates=hc_candidates if n_with_hc > 0 else None,
        output_path=merged_md_path,
    )
    print(f"  Merged .md: {merged_md_path} ({n_merged} patterns)")

    if n_merged == 0:
        print_warning("No patterns in merged .md")
        ctx.gcc_compile_result = result
        return

    # ── Build merged Docker ──
    print("\n  ══ Building merged Docker ══")
    ok = _build_custom_gcc(cfg, Path(merged_md_path), image_name=MERGED_IMAGE)
    if not ok:
        print_error("Merged Docker build failed")
        ctx.gcc_compile_result = result
        return

    # ── Compile each program with merged Docker ──
    print("\n  ══ Compiling all programs with merged GCC ══")
    abs_spec = os.path.abspath(specializer_dir)
    bm_rel = cfg.benchmark_dir
    _RISCV = "/opt/riscv"

    # Preserve originals before overwrite
    for prog in cfg.suite_programs:
        for ext in ("elf", "hex"):
            orig = os.path.join(cfg.benchmark_dir, prog[ext])
            saved = orig + ".baseline"
            if os.path.exists(orig) and not os.path.exists(saved):
                shutil.copy2(orig, saved)

    compile_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{abs_spec}:/work",
        "-w",
        f"/work/{bm_rel}",
        MERGED_IMAGE,
        "make",
        "-j4",
        "clean",
        "all",
        f"CC={_RISCV}/bin/riscv32-unknown-elf-gcc",
        f"OBJCOPY={_RISCV}/bin/riscv32-unknown-elf-objcopy",
        "EXTRA_CFLAGS=-mcustom-fused",
    ]

    ret = subprocess.call(compile_cmd, timeout=300)
    if ret != 0:
        print_error("Final compilation failed")
        ctx.gcc_compile_result = result
        return

    # Collect fused ELFs/hexs
    fused_elfs: Dict[str, str] = {}
    fused_hexs: Dict[str, str] = {}
    for prog in cfg.suite_programs:
        name = prog["name"]
        elf_src = os.path.join(cfg.benchmark_dir, prog["elf"])
        hex_src = os.path.join(cfg.benchmark_dir, prog["hex"])
        if os.path.exists(elf_src):
            elf_dst = os.path.join(cfg.output_dir, f"{name}_fused.elf")
            hex_dst = os.path.join(cfg.output_dir, f"{name}_fused.hex")
            shutil.copy2(elf_src, elf_dst)
            if os.path.exists(hex_src):
                shutil.copy2(hex_src, hex_dst)
            fused_elfs[name] = elf_dst
            fused_hexs[name] = hex_dst
            print_success(f"  {name}: {elf_dst}")

    ctx._fused_elfs = fused_elfs  # type: ignore[attr-defined]
    ctx._fused_hexs = fused_hexs  # type: ignore[attr-defined]
    ctx.gcc_compile_result = result

    # Compute filtered fused ops for RTL
    # Inspect all fused ELFs to find which custom instructions are actually used
    all_fused_used: Dict[str, UsedCustomInstruction] = {}
    for name, elf_path in fused_elfs.items():
        used = _inspect_binary_with_md(cfg, elf_path, merged_md_path, image=MERGED_IMAGE)
        for u in used:
            key = u.pattern_name or f"{u.opcode}_{u.funct3}_{u.funct7}"
            if key in all_fused_used:
                all_fused_used[key].count += u.count
            else:
                all_fused_used[key] = copy.deepcopy(u)

    result.used_instructions = list(all_fused_used.values())
    result.unique_patterns = len(all_fused_used)
    result.total_fused_count = sum(u.count for u in all_fused_used.values())

    if result.used_instructions:
        # Run fusion_rtl to generate FusedOperation objects from ctx.all_fusions
        # This populates ctx._fusion_rtl_fused_ops needed by compute_filtered_ops
        fusion_rtl.run(cfg, ctx)

        fused_ops = fusion_rtl.compute_filtered_ops(cfg, ctx)
        if fused_ops:
            changeset.add_fused_operations(fused_ops)
            ctx._rtl_changeset_fused_ops = fused_ops  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Phase 4: Multi-program HW Loop
# ---------------------------------------------------------------------------


def _run_multi_hwloop(cfg: "ToolConfig", ctx: "PipelineContext", changeset) -> None:
    """Run hwloop analysis per program, pick best depth from union."""
    import glob as _glob
    import subprocess

    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.cli import print_info, print_section, print_success, print_warning
    from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator
    from arvis.pipeline.hwloop_sweep import HWLoopCandidate

    print_section("MULTI-PROGRAM HW LOOP")

    bm_dir = Path(cfg.benchmark_dir)
    cc = cfg.riscv_gcc or "riscv32-unknown-elf-gcc"
    objcopy = cfg.riscv_objcopy or "riscv32-unknown-elf-objcopy"

    # Makefile knows the source files per program — extract from it
    makefile_text = (bm_dir / "Makefile").read_text()

    # Step 1: LTO compile each program to .s, detect eligible loops
    total_eligible = 0
    program_asm: Dict[str, Path] = {}  # name → LTO .s path

    for prog in cfg.suite_programs:
        name = prog["name"]
        elf_path = bm_dir / prog["elf"]
        if not elf_path.exists():
            continue

        # LTO compile to get single .s
        lto_elf = f"/tmp/_hwlp_{name}.elf"
        # Clean stale
        for f in _glob.glob(f"{lto_elf}*"):
            Path(f).unlink(missing_ok=True)

        # Extract source files for this program from Makefile
        import re

        m = re.search(rf"^src_{re.escape(name)}\s*:=\s*(.+)$", makefile_text, re.MULTILINE)
        if not m:
            print_warning(f"  {name}: no sources in Makefile")
            continue
        src_files = m.group(1).strip().split()
        # Resolve relative to bm_dir
        src_resolved = []
        for s in src_files:
            s = s.replace("$(EMBENCH)", str(bm_dir / "../../embench-iot"))
            src_resolved.append(s)

        cmd = [
            cc,
            "-march=rv32imc_zicsr",
            "-mabi=ilp32",
            "-O2",
            "-static",
            "-nostdlib",
            "-nostartfiles",
            "-I.",
            "-I../../embench-iot/support",
            "-DHAVE_BOARDSUPPORT_H",
            "-DHAVE_CONFIG_H",
            "-DWARMUP_HEAT=1",
            "-DGLOBAL_SCALE_FACTOR=1",
            "-ffunction-sections",
            "-fdata-sections",
            f"-I../../embench-iot/src/{name}",
            "-T",
            "link_cv32e40p.ld",
            "-Wl,--gc-sections",
            "-save-temps",
            "-o",
            lto_elf,
            "crt0_cv32e40p.S",
            "boardsupport.c",
            "libc_stubs.c",
            "../../embench-iot/support/main.c",
            "../../embench-iot/support/beebsc.c",
            *src_resolved,
        ]
        ret = subprocess.call(cmd, timeout=120, cwd=str(bm_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ret != 0:
            print_warning(f"  {name}: LTO compile failed")
            continue

        lto_s_files = _glob.glob(f"{lto_elf}.ltrans*.s")
        if not lto_s_files:
            print_warning(f"  {name}: no LTO .s produced")
            continue

        lto_s = Path(lto_s_files[0])
        det = AsmLoopDetector(str(lto_s))
        det.find_all_loops()
        eligible = sum(1 for l in det.all_loops if l.hw_eligible)
        total_eligible += eligible

        if eligible > 0:
            program_asm[name] = lto_s
            print_info(f"  {name}: {eligible} eligible loops")
        else:
            print_info(f"  {name}: no eligible loops")

    if total_eligible == 0:
        print_info("No eligible HW loops across any program — HW_LOOP=0")
        changeset.hw_loop_count = 0
        return

    # Step 2: Determine candidates (union of depths needed)
    from arvis.pipeline.hwloop_sweep import get_candidates

    all_candidates = set()
    for name, asm_path in program_asm.items():
        cands = get_candidates(str(asm_path))
        all_candidates |= set(cands)

    if not all_candidates:
        all_candidates = {1}
    candidates = sorted(all_candidates)
    print_info(f"HW_LOOP candidates: {candidates}")

    # Step 3: Patch + assemble each program for each candidate
    max_cnt_width = 10  # default

    for hw_val in candidates:
        cand = HWLoopCandidate(hw_loop=hw_val)
        total_patched = 0

        for prog in cfg.suite_programs:
            name = prog["name"]
            if name not in program_asm:
                continue

            lto_s = program_asm[name]
            det = AsmLoopDetector(str(lto_s))
            det.find_all_loops()

            gen = HWLoopGenerator(det, hw_loop=hw_val)
            gen.generate()
            patcher = AsmPatcher(gen, lto_s.read_text())
            patched = patcher.patch()
            stats = patcher.get_stats()
            total_patched += stats.get("loops_patched", 0)

            # Write patched .s
            patched_s = bm_dir / f"{name}_hw{hw_val}_patched.s"
            patched_s.write_text(patched)

            # Assemble
            elf_name = f"{name}_hw{hw_val}_hwonly.elf"
            hex_name = f"{name}_hw{hw_val}_hwonly.hex"
            asm_cmd = [
                cc,
                "-march=rv32imc_zicsr",
                "-mabi=ilp32",
                "-static",
                "-nostdlib",
                "-nostartfiles",
                "-T",
                "link_cv32e40p.ld",
                "-Wl,--gc-sections",
                "-o",
                elf_name,
                "crt0_cv32e40p.S",
                str(patched_s.name),
            ]
            ret = subprocess.call(
                asm_cmd,
                timeout=60,
                cwd=str(bm_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ret != 0:
                continue
            hex_cmd = [objcopy, "-O", "verilog", elf_name, hex_name]
            subprocess.call(
                hex_cmd,
                timeout=30,
                cwd=str(bm_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        cand.loops_patched = total_patched
        # Collect hex paths for all programs
        cand.plain_hex = str(bm_dir)  # directory — verification iterates
        ctx.hwloop_candidates.append(cand)
        print_info(f"  HW_LOOP={hw_val}: {total_patched} loops patched across all programs")

    # Analyze counter width from all patched assemblies
    from arvis.pipeline.hwloop_sweep import analyze_addr_width, analyze_counter_width

    for name, asm_path in program_asm.items():
        best_hw = candidates[-1]
        cw = analyze_counter_width(str(asm_path), best_hw)
        max_cnt_width = max(max_cnt_width, cw)

    # Analyze address width from compiled fused ELFs (one per program). The
    # widest binary across the whole set wins, since the same RTL must
    # accommodate all of them.
    max_addr_width = 12
    for elf_path in fused_elfs.values():
        if elf_path and Path(elf_path).exists():
            max_addr_width = max(max_addr_width, analyze_addr_width(str(elf_path)))

    # PC pipeline width — same logic as analyze_addr_width but for the
    # main PC (pc_if/pc_id/pc_q/branch_addr_n/mepc/uepc) rather than the
    # hwloop registers. We narrow once to the widest binary so the same
    # RTL fits every variant emitted by the multi-program build.
    try:
        from arvis.pipeline.pc_width import analyze_pc_width
        max_pc_width = 0
        for elf_path in fused_elfs.values():
            if elf_path and Path(elf_path).exists():
                max_pc_width = max(max_pc_width, analyze_pc_width(str(elf_path)))
        if max_pc_width > 0:
            changeset.pc_width = max_pc_width
            print_info(f"PC width: {max_pc_width} bits (auto-tuned)")
    except ImportError:
        pass  # arvis.pipeline.pc_width not importable; leave changeset.pc_width = 0

    # Pick best candidate
    valid = [c for c in ctx.hwloop_candidates if c.loops_patched > 0]
    if valid:
        best = max(valid, key=lambda c: c.loops_patched)
        changeset.hw_loop_count = best.hw_loop
        changeset.hw_loop_cnt_width = max_cnt_width
        changeset.hw_loop_addr_width = max_addr_width
        # Set hwloop_only paths for verification
        ctx.hwloop_only_hex_path = str(bm_dir)  # marker — verification uses per-program
        print_success(f"Best HW_LOOP={best.hw_loop}, CNT_WIDTH={max_cnt_width}, HWLP_ADDR_WIDTH={max_addr_width}")
    else:
        changeset.hw_loop_count = 0
        print_info("No loops patched — HW_LOOP=0")


# ---------------------------------------------------------------------------
# Phase 3: Multi-program pruning
# ---------------------------------------------------------------------------


def _run_multi_pruning(cfg: "ToolConfig", ctx: "PipelineContext", changeset) -> None:
    """Compute prune config from union of all programs' instructions."""
    from arvis.analysis.alu_usage import ALUUsageAnalyzer
    from arvis.analysis.disassembler import RISCVDisassembler
    from arvis.analysis.instruction_usage import analyze_binary
    from arvis.analysis.mul_usage import analyze_mul_usage
    from arvis.cli import print_info, print_metric, print_section
    from arvis.codegen.rtl.rtl_pruning import PruneConfig

    print_section("MULTI-PROGRAM PRUNING")

    objdump = cfg.riscv_objdump or "riscv32-unknown-elf-objdump"
    disasm = RISCVDisassembler(objdump)
    alu_analyzer = ALUUsageAnalyzer(objdump)

    # Analyze each program's ELF (use fused ELF if available)
    all_used: Set[str] = set()
    all_registers: Set[int] = {0, 2}
    all_alu_removable: Optional[Set[str]] = None
    all_mul_removable: Optional[Set[str]] = None

    fused_elfs = getattr(ctx, "_fused_elfs", {})

    for prog in cfg.suite_programs:
        name = prog["name"]
        elf_path = fused_elfs.get(name, f"{cfg.benchmark_dir}/{prog['elf']}")

        if not os.path.exists(elf_path):
            print_info(f"  {name}: ELF not found, skipping")
            continue

        instructions = disasm.disassemble(elf_path)
        alu_usage = alu_analyzer.analyze_instructions(instructions)
        used = set(alu_usage.instruction_counts.keys())
        all_used |= used

        # Registers
        for inst in instructions:
            for reg in [inst.rd, inst.rs1, inst.rs2]:
                if reg and reg.startswith("x"):
                    try:
                        all_registers.add(int(reg[1:]))
                    except ValueError:
                        pass

        # ALU removable: intersection (only remove what ALL programs don't use)
        removable = set(alu_usage.removable_ops)
        if all_alu_removable is None:
            all_alu_removable = removable
        else:
            all_alu_removable &= removable

        # MUL removable: intersection
        mul_usage = analyze_mul_usage(instructions, corev_pulp=0)
        if mul_usage.removable_modes:
            removable_mul = set(mul_usage.removable_modes)
            if all_mul_removable is None:
                all_mul_removable = removable_mul
            else:
                all_mul_removable &= removable_mul

        print_info(f"  {name}: {len(used)} mnemonics, {len(removable)} removable ALU ops")

    # Also analyze hwloop ELFs if they exist
    hw_val = changeset.hw_loop_count if hasattr(changeset, "hw_loop_count") else 0
    if hw_val > 0:
        for prog in cfg.suite_programs:
            name = prog["name"]
            hwlp_elf = os.path.join(cfg.benchmark_dir, f"{name}_hw{hw_val}_hwonly.elf")
            if os.path.exists(hwlp_elf):
                instructions = disasm.disassemble(hwlp_elf)
                alu_usage = alu_analyzer.analyze_instructions(instructions)
                all_used |= set(alu_usage.instruction_counts.keys())

    print_metric("Union of mnemonics", str(len(all_used)))
    print_metric("Registers used", f"{len(all_registers)}/32")
    print_metric("ALU ops removable", str(len(all_alu_removable or set())))

    # Build PruneConfig
    workload_profile = None
    # Try to get a workload profile from any program
    for prog in cfg.suite_programs:
        elf_path = fused_elfs.get(prog["name"], f"{cfg.benchmark_dir}/{prog['elf']}")
        if os.path.exists(elf_path):
            instructions = disasm.disassemble(elf_path)
            workload_profile = analyze_binary(instructions)
            break

    mul_usage_final = None
    for prog in cfg.suite_programs:
        elf_path = fused_elfs.get(prog["name"], f"{cfg.benchmark_dir}/{prog['elf']}")
        if os.path.exists(elf_path):
            instructions = disasm.disassemble(elf_path)
            mul_usage_final = analyze_mul_usage(instructions, corev_pulp=0)
            if all_mul_removable is not None:
                mul_usage_final.removable_modes = list(all_mul_removable)
            break

    prune_config = PruneConfig.from_analysis(
        alu_removable_ops=all_alu_removable or set(),
        used_instructions=all_used,
        used_csrs=set(),
        used_registers=all_registers,
        mul_usage=mul_usage_final,
        workload_profile=workload_profile,
    )

    if cfg.prune_rf_read_c:
        prune_config.enable_regfile_rd_c = False
    if cfg.prune_rf_write_b:
        prune_config.enable_regfile_wr_b = False

    changeset.add_prune_config(prune_config, all_used)
    ctx.prune_config = prune_config
    ctx._all_used_suite = all_used  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Verification: simulate each program on the single RTL
# ---------------------------------------------------------------------------


def _run_multi_verification(cfg: "ToolConfig", ctx: "PipelineContext", changeset) -> None:
    """Build single RTL, sweep HW_LOOP candidates, simulate each program's hex."""
    from arvis.cli import print_error, print_info, print_section, print_success
    from arvis.pipeline.verification import _make_sim_cfg, run_synth_step
    from arvis.simulation.verilator_runner import VerilatorRunner

    print_section("MULTI-PROGRAM VERIFICATION")

    candidates = ctx.hwloop_candidates
    hw_values = [c.hw_loop for c in candidates if c.loops_patched > 0] if candidates else []
    if not hw_values:
        hw_values = [0]  # No hwloop — just pruned

    # Include HW_LOOP=0 as baseline for comparison
    if 0 not in hw_values:
        hw_values = [0] + hw_values

    best_adp = None
    best_hw = 0
    best_results = {}
    sweep_data = []

    for hw_val in hw_values:
        print_section(f"SWEEP: HW_LOOP={hw_val}")

        # Apply RTL with this HW_LOOP value
        changeset.hw_loop_count = hw_val
        if ctx.prune_config:
            ctx.prune_config.hw_loop = hw_val
        changeset.apply(cfg, ctx, verbose=(hw_val == hw_values[0]))

        # Synthesize
        synth = run_synth_step(cfg, ctx, label=f"sweep_hw{hw_val}", prune_config=changeset.prune_config)
        cells = 0
        if synth and hasattr(synth, "pruned") and synth.pruned.success:
            cells = synth.pruned.cells

        # Build simulator
        sim_dir = os.path.join(cfg.output_dir, f"sim_hw{hw_val}")
        _TB_PARAMS = {"COREV_PULP", "FPU", "NUM_MHPMCOUNTERS", "HW_LOOP", "COREV_CLUSTER", "ZFINX"}
        all_flags = getattr(ctx, "verilator_extra_flags", [])
        extra_flags = [f for f in all_flags if any(f.startswith(f"-G{p}=") for p in _TB_PARAMS)]

        runner = VerilatorRunner(_make_sim_cfg(cfg))
        ok, sim_bin, build_out = runner.build_sim(
            rtl_dir=os.path.join(ctx.rtl_output_dir, "rtl"),
            output_dir=sim_dir,
            extra_flags=extra_flags,
        )
        if not ok:
            print_error(f"  Verilator build failed for HW_LOOP={hw_val}")
            continue

        # Simulate each program
        hw_results: Dict[str, dict] = {}
        total_cycles = 0
        all_pass = True

        for prog in cfg.suite_programs:
            name = prog["name"]

            # Choose hex: hwloop > fused > baseline
            fused_hexs = getattr(ctx, "_fused_hexs", {})
            if hw_val > 0:
                hex_path = os.path.join(cfg.benchmark_dir, f"{name}_hw{hw_val}_hwonly.hex")
                if not os.path.exists(hex_path):
                    hex_path = fused_hexs.get(name, "")
            else:
                hex_path = fused_hexs.get(name, "")

            if not hex_path or not os.path.exists(hex_path):
                baseline = os.path.join(cfg.benchmark_dir, prog["hex"] + ".baseline")
                hex_path = baseline if os.path.exists(baseline) else os.path.join(cfg.benchmark_dir, prog["hex"])

            if not os.path.exists(hex_path):
                continue

            sim_result = runner.run_sim(sim_bin, hex_path)
            cycles = sim_result.cycles if hasattr(sim_result, "cycles") else 0
            success = sim_result.success if hasattr(sim_result, "success") else False
            hw_results[name] = {"cycles": cycles, "success": success, "hex": hex_path}
            if success and cycles > 0:
                total_cycles += cycles
            else:
                all_pass = False

            status = "✓" if success else "✗"
            print(f"  {status} {name:20s} {cycles:>12,} cycles")

        # Compute ADP
        adp = cells * total_cycles if cells > 0 and total_cycles > 0 else float("inf")
        sweep_data.append(
            {
                "hw_loop": hw_val,
                "cells": cells,
                "total_cycles": total_cycles,
                "adp": adp,
                "all_pass": all_pass,
                "results": hw_results,
            }
        )
        print_info(f"  HW_LOOP={hw_val}: {cells:,} cells, {total_cycles:,} total cycles, ADP={adp:,.0f}")

        if all_pass and (best_adp is None or adp < best_adp):
            best_adp = adp
            best_hw = hw_val
            best_results = hw_results

    # Set the winner
    changeset.hw_loop_count = best_hw
    if ctx.prune_config:
        ctx.prune_config.hw_loop = best_hw
    ctx._suite_results = best_results  # type: ignore[attr-defined]
    ctx._sweep_data = sweep_data  # type: ignore[attr-defined]
    print_success(f"Best HW_LOOP={best_hw} (ADP={best_adp:,.0f})")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_suite_summary(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Print per-program results and geometric mean."""
    import math

    suite_results = getattr(ctx, "_suite_results", {})
    if not suite_results:
        print("  No simulation results available")
        return

    print(f"\n  {'Program':20s} {'Cycles':>12s} {'Status':>8s}")
    print(f"  {'─' * 20} {'─' * 12} {'─' * 8}")

    log_sum = 0
    n = 0
    for name, r in sorted(suite_results.items()):
        status = "PASS" if r["success"] else "FAIL"
        print(f"  {name:20s} {r['cycles']:>12,} {status:>8s}")
        if r["success"] and r["cycles"] > 0:
            log_sum += math.log(r["cycles"])
            n += 1

    if n > 0:
        geomean = math.exp(log_sum / n)
        print(f"\n  Geometric mean: {geomean:,.0f} cycles ({n} programs)")
