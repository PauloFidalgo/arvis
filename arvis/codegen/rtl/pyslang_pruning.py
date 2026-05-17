"""
Advanced RTL pruning using pyslang AST-based rewriting.

Uses pyslang's SyntaxTree parser and rewrite() API for precise,
structure-aware transformations on SystemVerilog files. This is more
reliable than regex-based pruning because it operates on the actual
parse tree — correctly handling multi-line case items, nested
begin/end blocks, and comma-separated case labels.

Pruning passes implemented:
  Pass A: ALU case-item pruning (all `case (operator_i)` in cv32e40p_alu.sv)
  Pass B: CSR register pruning (MHPM counters, PMP, debug triggers)
  Pass C: Multiplier case-item pruning (MUL_DOT8/MUL_DOT16)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

import pyslang

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _extract_label_names(item: pyslang.SyntaxNode) -> List[str]:
    """Extract ALU_* / CSR_* / MUL_* identifier names from a StandardCaseItem.

    Each expression in the case item's expression list is either an
    identifier like ``ALU_AND`` or a comma token separator. We extract
    just the identifier text, stripping whitespace and comments.
    """
    names: list[str] = []
    if item.kind != pyslang.SyntaxKind.StandardCaseItem:
        return names

    for expr in item.expressions:
        text = str(expr).strip()
        # Skip comma separators
        if text == ",":
            continue
        # Strip leading comments (pyslang includes trivia in str())
        # e.g. "// Standard Operations\n      ALU_AND" → "ALU_AND"
        lines = text.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            # May have multiple identifiers on same line separated by commas
            for part in stripped.split(","):
                part = part.strip()
                if part and not part.startswith("//"):
                    names.append(part)
    return names


def _case_selects_on(case_node: pyslang.SyntaxNode, signal_name: str) -> bool:
    """Check if a CaseStatement selects on a given signal name."""
    expr_str = str(case_node.expr).strip()
    return expr_str == signal_name


@dataclass
class PyslangPruneStats:
    """Statistics from a pyslang-based pruning pass."""

    file: str
    items_removed: int = 0
    items_total: int = 0
    labels_removed: List[str] = field(default_factory=list)
    description: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Pass A: ALU Case-Item Pruning
# ═══════════════════════════════════════════════════════════════════════════


def prune_alu_cases(sv_text: str, removable_ops: Set[str]) -> Tuple[str, PyslangPruneStats]:
    """Remove unused ALU operation case items from cv32e40p_alu.sv.

    Uses pyslang to parse the file and precisely identify all
    ``case (operator_i)`` statements. For each StandardCaseItem whose
    labels are ALL in ``removable_ops``, the item is removed from the
    AST via ``rewriter.remove()``.

    This handles:
      - Single-label items: ``ALU_BCLR: result_o = bclr_result;``
      - Multi-label items: ``ALU_MIN, ALU_MINU, ALU_MAX, ALU_MAXU: ...``
      - Multi-line begin/end blocks
      - Multiple case statements (result mux, cmp_signed, cmp_result, etc.)
      - Items that mix removable and non-removable labels (kept entirely —
        partial label removal would require deeper AST surgery)

    Parameters
    ----------
    sv_text : str
        Full text of cv32e40p_alu.sv
    removable_ops : set[str]
        ALU opcode names that are unused (e.g. ``{'ALU_BCLR', 'ALU_SHUF', ...}``)

    Returns
    -------
    (modified_text, stats)
    """
    stats = PyslangPruneStats(
        file="rtl/cv32e40p_alu.sv",
        description="ALU case-item pruning (pyslang)",
    )

    if not removable_ops:
        return sv_text, stats

    tree = pyslang.SyntaxTree.fromText(sv_text)

    # Collect all case items to remove
    items_to_remove: List[pyslang.SyntaxNode] = []
    total_operator_items = 0

    def find_removable(node):
        """Visit all nodes, find case(operator_i) items with removable labels."""
        if not isinstance(node, pyslang.SyntaxNode):
            return
        if node.kind == pyslang.SyntaxKind.CaseStatement:
            if _case_selects_on(node, "operator_i"):
                for item in node.items:
                    if item.kind == pyslang.SyntaxKind.StandardCaseItem:
                        nonlocal total_operator_items
                        total_operator_items += 1
                        labels = _extract_label_names(item)
                        if labels and all(lbl in removable_ops for lbl in labels):
                            items_to_remove.append(item)
                            stats.labels_removed.extend(labels)

    tree.root.visit(find_removable)

    stats.items_total = total_operator_items
    stats.items_removed = len(items_to_remove)

    if not items_to_remove:
        return sv_text, stats

    # Build a set of node IDs to remove for O(1) lookup
    remove_ids = {id(item) for item in items_to_remove}

    def handler(node, rewriter):
        if isinstance(node, pyslang.SyntaxNode):
            if node.kind == pyslang.SyntaxKind.StandardCaseItem:
                if id(node) in remove_ids:
                    rewriter.remove(node)

    new_tree = pyslang.rewrite(tree, handler)
    result_text = str(new_tree.root)

    # Post-process: remove empty case blocks that result from full pruning.
    # When ALL items in a case(operator_i) are removed, we get:
    #   always_comb begin
    #     ff_input = '0;
    #     case (operator_i)
    #     endcase
    #   end
    # Replace empty case blocks with a comment to avoid CaseStatementEmpty warnings.
    result_text = re.sub(
        r"(\n\s*)(?:unique\s+)?case\s*\([^)]+\)\s*\n\s*endcase",
        r"\1// case pruned: all items removed by workload specialization",
        result_text,
    )

    return result_text, stats


# ═══════════════════════════════════════════════════════════════════════════
# Pass B: CSR Register Case-Item Pruning
# ═══════════════════════════════════════════════════════════════════════════

# CSR groups that can be pruned when features are disabled
_HPM_COUNTER_CSRS = set()
_HPM_COUNTERH_CSRS = set()
_HPM_EVENT_CSRS = set()
for _n in range(3, 32):
    _HPM_COUNTER_CSRS.add(f"CSR_MHPMCOUNTER{_n}")
    _HPM_COUNTERH_CSRS.add(f"CSR_MHPMCOUNTER{_n}H")
    _HPM_EVENT_CSRS.add(f"CSR_MHPMEVENT{_n}")

_ALL_HPM_CSRS = _HPM_COUNTER_CSRS | _HPM_COUNTERH_CSRS | _HPM_EVENT_CSRS

_DEBUG_CSRS = {
    "CSR_DCSR",
    "CSR_DPC",
    "CSR_DSCRATCH0",
    "CSR_DSCRATCH1",
    "CSR_TSELECT",
    "CSR_TDATA1",
    "CSR_TDATA2",
    "CSR_TDATA3",
    "CSR_TINFO",
}

_PMP_CSRS = set()
for _n in range(4):
    _PMP_CSRS.add(f"CSR_PMPCFG{_n}")
for _n in range(16):
    _PMP_CSRS.add(f"CSR_PMPADDR{_n}")


def prune_csr_cases(
    sv_text: str,
    remove_hpm: bool = False,
    remove_debug: bool = False,
    remove_pmp: bool = False,
) -> Tuple[str, PyslangPruneStats]:
    """Remove unused CSR case items from cv32e40p_cs_registers.sv.

    The CSR register file uses large ``case (csr_addr_i)`` blocks for
    reading and writing CSR values. Many of these are for optional
    features (HPM counters, debug triggers, PMP) that may be unused.

    Parameters
    ----------
    sv_text : str
        Full text of cv32e40p_cs_registers.sv
    remove_hpm : bool
        Remove MHPMCOUNTER/MHPMEVENT CSR case items
    remove_debug : bool
        Remove DCSR/DPC/DSCRATCH/TDATA CSR case items
    remove_pmp : bool
        Remove PMPCFG/PMPADDR CSR case items

    Returns
    -------
    (modified_text, stats)
    """
    stats = PyslangPruneStats(
        file="rtl/cv32e40p_cs_registers.sv",
        description="CSR case-item pruning (pyslang)",
    )

    removable_csrs: Set[str] = set()
    if remove_hpm:
        removable_csrs |= _ALL_HPM_CSRS
    if remove_debug:
        removable_csrs |= _DEBUG_CSRS
    if remove_pmp:
        removable_csrs |= _PMP_CSRS

    if not removable_csrs:
        return sv_text, stats

    tree = pyslang.SyntaxTree.fromText(sv_text)

    items_to_remove: List[pyslang.SyntaxNode] = []
    total_csr_items = 0

    def find_removable(node):
        """Find case items in case(csr_addr_i) with removable CSR labels."""
        if not isinstance(node, pyslang.SyntaxNode):
            return
        if node.kind == pyslang.SyntaxKind.CaseStatement:
            expr_str = str(node.expr).strip()
            if "csr_addr_i" in expr_str:
                for item in node.items:
                    if item.kind == pyslang.SyntaxKind.StandardCaseItem:
                        nonlocal total_csr_items
                        total_csr_items += 1
                        labels = _extract_label_names(item)
                        if labels and all(lbl in removable_csrs for lbl in labels):
                            items_to_remove.append(item)
                            stats.labels_removed.extend(labels)

    tree.root.visit(find_removable)

    stats.items_total = total_csr_items
    stats.items_removed = len(items_to_remove)

    if not items_to_remove:
        return sv_text, stats

    remove_ids = {id(item) for item in items_to_remove}

    def handler(node, rewriter):
        if isinstance(node, pyslang.SyntaxNode):
            if node.kind == pyslang.SyntaxKind.StandardCaseItem:
                if id(node) in remove_ids:
                    rewriter.remove(node)

    new_tree = pyslang.rewrite(tree, handler)
    return str(new_tree.root), stats


# ═══════════════════════════════════════════════════════════════════════════
# Pass B2: Workload-driven CSR Case-Item Pruning
# ═══════════════════════════════════════════════════════════════════════════


def prune_csr_cases_by_labels(
    sv_text: str,
    removable_labels: Set[str],
) -> Tuple[str, PyslangPruneStats]:
    """Remove individual CSR case items by label name.

    Unlike prune_csr_cases() which removes entire feature groups,
    this function removes individual CSR addresses that the workload
    analysis determined are unused. This is workload-specific.

    Parameters
    ----------
    sv_text : str
        Full text of cv32e40p_cs_registers.sv
    removable_labels : set[str]
        CSR label names to remove (e.g. ``{'CSR_MSCRATCH', 'CSR_MISA', ...}``)
    """
    stats = PyslangPruneStats(
        file="rtl/cv32e40p_cs_registers.sv",
        description="Workload-driven CSR case-item pruning",
    )

    if not removable_labels:
        return sv_text, stats

    tree = pyslang.SyntaxTree.fromText(sv_text)

    items_to_remove: List[pyslang.SyntaxNode] = []
    total_csr_items = 0

    def find_removable(node):
        if not isinstance(node, pyslang.SyntaxNode):
            return
        if node.kind == pyslang.SyntaxKind.CaseStatement:
            expr_str = str(node.expr).strip()
            # Match both csr_addr_i (read mux) and the write case
            if "csr_addr" in expr_str or "instr_rdata_i[31:20]" in expr_str:
                for item in node.items:
                    if item.kind == pyslang.SyntaxKind.StandardCaseItem:
                        nonlocal total_csr_items
                        total_csr_items += 1
                        labels = _extract_label_names(item)
                        if labels and all(lbl in removable_labels for lbl in labels):
                            items_to_remove.append(item)
                            stats.labels_removed.extend(labels)

    tree.root.visit(find_removable)

    stats.items_total = total_csr_items
    stats.items_removed = len(items_to_remove)

    if not items_to_remove:
        return sv_text, stats

    remove_ids = {id(item) for item in items_to_remove}

    def handler(node, rewriter):
        if isinstance(node, pyslang.SyntaxNode):
            if node.kind == pyslang.SyntaxKind.StandardCaseItem:
                if id(node) in remove_ids:
                    rewriter.remove(node)

    new_tree = pyslang.rewrite(tree, handler)
    return str(new_tree.root), stats


# ═══════════════════════════════════════════════════════════════════════════
# Pass C: Multiplier Case-Item Pruning
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Pass D: Comparison-logic pruning in ALU
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator: apply all pyslang passes
# ═══════════════════════════════════════════════════════════════════════════


def apply_pyslang_pruning(
    output_root: Path,
    removable_alu_ops: Set[str],
    enable_hpm: bool = True,
    enable_debug: bool = True,
    corev_pulp: int = 0,
    removable_csr_labels: Optional[Set[str]] = None,
) -> List[PyslangPruneStats]:
    """Apply all pyslang-based pruning passes to the RTL workspace.

    This function is called from ``RTLPruner.apply()`` after the
    existing parameter-gating passes. It performs precise AST-level
    case-item removal using pyslang's ``rewrite()`` API.

    Parameters
    ----------
    output_root : Path
        Root of the modified RTL workspace (e.g. ``output/rtl_modified/``)
    removable_alu_ops : set[str]
        ALU opcode identifiers to remove (from PruneConfig)
    enable_hpm : bool
        Whether HPM counters are used (False → prune HPM CSRs)
    enable_debug : bool
        Whether debug features are used (False → prune debug CSRs)
    corev_pulp : int
        COREV_PULP parameter value (0 → prune MUL_DOT* ops)

    Returns
    -------
    list[PyslangPruneStats]
        Statistics from each pass
    """
    all_stats: List[PyslangPruneStats] = []

    # ── Pass A: ALU case-item pruning ──
    alu_path = output_root / "rtl" / "cv32e40p_alu.sv"
    if alu_path.exists() and removable_alu_ops:
        text = alu_path.read_text()
        modified, stats = prune_alu_cases(text, removable_alu_ops)
        if stats.items_removed > 0:
            alu_path.write_text(modified)
        all_stats.append(stats)

    # ── Pass B: CSR case-item pruning (feature-group based) ──
    csr_path = output_root / "rtl" / "cv32e40p_cs_registers.sv"
    remove_hpm = not enable_hpm
    remove_debug_csrs = False  # Debug CSRs handled by ARVIS_DBG pragmas
    if csr_path.exists() and remove_hpm:
        text = csr_path.read_text()
        modified, stats = prune_csr_cases(
            text,
            remove_hpm=remove_hpm,
            remove_debug=remove_debug_csrs,
            remove_pmp=False,
        )
        if stats.items_removed > 0:
            csr_path.write_text(modified)
        all_stats.append(stats)

    # ── Pass B2: Workload-driven CSR case-item pruning ──
    # Remove individual CSR read/write case entries based on analysis
    if csr_path.exists() and removable_csr_labels:
        text = csr_path.read_text()
        modified, stats = prune_csr_cases_by_labels(text, removable_csr_labels)
        if stats.items_removed > 0:
            csr_path.write_text(modified)
        all_stats.append(stats)

    # ── Pass C: Multiplier case-item pruning ──
    # DISABLED: DOT/MSU now handled by ENABLE_DOT_MUL/ENABLE_MSU
    # parameters via ternary gating in rtl_pruning.py.
    # Removing case items here would make the parameter ineffective.

    return all_stats
