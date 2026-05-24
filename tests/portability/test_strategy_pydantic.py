"""Pydantic-specific contract tests for the Decision hierarchy.

The base test file ``test_strategy.py`` covers the strategy
protocol; this file focuses on the Phase 4.3 upgrade that turned
Decisions from frozen ``@dataclass`` to Pydantic v2 ``BaseModel``.

Properties verified
-------------------
* immutability (``model_config = ConfigDict(frozen=True)``)
* validation (``@field_validator`` rejects malformed input)
* round-trip JSON serialisation
* extra field rejection (``extra="forbid"``)
* the schema is exportable for downstream tooling
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from arvis.core.strategy import (
    FusionDecision,
    LoopDecision,
    PruneDecision,
    WidthDecision,
)

# ─── Validation: PruneDecision ────────────────────────────────────


def test_prune_decision_rejects_negative_used_regs_mask():
    """``used_regs_mask`` is a non-negative bitfield."""
    with pytest.raises(ValidationError, match="non-negative"):
        PruneDecision(used_regs_mask=-1)


def test_prune_decision_rejects_out_of_range_register():
    """RV32 has 32 architectural integer registers (x0..x31)."""
    with pytest.raises(ValidationError, match="\\[0, 32\\)"):
        PruneDecision(unused_registers=(32,))
    with pytest.raises(ValidationError, match="\\[0, 32\\)"):
        PruneDecision(unused_registers=(-1,))


def test_prune_decision_accepts_valid_construction():
    """Happy-path: documented field types are accepted as-is."""
    d = PruneDecision(
        removable_alu_ops=frozenset({"ALU_DIV"}),
        feature_flags={"enable_fpu": True},
        unused_registers=(0, 5, 10, 31),
        used_regs_mask=0xDEADBEEF,
        target_overlay={"corev_pulp": 0, "fpu": 1},
    )
    assert "ALU_DIV" in d.removable_alu_ops
    assert d.feature_flags["enable_fpu"] is True
    assert 31 in d.unused_registers
    assert d.used_regs_mask == 0xDEADBEEF
    assert d.target_overlay["corev_pulp"] == 0


# ─── Validation: LoopDecision ────────────────────────────────────


def test_loop_decision_rejects_negative_nest_depth():
    with pytest.raises(ValidationError, match="non-negative"):
        LoopDecision(nest_depth=-1)


def test_loop_decision_rejects_zero_widths():
    with pytest.raises(ValidationError, match="\\[1, 32\\]"):
        LoopDecision(counter_width=0)
    with pytest.raises(ValidationError, match="\\[1, 32\\]"):
        LoopDecision(addr_width=0)


def test_loop_decision_rejects_out_of_range_widths():
    with pytest.raises(ValidationError, match="\\[1, 32\\]"):
        LoopDecision(counter_width=33)


# ─── Validation: WidthDecision ────────────────────────────────────


def test_width_decision_pc_width_zero_is_disabled_sentinel():
    """``pc_width=0`` is the documented "disabled" sentinel."""
    d = WidthDecision(pc_width=0)
    assert d.pc_width == 0


def test_width_decision_rejects_invalid_pc_width():
    """Any non-zero ``pc_width`` outside [1, 32] is rejected."""
    with pytest.raises(ValidationError, match="0 \\(disabled\\) or in \\[1, 32\\]"):
        WidthDecision(pc_width=33)


def test_width_decision_rejects_negative_fifo_depth():
    with pytest.raises(ValidationError, match="non-negative"):
        WidthDecision(fifo_depth=-1)


# ─── Immutability ─────────────────────────────────────────────────


def test_decisions_are_frozen():
    """``model_config = ConfigDict(frozen=True)`` is enforced."""
    d = PruneDecision()
    with pytest.raises(ValidationError, match="frozen"):
        d.used_regs_mask = 0


def test_loop_decision_is_frozen():
    d = LoopDecision()
    with pytest.raises(ValidationError, match="frozen"):
        d.nest_depth = 5


def test_width_decision_is_frozen():
    d = WidthDecision()
    with pytest.raises(ValidationError, match="frozen"):
        d.pc_width = 14


# ─── Extra fields ─────────────────────────────────────────────────


def test_unknown_field_is_rejected():
    """``extra="forbid"`` catches typos."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        PruneDecision(typo_field="oops")


# ─── Serialisation ────────────────────────────────────────────────


def test_width_decision_json_round_trip():
    """``model_dump_json`` + ``model_validate_json`` round-trip."""
    d = WidthDecision(pc_width=14, hwlp_addr_width=14, counter_width=12, fifo_depth=8)
    serialised = d.model_dump_json()
    restored = WidthDecision.model_validate_json(serialised)
    assert d == restored


def test_loop_decision_json_round_trip():
    d = LoopDecision(nest_depth=2, counter_width=12, addr_width=14)
    serialised = d.model_dump_json()
    restored = LoopDecision.model_validate_json(serialised)
    assert d == restored


def test_prune_decision_dict_round_trip():
    """``model_dump`` + ``**`` re-construction works for nested types."""
    d = PruneDecision(
        removable_alu_ops=frozenset({"ALU_DIV"}),
        feature_flags={"enable_fpu": True},
        target_overlay={"corev_pulp": 0},
    )
    payload = d.model_dump()
    restored = PruneDecision(**payload)
    assert d == restored


# ─── Schema export ───────────────────────────────────────────────


def test_prune_decision_schema_is_exportable():
    """``model_json_schema()`` produces a usable JSON schema."""
    schema = PruneDecision.model_json_schema()
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    # Spot-check a documented field.
    assert "used_regs_mask" in schema["properties"]
    # Schema is JSON-encodable in itself.
    json.dumps(schema)


# ─── Backwards-compat: positional / kwarg construction ────────────


def test_fusion_decision_default_is_empty():
    """Empty FusionDecision still parses (default tuple)."""
    d = FusionDecision()
    assert d.fused_ops == ()


def test_fusion_decision_accepts_arbitrary_records():
    """``arbitrary_types_allowed=True`` lets us carry opaque records."""

    class FakeOp:
        def __init__(self, name: str) -> None:
            self.name = name

    d = FusionDecision(fused_ops=(FakeOp("a"), FakeOp("b")))
    assert len(d.fused_ops) == 2
    assert d.fused_ops[0].name == "a"
