"""Pipeline orchestrator.

The :class:`Pipeline` composes a target, a workload, a list of
strategies, a verifier, an optional synthesis flow, and a
reporter.  It runs strategies in dependency order, collects the
resulting decisions, and emits one or more variant RTL trees.

Phase 1 ships the orchestrator's *shape* (the dataclasses for
configuration and result, plus a :meth:`run` skeleton that raises
``NotImplementedError`` for the real path).  The legacy
:func:`pipeline.runner.run_pipeline` continues to be the active
entry point until the strategies are retrofitted in subsequent
commits.

Once Phase 2 is complete this module replaces ``runner.py``
entirely: the if-tree on ``has_fusion``/``has_hwloop`` is gone,
variants are described as data
(:class:`VariantConfig`), and the pipeline is just a fold over
``(strategies, variants)``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
)

if TYPE_CHECKING:
    from arvis.core.reporter import Reporter
    from arvis.core.rtl_patch import RTLWorkspace
    from arvis.core.strategy import Decision, OptimizationStrategy
    from arvis.core.synthesis import SynthesisFlow, SynthResult
    from arvis.core.target import TargetCore
    from arvis.core.toolchain import Toolchain
    from arvis.core.verifier import SimResult, Verifier
    from arvis.core.workload import Workload


logger = logging.getLogger(__name__)


# ─── Variant configuration ─────────────────────────────────────────


@dataclass(frozen=True)
class VariantConfig:
    """Description of one RTL variant the pipeline should emit.

    A variant is a subset of decisions to apply.  For cv32e40p the
    standard variants are:

    - ``baseline``:        no decisions
    - ``pruned``:          {prune}
    - ``fused_pruned``:    {fuse, prune}
    - ``hwloop_pruned``:   {hwloop, prune}
    - ``all``:             {fuse, hwloop, prune, width}

    With this abstraction those become five :class:`VariantConfig`
    records instead of branched code in :func:`runner.run_pipeline`.

    Attributes
    ----------
    label:
        Output directory suffix and report name (e.g. ``"pruned"``).
        The pipeline emits each variant into ``rtl_{label}/`` under
        the configured output directory.
    decision_kinds:
        Class names (as strings) of :class:`Decision` types to apply
        in this variant.  Decisions of any other type produced by
        strategies are ignored for this variant.  Strings rather than
        class objects so YAML/CLI config can specify them directly.
    hex_source:
        Symbolic identifier picking which compiled hex this variant
        runs against (``"baseline"`` / ``"fused_only"`` /
        ``"hwloop_only"`` / ``"fused_hwloop"``).
    """

    label: str
    decision_kinds: frozenset[str] = field(default_factory=frozenset)
    hex_source: str = "baseline"

    def includes(self, decision_class_name: str) -> bool:
        """Return True if this variant should apply decisions of
        the given class.

        ``decision_kinds`` empty means "apply nothing" (baseline).
        """
        return decision_class_name in self.decision_kinds


# ─── Pipeline result types ─────────────────────────────────────────


@dataclass(frozen=True)
class VariantResult:
    """Outcome of emitting and verifying one variant.

    Strategies are not re-run per variant: they run once, produce
    decisions, and the pipeline picks the relevant subset for each
    variant.  Sim/synth results are per-variant.
    """

    label: str
    rtl_dir: Path
    hex_path: Path | None = None
    sim_result: SimResult | None = None
    synth_result: SynthResult | None = None


@dataclass
class PipelineResult:
    """Aggregated result of one :meth:`Pipeline.run`.

    Reporters consume this; later analyses (variant comparison,
    paper tables) read it as a JSON dump if the pipeline persists
    it.
    """

    workload_name: str
    target_name: str
    decisions: dict[str, Decision] = field(default_factory=dict)
    variants: list[VariantResult] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# ─── Pipeline ──────────────────────────────────────────────────────


class Pipeline:
    """Composition of target + strategies + verifier + synth + reporter.

    Constructed once per workload.  Calling :meth:`run` produces a
    :class:`PipelineResult`.

    The Phase 1 implementation of :meth:`run` is a stub that
    delegates to the legacy
    :func:`pipeline.runner.run_pipeline`.  Phase 2 fills it in with
    the strategy-driven logic.
    """

    def __init__(
        self,
        target: TargetCore,
        toolchain: Toolchain,
        strategies: Sequence[OptimizationStrategy[Decision]],
        verifier: Verifier,
        synthesis: SynthesisFlow | None = None,
        reporter: Reporter | None = None,
        variants: Sequence[VariantConfig] = (),
    ) -> None:
        self.target = target
        self.toolchain = toolchain
        self.strategies = list(strategies)
        self.verifier = verifier
        self.synthesis = synthesis
        self.reporter = reporter
        self.variants = list(variants)

    # ── Strategy ordering ──────────────────────────────────────────
    @staticmethod
    def _strategy_order_key(strategy: OptimizationStrategy[Decision]) -> int:
        """Default strategy ordering.

        Fusion runs before pruning because the prune analysis needs
        to see the fused binary.  Loop strategies run after fusion
        because hwloop patching operates on assembly produced by
        the fused compile.  Width strategies run last because they
        depend on the final binary's text size.

        Concrete pipelines may override by passing a custom
        ``key=`` to :func:`sorted` when calling the pipeline, or by
        constructing the ``strategies`` list in the desired order
        and skipping the sort.
        """
        # Late import to avoid a top-level cycle.
        from arvis.core.strategy import (
            FusionStrategy,
            LoopStrategy,
            PruningStrategy,
            WidthStrategy,
        )

        if isinstance(strategy, FusionStrategy):
            return 0
        if isinstance(strategy, LoopStrategy):
            return 1
        if isinstance(strategy, PruningStrategy):
            return 2
        if isinstance(strategy, WidthStrategy):
            return 3
        return 99  # unknown role goes last

    def ordered_strategies(self) -> list[OptimizationStrategy[Decision]]:
        """Return the strategy list in canonical execution order."""
        return sorted(self.strategies, key=self._strategy_order_key)

    # ── Run ────────────────────────────────────────────────────────
    def run(self, workload: Workload) -> PipelineResult:
        """Run the pipeline against a single workload.

        Two modes:

        - **Decisions-only** (when :attr:`variants` is empty): runs
          every applicable strategy's :meth:`analyze` in canonical
          order and returns the resulting :class:`Decision` objects
          inside :class:`PipelineResult`.  No RTL is emitted.

        - **Variant emission** (when :attr:`variants` is non-empty):
          after collecting decisions, iterates over each variant,
          builds a fresh :class:`RTLWorkspace`, applies the patches
          rendered from the relevant decisions, and records a
          :class:`VariantResult`.  Verification and synthesis are
          run per-variant when their respective ``verifier`` and
          ``synthesis`` slots are populated.

        Phase 2.6 implements both modes.  The active legacy entry
        point :func:`pipeline.runner.run_pipeline` is unchanged.
        """
        # Compute the workload profile.  In Phase 1 the workload is
        # responsible for its own profiling (it knows how to invoke
        # the toolchain).  Phase 2 may introduce a profile cache.
        try:
            profile = workload.profile(self.toolchain)
        except Exception as exc:
            from arvis.core.workload import WorkloadProfile

            profile = WorkloadProfile()
            extras: dict[str, Any] = {"profile_error": repr(exc)}
        else:
            extras = {}

        result = PipelineResult(
            workload_name=workload.name,
            target_name=self.target.name,
            extra=extras,
        )

        # ── Phase A: collect decisions ──
        for strategy in self.ordered_strategies():
            if not strategy.applicable(workload, self.target):
                continue
            try:
                decision = strategy.analyze(workload, profile, self.target)
            except Exception as exc:
                result.extra.setdefault("strategy_errors", {})[strategy.name] = repr(exc)
                continue
            result.decisions[strategy.name] = decision

        # ── Phase B: emit variants (if requested) ──
        if not self.variants:
            return result

        # Group decisions by their type name so variants can pick.
        # decisions[name] -> Decision; we want type_name -> [Decision].
        from collections import defaultdict

        decisions_by_kind: dict[str, list[Decision]] = defaultdict(list)
        for d in result.decisions.values():
            decisions_by_kind[type(d).__name__].append(d)

        for variant in self.variants:
            try:
                vr = self._emit_variant(variant, decisions_by_kind, workload)
            except Exception as exc:
                result.extra.setdefault("variant_errors", {})[variant.label] = repr(exc)
                continue
            result.variants.append(vr)

        # ── Phase C: emit report (if reporter is attached) ──
        if self.reporter is not None:
            try:
                self.reporter.emit(result, self._output_root_for_workload(workload))
            except Exception as exc:
                result.extra.setdefault("reporter_error", repr(exc))

        return result

    # ── Variant emission helpers ──────────────────────────────────
    def _emit_variant(
        self,
        variant: VariantConfig,
        decisions_by_kind: dict[str, list[Decision]],
        workload: Workload,
    ) -> VariantResult:
        """Render and apply patches for one variant."""
        from arvis.core.rtl_patch import RTLWorkspace

        out_root = self._output_root_for_workload(workload)
        workspace = RTLWorkspace(
            source_root=self.target.rtl_root,
            output_root=out_root / f"rtl_{variant.label}",
        )
        workspace.copy_fresh()

        # Compute per-variant shared state (e.g. encoding registry
        # for cv32e40p).  The target decides what shared state it
        # needs; this is its hook into the variant emission flow.
        # For targets that don't need it, the default no-op below
        # leaves workspace.metadata untouched.
        self._populate_workspace_metadata(variant, decisions_by_kind, workspace)

        # Patch ordering matters: prune first (strips datapaths the
        # specialized decoder no longer needs), then fusion (adds new
        # decoder cases on the surviving ALU), then loop (operates on
        # the post-fusion decoder), then width (final parameter
        # rewrites).  Mirrors the legacy RTLChangeSet.apply order.
        order = ("PruneDecision", "FusionDecision", "LoopDecision", "WidthDecision")

        for kind in order:
            if not variant.includes(kind):
                continue
            for decision in decisions_by_kind.get(kind, ()):
                patches = self.target.render_decision(decision, workspace)
                for patch in patches:
                    patch.apply(workspace)

        # Verification + synthesis are optional and only run when
        # the appropriate component is wired AND the workload can
        # resolve a hex file for this variant.  The default
        # workload returns ``None``, which safely skips simulation.
        sim_result = None
        synth_result = None
        hex_path = None

        try:
            hex_path = workload.hex_for_variant(variant.label)
        except Exception:
            logger.exception(
                "Soft-skip: workload.hex_for_variant raised for variant %r",
                variant.label,
            )

        rtl_dir_for_sim = workspace.output_root / "rtl"

        # Run the verifier when wired and a hex resolved.  Targets
        # that don't want simulation pass a NullVerifier whose
        # ``simulate`` returns ``SimResult(test_passed=False)`` --
        # a benign no-op.
        if self.verifier is not None and hex_path is not None:
            try:
                sim_result = self.verifier.simulate(
                    rtl_dir=rtl_dir_for_sim,
                    hex_path=hex_path,
                )
            except Exception:
                logger.exception(
                    "Soft-skip: verifier.simulate raised for variant %r",
                    variant.label,
                )

        # Run synthesis when wired (no hex needed).
        if self.synthesis is not None:
            try:
                synth_result = self.synthesis.synthesize(
                    rtl_dir=rtl_dir_for_sim,
                )
            except Exception:
                logger.exception(
                    "Soft-skip: synthesis.synthesize raised for variant %r",
                    variant.label,
                )

        return VariantResult(
            label=variant.label,
            rtl_dir=workspace.output_root,
            hex_path=hex_path,
            sim_result=sim_result,
            synth_result=synth_result,
        )

    def _populate_workspace_metadata(
        self,
        variant: VariantConfig,
        decisions_by_kind: dict[str, list[Decision]],
        workspace: RTLWorkspace,
    ) -> None:
        """Hook for the target to compute per-variant shared state.

        Called once per variant emission, before any patches run.
        The default implementation looks for an
        ``allocate_workspace_metadata`` method on the target; if
        present, the target gets to populate
        :attr:`workspace.metadata` with whatever the patches in
        this variant will need.

        Targets without per-variant shared state (e.g. simple cores
        with no encoding allocator) need not override.
        """
        hook = getattr(self.target, "allocate_workspace_metadata", None)
        if hook is None:
            return
        try:
            hook(variant, decisions_by_kind, workspace)
        except Exception:
            logger.exception(
                "Soft-skip: target.allocate_workspace_metadata raised "
                "for variant %r; continuing with empty metadata",
                variant.label,
            )

    def _output_root_for_workload(self, workload: Workload) -> Path:
        """Where this pipeline writes per-variant directories.

        Default: ``output/{workload.name}_specialized``.  Phase 2.7
        introduces a configurable output root on Pipeline so tests
        can redirect to a temp directory.
        """
        from pathlib import Path

        return Path(f"output/{workload.name}_specialized")
