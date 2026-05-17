"""
Spike instruction generator for custom fused instructions.

Provides TWO approaches:

**Approach A — Native rebuild (recommended):**
  Generates native Spike instruction definitions compiled into Spike.
  No --extlib needed. Exact encoding match, no dispatch translation.

**Approach B — RoCC extension (legacy, still supported):**
  Generates a C++ shared library using Spike's rocc_t API.
  Has R4-type funct7 encoding complexity but works without rebuilding.

Encoding: All fused instructions use RISC-V custom-0 (0x0B).
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CUSTOM_0_OPCODE = 0x0B

# ═══════════════════════════════════════════════════════════════════════════
# C expression table (shared by both approaches)
# ═══════════════════════════════════════════════════════════════════════════

MNEMONIC_TO_C: Dict[str, Dict[str, Any]] = {
    "add": {"expr": "((uint32_t)({a}) + (uint32_t)({b}))", "commutative": True},
    "addi": {"expr": "((uint32_t)({a}) + (uint32_t)({b}))", "commutative": True},
    "sub": {"expr": "((uint32_t)({a}) - (uint32_t)({b}))", "commutative": False},
    "and": {"expr": "((uint32_t)({a}) & (uint32_t)({b}))", "commutative": True},
    "andi": {"expr": "((uint32_t)({a}) & (uint32_t)({b}))", "commutative": True},
    "or": {"expr": "((uint32_t)({a}) | (uint32_t)({b}))", "commutative": True},
    "ori": {"expr": "((uint32_t)({a}) | (uint32_t)({b}))", "commutative": True},
    "xor": {"expr": "((uint32_t)({a}) ^ (uint32_t)({b}))", "commutative": True},
    "xori": {"expr": "((uint32_t)({a}) ^ (uint32_t)({b}))", "commutative": True},
    "sll": {"expr": "((uint32_t)({a}) << ((uint32_t)({b}) & 0x1F))", "commutative": False},
    "slli": {"expr": "((uint32_t)({a}) << ((uint32_t)({b}) & 0x1F))", "commutative": False},
    "srl": {"expr": "((uint32_t)({a}) >> ((uint32_t)({b}) & 0x1F))", "commutative": False},
    "srli": {"expr": "((uint32_t)({a}) >> ((uint32_t)({b}) & 0x1F))", "commutative": False},
    "sra": {
        "expr": "((uint32_t)((int32_t)({a}) >> ((uint32_t)({b}) & 0x1F)))",
        "commutative": False,
    },
    "srai": {
        "expr": "((uint32_t)((int32_t)({a}) >> ((uint32_t)({b}) & 0x1F)))",
        "commutative": False,
    },
    "slt": {"expr": "((uint32_t)((int32_t)({a}) < (int32_t)({b}) ? 1 : 0))", "commutative": False},
    "slti": {"expr": "((uint32_t)((int32_t)({a}) < (int32_t)({b}) ? 1 : 0))", "commutative": False},
    "sltu": {"expr": "((uint32_t)({a}) < (uint32_t)({b}) ? 1U : 0U)", "commutative": False},
    "sltiu": {"expr": "((uint32_t)({a}) < (uint32_t)({b}) ? 1U : 0U)", "commutative": False},
    "xori_neg1": {"expr": "(~(uint32_t)({a}))", "commutative": True, "unary": True},
    "mul": {"expr": "((uint32_t)((uint32_t)({a}) * (uint32_t)({b})))", "commutative": True},
    "mulh": {
        "expr": "((uint32_t)(((int64_t)(int32_t)({a}) * (int64_t)(int32_t)({b})) >> 32))",
        "commutative": True,
    },
    "mulhu": {
        "expr": "((uint32_t)(((uint64_t)(uint32_t)({a}) * (uint64_t)(uint32_t)({b})) >> 32))",
        "commutative": True,
    },
    "mulhsu": {
        "expr": "((uint32_t)(((int64_t)(int32_t)({a}) * (uint64_t)(uint32_t)({b})) >> 32))",
        "commutative": False,
    },
    "lui": {"expr": "((uint32_t)({b}) << 12)", "commutative": False, "unary": True},
    "mv": {"expr": "(uint32_t)({a})", "commutative": False, "unary": True},
    "nop": {"expr": "0U", "commutative": True, "unary": True},
}

IMM_FOLDS_C: Dict[Tuple[str, int], str] = {
    ("xori", -1): "xori_neg1",
}

_COMPRESSED_TO_BASE: Dict[str, str] = {
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
    "c.li": "addi",
    "c.lui": "lui",
    "c.mv": "mv",
    "c.lw": "lw",
    "c.lwsp": "lw",
    "c.sw": "sw",
    "c.swsp": "sw",
    "c.addi16sp": "addi",
    "c.addi4spn": "addi",
    "c.beqz": "beq",
    "c.bnez": "bne",
    "c.j": "jal",
    "c.jal": "jal",
    "c.jr": "jalr",
    "c.jalr": "jalr",
}


def _normalize_mnemonic(mnem: str) -> str:
    return _COMPRESSED_TO_BASE.get(mnem, mnem)


# ═══════════════════════════════════════════════════════════════════════════
# Approach A — Native Spike instruction definitions
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class FusedEncoding:
    """Encoding details for a single fused instruction."""

    name: str
    index: int
    is_r4: bool
    opcode: int = CUSTOM_0_OPCODE
    funct3: int = 0
    funct7: int = 0
    funct2: int = 0
    match: int = 0
    mask: int = 0
    c_body: str = ""
    constituents: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.is_r4:
            self.match = self.opcode | (self.funct3 << 12) | (self.funct2 << 25)
            self.mask = 0x0600_707F
        else:
            self.match = self.opcode | (self.funct3 << 12) | (self.funct7 << 25)
            self.mask = 0xFE00_707F


@dataclass
class SpikeNativeFiles:
    """Collection of files for native Spike integration."""

    insn_files: dict[str, str] = field(default_factory=dict)
    encoding_h_block: str = ""
    opcodes_block: str = ""
    patch_script: str = ""
    encodings: list[FusedEncoding] = field(default_factory=list)


# Spike insns/*.h expression map (uses Spike macros: RS1, RS2, RS3, WRITE_RD)
#
# IMPORTANT: Spike is internally RV64 — all sreg_t/reg_t are 64 bits even
# when simulating RV32.  Every arithmetic result MUST be explicitly truncated
# to 32 bits with sext32() so the fused instruction produces the same
# bit-pattern as the RTL (which is truly 32-bit).  Without this, operations
# like mul produce 64-bit results whose upper bits corrupt subsequent
# arithmetic (for example, a workload performing modular reduction may
# diverge because the upper 32 bits leak into the result).
_SPIKE_EXPR: dict[str, Optional[str]] = {
    # ALU reg-reg
    "add": "sext32((int32_t)RS1 + (int32_t)RS2)",
    "sub": "sext32((int32_t)RS1 - (int32_t)RS2)",
    "sll": "sext32((int32_t)((uint32_t)(int32_t)RS1 << (RS2 & 0x1F)))",
    "srl": "sext32((int32_t)((uint32_t)(int32_t)RS1 >> (RS2 & 0x1F)))",
    "sra": "sext32((int32_t)RS1 >> (RS2 & 0x1F))",
    "and": "sext32((int32_t)RS1 & (int32_t)RS2)",
    "or": "sext32((int32_t)RS1 | (int32_t)RS2)",
    "xor": "sext32((int32_t)RS1 ^ (int32_t)RS2)",
    "slt": "((int32_t)RS1 < (int32_t)RS2) ? 1 : 0",
    "sltu": "((uint32_t)(int32_t)RS1 < (uint32_t)(int32_t)RS2) ? 1 : 0",
    # ALU reg-imm
    "addi": "sext32((int32_t)RS1 + (int32_t)insn.i_imm())",
    "slli": "sext32((int32_t)((uint32_t)(int32_t)RS1 << insn.shamt()))",
    "srli": "sext32((int32_t)((uint32_t)(int32_t)RS1 >> insn.shamt()))",
    "srai": "sext32((int32_t)RS1 >> insn.shamt())",
    "andi": "sext32((int32_t)RS1 & (int32_t)insn.i_imm())",
    "ori": "sext32((int32_t)RS1 | (int32_t)insn.i_imm())",
    "xori": "sext32((int32_t)RS1 ^ (int32_t)insn.i_imm())",
    "slti": "((int32_t)RS1 < (int32_t)insn.i_imm()) ? 1 : 0",
    "sltiu": "((uint32_t)(int32_t)RS1 < (uint32_t)(int32_t)insn.i_imm()) ? 1 : 0",
    "lui": "sext32(insn.u_imm())",
    # M extension — 32-bit truncated
    "mul": "sext32((int32_t)((int32_t)RS1 * (int32_t)RS2))",
    "mulh": "sext32((int32_t)(((int64_t)(int32_t)RS1 * (int64_t)(int32_t)RS2) >> 32))",
    "mulhu": "sext32((int32_t)(((uint64_t)(uint32_t)(int32_t)RS1 * (uint64_t)(uint32_t)(int32_t)RS2) >> 32))",
    "mulhsu": "sext32((int32_t)(((int64_t)(int32_t)RS1 * (uint64_t)(uint32_t)(int32_t)RS2) >> 32))",
    "div": "sext32(((int32_t)RS2 == 0) ? (int32_t)-1 : (((int32_t)RS1 == INT32_MIN && (int32_t)RS2 == -1) ? (int32_t)RS1 : (int32_t)((int32_t)RS1 / (int32_t)RS2)))",
    "divu": "sext32(((uint32_t)(int32_t)RS2 == 0) ? (uint32_t)-1 : (uint32_t)((uint32_t)(int32_t)RS1 / (uint32_t)(int32_t)RS2))",
    "rem": "sext32(((int32_t)RS2 == 0) ? (int32_t)RS1 : (((int32_t)RS1 == INT32_MIN && (int32_t)RS2 == -1) ? 0 : (int32_t)((int32_t)RS1 % (int32_t)RS2)))",
    "remu": "sext32(((uint32_t)(int32_t)RS2 == 0) ? (uint32_t)(int32_t)RS1 : (uint32_t)((uint32_t)(int32_t)RS1 % (uint32_t)(int32_t)RS2))",
    # Load (result already 32-bit from MMU)
    "lw": "MMU.load<int32_t>(RS1 + insn.i_imm())",
    "lh": "sext32(MMU.load<int16_t>(RS1 + insn.i_imm()))",
    "lb": "sext32(MMU.load<int8_t>(RS1 + insn.i_imm()))",
    "lhu": "sext32((int32_t)(uint16_t)MMU.load<uint16_t>(RS1 + insn.i_imm()))",
    "lbu": "sext32((int32_t)(uint8_t)MMU.load<uint8_t>(RS1 + insn.i_imm()))",
    # Store (no rd, handled specially)
    "sw": None,
    "sh": None,
    "sb": None,
}


# Mnemonics that use immediates from the instruction encoding
_IMM_MNEMONICS = {"addi", "slli", "srli", "srai", "andi", "ori", "xori", "slti", "sltiu", "lui"}


def _replace_sv_func(text: str, func_name: str, c_cast: str) -> str:
    """Replace $signed(...) or $unsigned(...) with C cast, handling nested parens.

    Uses a simple scanner to find balanced parentheses after the function name.
    """
    result = []
    i = 0
    while i < len(text):
        if text[i:].startswith(func_name + "("):
            # Found $signed( or $unsigned(
            start = i + len(func_name) + 1  # skip past the opening (
            depth = 1
            j = start
            while j < len(text) and depth > 0:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            # text[start:j-1] is the argument (balanced)
            arg = text[start : j - 1]
            result.append(f"({c_cast}({arg}))")
            i = j
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def _sv_to_c(sv_expr: str) -> str:
    """Translate a SystemVerilog ALU expression to C for Spike.

    SV uses: operand_a_i (RS1), operand_b_i (RS2), operand_c_i (RS3)
    SV uses: $signed(), $unsigned(), 32'd<n>, {a,b} (concat)
    """
    c = sv_expr
    # SV $signed/$unsigned → C casts using balanced paren matching
    # Repeat until stable (handles nested $signed($signed(...)))
    for _ in range(10):
        prev = c
        c = _replace_sv_func(c, "$signed", "(int32_t)")
        c = _replace_sv_func(c, "$unsigned", "(uint32_t)")
        if c == prev:
            break
    # Operand mapping
    c = c.replace("operand_a_i", "((int32_t)RS1)")
    c = c.replace("operand_b_i", "((int32_t)RS2)")
    c = c.replace("operand_c_i", "((int32_t)RS3)")
    # SV 32'd<n> → C literal
    c = re.sub(r"32'd(\d+)", r"\1U", c)
    # SV 32'h<hex> → C hex literal
    c = re.sub(r"32'h([0-9a-fA-F]+)", r"0x\1U", c)
    # SV bit-width specifiers: <n>'d<m> → <m>U
    c = re.sub(r"\d+'d(\d+)", r"\1U", c)
    # SV concatenation {a, b} → not directly translatable, leave as-is
    # SV >>> (arithmetic right shift) → C arithmetic shift with signed cast
    # SV >> (logical right shift) → C logical shift with unsigned cast
    # IMPORTANT: Process >>> FIRST (before >>), and use a temporary marker
    # to avoid double-replacement.
    # Step 1: Replace >>> (arithmetic right shift) with marker
    c = c.replace(">>>", " __ARITH_RSHIFT__ ")
    # Step 2: All remaining >> are logical shifts in SV.
    # Cast their left operand from (int32_t) to (uint32_t) to get
    # zero-fill behavior instead of sign-extension.
    # We do a global replacement: any "(int32_t)X) >> N" → "(uint32_t)(int32_t)X) >> N"
    # This is safe because the outer sext32((uint32_t)(...)) in _gen_native_body
    # handles the final sign extension.
    c = c.replace(">> ", "__LOGICAL_RSHIFT__ ")
    c = c.replace(">>", "__LOGICAL_RSHIFT__")
    # Now replace back: for logical shifts, wrap the expression in uint32_t
    # Simple approach: insert (uint32_t) cast before every __LOGICAL_RSHIFT__
    # by finding the preceding closing paren and wrapping
    result_parts = []
    parts = c.split("__LOGICAL_RSHIFT__")
    for i, part in enumerate(parts):
        if i == 0:
            result_parts.append(part)
            continue
        # The LHS of the shift is everything before this split point
        # We need to cast it to uint32_t. Find the expression boundary.
        lhs = result_parts[-1]
        # Wrap the entire previous expression in (uint32_t)(...)
        # Simple heuristic: if lhs ends with ') ' or ')', wrap it
        lhs_stripped = lhs.rstrip()
        if lhs_stripped.endswith(")"):
            # Find the matching open paren
            depth = 0
            j = len(lhs_stripped) - 1
            while j >= 0:
                if lhs_stripped[j] == ")":
                    depth += 1
                elif lhs_stripped[j] == "(":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            if j >= 0:
                before = lhs_stripped[:j]
                expr = lhs_stripped[j:]
                result_parts[-1] = before + "(uint32_t)" + expr + " >> "
            else:
                result_parts[-1] = "(uint32_t)(" + lhs + ") >> "
        else:
            result_parts[-1] = "(uint32_t)(" + lhs + ") >> "
        result_parts.append(part)
    c = "".join(result_parts)
    # Step 3: Restore arithmetic shift marker back to >>
    c = c.replace(" __ARITH_RSHIFT__ ", " >> ")
    # SV <<< (logical left shift, same as << in C)
    c = c.replace("<<<", "<<")
    # CRITICAL: On Spike's 64-bit internal representation, left shifts
    # produce 64-bit results.  If followed by arithmetic right shift (>>>),
    # the sign bit is at position 47 (not 31), giving wrong RV32 results.
    # Fix: truncate every left shift result to 32 bits by wrapping in
    # (uint32_t)(...) << N → (uint32_t)((uint32_t)(...) << N)
    # This ensures bits 63:32 are zero after the shift.
    import re as _re2

    def _truncate_lshift(m: _re2.Match) -> str:
        """Wrap left shift in (uint32_t) to truncate to 32 bits."""
        expr = m.group(1)
        shift = m.group(2)
        return f"(uint32_t)({expr} << {shift})"

    # Match patterns like (EXPR << NU) where EXPR contains int32_t casts
    c = _re2.sub(r"\(([^()]+(?:\([^()]*\))*[^()]*)\s*<<\s*(\d+U?)\)", _truncate_lshift, c)
    # fused_imm_i: RTL routes instruction encoding bits as immediate
    # fused_imm_i[4:0] = instr[24:20] (rs2 field) → insn.rs2() in Spike
    # fused_imm_i[9:5] = instr[31:27] (rs3 field) → ((insn.bits() >> 27) & 0x1F)
    c = c.replace("fused_imm_i[4:0]", "insn.rs2()")
    c = c.replace("fused_imm_i[9:5]", "((insn.bits() >> 27) & 0x1F)")
    c = re.sub(r"fused_imm_i", "((insn.rs2()) | (((insn.bits() >> 27) & 0x1F) << 5))", c)
    # SV bit-selects [N:M] → remove (C uses explicit masks/casts instead)
    c = re.sub(r"\[\d+:\d+\]", "", c)
    return c


def _gen_native_body(
    name: str,
    parts: list[str],
    is_r4: bool,
    imm_values: dict[int, int] | None = None,
    is_reverse: bool = False,
    sv_expression: str | None = None,
) -> str:
    """Generate C++ body for insns/*.h using Spike macros.

    PRIMARY: Uses sv_expression from FusedOperation (the same expression
    that drives the RTL ALU patch). This already has HC immediates baked
    in, reversal applied, and correct operand mapping — all verified by
    the RTL generator. We just translate SV syntax → C syntax.

    FALLBACK: Uses _build_c_expression (RoCC builder) for ops without SV.
    """
    # Primary: use sv_expression from FusedOperation
    if sv_expression:
        c_expr = _sv_to_c(sv_expression)
        return f"// {'+'.join(parts)} (from sv_expression)\nWRITE_RD(sext32((uint32_t)({c_expr})));"

    # No sv_expression available (e.g. GIMPLE 3-gram with unknown mnemonics)
    return f"// {'+'.join(parts)} (no sv_expression)\nWRITE_RD(0); // unsupported"


def generate_insn_file(enc: FusedEncoding) -> str:
    """Generate a complete insns/<name>.h file for Spike."""
    hdr = (
        f"// Auto-generated: {enc.name}\n"
        f"// {' + '.join(enc.constituents)}\n"
        f"// match=0x{enc.match:08X} mask=0x{enc.mask:08X}\n"
    )
    return hdr + (enc.c_body or "WRITE_RD(0);") + "\n"


def generate_encoding_h_defines(encodings: list[FusedEncoding]) -> str:
    """Generate MATCH_/MASK_ #define lines for encoding.h (top section)."""
    lines = ["// ===== Auto-generated fused encoding defines ====="]
    for e in encodings:
        u = e.name.upper()
        lines.append(f"#define MATCH_{u} 0x{e.match:08x}")
        lines.append(f"#define MASK_{u}  0x{e.mask:08x}")
    lines.append("// ===== End fused encoding defines =====")
    lines.append("")
    return "\n".join(lines)


def generate_encoding_h_declares(encodings: list[FusedEncoding]) -> str:
    """Generate DECLARE_INSN lines for encoding.h (#ifdef DECLARE_INSN section)."""
    lines = ["// ===== Auto-generated fused DECLARE_INSN ====="]
    for e in encodings:
        u = e.name.upper()
        lines.append(f"DECLARE_INSN({e.name}, MATCH_{u}, MASK_{u})")
    lines.append("// ===== End fused DECLARE_INSN =====")
    lines.append("")
    return "\n".join(lines)


def generate_encoding_h_block(encodings: list[FusedEncoding]) -> str:
    """Generate combined MATCH_/MASK_ + DECLARE_INSN block (for reference)."""
    return generate_encoding_h_defines(encodings) + "\n" + generate_encoding_h_declares(encodings)


def generate_opcodes_block(encodings: list[FusedEncoding]) -> str:
    """Generate opcodes file entries."""
    lines = ["# Auto-generated fused opcodes"]
    for e in encodings:
        lines.append(f"{e.name:<30s} 0x{e.match:08x} 0x{e.mask:08x}")
    return "\n".join(lines)


def generate_patch_script(encodings: list[FusedEncoding], spike_src: str = "/tmp/spike-src") -> str:
    """Generate shell script to patch and rebuild Spike from source."""
    copies = "\n".join(f'cp "$INSN_DIR/{e.name}.h" "$SPIKE_SRC/riscv/insns/{e.name}.h"' for e in encodings)
    insn_names = " ".join(e.name for e in encodings)
    marker = encodings[0].name.upper() if encodings else "FUSED"

    return textwrap.dedent(f"""\
        #!/bin/bash
        set -euo pipefail
        SPIKE_SRC="{spike_src}"
        INSN_DIR="${{1:-.}}"
        DEFINES_PATCH="${{2:-encoding_defines.h}}"
        DECLARES_PATCH="${{3:-encoding_declares.h}}"

        ENC="$SPIKE_SRC/riscv/encoding.h"
        MK="$SPIKE_SRC/riscv/riscv.mk.in"

        echo "[spike-patch] Patching Spike at $SPIKE_SRC"

        # ── 1. Copy instruction body files ──
        {copies}
        echo "[spike-patch] Copied {len(encodings)} insn files"

        # ── 2. Patch encoding.h ──
        if ! grep -q "MATCH_{marker}" "$ENC" 2>/dev/null; then
            DECL_LINE=$(grep -n "^#ifdef DECLARE_INSN" "$ENC" | head -1 | cut -d: -f1)
            if [ -n "$DECL_LINE" ]; then
                head -n $((DECL_LINE-1)) "$ENC" > /tmp/_enc.h
                echo "" >> /tmp/_enc.h
                cat "$DEFINES_PATCH" >> /tmp/_enc.h
                echo "" >> /tmp/_enc.h
                ENDIF_LINE=$(tail -n +$DECL_LINE "$ENC" | grep -n "^#endif" | head -1 | cut -d: -f1)
                ENDIF_ABS=$((DECL_LINE + ENDIF_LINE - 1))
                sed -n "${{DECL_LINE}},$((ENDIF_ABS-1))p" "$ENC" >> /tmp/_enc.h
                cat "$DECLARES_PATCH" >> /tmp/_enc.h
                tail -n +"$ENDIF_ABS" "$ENC" >> /tmp/_enc.h
                mv /tmp/_enc.h "$ENC"
                echo "[spike-patch] Patched encoding.h"
            fi
        else
            echo "[spike-patch] encoding.h already patched"
        fi

        # ── 3. riscv.mk.in is patched by Python before this script runs ──
        if grep -q "riscv_insn_ext_fused" "$MK" 2>/dev/null; then
            echo "[spike-patch] riscv.mk.in already patched (by Python)"
        else
            echo "[spike-patch] WARNING: riscv.mk.in not patched"
        fi

        # ── 4. Generate .cc wrappers + rebuild ──
        echo "[spike-patch] Rebuilding Spike..."
        cd "$SPIKE_SRC"
        if [ ! -d build ]; then
            mkdir -p build && cd build
            ../configure --prefix=/opt/spike 2>&1 | tail -3
        else
            cd build
        fi

        for insn in {insn_names}; do
            UPPER=$(echo "$insn" | tr 'a-z' 'A-Z')
            if [ ! -f "${{insn}}.cc" ] || [ "$INSN_DIR/${{insn}}.h" -nt "${{insn}}.cc" ]; then
                sed "s/NAME/${{insn}}/" ../riscv/insn_template.cc | sed "s/OPCODE/ MATCH_${{UPPER}}/" > "${{insn}}.cc"
            fi
        done
        echo "[spike-patch] Generated .cc wrappers"

        rm -f insn_list.h libriscv.a
        make -j$(nproc 2>/dev/null||echo 4) 2>&1 | tail -5

        echo "[spike-patch] Done: $(pwd)/spike"
    """)


def generate_native_spike_files(fused_ops: list, output_dir: str) -> Optional[SpikeNativeFiles]:
    """Generate all files for native Spike rebuild with fused instructions.

    Deduplicates by encoding match value.  For R4-type instructions,
    funct7[6:2] = rs3 register index, so multiple binary entries map to the
    same logical instruction (same funct3 + funct2).  Native Spike definitions
    only need ONE insn/*.h per logical pattern.
    """
    os.makedirs(output_dir, exist_ok=True)
    encodings: list[FusedEncoding] = []
    seen_keys: set[tuple[int, int, int]] = set()

    for i, op in enumerate(fused_ops):
        mnemonics = getattr(op, "mnemonics", ())
        if not mnemonics:
            continue

        is_r4 = getattr(op, "n_inputs", 2) >= 3
        f3 = getattr(op, "funct3", 0)
        f7 = getattr(op, "funct7", i)
        opc = getattr(op, "opcode", CUSTOM_0_OPCODE)

        if is_r4:
            f2 = f7 & 0x3
            dedup_key = (opc, f3, f2)
        else:
            f2 = 0
            dedup_key = (opc, f3, f7)

        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        name = getattr(op, "name", None) or ("fused_" + "_".join(str(m) for m in mnemonics))
        name = re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()
        base_name = name
        suffix = 0
        while name in {e.name for e in encodings}:
            suffix += 1
            name = f"{base_name}_{suffix}"

        hc_imms = getattr(op, "imm_values", {})
        is_reverse = getattr(op, "is_reverse", False)
        sv_expr = getattr(op, "sv_expression", None)
        body = _gen_native_body(name, list(mnemonics), is_r4, hc_imms if hc_imms else None, is_reverse, sv_expr)

        enc = FusedEncoding(
            name=name,
            index=len(encodings),
            is_r4=is_r4,
            opcode=opc,
            funct3=f3,
            funct7=f7,
            funct2=f2,
            c_body=body,
            constituents=list(mnemonics),
        )
        encodings.append(enc)

    if not encodings:
        print("  No instructions to generate")
        return None

    result = SpikeNativeFiles(encodings=encodings)

    insn_dir = os.path.join(output_dir, "insns")
    os.makedirs(insn_dir, exist_ok=True)
    for enc in encodings:
        content = generate_insn_file(enc)
        result.insn_files[f"{enc.name}.h"] = content
        Path(os.path.join(insn_dir, f"{enc.name}.h")).write_text(content)

    result.encoding_h_block = generate_encoding_h_block(encodings)
    result.encoding_h_block = generate_encoding_h_block(encodings)
    Path(os.path.join(output_dir, "encoding_defines.h")).write_text(generate_encoding_h_defines(encodings))
    Path(os.path.join(output_dir, "encoding_declares.h")).write_text(generate_encoding_h_declares(encodings))
    Path(os.path.join(output_dir, "encoding_patch.h")).write_text(result.encoding_h_block)

    result.opcodes_block = generate_opcodes_block(encodings)
    Path(os.path.join(output_dir, "opcodes_patch")).write_text(result.opcodes_block)

    result.patch_script = generate_patch_script(encodings)
    script_path = os.path.join(output_dir, "patch_spike.sh")
    Path(script_path).write_text(result.patch_script)
    os.chmod(script_path, 0o755)

    print(f"  Generated native Spike files: {len(encodings)} instructions")
    print(f"    insns/: {len(result.insn_files)} files")
    print("    encoding_patch.h, opcodes_patch, patch_spike.sh")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Approach B — RoCC extension (legacy, kept for backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class SpikeCustomInsn:
    """A custom instruction entry for RoCC Spike dispatch."""

    opcode: int
    funct3: int
    funct7: int
    is_r4: bool
    c_expression: str
    name: str
    description: str


def _build_c_expression(
    mnemonics: Tuple[str, ...],
    imm_values: Dict[int, int],
) -> str:
    """Build a C expression from chained mnemonics for RoCC extension."""
    _RUNTIME_B_PORTS = ["xs2", "rs3_val"]
    resolved: List[str] = []
    b_sources: List[str] = []
    runtime_b_idx = 0

    for i, mnem in enumerate(mnemonics):
        base_mnem = _normalize_mnemonic(mnem)
        imm_val = imm_values.get(i)
        if imm_val is not None and (base_mnem, imm_val) in IMM_FOLDS_C:
            resolved.append(IMM_FOLDS_C[(base_mnem, imm_val)])
            b_sources.append("xs2")
        else:
            resolved.append(base_mnem)
            c_info = MNEMONIC_TO_C.get(base_mnem, {})
            if imm_val is not None:
                c_lit = f"0x{imm_val & 0xFFFFFFFF:08X}U" if imm_val < 0 else f"{imm_val}U"
                b_sources.append(c_lit)
            elif c_info.get("unary"):
                b_sources.append("xs2")
            else:
                if runtime_b_idx < len(_RUNTIME_B_PORTS):
                    b_sources.append(_RUNTIME_B_PORTS[runtime_b_idx])
                    runtime_b_idx += 1
                else:
                    raise ValueError(f"Fusion {' -> '.join(mnemonics)} needs too many inputs")

    return _chain_ops_c(resolved, b_sources)


def _chain_ops_c(ops: List[str], b_sources: List[str]) -> str:
    infos = [MNEMONIC_TO_C.get(op) for op in ops]
    for idx, info in enumerate(infos):
        if info is None:
            raise ValueError(f"Unknown mnemonic for C codegen: {ops[idx]}")
    result = str(infos[0]["expr"]).format(a="xs1", b=b_sources[0])
    for i in range(1, len(ops)):
        result = str(infos[i]["expr"]).format(a=result, b=b_sources[i])
    return result


def _build_spike_insn_from_fused_op(op) -> SpikeCustomInsn:
    c_expr = _build_c_expression(op.mnemonics, op.imm_values)
    return SpikeCustomInsn(
        opcode=op.opcode,
        funct3=op.funct3,
        funct7=op.funct7,
        is_r4=(op.n_inputs >= 3),
        c_expression=c_expr,
        name=op.name,
        description=op.description or f"fused: {' -> '.join(op.mnemonics)}",
    )


def generate_spike_extension(
    fused_ops: list,
    output_dir: str,
    extension_name: str = "fused",
    original_ops: Optional[list] = None,
) -> Optional[str]:
    """Generate a Spike RoCC extension C++ source file (Approach B)."""
    os.makedirs(output_dir, exist_ok=True)
    insns: List[SpikeCustomInsn] = []
    for op in fused_ops:
        try:
            insns.append(_build_spike_insn_from_fused_op(op))
        except (ValueError, KeyError) as e:
            print(f"  Skipping {op.name} for Spike: {e}")

    if not insns:
        print("  No instructions to generate Spike extension for")
        return None

    native_ops = original_ops if original_ops is not None else fused_ops
    native_dir = os.path.join(output_dir, "native")
    native = generate_native_spike_files(native_ops, native_dir)
    if native:
        print(f"  Also generated native Spike files in {native_dir}/ ({len(native.encodings)} unique patterns)")

    custom0 = [i for i in insns if i.opcode == 0x0B]
    custom2 = [i for i in insns if i.opcode == 0x5B]
    cpp = _generate_cpp(extension_name, custom0, custom2)

    cpp_path = os.path.join(output_dir, f"spike_{extension_name}.cpp")
    Path(cpp_path).write_text(cpp)
    print(f"  Generated Spike extension: {cpp_path}")
    print(f"     {len(custom0)} CUSTOM_0 + {len(custom2)} CUSTOM_2 = {len(insns)} instructions")
    return cpp_path


def _generate_dispatch_body(insns: List[SpikeCustomInsn]) -> str:
    if not insns:
        return "    illegal_instruction(*proc);\n    return 0;"
    lines = []
    lines.append("    uint32_t funct7 = insn.funct;")
    lines.append("    uint32_t funct3 = (insn.xd << 2) | (insn.xs1 << 1) | insn.xs2;")
    lines.append("")
    has_r4 = any(i.is_r4 for i in insns)
    if has_r4:
        lines.append("    uint32_t rs3_idx = (insn.funct >> 2) & 0x1F;")
        lines.append("    reg_t rs3_val = proc->get_state()->XPR[rs3_idx];")
        lines.append("")
    lines.append("    uint32_t key = (funct3 << 7) | funct7;")
    lines.append("    switch (key) {")
    for insn in insns:
        key = (insn.funct3 << 7) | insn.funct7
        lines.append(f"      case 0x{key:04X}: {{ // {insn.description}")
        lines.append(f"        return (reg_t)(uint32_t)({insn.c_expression});")
        lines.append("      }")
    lines.append("      default:")
    lines.append("        illegal_instruction(*proc);")
    lines.append("        return 0;")
    lines.append("    }")
    return "\n".join(lines)


def _generate_cpp(
    extension_name: str,
    custom0_insns: List[SpikeCustomInsn],
    custom2_insns: List[SpikeCustomInsn],
) -> str:
    """Generate the complete C++ source for the Spike RoCC extension."""
    custom0_body = _generate_dispatch_body(custom0_insns)
    custom2_body = _generate_dispatch_body(custom2_insns)
    total = len(custom0_insns) + len(custom2_insns)

    return (
        f"// Auto-generated Spike RoCC extension for custom fused instructions.\n"
        f"// Load with: spike --extlib=./lib{extension_name}.dylib --extension={extension_name}\n"
        f"// Total: {total} custom instructions\n"
        f"//   CUSTOM_0 (0x0B): {len(custom0_insns)} instructions\n"
        f"//   CUSTOM_2 (0x5B): {len(custom2_insns)} instructions\n\n"
        f'#include "rocc.h"\n\n'
        f"class {extension_name}_t : public rocc_t {{\n"
        f"public:\n"
        f'  const char* name() const override {{ return "{extension_name}"; }}\n\n'
        f"  reg_t custom0(processor_t* proc, rocc_insn_t insn, reg_t xs1, reg_t xs2) override {{\n"
        f"    (void)proc;\n"
        f"{custom0_body}\n"
        f"  }}\n\n"
        f"  reg_t custom2(processor_t* proc, rocc_insn_t insn, reg_t xs1, reg_t xs2) override {{\n"
        f"    (void)proc;\n"
        f"{custom2_body}\n"
        f"  }}\n"
        f"}};\n\n"
        f"REGISTER_EXTENSION({extension_name}, []() -> extension_t* {{ return new {extension_name}_t; }})\n"
    )


def build_spike_extension(
    cpp_path: str,
    output_dir: str,
    extension_name: str = "fused",
) -> Optional[str]:
    """Compile the Spike extension .cpp into a shared library."""
    if not os.path.exists(cpp_path):
        print(f"  Source not found: {cpp_path}")
        return None

    spike_include = _find_spike_include()
    if not spike_include:
        print("  Spike headers not found")
        return None

    system = platform.system()
    if system == "Darwin":
        lib_name = f"lib{extension_name}.dylib"
        shared_flag = "-dynamiclib"
    else:
        lib_name = f"lib{extension_name}.so"
        shared_flag = "-shared"

    lib_path = os.path.join(output_dir, lib_name)
    cxx = _find_cxx()
    if not cxx:
        print("  No C++ compiler found")
        return None

    spike_lib = _find_spike_lib()

    csrs_ext = os.path.join(spike_include, "csrs_ext.h")
    stub_created = False
    if not os.path.exists(csrs_ext):
        stub_path = os.path.join(output_dir, "csrs_ext.h")
        if not os.path.exists(stub_path):
            Path(stub_path).write_text(
                "// Auto-generated stub for OpenHW Spike fork\n"
                "#pragma once\n#include <cstdint>\n"
                "typedef uint64_t reg_t;\n"
                "namespace openhw {\n"
                "  class reg {\n  public:\n"
                "    virtual ~reg() {}\n"
                "    virtual reg_t read() const noexcept { return 0; }\n"
                "  };\n}\n"
            )
        stub_created = True

    spike_parent = os.path.dirname(spike_include)
    cmd = [
        cxx,
        "-std=c++17",
        "-O2",
        "-fPIC",
        shared_flag,
        f"-I{spike_include}",
        f"-I{spike_parent}",
    ]
    if stub_created:
        cmd.append(f"-I{output_dir}")
    if spike_lib:
        for lf in ["libriscv.so", "libriscv.dylib"]:
            full = os.path.join(spike_lib, lf)
            if os.path.exists(full):
                cmd.append(full)
                if system != "Darwin":
                    cmd.append(f"-Wl,-rpath,{spike_lib}")
                break
        else:
            cmd.extend([f"-L{spike_lib}", "-lriscv"])
    elif system == "Darwin":
        cmd.extend(["-undefined", "dynamic_lookup"])
    cmd.extend(["-o", lib_path, cpp_path])

    print(f"  Building: {' '.join(cmd[:6])} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print("  Compilation failed:")
            for line in result.stderr.strip().splitlines()[-10:]:
                print(f"     {line}")
            return None
        print(f"  Built: {lib_path} ({os.path.getsize(lib_path):,} bytes)")
        return lib_path
    except subprocess.TimeoutExpired:
        print("  Compilation timed out")
        return None
    except FileNotFoundError:
        print(f"  Compiler not found: {cxx}")
        return None


def _find_spike_include() -> Optional[str]:
    """Find Spike header directory containing rocc.h."""
    candidates = [
        "/opt/homebrew/Cellar/riscv-isa-sim/main/include/riscv",
        "/opt/homebrew/include/riscv",
        "/usr/local/include/riscv",
        "/usr/include/riscv",
    ]
    try:
        result = subprocess.run(
            ["brew", "--prefix", "riscv-isa-sim"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            candidates.insert(0, os.path.join(result.stdout.strip(), "include", "riscv"))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    for p in candidates:
        if os.path.exists(os.path.join(p, "rocc.h")):
            return p
    return None


def _find_spike_lib() -> Optional[str]:
    """Find Spike library directory."""
    candidates = [
        "/opt/homebrew/Cellar/riscv-isa-sim/main/lib",
        "/opt/homebrew/lib",
        "/usr/local/lib",
        "/usr/lib",
    ]
    try:
        result = subprocess.run(
            ["brew", "--prefix", "riscv-isa-sim"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            candidates.insert(0, os.path.join(result.stdout.strip(), "lib"))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    for p in candidates:
        for lib in ["libriscv.dylib", "libriscv.so", "libriscv.a"]:
            if os.path.exists(os.path.join(p, lib)):
                return p
    return None


def _find_cxx() -> Optional[str]:
    """Find a C++ compiler."""
    for cxx in ["g++", "clang++", "c++"]:
        if shutil.which(cxx):
            return cxx
    return None
