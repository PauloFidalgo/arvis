"""Contract tests for :mod:`targets.cv32e40p.passes`.

The passes are pure free functions that mutate a workspace.
These tests verify:

1. **Importability** -- every documented function exposed in
   ``__all__`` exists and is callable.
2. **No-op safety** -- callers can invoke the passes on a
   freshly-copied workspace without raising even when
   the workload's analysis would otherwise produce empty inputs.
3. **Idempotency** -- re-running the same pass with the same
   inputs is safe (the second invocation may be a no-op or a
   restart, but it must not corrupt the workspace).

The byte-equivalence guarantees of these passes are exercised
by the higher-level ``examples/portability_equivalence.py`` and
``examples/portability_gateway_smoke.py`` harnesses; this file
keeps the focus on the *contract* of each pass.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from arvis.core import RTLWorkspace
from arvis.targets import CV32E40P


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def workspace():
    """Fresh, copied cv32e40p RTL workspace per test."""
    target = CV32E40P()
    out = Path(tempfile.mkdtemp(prefix="passes_test_"))
    ws = RTLWorkspace(source_root=target.rtl_root, output_root=out)
    ws.copy_fresh()
    return ws


# ─── Public API surface ────────────────────────────────────────────


def test_passes_module_exports_documented_names():
    """``__all__`` lists every public pass; each name is a callable."""
    from arvis.targets.cv32e40p import passes

    expected = {
        "apply_hwloop_pragmas",
        "apply_fusion_patches",
        "clear_fused_pragmas",
        "apply_feature_pragmas",
        "apply_debug_pragmas",
        "apply_pulp_pragmas",
        "apply_irq_pragmas",
        "apply_ctrl_pragmas",
        "apply_pulp_pragmas_on_dir",
        "apply_ctrl_pragmas_on_dir",
        "apply_dce_cleanup",
        "apply_encoding_optimization",
    }
    assert set(passes.__all__) == expected
    for name in expected:
        attr = getattr(passes, name, None)
        assert callable(attr), f"{name} is not callable"


# ─── Hwloop pragmas ───────────────────────────────────────────────


def test_apply_hwloop_pragmas_zero_count_is_safe(workspace):
    """``hw_loop_count == 0`` strips pragma blocks without raising."""
    from arvis.targets.cv32e40p.passes import apply_hwloop_pragmas

    stats = apply_hwloop_pragmas(workspace, hw_loop_count=0)
    assert isinstance(stats, list)


def test_apply_hwloop_pragmas_idempotent(workspace):
    """Two consecutive runs leave the workspace in the same state."""
    from arvis.targets.cv32e40p.passes import apply_hwloop_pragmas

    apply_hwloop_pragmas(workspace, hw_loop_count=0)
    snapshot_1 = sorted(
        (p, p.read_bytes())
        for p in (workspace.output_root / "rtl").rglob("*.sv")
    )
    apply_hwloop_pragmas(workspace, hw_loop_count=0)
    snapshot_2 = sorted(
        (p, p.read_bytes())
        for p in (workspace.output_root / "rtl").rglob("*.sv")
    )
    assert snapshot_1 == snapshot_2


# ─── Fusion patches ───────────────────────────────────────────────


def test_clear_fused_pragmas_idempotent(workspace):
    """Two consecutive calls leave the tree in a stable state.

    The first call may have content to clear; the second call
    operates on the already-cleared blocks and must be a no-op.
    """
    from arvis.targets.cv32e40p.passes import clear_fused_pragmas

    rtl_dir = workspace.output_root / "rtl"
    pkg = rtl_dir / "include" / "cv32e40p_pkg.sv"
    if not pkg.exists():
        pytest.skip("cv32e40p_pkg.sv not present in this RTL tree")

    clear_fused_pragmas(rtl_dir)
    snapshot_1 = pkg.read_bytes()
    clear_fused_pragmas(rtl_dir)
    snapshot_2 = pkg.read_bytes()
    assert snapshot_1 == snapshot_2


def test_apply_fusion_patches_empty_op_list_returns_true(workspace):
    """A workspace with the canonical anchors accepts an empty
    fused_operations list and returns success."""
    from arvis.targets.cv32e40p.passes import apply_fusion_patches

    rtl_dir = workspace.output_root / "rtl"
    if not (rtl_dir / "include" / "cv32e40p_pkg.sv").exists():
        pytest.skip("cv32e40p_pkg.sv not present in this RTL tree")
    if not (rtl_dir / "cv32e40p_decoder.sv").exists():
        pytest.skip("cv32e40p_decoder.sv not present in this RTL tree")
    if not (rtl_dir / "cv32e40p_alu.sv").exists():
        pytest.skip("cv32e40p_alu.sv not present in this RTL tree")

    ok = apply_fusion_patches(workspace, fused_operations=())
    assert ok is True


def test_apply_fusion_patches_callback_is_invoked(workspace):
    """When the workspace has all the anchors, the callback fires."""
    from arvis.targets.cv32e40p.passes import apply_fusion_patches

    rtl_dir = workspace.output_root / "rtl"
    if not (rtl_dir / "include" / "cv32e40p_pkg.sv").exists():
        pytest.skip("cv32e40p_pkg.sv not present in this RTL tree")
    if not (rtl_dir / "cv32e40p_decoder.sv").exists():
        pytest.skip("cv32e40p_decoder.sv not present in this RTL tree")
    if not (rtl_dir / "cv32e40p_alu.sv").exists():
        pytest.skip("cv32e40p_alu.sv not present in this RTL tree")

    fired = []
    apply_fusion_patches(
        workspace,
        fused_operations=(),
        fusion_applied_callback=lambda: fired.append(1),
    )
    assert fired == [1]


# ─── Feature pragmas ──────────────────────────────────────────────


def test_apply_debug_pragmas_runs(workspace):
    """The debug pragma processor runs on a fresh workspace."""
    from arvis.targets.cv32e40p.passes import apply_debug_pragmas

    stats = apply_debug_pragmas(workspace, enable_debug=True)
    assert isinstance(stats, list)


def test_apply_pulp_pragmas_runs(workspace):
    """The PULP pragma processor runs on a fresh workspace."""
    from arvis.targets.cv32e40p.passes import apply_pulp_pragmas

    stats = apply_pulp_pragmas(workspace, corev_pulp=0)
    assert isinstance(stats, list)


def test_apply_irq_pragmas_runs(workspace):
    """The IRQ pragma processor runs on a fresh workspace."""
    from arvis.targets.cv32e40p.passes import apply_irq_pragmas

    stats = apply_irq_pragmas(workspace, enable_interrupts=True)
    assert isinstance(stats, list)


def test_apply_ctrl_pragmas_runs(workspace):
    """The CTRL pragma processor runs on a fresh workspace."""
    from arvis.targets.cv32e40p.passes import apply_ctrl_pragmas

    n = apply_ctrl_pragmas(
        workspace, enable_interrupts=True, enable_debug=True
    )
    assert isinstance(n, int) and n >= 0


# ─── Cleanup passes ───────────────────────────────────────────────


def test_apply_dce_cleanup_runs(workspace):
    """DCE cleanup runs and returns a list (possibly empty)."""
    from arvis.targets.cv32e40p.passes import apply_dce_cleanup

    result = apply_dce_cleanup(workspace)
    assert isinstance(result, list)


def test_apply_encoding_optimization_runs(workspace):
    """Encoding optimisation runs without raising on a fresh workspace."""
    from arvis.targets.cv32e40p.passes import apply_encoding_optimization

    # Soft-skip pattern: the function returns None on missing
    # dependencies, but does not raise.
    result = apply_encoding_optimization(
        workspace, removable_mul_modes=(), verbose=False
    )
    # Either a result object or None; both are valid contract-wise.
    assert result is None or hasattr(result, "total_bits_saved")
