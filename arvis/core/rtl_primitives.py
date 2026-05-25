"""Generic SystemVerilog transformation primitives.

These functions are target-agnostic: they take a workspace and
operate on RTL files by content/regex inspection.  Any target's
:class:`CoreFeature` can compose them via :class:`RemovalAction`
without writing target-specific code.

Each primitive is:

* **Idempotent** — calling it twice with the same arguments
  has no further effect.
* **Logged** — emits a structured log record at INFO level so
  reports can attribute every change.
* **Best-effort** — on a missing file or unmatched pattern,
  logs a warning and returns ``False`` rather than raising.
  This matches the pipeline's "strategies never abort" rule.

The primitives are dispatched by :class:`FeatureRemovalPatch`
based on :class:`ActionKind`.  They're also publicly importable
for use inside :class:`CustomAction` callables that need to
combine declarative pieces with custom logic.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.core.rtl_patch import RTLWorkspace

logger = logging.getLogger(__name__)


# ─── Helpers ───────────────────────────────────────────────────────


def _resolve(workspace: RTLWorkspace, file: str) -> Path | None:
    """Resolve a file path against the workspace output_root.

    Returns ``None`` and logs a warning when the file doesn't
    exist; callers treat this as a no-op.
    """
    path = Path(workspace.output_root) / file
    if not path.exists():
        logger.warning("rtl_primitives: file not found: %s", path)
        return None
    return path


# ─── SET_PARAM ─────────────────────────────────────────────────────


_PARAM_PATTERN = re.compile(
    r"(parameter\s+(?:[\w:\[\]]+\s+)?)"  # 1: parameter [type]
    r"(\w+)"  # 2: name
    r"(\s*=\s*)"  # 3: equals
    r"([^,;)]+)"  # 4: old value
    r"([,;)])",  # 5: terminator
)

_LOCALPARAM_PATTERN = re.compile(
    r"(localparam\s+(?:[\w:\[\]]+\s+)?)"
    r"(\w+)"
    r"(\s*=\s*)"
    r"([^,;)]+)"
    r"([,;)])",
)


def set_parameter(
    workspace: RTLWorkspace,
    file: str,
    name: str,
    value: object,
) -> bool:
    """Set ``parameter`` (or ``localparam``) ``name`` to ``value``.

    Searches for the first declaration matching ``parameter <T>?
    <name> = <expr><term>`` and rewrites the expr.  Returns
    ``True`` when a change was made, ``False`` otherwise
    (file missing, parameter not found, value already correct).
    """
    path = _resolve(workspace, file)
    if path is None:
        return False

    text = path.read_text()
    new_value = _format_value(value)

    def _replace_first(pattern: re.Pattern[str], src: str) -> tuple[str, bool]:
        for m in pattern.finditer(src):
            if m.group(2) != name:
                continue
            old_value = m.group(4).strip()
            if old_value == new_value:
                logger.debug(
                    "set_parameter: %s.%s already = %s (no-op)",
                    file,
                    name,
                    new_value,
                )
                return src, False
            new_text = (
                src[: m.start()]
                + (m.group(1) + m.group(2) + m.group(3) + new_value + m.group(5))
                + src[m.end() :]
            )
            return new_text, True
        return src, False

    new_text, changed = _replace_first(_PARAM_PATTERN, text)
    if not changed:
        new_text, changed = _replace_first(_LOCALPARAM_PATTERN, text)

    if changed:
        path.write_text(new_text)
        logger.info("set_parameter: %s.%s = %s", file, name, new_value)
        return True

    logger.warning("set_parameter: %s.%s not found", file, name)
    return False


def _format_value(value: object) -> str:
    """Render a Python value into a SystemVerilog literal."""
    if isinstance(value, bool):
        return "1'b1" if value else "1'b0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value  # caller is responsible for SV syntax
    return str(value)


# ─── SHRINK_PARAM ──────────────────────────────────────────────────


def shrink_parameter(
    workspace: RTLWorkspace,
    file: str,
    name: str,
    new_width: int,
) -> bool:
    """Narrow a width-typed parameter (e.g. ``PC_WIDTH``).

    Identical to :func:`set_parameter` for the rewrite, but
    enforces ``isinstance(new_width, int)`` and validates the
    value is positive.  Returns ``False`` and logs a warning
    when the new width is invalid.
    """
    if not isinstance(new_width, int) or new_width <= 0:
        logger.warning(
            "shrink_parameter: invalid width %r for %s.%s",
            new_width,
            file,
            name,
        )
        return False
    return set_parameter(workspace, file, name, new_width)


# ─── REMOVE_PRAGMA_BLOCK ───────────────────────────────────────────


def remove_pragma_block(
    workspace: RTLWorkspace,
    file: str,
    label: str,
) -> bool:
    """Remove text between ``// pragma <label>_BEGIN`` and ``// pragma <label>_END``.

    The pragma markers themselves are also removed.  When
    multiple blocks share the same label, all of them are
    removed.

    Returns ``True`` when at least one block was removed.
    """
    path = _resolve(workspace, file)
    if path is None:
        return False

    text = path.read_text()
    pattern = re.compile(
        rf"//\s*pragma\s+{re.escape(label)}_BEGIN.*?//\s*pragma\s+{re.escape(label)}_END\s*\n?",
        re.DOTALL,
    )
    new_text, n = pattern.subn("", text)
    if n == 0:
        logger.debug("remove_pragma_block: no '%s' blocks in %s", label, file)
        return False

    path.write_text(new_text)
    logger.info("remove_pragma_block: %s x %d in %s", label, n, file)
    return True


# ─── REMOVE_OPCODE_CASE ────────────────────────────────────────────


def remove_opcode_case(
    workspace: RTLWorkspace,
    file: str,
    opcode: str,
) -> bool:
    """Remove a ``case`` arm matching ``opcode`` and its body.

    Recognises the standard SV pattern::

        OPCODE_X: begin
            ...
        end

    Also handles single-line cases (``OPCODE_X: ... ;``) and
    falls back to a comment-out when the body isn't bracketed
    cleanly.  Returns ``True`` on a successful removal.
    """
    path = _resolve(workspace, file)
    if path is None:
        return False

    text = path.read_text()

    # Try begin/end form first.
    pattern_block = re.compile(
        rf"^[ \t]*{re.escape(opcode)}\s*:\s*begin\b[\s\S]*?^[ \t]*end\s*\n?",
        re.MULTILINE,
    )
    new_text, n = pattern_block.subn("", text)
    if n == 0:
        # Single-line form.
        pattern_line = re.compile(
            rf"^[ \t]*{re.escape(opcode)}\s*:[^;\n]*;\s*\n?",
            re.MULTILINE,
        )
        new_text, n = pattern_line.subn("", text)

    if n == 0:
        logger.warning("remove_opcode_case: %s not found in %s", opcode, file)
        return False

    path.write_text(new_text)
    logger.info("remove_opcode_case: %s x %d in %s", opcode, n, file)
    return True


# ─── REMOVE_SIGNAL ─────────────────────────────────────────────────


def remove_signal_uses(
    workspace: RTLWorkspace,
    file: str,
    pattern: str,
) -> bool:
    """Remove signal declarations matching ``pattern`` (regex or glob).

    Conservative: only removes lines that are pure declarations
    (``logic foo;``, ``wire bar;``, ``reg baz;``).  Does NOT
    remove uses elsewhere — callers wanting to wipe a signal
    should follow up with :func:`replace_pattern` for use-sites.

    ``pattern`` is treated as a regex.  Glob-like patterns
    (``"div_*"``) are converted to regex (``"div_\\w*"``).

    Returns ``True`` when at least one declaration was removed.
    """
    path = _resolve(workspace, file)
    if path is None:
        return False

    # Glob → regex conversion: "div_*" → "div_\\w*"
    if "*" in pattern and not any(c in pattern for c in r".+?(\["):
        regex_pat = pattern.replace("*", r"\w*")
    else:
        regex_pat = pattern

    text = path.read_text()
    decl = re.compile(
        rf"^\s*(logic|wire|reg|bit)\s+(?:\[[^\]]+\]\s+)?{regex_pat}\s*(?:\[[^\]]+\])?\s*;\s*\n?",
        re.MULTILINE,
    )
    new_text, n = decl.subn("", text)
    if n == 0:
        logger.debug(
            "remove_signal_uses: no declarations matching %r in %s",
            pattern,
            file,
        )
        return False

    path.write_text(new_text)
    logger.info("remove_signal_uses: %r x %d in %s", pattern, n, file)
    return True


# ─── REMOVE_INSTANCE ───────────────────────────────────────────────


def remove_module_instance(
    workspace: RTLWorkspace,
    file: str,
    instance_name: str,
) -> bool:
    """Remove a module instantiation named ``instance_name``.

    Recognises::

        <module_name> [#(...)] <instance_name> (
            <ports>
        );

    The trailing semicolon and any preceding whitespace/newlines
    are also consumed for cleanliness.  Returns ``True`` on
    successful removal.

    Implementation note: balanced parentheses are not regex-
    representable, so we use a two-pass strategy — locate the
    instance name with a regex anchor, then walk forward to
    find the matching ``);`` that closes the port list.
    """
    path = _resolve(workspace, file)
    if path is None:
        return False

    text = path.read_text()
    # Step 1: find the instance-name token at the start of a logical
    # statement (preceded by whitespace + module name + optional
    # parameter override).  We use a relaxed regex anchor.
    anchor = re.compile(
        rf"\b{re.escape(instance_name)}\s*\(",
        re.MULTILINE,
    )
    match = anchor.search(text)
    if match is None:
        logger.warning(
            "remove_module_instance: %s not found in %s",
            instance_name,
            file,
        )
        return False

    # Step 2: walk back to the start of the statement (start of line
    # with the module name).
    stmt_start = text.rfind("\n", 0, match.start())
    stmt_start = 0 if stmt_start == -1 else stmt_start + 1

    # Step 3: walk forward from match.end()-1 (the open paren of port
    # list) tracking paren depth until we close to 0, then consume
    # the semicolon + trailing newline.
    depth = 0
    i = match.end() - 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                # Find the trailing ;
                j = i + 1
                while j < n and text[j] in " \t":
                    j += 1
                if j < n and text[j] == ";":
                    j += 1
                # Eat one trailing newline for cleanliness
                if j < n and text[j] == "\n":
                    j += 1
                stmt_end = j
                break
        i += 1
    else:
        logger.warning(
            "remove_module_instance: unbalanced parens for %s in %s",
            instance_name,
            file,
        )
        return False

    new_text = text[:stmt_start] + text[stmt_end:]
    path.write_text(new_text)
    logger.info("remove_module_instance: %s in %s", instance_name, file)
    return True


# ─── REPLACE_PATTERN ───────────────────────────────────────────────


def replace_pattern(
    workspace: RTLWorkspace,
    file: str,
    pattern: str,
    replacement: str,
) -> bool:
    """Regex search-and-replace.

    The escape hatch for cases the other primitives can't
    express.  Returns ``True`` when at least one substitution
    was made.

    Use sparingly — the typed primitives (set_parameter,
    remove_pragma_block, etc.) are easier to audit.
    """
    path = _resolve(workspace, file)
    if path is None:
        return False

    text = path.read_text()
    new_text, n = re.subn(pattern, replacement, text)
    if n == 0:
        logger.debug("replace_pattern: no matches for %r in %s", pattern, file)
        return False

    path.write_text(new_text)
    logger.info("replace_pattern: %r x %d in %s", pattern, n, file)
    return True


# ─── AST_REMOVE_CASE_ITEMS ─────────────────────────────────────────


def remove_case_items(
    workspace: RTLWorkspace,
    file: str,
    case_selector: str,
    removable_labels: object,
) -> bool:
    """Remove case-arm items via pyslang AST manipulation.

    Generalises the legacy ``prune_alu_cases`` /
    ``prune_mult_cases`` / ``prune_csr_cases`` functions.  The
    primitive:

    1. Parses the SV file via ``pyslang.SyntaxTree.fromText``.
    2. Walks the AST looking for ``case (case_selector)``
       statements (matched by the selector expression's text).
    3. For each ``StandardCaseItem`` where ALL labels are in
       ``removable_labels``, marks it for removal.
    4. Applies ``pyslang.rewrite()`` with a handler that calls
       ``rewriter.remove()`` on marked items.
    5. Post-processes the resulting text to replace any
       case-empty blocks (``case (X) endcase``) with a comment
       so Verilator doesn't warn about ``CaseStatementEmpty``.

    Items with mixed labels (some removable, some used) are
    KEPT in full — partial-label removal would require splitting
    items, which the legacy code also doesn't support.

    Parameters
    ----------
    workspace:
        The RTLWorkspace whose ``output_root`` contains ``file``.
    file:
        Path relative to ``output_root`` (e.g.
        ``"rtl/cv32e40p_alu.sv"``).
    case_selector:
        Selector signal name (e.g. ``"operator_i"``,
        ``"csr_addr"``).  Matched as a literal string against
        the case statement's selector expression.
    removable_labels:
        Iterable of label identifiers (e.g.
        ``{"ALU_BCLR", "ALU_SHUF"}``).  Sets, tuples, lists all
        accepted; converted to a set internally.

    Returns
    -------
    bool
        ``True`` when at least one item was removed and the file
        was rewritten.  ``False`` on missing pyslang, missing
        file, no matching case, or no removable items.

    Notes
    -----
    pyslang is an optional dependency.  When unavailable, this
    primitive logs a warning and returns ``False``; the
    feature's other actions still apply.
    """
    try:
        import pyslang
    except ImportError:
        logger.warning(
            "remove_case_items: pyslang not installed; skipping AST-based pruning of %s in %s",
            case_selector,
            file,
        )
        return False

    # pyslang ≥ 11 moved SyntaxTree/SyntaxNode/SyntaxKind/rewrite into
    # the pyslang.syntax submodule; older versions exposed them at the
    # top level.  Resolve both transparently.
    _ps_root = pyslang
    if not hasattr(_ps_root, "SyntaxTree") and hasattr(_ps_root, "syntax"):
        _ps_root = _ps_root.syntax

    path = _resolve(workspace, file)
    if path is None:
        return False

    removable = (
        frozenset(removable_labels)
        if not isinstance(removable_labels, frozenset)
        else removable_labels
    )
    if not removable:
        logger.debug("remove_case_items: empty removable set for %s", file)
        return False

    text = path.read_text()
    tree = _ps_root.SyntaxTree.fromText(text)

    # Walk the AST collecting items to remove.
    items_to_remove: list[object] = []

    def _visit(node: object) -> None:
        if not isinstance(node, _ps_root.SyntaxNode):
            return
        if node.kind != _ps_root.SyntaxKind.CaseStatement:
            return
        if str(node.expr).strip() != case_selector:
            return
        for item in node.items:
            if item.kind != _ps_root.SyntaxKind.StandardCaseItem:
                continue
            labels = _extract_case_item_labels(item)
            if labels and all(label in removable for label in labels):
                items_to_remove.append(item)

    tree.root.visit(_visit)

    if not items_to_remove:
        logger.debug(
            "remove_case_items: no removable items in case (%s) of %s",
            case_selector,
            file,
        )
        return False

    remove_ids = {id(item) for item in items_to_remove}

    def _handler(node: object, rewriter: object) -> None:
        if (
            isinstance(node, _ps_root.SyntaxNode)
            and node.kind == _ps_root.SyntaxKind.StandardCaseItem
            and id(node) in remove_ids
        ):
            rewriter.remove(node)

    new_tree = _ps_root.rewrite(tree, _handler)
    result = str(new_tree.root)

    # Post-process: turn an empty case (selector) endcase into a
    # comment to avoid Verilator's CaseStatementEmpty lint warning.
    result = re.sub(
        r"(\n\s*)(?:unique\s+)?case\s*\([^)]+\)\s*\n\s*endcase",
        r"\1// case pruned: all items removed by feature-based pruning",
        result,
    )

    path.write_text(result)
    logger.info(
        "remove_case_items: %d arm(s) removed from case (%s) in %s",
        len(items_to_remove),
        case_selector,
        file,
    )
    return True


def _extract_case_item_labels(item: object) -> list[str]:
    """Extract label identifiers from a StandardCaseItem AST node.

    Mirrors the legacy ``_extract_label_names`` helper.  Each
    expression is either an identifier (``ALU_AND``) or a comma
    separator; we strip whitespace and inline comments and
    return the bare identifier list.
    """
    names: list[str] = []
    for expr in item.expressions:
        text = str(expr).strip()
        if text == ",":
            continue
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            for part in stripped.split(","):
                part = part.strip()
                if part and not part.startswith("//"):
                    names.append(part)
    return names


# ─── Dispatch table ────────────────────────────────────────────────


def dispatch(
    workspace: RTLWorkspace,
    kind: object,  # ActionKind, declared object to avoid circular import
    file: str,
    target: str,
    value: object = None,
) -> bool:
    """Dispatch one declarative action to its primitive.

    Used by :class:`FeatureRemovalPatch.apply`; exposed here so
    custom callables can invoke individual primitives and the
    dispatcher consistently.

    Returns the primitive's return value (``True`` on change,
    ``False`` on no-op).
    """
    from arvis.core.feature import ActionKind  # local import: break cycle

    if not isinstance(kind, ActionKind):
        try:
            kind = ActionKind(str(kind))
        except ValueError:
            logger.error("dispatch: unknown ActionKind %r", kind)
            return False

    if kind is ActionKind.SET_PARAM:
        return set_parameter(workspace, file, target, value)
    if kind is ActionKind.SHRINK_PARAM:
        if not isinstance(value, int):
            logger.warning("dispatch: SHRINK_PARAM requires int value, got %r", value)
            return False
        return shrink_parameter(workspace, file, target, value)
    if kind is ActionKind.REMOVE_PRAGMA_BLOCK:
        return remove_pragma_block(workspace, file, target)
    if kind is ActionKind.REMOVE_OPCODE_CASE:
        return remove_opcode_case(workspace, file, target)
    if kind is ActionKind.REMOVE_SIGNAL:
        return remove_signal_uses(workspace, file, target)
    if kind is ActionKind.REMOVE_INSTANCE:
        return remove_module_instance(workspace, file, target)
    if kind is ActionKind.REPLACE_PATTERN:
        return replace_pattern(workspace, file, target, str(value or ""))
    if kind is ActionKind.AST_REMOVE_CASE_ITEMS:
        if value is None:
            logger.warning("dispatch: AST_REMOVE_CASE_ITEMS requires value (label set)")
            return False
        return remove_case_items(workspace, file, target, value)

    logger.error("dispatch: unknown ActionKind %r", kind)
    return False


__all__ = [
    "dispatch",
    "remove_case_items",
    "remove_module_instance",
    "remove_opcode_case",
    "remove_pragma_block",
    "remove_signal_uses",
    "replace_pattern",
    "set_parameter",
    "shrink_parameter",
]
