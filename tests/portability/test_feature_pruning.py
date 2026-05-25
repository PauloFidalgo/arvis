"""Tests for Phase 8 declarative pruning architecture.

Coverage:

* :mod:`core.feature` — CoreFeature, RemovalAction, CustomAction,
  resolve_removable (cascade, conflicts, overrides).
* :mod:`core.rtl_primitives` — each primitive on isolated
  SystemVerilog snippets.  Idempotency + missing-file safety.
* :class:`FeatureBasedPruner` — walks target.features, applies
  predicates, produces PruneDecision with unused_features.
* :class:`FeatureRemovalPatch` — dispatches RemovalAction +
  CustomAction; handles unknown features and unknown action kinds.
* End-to-end branch-predictor removal scenario from the
  user-facing example.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from arvis.core.feature import (
    ActionKind,
    CoreFeature,
    CustomAction,
    RemovalAction,
    resolve_removable,
)
from arvis.core.feature_patch import FeatureRemovalPatch
from arvis.core.rtl_primitives import (
    dispatch,
    remove_module_instance,
    remove_opcode_case,
    remove_pragma_block,
    remove_signal_uses,
    replace_pattern,
    set_parameter,
    shrink_parameter,
)
from arvis.core.strategy import PruneDecision
from arvis.core.workload import WorkloadProfile
from arvis.strategies.pruning.feature_based import FeatureBasedPruner

# ─── Test fixtures ────────────────────────────────────────────────


class _MockWorkspace:
    """Minimal RTLWorkspace stand-in for primitive tests."""

    def __init__(self, root: Path) -> None:
        self.output_root = root
        self.source_root = root


@pytest.fixture
def workspace(tmp_path: Path) -> _MockWorkspace:
    """Workspace with rtl/ subdirectory ready for fixture files."""
    (tmp_path / "rtl").mkdir()
    return _MockWorkspace(tmp_path)


# ─── 1. CoreFeature / resolve_removable ──────────────────────────


class TestCoreFeature:
    """CoreFeature.is_unused respects predicate + override."""

    def test_unused_when_predicate_false_and_no_override(self) -> None:
        f = CoreFeature(
            name="x",
            description="x",
            usage_predicate=lambda p: False,
        )
        assert f.is_unused(WorkloadProfile()) is True

    def test_kept_when_predicate_true(self) -> None:
        f = CoreFeature(
            name="x",
            description="x",
            usage_predicate=lambda p: True,
        )
        assert f.is_unused(WorkloadProfile()) is False

    def test_kept_by_keep_when_override(self) -> None:
        f = CoreFeature(
            name="x",
            description="x",
            usage_predicate=lambda p: False,
            keep_when=lambda p, opts: opts.get("keep_x", False),
        )
        assert f.is_unused(WorkloadProfile()) is True
        assert f.is_unused(WorkloadProfile(), {"keep_x": True}) is False


class TestResolveRemovable:
    """resolve_removable handles cascades and conflicts."""

    def test_simple_unused_set(self) -> None:
        features = (
            CoreFeature("a", "a", usage_predicate=lambda p: False),
            CoreFeature("b", "b", usage_predicate=lambda p: True),
        )
        to_remove, conflicts = resolve_removable(features, WorkloadProfile())
        assert to_remove == {"a"}
        assert conflicts == []

    def test_cascade_via_requires(self) -> None:
        """If A is removed and A.requires=(B,), B is also scheduled."""
        features = (
            CoreFeature("a", "a", usage_predicate=lambda p: False, requires=("b",)),
            CoreFeature("b", "b", usage_predicate=lambda p: True),  # would stay
            CoreFeature("c", "c", usage_predicate=lambda p: True),  # stays
        )
        to_remove, _ = resolve_removable(features, WorkloadProfile())
        assert to_remove == {"a", "b"}

    def test_cascade_chain(self) -> None:
        """A→B→C cascade reaches all."""
        features = (
            CoreFeature("a", "", usage_predicate=lambda p: False, requires=("b",)),
            CoreFeature("b", "", usage_predicate=lambda p: True, requires=("c",)),
            CoreFeature("c", "", usage_predicate=lambda p: True),
        )
        to_remove, _ = resolve_removable(features, WorkloadProfile())
        assert to_remove == {"a", "b", "c"}

    def test_conflict_detected(self) -> None:
        """incompatible_with reports a conflict but doesn't raise."""
        features = (
            CoreFeature("a", "", usage_predicate=lambda p: False, incompatible_with=("b",)),
            CoreFeature("b", "", usage_predicate=lambda p: False),
        )
        to_remove, conflicts = resolve_removable(features, WorkloadProfile())
        assert to_remove == {"a", "b"}
        assert len(conflicts) == 1
        assert "a" in conflicts[0] and "b" in conflicts[0]


# ─── 2. RTL primitives ────────────────────────────────────────────


class TestSetParameter:
    def test_changes_value(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "module m #(parameter X = 1) ();\nendmodule\n"
        )
        ok = set_parameter(workspace, "rtl/m.sv", "X", 0)
        assert ok is True
        assert "parameter X = 0" in (workspace.output_root / "rtl/m.sv").read_text()

    def test_idempotent(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "module m #(parameter X = 0) ();\nendmodule\n"
        )
        # Already 0 → no-op (returns False for "no change made")
        assert set_parameter(workspace, "rtl/m.sv", "X", 0) is False

    def test_missing_file(self, workspace: _MockWorkspace) -> None:
        assert set_parameter(workspace, "rtl/nope.sv", "X", 0) is False

    def test_missing_parameter(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text("module m ();\nendmodule\n")
        assert set_parameter(workspace, "rtl/m.sv", "X", 0) is False

    def test_localparam(self, workspace: _MockWorkspace) -> None:
        """SET_PARAM also matches localparam."""
        (workspace.output_root / "rtl/m.sv").write_text(
            "module m;\n  localparam Y = 4;\nendmodule\n"
        )
        assert set_parameter(workspace, "rtl/m.sv", "Y", 8) is True
        assert "localparam Y = 8" in (workspace.output_root / "rtl/m.sv").read_text()


class TestShrinkParameter:
    def test_valid_width(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "module m #(parameter PC_WIDTH = 32) ();\nendmodule\n"
        )
        assert shrink_parameter(workspace, "rtl/m.sv", "PC_WIDTH", 16) is True

    def test_rejects_invalid_width(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "module m #(parameter PC_WIDTH = 32) ();\nendmodule\n"
        )
        assert shrink_parameter(workspace, "rtl/m.sv", "PC_WIDTH", 0) is False
        assert shrink_parameter(workspace, "rtl/m.sv", "PC_WIDTH", -1) is False
        assert shrink_parameter(workspace, "rtl/m.sv", "PC_WIDTH", "foo") is False  # type: ignore[arg-type]


class TestRemovePragmaBlock:
    def test_removes_block(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "x = 1;\n// pragma FOO_BEGIN\ny = 2;\nz = 3;\n// pragma FOO_END\nw = 4;\n"
        )
        assert remove_pragma_block(workspace, "rtl/m.sv", "FOO") is True
        text = (workspace.output_root / "rtl/m.sv").read_text()
        assert "y = 2" not in text and "z = 3" not in text
        assert "w = 4" in text and "x = 1" in text

    def test_multiple_blocks(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "// pragma A_BEGIN\nx;\n// pragma A_END\ny;\n// pragma A_BEGIN\nz;\n// pragma A_END\n"
        )
        assert remove_pragma_block(workspace, "rtl/m.sv", "A") is True
        assert "x;" not in (workspace.output_root / "rtl/m.sv").read_text()
        assert "z;" not in (workspace.output_root / "rtl/m.sv").read_text()

    def test_no_match(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text("nothing\n")
        assert remove_pragma_block(workspace, "rtl/m.sv", "FOO") is False


class TestRemoveOpcodeCase:
    def test_block_form(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/d.sv").write_text(
            "case (op)\n"
            "  OPCODE_AMO: begin\n    en = 1;\n  end\n"
            "  OPCODE_OP: begin\n    en2 = 1;\n  end\n"
            "endcase\n"
        )
        assert remove_opcode_case(workspace, "rtl/d.sv", "OPCODE_AMO") is True
        text = (workspace.output_root / "rtl/d.sv").read_text()
        assert "OPCODE_AMO" not in text
        assert "OPCODE_OP" in text

    def test_single_line_form(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/d.sv").write_text(
            "case (op)\n  OPCODE_AMO: en = 1;\n  OPCODE_OP: en2 = 1;\nendcase\n"
        )
        assert remove_opcode_case(workspace, "rtl/d.sv", "OPCODE_AMO") is True
        assert "OPCODE_AMO" not in (workspace.output_root / "rtl/d.sv").read_text()


class TestRemoveSignal:
    def test_removes_declaration(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text("logic clk;\nlogic div_en;\nlogic rst;\n")
        assert remove_signal_uses(workspace, "rtl/m.sv", "div_*") is True
        text = (workspace.output_root / "rtl/m.sv").read_text()
        assert "div_en" not in text
        assert "clk" in text and "rst" in text


class TestRemoveModuleInstance:
    def test_simple_instance(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "module top;\n  alu u_alu (.clk(clk));\n  bp u_bp (.clk(clk), .pc(pc));\nendmodule\n"
        )
        assert remove_module_instance(workspace, "rtl/m.sv", "u_bp") is True
        text = (workspace.output_root / "rtl/m.sv").read_text()
        assert "u_bp" not in text
        assert "u_alu" in text

    def test_with_param_override(self, workspace: _MockWorkspace) -> None:
        """Handles nested parens in #(.X(Y))."""
        (workspace.output_root / "rtl/m.sv").write_text(
            "module top;\n"
            "  bp #(.WIDTH(32), .DEPTH(16)) u_bp (\n"
            "      .clk(clk),\n"
            "      .pc(pc)\n"
            "  );\n"
            "  alu u_alu (.clk(clk));\n"
            "endmodule\n"
        )
        assert remove_module_instance(workspace, "rtl/m.sv", "u_bp") is True
        text = (workspace.output_root / "rtl/m.sv").read_text()
        assert "u_bp" not in text and "WIDTH(32)" not in text
        assert "u_alu" in text

    def test_missing_instance(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text("module top;\nendmodule\n")
        assert remove_module_instance(workspace, "rtl/m.sv", "u_x") is False


class TestReplacePattern:
    def test_simple_replace(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text("a = b ? c : d;\n")
        assert replace_pattern(workspace, "rtl/m.sv", r"b \? c : d", "c") is True
        assert (workspace.output_root / "rtl/m.sv").read_text() == "a = c;\n"


class TestDispatch:
    def test_dispatch_set_param(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/m.sv").write_text(
            "module m #(parameter X = 1) ();\nendmodule\n"
        )
        ok = dispatch(workspace, ActionKind.SET_PARAM, "rtl/m.sv", "X", 0)
        assert ok is True

    def test_dispatch_unknown_kind(self, workspace: _MockWorkspace) -> None:
        class _NotAnActionKind:
            pass

        # Returns False instead of raising
        assert dispatch(workspace, _NotAnActionKind(), "rtl/m.sv", "X", 0) is False


# ─── 3. FeatureBasedPruner ────────────────────────────────────────


class TestFeatureBasedPruner:
    """End-to-end: walk features, produce PruneDecision."""

    def test_unused_features_in_decision(self) -> None:
        class _Tgt:
            name = "test"
            features = (
                CoreFeature("div", "", usage_predicate=lambda p: False),
                CoreFeature("mul", "", usage_predicate=lambda p: True),
            )

        pruner = FeatureBasedPruner()
        decision = pruner.analyze(None, WorkloadProfile(), _Tgt())  # type: ignore[arg-type]
        assert decision.unused_features == frozenset({"div"})

    def test_overrides_propagate(self) -> None:
        class _Tgt:
            name = "test"
            features = (
                CoreFeature(
                    "x",
                    "",
                    usage_predicate=lambda p: False,
                    keep_when=lambda p, opts: opts.get("keep_x", False),
                ),
            )

        # Without override: removed
        d1 = FeatureBasedPruner().analyze(None, WorkloadProfile(), _Tgt())  # type: ignore[arg-type]
        assert "x" in d1.unused_features

        # With override: kept
        d2 = FeatureBasedPruner(overrides={"keep_x": True}).analyze(
            None,
            WorkloadProfile(),
            _Tgt(),  # type: ignore[arg-type]
        )
        assert "x" not in d2.unused_features

    def test_applicable_only_when_features_present(self) -> None:
        class _Empty:
            features = ()

        class _Filled:
            features = (CoreFeature("x", "", usage_predicate=lambda p: False),)

        pruner = FeatureBasedPruner()
        assert pruner.applicable(None, _Empty()) is False  # type: ignore[arg-type]
        assert pruner.applicable(None, _Filled()) is True  # type: ignore[arg-type]


# ─── 4. FeatureRemovalPatch ───────────────────────────────────────


class TestFeatureRemovalPatch:
    def test_applies_removal_actions(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/top.sv").write_text(
            "module top #(parameter ENABLE_DIV = 1) ();\nendmodule\n"
        )

        class _Tgt:
            name = "test"
            features = (
                CoreFeature(
                    "div_unit",
                    "ALU divider",
                    usage_predicate=lambda p: False,
                    removal_actions=(
                        RemovalAction(ActionKind.SET_PARAM, "rtl/top.sv", "ENABLE_DIV", 0),
                    ),
                ),
            )

        decision = PruneDecision(unused_features=frozenset({"div_unit"}))
        patch = FeatureRemovalPatch(decision=decision, target=_Tgt())  # type: ignore[arg-type]
        patch.apply(workspace)  # type: ignore[arg-type]

        assert "ENABLE_DIV = 0" in (workspace.output_root / "rtl/top.sv").read_text()

    def test_applies_custom_actions(self, workspace: _MockWorkspace) -> None:
        (workspace.output_root / "rtl/top.sv").write_text("foo = pred_pc;\n")

        called: list[tuple[Any, Any]] = []

        def _custom(ws: Any, dec: Any) -> None:
            called.append((ws, dec))
            (ws.output_root / "rtl/top.sv").write_text("foo = pc_plus_4;\n")

        class _Tgt:
            name = "test"
            features = (
                CoreFeature(
                    "bp",
                    "branch predictor",
                    usage_predicate=lambda p: False,
                    removal_actions=(CustomAction("retarget", _custom),),
                ),
            )

        decision = PruneDecision(unused_features=frozenset({"bp"}))
        patch = FeatureRemovalPatch(decision=decision, target=_Tgt())  # type: ignore[arg-type]
        patch.apply(workspace)  # type: ignore[arg-type]

        assert len(called) == 1
        assert "pc_plus_4" in (workspace.output_root / "rtl/top.sv").read_text()

    def test_unknown_feature_does_not_raise(self, workspace: _MockWorkspace) -> None:
        """A decision can carry feature names the target doesn't know;
        the patch logs a warning and continues."""

        class _Tgt:
            name = "test"
            features = ()

        decision = PruneDecision(unused_features=frozenset({"unknown_feature"}))
        patch = FeatureRemovalPatch(decision=decision, target=_Tgt())  # type: ignore[arg-type]
        patch.apply(workspace)  # type: ignore[arg-type] # no exception

    def test_empty_decision_is_noop(self, workspace: _MockWorkspace) -> None:
        class _Tgt:
            name = "test"
            features = (CoreFeature("x", "", usage_predicate=lambda p: True),)

        decision = PruneDecision()  # empty unused_features
        patch = FeatureRemovalPatch(decision=decision, target=_Tgt())  # type: ignore[arg-type]
        patch.apply(workspace)  # type: ignore[arg-type] # no-op

    def test_label_includes_count(self) -> None:
        class _Tgt:
            features = ()

        decision = PruneDecision(unused_features=frozenset({"a", "b", "c"}))
        patch = FeatureRemovalPatch(decision=decision, target=_Tgt())  # type: ignore[arg-type]
        assert "3 features" in patch.label


# ─── 5. End-to-end: branch predictor scenario ─────────────────────


class TestBranchPredictorScenario:
    """The user-facing example: declare BP feature + 3 actions, apply.

    Validates the entire declarative pipeline:
    feature surface -> pruner -> decision -> patch -> RTL.
    """

    def test_full_scenario(self, workspace: _MockWorkspace, caplog) -> None:
        # Create realistic-ish RTL with a branch predictor
        (workspace.output_root / "rtl/top.sv").write_text(
            "module top #(\n"
            "    parameter ENABLE_BRANCH_PRED = 1\n"
            ") ();\n"
            "  branch_predictor #(.WIDTH(32)) u_bp (\n"
            "      .clk(clk),\n"
            "      .pc(pc),\n"
            "      .pred_pc_o(pred_pc),\n"
            "      .pred_taken_o(pred_taken)\n"
            "  );\n"
            "  assign next_pc = pred_taken ? pred_pc : pc_plus_4;\n"
            "endmodule\n"
        )

        # Custom action: retarget the PC select mux
        def _retarget(ws: Any, _decision: Any) -> None:
            top = ws.output_root / "rtl/top.sv"
            text = top.read_text().replace(
                "assign next_pc = pred_taken ? pred_pc : pc_plus_4;",
                "assign next_pc = pc_plus_4;  // BP removed",
            )
            top.write_text(text)

        # Define feature exactly as the user-facing example shows
        bp_feature = CoreFeature(
            name="branch_predictor",
            description="Dynamic branch predictor",
            usage_predicate=lambda p: p.density(("beq", "bne", "blt", "bge")) > 0.05,
            keep_when=lambda p, opts: opts.get("keep_branch_predictor", False),
            removal_actions=(
                RemovalAction(ActionKind.SET_PARAM, "rtl/top.sv", "ENABLE_BRANCH_PRED", 0),
                RemovalAction(ActionKind.REMOVE_INSTANCE, "rtl/top.sv", "u_bp"),
                CustomAction("retarget_pc", _retarget),
            ),
        )

        class _Tgt:
            name = "rvmycore"
            features = (bp_feature,)

        # Workload with no branches → BP should be removed
        profile = WorkloadProfile(instr_histogram={"add": 100, "lw": 25})

        with caplog.at_level(logging.INFO):
            decision = FeatureBasedPruner().analyze(
                None,
                profile,
                _Tgt(),  # type: ignore[arg-type]
            )
            assert decision.unused_features == frozenset({"branch_predictor"})

            patch = FeatureRemovalPatch(decision=decision, target=_Tgt())  # type: ignore[arg-type]
            patch.apply(workspace)  # type: ignore[arg-type]

        # Verify final RTL state
        text = (workspace.output_root / "rtl/top.sv").read_text()
        assert "ENABLE_BRANCH_PRED = 0" in text
        assert "u_bp" not in text
        assert "BP removed" in text
        assert "branch_predictor #(" not in text or text.find("branch_predictor #(") == -1


# ─── 6. CV32E40P feature surface ──────────────────────────────────


class TestCV32E40PFeatures:
    """The cv32e40p feature surface is well-formed."""

    def test_loads_without_error(self) -> None:
        from arvis.targets.cv32e40p.features import CV32E40P_FEATURES

        assert len(CV32E40P_FEATURES) >= 10
        # Every feature has a non-empty name + description
        for f in CV32E40P_FEATURES:
            assert f.name
            assert f.description

    def test_target_exposes_features(self) -> None:
        from arvis.targets import CV32E40P

        target = CV32E40P()
        assert len(target.features) >= 10
        names = {f.name for f in target.features}
        # Spot-check several known features
        assert "div_unit" in names
        assert "fpu" in names
        assert "compressed" in names
        assert "interrupts" in names

    def test_div_predicate_detects_div(self) -> None:
        from arvis.targets.cv32e40p.features import CV32E40P_FEATURES

        div = next(f for f in CV32E40P_FEATURES if f.name == "div_unit")
        # Workload using DIV → keep
        with_div = WorkloadProfile(instr_histogram={"div": 5})
        assert div.is_unused(with_div) is False
        # Workload not using DIV → remove
        no_div = WorkloadProfile(instr_histogram={"add": 100})
        assert div.is_unused(no_div) is True

    def test_pulp_predicate_detects_pulp_ops(self) -> None:
        from arvis.targets.cv32e40p.features import CV32E40P_FEATURES

        pulp = next(f for f in CV32E40P_FEATURES if f.name == "pulp_extensions")
        # PULP custom op
        with_pulp = WorkloadProfile(instr_histogram={"p.lw": 10})
        assert pulp.is_unused(with_pulp) is False
        # No PULP ops
        no_pulp = WorkloadProfile(instr_histogram={"add": 100})
        assert pulp.is_unused(no_pulp) is True

    def test_multiplier_cascades_to_high(self) -> None:
        from arvis.targets.cv32e40p.features import CV32E40P_FEATURES

        # A workload with no MUL or MULH
        profile = WorkloadProfile(instr_histogram={"add": 100})
        to_remove, _ = resolve_removable(CV32E40P_FEATURES, profile)
        # multiplier and multiplier_high both removable; cascade
        # ensures multiplier_high is included even if predicate said keep
        assert "multiplier" in to_remove
        assert "multiplier_high" in to_remove

    def test_interrupts_kept_by_default(self) -> None:
        from arvis.targets.cv32e40p.features import CV32E40P_FEATURES

        profile = WorkloadProfile(instr_histogram={"add": 100})
        to_remove, _ = resolve_removable(CV32E40P_FEATURES, profile)
        assert "interrupts" not in to_remove  # safety default

    def test_interrupts_removable_via_override(self) -> None:
        from arvis.targets.cv32e40p.features import CV32E40P_FEATURES

        profile = WorkloadProfile(instr_histogram={"add": 100})
        to_remove, _ = resolve_removable(CV32E40P_FEATURES, profile, {"prune_interrupts": True})
        assert "interrupts" in to_remove
