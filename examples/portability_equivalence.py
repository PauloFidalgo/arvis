"""Phase 2.7b equivalence test — all four standard variants.

Verifies that the new ``Pipeline.run()`` variant emission path
produces RTL byte-identical to the legacy
``pipeline.rtl_changeset.RTLChangeSet.apply()`` path for **every**
standard cv32e40p variant:

- ``BASELINE``      — no decisions
- ``PRUNED``        — PruneDecision only
- ``FUSED_PRUNED``  — PruneDecision + FusionDecision
- ``HWLOOP_PRUNED`` — PruneDecision + LoopDecision (nest_depth>0)
- ``ALL``           — Prune + Fusion + Loop + Width

This is the regression-discipline guard for Phase 2.8 (replacing
the active runner.py call sites with Pipeline.run).  A failure
here means the new path is not a drop-in replacement for the
named variant and Phase 2.8 must wait.

Coverage limitation
-------------------
The FUSED_PRUNED and ALL variants depend on a ``FusionDecision``
populated with real ``FusedOperation`` objects.  Constructing
those objects requires a Docker GCC build (the legacy
``compute_filtered_ops`` reads ``gcc_compile_result.used_instructions``
which is only populated by ``pipeline.gcc_compile.run``).  Rather
than gate this test on a 5+ minute Docker build, we test those
variants with an EMPTY ``FusionDecision``: the orchestration is
exercised; the byte-identical claim holds for the empty-fusion
case.

To run the variants with real fusion ops, set
``ARVIS_REAL_FUSION=1`` in the environment.  The test then reads
a snapshot from ``targets/benchmarks/<name>/.fusion_decision.pkl``
when available; otherwise the variant is skipped with a clear
message.

Usage
-----
  $ python3 examples/portability_equivalence.py                 # all 4
  $ ARVIS_REAL_FUSION=1 python3 examples/portability_equivalence.py
"""

from __future__ import annotations

import hashlib
import os
import pickle
import sys
import tempfile
from pathlib import Path

# Allow the script to run from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arvis.core import (
    BuildRecipe,
    ExpectedResult,
    FusionDecision,
    ISADescriptor,
    LoopDecision,
    Pipeline,
    Toolchain,
    Workload,
    WorkloadProfile,
)
from arvis.core.verifier import Verifier, SimResult
from arvis.targets import CV32E40P
from arvis.targets.cv32e40p.variants import (
    BASELINE,
    PRUNED,
    FUSED_PRUNED,
    HWLOOP_PRUNED,
    ALL,
)
from arvis.strategies.pruning import UsageDrivenPruner
from arvis.workloads import BenchmarkWorkload


# ─── Minimal Toolchain + Verifier (Phase 1 shim style) ────────────


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


# ─── Hashing helpers ──────────────────────────────────────────────


def hash_tree(root: Path) -> dict:
    """Return ``{relative_path: sha256}`` over every .sv file."""
    out: dict = {}
    for sv in sorted(root.rglob("*.sv")):
        rel = sv.relative_to(root)
        out[str(rel)] = hashlib.sha256(sv.read_bytes()).hexdigest()
    return out


def diff_trees(legacy_root: Path, new_root: Path) -> tuple:
    """Return (only_legacy, only_new, differ) lists for printing."""
    legacy_h = hash_tree(legacy_root)
    new_h = hash_tree(new_root)
    only_legacy = sorted(set(legacy_h) - set(new_h))
    only_new = sorted(set(new_h) - set(legacy_h))
    common = sorted(set(legacy_h) & set(new_h))
    differ = [f for f in common if legacy_h[f] != new_h[f]]
    return only_legacy, only_new, differ, len(common)


# ─── Decision builders ────────────────────────────────────────────


def build_prune_decision(target, workload):
    """Run the live UsageDrivenPruner to build a real PruneDecision."""
    pruner = UsageDrivenPruner(verbose=False)
    profile = workload.profile(NullToolchain())
    return pruner.analyze(workload=workload, profile=profile, target=target)


def build_fusion_decision_empty():
    """Empty FusionDecision -- no fused ops.

    Used for FUSED_PRUNED / ALL variants in the default mode where
    we don't have a Docker GCC build available.  Documents what the
    orchestration produces in the no-fusion case (still a valid
    test of the pipeline machinery).
    """
    return FusionDecision()


def build_fusion_decision_real(workload):
    """Try to load a real FusionDecision from a workload-side pickle.

    Returns None when the pickle is absent.
    """
    pickle_path = workload.bench_dir / ".fusion_decision.pkl"
    if not pickle_path.exists():
        return None
    try:
        with open(pickle_path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def build_loop_decision(nest_depth=2, counter_width=12, addr_width=14):
    """Synthetic LoopDecision for HWLOOP_PRUNED / ALL tests.

    The values match what the live ``CV32E40PHWLoop`` strategy would
    produce on a typical small benchmark (nest=2, counter ~12 bits,
    addr ~14 bits).  Real strategy invocation needs Docker for the
    dual-compile; this synthetic decision exercises only the RTL
    emission path, which is what the equivalence test cares about.
    """
    return LoopDecision(
        nest_depth=nest_depth,
        counter_width=counter_width,
        addr_width=addr_width,
    )


# ─── Legacy emitter wrappers ──────────────────────────────────────


def emit_legacy_variant(
    target,
    output_dir,
    *,
    label,
    prune_decision=None,
    fusion_decision=None,
    loop_decision=None,
):
    """Run the legacy ``RTLChangeSet.apply`` for one variant.

    Returns the resulting RTL directory.
    """
    from arvis.codegen.rtl.base import RTLWorkspace as LegacyWS
    from arvis.codegen.rtl.rtl_pruning import PruneConfig, RTLPruner
    from arvis.pipeline.rtl_changeset import RTLChangeSet

    rtl_dir = output_dir / f"rtl_legacy_{label}"

    if prune_decision is None:
        # BASELINE: just copy fresh templates, no decisions to apply.
        if rtl_dir.exists():
            import shutil
            shutil.rmtree(rtl_dir)
        ws = LegacyWS(source_root=str(target.rtl_root), output_root=str(rtl_dir))
        ws.copy(verbose=False)
        return rtl_dir

    # Build a PruneConfig from the decision (same translation
    # PrunePatch._build_prune_config performs).
    config = PruneConfig()
    config.removable_alu_ops = set(prune_decision.removable_alu_ops)
    config.removable_mul_modes = set(prune_decision.removable_mul_modes)
    config.removable_opcode_groups = set(prune_decision.removable_opcode_groups)
    config.removable_csr_labels = set(prune_decision.removable_csr_labels)
    config.removable_csr_storage = set(prune_decision.removable_csr_storage)
    for k, v in prune_decision.feature_flags.items():
        if hasattr(config, k) and isinstance(getattr(config, k), bool):
            setattr(config, k, v)
    config.unused_registers = list(prune_decision.unused_registers)
    config.used_regs_mask = prune_decision.used_regs_mask
    for attr, value in prune_decision.target_overlay.items():
        if hasattr(config, attr) and isinstance(getattr(config, attr), int):
            setattr(config, attr, value)

    # Build the RTLChangeSet.  For the PRUNED variant this is just
    # the prune config; FUSED/HWLOOP/ALL add the corresponding
    # fields.
    cs = RTLChangeSet()
    cs.add_prune_config(config, set(prune_decision.used_instructions))

    if fusion_decision is not None and len(fusion_decision.fused_ops) > 0:
        cs.add_fused_operations(list(fusion_decision.fused_ops))

    if loop_decision is not None and loop_decision.nest_depth > 0:
        cs.hw_loop_count = loop_decision.nest_depth
        cs.hw_loop_cnt_width = loop_decision.counter_width
        cs.hw_loop_addr_width = loop_decision.addr_width

    cs._apply_label = f"legacy_{label}"

    # Build the cfg/ctx shims the legacy apply expects.  Both must
    # be permissive: the legacy code reads many attributes via
    # direct attribute access (not getattr-with-default), so any
    # attribute the shim doesn't predict must return a sensible
    # default rather than raise AttributeError.
    #
    # Class scopes in Python don't see enclosing function locals
    # via name lookup, so we build the cfg/ctx as instances by
    # plain object subclasses with __init__-set attributes.

    class _Cfg:
        def __init__(self, rtl_root, output_dir):
            self.rtl_root = rtl_root
            self.output_dir = output_dir
            self.prune_rf_read_c = False
            self.prune_rf_write_b = False
            self.enable_debug = True
            self.enable_hpm = True

        def __getattr__(self, name):
            # Permissive defaults for fields the legacy code reads
            # but the shim doesn't predict.
            if name.startswith("enable_"):
                return True
            if name.startswith("prune_"):
                return False
            return None

    class _Ctx:
        def __init__(self, rtl_output_dir):
            self.rtl_output_dir = rtl_output_dir
            self.gcc_compile_result = None

        def __getattr__(self, name):
            return None

    if rtl_dir.exists():
        import shutil
        shutil.rmtree(rtl_dir)

    cfg_inst = _Cfg(str(target.rtl_root), str(output_dir))
    ctx_inst = _Ctx(str(rtl_dir))
    cs.apply(cfg_inst, ctx_inst, verbose=False)
    return rtl_dir


def emit_new_variant(
    target,
    workload,
    output_dir,
    *,
    variant,
    prune_decision=None,
    fusion_decision=None,
    loop_decision=None,
):
    """Run the new ``Pipeline.run`` for one variant.

    The strategies list is empty (we feed pre-built decisions
    directly) so we bypass the strategy phase by stashing the
    decisions on the result manually.

    Returns the resulting RTL directory.
    """
    pipeline = Pipeline(
        target=target,
        toolchain=NullToolchain(),
        strategies=[],  # we don't run strategies; decisions are pre-built
        verifier=NullVerifier(),
        variants=[variant],
    )
    pipeline._output_root_for_workload = lambda wl: output_dir

    # We can't bypass Pipeline.run's strategy phase easily, so we
    # fake it by giving Pipeline a one-off workload that injects
    # the decisions when profile() runs.  Cleaner: call the
    # private _emit_variant directly with our decisions_by_kind.
    decisions_by_kind = {}
    if prune_decision is not None:
        decisions_by_kind["PruneDecision"] = [prune_decision]
    if fusion_decision is not None:
        decisions_by_kind["FusionDecision"] = [fusion_decision]
    if loop_decision is not None:
        decisions_by_kind["LoopDecision"] = [loop_decision]

    vr = pipeline._emit_variant(variant, decisions_by_kind, workload)
    return vr.rtl_dir


# ─── The equivalence test runner ──────────────────────────────────


def compare_variant(name, legacy_root, new_root):
    """Print a byte-by-byte comparison summary for one variant.

    Returns True on byte-identical, False otherwise.
    """
    only_legacy, only_new, differ, common_count = diff_trees(legacy_root, new_root)
    print(
        f"  [{name:<14}] legacy={legacy_root.name}  new={new_root.name}  "
        f"common={common_count}  diffs={len(differ)}  "
        f"only_legacy={len(only_legacy)}  only_new={len(only_new)}"
    )
    if differ:
        for f in differ[:5]:
            print(f"      DIFFER: {f}")
    if only_legacy:
        for f in only_legacy[:3]:
            print(f"      only legacy: {f}")
    if only_new:
        for f in only_new[:3]:
            print(f"      only new:    {f}")
    return not (differ or only_legacy or only_new)


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
    workload = BenchmarkWorkload(bench_dir=bench_dir)

    print(f"Equivalence test: all 4 variants on workload={workload.name}")
    print()

    # Build decisions once.  PruneDecision via the live strategy;
    # FusionDecision/LoopDecision either real (when ARVIS_REAL_FUSION
    # is set + pickle present) or synthetic.
    prune_decision = build_prune_decision(target, workload)
    print(
        f"  PruneDecision: "
        f"{len(prune_decision.removable_alu_ops)} ALU, "
        f"{len(prune_decision.removable_mul_modes)} MUL, "
        f"{len(prune_decision.removable_opcode_groups)} groups"
    )

    if os.environ.get("ARVIS_REAL_FUSION") == "1":
        fusion_decision = build_fusion_decision_real(workload)
        if fusion_decision is None:
            print(
                "  WARNING: ARVIS_REAL_FUSION=1 but no .fusion_decision.pkl "
                "in workload dir; falling back to empty FusionDecision."
            )
            fusion_decision = build_fusion_decision_empty()
        else:
            print(
                f"  FusionDecision: {len(fusion_decision.fused_ops)} fused ops "
                f"(loaded from {workload.bench_dir.name}/.fusion_decision.pkl)"
            )
    else:
        fusion_decision = build_fusion_decision_empty()
        print(
            f"  FusionDecision: empty "
            f"(set ARVIS_REAL_FUSION=1 + ship .fusion_decision.pkl for real ops)"
        )

    loop_decision = build_loop_decision()
    print(
        f"  LoopDecision: "
        f"nest={loop_decision.nest_depth}, "
        f"cnt={loop_decision.counter_width}b, "
        f"addr={loop_decision.addr_width}b"
    )
    print()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        all_pass = True

        # ── Variant 1: BASELINE ────────────────────────────────
        legacy = emit_legacy_variant(target, tmp, label="baseline")
        new = emit_new_variant(
            target, workload, tmp, variant=BASELINE,
        )
        if not compare_variant("BASELINE", legacy, new):
            all_pass = False

        # ── Variant 2: PRUNED ──────────────────────────────────
        legacy = emit_legacy_variant(
            target, tmp, label="pruned",
            prune_decision=prune_decision,
        )
        new = emit_new_variant(
            target, workload, tmp, variant=PRUNED,
            prune_decision=prune_decision,
        )
        if not compare_variant("PRUNED", legacy, new):
            all_pass = False

        # ── Variant 3: FUSED_PRUNED ────────────────────────────
        # With empty FusionDecision this collapses to PRUNED + a
        # no-op fusion patch; still tests that orchestration is
        # correct.  With ARVIS_REAL_FUSION=1 + real ops, this
        # tests fusion-decoder generation byte-exactly.
        legacy = emit_legacy_variant(
            target, tmp, label="fused_pruned",
            prune_decision=prune_decision,
            fusion_decision=fusion_decision,
        )
        new = emit_new_variant(
            target, workload, tmp, variant=FUSED_PRUNED,
            prune_decision=prune_decision,
            fusion_decision=fusion_decision,
        )
        if not compare_variant("FUSED_PRUNED", legacy, new):
            all_pass = False

        # ── Variant 4: HWLOOP_PRUNED ───────────────────────────
        legacy = emit_legacy_variant(
            target, tmp, label="hwloop_pruned",
            prune_decision=prune_decision,
            loop_decision=loop_decision,
        )
        new = emit_new_variant(
            target, workload, tmp, variant=HWLOOP_PRUNED,
            prune_decision=prune_decision,
            loop_decision=loop_decision,
        )
        if not compare_variant("HWLOOP_PRUNED", legacy, new):
            all_pass = False

        # ── Variant 5: ALL ─────────────────────────────────────
        legacy = emit_legacy_variant(
            target, tmp, label="all",
            prune_decision=prune_decision,
            fusion_decision=fusion_decision,
            loop_decision=loop_decision,
        )
        new = emit_new_variant(
            target, workload, tmp, variant=ALL,
            prune_decision=prune_decision,
            fusion_decision=fusion_decision,
            loop_decision=loop_decision,
        )
        if not compare_variant("ALL", legacy, new):
            all_pass = False

        print()
        if all_pass:
            print("  ✓ EQUIVALENT for all 5 variants tested")
            return 0
        print("  ✗ MISMATCH on at least one variant — see above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
