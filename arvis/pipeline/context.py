"""
Shared pipeline context — accumulates results across phases.

Each phase reads what it needs and writes its results here.
This avoids 10-element return tuples and global mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from arvis.analysis.alu_usage import ALUUsageResult
    from arvis.analysis.fusion import EnhancedFusionCandidate, FusionAnalyzer
    from arvis.analysis.memory import MemoryAnalysis
    from arvis.codegen.rtl.rtl_pruning import PruneConfig, PruneReport
    from arvis.pipeline.gcc_compile import GCCCompileResult
    from arvis.simulation.verilator_runner import SimulationResult
    from arvis.synthesis.yosys_synth import SynthComparison

from arvis.analysis.models import (
    AcceleratorAssessment,
    BasicBlock,
    DynamicProfile,
    Instruction,
    Loop,
)


@dataclass
class PipelineContext:
    """Accumulated results from all pipeline phases."""

    # ── Phase 1: Profiling ──
    instructions: List[Instruction] = field(default_factory=list)
    blocks: Dict[int, BasicBlock] = field(default_factory=dict)
    loops: List[Loop] = field(default_factory=list)
    profile: Optional[DynamicProfile] = None
    use_trace: bool = False
    mem_analysis: Optional[MemoryAnalysis] = None
    alu_usage: Optional[ALUUsageResult] = None

    # ── Phase 2: Selection ──
    all_fusions: List[EnhancedFusionCandidate] = field(default_factory=list)
    ranked_fusions: List[EnhancedFusionCandidate] = field(default_factory=list)
    selected_fusions: List[EnhancedFusionCandidate] = field(default_factory=list)
    optimal_selection: List = field(default_factory=list)
    accel_results: List[AcceleratorAssessment] = field(default_factory=list)
    selected_accels: List[AcceleratorAssessment] = field(default_factory=list)
    fusion_analyzer: Optional[FusionAnalyzer] = None

    # ── Phase 3: Pruning ──
    prune_config: Optional[PruneConfig] = None
    prune_report: Optional[PruneReport] = None
    rtl_output_dir: str = ""
    verilator_extra_flags: List[str] = field(default_factory=list)

    # ── Phase 5: Fusion RTL ──
    fusion_rtl_applied: bool = False
    firmware_patched: bool = False
    patched_hex_path: str = ""
    compacted_hex_path: str = ""
    firmware_patches_count: int = 0
    firmware_bytes_saved: int = 0

    # ── Phase 5b: GCC Compile ──
    gcc_compile_result: Optional[GCCCompileResult] = None
    fused_hex_path: str = ""
    fused_elf_path: str = ""

    # ── Phase 7: Verification ──
    synth_comparison: Optional[SynthComparison] = None
    synth_fused_only: Optional[SynthComparison] = None
    synth_hwloop_pruned: Optional[SynthComparison] = None
    synth_all: Optional[SynthComparison] = None
    sim_baseline: Optional[SimulationResult] = None
    sim_fused: Optional[SimulationResult] = None
    sim_pruned: Optional[SimulationResult] = None
    sim_hwloop_pruned: Optional[SimulationResult] = None
    sim_all: Optional[SimulationResult] = None

    # Best-perf variants (when best cycles ≠ best ADP)
    synth_hwloop_pruned_best_perf: Optional[SynthComparison] = None
    synth_all_best_perf: Optional[SynthComparison] = None
    sim_hwloop_pruned_best_perf: Optional[SimulationResult] = None
    sim_all_best_perf: Optional[SimulationResult] = None

    # HWLoop hex (fused + hwloop patched — for step 5: All)
    hwloop_hex_path: Optional[str] = None
    hwloop_elf_path: Optional[str] = None
    # HWLoop-only hex (no fused instructions — for step 4: HWLoop+Pruned)
    hwloop_only_hex_path: Optional[str] = None
    hwloop_only_elf_path: Optional[str] = None
    # Docker baseline (same compiler, no fused, no hwloop — for fair comparison)
    docker_baseline_hex: Optional[str] = None
    docker_baseline_elf: Optional[str] = None

    # ── Named hex files (unique per step, no overwrites) ──
    hex_baseline: Optional[str] = None          # Step 1 & 2: baseline + pruned
    hex_fused: Optional[str] = None             # Step 3: fused+pruned
    hex_hwloop_pruned: Optional[str] = None     # Step 4: hwloop+pruned (final, beneficial loops only)
    hex_all: Optional[str] = None               # Step 5: fused+hwloop+pruned (best ADP)
    hex_all_perf: Optional[str] = None          # Step 5: fused+hwloop+pruned (best cycles)

    # HWLoop sweep candidates (produced by hwloop phase, evaluated in verification)
    hwloop_candidates: List = field(default_factory=list)  # List[HWLoopCandidate]

    # ── Phase 8: Reporting ──
    report_path: str = ""

    # ── Phase 7: Pipeline.run_full_verification result (when --use-pipeline-runner) ──
    pipeline_result: object = None
