"""CV32E40P PULP-style hardware-loop strategy.

The default ARVIS hwloop strategy: detect eligible loops in the
fused assembly, dual-compile via the merged hwloop GCC fork, and
patch the loops with ``lp.start`` / ``lp.end`` / ``lp.count``
instructions.  The legacy implementation lives in
:mod:`pipeline.hwloop`; this strategy is its Phase 1 retrofit.

Of the four strategy retrofits this is the most heavily
side-effect-dependent (Docker image rebuilds, dual compile, ELF
patching, RTL merge), so the Phase 1 wrapper is correspondingly
thin: it constructs the cfg/ctx shims, delegates to the legacy
:func:`pipeline.hwloop.run`, and reads back the resulting state to
build a :class:`LoopDecision`.

Phase 2 will:

- Move loop *detection* (currently in
  :class:`analysis.hwloop.AsmLoopDetector`) into a pure analysis
  step that runs from a :class:`WorkloadProfile`.
- Move loop *patching* (currently in
  :class:`codegen.hwloop.generator.HWLoopGenerator`) into the
  target's render path.
- Move the dual-compile loop into the
  :class:`core.toolchain.Toolchain` interface so different
  toolchains can register their own hwloop binaries.

Until then, the strategy is faithful to the legacy behaviour but
intentionally narrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arvis.core.strategy import LoopDecision, LoopStrategy

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


# ─── Minimal shims ─────────────────────────────────────────────────


@dataclass
class _CfgShim:
    """Subset of :class:`config.ToolConfig` that
    :func:`pipeline.hwloop.run` reads."""

    rtl_root: str = "targets/cv32e40p"
    output_dir: str = "output/_hwloop_strategy_tmp"
    benchmark_name: str = "default"
    benchmark_dir: str = ""
    enabled_phases: tuple[str, ...] = ("fusion",)


@dataclass
class _CtxShim:
    """Subset of :class:`pipeline.context.PipelineContext` that
    :func:`pipeline.hwloop.run` reads or writes."""

    fused_elf_path: str | None = None
    fused_hex_path: str | None = None
    fused_asm_path: str | None = None
    hwloop_elf_path: str | None = None
    hwloop_hex_path: str | None = None
    hwloop_only_elf_path: str | None = None
    hwloop_only_hex_path: str | None = None
    hwloop_candidates: tuple[Any, ...] = ()

    def __getattr__(self, name: str) -> None:
        return None


@dataclass
class _ChangesetShim:
    """Subset of :class:`pipeline.rtl_changeset.RTLChangeSet` that
    :func:`pipeline.hwloop.run` writes to."""

    hw_loop_count: int = 0
    hw_loop_cnt_width: int = 32
    hw_loop_addr_width: int = 32
    fused_operations: tuple[Any, ...] = ()


# ─── The strategy ──────────────────────────────────────────────────


class CV32E40PHWLoop(LoopStrategy):
    """Patch eligible loops with PULP-style hwloop instructions.

    Parameters
    ----------
    rtl_root, output_dir, benchmark_name, benchmark_dir:
        Forwarded into the ``CfgShim`` consumed by
        :func:`pipeline.hwloop.run`.  Most users will leave them
        at defaults; out-of-tree builds override.
    """

    def __init__(
        self,
        *,
        rtl_root: str = "targets/cv32e40p",
        output_dir: str = "output/_hwloop_strategy_tmp",
        benchmark_name: str = "default",
        benchmark_dir: str = "",
    ) -> None:
        self.rtl_root = rtl_root
        self.output_dir = output_dir
        self.benchmark_name = benchmark_name
        self.benchmark_dir = benchmark_dir

    @property
    def name(self) -> str:
        return "cv32e40p-pulp-hwloop"

    def applicable(self, workload: Workload, target: TargetCore) -> bool:
        # Hwloop is meaningful only for targets that expose the
        # HW_LOOP parameter.  A future ibex target without the
        # PULP hwloop unit returns False and the pipeline skips.
        return target.parameter("HW_LOOP") is not None

    def analyze(
        self,
        workload: Workload,
        profile: WorkloadProfile,
        target: TargetCore,
    ) -> LoopDecision:
        from arvis.pipeline import hwloop as _legacy_hwloop

        # Pick the fused ELF.  The legacy hwloop.run expects
        # ctx.fused_elf_path to point at a fused binary; without
        # one the dual-compile path can't run and we report a
        # null decision.
        fused_elf: Path | None = None
        for elf in profile.elf_paths:
            if elf is None:
                continue
            stem = Path(str(elf)).stem
            if "fused" in stem and "baseline" not in stem:
                fused_elf = Path(str(elf))
                break
        if fused_elf is None:
            return LoopDecision()

        cfg = _CfgShim(
            rtl_root=self.rtl_root,
            output_dir=self.output_dir,
            benchmark_name=workload.name if workload is not None else self.benchmark_name,
            benchmark_dir=self.benchmark_dir,
        )
        ctx = _CtxShim(fused_elf_path=str(fused_elf))
        changeset = _ChangesetShim()

        try:
            _legacy_hwloop.run(
                cfg,  # type: ignore[arg-type]  # _CfgShim duck-types ToolConfig
                ctx,  # type: ignore[arg-type]  # _CtxShim duck-types PipelineContext
                changeset,  # type: ignore[arg-type]  # _ChangesetShim duck-types RTLChangeSet
            )
        except Exception:
            # Strategies cannot abort the pipeline.  Hwloop is
            # particularly likely to fail in environments without
            # Docker or the riscv-gcc-hwloop toolchain; we report
            # "no hwloop applied" and the pipeline carries on.
            return LoopDecision()

        # Extract the result from the mutated changeset shim.
        return LoopDecision(
            nest_depth=changeset.hw_loop_count,
            counter_width=changeset.hw_loop_cnt_width,
            addr_width=changeset.hw_loop_addr_width,
            patched_loops=tuple(getattr(ctx, "hwloop_candidates", ()) or ()),
        )
