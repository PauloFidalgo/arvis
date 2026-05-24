"""Custom-instruction encoding allocation for cv32e40p.

The cv32e40p uses R4-type slots in opcodes CUSTOM_0..CUSTOM_3 for
custom instructions.  Two consumer groups share this space:

- **Fusion** ops (consume from the front of the slot list).
- **Hardware-loop** instructions (consume from the tail; reserved
  for ``lp.bounds``, ``lp.count``, optionally ``lp.start`` and
  ``lp.end`` when ``hw_loop > 2``).

In a single variant emission, both consumers may be active (the
ALL variant) or only one (FUSED_PRUNED, HWLOOP_PRUNED).  The
encoding allocator builds one :class:`CustomInstructionRegistry`
per variant that captures whose slots are whose.  This module
exposes a single function, :func:`allocate_for_variant`, that the
pipeline calls between strategy collection and patch application.

The function delegates to the legacy
:func:`pipeline.custom_insn_registry.build_registry_from_used_instructions`
because that function already encodes the cv32e40p-specific
opcode/funct3/funct2 layout.  Phase 3.x may inline the allocation
logic here once the legacy registry module can be retired.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence


def allocate_for_variant(
    fusion_decision: Optional[Any],
    loop_decision: Optional[Any],
    *,
    next_r4_slot: int = 0,
) -> Any:
    """Build a custom-instruction registry from the active decisions.

    Parameters
    ----------
    fusion_decision:
        The :class:`core.strategy.FusionDecision` for this variant,
        or ``None`` when the variant excludes fusion.
    loop_decision:
        The :class:`core.strategy.LoopDecision` for this variant,
        or ``None`` when the variant excludes hwloop.
    next_r4_slot:
        Starting slot index for hwloop instructions.  Normally the
        index of the first free slot AFTER all fused ops are
        registered (the legacy compute_filtered_ops returns this
        as ``gcc_compile_result.next_r4_slot``).  Defaults to 0
        when the variant has no fused ops.

    Returns
    -------
    A :class:`pipeline.custom_insn_registry.CustomInstructionRegistry`
    populated with the hwloop slots if ``loop_decision.nest_depth``
    is non-zero.  When neither fusion nor hwloop is active the
    registry is empty.

    Notes
    -----
    The cv32e40p target's opcode space (see
    :meth:`targets.cv32e40p.core.CV32E40P.opcode_space`) is the
    upstream definition; this allocator is the per-variant
    accountant that tracks which slots are consumed by which
    instruction class.  The two should be kept in sync; the
    equivalence test in
    ``examples/portability_equivalence.py`` is the regression
    guard.
    """
    from arvis.pipeline.custom_insn_registry import build_registry_from_used_instructions

    nest_depth = 0
    if loop_decision is not None:
        nest_depth = int(getattr(loop_decision, "nest_depth", 0) or 0)

    # The legacy allocator only uses ``next_r4_slot`` when fusion
    # ops exist.  When the variant has no fusion, slot 0 is fine.
    has_fusion = (
        fusion_decision is not None
        and bool(getattr(fusion_decision, "fused_ops", ()))
    )
    start = next_r4_slot if has_fusion else 0

    return build_registry_from_used_instructions(
        hw_loop_count=nest_depth,
        next_r4_slot=start,
    )


# Well-known key for storing the registry in
# :attr:`core.rtl_patch.RTLWorkspace.metadata`.  Patches read by
# this key; tests may also inspect it.
WORKSPACE_REGISTRY_KEY = "cv32e40p_encoding_registry"
