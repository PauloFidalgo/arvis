"""Free-function passes that mutate a CV32E40P :class:`RTLWorkspace`.

The legacy :class:`pipeline.rtl_changeset.RTLChangeSet` originally
held these as private methods.  Several callers (the legacy
emitter, :class:`PrunePatch`, :class:`LoopPatch`,
:class:`FusionPatch`) all needed the same logic, which led to
brittle "build a temporary :class:`RTLChangeSet` shim" patterns.
By promoting the work to free functions:

* every caller uses a single, named, type-checked entry point;
* the dependencies on ``self`` are explicit (regular keyword
  arguments instead of attribute lookup with ``getattr(self, ...)``);
* unit tests can call the passes in isolation without standing
  up an :class:`RTLChangeSet`;
* legacy code becomes a thin wrapper that simply forwards.

These functions are intentionally **idempotent** wherever possible
(a constraint inherited from :class:`RTLPatch`).  The only exception
is :func:`apply_fusion_patches`, which clears existing pragma
content first to keep the second invocation clean.

Style note
----------
The signatures use keyword-only parameters (``*,``) to make
call sites self-documenting.  None of the parameters are mutually
optional: the caller knows exactly what the variant needs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arvis.core.rtl_patch import RTLWorkspace


logger = logging.getLogger(__name__)


# ─── Public API ────────────────────────────────────────────────────


def apply_hwloop_pragmas(
    workspace: RTLWorkspace,
    *,
    hw_loop_count: int,
    hwlp_encoding: Any | None = None,
    verbose: bool = False,
) -> list[Any]:
    """Process every ``ARVIS_HWLP`` pragma in the workspace.

    Stripped behaviour at ``hw_loop_count == 0``: the processor
    deletes the pragma blocks (the workload doesn't use HW loops).
    With ``hw_loop_count > 0`` the processor inserts the encoding-
    specific implementation between the markers.

    Parameters
    ----------
    workspace:
        Target workspace; ``workspace.output_root / "rtl"`` is
        scanned recursively.
    hw_loop_count:
        Variant's nest depth (0, 2, 3, 4, ...).
    hwlp_encoding:
        Optional encoding payload; currently a duck-typed object
        with ``bounds_funct3`` / ``count_funct3`` / ``start_funct3``
        / ``end_funct3`` integer attributes.  When ``None`` the
        processor uses template defaults.
    verbose:
        Reserved for future logging; currently ignored (the
        callers print their own banners).

    Returns
    -------
    list
        One ``HWLoopPragmaProcessor.Stats`` per file processed.
        Empty when no ``cv32e40p_*`` files matched.
    """
    from arvis.codegen.rtl.hwloop_pragma import HWLoopPragmaProcessor

    rtl_dir = Path(workspace.output_root) / "rtl"
    if not rtl_dir.exists():
        return []

    proc = HWLoopPragmaProcessor(hw_loop=hw_loop_count, hwlp_encoding=hwlp_encoding)
    return proc.process_dir(rtl_dir)


def clear_fused_pragmas(rtl_dir: Path) -> None:
    """Empty every ``ARVIS_FUSED_BEGIN/END`` block in the canonical
    cv32e40p target files.

    This makes :func:`apply_fusion_patches` safe to re-run: the
    fusion generator re-fills the markers from scratch.
    """
    rtl_dir = Path(rtl_dir)
    for filename in (
        "include/cv32e40p_pkg.sv",
        "cv32e40p_decoder.sv",
        "cv32e40p_alu.sv",
    ):
        fpath = rtl_dir / filename
        if not fpath.exists():
            continue
        text = fpath.read_text()
        text = re.sub(
            r"(// ARVIS_FUSED_BEGIN: \w+\n).*?(// ARVIS_FUSED_END: \w+)",
            r"\1\2",
            text,
            flags=re.DOTALL,
        )
        fpath.write_text(text)


def apply_fusion_patches(
    workspace: RTLWorkspace,
    *,
    fused_operations: Iterable[Any] = (),
    registry: Any | None = None,
    hwlp_encoding: Any | None = None,
    hw_loop_count: int = 0,
    fusion_applied_callback: Callable[[], None] | None = None,
) -> bool:
    """Inject fused-instruction RTL into the (already pruned) workspace.

    Patches between ``ARVIS_FUSED_BEGIN/END`` pragmas in:

    - ``include/cv32e40p_pkg.sv``    ALU enum entries
    - ``cv32e40p_decoder.sv``        OPCODE_CUSTOM_* case blocks
    - ``cv32e40p_alu.sv``            result_mux expressions

    Then applies the canonical post-fusion fixes:

    1. ``fused_imm_i`` port wiring (variable-immediate patterns)
    2. Adder reuse steering
    3. Mult pre-compute input steering
    4. SVA assertion disable (CUSTOM_0 legality)
    5. ``ALU_OP_WIDTH`` expansion (if enum values > 127)

    Parameters
    ----------
    workspace:
        Target workspace.
    fused_operations:
        Iterable of legacy ``FusedOperation`` records (the same
        objects the analysis stage produces).  May be empty when
        the variant only uses hwloop OPCODE_CUSTOM_* slots.
    registry:
        Optional custom-instruction registry; when present,
        ``registry.hwloop_instructions`` is appended to the
        decoder's CUSTOM_0 case block.
    hwlp_encoding:
        Reserved (currently unused; the registry already carries
        the encoding).  Accepted for API symmetry with
        :func:`apply_hwloop_pragmas`.
    hw_loop_count:
        Reserved (also currently unused; the registry pre-encodes
        the hwloop slots based on its own count).
    fusion_applied_callback:
        Optional zero-argument callable invoked after all post-
        fusion fixes succeed.  Used by the legacy ``ctx`` setter
        (``ctx.fusion_rtl_applied = True``).  None-safe.

    Returns
    -------
    bool
        ``True`` when patches were applied successfully, ``False``
        when the workspace lacks the required cv32e40p anchors
        (no exception raised).
    """
    from arvis.codegen.rtl.adder_reuse_patcher import (
        add_adder_reuse_wiring,
        needs_adder_reuse,
    )
    from arvis.codegen.rtl.fused_imm_patcher import add_fused_imm_port, needs_fused_imm
    from arvis.codegen.rtl.fusion_patches import (
        disable_custom0_sva,
        update_alu_op_width,
    )
    from arvis.codegen.rtl.isa_fusion.alu_single_cycle import RTLGenerator
    from arvis.codegen.rtl.mult_reuse_patcher import (
        _get_pre_compute_ops,
        add_mult_pre_compute_wiring,
        generate_mult_steering,
        needs_mult_pre_compute,
    )

    rtl_dir = Path(workspace.output_root) / "rtl"

    # ── Anchor check: bail out cleanly when target templates lack
    #    the canonical files.  Mirrors the legacy fast-path return
    #    that printed "❌ <name> file not found".
    pkg_path = rtl_dir / "include" / "cv32e40p_pkg.sv"
    dec_path = rtl_dir / "cv32e40p_decoder.sv"
    alu_path = rtl_dir / "cv32e40p_alu.sv"
    for path, name in (
        (pkg_path, "pkg"),
        (dec_path, "decoder"),
        (alu_path, "alu"),
    ):
        if not path.exists():
            print(f"  ❌ {name} file not found: {path}")
            return False

    # ── Step 1: clear any leftover pragma content from a prior run.
    clear_fused_pragmas(rtl_dir)

    # ── Step 2: register all (deduplicated) fused operations.
    gen = RTLGenerator(str(rtl_dir))
    gen.add_existing()  # parse baseline ALU values

    seen_names: set[str] = set()
    fused_list = list(fused_operations)
    for op in fused_list:
        if op.name in seen_names:
            continue
        seen_names.add(op.name)
        gen.fused_ops.append(op)
        gen.allocator.register(op)

    # ── Step 3: inject hwloop OPCODE_CUSTOM_* decoder entries.
    if registry is not None:
        gen._hwloop_registry_entries = registry.hwloop_instructions  # type: ignore[attr-defined]
        if registry.hwloop_instructions:
            for e in registry.hwloop_instructions:
                logger.info(
                    "HWLoop decoder entry: %s -> 0x%02x f3=%s f2=%s",
                    e.name,
                    e.opcode,
                    e.funct3,
                    e.funct2,
                )

    # ── Step 4: emit the patches.
    gen.write()

    # ── Step 5: post-fusion fixes ────────────────────────────────
    needs_imm = needs_fused_imm(fused_list)
    if not needs_imm and alu_path.exists():
        needs_imm = "fused_imm_i" in alu_path.read_text()
    if needs_imm:
        add_fused_imm_port(str(rtl_dir), imm_width=10)

    if needs_adder_reuse(fused_list):
        steering_sv = gen.generate_alu_adder_steering()
        add_adder_reuse_wiring(str(rtl_dir), steering_sv)

    if needs_mult_pre_compute(fused_list):
        pre_ops = _get_pre_compute_ops(fused_list)
        steering_sv = generate_mult_steering(pre_ops)
        add_mult_pre_compute_wiring(str(rtl_dir), steering_sv)

    disable_custom0_sva(rtl_dir)
    update_alu_op_width(rtl_dir, gen)

    # ── Step 6: notify caller that fusion succeeded.
    if fusion_applied_callback is not None:
        try:
            fusion_applied_callback()
        except Exception:
            logger.exception("Soft-skip: fusion_applied_callback raised; ignoring")

    return True


__all__ = [
    "apply_ctrl_pragmas",
    "apply_ctrl_pragmas_on_dir",
    "apply_dce_cleanup",
    "apply_debug_pragmas",
    "apply_encoding_optimization",
    "apply_feature_pragmas",
    "apply_fusion_patches",
    "apply_hwloop_pragmas",
    "apply_irq_pragmas",
    "apply_pulp_pragmas",
    "apply_pulp_pragmas_on_dir",
    "clear_fused_pragmas",
]


# ─── Feature-pragma processors ────────────────────────────────────


def apply_feature_pragmas(
    workspace: RTLWorkspace,
    *,
    feature: str,
    enabled: bool,
    level: int,
    generators: dict[str, Any],
    extra_dirs: list[str] | None = None,
) -> list[Any]:
    """Process ``ARVIS_<feature>`` pragmas across the workspace.

    The feature primitive: looks up named generators, expands the
    matching pragma blocks, and stitches the result back into the
    target file.  Used for DBG, PULP, IRQ, etc.

    Parameters
    ----------
    workspace:
        Target workspace.
    feature:
        Pragma family name -- ``"DBG"``, ``"PULP"``, ``"IRQ"``, ...
    enabled:
        When ``True`` the generator is invoked with the feature
        retained; when ``False`` the generator drops the block.
    level:
        Numeric depth (e.g. ``corev_pulp`` 0/1/2).
    generators:
        Map of pragma name to generator callable.
    extra_dirs:
        Optional list of directories under
        ``workspace.output_root`` to also scan (e.g.
        ``"example_tb/core"``).

    Returns
    -------
    list
        One stats record per processed file.
    """
    from arvis.codegen.rtl.rtl_pragma import RTLPragmaProcessor

    output_root = Path(workspace.output_root)
    rtl_dir = output_root / "rtl"

    proc = RTLPragmaProcessor()
    proc.add_feature(feature, enabled=enabled, level=level, generators=generators)
    all_stats = proc.process_dir(rtl_dir)

    for sub in extra_dirs or []:
        d = output_root / sub
        if d.exists():
            all_stats.extend(proc.process_dir(d))

    return all_stats


def apply_debug_pragmas(workspace: RTLWorkspace, *, enable_debug: bool) -> list[Any]:
    """Process ARVIS_DBG pragmas (RISC-V debug / JTAG infrastructure)."""
    from arvis.codegen.rtl.debug_pragma import DBG_GENERATORS

    return apply_feature_pragmas(
        workspace,
        feature="DBG",
        enabled=enable_debug,
        level=1 if enable_debug else 0,
        generators=DBG_GENERATORS,
        extra_dirs=["example_tb/core", "example_tb/core/verilator"],
    )


def apply_pulp_pragmas(workspace: RTLWorkspace, *, corev_pulp: int) -> list[Any]:
    """Process ARVIS_PULP pragmas (PULP custom instructions)."""
    from arvis.codegen.rtl.pulp_pragma import PULP_GENERATORS

    return apply_feature_pragmas(
        workspace,
        feature="PULP",
        enabled=corev_pulp > 0,
        level=corev_pulp,
        generators=PULP_GENERATORS,
    )


def apply_irq_pragmas(workspace: RTLWorkspace, *, enable_interrupts: bool) -> list[Any]:
    """Process ARVIS_IRQ pragmas (interrupt controller)."""
    from arvis.codegen.rtl.irq_pragma import IRQ_GENERATORS

    return apply_feature_pragmas(
        workspace,
        feature="IRQ",
        enabled=enable_interrupts,
        level=1 if enable_interrupts else 0,
        generators=IRQ_GENERATORS,
        extra_dirs=["example_tb/core", "example_tb/core/verilator"],
    )


def apply_ctrl_pragmas(
    workspace: RTLWorkspace,
    *,
    enable_interrupts: bool,
    enable_debug: bool,
) -> int:
    """Process ARVIS_CTRL pragmas in ``cv32e40p_controller.sv``.

    Unlike the other feature pragmas this one is bivariate -- the
    generators receive both ``enable_irq`` and ``enable_dbg`` and
    emit different RTL for each combination.

    Returns
    -------
    int
        Number of pragma blocks processed (for logging).
    """
    from arvis.codegen.rtl.ctrl_pragma import CTRL_GENERATORS

    rtl_dir = Path(workspace.output_root) / "rtl"
    ctrl_path = rtl_dir / "cv32e40p_controller.sv"
    if not ctrl_path.exists():
        return 0

    text = ctrl_path.read_text()
    total_processed = 0

    pattern = re.compile(
        r"(\s*)// ARVIS_CTRL_BEGIN: (\w+)\n(.*?)// ARVIS_CTRL_END: \2",
        re.DOTALL,
    )

    def _replacer(match: re.Match[str]) -> str:
        nonlocal total_processed
        indent = match.group(1)
        name = match.group(2)
        gen = CTRL_GENERATORS.get(name)
        if gen is None:
            return str(match.group(0))  # unknown pragma: keep
        result = gen(enable_interrupts, enable_debug, indent)
        total_processed += 1
        if result is None:
            return str(match.group(0))  # generator says "keep original"
        if result == "":
            return ""  # generator says "delete the block"
        return f"{indent}{result.rstrip()}\n"

    new_text = pattern.sub(_replacer, text)
    if new_text != text:
        ctrl_path.write_text(new_text)
    return total_processed


def apply_pulp_pragmas_on_dir(
    workspace: RTLWorkspace,
    target_dir: Path,
    *,
    corev_pulp: int,
) -> list[Any]:
    """Same as :func:`apply_pulp_pragmas` but scoped to one directory.

    Used for the ``rtl/include/`` subdirectory which holds
    ``cv32e40p_pkg.sv`` (typedef definitions need PULP-aware
    pragma processing too).
    """
    from arvis.codegen.rtl.pulp_pragma import PULP_GENERATORS
    from arvis.codegen.rtl.rtl_pragma import RTLPragmaProcessor

    proc = RTLPragmaProcessor()
    proc.add_feature(
        "PULP",
        enabled=corev_pulp > 0,
        level=corev_pulp,
        generators=PULP_GENERATORS,
    )
    return proc.process_dir(Path(target_dir))


def apply_ctrl_pragmas_on_dir(
    workspace: RTLWorkspace,
    target_dir: Path,
    *,
    enable_interrupts: bool,
    enable_debug: bool,
) -> None:
    """Same as :func:`apply_ctrl_pragmas` but scoped to one directory."""
    from arvis.codegen.rtl.ctrl_pragma import CTRL_GENERATORS

    target_dir = Path(target_dir)
    pattern = re.compile(
        r"(\s*)// ARVIS_CTRL_BEGIN: (\w+)\n(.*?)// ARVIS_CTRL_END: \2",
        re.DOTALL,
    )

    for sv_file in sorted(target_dir.glob("*.sv")):
        text = sv_file.read_text()
        original = text

        def _replacer(match: re.Match[str]) -> str:
            indent = match.group(1)
            name = match.group(2)
            gen = CTRL_GENERATORS.get(name)
            if gen is None:
                return str(match.group(0))
            result = gen(enable_interrupts, enable_debug, indent)
            if result is None:
                return str(match.group(0))
            if result == "":
                return ""
            return f"{indent}{result.rstrip()}\n"

        text = pattern.sub(_replacer, text)
        if text != original:
            sv_file.write_text(text)


# ─── Cleanup passes ───────────────────────────────────────────────


def apply_dce_cleanup(workspace: RTLWorkspace) -> list[Any]:
    """Iteratively remove dead signal declarations and assignments.

    Wraps :func:`codegen.rtl.pyslang_dce.run_dce_on_directory`.  All
    exceptions are caught and reported via ``cli.print_warning``;
    the workspace is left in whatever state DCE managed to reach.
    """
    rtl_dir = Path(workspace.output_root) / "rtl"
    try:
        from arvis.codegen.rtl.pyslang_dce import run_dce_on_directory

        return run_dce_on_directory(rtl_dir)
    except Exception:
        logger.warning("DCE cleanup skipped", exc_info=True)
        return []


def apply_encoding_optimization(
    workspace: RTLWorkspace,
    *,
    removable_mul_modes: Iterable[str] = (),
    verbose: bool = False,
) -> Any | None:
    """Reduce enum widths by removing unused members.

    Wraps :func:`codegen.rtl.encoding_optimizer.apply_encoding_optimization`
    with the canonical ``forced_unused`` derivation: only
    ``mul_opcode_e`` is forced for now (``alu_opcode_e`` is left
    alone because the fusion generator manages its width).

    Returns the optimizer's result object on success, ``None`` on
    failure (DCE-style soft-skip).
    """
    try:
        from arvis.analysis.enum_usage import analyze_enum_usage
        from arvis.codegen.rtl.encoding_optimizer import (
            apply_encoding_optimization as _apply,
        )
    except ImportError:
        return None

    output_root = Path(workspace.output_root)
    forced_unused: dict[str, set[str]] = {}
    mul = list(removable_mul_modes)
    if mul:
        forced_unused["mul_opcode_e"] = set(mul)

    try:
        usage_report = analyze_enum_usage(output_root)
        return _apply(
            output_root,
            usage_report=usage_report,
            forced_unused=forced_unused if forced_unused else None,
            verbose=verbose,
        )
    except Exception:
        logger.warning("Encoding optimization skipped", exc_info=True)
        return None
