"""Tests for ``core.isa.ISADescriptor``."""

from __future__ import annotations

import pytest

from arvis.core.isa import ISADescriptor


class TestISADescriptorPresets:
    def test_rv32imc_zicsr_preset(self):
        isa = ISADescriptor.rv32imc_zicsr()
        assert isa.name == "rv32imc_zicsr"
        assert isa.xlen == 32
        assert isa.has("I")
        assert isa.has("M")
        assert isa.has("C")
        assert isa.has("Zicsr")
        assert not isa.has("F")

    def test_rv32im_preset(self):
        isa = ISADescriptor.rv32im()
        assert isa.name == "rv32im"
        assert isa.has("I")
        assert isa.has("M")
        assert not isa.has("C")


class TestISADescriptorCFlags:
    def test_cflags_canonical_ordering(self):
        # Single-letter extensions must appear in canonical RISC-V
        # order (IMAFDQCB...), not alphabetical.
        isa = ISADescriptor.rv32imc_zicsr()
        assert isa.cflags == "rv32imc_zicsr"

    def test_cflags_no_extensions(self):
        isa = ISADescriptor(name="rv32i", standard_extensions=frozenset({"I"}))
        assert isa.cflags == "rv32i"

    def test_cflags_z_extensions_lowercase_and_underscored(self):
        isa = ISADescriptor(
            name="rv32im_zicsr_zifencei",
            standard_extensions=frozenset({"I", "M", "Zicsr", "Zifencei"}),
        )
        # Single letters first (canonical order), then Z* sorted
        assert isa.cflags == "rv32im_zicsr_zifencei"


class TestISADescriptorHas:
    def test_case_insensitive(self):
        isa = ISADescriptor(
            name="x", standard_extensions=frozenset({"M", "Zicsr"})
        )
        assert isa.has("m")
        assert isa.has("M")
        assert isa.has("zicsr")
        assert isa.has("ZICSR")

    def test_unknown_extension_returns_false(self):
        isa = ISADescriptor.rv32imc_zicsr()
        assert not isa.has("Q")
        assert not isa.has("Zfh")


class TestISADescriptorImmutability:
    def test_frozen(self):
        isa = ISADescriptor.rv32imc_zicsr()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            isa.name = "modified"  # type: ignore[misc]
