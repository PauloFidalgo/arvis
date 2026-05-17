"""Complete exhaustive 2-gram ALU fusion patterns for GCC .md files.

Generates ALL valid 2-gram ALU patterns including:
- 11 ALU ops: add, sub, and, or, xor, sll, srl, sra, slt, sltu, mul
- 4 operand variants: RR+RR, RI+RR, RR+RI, RI+RI
- 2 chain positions for non-commutative 2nd ops (normal + reverse)

Total: 485 patterns.

Encoding partition (R4 and R share bit positions, so funct3 partitions):
  funct3 0-3: R4-type (64 slots across 4 opcodes)
  funct3 4-7: R-type  (2048 slots across 4 opcodes)

R4 needs 186 slots but only 64 available. Overflow R4 patterns use
R-type with match_dup (3rd register = rs1 reuse).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Operation classification for GCC combine-pass compatibility
# ---------------------------------------------------------------------------

# Map mnemonics to abstract op-classes used by the combine classifier
_OP_CLASS: dict[str, str] = {
    "add": "add",
    "addi": "add",
    "sub": "sub",
    "and": "and",
    "andi": "and",
    "or": "or",
    "ori": "or",
    "xor": "xor",
    "xori": "xor",
    "sll": "shift",
    "slli": "shift",
    "srl": "shift",
    "srli": "shift",
    "sra": "shift",
    "srai": "shift",
}

# GCC combine-pass merge matrix.
# Key = (inner_class, outer_class) — inner executes first, outer wraps it.
# Value = True means combine merges them (Cat 1 → define_insn),
#         False means combine cannot merge (Cat 2 → define_peephole2).
_COMBINE_MERGES: dict[tuple[str, str], bool] = {
    # OUTER=add/sub wraps anything → True (Cat 1)
    ("shift", "add"): True,
    ("shift", "sub"): True,
    ("add", "add"): True,
    ("add", "sub"): True,
    ("sub", "add"): True,
    ("sub", "sub"): True,
    ("and", "add"): True,
    ("and", "sub"): True,
    ("or", "add"): True,
    ("or", "sub"): True,
    ("xor", "add"): True,
    ("xor", "sub"): True,
    # OUTER=shift wraps shift → True (Cat 1*)
    ("shift", "shift"): True,
    # OUTER=and/or/xor wraps shift → True (Cat 1)
    ("shift", "and"): True,
    ("shift", "or"): True,
    ("shift", "xor"): True,
    # OUTER=and/or/xor wraps and/or/xor → True (Cat 1)
    ("and", "and"): True,
    ("and", "or"): True,
    ("and", "xor"): True,
    ("or", "and"): True,
    ("or", "or"): True,
    ("or", "xor"): True,
    ("xor", "and"): True,
    ("xor", "or"): True,
    ("xor", "xor"): True,
    # OUTER=shift wraps add/sub → False (Cat 2)
    ("add", "shift"): False,
    ("sub", "shift"): False,
    # OUTER=and/or/xor wraps add/sub → False (Cat 2)
    ("add", "and"): False,
    ("add", "or"): False,
    ("add", "xor"): False,
    ("sub", "and"): False,
    ("sub", "or"): False,
    ("sub", "xor"): False,
    # OUTER=shift wraps and/or/xor → False (Cat 2)
    ("and", "shift"): False,
    ("or", "shift"): False,
    ("xor", "shift"): False,
}


ALU_OPS: list[str] = [
    "add",
    "sub",
    "and",
    "or",
    "xor",
    "sll",
    "srl",
    "sra",
    "slt",
    "sltu",
    "mul",
]
NON_COMM: set[str] = {"sub", "sll", "srl", "sra", "slt", "sltu"}
IMM_OPS: set[str] = {"add", "and", "or", "xor", "sll", "srl", "sra"}

_RTL: dict[str, str] = {
    "add": "plus",
    "sub": "minus",
    "and": "and",
    "or": "ior",
    "xor": "xor",
    "sll": "ashift",
    "srl": "lshiftrt",
    "sra": "ashiftrt",
    "slt": "lt",
    "sltu": "ltu",
    "mul": "mult",
}

# Extended RTL map including U-type instructions for HC pattern generation.
# lui is not in ALU_OPS (it's U-type, not RR) but can appear in HC patterns
# when the lui immediate is constant (e.g., lui+add for address computation).
_RTL_EXTENDED: dict[str, str] = {
    **_RTL,
    "lui": "ashift",  # lui rd,imm = rd = imm << 12 (for HC patterns only)
}

_OPCODES: list[int] = [0x0B, 0x2B, 0x5B, 0x7B]
_R4_SLOTS_PER_OPCODE = 8 * 4


def _rtl(op: str, a: str, b: str) -> str:
    r = _RTL[op]
    if op in ("slt", "sltu"):
        return f"({r}:SI {a} {b})"
    return f"({r}:SI {a} {b})"


def _reg(i: int, c: str = "r") -> str:
    return f'(match_operand:SI {i} "register_operand" "{c}")'


def _r4_enc(slot: int) -> tuple[int, int, int]:
    """R4 encoding: funct3 0-7, 32 slots per opcode, 128 total."""
    oi = slot // _R4_SLOTS_PER_OPCODE
    loc = slot % _R4_SLOTS_PER_OPCODE
    if oi >= len(_OPCODES):
        raise ValueError(f"R4 slot {slot} overflow (max {len(_OPCODES) * _R4_SLOTS_PER_OPCODE})")
    return _OPCODES[oi], loc // 4, loc % 4


def _r_enc(slot: int) -> tuple[int, int, int]:
    """R encoding: funct3 0-7, 128 funct7 per funct3, 4 opcodes."""
    per_opc = 8 * 128
    oi = slot // per_opc
    loc = slot % per_opc
    if oi >= len(_OPCODES):
        raise ValueError(f"R slot {slot} overflow")
    return _OPCODES[oi], loc // 128, loc % 128


def _emit(lines: list, name: str, comment: str, rtl_body: str, asm: str) -> None:
    lines.append(f";; {comment}")
    lines.append(f'(define_insn "{name}"')
    lines.append(f"  [(set {_reg(0, '=r')}\n        {rtl_body})]")
    lines.append('  "TARGET_CUSTOM_FUSED"')
    lines.append(f'  "{asm}"')
    lines.append('  [(set_attr "type" "arith")])')
    lines.append("")


# "Easy" single-cycle ops suitable for 3-gram fusion
_EASY_OPS: list[str] = ["add", "sub", "and", "or", "xor", "sll", "srl", "sra"]


def _detect_canonical_form(
    ma: str,
    mb: str,
    hc_map: dict[int, int],
) -> tuple[str, int] | None:
    """Detect if a 2-gram with hardcoded immediates has a GCC canonical form.

    GCC's combine pass (combine.cc) and simplify-rtx.cc apply
    canonicalizations that rewrite certain RTL patterns into
    different, equivalent forms.  Our define_insn patterns MUST
    match the canonical form, otherwise the GCC instruction
    selector will never match them.

    Returns (canonical_rtl_body, n_register_operands) or None if
    no special canonical form applies (use naive composition).

    The canonical_rtl_body uses match_operand placeholders:
      operand 0 = output (rd)
      operand 1 = first register input (rs1)
      operand 2 = second register input (rs2, if needed)
    """
    _IMM_TO_BASE = {
        "addi": "add",
        "andi": "and",
        "ori": "or",
        "xori": "xor",
        "slli": "sll",
        "srli": "srl",
        "srai": "sra",
    }
    ma_base = _IMM_TO_BASE.get(ma, ma)
    mb_base = _IMM_TO_BASE.get(mb, mb)

    # ── slli(N) + srai(N): sign-extend ──
    # GCC canonical: (sign_extend:SI (subreg:HI x 0)) for N=16
    #                (sign_extend:SI (subreg:QI x 0)) for N=24
    if ma_base == "sll" and mb_base == "sra":
        shift_l = hc_map.get(0)
        shift_r = hc_map.get(1)
        if shift_l is not None and shift_r is not None and shift_l == shift_r:
            if shift_l == 16:
                rtl = '(sign_extend:SI (subreg:HI (match_operand:SI 1 "register_operand" "r") 0))'
                return rtl, 1
            elif shift_l == 24:
                rtl = '(sign_extend:SI (subreg:QI (match_operand:SI 1 "register_operand" "r") 0))'
                return rtl, 1
            # Other shift amounts stay as naive ashift+ashiftrt
            # (GCC does NOT canonicalize arbitrary slli+srai pairs)

    # ── slli(N) + srli(N): zero-extend ──
    # GCC canonical: (zero_extend:SI (subreg:HI x 0)) for N=16
    #                (zero_extend:SI (subreg:QI x 0)) for N=24
    if ma_base == "sll" and mb_base == "srl":
        shift_l = hc_map.get(0)
        shift_r = hc_map.get(1)
        if shift_l is not None and shift_r is not None and shift_l == shift_r:
            if shift_l == 16:
                rtl = '(zero_extend:SI (subreg:HI (match_operand:SI 1 "register_operand" "r") 0))'
                return rtl, 1
            elif shift_l == 24:
                rtl = '(zero_extend:SI (subreg:QI (match_operand:SI 1 "register_operand" "r") 0))'
                return rtl, 1
        # slli(N) + srli(M) where M > N: bit-field extract
        # GCC canonical: (and:SI (lshiftrt:SI x (M-N)) (const_int mask))
        # where mask = (1 << (32-M)) - 1
        if shift_l is not None and shift_r is not None and shift_r > shift_l:
            net_shift = shift_r - shift_l
            mask = (1 << (32 - shift_r)) - 1
            rtl = (
                f"(and:SI (lshiftrt:SI "
                f'(match_operand:SI 1 "register_operand" "r") '
                f"(const_int {net_shift})) "
                f"(const_int {mask}))"
            )
            return rtl, 1

    # ── xori(-1): bitwise NOT ──
    # GCC canonical: (not:SI x) — always applied
    if ma_base == "xor" and hc_map.get(0) == -1:
        # xori(-1) + op_b  →  (op_b (not:SI x) ...)
        not_expr = '(not:SI (match_operand:SI 1 "register_operand" "r"))'
        if 1 in hc_map:
            # xori(-1) + op_b(const): both immediates hardcoded
            outer = _rtl(mb_base, not_expr, f"(const_int {hc_map[1]})")
            return outer, 1
        else:
            # xori(-1) + op_b(reg): e.g., ANDN pattern
            outer = _rtl(mb_base, not_expr, '(match_operand:SI 2 "register_operand" "r")')
            return outer, 2

    # ── addi(0): no-op add — GCC eliminates this ──
    # (plus:SI x (const_int 0)) is simplified to x by GCC.
    # Patterns with addi(0) as the first op will never match.
    if ma_base == "add" and hc_map.get(0) == 0:
        # Skip this pattern — GCC will never emit it
        return None, 0  # type: ignore[return-value]

    # ── Phantom pattern: slli(16) as component ──
    # GCC's expand pass converts C type casts like (int16_t)x directly
    # to (sign_extend:SI (subreg:HI x 0)), which the built-in
    # *extendhisi2 pattern matches.  The assembly sequence slli(16) +
    # srai(16) is the hardware implementation, but GCC's RTL never
    # contains (ashift:SI x (const_int 16)) — verified: 0 occurrences
    # in the 80K-line LTO combine dump.  Therefore ANY pattern
    # containing slli(16) or srai(16) as a standalone component
    # (not already handled by sign_extend canonical above) is phantom.
    #
    # Similarly, slli(24)+srai(24) → sign_extend QI is handled above.
    # Patterns like mul+slli(16), add+slli(16), and+slli(16) are
    # phantom because the slli(16) never exists as a separate RTL insn.
    if hc_map.get(1) == 16 and mb_base == "sll":
        # e.g., add+slli(16), mul+slli(16), and+slli(16)
        # The slli(16) is consumed by sign_extend in expand, not available
        return None, 0  # type: ignore[return-value]

    if hc_map.get(0) == 16 and ma_base == "sra":
        # e.g., srai(16)+sub, srai(16)+mul — the srai(16) is part of
        # sign_extend, not a standalone shift
        return None, 0  # type: ignore[return-value]

    # ── sub(x, const) → plus(x, -const) ──
    # GCC canonicalizes subtraction of a constant to addition of
    # its negation.  However, sub with TWO registers stays as minus.
    # This only matters if the second op is addi (sub + addi):
    # the result is (plus:SI (minus:SI a b) (const_int N)) which
    # GCC does NOT further simplify.  So no change needed here.

    # ── lui(IMM) + op_b: constant upper-immediate + register op ──
    # lui loads a 20-bit immediate shifted left by 12.  When the lui
    # immediate is hardcoded, the combined pattern is:
    #   (op_b (const_int (IMM << 12)) register)
    # GCC's combine pass naturally produces this form.
    if ma_base == "lui" and 0 in hc_map:
        lui_val = hc_map[0] << 12  # lui immediate is upper 20 bits
        # Handle sign extension for negative lui values
        if lui_val >= 0x80000000:
            lui_val -= 0x100000000
        const_expr = f"(const_int {lui_val})"
        if 1 in hc_map:
            # lui(IMM) + op_b(const): both hardcoded → single constant
            # GCC will constant-fold this, skip
            return None, 0  # type: ignore[return-value]
        else:
            # lui(IMM) + op_b(reg): e.g., lui+add for address computation
            reg_expr = '(match_operand:SI 1 "register_operand" "r")'
            outer = _rtl(mb_base, const_expr, reg_expr)
            return outer, 1

    # No special canonical form detected — use naive composition
    return None


def _generate_hardcoded_imm_patterns(
    lines: list[str],
    candidates: "Sequence | None",
    r_slot_start: int,
) -> tuple[int, int]:
    """Generate specialized patterns for candidates with hardcoded immediates.

    When analysis finds that an n-gram ALWAYS uses the same immediate value
    (e.g., ``slli rd,rs1,16`` + ``add rd,rd,rs2`` — where the shift is
    always 16), we emit a GCC pattern with ``(const_int 16)`` instead of
    ``(match_operand ... "const_int_operand" "i")``.

    **GCC Canonical Form Mapping**: Before emitting naive RTL composition,
    this function checks ``_detect_canonical_form()`` which maps specific
    mnemonic+immediate combinations to the canonical RTL that GCC's
    combine pass actually produces.  Key canonicalizations:

    - ``slli N + srai N`` (N=16) → ``(sign_extend:SI (subreg:HI x 0))``
    - ``slli N + srai N`` (N=24) → ``(sign_extend:SI (subreg:QI x 0))``
    - ``slli N + srli N`` (N=16) → ``(zero_extend:SI (subreg:HI x 0))``
    - ``slli N + srli N`` (N=24) → ``(zero_extend:SI (subreg:QI x 0))``
    - ``slli N + srli M`` (M>N)  → ``(and:SI (lshiftrt:SI x (M-N)) mask)``
    - ``xori(-1) + and``         → ``(and:SI (not:SI x) y)`` (ANDN)
    - ``xori(-1) + any``         → ``(OP (not:SI x) ...)``
    - ``addi(0) + any``          → SKIPPED (GCC eliminates add-zero)

    This means:
    - GCC matches ONLY when the immediate equals the hardcoded value
    - The ``.insn r`` encoding carries just rs1+rs2 (R-type, 2 regs)
    - The SV ALU expression can hardcode the literal: ``(a << 5'd16) + b``
    - No operand_c_i needed → works correctly with R-type hardware

    Each unique (mnemonic_pair, imm_position, imm_value) gets its own
    encoding slot and define_insn pattern.

    Returns (next_r_slot, patterns_added).
    """
    if not candidates:
        return r_slot_start, 0

    r_slot = r_slot_start
    count = 0
    seen: set[tuple[tuple[str, ...], tuple[tuple[int, int], ...]]] = set()

    lines.append("")
    lines.append(";; ── Section: Hardcoded-immediate specialized patterns ──")
    lines.append(";; These match ONLY when the immediate equals the specific value.")
    lines.append(";; Allows R-type encoding (2 regs) with hardcoded literal in SV.")
    lines.append(";; RTL uses GCC canonical forms (sign_extend, zero_extend, not, etc.)")
    lines.append("")

    for cand in candidates:
        pattern = getattr(cand, "pattern", None)
        imm_hc = getattr(cand, "imm_should_hardcode", None)
        imm_vals = getattr(cand, "hardcoded_imm_values", None)

        if not pattern or not imm_hc or not imm_vals:
            continue
        if len(pattern) < 2:
            continue

        # Collect hardcoded immediates: list of (position, value)
        hc_entries: list[tuple[int, int]] = []
        for i, (should_hc, val) in enumerate(zip(imm_hc, imm_vals)):
            if should_hc and val is not None:
                hc_entries.append((i, val))

        if not hc_entries:
            continue

        # Normalize mnemonics
        from arvis.analysis.registers import normalize_mnemonic

        mnems = tuple(normalize_mnemonic(m) for m in pattern)

        # Only handle 2-grams for now (3-grams would need R4 or more complex encoding)
        if len(mnems) != 2:
            continue

        # Build dedup key: (mnemonics, tuple of (position, value) pairs)
        dedup_key = (mnems, tuple(sorted(hc_entries)))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Check that the base ops are in our ALU ops list
        base_ops = []
        for m in mnems:
            # Strip trailing 'i' to get base op (addi->add, slli->sll)
            base = m.rstrip("i") if m.endswith("i") and m not in ("mulhi",) else m
            if base not in _RTL_EXTENDED:
                # Try with the 'i' suffix
                if m not in _RTL_EXTENDED:
                    break
                base = m
            base_ops.append(m)

        if len(base_ops) != len(mnems):
            continue

        # Map immediate mnemonics to base ops for _RTL lookup
        _IMM_TO_BASE = {
            "addi": "add",
            "andi": "and",
            "ori": "or",
            "xori": "xor",
            "slli": "sll",
            "srli": "srl",
            "srai": "sra",
            "slti": "slt",
            "sltiu": "sltu",
        }
        ma, mb = mnems
        ma_base = _IMM_TO_BASE.get(ma, ma)
        mb_base = _IMM_TO_BASE.get(mb, mb)
        if ma_base not in _RTL_EXTENDED or mb_base not in _RTL_EXTENDED:
            continue

        hc_positions = {pos for pos, _ in hc_entries}
        hc_map = {pos: val for pos, val in hc_entries}

        # ── Try GCC canonical form first ──
        canonical = _detect_canonical_form(ma, mb, hc_map)

        if canonical is not None:
            canonical_rtl, canonical_n_regs = canonical
            if canonical_n_regs == 0:
                # Pattern should be skipped (e.g., addi(0)+add is dead)
                logger.debug("Skipping dead pattern %s+%s (canonical eliminates it)", ma, mb)
                continue

            # Build encoding
            try:
                opc, f3, f7 = _r_enc(r_slot)
            except ValueError:
                break

            def _val_str(v: int) -> str:
                return f"neg{abs(v)}" if v < 0 else str(v)

            imm_desc = "_".join(f"{pos}eq{_val_str(val)}" for pos, val in sorted(hc_entries))
            name = f"fused_hc_{ma}_{mb}_{imm_desc}"

            # ASM template based on register count
            if canonical_n_regs == 1:
                asm = f".insn r 0x{opc:02x}, {f3}, 0x{f7:02x}, %0, %1, zero"
            else:
                asm = f".insn r 0x{opc:02x}, {f3}, 0x{f7:02x}, %0, %1, %2"

            freq = getattr(cand, "frequency", 0)
            imm_str = ", ".join(f"pos{p}={v}" for p, v in sorted(hc_entries))
            comment = f"{ma}+{mb} hardcoded({imm_str}) freq={freq} [canonical]"
            _emit(lines, name, comment, canonical_rtl, asm)
            r_slot += 1
            count += 1
            continue

        # ── Naive composition (no special canonical form) ──
        # Determine how many register operands after hardcoding
        n_regs = 1  # rs1 is always a register

        if 0 not in hc_positions:
            n_regs += 1  # op_a's second operand is a register
        if 1 not in hc_positions:
            n_regs += 1  # op_b's second operand is a register

        if n_regs > 2:
            continue  # Still needs 3+ regs, can't do R-type

        # Build RTL tree
        reg_idx = 1  # operand 0 is output
        src1 = _reg(reg_idx)
        reg_idx += 1

        if 0 in hc_map:
            src2 = f"(const_int {hc_map[0]})"
        else:
            src2 = _reg(reg_idx) if n_regs > 1 else _reg(reg_idx)
            reg_idx += 1

        inner = _rtl(ma_base, src1, src2)

        if 1 in hc_map:
            outer_src = f"(const_int {hc_map[1]})"
        else:
            outer_src = _reg(reg_idx)
            reg_idx += 1

        outer = _rtl(mb_base, inner, outer_src)

        # Build encoding
        try:
            opc, f3, f7 = _r_enc(r_slot)
        except ValueError:
            break  # Encoding space exhausted

        # Build pattern name (must be valid C identifier — no minus signs)
        def _val_str(v: int) -> str:
            return f"neg{abs(v)}" if v < 0 else str(v)

        imm_desc = "_".join(f"{pos}eq{_val_str(val)}" for pos, val in sorted(hc_entries))
        name = f"fused_hc_{ma}_{mb}_{imm_desc}"

        # Build asm template — only register operands in .insn r
        reg_ops = [0]  # rd
        for idx in range(1, reg_idx):
            reg_ops.append(idx)

        if len(reg_ops) == 2:
            asm = f".insn r 0x{opc:02x}, {f3}, 0x{f7:02x}, %0, %{reg_ops[1]}, zero"
        elif len(reg_ops) == 3:
            asm = f".insn r 0x{opc:02x}, {f3}, 0x{f7:02x}, %0, %{reg_ops[1]}, %{reg_ops[2]}"
        else:
            asm = f".insn r 0x{opc:02x}, {f3}, 0x{f7:02x}, %0, %{reg_ops[1]}, zero"

        freq = getattr(cand, "frequency", 0)
        imm_str = ", ".join(f"pos{p}={v}" for p, v in sorted(hc_entries))
        comment = f"{ma}+{mb} hardcoded({imm_str}) freq={freq}"
        _emit(lines, name, comment, outer, asm)
        r_slot += 1
        count += 1

        # NOTE: No _rev variants for HC patterns.
        # Reverse puts const_int in the first operand position of
        # non-commutative ops (e.g., ashiftrt(const_int 16, expr))
        # which is semantically nonsensical and never appears in
        # GCC's internal RTL representation.

    return r_slot, count


# ═══════════════════════════════════════════════════════════════════════════
# Parametric immediate patterns for GCC .md
# ═══════════════════════════════════════════════════════════════════════════

# Mnemonics that take an immediate (I-type)
_I_TYPE = {
    "sll": "slli",
    "srl": "srli",
    "sra": "srai",
    "add": "addi",
    "and": "andi",
    "or": "ori",
    "xor": "xori",
}


def generate_parametric_imm_patterns(
    patterns: list[tuple[tuple[str, ...], int]],
    r4_slot_start: int,
) -> tuple[list[str], int, int]:
    """Generate parametric-immediate .md patterns for GCC.

    Each pattern uses const_int_operand for the immediate, with a range
    check (0-31) in the condition. The .insn output encodes the immediate
    in the rs3 field as x%cN.

    Args:
        patterns: list of ((mnem1, mnem2, ...), match_count)
        r4_slot_start: first R4 encoding slot to use

    Returns:
        (lines, r4_slot_after, n_patterns)
    """
    lines = []
    r4_slot = r4_slot_start
    count = 0

    _IMM_BASE = {
        "addi": "add",
        "andi": "and",
        "ori": "or",
        "xori": "xor",
        "slli": "sll",
        "srli": "srl",
        "srai": "sra",
        "slti": "slt",
        "sltiu": "sltu",
    }

    for mnems, match_count in patterns:
        if len(mnems) < 2:
            continue

        # Normalize to base mnemonics
        norm = tuple(_IMM_BASE.get(m, m) for m in mnems)

        # Determine which positions have immediates
        imm_positions = []
        for i, m in enumerate(mnems):
            if m in _IMM_BASE:  # I-type mnemonic
                imm_positions.append(i)

        if not imm_positions:
            continue  # no immediates, skip (RR+RR handled elsewhere)

        # Build operand list: reg operands + const_int operands
        # Operand 0 = output (rd)
        # Then register inputs, then const_int inputs
        imm_idx_map = {}  # position -> operand index
        reg_idx_map = {}  # position -> operand index for the "other" reg input

        # For a 2-gram like slli+add: op0=rd, op1=rs1(A), op2=imm(A), op3=rs2(B)
        # For a 2-gram like add+slli: op0=rd, op1=rs1(A), op2=rs2(A), op3=imm(B)
        # For a 2-gram like slli+srai: op0=rd, op1=rs1(A), op2=imm(A), op3=imm(B)

        operands = []  # (type, idx) where type is 'reg' or 'imm'
        op_idx = 1
        for i, m in enumerate(norm):
            if m in _RTL:
                # This op needs inputs
                if i in imm_positions:
                    # I-type: 1 reg + 1 imm
                    if i == 0:
                        reg_idx_map[i] = op_idx
                        operands.append(("reg", op_idx))
                        op_idx += 1
                    imm_idx_map[i] = op_idx
                    operands.append(("imm", op_idx))
                    op_idx += 1
                else:
                    # R-type: 2 regs (but one is the chain from previous)
                    if i == 0:
                        reg_idx_map[i] = op_idx
                        operands.append(("reg", op_idx))
                        op_idx += 1
                        reg_idx_map[f"{i}b"] = op_idx
                        operands.append(("reg", op_idx))
                        op_idx += 1
                    else:
                        # Second op: one input is chain, other is new reg
                        reg_idx_map[i] = op_idx
                        operands.append(("reg", op_idx))
                        op_idx += 1

        # Build RTL expression (nested)
        def _build_rtl(pos, chain_expr):
            m = norm[pos]
            rtl_op = _RTL.get(m)
            if rtl_op is None:
                return None

            if pos in imm_positions:
                imm_op = imm_idx_map[pos]
                if pos == 0:
                    reg_op = reg_idx_map[pos]
                    a = f'(match_operand:SI {reg_op} "register_operand" "r")'
                else:
                    a = chain_expr
                b = f'(match_operand:SI {imm_op} "const_int_operand" "")'
            else:
                if pos == 0:
                    a = f'(match_operand:SI {reg_idx_map[pos]} "register_operand" "r")'
                    b = f'(match_operand:SI {reg_idx_map[f"{pos}b"]} "register_operand" "r")'
                else:
                    a = chain_expr
                    b = f'(match_operand:SI {reg_idx_map[pos]} "register_operand" "r")'

            # Non-commutative: order matters
            return f"({rtl_op}:SI {a} {b})"

        expr = None
        for pos in range(len(norm)):
            if pos == 0:
                expr = _build_rtl(pos, None)
            else:
                expr = _build_rtl(pos, expr)

        if expr is None:
            continue

        # Build condition: range check all immediates
        conds = ["TARGET_CUSTOM_FUSED"]
        for pos in imm_positions:
            idx = imm_idx_map[pos]
            conds.append(f"IN_RANGE(INTVAL(operands[{idx}]), 0, 31)")
        condition = " && ".join(conds)

        # Build .insn output template
        # R4 format: .insn r4 opc, f3, f2, %0, %rs1, %rs2_or_ximm, %rs3_ximm
        try:
            opc, f3, f2 = _r4_enc(r4_slot)
        except (IndexError, ValueError):
            break  # out of slots

        # Map operands to .insn fields: rd=%0, rs1, rs2, rs3
        # rs3 = first imm (as x%cN), rs2 = second imm or reg
        if len(imm_positions) == 1:
            # 1 imm: rs1=reg_input, rs2=other_reg, rs3=x%imm
            imm_op = imm_idx_map[imm_positions[0]]
            if imm_positions[0] == 0:
                # First op has imm: slli+add → rs1=%1(A.rs1), rs2=%3(B.rs2), rs3=x%c2(A.imm)
                rs1 = f"%{reg_idx_map[0]}"
                rs2 = f"%{reg_idx_map.get(1, reg_idx_map.get('0b', 1))}"
                rs3 = f"x%c{imm_op}"
            else:
                # Second op has imm: add+slli → rs1=%1(A.rs1), rs2=%2(A.rs2), rs3=x%c3(B.imm)
                rs1 = f"%{reg_idx_map.get(0, 1)}"
                rs2 = f"%{reg_idx_map.get('0b', reg_idx_map.get(0, 1))}"
                rs3 = f"x%c{imm_op}"
        elif len(imm_positions) == 2:
            # 2 imms: rs1=reg_input, rs2=x%imm1, rs3=x%imm0
            rs1 = f"%{reg_idx_map.get(0, 1)}"
            rs2 = f"x%c{imm_idx_map[imm_positions[1]]}"
            rs3 = f"x%c{imm_idx_map[imm_positions[0]]}"
        else:
            continue  # 3+ imms not supported

        asm_template = f".insn r4 0x{opc:02x}, {f3}, {f2}, %0, {rs1}, {rs2}, {rs3}"
        pat_name = "_".join(mnems) + "_param"

        # Emit define_insn
        lines.append(f";; {'+'.join(mnems)} parametric ({match_count}x)")
        lines.append(f'(define_insn "fused_{pat_name}"')
        lines.append('  [(set (match_operand:SI 0 "register_operand" "=r")')
        lines.append(f"        {expr})]")
        lines.append(f'  "{condition}"')
        lines.append(f'  "{asm_template}"')
        lines.append('  [(set_attr "type" "arith")])')
        lines.append("")

        r4_slot += 1
        count += 1

    return lines, r4_slot, count
