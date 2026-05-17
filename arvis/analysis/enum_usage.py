"""
Analyzes which enum/parameter values from cv32e40p_pkg.sv are
actually still referenced in the pruned RTL files.

After pruning removes unused features (debug, hwloop, IRQ, etc.),
many enum members become dead — they're defined in the package but
never referenced in any RTL file. This module identifies them so
the encoding optimizer can re-encode with fewer bits.

Supported enum types:
  - alu_opcode_e (ALU_ADD, ALU_SUB, ...)
  - mul_opcode_e (MUL_MAC32, MUL_H, ...)
  - ctrl_state_e (RESET, DECODE, FLUSH_EX, ...)
  - mult_state_e (IDLE_MULT, STEP0, ...)

Also detects bit-extraction patterns (e.g. operator_i[0]) that
constrain how certain enum values can be re-encoded.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class EnumDefinition:
    """Parsed enum type from cv32e40p_pkg.sv."""

    name: str  # e.g. "alu_opcode_e"
    width_param: Optional[str]  # e.g. "ALU_OP_WIDTH" or None
    width: int  # current bit width
    members: Dict[str, str]  # name -> original encoding string e.g. "7'b0011000"
    member_order: List[str]  # ordered list of member names


@dataclass
class EnumUsageResult:
    """Result of enum usage analysis for one enum type."""

    enum_def: EnumDefinition
    used_members: Set[str]  # members still referenced in RTL
    unused_members: Set[str]  # members defined but never referenced
    current_width: int
    min_width: int  # ceil(log2(len(used_members)))
    can_reduce: bool  # True if min_width < current_width
    bit_extraction_constraints: List[Tuple[str, str]]  # (signal_pattern, file)

    def summary(self) -> str:
        lines = [
            f"  {self.enum_def.name}:",
            f"    Members: {len(self.used_members)} used / {len(self.enum_def.members)} defined",
            f"    Width: {self.current_width} -> {self.min_width} bits"
            f" ({'REDUCIBLE' if self.can_reduce else 'no change'})",
        ]
        if self.unused_members:
            lines.append(f"    Unused: {sorted(self.unused_members)}")
        if self.bit_extraction_constraints:
            lines.append(f"    Bit-extraction constraints: {len(self.bit_extraction_constraints)}")
            for pat, f in self.bit_extraction_constraints:
                lines.append(f"      {pat} in {f}")
        return "\n".join(lines)


@dataclass
class EnumUsageReport:
    """Full report of enum usage analysis."""

    results: Dict[str, EnumUsageResult] = field(default_factory=dict)

    def summary(self) -> str:
        lines = ["Enum Usage Analysis:"]
        total_bits_saved = 0
        for name, result in self.results.items():
            lines.append(result.summary())
            if result.can_reduce:
                total_bits_saved += result.current_width - result.min_width
        if total_bits_saved > 0:
            lines.append(f"\n  Total encoding width reduction: {total_bits_saved} bits")
        return "\n".join(lines)


# ── Enum types we can optimize ──

# Signal names that carry each enum type, used for bit-extraction detection
_ENUM_SIGNAL_PATTERNS: Dict[str, List[str]] = {
    "alu_opcode_e": [
        r"alu_operator_o",
        r"alu_operator_ex",
        r"alu_operator",
        r"operator_i",
    ],
    "mul_opcode_e": [
        r"mul_operator",
        r"mult_operator\w*",
        r"mult_dot_op_\w*",
    ],
    "ctrl_state_e": [
        r"ctrl_fsm_cs",
        r"ctrl_fsm_ns",
    ],
    "mult_state_e": [
        r"mulh_CS",
        r"mulh_NS",
        r"mult_state\w*",
    ],
}


def parse_pkg_enums(pkg_path: Path) -> Dict[str, EnumDefinition]:
    """Parse enum definitions from cv32e40p_pkg.sv.

    Returns dict mapping enum type name to EnumDefinition.
    """
    text = pkg_path.read_text()
    enums: Dict[str, EnumDefinition] = {}

    # Parse width parameters
    width_params: Dict[str, int] = {}
    for m in re.finditer(r"parameter\s+(\w+)\s*=\s*(\d+)\s*;", text):
        width_params[m.group(1)] = int(m.group(2))

    # Parse typedef enum blocks
    # Pattern: typedef enum logic [WIDTH-1:0] { ... } name;
    enum_pattern = re.compile(
        r"typedef\s+enum\s+logic\s*"
        r"(?:\[(\w+(?:-1)?):0\])?\s*"  # optional [WIDTH-1:0] or [N:0]
        r"\{(.*?)\}\s*(\w+)\s*;",
        re.DOTALL,
    )

    for m in enum_pattern.finditer(text):
        width_expr = m.group(1)
        body = m.group(2)
        type_name = m.group(3)

        # Determine width
        width = 0
        width_param = None
        if width_expr:
            # Could be "ALU_OP_WIDTH-1" or just "4" or "2"
            if "-1" in width_expr:
                param_name = width_expr.replace("-1", "").strip()
                if param_name in width_params:
                    width = width_params[param_name]
                    width_param = param_name
                else:
                    try:
                        width = int(param_name)
                    except ValueError:
                        width = 8  # fallback
            else:
                try:
                    width = int(width_expr) + 1
                except ValueError:
                    if width_expr in width_params:
                        width = width_params[width_expr] + 1
                        width_param = width_expr
                    else:
                        width = 8
        else:
            # Single-bit enum (logic with no range)
            width = 1

        # Parse members
        members: Dict[str, str] = {}
        member_order: List[str] = []

        # Remove comments from body for easier parsing
        clean_body = re.sub(r"//[^\n]*", "", body)

        for mm in re.finditer(r"(\w+)\s*(?:=\s*(\d+'\s*[bBhHdD][0-9a-fA-F_xXzZ]+))?\s*[,}]?", clean_body):
            name = mm.group(1)
            encoding = mm.group(2) if mm.group(2) else None
            if name and not name.startswith("//"):
                members[name] = encoding or ""
                member_order.append(name)

        if members:
            enums[type_name] = EnumDefinition(
                name=type_name,
                width_param=width_param,
                width=width,
                members=members,
                member_order=member_order,
            )

    return enums


def find_used_members(
    rtl_dir: Path,
    enum_def: EnumDefinition,
    exclude_pkg: bool = True,
) -> Set[str]:
    """Scan all .sv files in rtl_dir for references to enum members.

    Returns the set of member names that are actually referenced.
    """
    used = set()
    member_names = set(enum_def.members.keys())

    # Build a single regex that matches any member name as a whole word
    if not member_names:
        return used

    # Sort by length (longest first) to avoid partial matches
    sorted_names = sorted(member_names, key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted_names) + r")\b")

    # Scan all .sv files
    for sv_file in rtl_dir.rglob("*.sv"):
        if exclude_pkg and sv_file.name == "cv32e40p_pkg.sv":
            continue

        try:
            text = sv_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        # Remove single-line comments (but not inside strings)
        # Remove lines that are fully commented or are PRUNED markers
        lines = text.split("\n")
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            # Skip lines that are entirely comments
            if stripped.startswith("//"):
                continue
            # Remove inline comments
            comment_idx = line.find("//")
            if comment_idx >= 0:
                line = line[:comment_idx]
            clean_lines.append(line)
        clean_text = "\n".join(clean_lines)

        for m in pattern.finditer(clean_text):
            used.add(m.group(1))

    return used


def find_bit_extraction_patterns(
    rtl_dir: Path,
    enum_name: str,
) -> List[Tuple[str, str]]:
    """Find bit-extraction patterns on signals of a given enum type.

    Looks for patterns like: signal_name[N], signal_name[N:M]
    where signal_name is a known carrier of the enum type.

    Returns list of (pattern_string, filename) tuples.
    """
    signal_patterns = _ENUM_SIGNAL_PATTERNS.get(enum_name, [])
    if not signal_patterns:
        return []

    constraints = []

    # Build regex for bit extraction on these signals
    signal_re = "|".join(signal_patterns)
    extraction_pattern = re.compile(rf"\b({signal_re})\s*\[(\d+(?:\s*:\s*\d+)?)\]")

    for sv_file in rtl_dir.rglob("*.sv"):
        if sv_file.name == "cv32e40p_pkg.sv":
            continue

        try:
            text = sv_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        # Remove comments
        text_clean = re.sub(r"//[^\n]*", "", text)
        text_clean = re.sub(r"/\*.*?\*/", "", text_clean, flags=re.DOTALL)

        for m in extraction_pattern.finditer(text_clean):
            signal = m.group(1)
            bits = m.group(2)
            rel_path = sv_file.relative_to(rtl_dir)
            constraints.append((f"{signal}[{bits}]", str(rel_path)))

    return constraints


def analyze_enum_usage(
    rtl_dir: Path,
    enum_types: Optional[List[str]] = None,
) -> EnumUsageReport:
    """Analyze enum usage across all RTL files in rtl_dir.

    Args:
        rtl_dir: Path to the pruned RTL output directory (e.g. output/X/rtl_modified)
        enum_types: Optional list of enum type names to analyze.
                   If None, analyzes all optimizable types.

    Returns:
        EnumUsageReport with results for each enum type.
    """
    pkg_path = rtl_dir / "rtl" / "include" / "cv32e40p_pkg.sv"
    if not pkg_path.exists():
        # Try without rtl/ prefix
        pkg_path = rtl_dir / "include" / "cv32e40p_pkg.sv"
    if not pkg_path.exists():
        print(f"  Warning: cv32e40p_pkg.sv not found in {rtl_dir}")
        return EnumUsageReport()

    # Parse all enums from package
    all_enums = parse_pkg_enums(pkg_path)

    # Default: analyze the optimizable enum types
    # Skip alu_opcode_e (has bit-extraction constraints + fused instruction interaction)
    # Skip debug_state_e (intentionally one-hot), FS_t, PrivLvl_t (fixed by spec),
    # csr_num_e (fixed by RISC-V spec), csr_opcode_e (fully used)
    optimizable = {"mul_opcode_e", "ctrl_state_e", "mult_state_e"}

    if enum_types:
        target_types = set(enum_types) & set(all_enums.keys())
    else:
        target_types = optimizable & set(all_enums.keys())

    report = EnumUsageReport()

    # Determine scan root (the directory containing rtl/)
    if (rtl_dir / "rtl").exists():
        scan_root = rtl_dir / "rtl"
    else:
        scan_root = rtl_dir

    for type_name in sorted(target_types):
        enum_def = all_enums[type_name]

        # Find which members are used
        used = find_used_members(scan_root, enum_def, exclude_pkg=True)

        # Also scan the include dir (for cross-references)
        include_dir = scan_root / "include"
        if include_dir.exists():
            # Don't re-scan pkg itself (exclude_pkg=True handles it)
            pass

        unused = set(enum_def.members.keys()) - used

        # Find bit-extraction constraints
        constraints = find_bit_extraction_patterns(scan_root, type_name)

        # Calculate minimum width
        n_used = len(used) if used else 1
        min_width = max(1, math.ceil(math.log2(max(n_used, 2))))

        can_reduce = min_width < enum_def.width

        result = EnumUsageResult(
            enum_def=enum_def,
            used_members=used,
            unused_members=unused,
            current_width=enum_def.width,
            min_width=min_width,
            can_reduce=can_reduce,
            bit_extraction_constraints=constraints,
        )
        report.results[type_name] = result

    return report
