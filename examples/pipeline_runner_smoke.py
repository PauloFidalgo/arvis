"""Phase 6 end-to-end Pipeline.run() smoke harness.

Exercises the full architecture in one invocation:

  1. Build a CV32E40P target.
  2. Construct a BenchmarkWorkload from the test ELFs.
  3. Wire the four abstract roles to concrete implementations:
        Verifier      -> VerilatorVerifier
        SynthesisFlow -> YosysSynthesisFlow
        Reporter      -> MarkdownReporter
        Toolchain     -> NullToolchain (we don't compile here;
                                         the workload ships
                                         pre-built ELFs)
  4. Run a UsageDrivenPruner strategy to produce a PruneDecision.
  5. Emit the BASELINE + PRUNED variants via Pipeline.run().
  6. Skip simulation (Verilator + benchmark hex isn't always
     available in CI); the smoke instead asserts the report
     file is produced and lists the right variants.

Why limited variant set
-----------------------

Running all 5 variants end-to-end with real Verilator + Yosys
takes minutes per variant.  This smoke validates that the
*architecture* works end-to-end; the full byte-equivalence
check lives in ``examples/portability_equivalence.py`` and
``examples/portability_gateway_smoke.py``.

Usage
-----

    .venv/bin/python examples/pipeline_runner_smoke.py

Exit code 0 = pipeline completed and produced a report.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

# Path setup so we can import the project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from arvis.core.isa import ISADescriptor
    from arvis.core.logging_config import configure_logging
    from arvis.core.pipeline import Pipeline
    from arvis.core.toolchain import Toolchain
    from arvis.report.markdown_reporter import MarkdownReporter
    from arvis.simulation.verifier import VerilatorVerifier
    from arvis.strategies.pruning.usage_driven import UsageDrivenPruner
    from arvis.synthesis.yosys_flow import YosysSynthesisFlow
    from arvis.targets import CV32E40P
    from arvis.targets.cv32e40p.variants import BASELINE, PRUNED
    from arvis.workloads.benchmark import BenchmarkWorkload

    configure_logging(level=logging.INFO)

    # ── 1. Resolve a benchmark with .elf files ─────────────────
    bench_root = Path(__file__).resolve().parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        print(f"  ✗ No benchmarks directory at {bench_root}", file=sys.stderr)
        return 1

    bench_dir = bench_root / "ud"
    if not bench_dir.exists():
        candidates = [d for d in bench_root.iterdir() if d.is_dir() and any(d.glob("*.elf"))]
        if not candidates:
            print(f"  ✗ No benchmark with .elf under {bench_root}", file=sys.stderr)
            return 1
        bench_dir = candidates[0]

    print(f"\n  Benchmark: {bench_dir.name}")

    # ── 2. Build the four roles ────────────────────────────────
    target = CV32E40P()
    workload = BenchmarkWorkload(bench_dir=bench_dir)

    class _NullToolchain(Toolchain):
        @property
        def name(self) -> str:
            return "null"

        @property
        def isa(self) -> ISADescriptor:
            return ISADescriptor(name="rv32i", xlen=32, standard_extensions=("i",))

        def compile(self, workload, cflags=(), output_dir=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError("smoke test does not compile")

        def assemble(self, sources, cflags=(), output=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def disassemble(self, elf_path):  # type: ignore[no-untyped-def]
            return ""

    verifier = VerilatorVerifier(rtl_root=str(target.rtl_root))
    synthesis = YosysSynthesisFlow()
    reporter = MarkdownReporter()

    print(f"  Target:    {target.name}")
    print(f"  Verifier:  {verifier.name}")
    print(f"  Synthesis: {synthesis.name}")
    print(f"  Reporter:  {reporter.name}")

    # ── 3. Build the pipeline ───────────────────────────────────
    out_dir = Path(tempfile.mkdtemp(prefix="pipeline_smoke_"))

    pipeline = Pipeline(
        target=target,
        toolchain=_NullToolchain(),
        strategies=[UsageDrivenPruner(verbose=False)],
        verifier=verifier,
        synthesis=synthesis,
        reporter=reporter,
        variants=[BASELINE, PRUNED],
    )
    pipeline._output_root_for_workload = lambda wl: out_dir  # type: ignore[method-assign]

    # ── 4. Run ──────────────────────────────────────────────────
    print(f"\n  Running pipeline... (output dir: {out_dir})")
    result = pipeline.run(workload)

    # ── 5. Validate ─────────────────────────────────────────────
    print("\n  ── Result summary ──")
    print(f"  Workload:  {result.workload_name}")
    print(f"  Target:    {result.target_name}")
    print(f"  Decisions: {len(result.decisions)}")
    for name in sorted(result.decisions.keys()):
        print(f"    - {name}")
    print(f"  Variants:  {len(result.variants)}")
    for v in result.variants:
        rtl_status = "✓" if v.rtl_dir.exists() else "✗"
        sim_status = "—"
        if v.sim_result is not None:
            sim_status = "✓ pass" if v.sim_result.test_passed else "✗ fail"
        synth_status = "—"
        if v.synth_result is not None and v.synth_result.cell_count is not None:
            synth_status = f"{v.synth_result.cell_count:,} cells"
        print(f"    - {v.label:<10s}  RTL: {rtl_status}  sim: {sim_status}  synth: {synth_status}")

    # The report should exist.
    report_path = out_dir / "report.md"
    if not report_path.exists():
        print(f"\n  ✗ Report file not produced: {report_path}", file=sys.stderr)
        return 1

    print(f"\n  ✓ Report: {report_path}")
    print("\n  ── Report preview (first 30 lines) ──")
    for line in report_path.read_text().splitlines()[:30]:
        print(f"  {line}")

    print("\n  ✓ Pipeline.run() end-to-end smoke PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
