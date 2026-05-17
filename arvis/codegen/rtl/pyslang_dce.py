"""
Pyslang Dead Code Elimination (DCE) cleanup pass.

Runs AFTER all pruning (parameters, pragmas, case removal) to remove
orphaned signal declarations and assignments that are no longer
referenced anywhere in the file.

This is the RTL-source-level equivalent of Yosys `opt_clean`, ensuring
that Verilator also benefits from dead code removal.

Algorithm:
  1. Parse file with pyslang
  2. Collect all local signal names (from declarations)
  3. For each signal, check if it appears on any RHS / in any expression
  4. Remove declarations + assignments for unreferenced signals
  5. Repeat until no more removals (cascading DCE)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


@dataclass
class DCEStats:
    """Statistics from a DCE pass."""

    file: str
    signals_removed: List[str] = field(default_factory=list)
    lines_removed: int = 0
    iterations: int = 0


def _find_declared_signals(lines: List[str]) -> Dict[str, int]:
    """Find all local signal declarations and their line indices.

    Returns dict of signal_name → line_index for `logic` declarations.
    Skips input/output ports (they're module interface, not local).
    Handles comma-separated declarations: `logic [31:0] a, b;`
    """
    signals: Dict[str, int] = {}
    _SKIP_NAMES = {"begin", "end", "if", "else", "case", "assign", "genvar"}

    # Match the full declaration line including all names
    decl_re = re.compile(
        r"^\s+logic\s+"  # leading whitespace + logic keyword
        r"(?:\[[^\]]+\]\s*)*"  # optional packed dimensions [31:0]
        r"([\w\s,\[\]:]+?)"  # signal name(s) — may be comma-separated
        r"\s*;"  # ends with ;
    )

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Skip ports, parameters, comments
        if stripped.startswith("input ") or stripped.startswith("output "):
            continue
        if stripped.startswith("//") or stripped.startswith("parameter"):
            continue
        if "ARVIS_" in stripped:
            continue
        # Strip inline comments before matching
        code_part = line.split("//")[0]
        if "logic" not in code_part:
            continue

        m = decl_re.match(code_part)
        if m:
            names_part = m.group(1)
            # Extract individual signal names (skip dimensions like [3:0])
            for token in names_part.split(","):
                token = token.strip()
                # Remove any unpacked dimensions like [3:0][1:0]
                name = re.sub(r"\[.*", "", token).strip()
                if name and name not in _SKIP_NAMES and re.match(r"^\w+$", name):
                    signals[name] = i

    return signals


def _is_signal_referenced(name: str, lines: List[str], decl_line: int) -> bool:
    """Check if a signal name appears anywhere except its own declaration.

    Uses word boundary matching to avoid false positives (e.g.,
    'shift_result' matching 'shift_result_expanded').
    """
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")

    for i, line in enumerate(lines):
        if i == decl_line:
            continue
        # Skip comments
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        if pattern.search(line):
            return True
    return False


def _remove_dead_assignments(lines: List[str], dead_signals: Set[str]) -> Tuple[List[str], int]:
    """Remove `assign signal_name = ...;` lines for dead signals."""
    result = []
    removed = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        line.strip()

        # Check for assign to dead signal
        assign_match = re.match(r"\s*assign\s+(\w+)", line)
        if assign_match and assign_match.group(1) in dead_signals:
            # Multi-line assign: skip until semicolon
            while i < len(lines) and ";" not in lines[i]:
                i += 1
            i += 1  # skip the line with semicolon
            removed += 1
            continue

        result.append(line)
        i += 1

    return result, removed


def run_dce_pass(sv_text: str, filename: str = "") -> Tuple[str, DCEStats]:
    """Run iterative dead code elimination on a single file.

    Args:
        sv_text: Full SystemVerilog source text
        filename: Filename for reporting

    Returns:
        (modified_text, stats)
    """
    stats = DCEStats(file=filename)
    lines = sv_text.split("\n")

    max_iterations = 5  # prevent infinite loops

    for iteration in range(max_iterations):
        declared = _find_declared_signals(lines)

        if not declared:
            break

        dead: Set[str] = set()
        for name, decl_line in declared.items():
            if not _is_signal_referenced(name, lines, decl_line):
                dead.add(name)

        if not dead:
            break

        stats.iterations += 1

        # Remove declarations for dead signals
        dead_decl_lines = {declared[name] for name in dead if name in declared}
        new_lines = []
        for i, line in enumerate(lines):
            if i in dead_decl_lines:
                stats.lines_removed += 1
                continue
            new_lines.append(line)

        # Remove assignments to dead signals
        new_lines, n_assigns = _remove_dead_assignments(new_lines, dead)
        stats.lines_removed += n_assigns

        stats.signals_removed.extend(sorted(dead))
        lines = new_lines

    return "\n".join(lines), stats


def run_dce_on_directory(rtl_dir: Path) -> List[DCEStats]:
    """Run DCE on all .sv files in the RTL directory.

    Args:
        rtl_dir: Path to rtl/ directory (output/rtl_modified/rtl/)

    Returns:
        List of DCEStats for files that had removals
    """
    all_stats: List[DCEStats] = []

    # Process all .sv files in the directory (skip packages)
    skip_files = {
        "cv32e40p_pkg.sv",
        "cv32e40p_apu_core_pkg.sv",
        "cv32e40p_fpu_pkg.sv",
    }

    for filepath in sorted(rtl_dir.glob("*.sv")):
        if filepath.name in skip_files:
            continue
        filename = filepath.name

        text = filepath.read_text()
        modified, stats = run_dce_pass(text, filename)

        if stats.signals_removed:
            filepath.write_text(modified)
            all_stats.append(stats)

    return all_stats
