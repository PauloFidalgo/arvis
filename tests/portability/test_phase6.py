"""Contract tests for Phase 6 concrete implementations.

Covers:

* :class:`simulation.verifier.VerilatorVerifier` -- name + the
  graceful-failure path when Verilator can't build (no real RTL).
* :class:`synthesis.yosys_flow.YosysSynthesisFlow` -- name +
  no-yosys-available fallback to empty SynthResult.
* :class:`targets.cv32e40p.sweep_evaluators.HWLoopVariantEvaluator`
  -- end-to-end with mock pipeline, mock verifier, mock synth.
* :class:`core.workload.Workload.hex_for_variant` default returns
  ``None``.
* :class:`core.pipeline.Pipeline._emit_variant` calls verifier +
  synth when both are wired and ``hex_for_variant`` resolves.

The integration test for the full Phase 6 flow lives in
``tests/portability/test_pipeline_runner.py``; this file
focuses on unit-level contracts for the new concrete classes.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from arvis.core.sweep import SweepCandidate
from arvis.core.synthesis import SynthResult
from arvis.core.verifier import SimResult

# ─── VerilatorVerifier ────────────────────────────────────────────


def test_verilator_verifier_name() -> None:
    """``name`` is the canonical identifier used in reports."""
    from arvis.simulation.verifier import VerilatorVerifier

    v = VerilatorVerifier(rtl_root="/nonexistent")
    assert v.name == "verilator"


def test_verilator_verifier_returns_failed_on_missing_rtl(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Missing RTL -> Verilator can't build -> ``test_passed=False``."""
    from arvis.simulation.verifier import VerilatorVerifier

    v = VerilatorVerifier(
        rtl_root=str(tmp_path / "nonexistent_rtl"),
        sim_timeout_seconds=5,
    )
    sink = io.StringIO()
    with redirect_stdout(sink):
        result = v.simulate(
            rtl_dir=tmp_path / "rtl",
            hex_path=tmp_path / "fake.hex",
            max_cycles=1000,
        )

    assert isinstance(result, SimResult)
    assert result.test_passed is False
    # ``total_cycles`` defaults to -1 when the simulator never ran;
    # legacy code returns 0 for "ran-but-zero".  Either is OK
    # for "didn't pass".
    assert result.total_cycles in (-1, 0)


# ─── YosysSynthesisFlow ───────────────────────────────────────────


def test_yosys_synthesis_flow_name() -> None:
    from arvis.synthesis.yosys_flow import YosysSynthesisFlow

    flow = YosysSynthesisFlow()
    assert flow.name == "yosys"


def test_yosys_synthesis_flow_returns_empty_when_yosys_missing(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Forcing a missing Yosys binary -> ``SynthResult`` with no metrics."""
    from arvis.synthesis.yosys_flow import YosysSynthesisFlow

    flow = YosysSynthesisFlow(yosys_bin="/definitely/not/yosys")
    result = flow.synthesize(rtl_dir=tmp_path)

    assert isinstance(result, SynthResult)
    assert result.cell_count is None
    assert result.technology == "nangate45"


def test_yosys_synthesis_flow_handles_invalid_rtl_gracefully(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A real Yosys but a fake RTL dir -> returns SynthResult with no count."""
    from arvis.synthesis.yosys_flow import YosysSynthesisFlow

    flow = YosysSynthesisFlow()
    result = flow.synthesize(rtl_dir=tmp_path / "no_rtl_here")
    assert isinstance(result, SynthResult)
    # Either Yosys failed (cell_count None) or it ran on empty input
    # (cell_count 0); either is acceptable.  We just assert no
    # exception escapes.
    assert result.cell_count is None or result.cell_count == 0


# ─── HWLoopVariantEvaluator ───────────────────────────────────────


class _MockVerifier:
    """Verifier that returns canned SimResult based on cycle table."""

    def __init__(self, cycles_by_depth: dict[int, int]) -> None:
        self.cycles_by_depth = cycles_by_depth
        self._last_rtl_dir: Path | None = None

    @property
    def name(self) -> str:
        return "mock"

    def simulate(self, rtl_dir, hex_path, max_cycles=10_000_000, firmware_args=None):  # type: ignore[no-untyped-def]
        self._last_rtl_dir = rtl_dir
        # Decode the depth from any digit-suffixed component of
        # the path -- works whether ``rtl_dir`` is the workspace
        # root or the inner ``rtl/`` directory.
        for part in reversed(Path(rtl_dir).parts):
            try:
                depth = int(part.rsplit("_", maxsplit=1)[-1])
                break
            except ValueError:
                continue
        else:
            return SimResult(test_passed=False)
        cycles = self.cycles_by_depth.get(depth, 0)
        if cycles == 0:
            return SimResult(test_passed=False)
        return SimResult(test_passed=True, total_cycles=cycles)


class _MockSynth:
    """SynthesisFlow that returns canned cell counts by depth."""

    def __init__(self, cells_by_depth: dict[int, int]) -> None:
        self.cells_by_depth = cells_by_depth

    @property
    def name(self) -> str:
        return "mock-synth"

    def synthesize(self, rtl_dir, technology="nangate45", constraints=None):  # type: ignore[no-untyped-def]
        for part in reversed(Path(rtl_dir).parts):
            try:
                depth = int(part.rsplit("_", maxsplit=1)[-1])
                break
            except ValueError:
                continue
        else:
            return SynthResult()
        cells = self.cells_by_depth.get(depth, 0)
        if cells == 0:
            return SynthResult()
        return SynthResult(cell_count=cells)


def _build_pipeline_and_target(tmp_path: Path):
    """Build a real Pipeline + cv32e40p target wired with mock
    verifier + synth.  Used by the HWLoopVariantEvaluator tests."""
    from arvis.core.pipeline import Pipeline
    from arvis.targets import CV32E40P

    target = CV32E40P()
    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[],
        verifier=_MockVerifier(cycles_by_depth={2: 1000, 3: 800, 4: 700}),  # type: ignore[arg-type]
        synthesis=_MockSynth(cells_by_depth={2: 10000, 3: 10500, 4: 12000}),  # type: ignore[arg-type]
        variants=[],
    )
    pipeline._output_root_for_workload = lambda wl: tmp_path  # type: ignore[method-assign]
    return target, pipeline


def test_hwloop_variant_evaluator_returns_metrics_dict(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: emit + sim + synth via mocks, expect the full
    ``{cycles, cells, adp, passed}`` shape."""
    from arvis.targets.cv32e40p.sweep_evaluators import HWLoopVariantEvaluator

    target, pipeline = _build_pipeline_and_target(tmp_path)

    ev = HWLoopVariantEvaluator(
        target=target,
        pipeline=pipeline,
        output_dir=tmp_path,
        hex_path_for=lambda c: tmp_path / "fake.hex",
        base_decisions={},
    )

    sink = io.StringIO()
    with redirect_stdout(sink):
        metrics = ev(SweepCandidate(overrides={"HW_LOOP": 3}))

    assert metrics["passed"] == 1.0
    assert metrics["cycles"] == 800.0
    assert metrics["cells"] == 10500.0
    assert metrics["adp"] == 800.0 * 10500.0 / 1e9


def test_hwloop_variant_evaluator_failed_sim_returns_inf_adp(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """When the mocked simulator returns ``passed=False``, ADP is
    infinity and ``passed=0``."""
    from arvis.targets.cv32e40p.sweep_evaluators import HWLoopVariantEvaluator

    target, pipeline = _build_pipeline_and_target(tmp_path)
    # Depth 99 isn't in the mock table -> sim fails.
    ev = HWLoopVariantEvaluator(
        target=target,
        pipeline=pipeline,
        output_dir=tmp_path,
        hex_path_for=lambda c: tmp_path / "fake.hex",
        base_decisions={},
    )

    sink = io.StringIO()
    with redirect_stdout(sink):
        metrics = ev(SweepCandidate(overrides={"HW_LOOP": 99}))

    assert metrics["passed"] == 0.0
    assert metrics["adp"] == float("inf")


def test_hwloop_variant_evaluator_handles_emit_variant_failure(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """When ``Pipeline._emit_variant`` raises, the evaluator returns
    a failed metrics dict instead of propagating."""
    from arvis.targets.cv32e40p.sweep_evaluators import HWLoopVariantEvaluator

    target, pipeline = _build_pipeline_and_target(tmp_path)

    # Override _emit_variant to always raise.
    def boom(*a, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated failure")

    pipeline._emit_variant = boom  # type: ignore[method-assign]

    ev = HWLoopVariantEvaluator(
        target=target,
        pipeline=pipeline,
        output_dir=tmp_path,
        hex_path_for=lambda c: tmp_path / "fake.hex",
        base_decisions={},
    )

    sink = io.StringIO()
    with redirect_stdout(sink):
        metrics = ev(SweepCandidate(overrides={"HW_LOOP": 3}))

    assert metrics["passed"] == 0.0
    assert metrics["adp"] == float("inf")


def test_hwloop_variant_evaluator_handles_hex_resolution_failure(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """When ``hex_path_for`` raises, the evaluator returns a failed
    metrics dict (the variant emission still succeeded but we
    can't simulate without a hex)."""
    from arvis.targets.cv32e40p.sweep_evaluators import HWLoopVariantEvaluator

    target, pipeline = _build_pipeline_and_target(tmp_path)

    def picky_hex(c):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("fake.hex")

    ev = HWLoopVariantEvaluator(
        target=target,
        pipeline=pipeline,
        output_dir=tmp_path,
        hex_path_for=picky_hex,
        base_decisions={},
    )

    sink = io.StringIO()
    with redirect_stdout(sink):
        metrics = ev(SweepCandidate(overrides={"HW_LOOP": 3}))

    assert metrics["passed"] == 0.0
    assert metrics["adp"] == float("inf")


# ─── Workload.hex_for_variant default ────────────────────────────


def test_workload_hex_for_variant_default_is_none() -> None:
    """The base :class:`Workload` returns ``None`` from
    :meth:`hex_for_variant`."""
    from arvis.core.workload import (
        BuildRecipe,
        ExpectedResult,
        Workload,
        WorkloadProfile,
    )

    class _StubWorkload(Workload):
        @property
        def name(self) -> str:
            return "stub"

        @property
        def sources(self) -> list[Path]:
            return []

        @property
        def cflags(self) -> list[str]:
            return []

        @property
        def build_recipe(self) -> BuildRecipe:
            return BuildRecipe(name="stub", source_files=())

        @property
        def expected(self) -> ExpectedResult:
            return ExpectedResult(stdout="")

        def profile(self, toolchain: object) -> WorkloadProfile:  # type: ignore[override]
            return WorkloadProfile()

    wl = _StubWorkload()
    assert wl.hex_for_variant("baseline") is None
    assert wl.hex_for_variant("pruned") is None


# ─── Pipeline._emit_variant integration with verifier + synth ────


def test_pipeline_emit_variant_calls_verifier_when_hex_resolved(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """When the workload resolves a hex AND a verifier is wired,
    ``_emit_variant`` populates ``VariantResult.sim_result``."""
    from arvis.core.pipeline import Pipeline, VariantConfig
    from arvis.core.workload import (
        BuildRecipe,
        ExpectedResult,
        Workload,
        WorkloadProfile,
    )
    from arvis.targets import CV32E40P

    class _SimpleWorkload(Workload):
        @property
        def name(self) -> str:
            return "simple"

        @property
        def sources(self) -> list[Path]:
            return []

        @property
        def cflags(self) -> list[str]:
            return []

        @property
        def build_recipe(self) -> BuildRecipe:
            return BuildRecipe(name="simple", source_files=())

        @property
        def expected(self) -> ExpectedResult:
            return ExpectedResult(stdout="")

        def profile(self, toolchain: object) -> WorkloadProfile:  # type: ignore[override]
            return WorkloadProfile()

        def hex_for_variant(self, variant_label: str) -> Path | None:
            return tmp_path / "fake.hex"

    target = CV32E40P()
    verifier = _MockVerifier(cycles_by_depth={0: 12345})
    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[],
        verifier=verifier,  # type: ignore[arg-type]
        variants=[VariantConfig(label="0", decision_kinds=frozenset())],
    )
    pipeline._output_root_for_workload = lambda wl: tmp_path  # type: ignore[method-assign]

    sink = io.StringIO()
    with redirect_stdout(sink):
        result = pipeline.run(_SimpleWorkload())

    assert len(result.variants) == 1
    vr = result.variants[0]
    # The mock verifier looks at the workspace label suffix.  Our
    # variant label is "0", so cycles_by_depth[0]=12345.
    assert vr.sim_result is not None
    assert vr.sim_result.test_passed is True
    assert vr.sim_result.total_cycles == 12345


def test_pipeline_emit_variant_skips_sim_when_hex_is_none(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A workload that returns ``None`` from ``hex_for_variant``
    skips simulation; ``VariantResult.sim_result`` stays None."""
    from arvis.core.pipeline import Pipeline, VariantConfig
    from arvis.core.workload import (
        BuildRecipe,
        ExpectedResult,
        Workload,
        WorkloadProfile,
    )
    from arvis.targets import CV32E40P

    class _NoHexWorkload(Workload):
        @property
        def name(self) -> str:
            return "no_hex"

        @property
        def sources(self) -> list[Path]:
            return []

        @property
        def cflags(self) -> list[str]:
            return []

        @property
        def build_recipe(self) -> BuildRecipe:
            return BuildRecipe(name="no_hex", source_files=())

        @property
        def expected(self) -> ExpectedResult:
            return ExpectedResult(stdout="")

        def profile(self, toolchain: object) -> WorkloadProfile:  # type: ignore[override]
            return WorkloadProfile()

        # default hex_for_variant -> None

    target = CV32E40P()
    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[],
        verifier=_MockVerifier(cycles_by_depth={}),  # type: ignore[arg-type]
        variants=[VariantConfig(label="0", decision_kinds=frozenset())],
    )
    pipeline._output_root_for_workload = lambda wl: tmp_path  # type: ignore[method-assign]

    sink = io.StringIO()
    with redirect_stdout(sink):
        result = pipeline.run(_NoHexWorkload())

    assert len(result.variants) == 1
    vr = result.variants[0]
    assert vr.sim_result is None  # no hex -> no sim
    assert vr.hex_path is None
