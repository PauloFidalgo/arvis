"""
Canonical register name/number mappings for RISC-V.

Single source of truth for register ABI names, x-register names,
and register number conversions used across the entire tool.
"""

from typing import Optional

# ── x-register → ABI name ──

REG_ABI = {
    "x0": "zero",
    "x1": "ra",
    "x2": "sp",
    "x3": "gp",
    "x4": "tp",
    "x5": "t0",
    "x6": "t1",
    "x7": "t2",
    "x8": "s0",
    "x9": "s1",
    "x10": "a0",
    "x11": "a1",
    "x12": "a2",
    "x13": "a3",
    "x14": "a4",
    "x15": "a5",
    "x16": "a6",
    "x17": "a7",
    "x18": "s2",
    "x19": "s3",
    "x20": "s4",
    "x21": "s5",
    "x22": "s6",
    "x23": "s7",
    "x24": "s8",
    "x25": "s9",
    "x26": "s10",
    "x27": "s11",
    "x28": "t3",
    "x29": "t4",
    "x30": "t5",
    "x31": "t6",
}

# ── ABI name → x-register ──

ABI_REG = {v: k for k, v in REG_ABI.items()}
ABI_REG["fp"] = "x8"  # common alias for s0/x8

# ── Name → register number (supports both x-names and ABI names) ──

REG_NUM: dict[str, int] = {
    **{f"x{i}": i for i in range(32)},
    "zero": 0,
    "ra": 1,
    "sp": 2,
    "gp": 3,
    "tp": 4,
    "t0": 5,
    "t1": 6,
    "t2": 7,
    "s0": 8,
    "fp": 8,
    "s1": 9,
    "a0": 10,
    "a1": 11,
    "a2": 12,
    "a3": 13,
    "a4": 14,
    "a5": 15,
    "a6": 16,
    "a7": 17,
    "s2": 18,
    "s3": 19,
    "s4": 20,
    "s5": 21,
    "s6": 22,
    "s7": 23,
    "s8": 24,
    "s9": 25,
    "s10": 26,
    "s11": 27,
    "t3": 28,
    "t4": 29,
    "t5": 30,
    "t6": 31,
}


# ── Compressed → base-ISA mnemonic mapping ──

COMPRESSED_TO_BASE: dict[str, str] = {
    "c.add": "add",
    "c.sub": "sub",
    "c.and": "and",
    "c.or": "or",
    "c.xor": "xor",
    "c.addi": "addi",
    "c.slli": "slli",
    "c.srli": "srli",
    "c.srai": "srai",
    "c.andi": "andi",
    "c.li": "addi",  # c.li rd, imm → addi rd, x0, imm
    "c.lui": "lui",
    "c.mv": "add",  # c.mv rd, rs2 → add rd, x0, rs2
    "c.lw": "lw",
    "c.lwsp": "lw",
    "c.sw": "sw",
    "c.swsp": "sw",
    "c.addi16sp": "addi",  # c.addi16sp sp, imm → addi sp, sp, imm
    "c.addi4spn": "addi",  # c.addi4spn rd, sp, imm → addi rd, sp, imm
}


def normalize_mnemonic(mnemonic: str) -> str:
    """Map a compressed mnemonic to its base-ISA equivalent.

    Non-compressed mnemonics are returned unchanged.  This ensures that
    fusion analysis and RTL generation treat ``c.add`` and ``add`` as
    the same operation, which matches what the CV32E40P hardware does
    (the compressed decoder decompresses before the main decoder).
    """
    return COMPRESSED_TO_BASE.get(mnemonic, mnemonic)


# ── Commutative operations (rs1/rs2 order doesn't matter) ──

COMMUTATIVE: set[str] = {
    "add",
    "addi",
    "and",
    "andi",
    "or",
    "ori",
    "xor",
    "xori",
    "mul",
    "c.add",
    "c.and",
    "c.or",
    "c.xor",
}


def normalize_reg(name: str) -> Optional[str]:
    """Normalize a register name to xN format. Returns None if not a register."""
    if name is None:
        return None
    name = name.strip().lower().rstrip(",")
    if name in REG_ABI:
        return name
    if name in ABI_REG:
        return ABI_REG[name]
    return None


def reg_to_num(name: str) -> int:
    """Convert register name (ABI or xN) to register number. Returns 0 for unknown."""
    if name is None:
        return 0
    result: int = REG_NUM.get(name.strip().lower().rstrip(","), 0)
    return result
