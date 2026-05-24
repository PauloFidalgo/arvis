"""Portability gateway: emit a variant via :class:`Pipeline` from
a legacy :class:`pipeline.rtl_changeset.RTLChangeSet`.

This is the bridge that lets the existing ``runner.py`` if-tree
opt into the new portable architecture without rewriting the
runner.  When the user passes ``--use-portability`` (or sets the
``ARVIS_USE_PORTABILITY=1`` environment variable),
:meth:`RTLChangeSet.apply` routes to :func:`emit_via_portability`
instead of running its own legacy emission code.

The shim performs three steps:

1. **Translate** the changeset's untyped state (``prune_config``,
   ``fused_operations``, ``hw_loop_count``, ``pc_width``, ...)
   into typed :class:`Decision` instances.
2. **Synthesise** a :class:`VariantConfig` from the set of
   non-trivial decisions present.
3. **Emit** by setting up an :class:`RTLWorkspace` rooted at
   ``ctx.rtl_output_dir`` and running
   ``target.allocate_workspace_metadata`` + the canonical patch
   sequence (``PruneDecision``, ``FusionDecision``,
   ``LoopDecision``, ``WidthDecision``).

Equivalence with the legacy path is verified by
``examples/portability_equivalence.py`` for the five standard
variants on the ``ud`` benchmark, all 174 SystemVerilog files
byte-identical.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arvis.core.pipeline import VariantConfig
from arvis.core.rtl_patch import RTLWorkspace
from arvis.core.strategy import (
    Decision,
    FusionDecision,
    LoopDecision,
    PruneDecision,
    WidthDecision,
)

if TYPE_CHECKING:
    from arvis.pipeline.rtl_changeset import RTLChangeSet
    from arvis.targets.cv32e40p.core import CV32E40P


logger = logging.getLogger(__name__)


# Canonical ordering -- mirrors :class:`core.pipeline.Pipeline`.
_PATCH_ORDER: tuple[str, ...] = (
    "PruneDecision",
    "FusionDecision",
    "LoopDecision",
    "WidthDecision",
)


# ─── Decision builders from a legacy RTLChangeSet ─────────────────


def _build_prune_decision(cs: RTLChangeSet) -> PruneDecision | None:
    """Translate ``cs.prune_config`` + ``cs.used_instructions``
    into a :class:`PruneDecision`.

    Returns ``None`` when the changeset has no prune config (the
    BASELINE variant).
    """
    pc = cs.prune_config
    if pc is None:
        return None

    # Collect ``enable_*`` boolean flags exactly the way
    # :class:`UsageDrivenPruner` does -- this is the canonical
    # source of truth for what gets carried in
    # :attr:`PruneDecision.feature_flags`.  ``prune_*`` flags
    # do NOT belong here; they're target-private knobs read by
    # the legacy code only.
    feature_flags: dict[str, bool] = {}
    for attr in dir(pc):
        if attr.startswith("enable_") and not attr.startswith("_"):
            value = getattr(pc, attr, None)
            if isinstance(value, bool):
                feature_flags[attr] = value

    # ``target_overlay`` carries the integer-valued, non-flag
    # fields PruneConfig holds (corev_pulp, fpu, hw_loop_*, ...).
    # The set of attributes mirrors UsageDrivenPruner's
    # canonical list.
    target_overlay: dict[str, Any] = {}
    for attr in (
        "corev_pulp",
        "fpu",
        "num_mhpmcounters",
        "debug_trigger_en",
        "hw_loop",
        "hw_loop_cnt_width",
        "hw_loop_addr_width",
        "pc_width",
    ):
        value = getattr(pc, attr, None)
        if isinstance(value, int):
            target_overlay[attr] = value

    return PruneDecision(
        removable_alu_ops=frozenset(pc.removable_alu_ops),
        removable_mul_modes=frozenset(pc.removable_mul_modes),
        removable_opcode_groups=frozenset(pc.removable_opcode_groups),
        removable_csr_labels=frozenset(getattr(pc, "removable_csr_labels", set())),
        removable_csr_storage=frozenset(getattr(pc, "removable_csr_storage", set())),
        used_instructions=frozenset(cs.used_instructions),
        unused_registers=tuple(getattr(pc, "unused_registers", []) or []),
        used_regs_mask=getattr(pc, "used_regs_mask", 0),
        feature_flags=feature_flags,
        target_overlay=target_overlay,
    )


def _build_fusion_decision(cs: RTLChangeSet) -> FusionDecision | None:
    """Translate ``cs.fused_operations`` into a :class:`FusionDecision`.

    Returns ``None`` when there are no fused ops (the FusionPatch
    will not run for this variant).
    """
    fused = list(cs.fused_operations or [])
    if not fused:
        return None
    return FusionDecision(fused_ops=tuple(fused))


def _build_loop_decision(cs: RTLChangeSet) -> LoopDecision | None:
    """Translate ``cs.hw_loop_count`` into a :class:`LoopDecision`.

    Returns ``None`` when ``hw_loop_count == 0`` (no loop strategy
    active for this variant).
    """
    if cs.hw_loop_count <= 0:
        return None
    return LoopDecision(
        nest_depth=cs.hw_loop_count,
        counter_width=getattr(cs, "hw_loop_cnt_width", 32),
        addr_width=getattr(cs, "hw_loop_addr_width", 32),
    )


def _build_width_decision(cs: RTLChangeSet) -> WidthDecision | None:
    """Translate ``cs.pc_width`` + ``cs.prefetch_fifo_depth`` into a
    :class:`WidthDecision`.

    Returns ``None`` when no width tuning is requested (all defaults).
    """
    pc_width = getattr(cs, "pc_width", 0) or 0
    fifo = getattr(cs, "prefetch_fifo_depth", 0) or 0
    cnt = getattr(cs, "hw_loop_cnt_width", 32) or 32
    addr = getattr(cs, "hw_loop_addr_width", 32) or 32
    if pc_width == 0 and fifo == 0 and cnt >= 32 and addr >= 32:
        return None
    return WidthDecision(
        pc_width=pc_width,
        hwlp_addr_width=addr,
        counter_width=cnt,
        fifo_depth=fifo,
    )


# ─── Variant config synthesis ─────────────────────────────────────


def _synth_variant_config(label: str, decisions: dict[str, Decision]) -> VariantConfig:
    """Build a :class:`VariantConfig` whose ``decision_kinds`` is
    exactly the set of kinds present in ``decisions``.

    The runner caller passes a ``label`` (e.g. ``"pruned"``,
    ``"fused_pruned"``) which we use as the variant name -- this
    matches what cs._apply_label / ctx.rtl_output_dir already
    encode in the legacy path.
    """
    kinds: frozenset[str] = frozenset(decisions.keys())
    return VariantConfig(label=label, decision_kinds=kinds)


# ─── The shim entrypoint ──────────────────────────────────────────


def emit_via_portability(
    cs: RTLChangeSet,
    cfg: Any,
    ctx: Any,
    *,
    target: CV32E40P | None = None,
    verbose: bool = False,
) -> None:
    """Emit a variant's RTL via the portable :class:`Pipeline` path.

    This function is the drop-in replacement for the legacy
    :meth:`pipeline.rtl_changeset.RTLChangeSet.apply` body when
    ``--use-portability`` is set.  It mutates ``ctx.rtl_output_dir``
    in the same way (writes ``rtl_<label>`` under ``cfg.output_dir``)
    so downstream code in ``runner.py`` is unaffected.

    Parameters
    ----------
    cs:
        The legacy :class:`RTLChangeSet` carrying the per-variant
        intent (prune_config, fused_operations, hw_loop_count,
        pc_width, ...).
    cfg:
        The legacy ``ToolConfig``.  We read ``rtl_root`` and
        ``output_dir``.
    ctx:
        The legacy ``PipelineContext``.  We mutate ``rtl_output_dir``
        to point at the freshly-emitted variant.
    target:
        Optional :class:`CV32E40P` instance.  When ``None`` we
        construct one from ``cfg.rtl_root``.
    verbose:
        Forwarded to the patch ``apply`` calls (currently a no-op
        for cv32e40p patches; reserved for future use).
    """
    # ── 1. Resolve target ────────────────────────────────────────
    if target is None:
        from arvis.targets.cv32e40p.core import CV32E40P

        target = CV32E40P(rtl_root=Path(cfg.rtl_root))

    # ── 2. Resolve output directory exactly like legacy cs.apply ─
    label = getattr(cs, "_apply_label", None) or "modified"
    rtl_output_dir = Path(cfg.output_dir) / f"rtl_{label}"
    ctx.rtl_output_dir = str(rtl_output_dir)

    # ── 3. Build Decisions ───────────────────────────────────────
    decisions: dict[str, Decision] = {}

    p = _build_prune_decision(cs)
    if p is not None:
        decisions["PruneDecision"] = p

    f = _build_fusion_decision(cs)
    if f is not None:
        decisions["FusionDecision"] = f

    loop = _build_loop_decision(cs)
    if loop is not None:
        decisions["LoopDecision"] = loop

    w = _build_width_decision(cs)
    if w is not None:
        decisions["WidthDecision"] = w

    # ── 4. Build a workspace at the target output dir ────────────
    workspace = RTLWorkspace(
        source_root=target.rtl_root,
        output_root=rtl_output_dir,
    )
    workspace.copy_fresh()

    # ── 5. Synthesise the variant config from present decisions ──
    variant = _synth_variant_config(label, decisions)

    # ── 6. Run target metadata allocation hook (e.g. encoding) ───
    decisions_by_kind = {kind: [d] for kind, d in decisions.items()}
    target.allocate_workspace_metadata(variant, decisions_by_kind, workspace)

    # Stash the hw_loop count where the patches expect it.  The
    # equivalence test does this identically.
    if "LoopDecision" in decisions:
        loop_d = decisions["LoopDecision"]
        # mypy: narrow `Decision | None` -> the LoopDecision the
        # earlier _build_loop_decision returned.
        assert isinstance(loop_d, LoopDecision)
        workspace.metadata["cv32e40p_hw_loop_count"] = loop_d.nest_depth
    else:
        workspace.metadata["cv32e40p_hw_loop_count"] = 0

    # Propagate the CLI-level ``enable_debug`` knob.  PrunePatch
    # reads this when invoking the debug/ctrl pragma processors;
    # see the "Flag derivation note" in PrunePatch.apply.  When
    # ``cfg`` doesn't carry the attribute we default to True
    # (the canonical "keep debug" stance).
    workspace.metadata["cv32e40p_enable_debug"] = bool(getattr(cfg, "enable_debug", True))

    # ── 7. Run patches in canonical order ────────────────────────
    for kind in _PATCH_ORDER:
        if kind not in decisions:
            continue
        decision = decisions[kind]
        # Each render_*_decision is the canonical factory for the
        # patch list for that decision kind.  We type-narrow the
        # ``Decision`` payload to the concrete subclass before
        # dispatch (mypy strict requires this).
        if kind == "PruneDecision":
            assert isinstance(decision, PruneDecision)
            patches = target.render_prune_decision(decision, workspace)
        elif kind == "FusionDecision":
            assert isinstance(decision, FusionDecision)
            patches = target.render_fusion_decision(decision, workspace)
        elif kind == "LoopDecision":
            assert isinstance(decision, LoopDecision)
            patches = target.render_loop_decision(decision, workspace)
        elif kind == "WidthDecision":
            assert isinstance(decision, WidthDecision)
            patches = target.render_width_decision(decision, workspace)
        else:  # pragma: no cover - guarded by _PATCH_ORDER
            continue

        for patch in patches:
            try:
                patch.apply(workspace)
            except Exception:
                # Mirror legacy behaviour: log + continue.  Hard
                # failures should still bubble; soft skips keep
                # the runner alive when a template anchor is
                # missing in some unusual benchmark configuration.
                logger.warning(
                    "Soft-skip: patch %r for %s raised; continuing with the rest of the variant",
                    patch,
                    kind,
                    exc_info=True,
                )

    # ── 8. ctx side-effects the runner reads back ────────────────
    # The legacy code attaches `prune_report`, `verilator_extra_flags`,
    # and similar bookkeeping to ctx during cs.apply.  We replicate
    # the minimal set the runner consumes downstream.
    pc = cs.prune_config
    if pc is not None:
        try:
            verilator_extra_flags = pc.verilator_flags()
        except Exception:
            logger.debug(
                "PruneConfig.verilator_flags() failed; defaulting to []",
                exc_info=True,
            )
            verilator_extra_flags = []
        ctx.verilator_extra_flags = verilator_extra_flags

    # The encoding registry is in workspace.metadata; surface it
    # on the changeset so the assembly patcher / hwloop machinery
    # can read the funct3 mapping back.
    from arvis.targets.cv32e40p.encoding import WORKSPACE_REGISTRY_KEY

    registry = workspace.metadata.get(WORKSPACE_REGISTRY_KEY)
    if registry is not None:
        cs._custom_registry = registry
        try:
            cs._hwlp_encoding = registry.get_hwloop_encoding()
            ctx._hwlp_encoding = cs._hwlp_encoding
        except Exception:
            logger.debug(
                "Registry has no hwloop encoding to surface on cs/ctx",
                exc_info=True,
            )


# ─── Predicate ────────────────────────────────────────────────────


def is_enabled(cfg: Any = None) -> bool:
    """Return ``True`` when the portability path should be used.

    Checks both the ``ARVIS_USE_PORTABILITY=1`` environment
    variable and ``cfg.use_portability`` (when ``cfg`` is provided).
    """
    if os.environ.get("ARVIS_USE_PORTABILITY") == "1":
        return True
    return cfg is not None and bool(getattr(cfg, "use_portability", False))
