"""ARVIS portability layer.

This package contains target-, toolchain-, and strategy-agnostic
abstractions.  No file in ``core`` imports from ``pipeline``,
``codegen``, or any concrete cv32e40p code: all of those are
*implementations* of the abstractions defined here.

Architecture overview
---------------------

The ARVIS specialization pipeline is structured around four roles:

1. **Target** (:mod:`core.target`) — the core to be specialized.
   A :class:`TargetCore` owns the source RTL, the parameter
   surface, the encoding space for custom operations, and the
   testbench.  It knows nothing about the strategies that will
   optimize it.

2. **Strategies** (:mod:`core.strategy`) — the optimization
   decisions.  :class:`OptimizationStrategy` analyses a workload and
   produces a typed :class:`Decision`.  Subtypes:
   :class:`PruningStrategy`, :class:`FusionStrategy`,
   :class:`LoopStrategy`, :class:`WidthStrategy`.  Each subtype
   commits to its own :class:`Decision` shape
   (:class:`PruneDecision`, :class:`FusionDecision`,
   :class:`LoopDecision`, :class:`WidthDecision`).

3. **Pipeline** (:mod:`core.pipeline`) — the orchestrator.  It
   composes a target, a workload, a list of strategies, a
   verifier, an optional synthesis flow, and a reporter.  Running
   the pipeline produces decisions and emits variant RTL trees.

4. **Workload** (:mod:`core.workload`) — what the pipeline is
   specializing for.  A :class:`Workload` describes the source
   files, compile recipe, and expected behaviour;
   :meth:`Workload.profile` produces a :class:`WorkloadProfile`
   (instruction histogram, cycle counts, etc.) used by strategies
   during analysis.

Pluggability is achieved through three additional interfaces:
:class:`Toolchain` (:mod:`core.toolchain`),
:class:`Verifier` (:mod:`core.verifier`),
:class:`SynthesisFlow` (:mod:`core.synthesis`),
and :class:`Reporter` (:mod:`core.reporter`).

Phase 1 of the migration introduces these abstractions without
changing existing pipeline behaviour.  Concrete strategies and the
CV32E40P target are retrofitted on top of the existing code.

This package is intentionally framework-light: it uses
:mod:`dataclasses` for value objects and :class:`abc.ABC` for
interfaces.  There is no plugin registry — strategies are composed
explicitly in Python or loaded from a YAML file (see
:mod:`core.pipeline`).
"""

# Public API.  Concrete implementations live in targets/, strategies/,
# etc.; this module only re-exports the abstract interfaces.

from arvis.core.isa import ISADescriptor
from arvis.core.strategy import (
    Decision,
    OptimizationStrategy,
    PruningStrategy,
    FusionStrategy,
    LoopStrategy,
    WidthStrategy,
    PruneDecision,
    FusionDecision,
    LoopDecision,
    WidthDecision,
)
from arvis.core.target import (
    TargetCore,
    CoreParameter,
    OpcodeSpace,
    OpcodeSlot,
)
from arvis.core.toolchain import Toolchain, CompiledArtifact
from arvis.core.verifier import Verifier, SimResult
from arvis.core.synthesis import SynthesisFlow, SynthResult
from arvis.core.reporter import Reporter
from arvis.core.workload import (
    Workload,
    WorkloadProfile,
    WorkloadSuite,
    BuildRecipe,
    ExpectedResult,
)
from arvis.core.rtl_patch import RTLPatch, RTLWorkspace
from arvis.core.pipeline import Pipeline, PipelineResult, VariantResult, VariantConfig

__all__ = [
    # ISA
    "ISADescriptor",
    # Strategy
    "Decision",
    "OptimizationStrategy",
    "PruningStrategy",
    "FusionStrategy",
    "LoopStrategy",
    "WidthStrategy",
    "PruneDecision",
    "FusionDecision",
    "LoopDecision",
    "WidthDecision",
    # Target
    "TargetCore",
    "CoreParameter",
    "OpcodeSpace",
    "OpcodeSlot",
    # Toolchain
    "Toolchain",
    "CompiledArtifact",
    # Verifier
    "Verifier",
    "SimResult",
    # Synthesis
    "SynthesisFlow",
    "SynthResult",
    # Reporter
    "Reporter",
    # Workload
    "Workload",
    "WorkloadProfile",
    "WorkloadSuite",
    "BuildRecipe",
    "ExpectedResult",
    # RTL patches
    "RTLPatch",
    "RTLWorkspace",
    # Pipeline
    "Pipeline",
    "PipelineResult",
    "VariantResult",
    "VariantConfig",
]
