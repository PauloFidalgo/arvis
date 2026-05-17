"""
Encoding Optimizer for cv32e40p_pkg.sv enums.

After pruning removes unused features (debug, hwloop, IRQ, PULP, etc.),
many enum members become dead. This module:

1. Removes unused enum members from the package
2. Re-encodes remaining members with minimal bit widths
3. Updates the width parameter (e.g. ALU_OP_WIDTH 7 -> 5)
4. Rewrites ALL .sv files to use the new encodings

Constraints:
  - debug_state_e is intentionally one-hot (maps to output pins) → skip
  - csr_num_e values are fixed by RISC-V spec → skip
  - ALU div operations use bit-extraction (operator_i[0], operator_i[1:0])
    so if div ops are used, their low bits must be preserved
  - FSM states with auto-numbering (no explicit value) can simply be
    renumbered by removing unused entries

The optimizer is conservative: if bit-extraction constraints exist for
an enum type and the constrained members are in use, it preserves those
bit relationships or skips the optimization.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from arvis.analysis.enum_usage import (
    EnumDefinition,
    EnumUsageReport,
    EnumUsageResult,
    analyze_enum_usage,
)


@dataclass
class EncodingChange:
    """Describes a single enum member's encoding change."""

    member_name: str
    old_encoding: str  # e.g. "7'b0011000"
    new_encoding: str  # e.g. "5'b00000"
    old_value: int
    new_value: int


@dataclass
class EnumReencoding:
    """Complete re-encoding plan for one enum type."""

    enum_name: str  # e.g. "alu_opcode_e"
    old_width: int
    new_width: int
    width_param: Optional[str]  # e.g. "ALU_OP_WIDTH" or None
    kept_members: List[str]  # members to keep (in order)
    removed_members: List[str]  # members to remove
    changes: List[EncodingChange]
    skipped: bool = False  # True if optimization was skipped
    skip_reason: str = ""


@dataclass
class EncodingOptimizationResult:
    """Result of the full encoding optimization pass."""

    reencodings: List[EnumReencoding] = field(default_factory=list)
    files_modified: int = 0
    total_bits_saved: int = 0

    def summary(self) -> str:
        lines = ["Encoding Optimization Results:"]
        for r in self.reencodings:
            if r.skipped:
                lines.append(f"  {r.enum_name}: SKIPPED ({r.skip_reason})")
                continue
            if r.old_width == r.new_width and not r.removed_members:
                lines.append(f"  {r.enum_name}: no change needed")
                continue
            lines.append(
                f"  {r.enum_name}: {r.old_width} -> {r.new_width} bits, "
                f"{len(r.kept_members)} kept, {len(r.removed_members)} removed"
            )
            for c in r.changes:
                lines.append(f"    {c.member_name}: {c.old_encoding} -> {c.new_encoding}")
        if self.total_bits_saved > 0:
            lines.append(f"\n  Total: {self.total_bits_saved} bits saved across {self.files_modified} files")
        return "\n".join(lines)


# ── ALU div ops that use bit-extraction ──
# operator_i[0] = signed flag, operator_i[1:0] = OpCode_SI
_ALU_DIV_OPS = {"ALU_DIVU", "ALU_DIV", "ALU_REMU", "ALU_REM"}

# These div ops have a specific bit-level encoding contract:
#   ALU_DIVU = ...00  (unsigned div)
#   ALU_DIV  = ...01  (signed div)
#   ALU_REMU = ...10  (unsigned rem)
#   ALU_REM  = ...11  (signed rem)
# bit[0] = signed, bit[1] = rem/div


def _parse_encoding_value(enc_str: str) -> int:
    """Parse a SystemVerilog literal like 7'b0011000 to an integer."""
    if not enc_str:
        return -1
    enc_str = enc_str.strip()
    # Match N'bBINARY or N'hHEX or N'dDECIMAL
    m = re.match(r"\d+'([bBhHdD])([0-9a-fA-F_xXzZ]+)", enc_str)
    if not m:
        return -1
    base_char = m.group(1).lower()
    value_str = m.group(2).replace("_", "")
    try:
        if base_char == "b":
            return int(value_str, 2)
        elif base_char == "h":
            return int(value_str, 16)
        elif base_char == "d":
            return int(value_str, 10)
    except ValueError:
        return -1
    return -1


def _make_encoding(value: int, width: int, fmt: str = "b") -> str:
    """Create a SystemVerilog encoding literal."""
    if fmt == "b":
        return f"{width}'b{value:0{width}b}"
    elif fmt == "h":
        hex_digits = (width + 3) // 4
        return f"{width}'h{value:0{hex_digits}x}"
    else:
        return f"{width}'d{value}"


def plan_reencoding(
    usage_report: EnumUsageReport,
) -> List[EnumReencoding]:
    """Plan re-encoding for all reducible enum types.

    Returns a list of EnumReencoding plans (some may be skipped).
    """
    plans: List[EnumReencoding] = []

    for type_name, result in usage_report.results.items():
        plan = _plan_single_enum(type_name, result)
        plans.append(plan)

    return plans


def _plan_single_enum(
    type_name: str,
    result: EnumUsageResult,
) -> EnumReencoding:
    """Plan re-encoding for a single enum type."""
    enum_def = result.enum_def
    used = result.used_members
    unused = result.unused_members

    # Safety: never re-encode FSM state enums — the controller relies on
    # specific state values and transitions. Re-encoding breaks the pipeline.
    _SKIP_ENUMS = {"ctrl_state_e", "debug_state_e", "csr_num_e"}
    if type_name in _SKIP_ENUMS:
        return EnumReencoding(
            enum_name=type_name,
            old_width=enum_def.width,
            new_width=enum_def.width,
            width_param=None,
            kept_members=list(enum_def.member_order),
            removed_members=[],
            changes=[],
            skipped=True,
            skip_reason=f"safety: {type_name} controls pipeline FSM",
        )

    # forced_dead: members that PruneConfig says are dead but are still
    # textually referenced in RTL. They MUST stay in the pkg, but don't
    # count toward the minimum encoding width.
    getattr(result, "_forced_dead", set())

    # Preserve member order from the original definition
    # kept_members = all textually referenced members (used)
    # removed_members = members truly absent from all RTL files
    kept_members = [m for m in enum_def.member_order if m in used]
    removed_members = [m for m in enum_def.member_order if m in unused]

    old_width = enum_def.width

    # ── Special case: bit-extraction constraints ──
    has_bit_constraints = len(result.bit_extraction_constraints) > 0

    if has_bit_constraints and type_name == "alu_opcode_e":
        return _plan_alu_with_div_constraints(enum_def, kept_members, removed_members, result)

    if has_bit_constraints:
        # For other types with bit extraction, skip for safety
        return EnumReencoding(
            enum_name=type_name,
            old_width=old_width,
            new_width=old_width,
            width_param=enum_def.width_param,
            kept_members=kept_members,
            removed_members=removed_members,
            changes=[],
            skipped=True,
            skip_reason=f"bit-extraction constraints: {result.bit_extraction_constraints}",
        )

    # ── Normal case: sequential re-encoding ──
    n_kept = len(kept_members)
    if n_kept == 0:
        return EnumReencoding(
            enum_name=type_name,
            old_width=old_width,
            new_width=old_width,
            width_param=enum_def.width_param,
            kept_members=kept_members,
            removed_members=removed_members,
            changes=[],
            skipped=True,
            skip_reason="no members used",
        )

    # Use the min_width from the report — it accounts for forced_dead
    # (members that are dead but textually referenced, so they need encodings
    # but don't count toward the minimum width)
    new_width = result.min_width
    # But ensure we have enough values for ALL kept members
    new_width = max(new_width, math.ceil(math.log2(max(n_kept, 2))))

    # For FSM states: if the original uses auto-numbering (no explicit values),
    # just removing unused members is enough — they'll auto-renumber
    has_explicit_values = any(enum_def.members.get(m, "") != "" for m in kept_members)

    changes: List[EncodingChange] = []
    for i, member in enumerate(kept_members):
        old_enc = enum_def.members.get(member, "")
        old_val = _parse_encoding_value(old_enc) if old_enc else i
        if old_val < 0:
            old_val = i

        if has_explicit_values:
            new_enc = _make_encoding(i, new_width)
        else:
            # Auto-numbered: just leave implicit (value = position)
            new_enc = ""

        changes.append(
            EncodingChange(
                member_name=member,
                old_encoding=old_enc,
                new_encoding=new_enc,
                old_value=old_val,
                new_value=i,
            )
        )

    # Don't reduce if no benefit
    if new_width >= old_width and not removed_members:
        return EnumReencoding(
            enum_name=type_name,
            old_width=old_width,
            new_width=old_width,
            width_param=enum_def.width_param,
            kept_members=kept_members,
            removed_members=removed_members,
            changes=[],
            skipped=True,
            skip_reason="no width reduction possible",
        )

    return EnumReencoding(
        enum_name=type_name,
        old_width=old_width,
        new_width=new_width,
        width_param=enum_def.width_param,
        kept_members=kept_members,
        removed_members=removed_members,
        changes=changes,
    )


def _plan_alu_with_div_constraints(
    enum_def: EnumDefinition,
    kept_members: List[str],
    removed_members: List[str],
    result: EnumUsageResult,
) -> EnumReencoding:
    """Plan ALU re-encoding, preserving div bit-extraction semantics.

    The divider uses operator_i[0] for signed/unsigned and
    operator_i[1:0] as OpCode_SI. We must ensure that if DIV ops are
    kept, their low 2 bits maintain the correct relationship:
      - DIVU: ...00
      - DIV:  ...01
      - REMU: ...10
      - REM:  ...11
    """
    old_width = enum_def.width

    # Check if any div ops are in the kept set
    used_div_ops = set(kept_members) & _ALU_DIV_OPS
    has_div_constraint = len(used_div_ops) > 0

    n_kept = len(kept_members)
    new_width = max(1, math.ceil(math.log2(max(n_kept, 2))))

    # If we have div constraint, we need at least 2 bits for the low part
    if has_div_constraint:
        new_width = max(new_width, 2)

    changes: List[EncodingChange] = []

    # Assign encodings: div ops get special treatment
    # Reserve values with specific low-2-bit patterns for div ops
    div_encoding_map = {
        "ALU_DIVU": 0b00,  # ...00
        "ALU_DIV": 0b01,  # ...01
        "ALU_REMU": 0b10,  # ...10
        "ALU_REM": 0b11,  # ...11
    }

    # First, allocate div ops if present
    used_values: Set[int] = set()
    div_base = 0  # The high bits for div ops

    if has_div_constraint:
        # Find a base value where we can fit all 4 div ops
        # The base provides the high bits, low 2 bits are the div encoding
        # Start with base=0 and find one that doesn't conflict
        # Since we're re-encoding from scratch, just use base=0
        for member in kept_members:
            if member in div_encoding_map:
                val = (div_base << 2) | div_encoding_map[member]
                used_values.add(val)

    # Then assign remaining members
    next_val = 0
    for member in kept_members:
        if member in div_encoding_map and has_div_constraint:
            # Already assigned
            val = (div_base << 2) | div_encoding_map[member]
        else:
            # Find next unused value
            while next_val in used_values:
                next_val += 1
            val = next_val
            used_values.add(val)
            next_val += 1

        old_enc = enum_def.members.get(member, "")
        old_val = _parse_encoding_value(old_enc) if old_enc else 0
        new_enc = _make_encoding(val, new_width)

        changes.append(
            EncodingChange(
                member_name=member,
                old_encoding=old_enc,
                new_encoding=new_enc,
                old_value=old_val if old_val >= 0 else 0,
                new_value=val,
            )
        )

    # Don't reduce if no benefit
    if new_width >= old_width and not removed_members:
        return EnumReencoding(
            enum_name="alu_opcode_e",
            old_width=old_width,
            new_width=old_width,
            width_param=enum_def.width_param,
            kept_members=kept_members,
            removed_members=removed_members,
            changes=[],
            skipped=True,
            skip_reason="no width reduction possible",
        )

    return EnumReencoding(
        enum_name="alu_opcode_e",
        old_width=old_width,
        new_width=new_width,
        width_param=enum_def.width_param,
        kept_members=kept_members,
        removed_members=removed_members,
        changes=changes,
    )


# ======================================================================
# RTL Rewriting
# ======================================================================


def _rewrite_pkg_enum(
    pkg_text: str,
    reencoding: EnumReencoding,
) -> str:
    """Rewrite a single enum typedef in the package file.

    Removes unused members, updates encodings, and adjusts width.
    """
    if reencoding.skipped or not reencoding.changes:
        return pkg_text

    # We need the enum definition info from the changes
    enum_name = reencoding.enum_name

    # Step 1: Update width parameter if applicable
    if reencoding.width_param and reencoding.new_width != reencoding.old_width:
        old_param = f"parameter {reencoding.width_param} = {reencoding.old_width};"
        new_param = f"parameter {reencoding.width_param} = {reencoding.new_width};"
        pkg_text = pkg_text.replace(old_param, new_param)

    # Step 2: Rebuild the enum body
    # Find the enum typedef block by searching for the closing marker first,
    # then finding the nearest typedef enum opening before it.
    # This avoids matching across multiple enum definitions.
    close_pattern = re.compile(r"\}\s*" + re.escape(enum_name) + r"\s*;")
    close_match = close_pattern.search(pkg_text)
    if not close_match:
        print(f"  Warning: Could not find closing of enum {enum_name} in pkg")
        return pkg_text

    # Search backward from close for the nearest 'typedef enum logic'
    before_close = pkg_text[: close_match.start()]
    # Find the last typedef enum logic [...] { before the closing
    open_pattern = re.compile(r"typedef\s+enum\s+logic\s*(?:\[[^\]]+\])?\s*\{")
    all_opens = list(open_pattern.finditer(before_close))
    if not all_opens:
        print(f"  Warning: Could not find opening of enum {enum_name} in pkg")
        return pkg_text

    last_open = all_opens[-1]  # The nearest opening before the close

    # Build match groups: (opening)(body)(closing)
    class _FakeMatch:
        def __init__(self, text, open_m, close_m):
            self._open_start = open_m.start()
            self._open_end = open_m.end()
            self._body_start = open_m.end()
            self._body_end = close_m.start()
            self._close_start = close_m.start()
            self._close_end = close_m.end()

        def start(self, g=0):
            if g == 0:
                return self._open_start
            if g == 1:
                return self._open_start
            if g == 2:
                return self._body_start
            if g == 3:
                return self._close_start

        def end(self, g=0):
            if g == 0:
                return self._close_end
            if g == 1:
                return self._open_end
            if g == 2:
                return self._body_end
            if g == 3:
                return self._close_end

        def group(self, g):
            if g == 1:
                return pkg_text[self._open_start : self._open_end]
            if g == 2:
                return pkg_text[self._body_start : self._body_end]
            if g == 3:
                return pkg_text[self._close_start : self._close_end]

    match = _FakeMatch(pkg_text, last_open, close_match)
    if not match:
        print(f"  Warning: Could not find enum {enum_name} in pkg")
        return pkg_text

    # Build new body from changes
    change_map = {c.member_name: c for c in reencoding.changes}

    # Parse the original body to preserve comments and structure
    original_body = match.group(2)
    new_body_lines = []

    # Track which members we've seen
    seen_members = set()

    # Process original body line by line
    lines = original_body.split("\n")
    in_fused_section = False

    for line in lines:
        stripped = line.strip()

        # Track ARVIS_FUSED sections
        if "ARVIS_FUSED_BEGIN" in stripped:
            in_fused_section = True
        if "ARVIS_FUSED_END" in stripped:
            in_fused_section = False

        # Check if this line defines an enum member
        member_match = re.match(r"^(\s*),?\s*(\w+)\s*=\s*\d+'[bBhHdD][0-9a-fA-F_]+(.*)$", line)
        if not member_match:
            # Also match auto-numbered members (no explicit value)
            member_match = re.match(r"^(\s*),?\s*(\w+)\s*(,?\s*(?://.*)?)\s*$", line)

        if member_match:
            indent = member_match.group(1) or "    "
            member_name = member_match.group(2)
            member_match.group(3) if len(member_match.groups()) > 2 else ""

            if member_name in change_map:
                # Keep this member with new encoding
                change = change_map[member_name]
                seen_members.add(member_name)

                if change.new_encoding:
                    # Preserve trailing comment if present
                    trailing_comment = ""
                    tc_match = re.search(r"//.*$", line)
                    if tc_match:
                        trailing_comment = "  " + tc_match.group(0)
                    new_body_lines.append(f"{indent}{member_name} = {change.new_encoding}{trailing_comment}")
                else:
                    new_body_lines.append(f"{indent}{member_name}")
            elif member_name in reencoding.removed_members:
                # Skip this member (it's removed)
                continue
            elif in_fused_section:
                # Fused member not in change_map — re-encode its literal
                # to match the new width
                enc_match = re.search(r"(\d+)'([bBhHdD])([0-9a-fA-F_]+)", line)
                if enc_match and reencoding.new_width != reencoding.old_width:
                    old_width_str = enc_match.group(1)
                    base = enc_match.group(2)
                    value_str = enc_match.group(3)
                    # Parse old value
                    old_val = _parse_encoding_value(f"{old_width_str}'{base}{value_str}")
                    if old_val >= 0:
                        new_enc = _make_encoding(old_val, reencoding.new_width)
                        # Replace the literal in the line
                        new_line = line.replace(enc_match.group(0), new_enc, 1)
                        new_body_lines.append(new_line)
                    else:
                        new_body_lines.append(line)
                else:
                    new_body_lines.append(line)
            # Otherwise skip (might be a stale member)
        else:
            # Non-member line (comment, blank, pragma marker)
            # Keep section markers and meaningful comments
            if "ARVIS_FUSED" in stripped or stripped == "" or stripped.startswith("//"):
                new_body_lines.append(line)

    # Now rebuild properly with commas
    # Find all member lines and add commas between them
    final_lines = []
    member_indices = []

    for i, line in enumerate(new_body_lines):
        stripped = line.strip().lstrip(",").strip()
        # Check if it's a member definition line
        if re.match(r"\w+\s*(?:=\s*\d+'[bBhHdD])?", stripped) and not stripped.startswith("//"):
            member_indices.append(i)
        final_lines.append(line)

    # Add commas: all members except the last one before ARVIS_FUSED_BEGIN
    # or the absolute last should have commas
    # Actually, let's rebuild more carefully
    result_body_lines = []
    member_lines_data = []  # (index, indent, member_text, trailing_comment)

    for i, line in enumerate(new_body_lines):
        stripped = line.strip()
        # Is this a member line?
        m = re.match(r"^(\s*),?\s*(\w+\s*(?:=\s*\d+'[bBhHdD][0-9a-fA-F_]+)?)\s*(//.*)?$", line)
        if m and not stripped.startswith("//"):
            indent = m.group(1) or "    "
            member_text = m.group(2).strip()
            comment = m.group(3) or ""
            member_lines_data.append((i, indent, member_text, comment))

    # Rebuild: add commas between consecutive members
    member_set = {i for i, _, _, _ in member_lines_data}
    member_idx_to_data = {i: (indent, mt, cmt) for i, indent, mt, cmt in member_lines_data}

    last_member_idx = member_lines_data[-1][0] if member_lines_data else -1

    for i, line in enumerate(new_body_lines):
        if i in member_set:
            indent, mt, cmt = member_idx_to_data[i]
            # Add comma if not the very last member
            # (But the last member before a FUSED_BEGIN pragma needs comma
            #  if there are fused members after it)
            is_last = i == last_member_idx

            # Check if there's a FUSED section with content after
            if is_last:
                # Check if there are fused members following
                remaining = "\n".join(new_body_lines[i + 1 :])
                has_fused_content = bool(re.search(r"ARVIS_FUSED_BEGIN.*?\w+\s*=\s*\d+", remaining, re.DOTALL))
                if has_fused_content:
                    is_last = False  # Need comma because fused entries follow

            comma = "" if is_last else ","
            if cmt:
                result_body_lines.append(f"{indent}{mt}{comma}  {cmt}")
            else:
                result_body_lines.append(f"{indent}{mt}{comma}")
        else:
            result_body_lines.append(line)

    new_body = "\n".join(result_body_lines)

    # Replace in pkg text
    pkg_text = pkg_text[: match.start(2)] + new_body + pkg_text[match.end(2) :]

    return pkg_text


def _rewrite_width_in_typedef(pkg_text: str, reencoding: EnumReencoding) -> str:
    """Update the width expression in the typedef line itself.

    For enums that don't use a width parameter (like ctrl_state_e with [4:0]),
    update the range directly.
    """
    if reencoding.skipped or reencoding.new_width == reencoding.old_width:
        return pkg_text

    enum_name = reencoding.enum_name

    if reencoding.width_param:
        # Width is via parameter, already handled in _rewrite_pkg_enum
        return pkg_text

    # Direct width in typedef: [4:0] -> [2:0]
    f"[{reencoding.old_width - 1}:0]"
    new_range = f"[{reencoding.new_width - 1}:0]"

    # Find the typedef line for this enum
    pattern = re.compile(
        rf"(typedef\s+enum\s+logic\s*)\[{reencoding.old_width - 1}:0\]"
        rf"(\s*\{{.*?\}}\s*{re.escape(enum_name)}\s*;)",
        re.DOTALL,
    )

    match = pattern.search(pkg_text)
    if match:
        pkg_text = pkg_text[: match.start()] + match.group(1) + new_range + match.group(2) + pkg_text[match.end() :]

    return pkg_text


def _clean_dead_enum_references(
    rtl_dir: Path,
    forced_dead: Dict[str, Set[str]],
) -> int:
    """Remove dead enum member references from all .sv files.

    Handles these patterns:
    - ``(signal != DEAD_MEMBER) &&`` in assertion chains → remove term (always true)
    - ``(signal == DEAD_MEMBER || ...)`` in conditions → remove term (always false)
    - ``(signal == DEAD_MEMBER)`` as entire condition → replace with 1'b0
    - Entire assertion lines that only reference dead members → remove line

    Returns number of references cleaned.
    """
    all_dead = set()
    for members in forced_dead.values():
        all_dead |= members

    if not all_dead:
        return 0

    total_cleaned = 0

    # Sort by length descending to avoid partial matches
    dead_sorted = sorted(all_dead, key=len, reverse=True)

    for sv_file in rtl_dir.rglob("*.sv"):
        if sv_file.name == "cv32e40p_pkg.sv":
            continue

        try:
            text = sv_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        original = text

        # SAFETY: Only clean dead references inside SVA assertion lines.
        # Functional RTL (if/case/assign) must NOT be modified — the pruner
        # already handled those. We only clean SVA assertions that Verilator
        # would complain about if the enum member doesn't exist.
        lines = text.split("\n")
        new_lines = []
        for line in lines:
            stripped = line.strip()
            # Only process SVA assertion lines
            is_sva = ("assert" in stripped or "assume" in stripped or "cover" in stripped) and "property" in stripped
            # Also process continuation lines of multi-line assertions
            # (lines that are part of a |-> chain)
            is_sva_cont = (
                stripped.startswith("(")
                and any(m in line for m in dead_sorted)
                and "|>" in "\n".join(lines[max(0, lines.index(line) - 5) : lines.index(line) + 1])
                if line in lines
                else False
            )

            if is_sva or is_sva_cont:
                modified_line = line
                for member in dead_sorted:
                    esc = re.escape(member)
                    # (signal != DEAD) && → remove (always true)
                    modified_line = re.sub(rf"\(\w+ != {esc}\b\)\s*&&\s*", "", modified_line)
                    modified_line = re.sub(rf"&&\s*\(\w+ != {esc}\b\)", "", modified_line)
                    # signal == DEAD || → remove (always false)
                    modified_line = re.sub(rf"\w+ == {esc}\b\s*\|\|\s*", "", modified_line)
                    modified_line = re.sub(rf"\|\|\s*\w+ == {esc}\b", "", modified_line)
                new_lines.append(modified_line)
            else:
                new_lines.append(line)

        text = "\n".join(new_lines)

        if text != original:
            sv_file.write_text(text)
            count = sum(1 for m in all_dead if m in original and m not in text)
            total_cleaned += count

    return total_cleaned


def apply_encoding_optimization(
    rtl_dir: Path,
    usage_report: Optional[EnumUsageReport] = None,
    forced_unused: Optional[Dict[str, Set[str]]] = None,
    verbose: bool = True,
) -> EncodingOptimizationResult:
    """Apply encoding optimization to all RTL files.

    This is the main entry point. It:
    1. Analyzes enum usage (if not already done)
    2. Plans re-encodings
    3. Rewrites cv32e40p_pkg.sv
    4. Updates all .sv files that reference the old encodings

    Args:
        rtl_dir: Path to the RTL output directory (containing rtl/ subdirectory)
        usage_report: Optional pre-computed usage report
        forced_unused: Optional dict mapping enum type name to a set of member
                      names that should be treated as unused regardless of
                      whether they appear in RTL text. This is critical for
                      ALU ops that may appear in dead comparisons (e.g.
                      ``alu_operator != ALU_BEXT`` in id_stage) but were
                      already pruned from the decoder and ALU case statements.
                      Example: ``{"alu_opcode_e": {"ALU_BEXT", "ALU_CLIP"}}``

    Returns:
        EncodingOptimizationResult with details of all changes
    """
    _print = print if verbose else (lambda *a, **k: None)
    result = EncodingOptimizationResult()

    # Step 0: Clean dead enum references from RTL files BEFORE analysis.
    # The pruning phase already removed these ops from the decoder and ALU
    # case statements, but dead references remain in id_stage.sv assertions
    # (e.g. `alu_operator != ALU_BEXT`). We must remove those first so
    # the members can then be removed from the package.
    if forced_unused:
        # Determine scan root
        scan_root = rtl_dir / "rtl" if (rtl_dir / "rtl").exists() else rtl_dir
        n_cleaned = _clean_dead_enum_references(scan_root, forced_unused)
        if n_cleaned > 0:
            _print(f"    Cleaned {n_cleaned} dead enum references from RTL files")

    # Step 1: Analyze usage AFTER cleaning dead references
    # (Don't use a pre-existing report — it was computed before cleaning)
    usage_report = analyze_enum_usage(rtl_dir)

    if not usage_report.results:
        _print("  No enum types to optimize")
        return result

    # Step 2: Plan re-encodings
    plans = plan_reencoding(usage_report)

    # Filter to only actionable plans
    actionable = [p for p in plans if not p.skipped and p.changes]

    if not actionable:
        _print("  No encoding optimizations possible")
        for p in plans:
            if p.skipped:
                _print(f"    {p.enum_name}: {p.skip_reason}")
        return result

    # Step 3: Rewrite pkg file
    pkg_path = rtl_dir / "rtl" / "include" / "cv32e40p_pkg.sv"
    if not pkg_path.exists():
        pkg_path = rtl_dir / "include" / "cv32e40p_pkg.sv"
    if not pkg_path.exists():
        _print("  Error: cv32e40p_pkg.sv not found")
        return result

    pkg_text = pkg_path.read_text()

    for plan in actionable:
        pkg_text = _rewrite_pkg_enum(pkg_text, plan)
        pkg_text = _rewrite_width_in_typedef(pkg_text, plan)
        result.reencodings.append(plan)
        bits_saved = plan.old_width - plan.new_width
        if bits_saved > 0:
            result.total_bits_saved += bits_saved

    pkg_path.write_text(pkg_text)
    result.files_modified = 1

    # Step 4: No need to update references in other .sv files!
    # SystemVerilog enum members are referenced by name (ALU_ADD, DECODE, etc.),
    # not by their encoded value. Since we changed the values in the package,
    # all files that `import cv32e40p_pkg::*` automatically get the new values.
    #
    # The ONLY exception is bit-extraction patterns (operator_i[0]) which we've
    # already handled in the planning phase by preserving bit relationships.

    # Step 3b: Signal width patching is DISABLED.
    # Changing wire widths (e.g. imm_b_mux_sel from [3:0] to [1:0]) requires
    # updating ALL consumers across the entire pipeline (decoder, id_stage,
    # ex_stage, etc.). Missing even one causes width mismatches and wrong
    # mux selection. The parameter values are already re-encoded with fewer
    # bits; the extra wire width is harmless (upper bits are zero).
    # _patch_signal_widths(rtl_dir)

    _print("\n  Encoding optimization applied:")
    for plan in actionable:
        n_removed = len(plan.removed_members)
        bits = plan.old_width - plan.new_width
        _print(
            f"    {plan.enum_name}: {plan.old_width}b -> {plan.new_width}b "
            f"({len(plan.kept_members)} kept, {n_removed} removed, "
            f"{bits} bits saved)"
        )

    for plan in plans:
        if plan.skipped:
            result.reencodings.append(plan)

    return result
