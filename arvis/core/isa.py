"""Base ISA description.

A :class:`ISADescriptor` captures what a target core implements at the
**standard** ISA level (RV32I / RV32IMC / RV32EC / etc.).  This is
distinct from the *custom extensions* a target may add — those live
in :class:`core.target.OpcodeSpace` because they are project-specific.

This module is intentionally RISC-V-only for now.  The fields are
shaped so that adding a non-RISC-V family later (Cortex-M, MIPS) is
possible by introducing a sibling class with the same protocol; we
chose not to abstract over instruction sets up-front because that
would over-engineer for a hypothetical port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


@dataclass(frozen=True)
class ISADescriptor:
    """Describes the standard ISA implemented by a target core.

    Two cores with the same ``ISADescriptor`` will accept the same
    binaries (modulo their custom extensions).  This is what makes a
    workload portable across cores.

    Attributes
    ----------
    name:
        Canonical name, e.g. ``"rv32imc_zicsr"``.  Used in CFLAGS
        construction and as a key for caching analyses.
    xlen:
        Native register width in bits.  ``32`` for RV32, ``64`` for
        RV64.  Currently only ``32`` is exercised.
    standard_extensions:
        The set of standard RISC-V extensions implemented (e.g.
        ``{"I", "M", "C", "Zicsr"}``).  Used by strategies to decide
        whether a transform is legal (e.g. C-extension presence
        affects loop body alignment).
    abi:
        ABI string, e.g. ``"ilp32"``.  Defaults to the natural ABI
        for the given XLEN.
    """

    name: str
    xlen: int = 32
    standard_extensions: FrozenSet[str] = field(default_factory=frozenset)
    abi: str = "ilp32"

    # ── Common presets ─────────────────────────────────────────────
    @classmethod
    def rv32imc_zicsr(cls) -> "ISADescriptor":
        """The default ISA for cv32e40p baseline benchmarks."""
        return cls(
            name="rv32imc_zicsr",
            xlen=32,
            standard_extensions=frozenset({"I", "M", "C", "Zicsr"}),
            abi="ilp32",
        )

    @classmethod
    def rv32im(cls) -> "ISADescriptor":
        """RV32IM (no compressed extension).  Useful when alignment
        matters more than code density."""
        return cls(
            name="rv32im",
            xlen=32,
            standard_extensions=frozenset({"I", "M", "Zicsr"}),
            abi="ilp32",
        )

    # ── Convenience predicates ─────────────────────────────────────
    def has(self, extension: str) -> bool:
        """Return True if ``extension`` is part of this ISA.

        Comparison is case-insensitive to match the convention of
        spelling extensions either ``Zicsr`` or ``zicsr`` in CFLAGS.
        """
        ext_upper = extension.upper()
        return any(e.upper() == ext_upper for e in self.standard_extensions)

    @property
    def cflags(self) -> str:
        """The ``-march=`` flag for this ISA.

        Constructed as ``rv{xlen}{ext_letters}`` followed by any
        multi-letter ``Z*`` extensions joined by ``_``.  Single-letter
        extensions are emitted in canonical RISC-V order
        (``IMAFDQCB...``) so the result is stable and GCC-accepted.
        """
        # Canonical order for single-letter extensions per the
        # RISC-V ISA manual.  Anything not in this list is appended
        # at the end (alphabetically) so unknown letters still appear.
        canonical = "IMAFDQCBPV"
        upper_exts = {e.upper() for e in self.standard_extensions if len(e) == 1}
        ordered_letters = [c for c in canonical if c in upper_exts]
        ordered_letters += sorted(upper_exts - set(canonical))
        ext_letters = "".join(c.lower() for c in ordered_letters)

        z_exts = sorted(e for e in self.standard_extensions if len(e) > 1)
        z_part = "_" + "_".join(z_exts).lower() if z_exts else ""
        return f"rv{self.xlen}{ext_letters}{z_part}"
