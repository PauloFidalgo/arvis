"""
Assembly-level fusion patcher.

Scans a .s file for consecutive instruction pairs/triples that match
known fused patterns, and replaces them with .insn directives.

Unlike the Docker/GCC approach, this preserves the original register
allocation and code layout — only the fused instructions change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Mnemonics that are ALU-fusible (single-cycle, no memory, no branch)
ALU_MNEMONICS = {
    "add",
    "sub",
    "and",
    "or",
    "xor",
    "sll",
    "srl",
    "sra",
    "slli",
    "srli",
    "srai",
    "addi",
    "andi",
    "ori",
    "xori",
    "slt",
    "sltu",
    "sltiu",
    "slti",
    "mul",
}

# Compressed → base ISA normalization
COMPRESSED_MAP = {
    "c.add": "add",
    "c.sub": "sub",
    "c.and": "and",
    "c.or": "or",
    "c.xor": "xor",
    "c.addi": "addi",
    "c.andi": "andi",
    "c.slli": "slli",
    "c.srli": "srli",
    "c.srai": "srai",
    "c.mv": "add",
    "c.li": "addi",
}


@dataclass
class AsmInsn:
    """Parsed assembly instruction."""

    line_num: int
    raw: str
    mnemonic: str
    base_mnem: str
    rd: Optional[str] = None
    rs1: Optional[str] = None
    rs2: Optional[str] = None
    rs3: Optional[str] = None
    imm: Optional[int] = None
    is_alu: bool = False


def parse_asm_instructions(lines: List[str]) -> List[AsmInsn]:
    """Parse assembly lines into structured instructions.

    Only parses ALU instructions — skips loads, stores, branches, labels.
    Returns instructions in order with their line numbers.
    """
    result = []
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Pure label line
        if stripped.endswith(":") and "\t" not in stripped and " " not in stripped.rstrip(":"):
            continue
        # Directive
        if stripped.startswith(".") and not stripped.startswith(".insn"):
            # But keep .LM labels as non-instructions (they'll be skipped)
            continue

        # Parse .insn lines: extract rd so we can detect .insn → next chains
        if stripped.startswith(".insn"):
            m_insn = re.match(
                r"\.insn\s+r4?\s+\S+,\s*\d+,\s*\S+,\s*(\w+),\s*(\w+),\s*(\w+)(?:,\s*(\w+))?",
                stripped,
            )
            if m_insn:
                rs3 = m_insn.group(4) if m_insn.group(4) and not m_insn.group(4).startswith("x") else None
                insn = AsmInsn(
                    line_num=i,
                    raw=raw,
                    mnemonic=".insn",
                    base_mnem=".insn",
                    rd=m_insn.group(1),
                    rs1=m_insn.group(2),
                    rs2=m_insn.group(3),
                    rs3=rs3,
                    is_alu=False,  # not patchable itself, but rd is trackable
                )
                result.append(insn)
            continue

        # Label on same line as instruction: "label: insn"
        if ":" in stripped.split("\t")[0] if "\t" in stripped else ":" in stripped.split(" ")[0]:
            parts = stripped.split(":", 1)
            stripped = parts[1].strip() if len(parts) > 1 else ""
            if not stripped:
                continue

        m = re.match(r"(\S+)\s*(.*)", stripped)
        if not m:
            continue

        mnem = m.group(1)
        operands_str = m.group(2).strip()
        base = COMPRESSED_MAP.get(mnem, mnem)
        is_alu = base in ALU_MNEMONICS

        insn = AsmInsn(line_num=i, raw=raw, mnemonic=mnem, base_mnem=base, is_alu=is_alu)

        if not operands_str:
            result.append(insn)
            continue

        ops = [x.strip() for x in operands_str.split(",")]

        # Handle memory ops: parse rd and base register from offset(reg)
        if any("(" in o for o in ops):
            insn.rd = ops[0] if ops else None
            for o in ops[1:]:
                m_mem = re.match(r"(-?\d+)\((\w+)\)", o)
                if m_mem:
                    insn.rs1 = m_mem.group(2)  # base register
                    break
            # For stores (sw, sh, sb), rs1 is the value, rs2-like is the base
            if insn.mnemonic in ("sw", "sh", "sb", "c.sw", "c.swsp"):
                # sw rs2, offset(rs1) — rd field is actually the source register
                pass  # rd=source reg, rs1=base reg — both are reads
            result.append(insn)
            continue

        insn.rd = ops[0] if ops else None
        _parse_operands(insn, mnem, base, ops)
        result.append(insn)

    return result


def _parse_operands(insn: AsmInsn, mnem: str, base: str, ops: List[str]):
    """Fill rs1, rs2, imm from operand list."""
    # Branches: no rd, both operands are sources
    BRANCH_MNEMS = {
        "beq",
        "bne",
        "blt",
        "bge",
        "bltu",
        "bgeu",
        "bgtu",
        "bleu",
        "blez",
        "bgez",
        "bltz",
        "bgtz",
        "beqz",
        "bnez",
    }
    if base in BRANCH_MNEMS or mnem in BRANCH_MNEMS:
        insn.rd = None
        insn.rs1 = ops[0] if ops else None
        insn.rs2 = ops[1] if len(ops) >= 2 else None
        return
    # Jumps: j/jal/jalr
    if base in ("j", "jr", "ret", "tail", "call"):
        insn.rd = None
        insn.rs1 = ops[0] if ops else None
        return
    if base == "jal":
        # jal rd, offset OR jal offset
        if len(ops) >= 2:
            insn.rs1 = None
        else:
            insn.rd = None
        return
    if base == "jalr":
        insn.rs1 = ops[1] if len(ops) >= 2 else ops[0] if ops else None
        return
    if mnem in ("c.mv",):
        insn.rs1 = "zero"
        insn.rs2 = ops[1] if len(ops) >= 2 else None
    elif mnem in ("c.li",):
        insn.rs1 = "zero"
        insn.imm = _try_parse_imm(ops[1]) if len(ops) >= 2 else None
    elif mnem.startswith("c.") and base in ("addi", "andi"):
        insn.rs1 = insn.rd
        insn.imm = _try_parse_imm(ops[1]) if len(ops) >= 2 else None
    elif mnem.startswith("c.") and base in ("slli", "srli", "srai"):
        insn.rs1 = insn.rd
        insn.imm = _try_parse_imm(ops[1]) if len(ops) >= 2 else None
    elif mnem.startswith("c.") and base in ("add", "sub", "and", "or", "xor"):
        insn.rs1 = insn.rd
        insn.rs2 = ops[1] if len(ops) >= 2 else None
    elif base in ("slli", "srli", "srai", "addi", "andi", "ori", "xori", "slti", "sltiu"):
        insn.rs1 = ops[1] if len(ops) >= 2 else None
        insn.imm = _try_parse_imm(ops[2]) if len(ops) >= 3 else None
        if insn.imm is None and len(ops) >= 3:
            insn.rs2 = ops[2]
    else:
        insn.rs1 = ops[1] if len(ops) >= 2 else None
        insn.rs2 = ops[2] if len(ops) >= 3 else None


def _try_parse_imm(s: str) -> Optional[int]:
    """Try to parse an immediate, return None if not a number."""
    if not s:
        return None
    s = s.strip()
    try:
        if s.startswith("0x") or s.startswith("0X"):
            return int(s, 16)
        if s.startswith("-0x") or s.startswith("-0X"):
            return -int(s[1:], 16)
        return int(s)
    except ValueError:
        return None


# Ops where operand order matters (a op b ≠ b op a)
NON_COMMUTATIVE = {"sub", "sll", "srl", "sra", "slli", "srli", "srai", "slt", "sltu", "sltiu"}
