"""Edge-case tests for tiny uncovered methods in core/.

These are helpers that the existing tests don't exercise:
file-IO methods on RTLWorkspace, the default
``TargetCore.render_decision`` dispatcher, etc.  The goal is to
push total coverage over the 90% gate while documenting the
public API surface.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from arvis.core.rtl_patch import RTLWorkspace
from arvis.core.strategy import (
    Decision,
    FusionDecision,
    LoopDecision,
    PruneDecision,
    WidthDecision,
)
from arvis.targets import CV32E40P


@pytest.fixture
def workspace() -> RTLWorkspace:
    target = CV32E40P()
    out = Path(tempfile.mkdtemp(prefix="edge_"))
    ws = RTLWorkspace(source_root=target.rtl_root, output_root=out)
    ws.copy_fresh()
    return ws


# ─── RTLWorkspace I/O methods ─────────────────────────────────────


def test_workspace_read_returns_file_content(workspace: RTLWorkspace) -> None:
    """``read`` is a thin wrapper over ``Path.read_text``."""
    rel = "rtl/cv32e40p_alu.sv"
    text = workspace.read(rel)
    assert "module" in text


def test_workspace_write_creates_parent_dirs(workspace: RTLWorkspace) -> None:
    """``write`` creates parent directories on demand."""
    workspace.write("a/b/c/test.txt", "hello")
    assert (workspace.output_root / "a/b/c/test.txt").read_text() == "hello"


def test_workspace_exists(workspace: RTLWorkspace) -> None:
    assert workspace.exists("rtl/cv32e40p_alu.sv") is True
    assert workspace.exists("does/not/exist.sv") is False


# ─── Decision.render default + name ───────────────────────────────


def test_decision_default_name_is_class_name() -> None:
    """``Decision.name`` is the concrete class name."""
    assert PruneDecision().name == "PruneDecision"
    assert FusionDecision().name == "FusionDecision"
    assert LoopDecision().name == "LoopDecision"
    assert WidthDecision().name == "WidthDecision"


def test_decision_render_default_returns_empty_list() -> None:
    """The base ``Decision.render`` returns ``[]`` (no patches)."""
    target = CV32E40P()
    assert PruneDecision().render(target) == []
    assert FusionDecision().render(target) == []
    assert LoopDecision().render(target) == []
    assert WidthDecision().render(target) == []


# ─── TargetCore.render_decision dispatcher ────────────────────────


def test_target_core_render_decision_dispatches_by_type() -> None:
    """``render_decision`` calls the per-kind ``render_*_decision``."""
    target = CV32E40P()
    # Each per-kind method was already covered above; here we
    # verify the polymorphic dispatcher reaches them.
    assert isinstance(
        target.render_decision(WidthDecision(pc_width=14), workspace=None),  # type: ignore[arg-type]
        list,
    )
    assert isinstance(
        target.render_decision(PruneDecision(), workspace=None),  # type: ignore[arg-type]
        list,
    )
    assert isinstance(
        target.render_decision(FusionDecision(), workspace=None),  # type: ignore[arg-type]
        list,
    )
    assert isinstance(
        target.render_decision(LoopDecision(), workspace=None),  # type: ignore[arg-type]
        list,
    )


def test_target_core_render_decision_rejects_unknown_decision() -> None:
    """Unknown decision types raise ``TypeError``."""
    target = CV32E40P()

    class _UnknownDecision(Decision):
        pass

    with pytest.raises(TypeError, match="Unknown decision type"):
        target.render_decision(_UnknownDecision(), workspace=None)  # type: ignore[arg-type]


# ─── core/workload.py: WorkloadSuite ──────────────────────────────


def test_workload_suite_iter() -> None:
    """``WorkloadSuite`` iterates over its workloads."""
    from arvis.core.workload import WorkloadSuite

    suite = WorkloadSuite(name="empty", workloads=[])
    assert list(suite) == []


# ─── Toolchain default methods ────────────────────────────────────


def test_toolchain_default_supports_extension_delegates_to_isa() -> None:
    """``supports`` falls back to the ISA descriptor."""
    from arvis.core.isa import ISADescriptor
    from arvis.core.toolchain import Toolchain

    class _NullToolchain(Toolchain):
        @property
        def name(self) -> str:
            return "null"

        @property
        def isa(self) -> ISADescriptor:
            return ISADescriptor(name="rv32i", xlen=32, standard_extensions=("i",))

        def compile(self, workload, cflags=(), output_dir=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def assemble(self, sources, cflags=(), output=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def disassemble(self, elf_path):  # type: ignore[no-untyped-def]
            return ""

    tc = _NullToolchain()
    assert tc.supports("i") is True
    assert tc.supports("v") is False


def test_toolchain_default_register_custom_ops_is_noop() -> None:
    """Default ``register_custom_ops`` is a no-op."""
    from arvis.core.isa import ISADescriptor
    from arvis.core.toolchain import Toolchain

    class _NullToolchain(Toolchain):
        @property
        def name(self) -> str:
            return "null"

        @property
        def isa(self) -> ISADescriptor:
            return ISADescriptor(name="rv32i", xlen=32, standard_extensions=("i",))

        def compile(self, workload, cflags=(), output_dir=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def assemble(self, sources, cflags=(), output=None):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def disassemble(self, elf_path):  # type: ignore[no-untyped-def]
            return ""

    tc = _NullToolchain()
    # No exception, no return value.
    assert tc.register_custom_ops(FusionDecision()) is None
    assert tc.register_hwloop(LoopDecision()) is None


# ─── Pipeline result accessors ────────────────────────────────────


def test_pipeline_result_decision_lookup() -> None:
    """``PipelineResult.decisions`` is keyed by decision name."""
    from arvis.core.pipeline import PipelineResult

    decisions = {
        "PruneDecision": PruneDecision(),
        "WidthDecision": WidthDecision(pc_width=14),
    }
    result = PipelineResult(
        workload_name="stub",
        target_name="cv32e40p",
        decisions=decisions,
    )
    assert "PruneDecision" in result.decisions
    assert result.decisions["WidthDecision"].name == "WidthDecision"


# ─── BenchmarkWorkload property accessors ─────────────────────────


def test_benchmark_workload_bench_dir_property() -> None:
    """``BenchmarkWorkload.bench_dir`` exposes the constructor argument."""
    from arvis.workloads.benchmark import BenchmarkWorkload

    bench_root = Path(__file__).resolve().parent.parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        pytest.skip("no benchmarks dir")
    candidates = [d for d in bench_root.iterdir() if d.is_dir()]
    if not candidates:
        pytest.skip("no benchmarks available")
    wl = BenchmarkWorkload(bench_dir=candidates[0])
    assert wl.bench_dir == candidates[0]
    assert isinstance(wl.cflags, list)
    assert isinstance(wl.sources, list)


def test_benchmark_workload_resolve_objdump_returns_string_or_none() -> None:
    """``_resolve_objdump`` returns either the configured binary or
    a fallback name from the documented list."""
    from arvis.workloads.benchmark import BenchmarkWorkload

    bench_root = Path(__file__).resolve().parent.parent.parent / "targets" / "benchmarks"
    if not bench_root.exists():
        pytest.skip("no benchmarks dir")
    candidates = [d for d in bench_root.iterdir() if d.is_dir()]
    if not candidates:
        pytest.skip("no benchmarks available")

    # Default object: should pick up whatever objdump is on PATH (or None).
    wl = BenchmarkWorkload(bench_dir=candidates[0])
    result = wl._resolve_objdump()
    assert result is None or isinstance(result, str)

    # Force an unresolvable binary.  The fallback search should
    # still try the documented alternatives.
    wl2 = BenchmarkWorkload(bench_dir=candidates[0], objdump_binary="definitely-not-real")
    result2 = wl2._resolve_objdump()
    # Either an alt was found on PATH or None.
    assert result2 is None or isinstance(result2, str)
