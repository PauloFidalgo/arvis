"""
Base classes for RTL code generation.

RTLWorkspace: manages a working copy of the cv32e40p RTL tree,
applies modifications via ARVIS pragmas, and compiles with Verilator.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


class RTLWorkspace:
    """
    A working copy of the cv32e40p RTL that can be modified and compiled.

    Usage:
        ws = RTLWorkspace('targets/cv32e40p', 'output/my_run/rtl')
        ws.copy()  # Copies entire cv32e40p tree to output dir
        ws.patch_between_pragmas('rtl/cv32e40p_alu.sv', 'result_mux', new_code)
        ws.compile_verilator('output/my_run/sim/obj_dir')
    """

    def __init__(self, source_root: str, output_root: str):
        """
        Args:
            source_root: Path to original cv32e40p (contains rtl/, bhv/, example_tb/)
            output_root: Path where the working copy will be created
        """
        self.source_root = Path(source_root).resolve()
        self.output_root = Path(output_root).resolve()
        self._copied = False

    @property
    def rtl_dir(self) -> Path:
        return self.output_root / "rtl"

    @property
    def bhv_dir(self) -> Path:
        return self.output_root / "bhv"

    @property
    def tb_dir(self) -> Path:
        return self.output_root / "example_tb" / "core"

    def copy(self, *, verbose: bool = True) -> None:
        """Copy the entire cv32e40p tree to output_root."""
        if self.output_root.exists():
            shutil.rmtree(self.output_root, ignore_errors=True)
        shutil.copytree(self.source_root, self.output_root, symlinks=True)
        self._copied = True
        if verbose:
            print(f"  📁 Copied RTL tree: {self.source_root} → {self.output_root}")

    def _ensure_copied(self):
        if not self._copied and not self.output_root.exists():
            raise RuntimeError("RTL workspace not initialized. Call copy() first.")

    def read_file(self, rel_path: str) -> str:
        """Read a file from the workspace by relative path."""
        self._ensure_copied()
        return (self.output_root / rel_path).read_text()

    def patch_between_pragmas(self, rel_path: str, tag: str, new_content: str) -> None:
        """
        Replace content between ARVIS_FUSED_BEGIN/END pragma pairs
        in the specified file.

        Args:
            rel_path: Relative path within workspace (e.g., 'rtl/cv32e40p_alu.sv')
            tag: Pragma tag name (e.g., 'result_mux')
            new_content: New content to insert between the pragmas
        """
        self._ensure_copied()
        filepath = self.output_root / rel_path
        text = filepath.read_text()

        pattern = (
            rf"(\s*// ARVIS_FUSED_BEGIN: {tag}\n)"
            rf".*?"
            rf"(\n\s*// ARVIS_FUSED_END: {tag})"
        )
        replacement = rf"\1{new_content}\2"
        result, count = re.subn(pattern, replacement, text, flags=re.DOTALL)
        if count == 0:
            raise ValueError(f"Pragma tag '{tag}' not found in {rel_path}")
        filepath.write_text(result)
