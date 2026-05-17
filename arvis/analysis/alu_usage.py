"""
Analyzes which ALU operations are actually used by the firmware.

This enables removing unused operations from the CV32E40P ALU to:
- Reduce gate count and area
- Simplify timing paths
- Lower power consumption
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set

from arvis.analysis.models import Instruction

# CV32E40P ALU operations mapped from instruction mnemonics
# Based on cv32e40p_pkg.sv alu_opcode_e enum
MNEMONIC_TO_ALU_OP = {
    # Arithmetic
    "add": "ALU_ADD",
    "addi": "ALU_ADD",
    "sub": "ALU_SUB",
    # Logical
    "and": "ALU_AND",
    "andi": "ALU_AND",
    "or": "ALU_OR",
    "ori": "ALU_OR",
    "xor": "ALU_XOR",
    "xori": "ALU_XOR",
    # Shifts
    "sll": "ALU_SLL",
    "slli": "ALU_SLL",
    "srl": "ALU_SRL",
    "srli": "ALU_SRL",
    "sra": "ALU_SRA",
    "srai": "ALU_SRA",
    # Comparisons (set less than)
    "slt": "ALU_SLTS",
    "slti": "ALU_SLTS",
    "sltu": "ALU_SLTU",
    "sltiu": "ALU_SLTU",
    # Upper immediate
    "lui": "ALU_LUI",
    "auipc": "ALU_AUIPC",
    # Branches (comparisons)
    "beq": "ALU_EQ",
    "bne": "ALU_NE",
    "blt": "ALU_LTS",
    "bge": "ALU_GES",
    "bltu": "ALU_LTU",
    "bgeu": "ALU_GEU",
    # Loads/Stores use ADD for address calculation
    "lw": "ALU_ADD",
    "lh": "ALU_ADD",
    "lhu": "ALU_ADD",
    "lb": "ALU_ADD",
    "lbu": "ALU_ADD",
    "sw": "ALU_ADD",
    "sh": "ALU_ADD",
    "sb": "ALU_ADD",
    # Jumps
    "jal": "ALU_ADD",  # PC + offset
    "jalr": "ALU_ADD",  # rs1 + offset
    # Multiplication (M extension)
    "mul": "ALU_ADD",  # Result from multiplier, ALU just passes
    "mulh": "ALU_ADD",
    "mulhsu": "ALU_ADD",
    "mulhu": "ALU_ADD",
    "div": "ALU_ADD",
    "divu": "ALU_ADD",
    "rem": "ALU_ADD",
    "remu": "ALU_ADD",
    # Compressed instructions map to their base counterparts
    "c.add": "ALU_ADD",
    "c.addi": "ALU_ADD",
    "c.addi16sp": "ALU_ADD",
    "c.addi4spn": "ALU_ADD",
    "c.sub": "ALU_SUB",
    "c.and": "ALU_AND",
    "c.andi": "ALU_AND",
    "c.or": "ALU_OR",
    "c.xor": "ALU_XOR",
    "c.slli": "ALU_SLL",
    "c.srli": "ALU_SRL",
    "c.srai": "ALU_SRA",
    "c.mv": "ALU_ADD",  # mv rd, rs2 → add rd, x0, rs2
    "c.li": "ALU_ADD",  # li rd, imm → addi rd, x0, imm
    "c.lui": "ALU_LUI",
    "c.beqz": "ALU_EQ",
    "c.bnez": "ALU_NE",
    "c.lw": "ALU_ADD",
    "c.lwsp": "ALU_ADD",
    "c.sw": "ALU_ADD",
    "c.swsp": "ALU_ADD",
    "c.j": "ALU_ADD",
    "c.jal": "ALU_ADD",
    "c.jr": "ALU_ADD",
    "c.jalr": "ALU_ADD",
    # CSR instructions
    "csrrw": "ALU_OR",  # Typically pass-through
    "csrrs": "ALU_OR",
    "csrrc": "ALU_AND",
    "csrrwi": "ALU_OR",
    "csrrsi": "ALU_OR",
    "csrrci": "ALU_AND",
    "csrw": "ALU_OR",  # pseudo for csrrw
    "csrr": "ALU_OR",  # pseudo for csrrs
    # Fence (no ALU)
    "fence": None,
    "fence.i": None,
    # System
    "ecall": None,
    "ebreak": None,
    "mret": None,
    "dret": None,
    "wfi": None,
    "nop": None,  # pseudo for addi x0, x0, 0 (no ALU output used)
    # Pseudo-instructions (objdump may show these instead of the base form)
    # Each maps to the ALU operation its base instruction uses.
    "mv": "ALU_ADD",  # mv rd, rs → add rd, rs, x0
    "li": "ALU_ADD",  # li rd, imm → addi rd, x0, imm (or lui+addi)
    "ret": "ALU_ADD",  # ret → jalr x0, x1, 0
    "j": "ALU_ADD",  # j offset → jal x0, offset
    "jr": "ALU_ADD",  # jr rs → jalr x0, rs, 0
    "not": "ALU_XOR",  # not rd, rs → xori rd, rs, -1
    "neg": "ALU_SUB",  # neg rd, rs → sub rd, x0, rs
    "seqz": "ALU_SLTU",  # seqz rd, rs → sltiu rd, rs, 1
    "snez": "ALU_SLTU",  # snez rd, rs → sltu rd, x0, rs
    "sltz": "ALU_SLTS",  # sltz rd, rs → slt rd, rs, x0
    "sgtz": "ALU_SLTS",  # sgtz rd, rs → slt rd, x0, rs
    "beqz": "ALU_EQ",  # beqz rs, off → beq rs, x0, off
    "bnez": "ALU_NE",  # bnez rs, off → bne rs, x0, off
    "blez": "ALU_GES",  # blez rs, off → bge x0, rs, off
    "bgez": "ALU_GES",  # bgez rs, off → bge rs, x0, off
    "bltz": "ALU_LTS",  # bltz rs, off → blt rs, x0, off
    "bgtz": "ALU_LTS",  # bgtz rs, off → blt x0, rs, off
    "zext.b": "ALU_AND",  # zext.b rd, rs → andi rd, rs, 0xff
    "sext.w": "ALU_ADD",  # sext.w rd, rs → addiw rd, rs, 0 (RV64 only, but just in case)
    "call": "ALU_ADD",  # call addr → auipc+jalr (both need ALU_ADD)
    "tail": "ALU_ADD",  # tail addr → auipc+jalr
    "la": "ALU_ADD",  # la rd, sym → auipc+addi
}

# All ALU operations defined in CV32E40P
ALL_ALU_OPS = {
    # Basic arithmetic/logic (always needed)
    "ALU_ADD",
    "ALU_SUB",
    "ALU_AND",
    "ALU_OR",
    "ALU_XOR",
    # Shifts
    "ALU_SLL",
    "ALU_SRL",
    "ALU_SRA",
    # Comparisons
    "ALU_SLTS",
    "ALU_SLTU",
    "ALU_EQ",
    "ALU_NE",
    "ALU_LTS",
    "ALU_GES",
    "ALU_LTU",
    "ALU_GEU",
    # Upper immediate
    "ALU_LUI",
    "ALU_AUIPC",
    # PULP extensions (optional)
    "ALU_ABS",
    "ALU_CLIP",
    "ALU_CLIPU",
    "ALU_INS",
    "ALU_BEXT",
    "ALU_BEXTU",
    "ALU_BINS",
    "ALU_BCLR",
    "ALU_BSET",
    "ALU_BREV",
    "ALU_FF1",
    "ALU_FL1",
    "ALU_CNT",
    "ALU_CLB",
    "ALU_MIN",
    "ALU_MINU",
    "ALU_MAX",
    "ALU_MAXU",
    "ALU_EXTS",
    "ALU_EXT",
    "ALU_ROR",
    "ALU_SHUF",
    "ALU_SHUF2",
    "ALU_PCKLO",
    "ALU_PCKHI",
}

# Operations that should never be removed (always needed for correct operation)
ESSENTIAL_OPS = {
    "ALU_ADD",  # Used for address calculation, jumps, etc.
    "ALU_SUB",  # Needed for comparisons internally
}


@dataclass
class ALUUsageResult:
    """Result of ALU usage analysis."""

    used_ops: Set[str] = field(default_factory=set)
    unused_ops: Set[str] = field(default_factory=set)
    removable_ops: Set[str] = field(default_factory=set)  # unused minus essential
    instruction_counts: Dict[str, int] = field(default_factory=dict)
    area_savings_estimate: float = 0.0

    def summary(self) -> str:
        lines = [
            "ALU Usage Analysis",
            f"  Used operations:     {len(self.used_ops)}",
            f"  Unused operations:   {len(self.unused_ops)}",
            f"  Removable operations: {len(self.removable_ops)}",
            f"  Estimated area savings: ~{self.area_savings_estimate:.1f}%",
        ]
        if self.removable_ops:
            lines.append("\n  Removable:")
            for op in sorted(self.removable_ops):
                lines.append(f"    - {op}")
        return "\n".join(lines)


class ALUUsageAnalyzer:
    """Analyzes which ALU operations are used by firmware."""

    def __init__(self, objdump_path: str = "riscv64-unknown-elf-objdump"):
        self.objdump = objdump_path

    def analyze_instructions(self, instructions: List[Instruction]) -> ALUUsageResult:
        """Analyze a list of Instruction objects."""
        inst_counts: Dict[str, int] = {}
        used_ops: Set[str] = set()

        for inst in instructions:
            mnemonic = inst.mnemonic.lower()
            inst_counts[mnemonic] = inst_counts.get(mnemonic, 0) + 1

            alu_op = MNEMONIC_TO_ALU_OP.get(mnemonic)
            if alu_op:
                used_ops.add(alu_op)

        return self._compute_result(used_ops, inst_counts)

    def _compute_result(self, used_ops: Set[str], inst_counts: Dict[str, int]) -> ALUUsageResult:
        """Compute the analysis result."""
        # Ensure essential ops are always marked as used
        used_ops = used_ops | ESSENTIAL_OPS

        # Find unused operations
        unused_ops = ALL_ALU_OPS - used_ops

        # Removable = unused minus essential
        removable_ops = unused_ops - ESSENTIAL_OPS

        # Estimate area savings
        # PULP extensions are the biggest (bit manipulation, clip, etc.)
        # Each operation is roughly equal in the case statement
        pulp_ops = {
            "ALU_ABS",
            "ALU_CLIP",
            "ALU_CLIPU",
            "ALU_INS",
            "ALU_BEXT",
            "ALU_BEXTU",
            "ALU_BINS",
            "ALU_BCLR",
            "ALU_BSET",
            "ALU_BREV",
            "ALU_FF1",
            "ALU_FL1",
            "ALU_CNT",
            "ALU_CLB",
            "ALU_MIN",
            "ALU_MINU",
            "ALU_MAX",
            "ALU_MAXU",
            "ALU_EXTS",
            "ALU_EXT",
            "ALU_ROR",
            "ALU_SHUF",
            "ALU_SHUF2",
            "ALU_PCKLO",
            "ALU_PCKHI",
        }

        removable_pulp = removable_ops & pulp_ops
        removable_basic = removable_ops - pulp_ops

        # PULP ops are more complex, weight them higher
        _total_ops = len(ALL_ALU_OPS)  # noqa: F841
        pulp_weight = 2.0  # PULP ops are ~2x the area of basic ops

        effective_removed = len(removable_basic) + len(removable_pulp) * pulp_weight
        effective_total = (len(ALL_ALU_OPS) - len(pulp_ops)) + len(pulp_ops) * pulp_weight

        area_savings = (effective_removed / effective_total) * 100 if effective_total > 0 else 0

        return ALUUsageResult(
            used_ops=used_ops,
            unused_ops=unused_ops,
            removable_ops=removable_ops,
            instruction_counts=inst_counts,
            area_savings_estimate=area_savings,
        )
