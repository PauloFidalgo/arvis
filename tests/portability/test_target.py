"""Tests for ``core.target.TargetCore`` and its concrete subclasses."""

from __future__ import annotations

import pytest

from arvis.core.target import CoreParameter, OpcodeSpace, OpcodeSlot
from arvis.targets import CV32E40P


class TestCoreParameter:
    def test_clamp_within_range(self):
        p = CoreParameter("X", default=10, minimum=4, maximum=16)
        assert p.clamp(8) == 8
        assert p.clamp(4) == 4
        assert p.clamp(16) == 16

    def test_clamp_below_minimum(self):
        p = CoreParameter("X", default=10, minimum=4, maximum=16)
        assert p.clamp(2) == 4
        assert p.clamp(0) == 4

    def test_clamp_above_maximum(self):
        p = CoreParameter("X", default=10, minimum=4, maximum=16)
        assert p.clamp(20) == 16
        assert p.clamp(99) == 16

    def test_clamp_no_bounds_passes_through(self):
        p = CoreParameter("X", default=10)
        assert p.clamp(0) == 0
        assert p.clamp(-1) == -1
        assert p.clamp(1_000_000) == 1_000_000


class TestOpcodeSpace:
    def test_allocate_consumes_in_order(self):
        slots = [OpcodeSlot(opcode=0x0B, funct3=0, label="a"),
                 OpcodeSlot(opcode=0x0B, funct3=1, label="b")]
        space = OpcodeSpace(name="test", available=list(slots))
        assert space.allocate().label == "a"
        assert space.allocate().label == "b"
        assert space.free_slots() == 0

    def test_allocate_when_exhausted_raises(self):
        space = OpcodeSpace(name="empty", available=[])
        with pytest.raises(RuntimeError, match="exhausted"):
            space.allocate()

    def test_consumed_tracked(self):
        slots = [OpcodeSlot(opcode=0x0B, funct3=0)]
        space = OpcodeSpace(name="test", available=list(slots))
        slot = space.allocate()
        assert slot in space.consumed
        assert slot not in space.available


class TestCV32E40P:
    def test_identity(self):
        target = CV32E40P()
        assert target.name == "cv32e40p"
        assert target.isa.name == "rv32imc_zicsr"

    def test_required_parameters_present(self):
        target = CV32E40P()
        names = {p.name for p in target.parameters()}
        # Every parameter ARVIS strategies look up by name MUST be
        # exposed by the target.  This test fails loudly if a
        # parameter rename breaks strategy applicability checks.
        assert "HW_LOOP" in names
        assert "CNT_WIDTH" in names
        assert "HWLP_ADDR_WIDTH" in names
        assert "PC_WIDTH" in names
        assert "FIFO_DEPTH" in names

    def test_parameter_ranges_defaults(self):
        target = CV32E40P()
        pc = target.parameter("PC_WIDTH")
        assert pc is not None
        assert pc.default == 32
        assert pc.minimum == 8
        assert pc.maximum == 32

    def test_parameter_lookup_unknown_returns_none(self):
        target = CV32E40P()
        assert target.parameter("NONEXISTENT_PARAM") is None

    def test_opcode_space_canonical_size(self):
        target = CV32E40P()
        space = target.opcode_space()
        # 4 opcodes * 8 funct3 * 4 funct2 = 128, minus 2 hwloop
        # tail reservations = 126.
        assert space.free_slots() == 126

    def test_opcode_space_independent_per_call(self):
        target = CV32E40P()
        space1 = target.opcode_space()
        space2 = target.opcode_space()
        space1.allocate()
        assert space1.free_slots() == 125
        assert space2.free_slots() == 126

    def test_opcode_slots_iteration_order(self):
        target = CV32E40P()
        space = target.opcode_space()
        first = space.allocate()
        # Canonical order: opcode-major, funct3-major, funct2-minor.
        # First slot should be CUSTOM_0 (0x0B), funct3=0, funct2=0.
        assert first.opcode == 0x0B
        assert first.funct3 == 0
        assert first.funct2 == 0

    def test_reset_vector_matches_testbench(self):
        target = CV32E40P()
        # cv32e40p testbench's BOOT_ADDR_I is 0x80.
        assert target.reset_vector == 0x80

    def test_synthesizable_files_all_under_rtl(self):
        from pathlib import Path
        target = CV32E40P()
        for f in target.synthesizable_files():
            assert isinstance(f, Path)
            # Files are relative paths under rtl/ or rtl/include/
            parts = f.parts
            assert parts[0] == "rtl"
