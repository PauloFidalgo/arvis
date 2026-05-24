# ARVIS Portability Architecture

This document describes the portability layer introduced on the
`portability` branch: a strategy-pluggable, target-agnostic
specialization pipeline for RISC-V cores. It is intended for
academic reviewers and contributors who need to understand the
design without reading every file.

## 1. Goals

The portability layer was added to address three concerns in the
original ARVIS code:

1. **Strategy lock-in.** Pruning was hard-coded to a usage-driven
   analysis; fusion was hard-coded to n-gram mining; hwloop was
   hard-coded to a PULP-style detector. Researchers could not A/B
   compare alternative strategies (e.g. cycle-driven pruning,
   profile-guided fusion) without modifying core code.
2. **Target lock-in.** Every step assumed cv32e40p RTL conventions,
   parameter names, encoding spaces, and testbench shape. Porting
   to a sibling core (ibex, Kelvin) required editing pipeline
   internals.
3. **Implicit state.** A single `PipelineContext` god-object
   accumulated ~40 attributes ad-hoc; phases communicated by
   reading and writing context fields. This made dataflow opaque
   and broke under composition.

The portability refactor introduces a small layer of typed
abstractions that **describe** rather than **prescribe** the work.
Concrete strategies, targets, toolchains, verifiers, synthesis
flows, and reporters all plug into a single `Pipeline` orchestrator.

## 2. Public surface

The `core/` package exports the abstract types. Concrete
implementations live alongside in `targets/`, `strategies/`, etc.

```
core/
├── isa.py           ISADescriptor (RV32IMC_Zicsr presets, cflags)
├── strategy.py      OptimizationStrategy[D] + 4 sub-abstracts +
│                    4 Decision dataclasses
├── target.py        TargetCore + CoreParameter + OpcodeSpace + OpcodeSlot
├── rtl_patch.py     RTLPatch hierarchy + RTLWorkspace
├── toolchain.py     Toolchain + CompiledArtifact
├── verifier.py      Verifier + SimResult
├── synthesis.py     SynthesisFlow + SynthResult
├── reporter.py      Reporter
├── workload.py      Workload + WorkloadProfile + WorkloadSuite +
│                    BuildRecipe + ExpectedResult
└── pipeline.py      Pipeline + PipelineResult + VariantResult +
                     VariantConfig
```

## 3. Class diagram

```mermaid
classDiagram
    %% ── Top-level orchestration ─────────────────────────────────
    class Pipeline {
        +target: TargetCore
        +toolchain: Toolchain
        +strategies: list~OptimizationStrategy~
        +verifier: Verifier
        +synthesis: SynthesisFlow
        +reporter: Reporter
        +variants: list~VariantConfig~
        +run(workload: Workload) PipelineResult
    }
    class PipelineResult {
        +decisions: dict
        +variants: list~VariantResult~
    }
    class VariantConfig {
        +label: str
        +decision_kinds: frozenset~str~
        +hex_source: str
        +includes(name) bool
    }

    %% ── Strategy hierarchy (Strategy + Generic) ─────────────────
    class OptimizationStrategy~D~ {
        <<abstract>>
        +name: str
        +applicable(workload, target) bool
        +analyze(workload, profile, target) D
    }
    class Decision {
        <<abstract>>
        +render(target) list~RTLPatch~
    }
    class PruningStrategy
    class FusionStrategy
    class LoopStrategy
    class WidthStrategy
    OptimizationStrategy <|-- PruningStrategy
    OptimizationStrategy <|-- FusionStrategy
    OptimizationStrategy <|-- LoopStrategy
    OptimizationStrategy <|-- WidthStrategy

    class PruneDecision
    class FusionDecision
    class LoopDecision
    class WidthDecision
    Decision <|-- PruneDecision
    Decision <|-- FusionDecision
    Decision <|-- LoopDecision
    Decision <|-- WidthDecision

    PruningStrategy ..> PruneDecision : produces
    FusionStrategy  ..> FusionDecision : produces
    LoopStrategy    ..> LoopDecision : produces
    WidthStrategy   ..> WidthDecision : produces

    %% ── Target abstraction ─────────────────────────────────────
    class TargetCore {
        <<abstract>>
        +name: str
        +isa: ISADescriptor
        +rtl_root: Path
        +parameters() list~CoreParameter~
        +opcode_space() OpcodeSpace
        +reset_vector: int
        +memory_layout: dict
        +testbench_dir() Path
        +render_decision(d, ws) list~RTLPatch~
    }
    class CV32E40P
    TargetCore <|-- CV32E40P

    class CoreParameter {
        +name: str
        +default: int
        +minimum: int
        +maximum: int
        +clamp(value) int
    }
    class OpcodeSpace {
        +available: list~OpcodeSlot~
        +allocate() OpcodeSlot
        +free_slots() int
    }
    TargetCore o-- CoreParameter
    TargetCore o-- OpcodeSpace

    %% ── RTL emission ───────────────────────────────────────────
    class RTLPatch {
        <<abstract>>
        +label: str
        +apply(workspace)
    }
    class RTLWorkspace {
        +source_root: Path
        +output_root: Path
        +copy_fresh()
        +read(rel) str
        +write(rel, content)
    }
    class WidthNarrowingPatch
    class PrunePatch
    class FusionPatch
    class LoopPatch
    RTLPatch <|-- WidthNarrowingPatch
    RTLPatch <|-- PrunePatch
    RTLPatch <|-- FusionPatch
    RTLPatch <|-- LoopPatch
    RTLPatch ..> RTLWorkspace : mutates

    %% ── Workload + Toolchain + Verifier + Synthesis ────────────
    class Workload {
        <<abstract>>
        +name: str
        +sources: list~Path~
        +cflags: list~str~
        +profile(toolchain) WorkloadProfile
    }
    class WorkloadProfile {
        +instr_histogram: dict
        +cycle_profile: dict
        +loops: tuple
        +elf_paths: tuple
    }
    class Toolchain {
        <<abstract>>
        +compile(workload, cflags) CompiledArtifact
        +disassemble(elf) str
        +register_custom_ops(decision)
    }
    class Verifier {
        <<abstract>>
        +simulate(rtl_dir, hex) SimResult
    }
    class SynthesisFlow {
        <<abstract>>
        +synthesize(rtl_dir, tech) SynthResult
    }

    Pipeline o-- TargetCore
    Pipeline o-- Toolchain
    Pipeline o-- OptimizationStrategy
    Pipeline o-- Verifier
    Pipeline o-- SynthesisFlow
    Pipeline o-- VariantConfig
    Pipeline ..> Workload : run(workload)
    Workload ..> WorkloadProfile : profile()
```

## 4. End-to-end sequence

A single `Pipeline.run(workload)` invocation with N variants:

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Pipeline
    participant TC as Toolchain
    participant Strats as Strategies
    participant Workload
    participant Target as TargetCore
    participant Patches as RTLPatches
    participant WS as RTLWorkspace
    participant Ver as Verifier
    participant Synth as SynthesisFlow
    participant Rpt as Reporter

    User->>Pipeline: run(workload)
    Pipeline->>Workload: profile(toolchain)
    Workload->>TC: disassemble + analyse
    TC-->>Workload: instr histogram, ELF paths
    Workload-->>Pipeline: WorkloadProfile

    loop each strategy in canonical order
        Pipeline->>Strats: applicable(workload, target)?
        Pipeline->>Strats: analyze(workload, profile, target)
        Strats-->>Pipeline: Decision (Prune / Fusion / Loop / Width)
    end

    loop each VariantConfig in pipeline.variants
        Pipeline->>WS: copy_fresh()
        loop each decision filtered by variant.decision_kinds
            Pipeline->>Target: render_decision(d, workspace)
            Target-->>Pipeline: list~RTLPatch~
            loop each patch
                Pipeline->>Patches: apply(workspace)
                Patches->>WS: read/write SV files
            end
        end
        opt verifier wired
            Pipeline->>Ver: simulate(workspace.output_root, hex)
            Ver-->>Pipeline: SimResult
        end
        opt synthesis wired
            Pipeline->>Synth: synthesize(workspace.output_root, tech)
            Synth-->>Pipeline: SynthResult
        end
    end

    opt reporter wired
        Pipeline->>Rpt: emit(result, output_dir)
    end

    Pipeline-->>User: PipelineResult
```

## 5. Design patterns used

| Pattern | Where | What it solves |
|---|---|---|
| **Strategy** | `OptimizationStrategy[D]` + sub-abstracts | Swappable analysis algorithms (n-gram fusion vs profile-guided fusion vs manual). |
| **Generic typing** | `OptimizationStrategy[D]` parametrised on the Decision type | Compile-time guarantee that a `PruningStrategy` returns a `PruneDecision`, not a `FusionDecision`. |
| **Value object** | `Decision` subclasses (frozen `@dataclass`) | Immutability; safe to share across phases; Python-pickleable for caching. |
| **Visitor (light)** | `TargetCore.render_decision` dispatches by `Decision` type | New decision types extend `render_decision` by overriding the corresponding `render_<kind>_decision` method on the target. |
| **Composite** | `RTLPatch.CompositePatch` | Group related edits under a single label without breaking idempotency. |
| **Template method** | `Pipeline.run()` skeleton with overridable `_strategy_order_key` | Pipelines may reorder strategies (e.g. for an experimental schedule) by subclassing. |
| **Builder** | `Pipeline(target=..., strategies=[...], variants=[...])` | Explicit composition of the pipeline; no global registry to mutate. |
| **Specification** | `VariantConfig.decision_kinds` filter at emission time | A variant is described as the set of decision types it includes; orthogonal to the strategy order. |

## 6. Patch ordering and dependencies

When a variant is emitted, its decisions are rendered into patches.
Patches are applied in a fixed canonical order:

1. **Prune** — strips datapaths the specialised decoder no longer
   needs (ALU ops, multiplier modes, opcode groups).
2. **Fusion** — adds new decoder cases on the surviving (post-prune)
   ALU/decoder.
3. **Loop** — operates on the post-fusion decoder
   (hwloop pragma processor + `cv32e40p_hwloop_regs.sv` template
   swap when `nest_depth > 0`).
4. **Width** — final parameter rewrites (`PC_WIDTH`,
   `HWLP_ADDR_WIDTH`, `CNT_WIDTH`).

This ordering matches the legacy `RTLChangeSet.apply` so the new
path is byte-identical to the old when given the same decisions
(verified for the `PRUNED` variant — see
`examples/portability_equivalence.py`).

## 7. Decision lifecycle

A decision has three phases of life:

```
strategy.analyze ─→ Decision ─→ target.render_decision ─→ list~RTLPatch~
   (analysis)        (typed)        (lowering)              (mutation)
```

- **Analysis** runs once per `Pipeline.run()`, producing one
  Decision per applicable strategy.
- **Lowering** runs once per (variant, decision) pair; the same
  Decision may be lowered into different patches in different
  variants (e.g. a `PruneDecision` is a no-op in the `BASELINE`
  variant but produces an `RTLPruner` patch in the `PRUNED`
  variant).
- **Mutation** runs once per patch when the variant emission
  applies it.

Decisions are immutable; patches operate on a workspace that's
freshly copied from the target's `rtl_root` per variant. There is
no shared mutable state between variants.

## 8. Decision data model (typed fields, no opaque bridge)

The four decision dataclasses (`PruneDecision`, `FusionDecision`,
`LoopDecision`, `WidthDecision`) carry exclusively **typed**
fields. Earlier drafts of the migration used an `Optional[Any]`
`target_payload` slot to ferry legacy data through the strategy
→ patch boundary; that bridge is closed.

`PruneDecision` in particular carries the full set of fields the
cv32e40p emitter needs:

- `removable_alu_ops: FrozenSet[str]`
- `removable_mul_modes: FrozenSet[str]`
- `removable_opcode_groups: FrozenSet[str]`
- `feature_flags: Dict[str, bool]` — every `enable_*` toggle
- `used_instructions: FrozenSet[str]`
- `unused_registers: Tuple[int, ...]`
- `used_regs_mask: int`
- `removable_csr_labels: FrozenSet[str]`
- `removable_csr_storage: FrozenSet[str]`
- `target_overlay: Dict[str, int]` — target capability counts
  (`corev_pulp`, `fpu`, `num_mhpmcounters`, etc.) the strategy
  observed and propagates

Translation between the legacy `PruneConfig` (mutable, attribute-
heavy) and the new `PruneDecision` (frozen, typed) is performed
in two symmetric places:

- `strategies.pruning.usage_driven.UsageDrivenPruner._translate`
  — `PruneConfig` → `PruneDecision`.
- `targets.cv32e40p.patches.PrunePatch._build_prune_config`
  — `PruneDecision` → `PruneConfig`.

The pair is round-trip-lossless versus the legacy
`compute_prune_config` output. Equivalence is verified by
`examples/portability_equivalence.py`: all 174 SV files in the
PRUNED variant's RTL workspace are byte-identical between the
legacy `RTLPruner` path and the new `Pipeline.run()` path.

## 9. Equivalence-test discipline

`examples/portability_equivalence.py` is the regression-discipline
guard for the migration. It:

1. Constructs a `PruneDecision` via the live strategy.
2. Applies it via the legacy `RTLPruner` to one workspace.
3. Applies it via the new `Pipeline.run()` to another workspace.
4. Hashes every `.sv` file in both trees and reports diffs.

A green run means the new path is a verified drop-in for the
legacy path on the variant under test. The repository
guarantees the test passes for the `PRUNED` variant on the `ud`
benchmark (174 SV files byte-identical).

`FUSED_PRUNED`, `HWLOOP_PRUNED`, and `ALL` coverage is in flight
(see `docs/portability.md` § Limitations).

## 10. Migration phases

The portability layer was introduced incrementally:

| Phase | Commits | What landed |
|---|---|---|
| **1.1–1.7** | `daf82a9..83697d8` | `core/` abstractions; concrete strategies and target; `Pipeline.run()` in decisions-only mode. |
| **2.1–2.4** | `8d6b3e2..0820409` | Real `RTLPatch` implementations for each Decision type. |
| **2.5–2.7** | `5d32664..1af0adb` | Standard variant configs; per-variant emission; equivalence test for `PRUNED`. |
| **3** | committed | Doc, test suite, full equivalence, typed-field migration (target_payload removed), partial codegen factoring. |
| **3 (in progress)** | next | Encoding allocator extraction; FUSED_PRUNED / HWLOOP_PRUNED / ALL equivalence; runner.py replacement (gated). |

Each commit is independently buildable and the legacy pipeline
remains the active path until Phase 2.8 wires in the new
`Pipeline.run()` (gated behind `--use-portability`).

## 11. Pointers

- **Add a new pruning strategy**: see `strategies/pruning/usage_driven.py`,
  subclass `PruningStrategy`, return `PruneDecision`. Compose into a
  `Pipeline` instead of `UsageDrivenPruner`. No core changes needed.
- **Add a new target core**: see `docs/portability.md` for the
  port playbook.
- **Run the equivalence test**: `python3 examples/portability_equivalence.py`.
- **Run the smoke test**: `python3 examples/portability_smoke.py`.
