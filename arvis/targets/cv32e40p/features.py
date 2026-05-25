"""Declarative feature surface for the cv32e40p target.

This module is the cv32e40p-specific data layer for Phase 8's
feature-based pruning.  Each :class:`CoreFeature` here encodes:

* What the feature is (description).
* How to detect that it's unused (``usage_predicate``).
* Which RTL files / parameters / pragmas to touch when removing
  it (``removal_actions``).

The :class:`FeatureBasedPruner` and
:class:`FeatureRemovalPatch` consume this list directly — no
target-specific code is required beyond the data here and (rarely)
a :class:`CustomAction` callable for transformations the
declarative kinds can't express.

Coverage relative to legacy ``PruneConfig``
-------------------------------------------

The legacy :class:`codegen.rtl.rtl_pruning.PruneConfig` carries
~15 ``enable_*`` flags.  Each maps to one feature here.  The
mapping is documented per-feature in the docstring; equivalence
is validated by ``examples/portability_equivalence.py`` extended
to compare the new path's RTL output to the legacy path on a
representative benchmark suite.

PHASE 8 SCOPE
-------------

This file lists the features but DOES NOT yet replace the legacy
:class:`PrunePatch`.  Both paths coexist:

* The legacy ``UsageDrivenPruner`` produces the field-based
  decision (``feature_flags``, ``removable_alu_ops``, etc.) which
  ``PrunePatch`` consumes.
* The new ``FeatureBasedPruner`` produces an
  ``unused_features``-based decision which ``FeatureRemovalPatch``
  consumes.

Per-target dispatch in :meth:`CV32E40P.render_prune_decision`
chooses between them based on which decision fields are
populated.  When the new path proves byte-equivalent across the
benchmark suite, the legacy path can be retired.
"""

from __future__ import annotations

from arvis.core.feature import ActionKind, CoreFeature, RemovalAction

# ─── Mnemonic groups (lookup tables for predicates) ───────────────


_DIV_MNEMONICS = ("div", "divu", "rem", "remu")
_MUL_MNEMONICS = ("mul", "mulh", "mulhsu", "mulhu")
_MULH_MNEMONICS = ("mulh", "mulhsu", "mulhu")
_ATOMIC_MNEMONICS = (
    "lr.w",
    "sc.w",
    "amoswap.w",
    "amoadd.w",
    "amoand.w",
    "amoor.w",
    "amoxor.w",
    "amomax.w",
    "amomin.w",
)
_FENCE_MNEMONICS = ("fence", "fence.i")
_SYSTEM_MNEMONICS = ("ecall", "ebreak", "wfi", "mret", "uret", "sret", "dret")

# PULP custom-opcode mnemonics (CV32E40P COREV_PULP extensions).
_PULP_PREFIXES = ("p.", "lp.", "cv.")


# ─── Feature definitions ───────────────────────────────────────────


_DIV_UNIT = CoreFeature(
    name="div_unit",
    description="ALU divider (DIV / DIVU / REM / REMU)",
    usage_predicate=lambda p: p.uses_any(_DIV_MNEMONICS),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_DIV",
            value=0,
        ),
    ),
    estimated_area_pct=4.0,
)
"""Maps to legacy ``PruneConfig.enable_div``."""


_MULTIPLIER = CoreFeature(
    name="multiplier",
    description="Integer multiplier (MUL only — does not cover MULH variants)",
    usage_predicate=lambda p: p.uses_any(_MUL_MNEMONICS),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_MUL",
            value=0,
        ),
    ),
    requires=("multiplier_high",),  # if MUL goes, MULH is also dead
    estimated_area_pct=8.0,
)
"""Maps to legacy ``PruneConfig.enable_mul``."""


_MULTIPLIER_HIGH = CoreFeature(
    name="multiplier_high",
    description="Upper-half multiplier (MULH / MULHSU / MULHU)",
    usage_predicate=lambda p: p.uses_any(_MULH_MNEMONICS),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_MULH",
            value=0,
        ),
    ),
    estimated_area_pct=2.5,
)
"""Maps to legacy ``PruneConfig.enable_mul_h``."""


_SLEEP = CoreFeature(
    name="sleep",
    description="Sleep / WFI support",
    usage_predicate=lambda p: p.uses("wfi"),
    keep_when=lambda p, opts: opts.get("keep_sleep", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_SLEEP",
            value=0,
        ),
    ),
    estimated_area_pct=0.5,
)
"""Maps to legacy ``PruneConfig.enable_sleep``."""


_DEBUG = CoreFeature(
    name="debug",
    description="RISC-V debug spec support (debug ROM, dpc/dscratch CSRs)",
    # Conservative: keep debug unless explicitly opted out.
    # ARVIS workloads typically don't exercise debug, so density
    # is always 0; rely on the override flag.
    usage_predicate=lambda p: False,
    keep_when=lambda p, opts: not opts.get("prune_debug", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_DEBUG",
            value=0,
        ),
    ),
    estimated_area_pct=2.0,
)
"""Maps to legacy ``PruneConfig.enable_debug``."""


_HPM_COUNTERS = CoreFeature(
    name="hpm_counters",
    description="Hardware performance monitor counters (mhpmcounter*)",
    # Default: keep (safety).  Predicate-False + keep_when-True
    # idiom (see _INTERRUPTS) means the feature is removable
    # only via the ``prune_hpm`` override.
    usage_predicate=lambda p: False,
    keep_when=lambda p, opts: not opts.get("prune_hpm", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="NUM_MHPMCOUNTERS",
            value=0,
        ),
    ),
    estimated_area_pct=1.5,
)
"""Maps to legacy ``PruneConfig.enable_hpm`` / ``num_mhpmcounters``."""


_PULP_EXTENSIONS = CoreFeature(
    name="pulp_extensions",
    description="PULP custom extensions (post-incrementing loads, hwloop, dot-mul, ...)",
    usage_predicate=lambda p: any(m.startswith(_PULP_PREFIXES) for m in p.instr_histogram),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="COREV_PULP",
            value=0,
        ),
        RemovalAction(
            kind=ActionKind.REMOVE_PRAGMA_BLOCK,
            file="rtl/cv32e40p_id_stage.sv",
            target="PULP_ONLY_IMM",
        ),
    ),
    estimated_area_pct=12.0,
)
"""Maps to legacy ``PruneConfig.corev_pulp`` + the PULP-only
immediate label removal in id_stage."""


_FPU = CoreFeature(
    name="fpu",
    description="Floating-point unit (RV32F / RV32D)",
    # FP mnemonics start with 'f' but so do many integer ones (fence).
    # Use a more specific check.
    usage_predicate=lambda p: any(
        m.startswith(
            (
                "fadd",
                "fsub",
                "fmul",
                "fdiv",
                "fsqrt",
                "fmadd",
                "fmsub",
                "fnmadd",
                "fnmsub",
                "fmin",
                "fmax",
                "fcvt",
                "fmv.",
                "fsgnj",
                "feq.",
                "flt.",
                "fle.s",
                "fle.d",
                "fclass.",
                "flw",
                "fsw",
                "fld",
                "fsd",
            )
        )
        for m in p.instr_histogram
    ),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="FPU",
            value=0,
        ),
    ),
    estimated_area_pct=15.0,
)
"""Maps to legacy ``PruneConfig.fpu``."""


_REGFILE_PORT_C = CoreFeature(
    name="regfile_read_port_c",
    description="Register-file read port C (third operand for fused / DOT ops)",
    # The third read port is needed by fused 4-operand instructions
    # and by DOT/MSU.  When PULP is removed and no fusion is used,
    # this port is wasted.  Conservative: depend on PULP being kept.
    usage_predicate=lambda p: any(m.startswith(_PULP_PREFIXES) for m in p.instr_histogram),
    keep_when=lambda p, opts: opts.get("keep_regfile_rd_c", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_REGFILE_RD_C",
            value=0,
        ),
    ),
    estimated_area_pct=3.0,
)
"""Maps to legacy ``PruneConfig.enable_regfile_rd_c``."""


_REGFILE_PORT_B_WRITE = CoreFeature(
    name="regfile_write_port_b",
    description="Register-file write port B (post-inc / dual writeback)",
    # Like port C above: only used by PULP post-increment / dual-
    # writeback semantics.
    usage_predicate=lambda p: any(m.startswith(_PULP_PREFIXES) for m in p.instr_histogram),
    keep_when=lambda p, opts: opts.get("keep_regfile_wr_b", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_REGFILE_WR_B",
            value=0,
        ),
    ),
    estimated_area_pct=2.0,
)
"""Maps to legacy ``PruneConfig.enable_regfile_wr_b``."""


_COMPRESSED = CoreFeature(
    name="compressed",
    description="RVC compressed instruction support",
    # Detection: presence of any 2-byte (compressed) instruction
    # in the disassembly.  Cv32e40p baseline always has C; we
    # only remove when the binary is built without it.
    # The 'extra' map is the right channel for this kind of
    # binary-property hint.
    usage_predicate=lambda p: bool(p.extra.get("has_compressed", True)),
    keep_when=lambda p, opts: opts.get("keep_compressed", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_if_stage.sv",
            target="ENABLE_COMPRESSED",
            value=0,
        ),
    ),
    estimated_area_pct=3.0,
)
"""Maps to legacy ``PruneConfig.enable_compressed``."""


_PULP_ALU_OPS = CoreFeature(
    name="pulp_alu_ops",
    description=(
        "PULP-specific ALU operations (bit-manipulation, count, "
        "extract/insert).  Removed via AST-based case-arm pruning "
        "when COREV_PULP is disabled."
    ),
    # The legacy parameter gate (COREV_PULP=0) sets up generate-if
    # blocks, but the case arms in the ALU still occupy decoder
    # space.  We strip them via AST.  Tied to the same predicate as
    # _PULP_EXTENSIONS so they cascade.
    usage_predicate=lambda p: any(m.startswith(_PULP_PREFIXES) for m in p.instr_histogram),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.AST_REMOVE_CASE_ITEMS,
            file="rtl/cv32e40p_alu.sv",
            target="operator_i",
            value=frozenset(
                {
                    # Bit manipulation
                    "ALU_BCLR",
                    "ALU_BSET",
                    "ALU_BEXT",
                    "ALU_BEXTU",
                    "ALU_BINS",
                    "ALU_BREV",
                    # Count / find
                    "ALU_FF1",
                    "ALU_FL1",
                    "ALU_CLB",
                    "ALU_CNT",
                    "ALU_ROR",
                    # Extract / pack
                    "ALU_EXTS",
                    "ALU_EXT",
                    "ALU_INS",
                    # SIMD shuffles
                    "ALU_SHUF",
                    "ALU_SHUF2",
                    "ALU_PCKLO",
                    "ALU_PCKHI",
                }
            ),
        ),
    ),
    estimated_area_pct=2.5,
)
"""Removes PULP-specific case arms from cv32e40p_alu.sv via AST.

This is the canonical example of AST_REMOVE_CASE_ITEMS in
action.  The labels are known at definition time (they're a
fixed set defined by the cv32e40p ALU), so a declarative kind
fits perfectly — no per-workload computation needed beyond the
binary "is PULP used?" predicate.
"""


_INTERRUPTS = CoreFeature(
    name="interrupts",
    description="Interrupt path (mtvec, mip, mie, interrupt controller)",
    # Conservative default: keep.  Removing interrupts is unsafe
    # for any workload that needs to be re-entrant.  The
    # predicate is intentionally always-false so the keep_when
    # gate is the actual decision: by default keep_when returns
    # True (kept); when ``prune_interrupts`` override is set,
    # keep_when returns False and the feature is removed.
    usage_predicate=lambda p: False,
    keep_when=lambda p, opts: not opts.get("prune_interrupts", False),
    removal_actions=(
        RemovalAction(
            kind=ActionKind.SET_PARAM,
            file="rtl/cv32e40p_top.sv",
            target="ENABLE_INTERRUPTS",
            value=0,
        ),
    ),
    estimated_area_pct=4.0,
)
"""Maps to legacy ``PruneConfig.enable_interrupts``."""


# ─── The full feature surface ──────────────────────────────────────


CV32E40P_FEATURES: tuple[CoreFeature, ...] = (
    _DIV_UNIT,
    _MULTIPLIER,
    _MULTIPLIER_HIGH,
    _SLEEP,
    _DEBUG,
    _HPM_COUNTERS,
    _PULP_EXTENSIONS,
    _PULP_ALU_OPS,
    _FPU,
    _REGFILE_PORT_C,
    _REGFILE_PORT_B_WRITE,
    _COMPRESSED,
    _INTERRUPTS,
)
"""The declarative feature surface of cv32e40p.

12 features covering ~80% of the legacy ``PruneConfig`` flag
surface.  Remaining flags (CSR labels, opcode-group pruning,
register-file write masks) operate on data the binary
disassembly carries directly and stay on the legacy path until
the cv32e40p emitter is fully migrated.
"""


__all__ = ["CV32E40P_FEATURES"]
