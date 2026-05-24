"""Phase 2.7 equivalence test.

Verifies that the new ``Pipeline.run()`` variant emission path
produces RTL byte-identical to the legacy
``pipeline.rtl_changeset.RTLChangeSet.apply()`` path for the
PRUNED variant.

This is the load-bearing validation before Phase 2.8 swaps the
active runner.py call sites.  Failure here means the new path is
not a drop-in replacement and we'd need to triage before
proceeding.

Coverage in this commit
-----------------------
* PRUNED variant (only PruneDecision applied) -- the strongest
  test we can do without a Docker GCC dual-compile (which the
  hwloop strategy requires) or a separately-driven encoding
  allocation pass (which fusion needs).

Phase 3 extends this to the remaining variants once the legacy
fusion / hwloop paths are factored cleanly out of the legacy
RTLChangeSet.

Usage::

    python3 examples/portability_equivalence.py

Exit code 0 = bit-identical, 1 = mismatch.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

# Allow the script to run from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arvis.core import (
    BuildRecipe,
    ExpectedResult,
    ISADescriptor,
    Pipeline,
    Toolchain,
    Workload,
    WorkloadProfile,
)
from arvis.core.verifier import Verifier, SimResult
from arvis.targets import CV32E40P
from arvis.targets.cv32e40p.variants import PRUNED
from arvis.strategies.pruning import UsageDrivenPruner


# ─── Minimal Workload / Toolchain / Verifier (Phase 1 shim style) ─


class StaticWorkload(Workload):
    def __init__(self, name: str, bench_dir: Path) -> None:
        self._name = name
        self._bench_dir = bench_dir

    @property
    def name(self) -> str:
        return self._name

    @property
    def sources(self):
        return []

    @property
    def cflags(self):
        return []

    @property
    def build_recipe(self) -> BuildRecipe:
        return BuildRecipe()

    @property
    def expected(self) -> ExpectedResult:
        return ExpectedResult()

    def profile(self, toolchain: Toolchain) -> WorkloadProfile:
        excluded = ("spike", ".baseline", "_baseline_")
        elfs = tuple(
            sorted(
                p
                for p in self._bench_dir.glob("*.elf")
                if not any(s in p.name for s in excluded)
            )
        )
        return WorkloadProfile(elf_paths=elfs)


class NullToolchain(Toolchain):
    @property
    def name(self) -> str:
        return "null"

    @property
    def isa(self) -> ISADescriptor:
        return ISADescriptor.rv32imc_zicsr()

    def compile(self, *a, **kw):
        raise NotImplementedError

    def assemble(self, *a, **kw):
        raise NotImplementedError

    def disassemble(self, elf):
        return ""


class NullVerifier(Verifier):
    @property
    def name(self) -> str:
        return "null"

    def simulate(self, *a, **kw) -> SimResult:
        return SimResult(test_passed=False)


# ─── Test harness ──────────────────────────────────────────────────


def hash_tree(root: Path) -> dict:
    """Return ``{relative_path: sha256}`` over every .sv file."""
    out: dict = {}
    for sv in sorted(root.rglob("*.sv")):
        rel = sv.relative_to(root)
        out[str(rel)] = hashlib.sha256(sv.read_bytes()).hexdigest()
    return out


def run_legacy_pruned(target: CV32E40P, decision, output_dir: Path) -> Path:
    """Apply the same PruneDecision via the legacy RTLPruner.

    Mirrors what
    :meth:`pipeline.rtl_changeset.RTLChangeSet.apply` does for
    the PRUNED variant: copy fresh, run RTLPruner.  Skip the rest
    (no fusion, no hwloop, no encoding optimization, no width
    narrowing -- per the PRUNED variant config).
    """
    from arvis.codegen.rtl.base import RTLWorkspace as LegacyWS
    from arvis.codegen.rtl.rtl_pruning import PruneConfig, RTLPruner

    rtl_dir = output_dir / "rtl_legacy_pruned"
    ws = LegacyWS(source_root=str(target.rtl_root), output_root=str(rtl_dir))

    # The PrunePatch uses the legacy PruneConfig from
    # decision.target_payload when present (UsageDrivenPruner
    # always populates it).  For the legacy run we feed the same
    # config directly to RTLPruner.
    config = decision.target_payload
    if not isinstance(config, PruneConfig):
        # Reconstruct minimal config (mirrors PrunePatch fallback).
        config = PruneConfig()
        config.removable_alu_ops = set(decision.removable_alu_ops)
        config.removable_mul_modes = set(decision.removable_mul_modes)
        config.removable_opcode_groups = set(decision.removable_opcode_groups)
        for k, v in decision.feature_flags.items():
            if hasattr(config, k) and isinstance(getattr(config, k), bool):
                setattr(config, k, v)

    # Match the new path's workspace setup.  Legacy
    # RTLWorkspace uses ``.copy()`` rather than ``.copy_fresh()``.
    if Path(ws.output_root).exists():
        import shutil
        shutil.rmtree(ws.output_root)
    ws.copy(verbose=False)
    RTLPruner(ws).apply(config, verbose=False)

    return rtl_dir


def run_new_pruned(target: CV32E40P, workload: StaticWorkload, tmp_root: Path) -> Path:
    """Apply the same PruneDecision via the new Pipeline.run."""
    pipeline = Pipeline(
        target=target,
        toolchain=NullToolchain(),
        strategies=[UsageDrivenPruner(verbose=False)],
        verifier=NullVerifier(),
        variants=[PRUNED],
    )
    pipeline._output_root_for_workload = lambda wl: tmp_root

    result = pipeline.run(workload)
    if not result.variants:
        raise RuntimeError(
            f"No variants emitted; result.extra={result.extra}"
        )
    return result.variants[0].rtl_dir


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    bench_root = repo_root / "targets" / "benchmarks"
    bench_dir = bench_root / "ud"
    if not bench_dir.exists():
        candidates = (
            [d for d in bench_root.iterdir() if d.is_dir() and any(d.glob("*.elf"))]
            if bench_root.exists()
            else []
        )
        if not candidates:
            print(
                f"ERROR: no benchmark with .elf files found under {bench_root}",
                file=sys.stderr,
            )
            return 1
        bench_dir = candidates[0]
        print(f"  (ud not present — falling back to {bench_dir.name})")

    target = CV32E40P()
    workload = StaticWorkload(name=bench_dir.name, bench_dir=bench_dir)

    print(f"Equivalence test: PRUNED variant on workload={workload.name}")
    print()

    # Build the decision once (we need to feed the same one to
    # both paths).  We do this by running the strategy directly.
    pruner = UsageDrivenPruner(verbose=False)
    profile = workload.profile(NullToolchain())
    decision = pruner.analyze(workload=workload, profile=profile, target=target)
    print(
        f"  PruneDecision: "
        f"{len(decision.removable_alu_ops)} ALU, "
        f"{len(decision.removable_mul_modes)} MUL, "
        f"{len(decision.removable_opcode_groups)} groups"
    )

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)

        # Run both paths
        legacy_rtl = run_legacy_pruned(target, decision, tmp)
        new_rtl = run_new_pruned(target, workload, tmp)

        print(f"  legacy RTL: {legacy_rtl}")
        print(f"  new    RTL: {new_rtl}")

        legacy_h = hash_tree(legacy_rtl)
        new_h = hash_tree(new_rtl)

        legacy_files = set(legacy_h)
        new_files = set(new_h)

        only_legacy = sorted(legacy_files - new_files)
        only_new = sorted(new_files - legacy_files)
        common = sorted(legacy_files & new_files)

        print()
        print(f"  legacy-only files: {len(only_legacy)}")
        print(f"  new-only files:    {len(only_new)}")
        print(f"  common files:      {len(common)}")

        differ = [f for f in common if legacy_h[f] != new_h[f]]
        print(f"  differ:            {len(differ)}")

        if only_legacy or only_new or differ:
            print()
            for f in only_legacy[:5]:
                print(f"    only in legacy: {f}")
            for f in only_new[:5]:
                print(f"    only in new:    {f}")
            for f in differ[:5]:
                print(f"    DIFFER: {f}")
            return 1

        print()
        print(
            f"  ✓ EQUIVALENT: all {len(common)} SV files byte-identical "
            f"between legacy and new paths"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
