"""Combined fused instruction .md generator + peephole2 pattern generator.

Part 1 — 3-pass merged .md generation:
  Delegates to ``exhaustive_patterns.generate_exhaustive_md`` which produces
  all 485 exhaustive 2-gram ALU patterns.  Also provides ``generate_merged_md``
  for the 3-pass pipeline: merges used patterns from Pass 1-3 (static) +
  HC (profiling) into a single .md file with fresh encoding assignments.

Part 2 — Category 2 peephole2 patterns:
  For (op1, op2) pairs that GCC's combine pass will NOT merge (Cat 2),
  emits ``define_peephole2`` + matching ``define_insn`` with UNSPEC.
  UNSPEC numbers start at 1000 + hc_code to avoid collisions.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ===================================================================
# Part 1: Legacy combined .md generation + 3-pass merge
# ===================================================================


def generate_combined_md(
    all_fusions: list,
    output_path: str,
    max_3gram_patterns: Optional[int] = None,
) -> Tuple[str, int, int]:
    """Generate .md with R4 (RR+RR) + hardcoded-immediate patterns only.

    Only includes:
    - R4-type RR+RR patterns (3 register inputs, correct hardware)
    - Hardcoded-immediate patterns from profiling (const_int N, R-type safe)

    Does NOT include generic RI/RR/RI+RI patterns (broken for R-type).

    Returns (content, n_r4, n_hc).
    """
    from .exhaustive_patterns import _generate_hardcoded_imm_patterns

    lines: list[str] = []
    lines.append(";; === Hardcoded-Immediate patterns ONLY (testing) ===")
    lines.append("")  # placeholder for total line

    count = 0

    # ── ONLY hardcoded-immediate patterns from profiling ──
    hc_lines: list[str] = []
    r_slot = 0
    _, n_hc = _generate_hardcoded_imm_patterns(hc_lines, all_fusions, r_slot)
    if n_hc > 0:
        lines.extend(hc_lines)
        count += n_hc

    lines[1] = f";; Total: {count} patterns (HC={n_hc})"

    content = "\n".join(lines)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(content)

    print(f"\n  Combined .md: {output_path}")
    print(f"     HC patterns (const):  {n_hc}")
    print(f"     Total:                {count}")

    return content, 0, n_hc


# ---------------------------------------------------------------------------
# 3-pass merged .md generation
# ---------------------------------------------------------------------------

# R4-type encoding: 4 opcodes × 8 funct3 × 4 funct2 = 128 slots total
# hwloop reserves the LAST N slots (bounds + count per nesting level)
_OPCODES = [0x0B, 0x2B, 0x5B, 0x7B]
_R4_SLOTS_PER_OPCODE = 8 * 4  # 8 funct3 × 4 funct2
_R4_MAX = len(_OPCODES) * _R4_SLOTS_PER_OPCODE  # 128
_HWLOOP_RESERVED = 2  # bounds + count


def _r4_enc(slot: int) -> Tuple[int, int, int]:
    """R4 encoding for slot index. Returns (opcode, funct3, funct2)."""
    oi = slot // _R4_SLOTS_PER_OPCODE
    loc = slot % _R4_SLOTS_PER_OPCODE
    if oi >= len(_OPCODES):
        raise ValueError(f"R4 slot {slot} overflow (max {_R4_MAX})")
    return _OPCODES[oi], loc // 4, loc % 4


def _r_enc(slot: int) -> Tuple[int, int, int]:
    """R-type encoding for slot index. Returns (opcode, funct3, funct7)."""
    per_opc = 8 * 128
    oi = slot // per_opc
    loc = slot % per_opc
    if oi >= len(_OPCODES):
        raise ValueError(f"R-type slot {slot} overflow")
    return _OPCODES[oi], loc // 128, loc % 128


def _extract_pattern_blocks(md_content: str) -> List[Dict]:
    """Extract individual define_insn blocks from an .md file.

    Returns a list of dicts with keys:
        name: pattern name (e.g., "fused_add_sub_rr")
        comment: the ;; comment line(s) preceding the pattern
        rtl_body: the full RTL body string between set and )]
        asm_template: the .insn assembly template
        full_text: the complete define_insn block text
        is_r4: True if uses .insn r4
        encoding: (opcode, funct3, funct7_or_funct2)
    """
    patterns = []
    lines = md_content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # Look for define_insn start
        m_insn = re.match(r'\(define_insn "([^"]+)"', line)
        if not m_insn:
            i += 1
            continue

        name = m_insn.group(1)

        # Collect the comment line(s) above
        comment_lines: list[str] = []
        j = i - 1
        while j >= 0 and lines[j].startswith(";;"):
            comment_lines.insert(0, lines[j])
            j -= 1

        # Collect the full define_insn block (ends with a line containing only ")")
        block_lines = []
        while i < len(lines):
            block_lines.append(lines[i])
            # End of define_insn: line starts with ")" or contains ")])"
            if re.match(r"^\s*\[(set_attr|$)", lines[i]):
                pass
            # Look for the closing pattern: a line that is just ")" after attrs
            if lines[i].strip() == "" and block_lines and "set_attr" in block_lines[-2]:
                break
            # More robust: look for the line with set_attr ... )]
            if "set_attr" in lines[i] and lines[i].rstrip().endswith(")"):
                i += 1
                # Possibly blank line after
                break
            i += 1
        else:
            i += 1
            continue

        full_text = "\n".join(comment_lines + block_lines)

        # Extract .insn template to determine encoding
        asm_match = re.search(
            r'"(\.insn\s+r4?\s+0x([0-9a-f]+),\s*(\d+),\s*(?:0x)?([0-9a-f]+).*?)"',
            full_text,
        )
        if not asm_match:
            i += 1
            continue

        asm_template = asm_match.group(1)
        is_r4 = ".insn r4" in asm_template
        opcode = int(asm_match.group(2), 16)
        funct3 = int(asm_match.group(3))
        funct_val = int(asm_match.group(4), 16)

        # Extract the RTL body (between [(set ... and )]  "")
        rtl_match = re.search(
            r"\[\(set\s.*?\)\]",
            full_text,
            re.DOTALL,
        )
        rtl_body = rtl_match.group(0) if rtl_match else ""

        # Extract mnemonics from pattern name
        # e.g., "fused_slli_add_param" → ("slli", "add")
        # e.g., "fused_add_sub_rr_p1" → ("add", "sub")
        name_parts = name.replace("fused_", "").split("_")
        # Remove suffixes: param, rr, rev, p1, p2, p3, etc.
        _SUFFIXES = {"param", "rr", "rev", "p1", "p2", "p3", "hc"}
        mnemonics = tuple(p for p in name_parts if p not in _SUFFIXES and not p.startswith("s="))

        patterns.append(
            {
                "name": name,
                "comment": "\n".join(comment_lines),
                "full_text": full_text,
                "asm_template": asm_template,
                "rtl_body": rtl_body,
                "is_r4": is_r4,
                "encoding": (opcode, funct3, funct_val),
                "mnemonics": mnemonics,
            }
        )

        i += 1

    return patterns


def _rewrite_encoding(
    full_text: str,
    old_asm: str,
    new_asm: str,
) -> str:
    """Replace the .insn assembly template in a define_insn block."""
    return full_text.replace(f'"{old_asm}"', f'"{new_asm}"')


def generate_merged_md(
    pass_used_list: Optional[List[Tuple[str, List[Tuple[int, int, int, bool]]]]] = None,
    hc_candidates: Optional[list] = None,
    output_path: Optional[str] = None,
    parametric_patterns: Optional[list] = None,
    *,
    # Legacy 2-pass interface (backward compatibility)
    pass1_used_encodings: Optional[List[Tuple[int, int, int, bool]]] = None,
    pass2_used_encodings: Optional[List[Tuple[int, int, int, bool]]] = None,
    pass1_md_path: Optional[str] = None,
    pass2_md_path: Optional[str] = None,
) -> Tuple[str, int]:
    """Generate merged .md combining used patterns from N passes + HC + Cat2.

    Accepts either:
    - ``pass_used_list``: list of (md_path, used_encodings) for N passes
    - Legacy: ``pass1_used_encodings``/``pass2_used_encodings`` + paths

    Steps:
    1. Parses each pass's static .md file
    2. Extracts only the define_insn blocks for patterns GCC actually used
    3. Generates HC patterns from profiling candidates (Cat 1)
    4. Generates peephole2 patterns for Cat 2 HC candidates
    5. Assigns fresh sequential encodings (R4 first, then R-type)
    6. Writes the merged .md file

    Returns:
        (md_content, total_pattern_count)
    """
    from .exhaustive_patterns import (
        _generate_hardcoded_imm_patterns,
    )

    # Normalize to pass_used_list format
    if pass_used_list is None:
        pass_used_list = []
        if pass1_used_encodings is not None:
            p1_path = pass1_md_path or "tools/custom-fused-pass1.md"
            pass_used_list.append((p1_path, pass1_used_encodings))
        if pass2_used_encodings is not None:
            p2_path = pass2_md_path or "tools/custom-fused-pass2.md"
            pass_used_list.append((p2_path, pass2_used_encodings))

    # Collect used patterns from all passes
    all_pass_patterns: List[List[Dict]] = []
    for md_path, used_encodings in pass_used_list:
        enc_set: set = set()
        for opcode, funct3, funct_val, _is_r4 in used_encodings:
            enc_set.add((opcode, funct3, funct_val))

        pass_patterns: List[Dict] = []
        if Path(md_path).exists():
            md_content = Path(md_path).read_text()
            all_blocks = _extract_pattern_blocks(md_content)
            for pat in all_blocks:
                if pat["encoding"] in enc_set:
                    pass_patterns.append(pat)
            logger.info("%s: %d/%d patterns used", md_path, len(pass_patterns), len(all_blocks))
        else:
            logger.warning("Pass .md not found: %s", md_path)

        all_pass_patterns.append(pass_patterns)

    # Deduplicate by pattern name across all passes
    seen_names: set = set()
    used_patterns: List[Dict] = []
    for pass_patterns in all_pass_patterns:
        for pat in pass_patterns:
            if pat["name"] not in seen_names:
                seen_names.add(pat["name"])
                used_patterns.append(pat)

    # Filter out DSP-unfriendly mul fusions (e.g. srl+mul, mul+addi, and+mul, ...).
    # The static .md naming convention is fused_<m1>_<m2>_(rr|...)_p<N> (with an
    # optional _rev_ suffix before _p).  Extract the mnemonic pair from the name
    # and apply the project-wide is_dsp_unfriendly_mul_fusion predicate.
    from arvis.codegen.rtl.isa_fusion.alu_single_cycle import is_dsp_unfriendly_mul_fusion

    def _extract_mnems_from_name(name: str) -> Optional[Tuple[str, str]]:
        # name looks like "fused_<m1>_<m2>_rr_p1" or "fused_<m1>_<m2>_rr_rev_p3"
        if not name.startswith("fused_"):
            return None
        parts = name.split("_")
        # parts[0] == "fused", parts[1] = m1, parts[2] = m2, then suffixes
        if len(parts) < 4:
            return None
        return (parts[1], parts[2])

    filtered_used: List[Dict] = []
    rejected_names: List[str] = []
    for pat in used_patterns:
        mnems = _extract_mnems_from_name(pat["name"])
        if mnems is not None and is_dsp_unfriendly_mul_fusion(mnems):
            rejected_names.append(pat["name"])
            continue
        filtered_used.append(pat)
    if rejected_names:
        logger.info(
            "Dropped %d DSP-unfriendly mul fusion patterns: %s",
            len(rejected_names),
            ", ".join(rejected_names),
        )
    used_patterns = filtered_used

    # Also filter parametric patterns: the input is list of ((m1, m2), count).
    if parametric_patterns:
        before_param = len(parametric_patterns)
        parametric_patterns = [(pat, cnt) for pat, cnt in parametric_patterns if not is_dsp_unfriendly_mul_fusion(pat)]
        if before_param != len(parametric_patterns):
            logger.info(
                "Dropped %d DSP-unfriendly parametric mul fusions",
                before_param - len(parametric_patterns),
            )

    # Separate into R4 (3-input) and R-type (2-input) patterns
    r4_patterns = [p for p in used_patterns if p["is_r4"]]
    r_patterns = [p for p in used_patterns if not p["is_r4"]]

    # Assign fresh encodings
    lines: list[str] = []
    lines.append(";; === Merged patterns: Pass1 + Pass2 used + HC ===")
    lines.append("")  # placeholder for totals

    r4_slot = 0
    r_slot = 0
    total = 0

    # R4 patterns first (3-input, funct3 0-3)
    if r4_patterns:
        lines.append("")
        lines.append(";; ── R4-type patterns (3 register inputs) ──")
        lines.append("")

    for pat in r4_patterns:
        if r4_slot >= _R4_MAX:
            logger.warning(
                "R4 slots exhausted (%d), dropping pattern %s",
                _R4_MAX,
                pat["name"],
            )
            break
        opcode, funct3, funct2 = _r4_enc(r4_slot)
        new_asm = f".insn r4 0x{opcode:02x}, {funct3}, {funct2}, %0, %1, %2, %3"
        new_text = _rewrite_encoding(pat["full_text"], pat["asm_template"], new_asm)
        lines.append(new_text)
        lines.append("")
        r4_slot += 1
        total += 1

    # R-type patterns (2-input, funct3 4-7)
    if r_patterns:
        lines.append("")
        lines.append(";; ── R-type patterns (2 register inputs) ──")
        lines.append("")

    for pat in r_patterns:
        opcode, funct3, funct7 = _r_enc(r_slot)
        # Determine register operand references from original asm
        # Keep the same %0, %1, %2/zero structure
        orig_regs = re.findall(r"(%\d+|zero)", pat["asm_template"])
        if len(orig_regs) >= 3:
            reg_part = ", ".join(orig_regs[:3])
        else:
            reg_part = "%0, %1, zero"
        new_asm = f".insn r 0x{opcode:02x}, {funct3}, 0x{funct7:02x}, {reg_part}"
        new_text = _rewrite_encoding(pat["full_text"], pat["asm_template"], new_asm)
        lines.append(new_text)
        lines.append("")
        r_slot += 1
        total += 1

    # HC patterns from profiling (Category 1 — combine pass merges them)
    hc_lines: list[str] = []
    n_hc = 0
    if hc_candidates:
        _, n_hc = _generate_hardcoded_imm_patterns(hc_lines, hc_candidates, r_slot)
        if n_hc > 0:
            lines.append("")
            lines.append(";; ── Hardcoded-immediate patterns (from profiling) ──")
            lines.append("")
            lines.extend(hc_lines)
            r_slot += n_hc
            total += n_hc

    # NOTE: Cat 2 patterns (add→shift, add→bitwise, bitwise→shift) are NOT
    # emitted as define_peephole2 — GCC's scheduler breaks consecutive insn
    # pairs making peephole2 unreliable.  Instead, Cat 2 patterns are added
    # to patterns.json and matched by the GIMPLE plugin (fused_pass.so) at
    # tree level, where the operations are naturally adjacent before scheduling.
    # See get_cat2_gimple_patterns() below.

    # Parametric immediate patterns (combine pass, imm encoded as x%cN in rs3)
    from arvis.codegen.gcc.exhaustive_patterns import generate_parametric_imm_patterns

    if parametric_patterns:
        param_lines, r4_slot, n_param = generate_parametric_imm_patterns(parametric_patterns, r4_slot_start=r4_slot)
        if n_param > 0:
            lines.append("")
            lines.append(";; ── Parametric-immediate patterns (imm in rs3/rs2 field) ──")
            lines.append("")
            lines.extend(param_lines)
            total += n_param

    # Update header
    pass_counts = ", ".join(f"pass{i + 1}={len(pp)}" for i, pp in enumerate(all_pass_patterns))
    lines[1] = f";; Total: {total} patterns (R4={r4_slot}, R={r_slot}, HC={n_hc}, {pass_counts})"

    content = "\n".join(lines)

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(content)
        logger.info("Merged .md written to %s (%d patterns)", output_path, total)

    return content, total, r4_slot


# ===================================================================
# Part 2: Category 2 — GIMPLE plugin patterns + Spike encodings
# ===================================================================

# Instructions that take an immediate second operand
_PEEP_IMM_INSNS = {"addi", "andi", "ori", "xori", "slli", "srli", "srai"}

# Map mnemonic to GIMPLE tree code for Cat 2 patterns
_MNEM_TO_GIMPLE: dict[str, str] = {
    "add": "PLUS_EXPR",
    "addi": "PLUS_EXPR",
    "sub": "MINUS_EXPR",
    "mul": "MULT_EXPR",
    "and": "BIT_AND_EXPR",
    "andi": "BIT_AND_EXPR",
    "or": "BIT_IOR_EXPR",
    "ori": "BIT_IOR_EXPR",
    "xor": "BIT_XOR_EXPR",
    "xori": "BIT_XOR_EXPR",
    "sll": "LSHIFT_EXPR",
    "slli": "LSHIFT_EXPR",
    "srl": "RSHIFT_EXPR",
    "srli": "RSHIFT_EXPR",
    "sra": "RSHIFT_EXPR",
    "srai": "RSHIFT_EXPR",
}

_MNEM_COMMUTATIVE: dict[str, bool] = {
    "add": True,
    "addi": True,
    "sub": False,
    "mul": True,
    "and": True,
    "andi": True,
    "or": True,
    "ori": True,
    "xor": True,
    "xori": True,
    "sll": False,
    "slli": False,
    "srl": False,
    "srli": False,
    "sra": False,
    "srai": False,
}
