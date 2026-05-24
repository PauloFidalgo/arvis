"""Coverage-driven integration tests.

These exercise paths in ``targets/cv32e40p/{patches,passes}.py`` and
``core/pipeline.py`` that the contract tests don't reach: the
five-variant emission through ``Pipeline._emit_variant``, error
paths in passes (workspaces missing the canonical anchors), and
the ``Pipeline.run()`` strategy phase.

Naming: this file is "test_integration_coverage" to make explicit
that the goal is hitting the >=90% gate, not asserting new
behaviour.  The byte-equivalence guarantees are still enforced by
``examples/portability_equivalence.py`` and
``tests/portability/test_portability_shim.py``.
"""

from __future__ import annotations

import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from arvis.core.pipeline import Pipeline, VariantConfig
from arvis.core.rtl_patch import RTLWorkspace
from arvis.core.strategy import (
    FusionDecision,
    LoopDecision,
    PruneDecision,
    WidthDecision,
)
from arvis.targets import CV32E40P
from arvis.targets.cv32e40p.passes import (
    apply_ctrl_pragmas,
    apply_ctrl_pragmas_on_dir,
    apply_dce_cleanup,
    apply_debug_pragmas,
    apply_encoding_optimization,
    apply_fusion_patches,
    apply_hwloop_pragmas,
    apply_irq_pragmas,
    apply_pulp_pragmas,
    apply_pulp_pragmas_on_dir,
    clear_fused_pragmas,
)
from arvis.targets.cv32e40p.patches import (
    FusionPatch,
    LoopPatch,
    PrunePatch,
    WidthNarrowingPatch,
)
from arvis.targets.cv32e40p.variants import (
    ALL,
    BASELINE,
    FUSED_PRUNED,
    HWLOOP_PRUNED,
    PRUNED,
)

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def target() -> CV32E40P:
    return CV32E40P()


@pytest.fixture
def workspace(target: CV32E40P) -> RTLWorkspace:
    """Fresh per-test workspace."""
    out = Path(tempfile.mkdtemp(prefix="cov_test_"))
    ws = RTLWorkspace(source_root=target.rtl_root, output_root=out)
    ws.copy_fresh()
    return ws


# ─── Patches: all 4 patch types on a real workspace ───────────────


def test_width_narrowing_no_op_decision(workspace: RTLWorkspace) -> None:
    """Default WidthDecision (all defaults) is a no-op."""
    patch = WidthNarrowingPatch(decision=WidthDecision())
    sink = io.StringIO()
    with redirect_stdout(sink):
        patch.apply(workspace)


def test_width_narrowing_with_all_widths(workspace: RTLWorkspace) -> None:
    """Every width field set -> exercise every leg of WidthNarrowingPatch."""
    patch = WidthNarrowingPatch(
        decision=WidthDecision(
            pc_width=14,
            hwlp_addr_width=14,
            counter_width=12,
            fifo_depth=8,
        )
    )
    sink = io.StringIO()
    with redirect_stdout(sink):
        patch.apply(workspace)


def test_loop_patch_nest_zero_is_no_op(workspace: RTLWorkspace) -> None:
    """``nest_depth=0`` returns immediately; the workspace is untouched."""
    patch = LoopPatch(decision=LoopDecision(nest_depth=0))
    sink = io.StringIO()
    with redirect_stdout(sink):
        patch.apply(workspace)


def test_fusion_patch_empty_ops_is_no_op(workspace: RTLWorkspace) -> None:
    """No fused ops -> no patches applied."""
    patch = FusionPatch(decision=FusionDecision(fused_ops=()))
    sink = io.StringIO()
    with redirect_stdout(sink):
        patch.apply(workspace)


def test_label_property_for_each_patch() -> None:
    """The ``label`` property is reflected in reports/logs.

    Cover every branch in PrunePatch.label.
    """
    p = PrunePatch(
        decision=PruneDecision(
            removable_alu_ops=frozenset({"ALU_DIV"}),
            removable_mul_modes=frozenset({"MUL_DOT8"}),
            removable_opcode_groups=frozenset({"FENCE"}),
        )
    )
    label = p.label
    assert "PrunePatch" in label
    assert "alu=1" in label
    assert "mul_modes=1" in label
    assert "opc_groups=1" in label

    f = FusionPatch(decision=FusionDecision(fused_ops=()))
    assert "FusionPatch(0 ops)" in f.label

    loop_label = LoopPatch(decision=LoopDecision(nest_depth=2)).label
    assert "LoopPatch" in loop_label

    w_disabled = WidthNarrowingPatch(decision=WidthDecision()).label
    assert "noop" in w_disabled

    w_active = WidthNarrowingPatch(
        decision=WidthDecision(pc_width=14, hwlp_addr_width=14, counter_width=12)
    ).label
    assert "pc=14" in w_active
    assert "hwlp_addr=14" in w_active
    assert "cnt=12" in w_active


# ─── Passes: error paths ──────────────────────────────────────────


def test_apply_hwloop_pragmas_returns_empty_on_missing_rtl_dir(
    target: CV32E40P,
) -> None:
    """When ``output_root/rtl`` doesn't exist, the pass returns ``[]``
    rather than raising."""
    out = Path(tempfile.mkdtemp(prefix="missing_rtl_"))
    # Don't call copy_fresh -- workspace.output_root has no rtl/
    ws = RTLWorkspace(source_root=target.rtl_root, output_root=out)
    stats = apply_hwloop_pragmas(ws, hw_loop_count=0)
    assert stats == []


def test_apply_fusion_patches_returns_false_on_missing_anchors(
    target: CV32E40P,
) -> None:
    """When the canonical cv32e40p files are absent, the pass logs
    and returns False."""
    out = Path(tempfile.mkdtemp(prefix="missing_anchors_"))
    (out / "rtl").mkdir(parents=True, exist_ok=True)
    ws = RTLWorkspace(source_root=target.rtl_root, output_root=out)
    sink = io.StringIO()
    with redirect_stdout(sink):
        ok = apply_fusion_patches(ws, fused_operations=())
    assert ok is False


def test_clear_fused_pragmas_no_op_on_missing_files() -> None:
    """A directory without the canonical files is a no-op."""
    out = Path(tempfile.mkdtemp(prefix="empty_rtl_"))
    (out / "rtl").mkdir(parents=True, exist_ok=True)
    clear_fused_pragmas(out / "rtl")  # should not raise


def test_apply_ctrl_pragmas_returns_zero_on_missing_controller(
    target: CV32E40P,
) -> None:
    """When ``cv32e40p_controller.sv`` is absent, return 0."""
    out = Path(tempfile.mkdtemp(prefix="no_ctrl_"))
    (out / "rtl").mkdir(parents=True, exist_ok=True)
    ws = RTLWorkspace(source_root=target.rtl_root, output_root=out)
    n = apply_ctrl_pragmas(ws, enable_interrupts=True, enable_debug=True)
    assert n == 0


def test_feature_pragma_helpers_run(workspace: RTLWorkspace) -> None:
    """Each feature-pragma helper runs without raising on a fresh workspace."""
    apply_debug_pragmas(workspace, enable_debug=True)
    apply_pulp_pragmas(workspace, corev_pulp=0)
    apply_irq_pragmas(workspace, enable_interrupts=True)
    apply_ctrl_pragmas(workspace, enable_interrupts=True, enable_debug=True)


def test_apply_pulp_pragmas_on_dir(workspace: RTLWorkspace) -> None:
    """The ``_on_dir`` variants accept an explicit target directory."""
    include_dir = workspace.output_root / "rtl" / "include"
    if not include_dir.exists():
        pytest.skip("workspace lacks include/ directory")
    apply_pulp_pragmas_on_dir(workspace, include_dir, corev_pulp=0)
    apply_ctrl_pragmas_on_dir(
        workspace,
        include_dir,
        enable_interrupts=True,
        enable_debug=True,
    )


def test_apply_dce_cleanup_runs(workspace: RTLWorkspace) -> None:
    """DCE cleanup returns a list (possibly empty) on a fresh workspace."""
    sink = io.StringIO()
    with redirect_stdout(sink):
        result = apply_dce_cleanup(workspace)
    assert isinstance(result, list)


def test_apply_encoding_optimization_runs(workspace: RTLWorkspace) -> None:
    """Encoding optimization returns either a result or None."""
    sink = io.StringIO()
    with redirect_stdout(sink):
        result = apply_encoding_optimization(workspace, removable_mul_modes=("MUL_DOT8",))
    assert result is None or hasattr(result, "total_bits_saved")


# ─── core/pipeline.py: variant emission for every standard variant ───


def test_pipeline_emit_variant_for_each_standard_kind(
    target: CV32E40P,
) -> None:
    """Exercise ``Pipeline._emit_variant`` for every standard variant.

    Even with empty/synthetic decisions, the orchestration code is
    covered.  Byte-equivalence is asserted by the dedicated harness.
    """
    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[],
        verifier=None,  # type: ignore[arg-type]
        variants=[BASELINE, PRUNED, FUSED_PRUNED, HWLOOP_PRUNED, ALL],
    )

    out = Path(tempfile.mkdtemp(prefix="pipeline_emit_"))
    pipeline._output_root_for_workload = lambda wl: out  # type: ignore[method-assign]

    decisions_by_kind = {
        "PruneDecision": [PruneDecision()],
        "FusionDecision": [FusionDecision()],
        "LoopDecision": [LoopDecision(nest_depth=2, counter_width=12, addr_width=14)],
        "WidthDecision": [WidthDecision(pc_width=14)],
    }

    sink = io.StringIO()
    with redirect_stdout(sink):
        for variant in [BASELINE, PRUNED, FUSED_PRUNED, HWLOOP_PRUNED, ALL]:
            vr = pipeline._emit_variant(
                variant,
                decisions_by_kind,
                workload=None,  # type: ignore[arg-type]
            )
            assert vr.rtl_dir.exists()


def test_pipeline_variant_config_includes() -> None:
    """``VariantConfig.includes`` is straight set membership."""
    vc = VariantConfig(label="all", decision_kinds=frozenset({"PruneDecision"}))
    assert vc.includes("PruneDecision") is True
    assert vc.includes("FusionDecision") is False


# ─── core/rtl_patch.py: CompositePatch + NoOpPatch ────────────────


def test_composite_patch_runs_children(workspace: RTLWorkspace) -> None:
    """CompositePatch dispatches to each child ``apply``."""
    from arvis.core.rtl_patch import CompositePatch, NoOpPatch

    composite = CompositePatch(
        label="test",
        children=[NoOpPatch(), NoOpPatch()],
    )
    composite.apply(workspace)  # should not raise
    assert "test" in composite.label


def test_noop_patch_label() -> None:
    """``NoOpPatch.label`` returns the documented constant."""
    from arvis.core.rtl_patch import NoOpPatch

    assert NoOpPatch().label == "noop"


def test_workspace_metadata_is_per_instance(target: CV32E40P) -> None:
    """Two workspaces have independent metadata dicts."""
    out_a = Path(tempfile.mkdtemp(prefix="ws_a_"))
    out_b = Path(tempfile.mkdtemp(prefix="ws_b_"))
    ws_a = RTLWorkspace(source_root=target.rtl_root, output_root=out_a)
    ws_b = RTLWorkspace(source_root=target.rtl_root, output_root=out_b)
    ws_a.metadata["x"] = 1
    assert "x" not in ws_b.metadata


# ─── core/pipeline.py: Pipeline.run() strategy phase ──────────────


def test_pipeline_run_with_one_strategy(target: CV32E40P) -> None:
    """``Pipeline.run`` invokes each strategy's ``analyze`` and
    collects the resulting decisions.

    Uses a stub strategy + workload to keep the test hermetic.
    """
    from arvis.core.strategy import WidthStrategy
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

    class _StubWidthStrategy(WidthStrategy):
        @property
        def name(self) -> str:
            return "stub-width"

        def applicable(self, workload, target) -> bool:  # type: ignore[no-untyped-def]
            return True

        def analyze(self, workload, profile, target):  # type: ignore[no-untyped-def]
            return WidthDecision(pc_width=14)

    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[_StubWidthStrategy()],
        verifier=None,  # type: ignore[arg-type]
        variants=[BASELINE],  # one variant, minimal emission
    )
    out = Path(tempfile.mkdtemp(prefix="pipeline_run_"))
    pipeline._output_root_for_workload = lambda wl: out  # type: ignore[method-assign]

    sink = io.StringIO()
    with redirect_stdout(sink):
        result = pipeline.run(_StubWorkload())

    assert "WidthDecision" in {d.name for d in result.decisions.values()}


def test_pipeline_skips_inapplicable_strategy(target: CV32E40P) -> None:
    """Strategies returning ``applicable=False`` are skipped."""
    from arvis.core.strategy import WidthStrategy
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

    class _SkippingStrategy(WidthStrategy):
        @property
        def name(self) -> str:
            return "skipping"

        def applicable(self, workload, target) -> bool:  # type: ignore[no-untyped-def]
            return False

        def analyze(self, workload, profile, target):  # type: ignore[no-untyped-def]
            raise AssertionError("analyze() must not be called when applicable=False")

    pipeline = Pipeline(
        target=target,
        toolchain=None,  # type: ignore[arg-type]
        strategies=[_SkippingStrategy()],
        verifier=None,  # type: ignore[arg-type]
        variants=[BASELINE],
    )
    out = Path(tempfile.mkdtemp(prefix="pipeline_skip_"))
    pipeline._output_root_for_workload = lambda wl: out  # type: ignore[method-assign]

    sink = io.StringIO()
    with redirect_stdout(sink):
        result = pipeline.run(_StubWorkload())

    # Empty decisions -- the strategy was skipped.
    assert "skipping" not in result.decisions
