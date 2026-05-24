"""CV32E40P :class:`core.target.TargetCore` implementation.

This module is the authoritative description of the cv32e40p
target as ARVIS sees it.  It does NOT contain RTL rendering logic
yet; the rendering still happens inside
:class:`pipeline.rtl_changeset.RTLChangeSet` via the legacy path.
What this module DOES provide:

- The path to the RTL templates and the synthesizable file list
- The tunable parameter surface (HW_LOOP, CNT_WIDTH, HWLP_ADDR_WIDTH,
  PC_WIDTH, FIFO_DEPTH)
- The opcode space available for custom fused operations
  (R4-type slots in CUSTOM_0..CUSTOM_3, with the last two
  reserved for hwloop bounds/count instructions)
- The reset vector and memory layout used by the standard
  testbench
- The path to the Verilator testbench

The class is small and side-effect-free; it is safe to construct
multiple instances per pipeline run.  Each call to
:meth:`opcode_space` returns a fresh allocator so concurrent runs
don't share state.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Mapping, Tuple

from arvis.core.isa import ISADescriptor
from arvis.core.target import CoreParameter, OpcodeSlot, OpcodeSpace, TargetCore


# ─── R4-type encoding constants (mirrored from peephole_gen) ───────
#
# These values must match the cv32e40p decoder's expectations.  The
# canonical source is :func:`codegen.gcc.peephole_gen._r4_enc`; we
# replicate the constants here so the target can describe its
# encoding space without reaching into the toolchain code.

_R4_OPCODES: Tuple[int, ...] = (0x0B, 0x2B, 0x5B, 0x7B)  # CUSTOM_0..3
_R4_FUNCT3_PER_OPCODE = 8
_R4_FUNCT2_PER_FUNCT3 = 4
_R4_SLOTS_PER_OPCODE = _R4_FUNCT3_PER_OPCODE * _R4_FUNCT2_PER_FUNCT3  # 32
_R4_TOTAL = len(_R4_OPCODES) * _R4_SLOTS_PER_OPCODE  # 128
_R4_HWLOOP_RESERVED_TAIL = 2  # bounds + count instructions per nest level


def _build_r4_slots() -> List[OpcodeSlot]:
    """Build all 128 R4-type slots in canonical iteration order.

    The order is: opcode-major, funct3-major-within-opcode,
    funct2-minor.  This matches the legacy
    :func:`codegen.gcc.peephole_gen._r4_enc` so a strategy that
    allocates the first N slots agrees with the legacy allocator on
    which N opcodes get used.
    """
    slots: List[OpcodeSlot] = []
    for opcode in _R4_OPCODES:
        for funct3 in range(_R4_FUNCT3_PER_OPCODE):
            for funct2 in range(_R4_FUNCT2_PER_FUNCT3):
                slots.append(
                    OpcodeSlot(
                        opcode=opcode,
                        funct3=funct3,
                        funct2=funct2,
                        label=f"CUSTOM_op={opcode:#04x}/funct3={funct3}/funct2={funct2}",
                    )
                )
    return slots


# ─── CV32E40P TargetCore ───────────────────────────────────────────


class CV32E40P(TargetCore):
    """The CV32E40P RISC-V core (OpenHW Group).

    Constructed with an ``rtl_root`` path; defaults to the in-tree
    ``targets/cv32e40p/`` location.  Tests and out-of-tree users
    can override.
    """

    DEFAULT_RTL_ROOT = Path("targets/cv32e40p")

    # Parameter defaults match ``cv32e40p_top.sv``'s parameter list.
    # When ARVIS narrows them, it does so per-variant by emitting an
    # override; the defaults here represent the unspecialized core.
    _PARAMETERS: Tuple[CoreParameter, ...] = (
        CoreParameter(
            name="HW_LOOP",
            default=0,
            minimum=0,
            maximum=8,
            description="Number of hardware-loop nest levels (0=disabled).",
        ),
        CoreParameter(
            name="CNT_WIDTH",
            default=32,
            minimum=8,
            maximum=32,
            description="Hardware-loop counter register width in bits.",
        ),
        CoreParameter(
            name="HWLP_ADDR_WIDTH",
            default=32,
            minimum=12,
            maximum=32,
            description="LP_start/end/last register width in bits.",
        ),
        CoreParameter(
            name="PC_WIDTH",
            default=32,
            minimum=8,
            maximum=32,
            description="Main pipeline PC width in bits.",
        ),
        CoreParameter(
            name="FIFO_DEPTH",
            default=2,
            minimum=2,
            maximum=8,
            description="Prefetch buffer FIFO depth.",
        ),
    )

    def __init__(self, rtl_root: Path = DEFAULT_RTL_ROOT) -> None:
        self._rtl_root = Path(rtl_root)

    # ── Identity ───────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "cv32e40p"

    @property
    def isa(self) -> ISADescriptor:
        return ISADescriptor.rv32imc_zicsr()

    # ── RTL surface ────────────────────────────────────────────────
    @property
    def rtl_root(self) -> Path:
        return self._rtl_root

    def synthesizable_files(self) -> List[Path]:
        """The RTL files that make up the synthesizable core.

        Mirrors what ``cv32e40p_manifest.flist`` lists, minus
        FPU-only files that aren't relevant for ARVIS's runs.
        Paths are relative to :attr:`rtl_root`.
        """
        rtl = Path("rtl")
        include = rtl / "include"
        return [
            include / "cv32e40p_pkg.sv",
            include / "cv32e40p_apu_core_pkg.sv",
            rtl / "cv32e40p_aligner.sv",
            rtl / "cv32e40p_alu.sv",
            rtl / "cv32e40p_alu_div.sv",
            rtl / "cv32e40p_apu_disp.sv",
            rtl / "cv32e40p_compressed_decoder.sv",
            rtl / "cv32e40p_controller.sv",
            rtl / "cv32e40p_core.sv",
            rtl / "cv32e40p_cs_registers.sv",
            rtl / "cv32e40p_decoder.sv",
            rtl / "cv32e40p_ex_stage.sv",
            rtl / "cv32e40p_ff_one.sv",
            rtl / "cv32e40p_fifo.sv",
            rtl / "cv32e40p_hwloop_regs.sv",
            rtl / "cv32e40p_id_stage.sv",
            rtl / "cv32e40p_if_stage.sv",
            rtl / "cv32e40p_int_controller.sv",
            rtl / "cv32e40p_load_store_unit.sv",
            rtl / "cv32e40p_mult.sv",
            rtl / "cv32e40p_obi_interface.sv",
            rtl / "cv32e40p_popcnt.sv",
            rtl / "cv32e40p_prefetch_buffer.sv",
            rtl / "cv32e40p_prefetch_controller.sv",
            rtl / "cv32e40p_register_file_ff.sv",
            rtl / "cv32e40p_sleep_unit.sv",
            rtl / "cv32e40p_top.sv",
        ]

    # ── Parameters ─────────────────────────────────────────────────
    def parameters(self) -> List[CoreParameter]:
        return list(self._PARAMETERS)

    # ── Encoding ───────────────────────────────────────────────────
    def opcode_space(self) -> OpcodeSpace:
        """A fresh R4-type opcode space with 128 slots, less the
        hwloop tail reservation.

        Strategies that want hwloop instructions take the LAST N
        slots; everything else (fused ops) consumes from the
        front.  The legacy ``_r4_enc`` allocator follows the same
        convention so this OpcodeSpace agrees with it slot-for-slot
        on which encodings get used.
        """
        all_slots = _build_r4_slots()
        # Reserve the tail for hwloop bounds/count.  Strategies
        # that need hwloop slots pop from the *end* of consumed; we
        # expose that via :meth:`reserve_hwloop_slots` when the
        # allocator integration lands in Phase 2.  For now, simply
        # exclude them from the default front-allocator.
        usable = all_slots[: -_R4_HWLOOP_RESERVED_TAIL]
        return OpcodeSpace(name="cv32e40p-R4", available=usable)

    # ── Memory layout ──────────────────────────────────────────────
    @property
    def reset_vector(self) -> int:
        """Address the core fetches from on reset.

        Matches ``BOOT_ADDR_I`` in the testbench; the standard
        cv32e40p flow uses ``0x80``.
        """
        return 0x80

    @property
    def memory_layout(self) -> Mapping[str, Tuple[int, int]]:
        """Symbolic ``{region: (start, size)}`` map.

        The standard testbench uses a single contiguous memory
        region from address 0 with 1 MiB capacity.  The reset
        vector lies inside the text region.
        """
        return {
            "text": (0x0000, 0x10_0000),  # 1 MiB
            "data": (0x10_0000, 0x10_0000),  # second MiB
        }

    # ── Testbench wiring ───────────────────────────────────────────
    def testbench_dir(self) -> Path:
        """Path to the Verilator testbench shipped with the core."""
        return self._rtl_root / "example_tb" / "core"

    # ── Decision rendering (Phase 2) ──────────────────────────────
    # Each render_* method translates one Decision shape into a
    # list of RTLPatches that the pipeline applies in order to a
    # fresh workspace.  Phase 2.1 implements the width path; the
    # other three roles still inherit no-op stubs from TargetCore
    # (see Phase 2.2-2.4 for their implementations).

    def render_width_decision(self, decision, workspace):
        """Render a :class:`WidthDecision` to a list of patches.

        Returns a single :class:`WidthNarrowingPatch` carrying all
        three narrowings (PC, HWLP addr, counter).  When the
        decision is fully default (all widths >= 32, pc_width == 0)
        the patch is a no-op.
        """
        from arvis.targets.cv32e40p.patches import WidthNarrowingPatch

        return [WidthNarrowingPatch(decision=decision)]

    def render_prune_decision(self, decision, workspace):
        """Render a :class:`PruneDecision` to a list of patches.

        Returns a single :class:`PrunePatch`.  The patch
        reconstructs a legacy :class:`PruneConfig` from the
        typed Decision fields (lossless versus the original
        ``compute_prune_config`` output) and hands it to the
        legacy :class:`RTLPruner`.  Equivalence is verified by
        ``examples/portability_equivalence.py``.
        """
        from arvis.targets.cv32e40p.patches import PrunePatch

        return [PrunePatch(decision=decision)]

    def render_fusion_decision(self, decision, workspace):
        """Render a :class:`FusionDecision` to a list of patches.

        Returns a single :class:`FusionPatch`.  Empty decisions
        (no fused ops) produce a no-op patch.

        Pre-requisite: the workspace must already be in its
        post-pruning state (PrunePatch run first) when the
        variant includes pruning, because fusion patches operate
        on the surviving decoder/ALU files.
        """
        from arvis.targets.cv32e40p.patches import FusionPatch

        return [FusionPatch(decision=decision)]

    def render_loop_decision(self, decision, workspace):
        """Render a :class:`LoopDecision` to a list of patches.

        Returns a single :class:`LoopPatch`.  Handles both the
        ``nest_depth == 0`` case (pragma processor strips ARVIS_HWLP
        markers) and the ``nest_depth > 0`` case (template swap +
        funct3 patching).
        """
        from arvis.targets.cv32e40p.patches import LoopPatch

        return [LoopPatch(decision=decision)]

    # ── Per-variant workspace metadata ────────────────────────────
    def allocate_workspace_metadata(
        self, variant, decisions_by_kind, workspace
    ) -> None:
        """Compute the cv32e40p custom-instruction encoding registry
        and stash it in :attr:`workspace.metadata`.

        Called once per variant emission by
        :meth:`Pipeline._emit_variant` BEFORE any patches run.
        Patches read the registry under
        :data:`targets.cv32e40p.encoding.WORKSPACE_REGISTRY_KEY`
        when they need it (FusionPatch and LoopPatch in particular
        for the ALL variant where they share opcode space).
        """
        from arvis.targets.cv32e40p.encoding import (
            allocate_for_variant,
            WORKSPACE_REGISTRY_KEY,
        )

        # Pick the relevant decisions for this variant.  When the
        # variant excludes a decision kind, pass None so the
        # allocator knows to skip those slots.
        fusion_decision = None
        loop_decision = None
        if variant.includes("FusionDecision"):
            fdl = decisions_by_kind.get("FusionDecision", [])
            if fdl:
                fusion_decision = fdl[0]  # one decision per kind today
        if variant.includes("LoopDecision"):
            ldl = decisions_by_kind.get("LoopDecision", [])
            if ldl:
                loop_decision = ldl[0]

        registry = allocate_for_variant(fusion_decision, loop_decision)
        workspace.metadata[WORKSPACE_REGISTRY_KEY] = registry
