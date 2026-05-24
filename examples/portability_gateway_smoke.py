"""Phase 2.8 gateway smoke test.

Verifies that ``RTLChangeSet.apply`` produces byte-identical RTL
whether the legacy emission path (default) or the portability
gateway (``--use-portability`` / ``ARVIS_USE_PORTABILITY=1``)
is selected.

This is the regression discipline guard for Phase 2.8: it
exercises the actual dispatch site in
:meth:`pipeline.rtl_changeset.RTLChangeSet.apply` -- not the
private :func:`Pipeline._emit_variant` path that
``examples/portability_equivalence.py`` uses.

The test runs four variants (PRUNED, FUSED_PRUNED,
HWLOOP_PRUNED, ALL) on the ``ud`` benchmark.  BASELINE is
skipped because it doesn't use cs.apply (the runner uses
``verification.run_baseline_sim`` directly).

Exit code 0 = all variants byte-equivalent.
Exit code 1 = at least one variant differs.

Usage::

    python3 examples/portability_gateway_smoke.py
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Path setup so we can import the project + the equivalence helpers
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from portability_equivalence import build_prune_decision  # noqa: E402

from arvis.codegen.rtl.rtl_pruning import PruneConfig  # noqa: E402
from arvis.pipeline.rtl_changeset import RTLChangeSet  # noqa: E402
from arvis.targets.cv32e40p.core import CV32E40P  # noqa: E402
from arvis.workloads.benchmark import BenchmarkWorkload  # noqa: E402


def _build_prune_config(prune_decision):
    pc = PruneConfig()
    pc.removable_alu_ops = set(prune_decision.removable_alu_ops)
    pc.removable_mul_modes = set(prune_decision.removable_mul_modes)
    pc.removable_opcode_groups = set(prune_decision.removable_opcode_groups)
    pc.removable_csr_labels = set(prune_decision.removable_csr_labels)
    pc.removable_csr_storage = set(prune_decision.removable_csr_storage)
    for k, v in prune_decision.feature_flags.items():
        if hasattr(pc, k) and isinstance(getattr(pc, k), bool):
            setattr(pc, k, v)
    pc.unused_registers = list(prune_decision.unused_registers)
    pc.used_regs_mask = prune_decision.used_regs_mask
    for attr, value in prune_decision.target_overlay.items():
        if hasattr(pc, attr) and isinstance(getattr(pc, attr), int):
            setattr(pc, attr, value)
    return pc


class _Cfg:
    """Permissive ToolConfig shim."""

    def __init__(self, rtl_root, output_dir, use_portability):
        self.rtl_root = rtl_root
        self.output_dir = output_dir
        self.use_portability = use_portability
        self.prune_rf_read_c = False
        self.prune_rf_write_b = False
        self.enable_debug = True
        self.enable_hpm = True

    def __getattr__(self, name):
        if name.startswith("enable_"):
            return True
        if name.startswith("prune_"):
            return False
        return None


class _Ctx:
    """Permissive PipelineContext shim."""

    def __init__(self):
        self.rtl_output_dir = None
        self.gcc_compile_result = None

    def __getattr__(self, name):
        return None


def _build_cs(*, prune_decision, has_fusion, hw_loop_count):
    """Build an RTLChangeSet for a given variant."""
    cs = RTLChangeSet()
    cs.add_prune_config(
        _build_prune_config(prune_decision),
        set(prune_decision.used_instructions),
    )
    if has_fusion:
        # Empty fused_operations is the default; with a real
        # Docker/GCC pipeline you'd populate it from the analysis
        # phase.  For the smoke test we only verify the dispatch
        # path; the equivalence harness covers fused-vs-no-fused.
        pass
    if hw_loop_count > 0:
        cs.hw_loop_count = hw_loop_count
        cs.hw_loop_cnt_width = 12
        cs.hw_loop_addr_width = 14
    return cs


def _emit(cs, *, label, target, use_portability):
    out = Path(tempfile.mkdtemp(prefix=f"gateway_{label}_"))
    cs._apply_label = label
    cfg = _Cfg(str(target.rtl_root), str(out), use_portability)
    ctx = _Ctx()
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        cs.apply(cfg, ctx, verbose=False)
    return Path(ctx.rtl_output_dir)


def _diff_trees(a, b):
    result = subprocess.run(
        ["diff", "-r", "-q", str(a / "rtl"), str(b / "rtl")],
        capture_output=True,
        text=True,
    )
    return [l for l in result.stdout.splitlines() if l.strip()]


def main() -> int:
    bench_root = (
        Path(__file__).resolve().parent.parent / "targets" / "benchmarks"
    )
    bench_dir = bench_root / "ud"
    if not bench_dir.exists():
        # Fallback: any sub-directory with an .elf -- mirrors
        # examples/portability_equivalence.py.
        candidates = (
            [d for d in bench_root.iterdir() if d.is_dir() and any(d.glob("*.elf"))]
            if bench_root.exists()
            else []
        )
        if not candidates:
            print(
                f"  ✗ No benchmark with .elf files under {bench_root}",
                file=sys.stderr,
            )
            return 1
        bench_dir = candidates[0]
        print(f"  (ud not present -- falling back to {bench_dir.name})\n")

    target = CV32E40P()
    workload = BenchmarkWorkload(bench_dir=bench_dir)

    # Run analysis once to get the prune decision.
    prune_decision = build_prune_decision(target, workload)

    variants = [
        ("pruned", dict(prune_decision=prune_decision, has_fusion=False, hw_loop_count=0)),
        ("fused_pruned", dict(prune_decision=prune_decision, has_fusion=True, hw_loop_count=0)),
        ("hwloop_pruned", dict(prune_decision=prune_decision, has_fusion=False, hw_loop_count=2)),
        ("all", dict(prune_decision=prune_decision, has_fusion=True, hw_loop_count=2)),
    ]

    print("\n=== Phase 2.8 gateway smoke (cs.apply through both paths) ===\n")
    all_pass = True
    for label, kwargs in variants:
        # Two fresh changesets -- side-effects of cs.apply may persist
        # on the instance (registry caching, etc.).
        cs_legacy = _build_cs(**kwargs)
        cs_portab = _build_cs(**kwargs)

        legacy_out = _emit(cs_legacy, label=label, target=target, use_portability=False)
        portab_out = _emit(cs_portab, label=label, target=target, use_portability=True)

        diffs = _diff_trees(legacy_out, portab_out)
        n_legacy = len(list((legacy_out / "rtl").rglob("*.sv")))
        n_portab = len(list((portab_out / "rtl").rglob("*.sv")))

        ok = not diffs and n_legacy == n_portab
        marker = "✓" if ok else "✗"
        print(
            f"  {marker} [{label:<14}] legacy={n_legacy} files, "
            f"portab={n_portab} files, diffs={len(diffs)}"
        )
        if diffs:
            for d in diffs[:3]:
                print(f"      {d}")
            all_pass = False

    print()
    if all_pass:
        print("  ✓ Gateway path BYTE-EQUIVALENT to legacy on all 4 cs.apply variants")
        return 0
    print("  ✗ Gateway path DIFFERS from legacy")
    return 1


if __name__ == "__main__":
    sys.exit(main())
