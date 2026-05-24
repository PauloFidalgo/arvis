"""Tests for ``core.rtl_patch.RTLPatch`` and concrete cv32e40p patches."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from arvis.core import RTLWorkspace, WidthDecision, PruneDecision
from arvis.core.rtl_patch import CompositePatch, NoOpPatch
from arvis.targets import CV32E40P
from arvis.targets.cv32e40p.patches import WidthNarrowingPatch, PrunePatch, FusionPatch, LoopPatch
from arvis.core.strategy import FusionDecision, LoopDecision


# ─── Workspace fixture ─────────────────────────────────────────────


def _has_hwlp_anchor(target) -> bool:
    """The PC width surgery uses HWLP_ADDR_WIDTH as a regex anchor.

    Trees that haven't had the HWLP_ADDR_WIDTH parameter introduced
    (e.g. an early-baseline cv32e40p) cannot have PC_WIDTH inserted
    by ``apply_pc_width.patch_rtl_dir``.  Tests that depend on
    PC_WIDTH presence are skipped on such trees.
    """
    top = target.rtl_root / "rtl" / "cv32e40p_top.sv"
    if not top.exists():
        return False
    return "parameter HWLP_ADDR_WIDTH" in top.read_text()


@pytest.fixture
def cv32_workspace():
    """A freshly-copied cv32e40p workspace in a temp directory."""
    target = CV32E40P()
    with tempfile.TemporaryDirectory() as tmp:
        ws = RTLWorkspace(target.rtl_root, Path(tmp) / "rtl_test")
        ws.copy_fresh()
        yield ws


@pytest.fixture
def cv32_target():
    return CV32E40P()


PC_WIDTH_PRECONDITION_MSG = (
    "RTL templates lack HWLP_ADDR_WIDTH parameter (apply_pc_width "
    "uses it as anchor to insert PC_WIDTH); tree predates the "
    "Phase 4 hwloop work."
)


# ─── NoOpPatch + CompositePatch ────────────────────────────────────


class TestBasePatches:
    def test_noop_patch_label(self):
        p = NoOpPatch()
        assert p.label == "noop"

    def test_noop_patch_apply_does_nothing(self, cv32_workspace):
        before = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        NoOpPatch().apply(cv32_workspace)
        after = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        assert before == after

    def test_composite_patch_runs_children_in_order(self, cv32_workspace):
        events = []

        class Child(NoOpPatch):
            def __init__(self, name):
                self._name = name

            def apply(self, workspace):
                events.append(self._name)

        comp = CompositePatch("test", [Child("a"), Child("b"), Child("c")])
        comp.apply(cv32_workspace)
        assert events == ["a", "b", "c"]


# ─── WidthNarrowingPatch ───────────────────────────────────────────


class TestWidthNarrowingPatch:
    def test_pc_width_only(self, cv32_workspace, cv32_target):
        if not _has_hwlp_anchor(cv32_target):
            pytest.skip(PC_WIDTH_PRECONDITION_MSG)
        patch = WidthNarrowingPatch(decision=WidthDecision(pc_width=14))
        patch.apply(cv32_workspace)
        top = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        m = re.search(r"parameter PC_WIDTH = (\d+)", top)
        assert m is not None
        assert m.group(1) == "14"

    def test_hwlp_addr_width(self, cv32_workspace, cv32_target):
        if not _has_hwlp_anchor(cv32_target):
            pytest.skip(PC_WIDTH_PRECONDITION_MSG)
        patch = WidthNarrowingPatch(decision=WidthDecision(hwlp_addr_width=12))
        patch.apply(cv32_workspace)
        top = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        m = re.search(r"parameter HWLP_ADDR_WIDTH\s*=\s*(\d+)", top)
        assert m is not None
        assert m.group(1) == "12"

    def test_counter_width(self, cv32_workspace, cv32_target):
        if not _has_hwlp_anchor(cv32_target):
            pytest.skip(PC_WIDTH_PRECONDITION_MSG)
        patch = WidthNarrowingPatch(decision=WidthDecision(counter_width=10))
        patch.apply(cv32_workspace)
        # CNT_WIDTH lives in cv32e40p_top.sv and elsewhere
        top = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        m = re.search(r"parameter CNT_WIDTH\s*=\s*(\d+)", top)
        assert m is not None
        assert m.group(1) == "10"

    def test_idempotent(self, cv32_workspace, cv32_target):
        if not _has_hwlp_anchor(cv32_target):
            pytest.skip(PC_WIDTH_PRECONDITION_MSG)
        patch = WidthNarrowingPatch(decision=WidthDecision(pc_width=14, hwlp_addr_width=12))
        patch.apply(cv32_workspace)
        before = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        patch.apply(cv32_workspace)
        after = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        assert before == after

    def test_default_decision_is_noop(self, cv32_workspace):
        before = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        WidthNarrowingPatch(decision=WidthDecision()).apply(cv32_workspace)
        after = (cv32_workspace.output_root / "rtl/cv32e40p_top.sv").read_text()
        assert before == after

    def test_label_describes_widths(self):
        d = WidthDecision(pc_width=14, hwlp_addr_width=12, counter_width=8)
        p = WidthNarrowingPatch(decision=d)
        assert "pc=14" in p.label
        assert "hwlp_addr=12" in p.label
        assert "cnt=8" in p.label

    def test_label_for_noop(self):
        d = WidthDecision()
        p = WidthNarrowingPatch(decision=d)
        assert p.label == "WidthNarrowingPatch(noop)"


# ─── PrunePatch ────────────────────────────────────────────────────


class TestPrunePatch:
    def test_empty_decision_is_safe(self, cv32_workspace):
        # Empty PruneDecision -> falls back to reconstructed minimal
        # config -> should not crash, may or may not change RTL
        # (RTLPruner with no removable_alu_ops typically does nothing).
        patch = PrunePatch(decision=PruneDecision())
        patch.apply(cv32_workspace)  # should not raise

    def test_label_includes_counts(self):
        d = PruneDecision(
            removable_alu_ops=frozenset({"ALU_DIV", "ALU_REM"}),
            removable_mul_modes=frozenset({"MUL_DOT8"}),
            removable_opcode_groups=frozenset({"PULP", "FP"}),
        )
        p = PrunePatch(decision=d)
        assert "alu=2" in p.label
        assert "mul_modes=1" in p.label
        assert "opc_groups=2" in p.label


# ─── FusionPatch ───────────────────────────────────────────────────


class TestFusionPatch:
    def test_empty_decision_is_noop(self, cv32_workspace):
        before = (cv32_workspace.output_root / "rtl/cv32e40p_decoder.sv").read_text()
        FusionPatch(decision=FusionDecision()).apply(cv32_workspace)
        after = (cv32_workspace.output_root / "rtl/cv32e40p_decoder.sv").read_text()
        assert before == after

    def test_label_with_count(self):
        # FusedOperation objects are opaque; we just verify the
        # label reports the count.
        class Fake:
            pass
        d = FusionDecision(fused_ops=(Fake(), Fake(), Fake()))
        p = FusionPatch(decision=d)
        assert "3 ops" in p.label


# ─── LoopPatch ─────────────────────────────────────────────────────


class TestLoopPatch:
    def test_nest_zero_label(self):
        p = LoopPatch(decision=LoopDecision(nest_depth=0))
        assert p.label == "LoopPatch(noop)"

    def test_nest_nonzero_label(self):
        p = LoopPatch(decision=LoopDecision(nest_depth=2, counter_width=12, addr_width=14))
        assert "nest=2" in p.label
        assert "cnt=12" in p.label
        assert "addr=14" in p.label

    def test_nest_zero_strips_pragmas(self, cv32_workspace, cv32_target):
        # Before applying, ARVIS_HWLP_BEGIN markers exist in the
        # decoder template.  After applying with nest=0 the markers
        # should be gone (legacy behaviour: pragmas are processed
        # unconditionally).
        decoder = cv32_workspace.output_root / "rtl/cv32e40p_decoder.sv"
        before = decoder.read_text()
        if "ARVIS_HWLP_BEGIN" not in before:
            pytest.skip(
                "RTL decoder lacks ARVIS_HWLP_BEGIN markers; tree predates "
                "the Phase 4 hwloop pragma work."
            )
        LoopPatch(decision=LoopDecision(nest_depth=0)).apply(cv32_workspace)
        after = decoder.read_text()
        assert "ARVIS_HWLP_BEGIN" not in after
