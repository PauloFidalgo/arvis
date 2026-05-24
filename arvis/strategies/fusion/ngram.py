"""N-gram-mining fusion strategy.

The default ARVIS fusion strategy: mine the disassembly for
recurring instruction sequences (n-grams), score them by frequency
and DSP-friendliness, and emit a list of custom fused
instructions.

Phase 1 retrofit: this is an adapter around the existing
:func:`pipeline.fusion_rtl.compute_filtered_ops` (which itself
runs n-gram analysis from :mod:`pipeline.selection` and the
DSP-friendliness filter from
:mod:`codegen.rtl.isa_fusion.alu_single_cycle`).

Phase 2 will move the analysis logic out of the legacy modules so
this strategy stops adapting and just analyses directly.  Until
then, behaviour is bit-identical to the legacy path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arvis.core.strategy import FusionDecision, FusionStrategy

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


# ─── Minimal ctx/cfg shims (subset used by compute_filtered_ops) ──


@dataclass
class _CfgShim:
    """Subset of :class:`config.ToolConfig` that
    :func:`compute_filtered_ops` actually reads."""

    rtl_root: str = "targets/cv32e40p"
    output_dir: str = "output/_fusion_strategy_tmp"
    benchmark_name: str = "default"


@dataclass
class _CtxShim:
    """Subset of :class:`pipeline.context.PipelineContext` that
    :func:`compute_filtered_ops` actually reads."""

    fused_elf_path: str | None = None
    fused_hex_path: str | None = None
    fused_asm_path: str | None = None
    gcc_compile_result: object | None = None
    all_fusions: object | None = None
    _fusion_rtl_fused_ops: list[Any] | None = None

    def __getattr__(self, name: str) -> None:
        # Anything else accessed via getattr() returns None.  The
        # legacy code typically guards with hasattr or `or`, so
        # None is a safe default.
        return None


# ─── The strategy ──────────────────────────────────────────────────


class NGramFusion(FusionStrategy):
    """Mine n-grams, filter by DSP-friendliness, emit fused ops.

    Parameters
    ----------
    rtl_root:
        Path to the target's RTL templates.  The legacy filter
        reads :file:`cv32e40p_pkg.sv` to learn the existing ALU
        operator names so it can avoid colliding.  Defaults to
        ``targets/cv32e40p`` -- override for out-of-tree targets.
    output_dir:
        Working directory for intermediate fusion artifacts.
        Defaults to ``output/_fusion_strategy_tmp``.
    """

    def __init__(
        self,
        *,
        rtl_root: str = "targets/cv32e40p",
        output_dir: str = "output/_fusion_strategy_tmp",
    ) -> None:
        self.rtl_root = rtl_root
        self.output_dir = output_dir

    @property
    def name(self) -> str:
        return "ngram-fusion"

    def applicable(self, workload: Workload, target: TargetCore) -> bool:
        # Fusion is meaningful for any target with a non-empty
        # custom-opcode space.  A target that exposes zero R4 slots
        # would skip fusion automatically.
        try:
            space = target.opcode_space()
            return space.free_slots() > 0
        except Exception:
            return False

    def analyze(
        self,
        workload: Workload,
        profile: WorkloadProfile,
        target: TargetCore,
    ) -> FusionDecision:
        from arvis.pipeline import fusion_rtl

        # Pick the fused ELF (compute_filtered_ops needs to
        # disassemble the post-fusion binary).  In Phase 1 the
        # caller is expected to have run a fused compile already
        # and stuffed the path into the profile.  When no fused
        # ELF is present we return an empty decision rather than
        # aborting -- this matches the legacy path's "no fusion
        # candidates" outcome.
        fused_elf: Path | None = None
        for elf in profile.elf_paths:
            if elf is None:
                continue
            stem = Path(str(elf)).stem
            if "fused" in stem and "baseline" not in stem:
                fused_elf = Path(str(elf))
                break
        if fused_elf is None:
            return FusionDecision()

        cfg = _CfgShim(
            rtl_root=self.rtl_root,
            output_dir=self.output_dir,
            benchmark_name=workload.name if workload is not None else "default",
        )
        ctx = _CtxShim(
            fused_elf_path=str(fused_elf),
        )

        try:
            fused_ops = fusion_rtl.compute_filtered_ops(
                cfg,  # type: ignore[arg-type]  # _CfgShim duck-types ToolConfig
                ctx,  # type: ignore[arg-type]  # _CtxShim duck-types PipelineContext
            )
        except Exception:
            # Strategies cannot abort the pipeline; an analysis
            # failure becomes "no fusions".
            return FusionDecision()

        if not fused_ops:
            return FusionDecision()

        # FusedOperation objects are not yet target-agnostic; we
        # carry them through the FusionDecision opaquely.  Phase 3
        # introduces a target-neutral FusedOp record that the
        # cv32e40p target then translates back into the legacy
        # FusedOperation for emission.
        return FusionDecision(fused_ops=tuple(fused_ops))
