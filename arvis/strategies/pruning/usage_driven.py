"""Usage-driven pruner.

The default ARVIS pruner: analyse the disassembly of the binary
that will run on the core, count which ALU operations / multiplier
modes / opcode groups are used, and report the unused ones as
removable.

Phase 1 retrofit: this strategy is a thin adapter around
:func:`pipeline.pruning.compute_prune_config`.  The legacy
function still does the heavy lifting; the adapter:

1. Builds a minimal ``ctx``-like object from the
   :class:`WorkloadProfile` (specifically the fused/baseline ELF
   path).
2. Builds a minimal ``cfg``-like object carrying the
   CLI-controlled overrides (``prune_rf_read_c``,
   ``prune_rf_write_b``, ``riscv_objdump``).
3. Calls :func:`compute_prune_config` and translates the returned
   :class:`codegen.rtl.rtl_pruning.PruneConfig` into a typed
   :class:`PruneDecision`.

Phase 2 will move the analysis logic out of
:func:`compute_prune_config` so this strategy stops adapting and
just analyses directly.  Until then, behaviour is bit-identical
to the legacy path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from arvis.core.strategy import PruneDecision, PruningStrategy

if TYPE_CHECKING:
    from arvis.core.target import TargetCore
    from arvis.core.workload import Workload, WorkloadProfile


# ─── Minimal ctx/cfg shims used by compute_prune_config ────────────


@dataclass
class _CfgShim:
    """Subset of :class:`config.ToolConfig` that
    :func:`compute_prune_config` actually reads."""

    prune_rf_read_c: bool = False
    prune_rf_write_b: bool = False
    riscv_objdump: str = "riscv64-unknown-elf-objdump"


@dataclass
class _CtxShim:
    """Subset of :class:`pipeline.context.PipelineContext` that
    :func:`compute_prune_config` actually reads."""

    fused_elf_path: Optional[str] = None
    prune_config: object = None  # written by compute_prune_config

    # Other ctx fields are accessed via getattr(ctx, name, default)
    # in the legacy code; we let __getattr__ return None for any
    # field we haven't predicted.
    def __getattr__(self, name: str):
        return None


# ─── The strategy ──────────────────────────────────────────────────


class UsageDrivenPruner(PruningStrategy):
    """Disable features that the workload's binary doesn't touch.

    Parameters
    ----------
    prune_rf_read_c:
        Force-prune regfile read port C even if used.  Mirrors
        ``ToolConfig.prune_rf_read_c``.
    prune_rf_write_b:
        Force-prune regfile write port B even if used.
    objdump_binary:
        ``riscv*-objdump`` binary to use for disassembly.
    verbose:
        Forward to the legacy function for log output.
    """

    def __init__(
        self,
        *,
        prune_rf_read_c: bool = False,
        prune_rf_write_b: bool = False,
        objdump_binary: str = "riscv64-unknown-elf-objdump",
        verbose: bool = False,
    ) -> None:
        self.prune_rf_read_c = prune_rf_read_c
        self.prune_rf_write_b = prune_rf_write_b
        self.objdump_binary = objdump_binary
        self.verbose = verbose

    @property
    def name(self) -> str:
        return "usage-driven-pruner"

    def applicable(self, workload: "Workload", target: "TargetCore") -> bool:
        # Pruning is meaningful for any cv32e40p-shaped target.  We
        # use parameter-presence as a soft proxy: a target that
        # exposes HW_LOOP belongs to the cv32e40p family today.  A
        # future ibex target without HW_LOOP would still be
        # prunable — at that point we'd swap the predicate.
        return True

    def analyze(
        self,
        workload: "Workload",
        profile: "WorkloadProfile",
        target: "TargetCore",
    ) -> PruneDecision:
        from arvis.pipeline.pruning import compute_prune_config

        # Pick the ELF that pruning should analyse.  The legacy
        # convention: when a fused binary exists it's authoritative
        # (since fusion replaces some instructions).  Otherwise the
        # baseline ELF is used.  The profile carries every variant
        # ELF; we pick the first one whose name is not the
        # baseline, falling back to the only ELF available.
        chosen_elf: Optional[Path] = None
        for elf in profile.elf_paths:
            if elf is None or not Path(str(elf)).exists():
                continue
            stem = Path(str(elf)).stem
            # Heuristic preference: prefer a fused ELF over a
            # baseline ELF.  "_fused" is the conventional suffix
            # in ``targets/benchmarks/<bm>/<bm>_fused.elf``.
            if "fused" in stem and "baseline" not in stem:
                chosen_elf = Path(str(elf))
                break
        if chosen_elf is None:
            for elf in profile.elf_paths:
                if elf is not None and Path(str(elf)).exists():
                    chosen_elf = Path(str(elf))
                    break

        cfg = _CfgShim(
            prune_rf_read_c=self.prune_rf_read_c,
            prune_rf_write_b=self.prune_rf_write_b,
            riscv_objdump=self.objdump_binary,
        )
        ctx = _CtxShim(
            fused_elf_path=str(chosen_elf) if chosen_elf is not None else None,
        )

        # Delegate.  ``compute_prune_config`` mutates ``ctx`` with
        # ``ctx.prune_config = ...`` and returns
        # ``(prune_config, used_instructions)``.  We capture the
        # tuple and translate.
        try:
            prune_config, used_instructions = compute_prune_config(
                cfg, ctx, verbose=self.verbose
            )
        except Exception:
            # Strategies are not allowed to abort the pipeline.  An
            # analysis failure becomes "no pruning decisions" which
            # the renderer treats as a no-op.
            return PruneDecision()

        return self._translate(prune_config, used_instructions)

    # ── PruneConfig -> PruneDecision translation ──────────────────
    @staticmethod
    def _translate(prune_config, used_instructions) -> PruneDecision:
        """Translate the legacy :class:`PruneConfig` value object
        into the typed :class:`PruneDecision`.

        The translation is round-trip-lossless: every field the
        legacy :class:`RTLPruner` reads from the config is carried
        on the decision, either as a typed field or in
        ``feature_flags`` / ``target_overlay``.  The cv32e40p
        emitter reconstructs an equivalent ``PruneConfig`` from
        these fields when applying patches; equivalence is verified
        by ``examples/portability_equivalence.py``.
        """
        removable_alu_ops = frozenset(getattr(prune_config, "removable_alu_ops", set()))
        removable_mul_modes = frozenset(
            getattr(prune_config, "removable_mul_modes", set())
        )
        removable_opcode_groups = frozenset(
            getattr(prune_config, "removable_opcode_groups", set())
        )
        removable_csr_labels = frozenset(
            getattr(prune_config, "removable_csr_labels", set())
        )
        removable_csr_storage = frozenset(
            getattr(prune_config, "removable_csr_storage", set())
        )

        # Collect all enable_* flags into a dict.  This is forward-
        # compatible: when new flags are added to PruneConfig they
        # appear automatically in feature_flags.
        feature_flags = {}
        for attr in dir(prune_config):
            if attr.startswith("enable_") and not attr.startswith("_"):
                value = getattr(prune_config, attr)
                if isinstance(value, bool):
                    feature_flags[attr] = value

        # Target-wide configuration knobs (capabilities the
        # strategy observed and propagates).
        target_overlay = {}
        for attr in (
            "corev_pulp",
            "fpu",
            "num_mhpmcounters",
            "debug_trigger_en",
            "hw_loop",
            "hw_loop_cnt_width",
            "hw_loop_addr_width",
        ):
            value = getattr(prune_config, attr, None)
            if isinstance(value, int):
                target_overlay[attr] = value

        # Register-level analysis.
        unused_registers = tuple(
            sorted(getattr(prune_config, "unused_registers", []))
        )
        used_regs_mask = int(getattr(prune_config, "used_regs_mask", 0xFFFFFFFF))

        return PruneDecision(
            removable_alu_ops=removable_alu_ops,
            removable_mul_modes=removable_mul_modes,
            removable_opcode_groups=removable_opcode_groups,
            feature_flags=feature_flags,
            used_instructions=frozenset(used_instructions),
            unused_registers=unused_registers,
            used_regs_mask=used_regs_mask,
            removable_csr_labels=removable_csr_labels,
            removable_csr_storage=removable_csr_storage,
            target_overlay=target_overlay,
        )
