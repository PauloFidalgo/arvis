"""Unified Custom Instruction Registry.

Single source of truth for ALL custom instruction encodings:
- Fused RR+RR patterns (from GCC passes)
- Fused parametric patterns (immediate in rs3/rs2)
- HWLoop instructions (bounds, count, start, end)

Assigns sequential R4 encodings across CUSTOM_0/1/2/3 opcode spaces.
The decoder generator, assembly patcher, and pragma system all read from this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from arvis.codegen.gcc.peephole_gen import _r4_enc


@dataclass
class CustomInstruction:
    """One custom instruction with its assigned encoding."""

    name: str
    opcode: int  # 7-bit opcode (0x0b, 0x2b, 0x5b, 0x7b)
    funct3: int  # 3-bit function select
    funct2: int  # 2-bit function select (R4-type)
    is_r4: bool = True  # R4-type (decode on [26:25]) vs R-type (decode on [31:25])
    kind: str = "fused"  # "fused" | "hwloop"
    # Decoder signals
    signals: Dict[str, str] = field(default_factory=dict)
    # Metadata
    description: str = ""
    count: int = 0  # how many times used in binary


@dataclass
class CustomInstructionRegistry:
    """Registry of all custom instructions with sequential encoding."""

    instructions: List[CustomInstruction] = field(default_factory=list)
    _next_slot: int = 0

    def _alloc_hwloop_slot(self) -> Tuple[int, int, int]:
        """Allocate a full funct3 slot for a hwloop instruction.
        HWLoop instructions use bits[31:20] for data, so they can't share
        funct3 with other instructions (no funct2 sub-decode)."""
        # Round up to next funct3 boundary (multiple of 4)
        self._next_slot = ((self._next_slot + 3) // 4) * 4
        opcode, funct3, _ = _r4_enc(self._next_slot)
        self._next_slot += 4  # reserve all 4 funct2 values
        return opcode, funct3, 0

    def _ensure_hwloop_same_opcode(self, n_insns: int = 4):
        """Ensure the next n_insns hwloop slots all fit on the same opcode.
        If not enough funct3 slots remain on the current opcode, jump to the next."""
        from arvis.codegen.gcc.peephole_gen import _R4_SLOTS_PER_OPCODE

        aligned = ((self._next_slot + 3) // 4) * 4
        slots_needed = n_insns * 4  # each hwloop insn takes a full funct3 (4 funct2 slots)
        opc_start = (aligned // _R4_SLOTS_PER_OPCODE) * _R4_SLOTS_PER_OPCODE
        opc_end = opc_start + _R4_SLOTS_PER_OPCODE
        if aligned + slots_needed > opc_end:
            # Jump to next opcode
            self._next_slot = opc_end

    def add_hwloop_bounds(self, signals: Optional[Dict[str, str]] = None) -> CustomInstruction:
        """Add hwloop.bounds at the next available funct3 slot."""
        if signals is None:
            signals = {"hwlp_we": "1'b1"}
        opcode, funct3, _ = self._alloc_hwloop_slot()
        insn = CustomInstruction(
            name="hwloop.bounds",
            opcode=opcode,
            funct3=funct3,
            funct2=0,
            is_r4=False,
            kind="hwloop",
            signals=signals,
            description="hwloop.bounds",
        )
        self.instructions.append(insn)
        return insn

    def add_hwloop_count(self, signals: Optional[Dict[str, str]] = None) -> CustomInstruction:
        """Add hwloop.count at the next available funct3 slot."""
        if signals is None:
            signals = {"hwlp_we": "1'b1", "regc_used_o": "1'b1", "regc_mux_o": "REGC_S4"}
        opcode, funct3, _ = self._alloc_hwloop_slot()
        insn = CustomInstruction(
            name="hwloop.count",
            opcode=opcode,
            funct3=funct3,
            funct2=0,
            is_r4=False,
            kind="hwloop",
            signals=signals,
            description="hwloop.count",
        )
        self.instructions.append(insn)
        self._next_slot += 1
        return insn

    def add_hwloop_start(self, signals: Optional[Dict[str, str]] = None) -> CustomInstruction:
        """Add hwloop.start (for HW_LOOP > 2)."""
        if signals is None:
            signals = {"hwlp_we": "1'b1"}
        opcode, funct3, _ = self._alloc_hwloop_slot()
        insn = CustomInstruction(
            name="hwloop.start",
            opcode=opcode,
            funct3=funct3,
            funct2=0,
            is_r4=False,
            kind="hwloop",
            signals=signals,
            description="hwloop.start",
        )
        self.instructions.append(insn)
        return insn

    def add_hwloop_end(self, signals: Optional[Dict[str, str]] = None) -> CustomInstruction:
        """Add hwloop.end (for HW_LOOP > 2)."""
        if signals is None:
            signals = {"hwlp_we": "1'b1"}
        opcode, funct3, _ = self._alloc_hwloop_slot()
        insn = CustomInstruction(
            name="hwloop.end",
            opcode=opcode,
            funct3=funct3,
            funct2=0,
            is_r4=False,
            kind="hwloop",
            signals=signals,
            description="hwloop.end",
        )
        self.instructions.append(insn)
        return insn

    @property
    def next_slot(self) -> int:
        return self._next_slot

    @property
    def fused_instructions(self) -> List[CustomInstruction]:
        return [i for i in self.instructions if i.kind == "fused"]

    @property
    def hwloop_instructions(self) -> List[CustomInstruction]:
        return [i for i in self.instructions if i.kind == "hwloop"]

    def get_hwloop_encoding(self):
        """Return HWLoopEncoding for the assembly patcher."""
        from arvis.codegen.hwloop.generator import HWLoopEncoding

        enc = HWLoopEncoding()
        for insn in self.hwloop_instructions:
            if insn.name == "hwloop.bounds":
                enc.bounds_opcode = insn.opcode
                enc.bounds_funct3 = insn.funct3
            elif insn.name == "hwloop.count":
                enc.count_opcode = insn.opcode
                enc.count_funct3 = insn.funct3
            elif insn.name == "hwloop.start":
                enc.start_opcode = insn.opcode
                enc.start_funct3 = insn.funct3
            elif insn.name == "hwloop.end":
                enc.end_opcode = insn.opcode
                enc.end_funct3 = insn.funct3
        return enc

    def by_opcode(self) -> Dict[int, List[CustomInstruction]]:
        """Group instructions by opcode."""
        result: Dict[int, List[CustomInstruction]] = {}
        for insn in self.instructions:
            result.setdefault(insn.opcode, []).append(insn)
        return result


def build_registry_from_used_instructions(
    hw_loop_count: int = 0,
    next_r4_slot: int = 0,
) -> CustomInstructionRegistry:
    """Build a registry from the binary inspection results + hwloop config.

    next_r4_slot: the first free slot after all fused patterns in the .md.
    hwloop instructions are placed starting at this slot.
    """
    reg = CustomInstructionRegistry()
    reg._next_slot = next_r4_slot

    if hw_loop_count > 0:
        reg._ensure_hwloop_same_opcode(n_insns=4)
        reg.add_hwloop_bounds()
        reg.add_hwloop_count()
        # Always allocate start/end slots for unique funct3 in template replacement
        reg.add_hwloop_start()
        reg.add_hwloop_end()

    return reg
