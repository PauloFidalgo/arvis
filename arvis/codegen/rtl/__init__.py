"""
RTL code generation for cv32e40p specialization.

Four main transformation passes:
  1. ISA Fusion    — Fuse instruction sequences into custom operations
  2. RTL Pruning   — Remove unused ALU operations to save area
  3. SPM Generator — Add scratchpad memory for hot data regions
  4. Accelerator   — Create dedicated hardware accelerators for hot loops

Each pass operates on an RTLWorkspace (a working copy of the cv32e40p tree)
and modifies the RTL files between ARVIS pragma markers.
"""

from .base import RTLWorkspace as RTLWorkspace
