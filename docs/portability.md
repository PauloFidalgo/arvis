# Porting ARVIS to a New Target Core

This document is the playbook for porting the ARVIS specialization
pipeline to a new RISC-V core (or any core with a similar
parameter-driven SystemVerilog implementation). Following it should
let a researcher add a new target without modifying the `core/`,
`strategies/`, or `pipeline/` packages.

The reference port is `targets/cv32e40p/`. Read it alongside this
document.

## Prerequisites

Your core must:

1. Be a parameterised SystemVerilog implementation (parameters used
   for configuration tuning, not hard-coded literals).
2. Have a separable testbench that the simulator can drive with a
   hex file.
3. Use a RISC-V variant that GCC or LLVM can target (or come with
   a compatible toolchain).
4. Reserve at least one opcode space for custom instructions if
   you want fusion to apply.

If your core lacks (1) (e.g. uses generate-statements or a
configuration package instead of parameters), you'll need to
parameterise the relevant signals first; ARVIS does not do that
work for you.

## Five-step checklist

### Step 1. Create the package skeleton

```
targets/<core_name>/
├── __init__.py            # re-export the TargetCore subclass
├── core.py                # the TargetCore subclass
├── patches.py             # RTLPatch implementations (one per decision type)
├── variants.py            # VariantConfig instances for the standard set
└── rtl/                   # source SystemVerilog templates (your core)
```

`targets/<core_name>/__init__.py` should contain:

```python
from targets.<core_name>.core import <CoreName>

__all__ = ["<CoreName>"]
```

### Step 2. Implement `TargetCore`

Subclass `core.target.TargetCore` and implement the required
methods. Minimum surface:

```python
from core.target import TargetCore, CoreParameter, OpcodeSpace, OpcodeSlot
from core.isa import ISADescriptor


class IbexCore(TargetCore):
    DEFAULT_RTL_ROOT = Path("targets/ibex")

    @property
    def name(self):
        return "ibex"

    @property
    def isa(self):
        return ISADescriptor.rv32imc_zicsr()  # or your variant

    @property
    def rtl_root(self):
        return self._rtl_root

    def synthesizable_files(self):
        return [Path("rtl/ibex_top.sv"), ...]

    def parameters(self):
        return [
            CoreParameter("PC_WIDTH", default=32, minimum=8, maximum=32,
                          description="Main pipeline PC width"),
            # ... add your tunable parameters
        ]

    def opcode_space(self):
        # Return a fresh OpcodeSpace populated with the slots
        # your core reserves for custom instructions.
        return OpcodeSpace(name="ibex-custom",
                           available=[OpcodeSlot(...) for ...])

    @property
    def reset_vector(self):
        return 0x100  # or whatever your testbench uses

    @property
    def memory_layout(self):
        return {"text": (0, 0x10_0000), "data": (0x10_0000, 0x10_0000)}

    def testbench_dir(self):
        return self._rtl_root / "tb"
```

The base class provides a `render_decision(d, workspace)`
dispatcher that delegates to per-decision-type
`render_<kind>_decision` methods. Override only the ones you
support; default no-ops are inherited.

### Step 3. Implement `RTLPatch` classes

For each decision type your core supports, write an `RTLPatch`
subclass in `targets/<core_name>/patches.py`. The patch's
`apply(workspace)` method mutates the SystemVerilog files inside
the workspace.

Patches **must** be idempotent — applying twice is a no-op on
the second run. The reference cv32e40p patches use either
per-edit marker comments (`apply_pc_width`) or
parameter-name-anchored regexes (`HWLP_ADDR_WIDTH`,
`CNT_WIDTH`) to achieve idempotency.

```python
from core.rtl_patch import RTLPatch, RTLWorkspace
from core.strategy import WidthDecision


class IbexWidthPatch(RTLPatch):
    def __init__(self, decision: WidthDecision):
        self.decision = decision

    def apply(self, workspace: RTLWorkspace) -> None:
        # Mutate workspace.output_root / "rtl" / ...
        pass
```

Then wire it into the target:

```python
class IbexCore(TargetCore):
    def render_width_decision(self, decision, workspace):
        return [IbexWidthPatch(decision=decision)]
```

### Step 4. Define the variant configs

In `targets/<core_name>/variants.py`, define
`VariantConfig` instances matching the variants you want to emit.
The cv32e40p set is a good template:

```python
BASELINE      = VariantConfig(label="baseline", decision_kinds=frozenset())
PRUNED        = VariantConfig(label="pruned",
                              decision_kinds=frozenset({"PruneDecision"}))
FUSED_PRUNED  = VariantConfig(label="fused_pruned",
                              decision_kinds=frozenset({"PruneDecision",
                                                        "FusionDecision"}))
ALL           = VariantConfig(label="all",
                              decision_kinds=frozenset({"PruneDecision",
                                                        "FusionDecision",
                                                        "LoopDecision",
                                                        "WidthDecision"}))
```

Custom variants are just additional `VariantConfig` entries.
There is no fixed list.

### Step 5. Smoke test the port

Before running the full pipeline, verify:

```python
from targets.<core_name> import <CoreName>

target = <CoreName>()
print(target.name, target.isa.name)
print([p.name for p in target.parameters()])
print(target.opcode_space().free_slots(), "free slots")

# RTL workspace round-trip
import tempfile
from core import RTLWorkspace
with tempfile.TemporaryDirectory() as tmp:
    ws = RTLWorkspace(target.rtl_root, Path(tmp))
    ws.copy_fresh()
    print(len(list(ws.output_root.rglob("*.sv"))), "SV files")
```

Then plug into a `Pipeline` with a single strategy:

```python
from core import Pipeline
from strategies.width import PCWidthNarrowing

pipeline = Pipeline(
    target=<CoreName>(),
    toolchain=YourToolchain(),
    strategies=[PCWidthNarrowing()],
    verifier=YourVerifier(),
    variants=[BASELINE, ALL],
)
result = pipeline.run(your_workload)
```

If the smoke test produces sane RTL, you're done with the port.

## What you do NOT need to touch

- `core/` — the abstract types are target-neutral.
- `strategies/` — strategy implementations work against any
  `TargetCore` (provided their `applicable()` predicate is
  satisfied; e.g. `HWLPAddrNarrowing` requires the target to
  expose an `HWLP_ADDR_WIDTH` parameter, which a non-PULP core
  might omit).
- `pipeline/` — the legacy package remains the active path until
  Phase 2.8 wires in the new orchestrator.

## Common gotchas

### Parameter naming

`CoreParameter.name` must match the SystemVerilog parameter
identifier exactly. The cv32e40p target uses `CNT_WIDTH`
(matching the RTL) rather than `HW_LOOP_CNT_WIDTH` (the
semantic name). Strategies use the `CoreParameter.name` as a
lookup key for applicability checks.

### Workspace lifetime

Each variant emission gets a fresh workspace. Patches must not
assume any prior state beyond what the previous patches in the
canonical order produced. The order is:

1. Prune
2. Fusion
3. Loop
4. Width

Fusion patches operate on the post-pruning decoder; loop
patches operate on the post-fusion tree; width patches do final
parameter rewrites.

### Encoding allocation

Fusion and hwloop share opcode space. The cv32e40p target
reserves 2 R4-type slots at the tail for hwloop bounds/count
instructions; fusion ops consume from the front. If your core's
encoding scheme differs, design your `OpcodeSpace.allocate()` to
return slots in the order your decoder expects.

### Testbench wiring

`testbench_dir()` returns the path to your testbench. The
`Verifier` implementation reads from this path. The reference
`VerilatorVerifier` (Phase 3) expects an `example_tb/core/verilator/`
subdirectory; if your testbench is shaped differently, write a
custom `Verifier` subclass.

## Running the equivalence test on your port

If you want byte-identical equivalence between an existing
hand-tuned RTL flow and the new pipeline-emitted RTL:

1. Capture the RTL produced by your hand-tuned flow.
2. Reproduce the same decisions in `Pipeline.run()`.
3. Hash every `.sv` file in both trees and compare.

`examples/portability_equivalence.py` shows how this is done for
cv32e40p; copy and adapt.

## Limitations

- **Cross-ISA targets**: only RISC-V is supported today. The
  `ISADescriptor` shape would need extending for non-RISC-V
  cores. This is documented as a future direction; not a current
  priority.
- **Compiler retargeting**: the `Toolchain` interface exposes
  `register_custom_ops` and `register_hwloop` hooks, but only
  GCC has a concrete implementation. An LLVM port is documented
  but not implemented; the formalism gap (GCC `.md` vs LLVM `.td`)
  makes a unified compiler abstraction non-trivial.
- **Workload model**: the `Workload` abstraction is in place but
  only `StaticWorkload` ships (in `examples/`). Real workload
  classes for Embench, MiBench etc. are deferred to a follow-up.

## Reference

- Architecture overview: [`architecture.md`](architecture.md).
- Source-level entry points: see `core/__init__.py` for the
  public API.
- Equivalence test: `examples/portability_equivalence.py`.
- Smoke test: `examples/portability_smoke.py`.
