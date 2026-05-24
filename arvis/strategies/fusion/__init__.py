"""Fusion strategies.

A :class:`FusionStrategy` decides which custom fused instructions
should be added to the target.  Currently:

- :class:`NGramFusion` — n-gram mining over the disassembly,
  filtered by DSP-friendliness.

Future strategies in this package may include:
- ``ProfileGuidedFusion`` — focus on hot regions using cycle
  profile.
- ``ManualFusion`` — read fused-op definitions from a YAML file.
- ``MLFusion`` — predict beneficial fusions with a model.
- ``VendorFusion`` — use vendor-supplied custom instructions.
"""

from arvis.strategies.fusion.ngram import NGramFusion

__all__ = ["NGramFusion"]
