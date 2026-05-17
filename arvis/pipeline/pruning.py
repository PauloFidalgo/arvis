"""
Phase 3: RTL Pruning - Computation Only.

Computes the PruneConfig and used instruction set from workload analysis.
No file I/O - all RTL modifications are deferred to RTLChangeSet.apply().

When a fused binary exists (from GCC compile), it is the SOLE source of
truth for registers, instructions, and ALU ops - because that is the
only binary that will actually execute on the specialized core.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Set, Tuple

if TYPE_CHECKING:
    from arvis.codegen.rtl.rtl_pruning import PruneConfig
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


def compute_prune_config(
    cfg: "ToolConfig",
    ctx: "PipelineContext",
    *,
    verbose: bool = True,
) -> Tuple["PruneConfig", Set[str]]:
    """Compute pruning decisions without touching any files.

    When a fused binary exists, its instruction set and register usage
    are the SOLE inputs to pruning. The original/verilator ELFs are
    ignored because only the fused binary will run on the core.

    Returns:
        (prune_config, all_used_instructions)
    """
    from arvis.codegen.rtl.rtl_pruning import PruneConfig

    _print = print if verbose else (lambda *a, **k: None)

    from arvis.cli import print_section

    _print_section = print_section if verbose else (lambda *a, **k: None)
    _print_section("PHASE 3: PRUNING ANALYSIS (computation only)")

    # Determine the FINAL binary that will run on the core.
    # If a fused binary exists, it supersedes everything else.
    has_fused = ctx.fused_elf_path and os.path.exists(ctx.fused_elf_path)

    if has_fused:
        _print("\n  Using FUSED binary as sole source of truth")
        _print("  (original/verilator ELFs ignored - only fused binary runs on core)")
        all_used, alu_usage, used_registers, workload_profile, mul_usage = _analyze_final_binary(
            cfg,
            ctx,
            verbose=verbose,
        )
    else:
        _print("\n  No fused binary - using original binary analysis")
        all_used, alu_usage, used_registers, workload_profile, mul_usage = _analyze_original_binaries(
            cfg, ctx, verbose=verbose
        )

    _print(f"  Final instruction set: {len(all_used)} mnemonics")

    # Removable ALU ops
    removable_alu_ops = set(alu_usage.removable_ops)

    # Build PruneConfig
    prune_config = PruneConfig.from_analysis(
        alu_removable_ops=removable_alu_ops,
        used_instructions=all_used,
        used_csrs=set(),
        used_registers=used_registers,
        mul_usage=mul_usage,
        workload_profile=workload_profile,
    )

    # ── Post-fusion mul-mode reconciliation ─────────────────────────
    # The fused binary disassembles to raw `.insn r4 ...` lines, so
    # `analyze_mul_usage` cannot tell that fused custom instructions
    # internally drive MUL_MAC32 / MUL_MSU32.  Re-enable the
    # corresponding hardware paths based on the filtered fusion ops.
    fused_ops = getattr(ctx, "_rtl_changeset_fused_ops", None) or []
    mult_fused_ops = [op for op in fused_ops if getattr(op, "execution_unit", "") == "mult"]
    if mult_fused_ops:
        # Any mult fusion implies the basic 32x32 MAC HW must stay.
        prune_config.enable_mul = True
        # MSU path needed only when at least one fused op reuses MSU32.
        if any(getattr(op, "mult_pattern", "") == "msu" for op in mult_fused_ops):
            prune_config.enable_msu = True
        # The DSP-unfriendly fusions are filtered upstream, so no fused
        # op needs the DOT8/DOT16 datapath.  Keep it pruned.
        prune_config.enable_dot_mul = False
        if "MUL_MSU32" in prune_config.removable_mul_modes:
            prune_config.removable_mul_modes = prune_config.removable_mul_modes - {"MUL_MSU32"}
        _print(
            f"  Fused mult ops keep enable_mul={prune_config.enable_mul}, "
            f"enable_msu={prune_config.enable_msu}, "
            f"enable_dot_mul={prune_config.enable_dot_mul}"
        )

    # Apply CLI hardware resource overrides
    if cfg.prune_rf_read_c:
        prune_config.enable_regfile_rd_c = False
        _print("\n  CLI override: RF read port C -> PRUNE")
    else:
        prune_config.enable_regfile_rd_c = True

    if cfg.prune_rf_write_b:
        prune_config.enable_regfile_wr_b = False
        _print("  CLI override: RF write port B -> PRUNE")
    else:
        prune_config.enable_regfile_wr_b = True

    # Report
    if prune_config.unused_registers:
        n_unused = len(prune_config.unused_registers)
        _print(f"\n  Register file: {n_unused} registers unused (max used: x{prune_config.max_used_register})")
        if n_unused > 0:
            _print(f"    Unused: {sorted(prune_config.unused_registers)}")

    if prune_config.removable_mul_modes:
        _print(f"\n  Multiplier modes removable: {sorted(prune_config.removable_mul_modes)}")

    if prune_config.removable_csr_labels:
        _print(f"\n  CSR labels removable: {len(prune_config.removable_csr_labels)} case items")

    ctx.prune_config = prune_config

    return prune_config, all_used


def _analyze_final_binary(cfg: "ToolConfig", ctx: "PipelineContext", *, verbose: bool = True) -> tuple:
    """Analyze ONLY the fused binary - the sole binary that runs on core."""
    from arvis.analysis.alu_usage import ALUUsageAnalyzer
    from arvis.analysis.disassembler import RISCVDisassembler
    from arvis.analysis.instruction_usage import analyze_binary
    from arvis.analysis.mul_usage import analyze_mul_usage

    _print = print if verbose else (lambda *a, **k: None)
    objdump = cfg.riscv_objdump or "riscv64-unknown-elf-objdump"

    disasm = RISCVDisassembler(objdump)
    fused_instructions = disasm.disassemble(ctx.fused_elf_path)

    alu_analyzer = ALUUsageAnalyzer(objdump)
    alu_usage = alu_analyzer.analyze_instructions(fused_instructions)
    all_used = set(alu_usage.instruction_counts.keys())
    _print(f"  Fused binary instructions: {len(all_used)} mnemonics")

    used_registers: set = set()
    for inst in fused_instructions:
        for reg in [inst.rd, inst.rs1, inst.rs2]:
            if reg and reg.startswith("x"):
                try:
                    used_registers.add(int(reg[1:]))
                except ValueError:
                    pass
    used_registers.add(0)
    used_registers.add(2)
    _print(f"  Fused binary registers: {len(used_registers)}/32 used")

    workload_profile = None
    if fused_instructions:
        workload_profile = analyze_binary(fused_instructions)
        _print("  WorkloadProfile:")
        _print(
            f"    Registers used: {len(workload_profile.used_registers)}/32 "
            f"(unused: {sorted(workload_profile.unused_registers)})"
        )
        _print(f"    CSRs: {sorted(workload_profile.used_csr_names) or 'none'}")
        _print(f"    Required CSRs: {sorted(workload_profile.required_csr_names)}")
        _print(
            f"    MUL: {workload_profile.uses_mul}, "
            f"MULH: {workload_profile.uses_mulh}, "
            f"DIV: {workload_profile.uses_div}"
        )
        used_registers = used_registers | workload_profile.used_registers

    mul_usage = None
    if fused_instructions:
        mul_usage = analyze_mul_usage(fused_instructions, corev_pulp=0)
        if mul_usage.removable_modes:
            _print("\n  Multiplier analysis:")
            _print(f"    Used modes: {sorted(mul_usage.used_modes)}")
            _print(f"    Removable modes: {sorted(mul_usage.removable_modes)}")

    ctx.alu_usage = alu_usage
    return all_used, alu_usage, used_registers, workload_profile, mul_usage


def _analyze_original_binaries(cfg: "ToolConfig", ctx: "PipelineContext", *, verbose: bool = True) -> tuple:
    """Analyze original binary (no fused binary available)."""
    from arvis.analysis.instruction_usage import analyze_binary
    from arvis.analysis.mul_usage import analyze_mul_usage

    _print = print if verbose else (lambda *a, **k: None)

    # Instructions from trace + Spike objdump
    all_used = set(ctx.alu_usage.instruction_counts.keys())
    if ctx.instructions:
        all_used |= {insn.mnemonic for insn in ctx.instructions if insn.mnemonic}
    n_spike = len(all_used)

    # CRITICAL: also analyze the CV32E40P binary (the one that actually runs
    # on Verilator). It may have different crt0/printf with extra instructions
    # (csrrs, csrrw for perf counters, divu/remu for printf number formatting)
    from arvis.analysis.alu_usage import ALUUsageAnalyzer
    from arvis.analysis.disassembler import RISCVDisassembler
    from arvis.config import BENCHMARKS

    bm_info = BENCHMARKS.get(cfg.benchmark_name, {})
    verilator_elf_name = bm_info.get("verilator_elf", "")
    cv32_insns = []
    if verilator_elf_name:
        verilator_elf_path = os.path.join(cfg.benchmark_dir, verilator_elf_name)
        # Prefer the baseline ELF (saved before Docker overwrites it)
        baseline_elf = verilator_elf_path + ".baseline"
        if os.path.exists(baseline_elf):
            verilator_elf_path = baseline_elf
        if os.path.exists(verilator_elf_path):
            objdump = cfg.riscv_objdump or "riscv64-unknown-elf-objdump"
            disasm = RISCVDisassembler(objdump)
            cv32_insns = disasm.disassemble(verilator_elf_path)
            cv32_mnemonics = {insn.mnemonic for insn in cv32_insns if insn.mnemonic}
            extra = cv32_mnemonics - all_used
            if extra:
                _print(f"  [+] CV32E40P binary adds {len(extra)} insns: {sorted(extra)}")
            all_used |= cv32_mnemonics

    _print(
        f"  Original binary instructions: {len(all_used)} mnemonics ({n_spike} from Spike, {len(all_used) - n_spike} from CV32E40P)"  # noqa: E501
    )

    # Recompute ALU usage from the VERILATOR binary (the one that actually
    # runs on the core). This ensures removable_ops reflects what the real
    # binary needs, not just the Spike binary.
    alu_usage = ctx.alu_usage
    if cv32_insns:
        objdump = cfg.riscv_objdump or "riscv64-unknown-elf-objdump"
        alu_analyzer = ALUUsageAnalyzer(objdump)
        alu_usage = alu_analyzer.analyze_instructions(cv32_insns)
        # Also merge Spike instructions so we don't miss anything
        if ctx.instructions:
            spike_alu = ctx.alu_usage
            for op in spike_alu.used_ops:
                if op not in alu_usage.used_ops:
                    alu_usage.used_ops.add(op)
                    alu_usage.removable_ops.discard(op)
        _print(
            f"  ALU usage (from Verilator ELF): {len(alu_usage.used_ops)} used, "
            f"{len(alu_usage.removable_ops)} removable"
        )

    # WorkloadProfile from the VERILATOR binary (preferred) or Spike binary
    workload_profile = None
    combined_insns = cv32_insns if cv32_insns else (ctx.instructions or [])
    if combined_insns:
        workload_profile = analyze_binary(combined_insns)
        # Merge Spike instructions into profile if we used Verilator as base
        if cv32_insns and ctx.instructions:
            spike_profile = analyze_binary(ctx.instructions)
            workload_profile.used_registers |= spike_profile.used_registers
            workload_profile.unused_registers -= spike_profile.used_registers
        _print("\n  WorkloadProfile:")
        _print(
            f"    Registers used: {len(workload_profile.used_registers)}/32 "
            f"(unused: {sorted(workload_profile.unused_registers)})"
        )
        _print(f"    CSRs: {sorted(workload_profile.used_csr_names) or 'none'}")
        _print(f"    Required CSRs: {sorted(workload_profile.required_csr_names)}")
        _print(
            f"    MUL: {workload_profile.uses_mul}, "
            f"MULH: {workload_profile.uses_mulh}, "
            f"DIV: {workload_profile.uses_div}"
        )

    used_registers = None
    if workload_profile:
        used_registers = workload_profile.used_registers

    # Multiplier usage from the VERILATOR binary
    mul_usage = None
    if combined_insns:
        mul_usage = analyze_mul_usage(combined_insns, corev_pulp=0)
        # Merge Spike mul usage
        if cv32_insns and ctx.instructions:
            spike_mul = analyze_mul_usage(ctx.instructions, corev_pulp=0)
            for mode in spike_mul.used_modes:
                if mode not in mul_usage.used_modes:
                    mul_usage.used_modes.add(mode)
                    mul_usage.removable_modes.discard(mode)
        if mul_usage.removable_modes:
            _print("\n  Multiplier analysis:")
            _print(f"    Used modes: {sorted(mul_usage.used_modes)}")
            _print(f"    Removable modes: {sorted(mul_usage.removable_modes)}")

    return all_used, alu_usage, used_registers, workload_profile, mul_usage


def run(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Legacy entry point - computes PruneConfig AND applies RTL changes.

    Exists for backward compatibility with tests and standalone pruning-only
    runs. The main pipeline uses compute_prune_config() + RTLChangeSet.apply().
    """
    from arvis.codegen.rtl.base import RTLWorkspace
    from arvis.codegen.rtl.rtl_pruning import RTLPruner

    prune_config, all_used = compute_prune_config(cfg, ctx)

    # Apply RTL changes directly (legacy path)
    ctx.rtl_output_dir = os.path.join(cfg.output_dir, "rtl_modified")
    ws = RTLWorkspace(cfg.rtl_root, ctx.rtl_output_dir)
    ws.copy()

    pruner = RTLPruner(ws)
    ctx.prune_report = pruner.apply(prune_config)

    print(f"\n  Decoder generation ({len(all_used)} mnemonics):")
    dec_stats = pruner.generate_specialized_decoder(used_instructions=all_used)
    ctx.prune_report.file_stats.append(dec_stats)

    print(f"\n{ctx.prune_report.summary()}")
    ctx.verilator_extra_flags = prune_config.verilator_flags()
