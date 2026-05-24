"""Target-agnostic RTL patch protocol.

A :class:`RTLPatch` describes one self-contained edit to an RTL
tree.  Patches are produced by :meth:`TargetCore.render_decision`
and applied by :meth:`Pipeline.run`.  Their *only* job is to mutate
files inside an :class:`RTLWorkspace`; they have no opinions about
what they're patching for, which makes them composable across
strategies.

This module is target-agnostic.  A cv32e40p-specific patch
(e.g. ``set parameter PC_WIDTH = N in cv32e40p_top.sv``) is just an
``RTLPatch`` subclass that knows the file name and the regex.
Other cores use the same interface with their own subclasses.

Patches must be **idempotent**: running the same patch twice on the
same workspace must be a no-op on the second run.  This lets the
pipeline freely re-run patches when verifying intermediate state,
and matches how ``apply_pc_width.patch_rtl_dir`` behaves today.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class RTLWorkspace:
    """A working copy of a target's RTL tree.

    A workspace is created by copying :attr:`TargetCore.rtl_root`
    into an output directory.  All :class:`RTLPatch` objects mutate
    files inside that directory; the original templates are never
    modified.

    The workspace also offers a small file I/O API so that patches
    don't need to know the absolute path of the workspace root.

    Patches in the same variant emission may need to share state
    (e.g. a custom-instruction encoding registry computed once and
    consumed by both the fusion and hwloop renderings).
    :attr:`metadata` is a mutable per-workspace dict for exactly
    that: the pipeline populates it before applying patches, and
    patches read it via well-known string keys.  Cross-workspace
    leakage is impossible because each variant gets a fresh
    workspace instance.
    """

    def __init__(self, source_root: Path, output_root: Path):
        self.source_root = Path(source_root)
        self.output_root = Path(output_root)
        self.metadata: dict[str, Any] = {}

    # ── Lifecycle ──────────────────────────────────────────────────
    def copy_fresh(self) -> None:
        """Wipe ``output_root`` and re-copy the templates.

        Called at the start of every variant emission so each
        variant starts from a clean slate.  Mirrors today's
        :class:`pipeline.rtl_changeset.RTLChangeSet` Step 1.
        """
        if self.output_root.exists():
            shutil.rmtree(self.output_root)
        shutil.copytree(self.source_root, self.output_root)

    # ── File I/O ───────────────────────────────────────────────────
    def read(self, rel_path: str) -> str:
        """Read a file inside the workspace as text."""
        return (self.output_root / rel_path).read_text()

    def write(self, rel_path: str, content: str) -> None:
        """Write a file inside the workspace.

        Parent directories are created if missing.  Mirrors
        :meth:`pathlib.Path.write_text`.
        """
        full = self.output_root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def exists(self, rel_path: str) -> bool:
        return (self.output_root / rel_path).exists()

    def file_path(self, rel_path: str) -> Path:
        """Return the absolute path of a file inside the workspace.

        Useful for patches that need to call out to subprocesses
        (objdump, sed, etc.) on a file by path.
        """
        return self.output_root / rel_path


# ─── Patch hierarchy ───────────────────────────────────────────────


class RTLPatch(ABC):
    """One self-contained, idempotent edit to an RTL workspace.

    Concrete subclasses live in target-specific packages
    (e.g. ``targets/cv32e40p/patches.py``) because the regex
    patterns and file names are core-specific.  The base class only
    requires :meth:`apply` and a stable :attr:`label` for reporting.
    """

    # ── Identity ───────────────────────────────────────────────────
    @property
    def label(self) -> str:
        """Short string used in reports and logs."""
        return self.__class__.__name__

    # ── Behaviour ──────────────────────────────────────────────────
    @abstractmethod
    def apply(self, workspace: RTLWorkspace) -> None:
        """Mutate ``workspace`` to apply this patch.

        Implementations MUST be idempotent: a second invocation on
        an already-patched workspace must leave it unchanged.
        """
        ...


# ─── Helpful base patches that most targets will subclass ──────────


class CompositePatch(RTLPatch):
    """A patch that is the in-order composition of other patches.

    Useful for grouping patches that share a label but live in
    different files (e.g. "narrow PC width" touches ten files).
    Idempotency is preserved as long as each child patch is
    idempotent.
    """

    def __init__(self, label: str, children: Iterable[RTLPatch]):
        self._label = label
        self._children = list(children)

    @property
    def label(self) -> str:
        return self._label

    def apply(self, workspace: RTLWorkspace) -> None:
        for child in self._children:
            child.apply(workspace)


class NoOpPatch(RTLPatch):
    """A patch that does nothing.

    Useful as a placeholder return from
    :meth:`TargetCore.render_decision` when a decision is empty or
    not yet implemented.
    """

    @property
    def label(self) -> str:
        return "noop"

    def apply(self, workspace: RTLWorkspace) -> None:
        return
