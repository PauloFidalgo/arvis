"""Tests for ``core.strategy`` decisions and strategy contracts."""

from __future__ import annotations

import pytest

from arvis.core.strategy import (
    Decision,
    FusionDecision,
    LoopDecision,
    OptimizationStrategy,
    PruneDecision,
    PruningStrategy,
    FusionStrategy,
    LoopStrategy,
    WidthDecision,
    WidthStrategy,
)


class TestDecisionsAreImmutable:
    def test_prune_decision_frozen(self):
        d = PruneDecision()
        with pytest.raises(Exception):  # FrozenInstanceError
            d.removable_alu_ops = frozenset({"ALU_DIV"})  # type: ignore[misc]

    def test_fusion_decision_frozen(self):
        d = FusionDecision()
        with pytest.raises(Exception):
            d.fused_ops = ()  # type: ignore[misc]

    def test_loop_decision_frozen(self):
        d = LoopDecision()
        with pytest.raises(Exception):
            d.nest_depth = 4  # type: ignore[misc]

    def test_width_decision_frozen(self):
        d = WidthDecision()
        with pytest.raises(Exception):
            d.pc_width = 14  # type: ignore[misc]


class TestDecisionsHaveSensibleDefaults:
    def test_prune_decision_default_is_no_pruning(self):
        d = PruneDecision()
        assert len(d.removable_alu_ops) == 0
        assert len(d.removable_mul_modes) == 0
        assert len(d.removable_opcode_groups) == 0
        assert len(d.feature_flags) == 0

    def test_fusion_decision_default_is_no_fusion(self):
        d = FusionDecision()
        assert len(d.fused_ops) == 0

    def test_loop_decision_default_is_disabled(self):
        d = LoopDecision()
        assert d.nest_depth == 0  # 0 = disabled
        assert d.counter_width == 32  # 32 = no narrowing
        assert d.addr_width == 32

    def test_width_decision_default_is_no_narrowing(self):
        d = WidthDecision()
        assert d.pc_width == 0  # 0 = disabled (no narrowing)
        assert d.hwlp_addr_width == 32  # 32 = no narrowing
        assert d.counter_width == 32


class TestDecisionRender:
    """Decision.render() returns RTLPatch lists, not raw mutations."""

    def test_render_returns_list(self):
        from arvis.targets import CV32E40P
        target = CV32E40P()
        for cls in (PruneDecision, FusionDecision, LoopDecision, WidthDecision):
            d = cls()
            patches = target.render_decision(d, workspace=None)
            assert isinstance(patches, list)


class TestStrategySubtypes:
    """Concrete strategies must subclass the right abstract."""

    def test_usage_driven_pruner_is_pruning(self):
        from arvis.strategies.pruning import UsageDrivenPruner
        s = UsageDrivenPruner()
        assert isinstance(s, PruningStrategy)
        assert isinstance(s, OptimizationStrategy)

    def test_ngram_fusion_is_fusion(self):
        from arvis.strategies.fusion import NGramFusion
        s = NGramFusion()
        assert isinstance(s, FusionStrategy)

    def test_cv32e40p_hwloop_is_loop(self):
        from arvis.strategies.hwloop import CV32E40PHWLoop
        s = CV32E40PHWLoop()
        assert isinstance(s, LoopStrategy)

    def test_pc_width_narrowing_is_width(self):
        from arvis.strategies.width import PCWidthNarrowing
        s = PCWidthNarrowing()
        assert isinstance(s, WidthStrategy)


class TestStrategyApplicability:
    """applicable() returns a bool and respects target capability."""

    def test_pc_width_applicable_to_cv32e40p(self):
        from arvis.strategies.width import PCWidthNarrowing
        from arvis.targets import CV32E40P
        target = CV32E40P()
        assert PCWidthNarrowing().applicable(workload=None, target=target) is True

    def test_pc_width_skipped_without_pc_width_param(self):
        """A target that doesn't expose PC_WIDTH is opted out."""
        from arvis.strategies.width import PCWidthNarrowing

        class FakeTarget:
            def parameter(self, name):
                return None  # no parameters

        assert PCWidthNarrowing().applicable(workload=None, target=FakeTarget()) is False

    def test_hwloop_applicability_gated_on_HW_LOOP_param(self):
        from arvis.strategies.hwloop import CV32E40PHWLoop

        class WithHwLoop:
            def parameter(self, name):
                return object() if name == "HW_LOOP" else None

        class WithoutHwLoop:
            def parameter(self, name):
                return None

        assert CV32E40PHWLoop().applicable(workload=None, target=WithHwLoop()) is True
        assert CV32E40PHWLoop().applicable(workload=None, target=WithoutHwLoop()) is False
