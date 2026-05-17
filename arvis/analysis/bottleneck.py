"""Bottleneck analysis: measure where cycles are lost and recommend optimizations.

Uses the dynamic trace + instruction mix to compute a CPI breakdown and
identify the dominant bottleneck (fetch stalls, branch penalties, load-use
stalls, multi-cycle ops, or instruction count).

The recommendation drives which ARVIS specializations to apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .models import BasicBlock, DynamicProfile, Instruction
from .registers import normalize_mnemonic


@dataclass
class CPIBreakdown:
    """CPI component breakdown from profiling."""

    total_cycles: int = 0
    total_instructions: int = 0
    cpi: float = 0.0

    # Cycle components
    base_cycles: int = 0  # 1 cycle per instruction (ideal)
    branch_penalty: int = 0  # taken branch flush cycles
    load_use_stalls: int = 0  # load-use hazard stalls
    multicycle_extra: int = 0  # mulh/div extra cycles
    fetch_stalls: int = 0  # estimated fetch buffer stalls

    # Instruction mix
    n_loads: int = 0
    n_stores: int = 0
    n_branches: int = 0
    n_mul: int = 0
    n_div: int = 0
    n_alu: int = 0

    # Recommendations (sorted by impact)
    recommendations: List[Tuple[str, int, str]] = field(default_factory=list)


def analyze_bottleneck(
    blocks: Dict[int, BasicBlock],
    profile: DynamicProfile,
    baseline_cycles: int = 0,
) -> CPIBreakdown:
    """Analyze CPI breakdown and identify the dominant bottleneck."""

    result = CPIBreakdown()
    result.total_instructions = profile.total_instructions

    # Count instruction classes weighted by execution frequency
    pc_to_inst: Dict[int, Instruction] = {}
    for block in blocks.values():
        for inst in block.instructions:
            pc_to_inst[inst.address] = inst

    for block in blocks.values():
        freq = profile.block_exec_counts.get(block.id if hasattr(block, "id") else 0, 0)
        if freq == 0:
            # Try matching by first instruction PC
            if block.instructions:
                pc = block.instructions[0].address
                freq = profile.instruction_exec_counts.get(pc, 0)
        if freq == 0:
            continue

        for i, inst in enumerate(block.instructions):
            m = normalize_mnemonic(inst.mnemonic)

            if inst.is_load:
                result.n_loads += freq
            elif inst.is_store:
                result.n_stores += freq
            elif inst.is_branch or inst.is_jump:
                result.n_branches += freq
                # Estimate ~65% taken for loop back-edges
                result.branch_penalty += int(freq * 0.65)
            elif m == "mul":
                result.n_mul += freq
            elif m in ("div", "divu", "rem", "remu"):
                result.n_div += freq
                result.multicycle_extra += freq * 33  # ~34 cycle div
            elif m in ("mulh", "mulhsu", "mulhu"):
                result.n_mul += freq
                result.multicycle_extra += freq * 3  # 4-cycle mulh
            else:
                result.n_alu += freq

            # Load-use stall detection
            if i > 0:
                prev = block.instructions[i - 1]
                if prev.is_load and prev.rd:
                    if prev.rd == inst.rs1 or prev.rd == inst.rs2:
                        result.load_use_stalls += freq

    result.base_cycles = result.total_instructions

    # Fetch stalls = total_cycles - (base + known stalls)
    known_overhead = result.branch_penalty + result.load_use_stalls + result.multicycle_extra

    if baseline_cycles > 0:
        result.total_cycles = baseline_cycles
    else:
        # Estimate: assume CPI ~1.8 if no simulation data
        result.total_cycles = int(result.total_instructions * 1.8)

    result.fetch_stalls = max(0, result.total_cycles - result.base_cycles - known_overhead)

    if result.total_instructions > 0:
        result.cpi = result.total_cycles / result.total_instructions

    # Build recommendations sorted by cycle impact
    recs = []

    if result.fetch_stalls > result.total_cycles * 0.05:
        recs.append(
            (
                "prefetch_buffer",
                result.fetch_stalls,
                f"Increase prefetch FIFO depth (fetch stalls = {result.fetch_stalls:,} cycles, "
                f"{result.fetch_stalls / result.total_cycles * 100:.1f}% of total)",
            )
        )

    # Fusion benefit = instruction count reduction
    # Estimate ~15% of ALU instructions are fusible
    fusion_est = int(result.n_alu * 0.15)
    if fusion_est > 0:
        recs.append(
            (
                "instruction_fusion",
                fusion_est,
                f"ALU instruction fusion (est. {fusion_est:,} cycles from {result.n_alu:,} ALU instructions)",
            )
        )

    if result.branch_penalty > result.total_cycles * 0.01:
        recs.append(
            (
                "hw_loop",
                result.branch_penalty,
                f"Hardware loops (branch penalties = {result.branch_penalty:,} cycles, "
                f"{result.branch_penalty / result.total_cycles * 100:.1f}%)",
            )
        )

    if result.load_use_stalls > result.total_cycles * 0.01:
        recs.append(
            (
                "load_compute_fusion",
                result.load_use_stalls,
                f"Load-compute fusion ({result.load_use_stalls:,} load-use stalls, "
                f"{result.load_use_stalls / result.total_cycles * 100:.1f}%)",
            )
        )

    if result.multicycle_extra > result.total_cycles * 0.01:
        recs.append(
            (
                "div_optimization",
                result.multicycle_extra,
                f"Division optimization ({result.multicycle_extra:,} extra cycles from "
                f"{result.n_div:,} div instructions)",
            )
        )

    if result.n_loads > result.total_instructions * 0.15:
        recs.append(
            (
                "memory_optimization",
                result.n_loads,
                f"Memory optimization ({result.n_loads:,} loads = "
                f"{result.n_loads / result.total_instructions * 100:.1f}% of instructions)",
            )
        )

    recs.sort(key=lambda r: r[1], reverse=True)
    result.recommendations = recs

    return result


def print_bottleneck_report(result: CPIBreakdown) -> None:
    """Print the bottleneck analysis report."""
    print("\n  CPI Analysis:")
    print(f"    Total cycles:      {result.total_cycles:>12,}")
    print(f"    Total instructions:{result.total_instructions:>12,}")
    print(f"    CPI:               {result.cpi:>12.2f}")
    print("")
    print("  Cycle Breakdown:")
    print(f"    Base (1/insn):     {result.base_cycles:>12,}  ({result.base_cycles / result.total_cycles * 100:5.1f}%)")
    print(
        f"    Fetch stalls:      {result.fetch_stalls:>12,}  ({result.fetch_stalls / result.total_cycles * 100:5.1f}%)"
    )
    print(
        f"    Branch penalties:  {result.branch_penalty:>12,}  "
        f"({result.branch_penalty / result.total_cycles * 100:5.1f}%)"
    )
    print(
        f"    Load-use stalls:   {result.load_use_stalls:>12,}  "
        f"({result.load_use_stalls / result.total_cycles * 100:5.1f}%)"
    )
    print(
        f"    Multi-cycle ops:   {result.multicycle_extra:>12,}  "
        f"({result.multicycle_extra / result.total_cycles * 100:5.1f}%)"
    )
    print("")
    print("  Recommendations (by impact):")
    for i, (name, cycles, desc) in enumerate(result.recommendations):
        print(f"    {i + 1}. {desc}")
