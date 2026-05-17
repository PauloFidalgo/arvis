"""
Phase 1: Workload Profiling.

Disassembles the ELF, builds CFG, detects loops, parses execution traces,
analyzes memory access patterns, and determines ALU operation usage.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


from arvis.cli import print_error, print_success, print_warning


def _run_spike_trace(cfg: "ToolConfig") -> None:
    """Run Spike to generate an execution trace if one doesn't exist.

    Uses the benchmark's Spike ELF (linked at 0x80000000) with
    Spike's -l flag to produce a trace log. The entry point is
    read from the ELF header to handle different linker scripts.
    """
    import shutil
    import subprocess

    from arvis.config import BENCHMARKS

    bm = BENCHMARKS.get(cfg.benchmark_name, {})
    spike_elf_name = bm.get("elf", "")

    # Find the Spike ELF
    spike_elf = os.path.join(cfg.benchmark_dir, spike_elf_name)
    if not os.path.exists(spike_elf):
        # Try common names
        for name in [
            f"{cfg.benchmark_name}_spike.elf",
            f"{cfg.benchmark_name}.elf",
        ]:
            candidate = os.path.join(cfg.benchmark_dir, name)
            if os.path.exists(candidate):
                spike_elf = candidate
                break
        else:
            print_warning(f"No Spike ELF found for {cfg.benchmark_name}")
            return

    # Find spike binary
    spike_bin = cfg.spike_bin or shutil.which("spike")
    if not spike_bin:
        print_warning("Spike not found — skipping trace generation")
        return

    # Read ELF entry point to set --pc correctly
    entry_point = "0x80000000"  # default
    try:
        objdump = cfg.riscv_objdump or "riscv64-unknown-elf-objdump"
        readelf = objdump.replace("objdump", "readelf")
        result = subprocess.run(
            [readelf, "-h", spike_elf],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            import re

            m = re.search(r"Entry point address:\s+(0x[0-9a-fA-F]+)", result.stdout)
            if m:
                entry_point = m.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Ensure traces directory exists
    trace_dir = os.path.dirname(cfg.trace_path)
    os.makedirs(trace_dir, exist_ok=True)

    # Run Spike with trace logging
    isa = getattr(cfg, "spike_isa", "rv32imc_zicsr")
    spike_cmd = [
        spike_bin,
        f"--isa={isa}",
        f"--pc={entry_point}",
        "-l",
        f"--log={cfg.trace_path}",
        spike_elf,
    ]

    print(f"  Running Spike: {' '.join(spike_cmd[:4])} ...")
    print(f"  Spike ELF: {spike_elf}")
    print(f"  Entry point: {entry_point}")
    print(f"  Trace output: {cfg.trace_path}")

    try:
        result = subprocess.run(
            spike_cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min max
        )
        if result.returncode != 0 and result.returncode != 1:
            # returncode=1 is normal for tohost exit
            print_warning(f"Spike exited with code {result.returncode}")
            if result.stderr:
                print(f"     {result.stderr[:200]}")

        if os.path.exists(cfg.trace_path):
            size = os.path.getsize(cfg.trace_path)
            print_success(f"Spike trace generated: {size:,} bytes")
        else:
            print_error("Spike trace not generated")
    except subprocess.TimeoutExpired:
        print_warning("Spike timed out after 300s — trace may be incomplete")
        # Kill spike and use partial trace
        if os.path.exists(cfg.trace_path):
            size = os.path.getsize(cfg.trace_path)
            print(f"  Using partial trace: {size:,} bytes")
    except FileNotFoundError:
        print_error(f"Spike binary not found: {spike_bin}")


def _clean_benchmark_build(cfg: "ToolConfig") -> None:
    """Clean benchmark build artifacts to remove stale custom-GCC binaries.

    A previous pipeline run may have compiled the benchmark with
    -mcustom-fused, embedding .insn (custom opcode) instructions.
    The baseline core can't execute these → illegal instruction trap.
    Running make clean ensures a fresh build with standard GCC.
    """
    import subprocess

    benchmark_dir = cfg.benchmark_dir
    makefile = os.path.join(benchmark_dir, "Makefile")

    if not os.path.exists(makefile):
        return

    # Check if ELF exists and contains custom opcodes
    elf_path = cfg.elf_path
    if not os.path.exists(elf_path):
        return

    # Quick check: scan binary for CUSTOM opcode bytes
    # CUSTOM_0=0x0B, CUSTOM_2=0x5B in bits [6:0]
    needs_clean = False
    try:
        objdump = cfg.riscv_objdump or "riscv64-unknown-elf-objdump"
        result = subprocess.run(
            [objdump, "-d", elf_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            # Check for .insn directives (custom opcodes)
            insn_count = result.stdout.count(".insn")
            # Also check for raw CUSTOM opcode patterns
            import re

            custom_pattern = re.compile(
                r"^\s*[0-9a-f]+:\s+[0-9a-f]{6}(?:0b|5b)\s",
                re.MULTILINE,
            )
            custom_matches = len(custom_pattern.findall(result.stdout))
            if insn_count > 0 or custom_matches > 0:
                needs_clean = True
                print(
                    f"  ⚠️  Stale binary detected: {elf_path} contains "
                    f"{insn_count} .insn + {custom_matches} custom opcodes"
                )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Can't check — clean anyway to be safe
        needs_clean = True

    if needs_clean:
        print(f"  Cleaning {benchmark_dir} to remove stale custom-GCC artifacts...")
        try:
            subprocess.run(
                ["make", "-C", benchmark_dir, "clean"],
                capture_output=True,
                timeout=30,
            )
            # Rebuild with standard GCC
            subprocess.run(
                ["make", "-C", benchmark_dir, "all"],
                capture_output=True,
                timeout=120,
            )
            print_success("Benchmark rebuilt with standard GCC")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print_warning("Could not clean benchmark build")


def run(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Execute Phase 1: Workload Profiling."""
    from arvis.analysis.alu_usage import ALUUsageAnalyzer
    from arvis.analysis.cfg import CFGBuilder
    from arvis.analysis.disassembler import RISCVDisassembler
    from arvis.analysis.loops import LoopDetector
    from arvis.analysis.memory import MemoryAnalyzer
    from arvis.analysis.models import DynamicProfile
    from arvis.analysis.profiler import TraceParser
    from arvis.analysis.tracer_parser import TracerParser as SpikeParser
    from arvis.cli import print_metric, print_section, print_step

    print_section("WORKLOAD PROFILING")

    # 0. Clean benchmark build to ensure no stale custom-GCC binaries
    # A previous pipeline run may have compiled with -mcustom-fused,
    # leaving .insn (custom opcodes) in the ELF. The baseline core
    # can't execute these → illegal instruction trap → hang.
    _clean_benchmark_build(cfg)

    # 1. Disassemble
    print_step("1/4", f"Disassembling {cfg.elf_path}")
    disasm = RISCVDisassembler(cfg.riscv_objdump)
    ctx.instructions = disasm.disassemble(cfg.elf_path)
    if not ctx.instructions:
        raise RuntimeError("No instructions found in ELF")

    # 2. CFG + Loops
    print_step("2/4", "Building CFG + detecting loops")
    builder = CFGBuilder(ctx.instructions)
    ctx.blocks = builder.build()
    entry_id = min(ctx.blocks.keys())
    detector = LoopDetector(ctx.blocks, entry_id)
    ctx.loops = detector.find_all_loops()
    print_metric("Basic blocks", str(len(ctx.blocks)))
    print_metric("Natural loops", str(len(ctx.loops)))

    # 3. Profiling (dynamic trace or static estimation)
    print_step("3/4", "Dynamic profiling")

    # Auto-run Spike if trace doesn't exist
    if ctx.use_trace and not os.path.exists(cfg.trace_path):
        _run_spike_trace(cfg)

    trace_profile = None
    if ctx.use_trace and os.path.exists(cfg.trace_path):
        spike_parser = SpikeParser()
        trace_profile = spike_parser.parse_file(
            Path(cfg.trace_path),
            blocks=ctx.blocks,
            store_entries=False,
            progress_every=500_000,
        )
        print(f"\n{spike_parser.get_quality_report(trace_profile)}")

        # Build PC → block mapping
        pc_to_block = {}
        block_starts = set()
        for bid, block in ctx.blocks.items():
            for inst in block.instructions:
                pc_to_block[inst.address] = bid
            if block.instructions:
                block_starts.add(block.instructions[0].address)

        block_exec_counts: dict[int, int] = {}
        for pc, count in trace_profile.pc_exec_counts.items():
            if pc in pc_to_block and pc in block_starts:
                bid = pc_to_block[pc]
                block_exec_counts[bid] = block_exec_counts.get(bid, 0) + count

        register_values = {}
        for key, values in trace_profile.register_values.items():
            if len(key) == 3:
                register_values[key] = values
            elif len(key) == 2:
                pc, reg = key
                if pc in pc_to_block:
                    bid = pc_to_block[pc]
                    block = ctx.blocks[bid]
                    for idx, inst in enumerate(block.instructions):
                        if inst.address == pc:
                            register_values[(bid, idx, reg)] = values
                            break

        ctx.profile = DynamicProfile(
            block_exec_counts=block_exec_counts,
            instruction_exec_counts=dict(trace_profile.pc_exec_counts),
            register_values=register_values,
            total_instructions=trace_profile.total_instructions,
        )
    else:
        static_parser = TraceParser(ctx.blocks)
        ctx.profile = static_parser.estimate_from_static(ctx.blocks)

    # Compute loop hotness
    for loop in ctx.loops:
        dyn = sum(
            ctx.profile.block_exec_counts.get(bid, 0) * len(ctx.blocks[bid].instructions) for bid in loop.body_block_ids
        )
        loop.total_dynamic_instructions = dyn
        loop.hotness_score = dyn / max(ctx.profile.total_instructions, 1)

        header_execs = ctx.profile.block_exec_counts.get(loop.header_block_id, 0)
        entry_count = sum(
            ctx.profile.block_exec_counts.get(p, 0)
            for p in ctx.blocks[loop.header_block_id].predecessors
            if p not in loop.body_block_ids
        )
        loop.trip_count_estimate = header_execs // max(entry_count, 1) if entry_count > 0 else header_execs
    ctx.loops.sort(key=lambda lp: lp.hotness_score, reverse=True)

    # Memory access analysis (inline with profiling)
    if ctx.use_trace and os.path.exists(cfg.trace_path) and trace_profile:
        text_start = min(i.address for i in ctx.instructions) if ctx.instructions else 0
        text_end = max(i.address for i in ctx.instructions) + 4 if ctx.instructions else 0
        mem_analyzer = MemoryAnalyzer(text_start=text_start, text_end=text_end)

        if trace_profile.memory_accesses:
            for pc, addr, is_store in trace_profile.memory_accesses:
                if text_start <= addr < text_end:
                    continue
                if is_store:
                    mem_analyzer.store_addrs.append(addr)
                else:
                    mem_analyzer.load_addrs.append(addr)
            print(f"  Using {len(trace_profile.memory_accesses):,} pre-parsed memory accesses")
        else:
            mem_analyzer.parse_commit_log(cfg.trace_path)

        ctx.mem_analysis = mem_analyzer.analyze()
        if ctx.mem_analysis.total_accesses > 0:
            ma = ctx.mem_analysis
            print_metric("Memory accesses", f"{ma.total_accesses:,}")
            print_metric("Locality score", f"{ma.locality_score:.2f}")

    # 4. ALU usage
    print_step("4/4", "ALU usage analysis")
    alu_analyzer = ALUUsageAnalyzer(cfg.riscv_objdump or "riscv64-unknown-elf-objdump")
    ctx.alu_usage = alu_analyzer.analyze_instructions(ctx.instructions)
    print_metric("ALU ops used", str(len(ctx.alu_usage.used_ops)))
    print_metric("ALU ops removable", str(len(ctx.alu_usage.removable_ops)))
