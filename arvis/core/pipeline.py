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

from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
)

if TYPE_CHECKING:
    from arvis.core.reporter import Reporter
    from arvis.core.strategy import Decision, OptimizationStrategy
    from arvis.core.synthesis import SynthesisFlow, SynthResult
    from arvis.core.target import TargetCore
    from arvis.core.toolchain import Toolchain
    from arvis.core.verifier import Verifier, SimResult
    from arvis.core.workload import Workload


# ─── Variant configuration ─────────────────────────────────────────


@dataclass(frozen=True)
class VariantConfig:
    """Description of one RTL variant the pipeline should emit.

    A variant is a subset of decisions to apply.  For cv32e40p the
    legacy variants are:

    - ``baseline``:        no decisions
    - ``pruned``:          {prune}
    - ``fused_pruned``:    {fuse, prune}
    - ``hwloop_pruned``:   {hwloop, prune}
    - ``all``:             {fuse, hwloop, prune, width}

    With this abstraction those become four ``VariantConfig`` records
    instead of branched code in :func:`runner.run_pipeline`.

    Attributes
    ----------
    label:
        Output directory suffix and report name (e.g. ``"pruned"``).
    decision_kinds:
        Which :class:`Decision` types to apply.  Decisions of any
        other type produced by strategies are ignored for this
        variant.
    hex_source:
        Symbolic identifier picking which compiled hex this variant
        runs against (``"baseline"`` / ``"fused_only"`` /
        ``"hwloop_only"`` / ``"fused_hwloop"``).
    """

    label: str
    decision_kinds: frozenset = field(default_factory=frozenset)
    hex_source: str = "baseline"


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
    hex_path: Optional[Path] = None
    sim_result: Optional["SimResult"] = None
    synth_result: Optional["SynthResult"] = None


@dataclass
class PipelineResult:
    """Aggregated result of one :meth:`Pipeline.run`.

    Reporters consume this; later analyses (variant comparison,
    paper tables) read it as a JSON dump if the pipeline persists
    it.
    """

    workload_name: str
    target_name: str
    decisions: Dict[str, "Decision"] = field(default_factory=dict)
    variants: List[VariantResult] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


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
        target: "TargetCore",
        toolchain: "Toolchain",
        strategies: Sequence["OptimizationStrategy"],
        verifier: "Verifier",
        synthesis: Optional["SynthesisFlow"] = None,
        reporter: Optional["Reporter"] = None,
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
    def _strategy_order_key(strategy: "OptimizationStrategy") -> int:
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

    def ordered_strategies(self) -> List["OptimizationStrategy"]:
        """Return the strategy list in canonical execution order."""
        return sorted(self.strategies, key=self._strategy_order_key)

    # ── Run ────────────────────────────────────────────────────────
    def run(self, workload: "Workload") -> PipelineResult:
        """Run the pipeline against a single workload.

        Phase 1 mode: runs every applicable strategy's
        :meth:`analyze` in canonical order and collects the
        resulting :class:`Decision` objects into a
        :class:`PipelineResult`.  No RTL is emitted in this mode --
        the legacy :func:`pipeline.runner.run_pipeline` continues
        to handle variant emission and verification.

        This is sufficient to:

        - Smoke-test the strategy contract end-to-end.
        - A/B-compare two strategies of the same role on the same
          workload (just swap the strategy in the constructor).
        - Generate Decision dumps for paper tables / reproducibility.

        Phase 2 will extend this method to:

        1. Compile the workload via ``self.toolchain``.
        2. Compute the WorkloadProfile.
        3. Run strategies (this loop, unchanged).
        4. For each :class:`VariantConfig` in :attr:`variants`,
           emit RTL via ``target.render_decision``,
           verify via ``self.verifier``,
           synthesise via ``self.synthesis``.
        5. Emit the final report via ``self.reporter``.
        """
        # Compute the workload profile.  In Phase 1 the workload is
        # responsible for its own profiling (it knows how to invoke
        # the toolchain).  Phase 2 may introduce a profile cache.
        try:
            profile = workload.profile(self.toolchain)
        except Exception as exc:
            # A failed profile is non-fatal: every strategy will
            # see an empty profile and bail with an empty decision.
            # We surface the underlying exception via the result's
            # ``extra`` so callers can introspect.
            from arvis.core.workload import WorkloadProfile

            profile = WorkloadProfile()
            extras = {"profile_error": repr(exc)}
        else:
            extras = {}

        result = PipelineResult(
            workload_name=workload.name,
            target_name=self.target.name,
            extra=extras,
        )

        for strategy in self.ordered_strategies():
            if not strategy.applicable(workload, self.target):
                continue
            try:
                decision = strategy.analyze(workload, profile, self.target)
            except Exception as exc:
                # Strategy failures must not abort the pipeline.
                # Record the exception in extras and continue.
                result.extra.setdefault("strategy_errors", {})[
                    strategy.name
                ] = repr(exc)
                continue
            result.decisions[strategy.name] = decision

        return result
