"""Contract tests for :mod:`targets.cv32e40p.portability_shim`.

The shim is the bridge that lets ``RTLChangeSet.apply`` route
through the portable :class:`Pipeline` path.  These tests cover:

1. **Decision construction** -- every ``_build_*_decision``
   helper handles its inputs correctly, including absence
   sentinels (``None`` returns).
2. **``is_enabled`` predicate** -- env var + cfg attr both
   trigger the gateway; neither = legacy.
3. **End-to-end equivalence** -- ``emit_via_portability``
   on a real cv32e40p workspace produces a SystemVerilog tree
   byte-identical to the legacy ``cs.apply`` path on the
   ``ud`` benchmark (or any benchmark with .elf files).
4. **VariantConfig synthesis** -- the variant kinds reflect
   the present decisions exactly.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from arvis.core.strategy import (
    FusionDecision,
    LoopDecision,
    PruneDecision,
    WidthDecision,
)
from arvis.targets.cv32e40p.portability_shim import (
    _build_fusion_decision,
    _build_loop_decision,
    _build_prune_decision,
    _build_width_decision,
    _synth_variant_config,
    is_enabled,
)

# ─── is_enabled predicate ─────────────────────────────────────────


def test_is_enabled_false_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var, no cfg attr -> False (legacy path)."""
    monkeypatch.delenv("ARVIS_USE_PORTABILITY", raising=False)
    assert is_enabled() is False
    assert is_enabled(None) is False


def test_is_enabled_via_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ARVIS_USE_PORTABILITY=1`` flips the gateway on."""
    monkeypatch.setenv("ARVIS_USE_PORTABILITY", "1")
    assert is_enabled() is True


def test_is_enabled_via_cfg_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cfg.use_portability=True`` flips the gateway on."""
    monkeypatch.delenv("ARVIS_USE_PORTABILITY", raising=False)

    class _Cfg:
        use_portability = True

    assert is_enabled(_Cfg()) is True


def test_is_enabled_env_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both env and cfg disagree, env wins (admin override)."""
    monkeypatch.setenv("ARVIS_USE_PORTABILITY", "1")

    class _Cfg:
        use_portability = False

    assert is_enabled(_Cfg()) is True


# ─── Decision constructors: absence sentinels ─────────────────────


def test_build_prune_decision_returns_none_when_no_config() -> None:
    """``cs.prune_config is None`` -> no PruneDecision."""

    class _CS:
        prune_config = None
        used_instructions: set[str] = set()

    assert _build_prune_decision(_CS()) is None


def test_build_fusion_decision_returns_none_when_empty() -> None:
    """Empty fused_operations -> no FusionDecision."""

    class _CS:
        fused_operations: list[object] = []

    assert _build_fusion_decision(_CS()) is None


def test_build_fusion_decision_returns_none_when_attribute_missing() -> None:
    """``getattr`` default of ``None`` is treated as absence."""

    class _CS:
        fused_operations = None

    assert _build_fusion_decision(_CS()) is None


def test_build_loop_decision_returns_none_when_no_loop() -> None:
    """``hw_loop_count == 0`` -> no LoopDecision."""

    class _CS:
        hw_loop_count = 0

    assert _build_loop_decision(_CS()) is None


def test_build_loop_decision_constructs_when_active() -> None:
    """Positive ``hw_loop_count`` -> a typed LoopDecision."""

    class _CS:
        hw_loop_count = 2
        hw_loop_cnt_width = 12
        hw_loop_addr_width = 14

    d = _build_loop_decision(_CS())
    assert isinstance(d, LoopDecision)
    assert d.nest_depth == 2
    assert d.counter_width == 12
    assert d.addr_width == 14


def test_build_width_decision_returns_none_for_all_defaults() -> None:
    """No tuning at all -> no WidthDecision."""

    class _CS:
        pc_width = 0
        prefetch_fifo_depth = 0
        hw_loop_cnt_width = 32
        hw_loop_addr_width = 32

    assert _build_width_decision(_CS()) is None


def test_build_width_decision_constructs_when_pc_narrowed() -> None:
    """Any non-default field triggers a WidthDecision."""

    class _CS:
        pc_width = 14
        prefetch_fifo_depth = 0
        hw_loop_cnt_width = 32
        hw_loop_addr_width = 32

    d = _build_width_decision(_CS())
    assert isinstance(d, WidthDecision)
    assert d.pc_width == 14


def test_build_width_decision_constructs_for_fifo_depth() -> None:
    """``fifo_depth > 0`` is a non-default trigger."""

    class _CS:
        pc_width = 0
        prefetch_fifo_depth = 8
        hw_loop_cnt_width = 32
        hw_loop_addr_width = 32

    d = _build_width_decision(_CS())
    assert d is not None
    assert d.fifo_depth == 8


# ─── VariantConfig synthesis ──────────────────────────────────────


def test_synth_variant_config_decision_kinds_match():
    """``decision_kinds`` reflects the present decisions exactly."""
    decisions = {
        "PruneDecision": PruneDecision(),
        "FusionDecision": FusionDecision(),
    }
    vc = _synth_variant_config("fused_pruned", decisions)
    assert vc.label == "fused_pruned"
    assert vc.decision_kinds == frozenset({"PruneDecision", "FusionDecision"})


def test_synth_variant_config_empty_for_baseline():
    """No decisions -> empty kind set (the BASELINE shape)."""
    vc = _synth_variant_config("baseline", {})
    assert vc.decision_kinds == frozenset()


# ─── End-to-end emit_via_portability vs legacy ────────────────────


@pytest.fixture
def benchmark_dir() -> Path:
    """Pick the first benchmark with a real ``.elf`` file."""
    bench_root = Path(__file__).resolve().parent.parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        pytest.skip(f"No benchmarks directory at {bench_root}")
    for d in sorted(bench_root.iterdir()):
        if d.is_dir() and any(d.glob("*.elf")):
            return d
    pytest.skip(f"No benchmark with .elf files under {bench_root}")
    return Path()  # unreachable; keeps mypy happy


def _build_prune_config(prune_decision: PruneDecision):
    """Reconstruct a legacy ``PruneConfig`` from a typed decision."""
    from arvis.codegen.rtl.rtl_pruning import PruneConfig

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
    """Permissive ToolConfig shim mirroring the gateway smoke."""

    def __init__(self, rtl_root: str, output_dir: str, *, use_portability: bool) -> None:
        self.rtl_root = rtl_root
        self.output_dir = output_dir
        self.use_portability = use_portability
        self.prune_rf_read_c = False
        self.prune_rf_write_b = False
        self.enable_debug = True
        self.enable_hpm = True

    def __getattr__(self, name: str):
        if name.startswith("enable_"):
            return True
        if name.startswith("prune_"):
            return False
        return None


class _Ctx:
    """Permissive PipelineContext shim."""

    def __init__(self) -> None:
        self.rtl_output_dir: str | None = None
        self.gcc_compile_result = None

    def __getattr__(self, name: str):
        return None


def test_emit_via_portability_pruned_byte_equivalent(benchmark_dir: Path) -> None:
    """End-to-end: gateway PRUNED == legacy PRUNED, byte-for-byte."""
    pytest.importorskip("codegen.rtl.rtl_pruning")
    from arvis.pipeline.rtl_changeset import RTLChangeSet
    from arvis.strategies.pruning.usage_driven import UsageDrivenPruner
    from arvis.targets.cv32e40p.core import CV32E40P
    from arvis.workloads.benchmark import BenchmarkWorkload

    # Build a real PruneDecision via the live strategy.
    target = CV32E40P()
    workload = BenchmarkWorkload(bench_dir=benchmark_dir)
    sink = io.StringIO()
    with redirect_stdout(sink):
        prune_decision = UsageDrivenPruner(verbose=False).analyze(
            workload=workload,
            profile=workload.profile(toolchain=None),  # type: ignore[arg-type]
            target=target,
        )

    pc = _build_prune_config(prune_decision)

    def _emit(use_portability: bool, label: str = "pruned", *, hw_loop_count: int = 0) -> Path:
        out = Path(tempfile.mkdtemp(prefix=f"shim_test_{label}_{use_portability}_"))
        cs = RTLChangeSet()
        cs.add_prune_config(pc, set(prune_decision.used_instructions))
        if hw_loop_count > 0:
            cs.hw_loop_count = hw_loop_count
            cs.hw_loop_cnt_width = 12
            cs.hw_loop_addr_width = 14
        cs._apply_label = label
        cfg = _Cfg(str(target.rtl_root), str(out), use_portability=use_portability)
        ctx = _Ctx()
        with redirect_stdout(io.StringIO()):
            cs.apply(cfg, ctx, verbose=False)
        return Path(ctx.rtl_output_dir)  # type: ignore[arg-type]

    # PRUNED
    legacy_out = _emit(use_portability=False, label="pruned")
    portab_out = _emit(use_portability=True, label="pruned")
    result = subprocess.run(
        ["diff", "-r", "-q", str(legacy_out / "rtl"), str(portab_out / "rtl")],
        capture_output=True,
        text=True,
    )
    diffs = [line for line in result.stdout.splitlines() if line.strip()]
    assert not diffs, f"Gateway diverges from legacy on PRUNED: {diffs[:5]}"

    # HWLOOP_PRUNED -- exercises the LoopDecision path in emit_via_portability
    legacy_h = _emit(use_portability=False, label="hwloop_pruned", hw_loop_count=2)
    portab_h = _emit(use_portability=True, label="hwloop_pruned", hw_loop_count=2)
    result_h = subprocess.run(
        ["diff", "-r", "-q", str(legacy_h / "rtl"), str(portab_h / "rtl")],
        capture_output=True,
        text=True,
    )
    diffs_h = [line for line in result_h.stdout.splitlines() if line.strip()]
    assert not diffs_h, f"Gateway diverges from legacy on HWLOOP_PRUNED: {diffs_h[:5]}"
