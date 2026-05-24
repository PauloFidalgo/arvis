"""Phase 1 smoke test: build a Pipeline and run all strategies on a
real benchmark, collecting the decisions.

This is NOT the active pipeline path.  It exercises the new
``core/`` + ``strategies/`` + ``targets/`` layer end-to-end so we
have confidence the abstractions are usable before Phase 2 starts
replacing the legacy ``pipeline.runner.run_pipeline``.

Usage::

    python3 examples/portability_smoke.py

Expected output: each strategy reports a non-empty decision (or
explains why it bailed).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

# Allow the script to run from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arvis.core import (
    BuildRecipe,
    CompiledArtifact,
    ExpectedResult,
    ISADescriptor,
    Pipeline,
    Workload,
    WorkloadProfile,
    Toolchain,
)
from arvis.core.verifier import Verifier, SimResult
from arvis.targets import CV32E40P
from arvis.strategies.fusion import NGramFusion
from arvis.strategies.hwloop import CV32E40PHWLoop
from arvis.strategies.pruning import UsageDrivenPruner
from arvis.strategies.width import (
    CounterWidthNarrowing,
    HWLPAddrNarrowing,
    PCWidthNarrowing,
)


# ─── Minimal Workload + Toolchain + Verifier impls ────────────────
# These let us exercise Pipeline.run() without needing the full
# Phase 2 toolchain abstractions in place yet.  They wrap the
# existing artifacts in ``targets/benchmarks/<name>/`` for the
# benchmark we're smoke-testing on.


class StaticWorkload(Workload):
    """A workload whose ELFs are pre-built and just listed.

    Useful for smoke-testing the strategies against existing
    artifacts in ``targets/benchmarks/<bm>/`` without invoking the
    toolchain.  Phase 2 introduces a real ``Workload`` subclass
    that drives the build via ``Toolchain``.
    """

    def __init__(self, name: str, bench_dir: Path) -> None:
        self._name = name
        self._bench_dir = bench_dir

    @property
    def name(self) -> str:
        return self._name

    @property
    def sources(self) -> List[Path]:
        return list(self._bench_dir.glob("*.c")) + list(self._bench_dir.glob("*.s"))

    @property
    def cflags(self) -> List[str]:
        return ["-O2"]

    @property
    def build_recipe(self) -> BuildRecipe:
        return BuildRecipe(working_dir=self._bench_dir)

    @property
    def expected(self) -> ExpectedResult:
        return ExpectedResult()

    def profile(self, toolchain: Toolchain) -> WorkloadProfile:
        # Phase 1 smoke-test profile: collect every ELF the
        # benchmark directory ships, EXCLUDING:
        # - spike ELFs (built for the ISA simulator with a much
        #   larger memory layout; they'd skew width analyses).
        # - .baseline alternates (snapshots from earlier builds).
        # Phase 2's real Workload.profile will know precisely
        # which ELFs are in-scope from the build recipe.
        excluded_substrings = ("spike", ".baseline", "_baseline_")
        elfs = tuple(
            sorted(
                p
                for p in self._bench_dir.glob("*.elf")
                if not any(s in p.name for s in excluded_substrings)
            )
        )
        return WorkloadProfile(elf_paths=elfs)


class NullToolchain(Toolchain):
    """A toolchain that doesn't compile anything.

    Sufficient for the smoke test because the pre-built ELFs in
    ``targets/benchmarks/<bm>/`` already exist on disk.  Phase 2
    introduces ``GCCToolchain`` that actually drives the Docker
    build.
    """

    @property
    def name(self) -> str:
        return "null"

    @property
    def isa(self) -> ISADescriptor:
        return ISADescriptor.rv32imc_zicsr()

    def compile(self, workload, cflags=(), output_dir=None):
        raise NotImplementedError("smoke test uses pre-built ELFs")

    def assemble(self, sources, cflags=(), output=None):
        raise NotImplementedError("smoke test uses pre-built ELFs")

    def disassemble(self, elf_path):
        return ""


class NullVerifier(Verifier):
    """No-op verifier — Phase 1 smoke test doesn't simulate."""

    @property
    def name(self) -> str:
        return "null"

    def simulate(self, rtl_dir, hex_path, max_cycles=10_000_000, firmware_args={}):
        return SimResult(test_passed=False)


# ─── Smoke test ────────────────────────────────────────────────────


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    bench_root = repo_root / "targets" / "benchmarks"
    bench_dir = bench_root / "ud"
    if not bench_dir.exists():
        # Fall back to whatever benchmark is available.  arvis-public
        # ships only a minimal benchmark; full ud is in the private
        # tree.  Pick the first directory that contains at least one
        # ELF so the smoke test has something concrete to chew on.
        candidates = [
            d for d in bench_root.iterdir() if d.is_dir() and any(d.glob("*.elf"))
        ] if bench_root.exists() else []
        if not candidates:
            print(
                f"ERROR: no benchmark with .elf files found under {bench_root}",
                file=sys.stderr,
            )
            print(
                "       The smoke test needs at least one pre-built ELF.",
                file=sys.stderr,
            )
            return 1
        bench_dir = candidates[0]
        print(f"  (ud not present — falling back to {bench_dir.name})")

    target = CV32E40P()
    workload = StaticWorkload(name="ud", bench_dir=bench_dir)
    toolchain = NullToolchain()
    verifier = NullVerifier()

    pipeline = Pipeline(
        target=target,
        toolchain=toolchain,
        strategies=[
            NGramFusion(),
            # CV32E40PHWLoop()  -- intentionally omitted from the
            # default smoke test.  It delegates to the legacy
            # ``pipeline.hwloop.run`` which performs a Docker
            # image build (~5 min on first run) and dual-compiles
            # the workload.  Add it back when you want to verify
            # the full integration, but expect a long runtime.
            UsageDrivenPruner(verbose=False),
            PCWidthNarrowing(),
            HWLPAddrNarrowing(),
            CounterWidthNarrowing(),
        ],
        verifier=verifier,
    )

    print(f"Running pipeline on workload={workload.name} target={target.name}")
    print(f"  strategies (in canonical order):")
    for s in pipeline.ordered_strategies():
        print(f"    - {s.name}")

    result = pipeline.run(workload)

    print()
    print(f"Decisions collected: {len(result.decisions)}")
    for sname, decision in result.decisions.items():
        # Pretty-print depending on decision type
        from arvis.core import (
            FusionDecision,
            LoopDecision,
            PruneDecision,
            WidthDecision,
        )

        if isinstance(decision, PruneDecision):
            print(
                f"  {sname:30s} -> {len(decision.removable_alu_ops)} ALU ops, "
                f"{len(decision.removable_mul_modes)} MUL modes, "
                f"{len(decision.removable_opcode_groups)} groups removable; "
                f"{len(decision.used_instructions)} insns used; "
                f"{len(decision.feature_flags)} flags"
            )
        elif isinstance(decision, FusionDecision):
            print(
                f"  {sname:30s} -> {len(decision.fused_ops)} fused operations"
            )
        elif isinstance(decision, LoopDecision):
            print(
                f"  {sname:30s} -> nest={decision.nest_depth} cnt={decision.counter_width}b "
                f"addr={decision.addr_width}b loops={len(decision.patched_loops)}"
            )
        elif isinstance(decision, WidthDecision):
            print(
                f"  {sname:30s} -> pc={decision.pc_width} hwlp_addr={decision.hwlp_addr_width} "
                f"counter={decision.counter_width}"
            )
        else:
            print(f"  {sname:30s} -> {decision}")

    if result.extra:
        print()
        print(f"Extras: {result.extra}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
