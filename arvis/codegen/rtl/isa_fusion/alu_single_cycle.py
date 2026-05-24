"""
Programmatic RTL code generator for ALU-only fused operations.

Generates SystemVerilog patches for cv32e40p by inserting code between
ARVIS_FUSED_BEGIN/END pragma markers in:
  - cv32e40p_pkg.sv     → ALU opcode enum entries
  - cv32e40p_decoder.sv → CUSTOM_0 decode cases
  - cv32e40p_alu.sv     → result_mux expressions

Encoding scheme for ALU-only 2-input/1-output (R-type):
  [funct7:31-25][rs2:24-20][rs1:19-15][funct3:14-12][rd:11-7][opcode:6-0]
  opcode = CUSTOM_0 (0x0b)
  funct3 = 3'b000 (for 2-in/1-out)
  funct7 = unique per fused operation

For 3-input/1-output or 2-input/2-output (R4-type):
  funct3 encodes the variant
  funct7[6:2] = operation ID, funct7[1:0] = operand order variant

Naming: ALU_{OP1}_{OP2}[_{OP3}][_V{variant}]
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from arvis.analysis.registers import normalize_mnemonic

# ═══════════════════════════════════════════════════════════════════════════
# ISA mnemonic → SystemVerilog operator mapping
# ═══════════════════════════════════════════════════════════════════════════
#
# For each RISC-V ALU mnemonic, define how it maps to a SV expression
# on operand_a_i and operand_b_i (the ALU's two main inputs).
#
# The expression uses 'a' for the first operand and 'b' for the second.
# For unary ops (like xori with imm=-1), the imm is folded into the
# expression (e.g., xori x,x,-1 → ~a).
#
# Some operations are commutative: add(a,b) = add(b,a)
# Non-commutative: sub(a,b) ≠ sub(b,a) → need variant encoding
#
# IMPORTANT: Only BASE-ISA mnemonics are listed here.  Compressed
# instructions (c.add, c.slli, …) are NOT included because the
# CV32E40P compressed decoder in the IF stage decompresses them into
# their base-ISA equivalents BEFORE the main decoder sees them.
# All fusion analysis normalizes compressed mnemonics to base-ISA
# equivalents via normalize_mnemonic(), so by the time we reach
# this table, only base-ISA keys are ever looked up.

MNEMONIC_TO_SV: Dict[str, Dict[str, Any]] = {
    # Arithmetic
    "add": {"expr": "({a} + {b})", "commutative": True},
    "addi": {"expr": "({a} + {b})", "commutative": True},
    "sub": {"expr": "({a} - {b})", "commutative": False},
    # Logic
    "and": {"expr": "({a} & {b})", "commutative": True},
    "andi": {"expr": "({a} & {b})", "commutative": True},
    "or": {"expr": "({a} | {b})", "commutative": True},
    "ori": {"expr": "({a} | {b})", "commutative": True},
    "xor": {"expr": "({a} ^ {b})", "commutative": True},
    "xori": {"expr": "({a} ^ {b})", "commutative": True},
    # Shifts
    "sll": {"expr": "({a} << {b}[4:0])", "commutative": False},
    "slli": {"expr": "({a} << {b}[4:0])", "commutative": False},
    "srl": {"expr": "({a} >> {b}[4:0])", "commutative": False},
    "srli": {"expr": "({a} >> {b}[4:0])", "commutative": False},
    "sra": {"expr": "({{{{32{{{a}[31]}}}}, {a}}} >> {b}[4:0])", "commutative": False},
    "srai": {"expr": "({{{{32{{{a}[31]}}}}, {a}}} >> {b}[4:0])", "commutative": False},
    # Comparisons — SV concatenation {cmp, 31'b0} uses doubled braces
    # to escape Python format: {{ → literal {, }} → literal }
    "slt": {"expr": "{{31'b0, $signed({a}) < $signed({b})}}", "commutative": False},
    "slti": {"expr": "{{31'b0, $signed({a}) < $signed({b})}}", "commutative": False},
    "sltu": {"expr": "{{31'b0, {a} < {b}}}", "commutative": False},
    "sltiu": {"expr": "{{31'b0, {a} < {b}}}", "commutative": False},
    # Special: xori with imm=-1 is bitwise NOT
    "xori_neg1": {"expr": "~{a}", "commutative": True, "unary": True},
    # Multiply (single-cycle 32-bit)
    "mul": {"expr": "({a} * {b})", "commutative": True},
    # Upper immediate
    "lui": {"expr": "({b} << 12)", "commutative": False, "unary": True},
    # Memory operations — SV expression represents the ADDRESS computation
    # (base + offset). The actual load/store is handled by the LSU, not ALU.
    # These mappings enable load-compute and compute-store fusion expressions.
    "lw": {"expr": "({a} + {b})", "commutative": False, "memory": "load"},
    "lh": {"expr": "({a} + {b})", "commutative": False, "memory": "load"},
    "lhu": {"expr": "({a} + {b})", "commutative": False, "memory": "load"},
    "lb": {"expr": "({a} + {b})", "commutative": False, "memory": "load"},
    "lbu": {"expr": "({a} + {b})", "commutative": False, "memory": "load"},
    "sw": {"expr": "({a} + {b})", "commutative": False, "memory": "store"},
    "sh": {"expr": "({a} + {b})", "commutative": False, "memory": "store"},
    "sb": {"expr": "({a} + {b})", "commutative": False, "memory": "store"},
    # Pseudo-instructions / move
    "mv": {"expr": "{a}", "commutative": False, "unary": True},
    "nop": {"expr": "32'b0", "commutative": True, "unary": True},
}

# Immediates that fold into the operation (change the mnemonic meaning)
IMM_FOLDS = {
    ("xori", -1): "xori_neg1",  # xori x,x,-1 → bitwise NOT
}


# RISC-V CUSTOM opcode spaces
OPCODE_CUSTOM_0 = 0x0B
OPCODE_CUSTOM_1 = 0x2B

# ═══════════════════════════════════════════════════════════════════════════
# Execution unit classification
# ═══════════════════════════════════════════════════════════════════════════
#
# Mnemonics that require the multiplier hardware.  Any n-gram containing
# at least one of these must be routed to the MULT unit, not the ALU.
MUL_MNEMONICS = frozenset({"mul", "mulh", "mulhu", "mulhsu"})

# GIMPLE-derived op names that contain multiplication.
# These are used in patterns.json 3-gram ops field (e.g., "lshift", "plus", "mult").
# 3-gram patterns with GIMPLE "mult" where ALL multiply operands are
# hardcoded constants are kept in ALU — synthesis optimizes constant
# multiplication into shift+add chains (CSD decomposition), so no full
# 32x32 multiplier is synthesized.  Only variable×variable multiplications
# need the MULT hardware unit.
GIMPLE_MUL_OPS = frozenset({"mult"})


def classify_execution_unit(mnemonics: Tuple[str, ...]) -> str:
    """Classify an n-gram to its execution unit based on its mnemonics.

    Returns:
        "alu"  — route to cv32e40p_alu.sv
        "mult" — route to cv32e40p_mult.sv (reuse existing multiplier)

    This is a pure function of the mnemonic tuple — no workload-specific
    hardcoding.  Works for any n-gram from any benchmark.
    """
    for mnem in mnemonics:
        base = normalize_mnemonic(mnem)
        if base in MUL_MNEMONICS:
            return "mult"
    return "alu"


def classify_mult_pattern(
    mnemonics: Tuple[str, ...],
) -> str:
    """For MUL-involving fusions, classify the specific pattern shape.

    This determines how to map the fusion to existing or extended
    multiplier hardware in cv32e40p_mult.sv.

    Returns one of:
        "mac"          — mul+add: a*b+c → reuse MUL_MAC32 (zero new HW!)
        "msu"          — mul+sub: c-a*b → reuse MUL_MSU32 (zero new HW!)
        "mul_post_sub" — mul+sub: a*b-c → needs new MUL opcode
        "pre_compute"  — ALU_OP+mul: (f(a,b))*c → extend mult pre-mux
        "post_compute" — mul+ALU_OP: a*b then ALU → extend mult post-mux
        "complex"      — 3+ gram chains involving mul → needs analysis
    """
    if not mnemonics:
        return "complex"

    norm = tuple(normalize_mnemonic(m) for m in mnemonics)
    n = len(norm)

    # Find position of the multiply
    mul_positions = [i for i, m in enumerate(norm) if m in MUL_MNEMONICS]
    if not mul_positions:
        return "complex"  # no mul — shouldn't happen if caller checked

    mul_pos = mul_positions[0]

    if n == 2:
        other_pos = 1 - mul_pos
        other_mnem = norm[other_pos]

        if mul_pos == 0:
            # mul is first: mul+ALU_OP
            if other_mnem in ("add", "addi"):
                # mul+add → a*b + c = MAC
                return "mac"
            elif other_mnem == "sub":
                # mul+sub → (a*b) - c = mul_post_sub
                # Note: NOT MSU.  MSU is c - a*b.
                # But in the n-gram "mul,sub", the sub's rs1 is
                # the mul result, so result = mul_result - rs2_of_sub.
                # This maps to: (a*b) - c, which isn't standard MSU.
                return "mul_post_sub"
            else:
                return "post_compute"
        else:
            # ALU_OP is first, mul is second: ALU_OP+mul
            if other_mnem in ("add", "addi"):
                # add+mul → only makes sense if add feeds into mul
                # This is a pre-compute pattern
                return "pre_compute"
            elif other_mnem == "sub":
                # sub+mul → pre-compute subtraction, then multiply
                return "pre_compute"
            elif other_mnem in ("or", "ori", "and", "andi", "xor", "xori"):
                return "pre_compute"
            elif other_mnem in ("sll", "slli", "srl", "srli", "sra", "srai"):
                return "pre_compute"
            else:
                return "pre_compute"

    # For longer chains, check where mul appears
    if n >= 3:
        return "complex"

    return "complex"


def is_dsp_unfriendly_mul_fusion(mnemonics: Tuple[str, ...]) -> bool:
    """Return True if this mul-involving fusion is NOT DSP-friendly.

    The cv32e40p multiplier exposes exactly two single-cycle datapaths
    that fold cleanly into the existing DSP MAC:

      MUL_MAC32 :  a * b + c     ←  fuse "mul, add"  or  "add, mul"
      MUL_MSU32 :  c - (a * b)   ←  fuse "mul, sub"  or  "sub, mul"
                                    (after operand swap as needed)

    Anything else — multi-cycle ``mulh*``, immediate-form siblings
    (``mul + addi``, ``andi + mul``, ...), shift mixed with mul, bitwise
    mixed with mul, post-shift on the product, 3+ gram chains — drops
    extra logic onto the multiplier critical path and destroys FPGA
    Fmax.  Reject all of those.

    Pure ALU fusions (no mul mnemonic at all) always return False.

    Returns True for fusions that should be REJECTED.
    """
    norm = tuple(normalize_mnemonic(m) for m in mnemonics)

    # Pure ALU fusion — caller must not reject.
    if not any(m in MUL_MNEMONICS for m in norm):
        return False

    # Reject 3+ gram chains containing any mul-class mnemonic.
    if len(norm) != 2:
        return True

    # Multi-cycle mulh* never folds into a single-cycle DSP path.
    if "mulh" in norm or "mulhu" in norm or "mulhsu" in norm:
        return True

    # Both must be a single-cycle pair: exactly one ``mul`` and one
    # register-form ``add`` or ``sub``.  Anything else (immediate-form
    # add/sub, shift, bitwise, compare, ...) requires extra logic on
    # the DSP path.
    #
    # We further restrict to mul-FIRST patterns only.  The RTL decoder
    # (cv32e40p_decoder.sv) only implements ``mul+add`` (MUL_MAC32) and
    # ``mul+sub`` (MUL_MSU32) under OPCODE_CUSTOM_0.  The reverse-order
    # variants ``add+mul`` and ``sub+mul`` would mathematically fold into
    # the same MAC/MSU paths but require an extra operand-mux on the
    # multiplier critical path, hurting FPGA Fmax.  We chose to keep
    # only the mul-first variants in the RTL; if GCC still produces
    # mul-second fusions, the simulator hits ``illegal_insn`` and traps.
    # Reject mul-second patterns here so the GCC combine pass never
    # emits them.
    if norm[1] == "mul":
        return True
    # mul-first: allow only add/sub as the second mnemonic.
    other = norm[1]
    return other not in ("add", "sub")


# ═══════════════════════════════════════════════════════════════════════════
# Hardware reuse strategy for ALU-classified fused operations
# ═══════════════════════════════════════════════════════════════════════════
#
# Instead of emitting inline combinational expressions in the result mux
# (which synthesises duplicate adders/shifters), we classify each fusion
# to determine whether it can steer the EXISTING partitioned adder via
# input muxing, reading ``adder_result`` in the result mux.
#
# Strategies:
#   "inline"       — keep current inline SV expression (logic-only, cheap)
#   "reuse_adder"  — steer adder_op_a / adder_op_b, read adder_result
#
# Mnemonics that are pure logic (zero adder/shifter cost):
_LOGIC_MNEMONICS = frozenset({"and", "andi", "or", "ori", "xor", "xori", "xori_neg1"})
# Mnemonics that use the adder:
_ADDER_MNEMONICS = frozenset({"add", "addi", "sub"})
# Mnemonics that are constant shifts (just wire routing at synthesis):
_CONST_SHIFT_MNEMONICS = frozenset({"slli", "srli", "srai"})


def classify_hw_strategy(
    mnemonics: Tuple[str, ...],
    imm_values: Dict[int, int],
) -> str:
    """Classify an ALU-classified fused operation's hardware reuse strategy.

    Examines the mnemonic chain to determine if the existing partitioned
    adder can be reused instead of synthesising a new one inline.

    For a fusion to reuse the adder, it must have EXACTLY ONE add/sub step,
    and all pre-computation before that step must be either:
      - Pure logic (AND/OR/XOR) — cheap, doesn't need adder
      - Constant shift (slli/srli/srai with hardcoded imm) — wire routing

    For a 2-gram ``logic+add``:
      adder_op_a = (operand_a_i LOGIC operand_b_i), adder_op_b = operand_c_i
      result = adder_result

    For a 2-gram ``const_shift+add``:
      The shift is constant so it's free wiring.  But the adder inputs
      need the shifted value.  We steer:
        adder_op_a = (operand_a_i << const), adder_op_b = operand_b_i
      This reuses the adder but the shift itself is still inline (0-cost wire).

    Returns:
        "reuse_adder" — steer existing adder inputs, read adder_result
        "inline"      — keep current inline expression
    """
    if not mnemonics:
        return "inline"

    norm = tuple(normalize_mnemonic(m) for m in mnemonics)

    # Resolve folded immediates (e.g., xori -1 → xori_neg1)
    resolved = []
    for i, mnem in enumerate(norm):
        imm_val = imm_values.get(i)
        if imm_val is not None and (mnem, imm_val) in IMM_FOLDS:
            resolved.append(IMM_FOLDS[(mnem, imm_val)])
        else:
            resolved.append(mnem)

    # Count how many steps require the adder
    adder_count = sum(1 for m in resolved if m in _ADDER_MNEMONICS)

    if adder_count == 0:
        # No adder use — check if we can reuse the barrel shifter
        if classify_hw_strategy_shifter(mnemonics, imm_values):
            return "reuse_shifter"
        return "inline"

    if adder_count > 1:
        # Multiple adder uses (e.g., sub+add, add+sub) — can't reuse
        # single adder for both.  Keep inline.
        return "inline"

    # Exactly one adder use.  Check that all non-adder steps are cheap.
    adder_idx = None
    for i, m in enumerate(resolved):
        if m in _ADDER_MNEMONICS:
            adder_idx = i
            break

    assert adder_idx is not None

    # All steps before the adder must be logic or constant shifts
    for i in range(adder_idx):
        m = resolved[i]
        if m in _LOGIC_MNEMONICS:
            continue
        if m in _CONST_SHIFT_MNEMONICS and i in imm_values:
            continue  # Constant shift — just wiring
        # Variable shift or other complex op — can't easily pre-compute
        return "inline"

    # Steps after the adder: if they're cheap (pure logic, constant shift),
    # we can still reuse the adder and apply the post-op to adder_result
    # in the result mux.  Only if there are complex post-ops (variable
    # shift, another add, comparison, etc.) do we fall back to inline.
    for i in range(adder_idx + 1, len(resolved)):
        m = resolved[i]
        if m in _LOGIC_MNEMONICS:
            continue  # Cheap post-op on adder_result
        if m in _CONST_SHIFT_MNEMONICS and i in imm_values:
            continue  # Constant shift on adder_result = free wiring
        # Complex post-op — can't simply post-process adder_result
        return "inline"

    return "reuse_adder"


# ── Shifter reuse: first op is a shift, rest are cheap ──
_SHIFT_MNEMONICS = frozenset({"sll", "srl", "sra", "slli", "srli", "srai"})


def classify_hw_strategy_shifter(
    mnemonics: Tuple[str, ...],
    imm_values: Dict[int, int],
) -> bool:
    """Return True if this pattern should reuse the existing barrel shifter.

    First op must be a shift. Remaining ops must be pure logic (and/or/xor/andi)
    — NOT another shift (can't use barrel shifter twice in one cycle).
    """
    if len(mnemonics) < 2:
        return False
    norm = tuple(normalize_mnemonic(m) for m in mnemonics)
    # First op must be a constant shift
    if norm[0] not in _CONST_SHIFT_MNEMONICS or 0 not in imm_values:
        return False
    # Remaining ops must be pure logic only (no shifts, no adder)
    for i in range(1, len(norm)):
        m = norm[i]
        if m in _LOGIC_MNEMONICS:
            continue
        return False
    return True


@dataclass
class FusedOperation:
    """A fused operation to be added to the RTL."""

    name: str  # e.g., "XORI_AND"
    mnemonics: Tuple[str, ...]  # e.g., ('xori', 'and')
    imm_values: Dict[int, int]  # position → immediate value (for folds)
    n_inputs: int  # 2 or 3 external inputs
    n_outputs: int  # 1 or 2 external outputs
    sv_expression: str  # SystemVerilog expression for result
    funct7: int  # Assigned encoding
    funct3: int  # Assigned encoding
    opcode: int = OPCODE_CUSTOM_0  # Which CUSTOM opcode space (0x0B or 0x2B)
    variant: int = 0  # Operand order variant (0=default)
    description: str = ""
    # Execution unit routing (set by classifier)
    execution_unit: str = "alu"  # "alu" or "mult"
    mult_pattern: str = ""  # "mac", "msu", "pre_compute", etc.
    mult_opcode_sv: str = ""  # SV enum value: "MUL_MAC32", "MUL_MSU32", etc.
    # Hardware reuse strategy (set by classify_hw_strategy)
    hw_strategy: str = "inline"  # "inline" or "reuse_adder"
    # Immediate encoding strategy per position:
    #   "hardcoded" — baked into SV expression (default, current behavior)
    #   "rs3_5bit" — read from instr_rdata_i[31:27], 0-31
    #   "rs2_rs3_10bit" — read from {instr[24:20], instr[31:27]}, 0-1023 (future)
    imm_encoding: Dict[int, str] = field(default_factory=dict)
    # Set of immediate values actually observed in the workload binary for
    # this parametric pattern's fused_imm[4:0] field (rs3 / instr[31:27]).
    # Populated by ``compute_filtered_ops`` after binary inspection.
    # Empty for non-parametric patterns and for new (unobserved) ops.
    # Drives two RTL optimizations:
    #   1. K=1 hardcoding — when len(used_imm_values)==1, emit the literal
    #      constant in RTL instead of the parametric ``fused_imm_i[4:0]``
    #      expression.
    #   2. Per-direction shared mux — the union of these sets across all
    #      parametric ops keys the shared sh_r / sh_l / sh_ra / a_and muxes.
    used_imm_values: Set[int] = field(default_factory=set)


@dataclass
class EncodingAllocator:
    """
    Tracks and allocates instruction encodings in CUSTOM_0 space.

    Encoding scheme:
    - funct3 = 000: R-type (2 inputs, 1 output)
    - funct3 = 001: R-type (2 inputs, 2 outputs) — needs dual-write
    - funct3 = 010: R4-type (3 inputs, 1 output) — uses rs3 field
    - funct3 = 011: R4-type (3 inputs, 2 outputs)
    - funct3 = 100-111: reserved

    Within each funct3 class:
    - funct7[6:2] = operation ID (0-31, up to 32 operations per class)
    - funct7[1:0] = operand order variant (0-3)
    """

    # Track used funct7 values per funct3
    # funct3 0-3: ALU-only fusions
    # funct3 4-7: LSU fusions (load_compute, compute_store)
    used: Dict[int, set] = field(
        default_factory=lambda: {
            0: set(),
            1: set(),
            2: set(),
            3: set(),
            4: set(),
            5: set(),
            6: set(),
            7: set(),
        }
    )
    # Track all allocated operations
    operations: List[FusedOperation] = field(default_factory=list)

    # Separate tracking per opcode space (CUSTOM-0 and CUSTOM-1)
    used_by_opcode: Dict[int, Dict[int, set]] = field(default_factory=dict)

    def __post_init__(self):
        # Initialize per-opcode tracking if not set
        if not self.used_by_opcode:
            self.used_by_opcode = {
                OPCODE_CUSTOM_0: {i: set() for i in range(8)},
                OPCODE_CUSTOM_1: {i: set() for i in range(8)},
            }
        # Migrate legacy 'used' into CUSTOM-0 space
        for f3, f7s in self.used.items():
            if f7s:
                c0 = self.used_by_opcode.setdefault(OPCODE_CUSTOM_0, {})
                c0.setdefault(f3, set()).update(f7s)

    def allocate(self, n_inputs: int, n_outputs: int, variant: int = 0) -> Tuple[int, int, int]:
        """
        Allocate a (funct3, funct7, opcode) triple for the given port requirements.

        Tries CUSTOM-0 first; if full, overflows to CUSTOM-1.

        Returns: (funct3, funct7, opcode)
        Raises ValueError if both opcode spaces are full.
        """
        # Select funct3 based on port requirements
        if n_inputs <= 2 and n_outputs <= 1:
            funct3 = 0b000
        elif n_inputs <= 2 and n_outputs <= 2:
            funct3 = 0b001
        elif n_inputs <= 3 and n_outputs <= 1:
            funct3 = 0b010
        elif n_inputs <= 3 and n_outputs <= 2:
            funct3 = 0b011
        else:
            raise ValueError(f"Cannot encode {n_inputs} inputs / {n_outputs} outputs in CUSTOM opcode space")

        # Try each opcode space in order
        for opcode in (OPCODE_CUSTOM_0, OPCODE_CUSTOM_1):
            try:
                funct7 = self._try_allocate(opcode, funct3, variant)
                return funct3, funct7, opcode
            except ValueError:
                continue

        # If primary funct3 is full, try alternate funct3 values (4-7 for R4 overflow)
        if n_inputs >= 3:
            for alt_f3 in (0b100, 0b101, 0b110, 0b111):
                for opcode in (OPCODE_CUSTOM_0, OPCODE_CUSTOM_1):
                    try:
                        funct7 = self._try_allocate(opcode, alt_f3, variant)
                        return alt_f3, funct7, opcode
                    except ValueError:
                        continue

        raise ValueError(f"Encoding space full for funct3={funct3:03b} in both CUSTOM-0 and CUSTOM-1")

    def _try_allocate(self, opcode: int, funct3: int, variant: int = 0) -> int:
        """Try to allocate in a specific opcode space. Raises ValueError if full."""
        space = self.used_by_opcode.setdefault(opcode, {})
        used_f7s = space.setdefault(funct3, set())
        is_r4 = funct3 in (0b010, 0b011, 0b100, 0b101, 0b110, 0b111)

        if is_r4:
            for op_id in range(4):
                funct7 = op_id
                if funct7 not in used_f7s:
                    used_f7s.add(funct7)
                    # Also update legacy 'used' dict for backwards compat
                    self.used.setdefault(funct3, set()).add(funct7)
                    return funct7
            raise ValueError(f"R4-type funct3={funct3:03b} full in 0x{opcode:02x}")
        else:
            for op_id in range(32):
                funct7 = (op_id << 2) | (variant & 0x3)
                if funct7 not in used_f7s:
                    used_f7s.add(funct7)
                    self.used.setdefault(funct3, set()).add(funct7)
                    return funct7
            raise ValueError(f"R-type funct3={funct3:03b} full in 0x{opcode:02x}")

    def register(self, op: FusedOperation):
        """Register an already-allocated operation."""
        if op.funct3 not in self.used:
            self.used[op.funct3] = set()
        self.used[op.funct3].add(op.funct7)
        self.operations.append(op)


class RTLGenerator:
    """
    Generates SystemVerilog code for ALU-only fused operations.

    Usage:
        gen = RTLGenerator('targets/cv32e40p/rtl')
        gen.add_existing()  # Parse existing ARVIS entries
        gen.add_fusion(('xori', 'and'), imm_values={0: -1},
                       n_inputs=2, n_outputs=1)
        gen.add_fusion(('slli', 'srli', 'add'), ...)
        gen.write()         # Patch the RTL files in-place
    """

    def __init__(self, rtl_dir: str, workspace=None):
        self.rtl_dir = Path(rtl_dir)
        self.pkg_path = self.rtl_dir / "include" / "cv32e40p_pkg.sv"
        self.decoder_path = self.rtl_dir / "cv32e40p_decoder.sv"
        self.alu_path = self.rtl_dir / "cv32e40p_alu.sv"
        self.workspace = workspace  # Optional RTLWorkspace for managed writes
        self.allocator = EncodingAllocator()
        self.fused_ops: List[FusedOperation] = []
        # Next ALU opcode value (7-bit enum, start after existing)
        self._next_alu_opcode = 0b1000000  # 64, first free slot

    def add_existing(self):
        """Parse existing ARVIS entries and all ALU enum values to avoid conflicts."""
        pkg_text = self.pkg_path.read_text()

        # Scan ALL ALU_* enum values in the entire pkg file to find the
        # maximum used value.  This prevents collisions with baseline
        # enums like ALU_BREV that live outside the ARVIS pragma region.
        all_alu_pat = re.compile(r"ALU_(\w+)\s*=\s*7'b([01]+)")
        for m in all_alu_pat.finditer(pkg_text):
            val = int(m.group(2), 2)
            self._next_alu_opcode = max(self._next_alu_opcode, val + 1)

        # Additionally log fused entries between pragmas (informational)
        pragma_m = re.search(
            r"// ARVIS_FUSED_BEGIN: alu_opcodes\n(.*?)\n?\s*// ARVIS_FUSED_END: alu_opcodes",
            pkg_text,
            re.DOTALL,
        )
        if pragma_m:
            for line in pragma_m.group(1).splitlines():
                em = re.match(r"\s*ALU_(\w+)\s*=\s*7'b(\d+)", line)
                if em:
                    name = em.group(1)
                    val = int(em.group(2), 2)
                    print(f"  Found existing fused: ALU_{name} = 7'b{val:07b}")

        # Parse existing decoder entries to register used funct7/funct3
        dec_text = self.decoder_path.read_text()
        dec_m = re.search(
            r"// ARVIS_FUSED_BEGIN: decoder_custom0\n(.*?)\n\s*// ARVIS_FUSED_END: decoder_custom0",
            dec_text,
            re.DOTALL,
        )
        if dec_m:
            # Extract funct3/funct7 pairs from the decoder
            for f3_match in re.finditer(r"3'b(\d+):", dec_m.group(1)):
                funct3 = int(f3_match.group(1), 2)
                # Find funct7 checks within this block
                # Look for: instr_rdata_i[31:25] == 7'b0000001
                rest = dec_m.group(1)[f3_match.end() :]
                for f7_match in re.finditer(r"7'b(\d+)", rest):
                    funct7 = int(f7_match.group(1), 2)
                    self.allocator.used[funct3].add(funct7)
                    break  # Only first funct7 per funct3 block

    def _build_sv_expression(
        self,
        mnemonics: Tuple[str, ...],
        imm_values: Dict[int, int],
        chain_on_b: bool = False,
        parametric_imm: bool = False,
    ) -> str:
        """
        Build the SystemVerilog expression for a fused operation.

        Chains the operations: first op gets operand_a_i and operand_b_i,
        subsequent ops feed the result of the previous.

        For ALU-only 2-input/1-output:
          operand_a_i = rs1, operand_b_i = rs2
          Result = chain of operations applied to these inputs.

        When ``imm_values[i]`` is set for position *i*, that
        instruction's ``{b}`` placeholder is replaced with the
        hardcoded literal value (e.g. ``32'd1`` for a shift amount)
        instead of ``operand_b_i``.
        """
        # Resolve mnemonics and per-position operand-b sources.
        # Each entry in ``b_sources`` is the SV expression that
        # replaces ``{b}`` for the instruction at that position.
        #
        # The cv32e40p ALU has 3 input ports: operand_a_i, operand_b_i,
        # operand_c_i.  The first instruction always receives operand_a_i
        # via ``{a}``.  For ``{b}`` slots that need a runtime register
        # (not hardcoded and not unary), we assign them in order:
        #   1st runtime {b} → operand_b_i
        #   2nd runtime {b} → operand_c_i
        # This correctly maps up to 3 external register inputs.
        _RUNTIME_B_PORTS = ["operand_b_i", "operand_c_i"]
        # Parametric immediate fields: rs3[31:27] first, rs2[24:20] second
        _PARAM_IMM_FIELDS = ["fused_imm_i[4:0]", "fused_imm_i[9:5]"]
        _param_imm_idx = 0

        resolved: List[str] = []
        b_sources: List[str] = []
        runtime_b_idx = 0  # index into _RUNTIME_B_PORTS
        _param_imm_idx = 0  # index for parametric immediate fields
        for i, mnem in enumerate(mnemonics):
            # Normalize compressed → base-ISA (defensive; signatures
            # should already be normalized by the analysis layer)
            base_mnem = normalize_mnemonic(mnem)
            imm_val = imm_values.get(i)
            # Check for special folds first (e.g. xori -1 → NOT)
            if imm_val is not None and (base_mnem, imm_val) in IMM_FOLDS:
                resolved.append(IMM_FOLDS[(base_mnem, imm_val)])
                b_sources.append("operand_b_i")  # unused for unary
            else:
                resolved.append(base_mnem)
                sv_info = MNEMONIC_TO_SV.get(base_mnem, {})
                if imm_val is not None:
                    if parametric_imm:
                        # Parametric: immediate passed via operand_c_i (rs3→REGC_S4)
                        # or operand_b_i (rs2) for second immediate
                        sv_lit = _PARAM_IMM_FIELDS[_param_imm_idx]
                        _param_imm_idx += 1
                    else:
                        # Hardcode the immediate as a 32-bit literal.
                        if imm_val < 0:
                            sv_lit = f"32'h{imm_val & 0xFFFFFFFF:08X}"
                        else:
                            sv_lit = f"32'd{imm_val}"
                    b_sources.append(sv_lit)
                elif sv_info.get("unary"):
                    # Unary ops don't use {b} at runtime
                    b_sources.append("operand_b_i")  # placeholder, unused
                else:
                    # Runtime register input — assign next available port
                    if runtime_b_idx < len(_RUNTIME_B_PORTS):
                        b_sources.append(_RUNTIME_B_PORTS[runtime_b_idx])
                        runtime_b_idx += 1
                    else:
                        raise ValueError(
                            f"Fusion {' → '.join(mnemonics)} needs "
                            f">{len(_RUNTIME_B_PORTS) + 1} external register inputs "
                            f"(max {len(_RUNTIME_B_PORTS) + 1}: "
                            f"operand_a_i + {', '.join(_RUNTIME_B_PORTS)})"
                        )

        expr = self._chain_ops(resolved, b_sources, chain_on_b=chain_on_b)

        # ── Validate: R-type (≤2 inputs) must not reference operand_c_i ──
        # operand_c_i is only available when the decoder sets regc_used_o=1
        # and regc_mux_o=REGC_S4 (reading instr[31:27] as a register addr).
        # For R-type encoding, instr[31:25] is funct7 (decode bits), so
        # operand_c_i is undefined.  Any expression referencing it will
        # read garbage and cause incorrect results / simulation timeouts.
        n_runtime_ports = runtime_b_idx  # how many runtime {b} slots used
        total_ext_inputs = 1 + n_runtime_ports  # 1 for operand_a + runtime {b}s
        if "operand_c_i" in expr and total_ext_inputs <= 2 and not parametric_imm:
            raise ValueError(
                f"SV expression for {' → '.join(mnemonics)} references "
                f"operand_c_i but only has {total_ext_inputs} external inputs "
                f"(R-type encoding). operand_c_i is not connected for R-type. "
                f"Expression: {expr}"
            )

        # Post-process: replace invalid bit-selects from literals.
        # Shift expressions use {b}[4:0], but when {b} is a literal
        # like 32'd16, the result "32'd16[4:0]" is invalid SV.
        # Replace "32'd<N>[4:0]" with "5'd<N>" (already 5-bit width).
        import re as _re

        def _fix_literal_bitselect(m: _re.Match) -> str:
            val = int(m.group(1))
            return f"5'd{val}"

        expr = _re.sub(r"32'd(\d+)\[4:0\]", _fix_literal_bitselect, expr)

        # Also handle hex literals: 32'hXXXX[4:0]
        def _fix_hex_bitselect(m: _re.Match) -> str:
            val = int(m.group(1), 16) & 0x1F
            return f"5'd{val}"

        expr = _re.sub(r"32'h([0-9A-Fa-f]+)\[4:0\]", _fix_hex_bitselect, expr)

        # Parametric immediate: operand_c_i[4:0] is already valid SV
        # (operand_c_i carries rs3 field value via regc_mux_o=REGC_S4)

        expr = _re.sub(r"32'h([0-9A-Fa-f]+)\[4:0\]", _fix_hex_bitselect, expr)

        # Clean double bit-selects: fused_imm_i[4:0][4:0] → fused_imm_i[4:0]
        expr = _re.sub(r"(fused_imm_i\[\d+:\d+\])\[4:0\]", r"\1", expr)

        return expr

    def _chain_ops(
        self,
        ops: List[str],
        b_sources: List[str],
        chain_on_b: bool = False,
    ) -> str:
        """Chain N operations into a single SV expression.

        Parameters
        ----------
        ops : list[str]
            Resolved mnemonic keys into ``MNEMONIC_TO_SV``.
        b_sources : list[str]
            Per-instruction SV expression for the ``{b}`` slot.
            Either ``"operand_b_i"`` (register) or a literal.
        """
        if not ops:
            raise ValueError("Empty operation list")

        infos = [MNEMONIC_TO_SV.get(op) for op in ops]
        for idx, info in enumerate(infos):
            if info is None:
                raise ValueError(f"Unknown mnemonic: {ops[idx]}")

        # First instruction: ``{a}`` = operand_a_i
        # Always pass both a and b — even "unary" ops may reference {b}
        # (e.g., lui uses ({b} << 12)).  The b_sources list already
        # contains the correct placeholder for each position.
        info0 = infos[0]
        assert info0 is not None
        result = str(info0["expr"]).format(a="operand_a_i", b=b_sources[0])

        # Subsequent instructions: ``{a}`` = previous result
        for i in range(1, len(ops)):
            info_i = infos[i]
            assert info_i is not None
            if chain_on_b and i == len(ops) - 1:
                # Chain feeds {b} slot of last op (e.g., sub rd, ext, chain)
                result = str(info_i["expr"]).format(a=b_sources[i], b=result)
            else:
                result = str(info_i["expr"]).format(a=result, b=b_sources[i])

        return str(result)  # type: ignore[no-any-return]  # Dict[str, Any] propagates Any

    def _build_sv_expression_3gram_reuse(self, mnemonics: tuple, chain_positions: tuple = (0, 0)) -> str:
        """Build SV expression for 3-gram chains where 3rd op reuses operand_a.

        Chain positions specify where the chain result feeds into each
        subsequent op (0=first operand slot, 1=second operand slot).

        For chain_positions=(0,0) — chain→rs1 for both op2 and op3:
          t1 = operand_a OP1 operand_b
          t2 = t1 OP2 operand_c         (chain feeds rs1)
          rd = t2 OP3 operand_a          (chain feeds rs1, reuse operand_a)

        For chain_positions=(1,0) — chain→rs2 for op2, chain→rs1 for op3:
          t1 = operand_a OP1 operand_b
          t2 = operand_c OP2 t1          (chain feeds rs2)
          rd = t2 OP3 operand_a          (chain feeds rs1, reuse operand_a)

        Uses exactly 3 read ports: operand_a, operand_b, operand_c.
        """
        if len(mnemonics) != 3:
            raise ValueError(f"_build_sv_expression_3gram_reuse only handles 3-grams, got {len(mnemonics)}")

        infos = []
        for m in mnemonics:
            info = MNEMONIC_TO_SV.get(m)
            if info is None:
                raise ValueError(f"No SV mapping for mnemonic '{m}'")
            infos.append(info)

        # Step 1: t1 = operand_a OP1 operand_b
        t1 = infos[0]["expr"].format(a="operand_a_i", b="operand_b_i")

        # Step 2: t2 depends on chain_positions[0]
        if chain_positions[0] == 0:
            # chain→rs1: t2 = t1 OP2 operand_c
            t2 = infos[1]["expr"].format(a=t1, b="operand_c_i")
        else:
            # chain→rs2: t2 = operand_c OP2 t1
            t2 = infos[1]["expr"].format(a="operand_c_i", b=t1)

        # Step 3: rd depends on chain_positions[1]
        if chain_positions[1] == 0:
            # chain→rs1: rd = t2 OP3 operand_a (match_dup reuse)
            result = infos[2]["expr"].format(a=t2, b="operand_a_i")
        else:
            # chain→rs2: rd = operand_a OP3 t2
            result = infos[2]["expr"].format(a="operand_a_i", b=t2)

        return str(result)  # type: ignore[no-any-return]  # Dict[str, Any] propagates Any

    def add_fusion(
        self,
        mnemonics: Tuple[str, ...],
        imm_values: Optional[Dict[int, int]] = None,
        n_inputs: int = 2,
        n_outputs: int = 1,
        variant: int = 0,
        description: str = "",
        chain_on_b: bool = False,
        parametric_imm: bool = False,
    ) -> FusedOperation:
        """
        Add a fused operation to be generated.

        Args:
            mnemonics: Tuple of ISA mnemonics, e.g., ('xori', 'and')
            imm_values: Dict of {position: value} for folded immediates
            n_inputs: Number of external register inputs (2 or 3)
            n_outputs: Number of external register outputs (1 or 2)
            variant: Operand order variant (0-3)
            description: Human-readable description

        Returns: The allocated FusedOperation
        """
        if imm_values is None:
            imm_values = {}

        # Normalize compressed mnemonics to base-ISA equivalents.
        # The analysis layer already does this in _compute_signature(),
        # but we normalize defensively here to guarantee that the RTL
        # generator never creates duplicate decoder entries for what
        # the CV32E40P hardware sees as the same decompressed instruction.
        mnemonics = tuple(normalize_mnemonic(m) for m in mnemonics)

        # Build name: ALU_OP1_OP2[_V{variant}]
        name_parts = []
        for i, m in enumerate(mnemonics):
            base = m.upper()
            key = (m, imm_values.get(i))
            if key in IMM_FOLDS:
                # E.g., xori with -1 → just "XORI" (the neg1 is implicit)
                pass
            name_parts.append(base)
        name = "_".join(name_parts)
        if variant > 0:
            name += f"_V{variant}"
        # Append HC immediate values to disambiguate same-pattern different-imm
        if imm_values and not parametric_imm:
            imm_suffix = "_".join(
                f"HC{k}eq{abs(v)}{'n' if v < 0 else ''}"
                for k, v in sorted(imm_values.items())
                if (mnemonics[k], v) not in IMM_FOLDS
            )
            if imm_suffix:
                name += f"_{imm_suffix}"
        elif parametric_imm:
            name += "_PARAM"
        if chain_on_b:
            name += "_REV"

        # Build SV expression
        sv_expr = self._build_sv_expression(mnemonics, imm_values, chain_on_b=chain_on_b, parametric_imm=parametric_imm)

        # Allocate encoding (may overflow from CUSTOM-0 to CUSTOM-1)
        funct3, funct7, opcode = self.allocator.allocate(n_inputs, n_outputs, variant)

        if not description:
            description = f"Fused {' → '.join(mnemonics)}"

        # ── Classify execution unit ──
        exec_unit = classify_execution_unit(mnemonics)
        mult_pat = ""
        mult_opc_sv = ""

        if exec_unit == "mult":
            mult_pat = classify_mult_pattern(mnemonics)
            # Map known patterns to existing MUL opcodes (zero new HW!)
            if mult_pat == "mac":
                mult_opc_sv = "MUL_MAC32"
            elif mult_pat == "msu":
                mult_opc_sv = "MUL_MSU32"
            else:
                # Pre-compute, post-compute, complex → need new MUL opcodes
                # These will be generated as MUL_FUSED_xxx enum entries
                mult_opc_sv = f"MUL_FUSED_{name}"

        # ── Classify hardware reuse strategy for ALU ops ──
        hw_strat = "inline"
        if exec_unit == "alu":
            hw_strat = classify_hw_strategy(mnemonics, imm_values)

        # Build imm_encoding map
        imm_enc: Dict[int, str] = {}
        if parametric_imm:
            for pos in imm_values:
                if (mnemonics[pos], imm_values[pos]) not in IMM_FOLDS:
                    imm_enc[pos] = "rs3_5bit"  # future: "rs2_rs3_10bit"
        # else: all hardcoded (default empty dict)

        op = FusedOperation(
            name=name,
            mnemonics=mnemonics,
            imm_values=imm_values,
            n_inputs=n_inputs,
            n_outputs=n_outputs,
            sv_expression=sv_expr,
            funct7=funct7,
            funct3=funct3,
            opcode=opcode,
            variant=variant,
            description=description,
            execution_unit=exec_unit,
            mult_pattern=mult_pat,
            mult_opcode_sv=mult_opc_sv,
            hw_strategy=hw_strat,
            imm_encoding=imm_enc,
        )

        self.fused_ops.append(op)
        self.allocator.register(op)
        return op

    def _alu_ops(self) -> List[FusedOperation]:
        """Return only ALU-classified fused operations."""
        return [op for op in self.fused_ops if op.execution_unit == "alu"]

    def _mult_ops(self) -> List[FusedOperation]:
        """Return only MULT-classified fused operations."""
        return [op for op in self.fused_ops if op.execution_unit == "mult"]

    def _mult_ops_needing_new_opcode(self) -> List[FusedOperation]:
        """MULT ops that can't reuse MUL_MAC32/MUL_MSU32 and need new opcodes."""
        return [op for op in self._mult_ops() if op.mult_opcode_sv not in ("MUL_MAC32", "MUL_MSU32")]

    def generate_pkg(self) -> str:
        """Generate ALU opcode enum entries for ALU-classified ops only.

        MULT-classified ops that reuse existing MUL opcodes (MAC32, MSU32)
        don't need ALU enum entries at all.  MULT ops needing new opcodes
        get entries in the mul_opcode_e enum via generate_mult_pkg().

        Uses ``_next_alu_opcode`` as the starting value so that generated
        enums never collide with any existing ``ALU_*`` value in the
        baseline package (including values outside the ARVIS pragma
        region such as ``ALU_BREV``).

        The last baseline enum entry before the ARVIS_FUSED_BEGIN pragma
        has no trailing comma.  We emit a leading comma on the first
        fused entry so that the enum remains syntactically valid.
        """
        alu_ops = self._alu_ops()
        if not alu_ops:
            return ""
        lines = []
        base = self._next_alu_opcode
        max_val = base + len(alu_ops) - 1
        needed_bits = max(7, max_val.bit_length())
        for i, op in enumerate(alu_ops):
            alu_val = base + i
            # Leading comma before first entry to continue from last baseline enum value;
            # trailing comma on all entries except the last.
            if i == 0:
                prefix = ","
            else:
                prefix = ""
            comma = "," if i < len(alu_ops) - 1 else ""
            entry = f"{prefix}ALU_{op.name} = {needed_bits}'b{alu_val:0{needed_bits}b}{comma}"
            lines.append(f"    {entry}  // {op.description}")
        return "\n".join(lines)

    def generate_mult_pkg(self) -> str:
        """Generate MUL opcode enum entries for MULT ops needing new opcodes.

        Operations that map to existing MUL_MAC32/MUL_MSU32 don't need
        new enum entries.  Only pre_compute, post_compute, complex, and
        mul_post_sub patterns need new MUL_FUSED_xxx entries.

        Returns SV code to insert between ARVIS_FUSED_BEGIN/END: mul_opcodes
        pragmas in cv32e40p_pkg.sv.
        """
        new_ops = self._mult_ops_needing_new_opcode()
        if not new_ops:
            return ""

        # MUL_OP_WIDTH = 3 → values 0..7.  Existing: 0-6 (MAC32..H).
        # We start at 7.  If we need more, MUL_OP_WIDTH must be widened.
        lines = []
        base_val = 0b111  # 7, first free slot after MUL_H=6
        # Determine needed width: if any value ≥ 8, all need 4-bit width
        max_val = base_val + len(new_ops) - 1
        needed_width = max(3, max_val.bit_length())
        for i, op in enumerate(new_ops):
            mul_val = base_val + i
            if i == 0:
                prefix = ","
            else:
                prefix = ""
            comma = "," if i < len(new_ops) - 1 else ""
            entry = f"{prefix}{op.mult_opcode_sv} = {needed_width}'b{mul_val:0{needed_width}b}{comma}"
            lines.append(f"    {entry}  // {op.description}")
        return "\n".join(lines)

    def _decoder_signals_for_op(self, op: FusedOperation, indent: str) -> List[str]:
        """Generate decoder signal assignments for a fused operation.

        Handles both ALU-only and LSU-involved fused operations.

        ALU-only operations set:
          alu_operator_o, regfile_alu_we, rega_used_o, [regb_used_o, regc_used_o]

        LSU load operations additionally set:
          data_req, regfile_mem_we (instead of regfile_alu_we),
          data_type_o, data_sign_extension_o

        LSU store operations additionally set:
          data_req, data_we_o, data_type_o, alu_op_c_mux_sel_o
        """
        # Check if this is an LSU fused operation (from load_compute.py)
        # by checking for the memory_type attribute
        memory_type = getattr(op, "memory_type", "")

        if memory_type in ("load", "load_compute"):
            # LSU load fusion: ALU computes address, LSU loads data
            # For load_compute: cycle 1 loads, cycle 2 ALU computes on
            # loaded data.  The decoder still issues the load request;
            # the ALU result mux expression operates on the loaded value
            # that the LSU routes back via operand_a_i on cycle 2.
            dt_sv = getattr(op, "data_type_sv", "2'b00")
            se_sv = getattr(op, "sign_ext_sv", "2'b00")
            signals = [
                f"{indent}alu_operator_o = ALU_{op.name};",
                f"{indent}rega_used_o    = 1'b1;",
            ]
            if op.n_inputs >= 2:
                signals.append(f"{indent}regb_used_o    = 1'b1;")
            if op.n_inputs >= 3:
                signals.append(f"{indent}regc_used_o    = 1'b1;")
                signals.append(f"{indent}regc_mux_o     = REGC_S4;")
            signals.extend(
                [
                    f"{indent}data_req       = 1'b1;",
                    f"{indent}regfile_mem_we = 1'b1;",
                    f"{indent}data_type_o    = {dt_sv};",
                    f"{indent}data_sign_extension_o = {se_sv};",
                ]
            )
            return signals

        elif memory_type == "store":
            # LSU store fusion: ALU computes address, operand_c holds store data
            dt_sv = getattr(op, "data_type_sv", "2'b00")
            signals = [
                f"{indent}alu_operator_o = ALU_{op.name};",
                f"{indent}rega_used_o    = 1'b1;",
            ]
            if op.n_inputs >= 2:
                signals.append(f"{indent}regb_used_o    = 1'b1;")
            signals.extend(
                [
                    f"{indent}data_req       = 1'b1;",
                    f"{indent}data_we_o      = 1'b1;",
                    f"{indent}data_type_o    = {dt_sv};",
                ]
            )
            if op.n_inputs >= 3:
                signals.extend(
                    [
                        f"{indent}regc_used_o    = 1'b1;",
                        f"{indent}regc_mux_o     = REGC_S4;",
                        f"{indent}alu_op_c_mux_sel_o = OP_C_REGC_OR_FWD;",
                    ]
                )
            else:
                signals.append(f"{indent}alu_op_c_mux_sel_o = OP_C_REGB_OR_FWD;")
            return signals

        elif op.execution_unit == "mult":
            # ── MULT-routed fusion ──
            # Route to the multiplier unit instead of ALU.
            # The decoder sets mult_int_en + mult_operator_o so the
            # ex_stage muxes mult_result to the write-back port.
            # alu_en must be 0 to prevent the ALU from running in parallel.
            #
            # The mac/msu/mul_post_sub patterns are inherently 3-register
            # operations:
            #   mac:          rd = rs1 * rs2 + rs3      (3 reads)
            #   msu:          rd = rs3 - (rs1 * rs2)    (3 reads)
            #   mul_post_sub: rd = (rs1 * rs2) - rs3    (3 reads)
            # We MUST emit regb_used / regc_used / regc_mux=REGC_S4 for
            # them regardless of what `op.n_inputs` says.  Earlier
            # codegen paths sometimes inherit a wrong n_inputs (e.g.
            # 1) from upstream liveness analysis, which would cause the
            # decoder to leave operand_b/c at zero -> the multiplier
            # silently computes a*0+0 = 0.
            mult_3reg_patterns = {"mac", "msu", "mul_post_sub"}
            mult_pat = getattr(op, "mult_pattern", "") or ""
            needs_3_reads = mult_pat in mult_3reg_patterns or op.n_inputs >= 3

            signals = [
                f"{indent}alu_en         = 1'b0;",
                f"{indent}mult_int_en    = 1'b1;",
                f"{indent}mult_operator_o = {op.mult_opcode_sv};",
                f"{indent}regfile_alu_we = 1'b1;",
                f"{indent}rega_used_o    = 1'b1;",
            ]
            if op.n_inputs >= 2 or needs_3_reads:
                signals.append(f"{indent}regb_used_o    = 1'b1;")
            imm_enc_mult = getattr(op, "imm_encoding", {})
            is_parametric_mult = bool(imm_enc_mult)
            if is_parametric_mult:
                signals.append(f"{indent}fused_imm_o    = {{instr_rdata_i[24:20], instr_rdata_i[31:27]}};")
            elif needs_3_reads:
                signals.append(f"{indent}regc_used_o    = 1'b1;")
                signals.append(f"{indent}regc_mux_o     = REGC_S4;")
            return signals

        else:
            # Standard ALU-only fusion
            signals = [
                f"{indent}alu_operator_o = ALU_{op.name};",
                f"{indent}regfile_alu_we = 1'b1;",
                f"{indent}rega_used_o    = 1'b1;",
            ]

            # For CUSTOM_2 (0x5B) 3-gram patterns with variable immediates
            # in the shift position: route instr[24:20] as a zero-extended
            # immediate to operand_b instead of reading the register file.
            # This eliminates the need for a separate `li` instruction.
            uses_imm_in_rs2 = getattr(op, "opcode", 0x0B) == 0x5B and op.n_inputs <= 1

            # Parametric immediate: pass raw instruction bits via fused_imm bus
            imm_enc = getattr(op, "imm_encoding", {})
            is_parametric = bool(imm_enc)

            if is_parametric:
                # Don't read register file for rs3/rs2 — route instruction
                # bits to fused_imm bus instead
                signals.append(f"{indent}fused_imm_o    = {{instr_rdata_i[24:20], instr_rdata_i[31:27]}};")
                # Only set regb if there's a real register input (not an immediate)
                n_imm_positions = len(imm_enc)
                if n_imm_positions < 2 and op.n_inputs >= 2:
                    signals.append(f"{indent}regb_used_o    = 1'b1;")
                # Don't set regc_used — rs3 carries immediate, not register
            elif uses_imm_in_rs2:
                # Don't read register file for rs2 — route instruction
                # bits to fused_imm bus instead
                signals.append(f"{indent}regb_used_o    = 1'b0;")
                # Set fused_imm_o from instruction encoding fields:
                # fused_imm[4:0] = instr[24:20] (rs2), fused_imm[9:5] = instr[31:27] (rs3)
                signals.append(f"{indent}fused_imm_o    = {{instr_rdata_i[31:27], instr_rdata_i[24:20]}};")
            elif op.n_inputs >= 2:
                signals.append(f"{indent}regb_used_o    = 1'b1;")

            if (op.n_inputs >= 3 or "operand_c_i" in op.sv_expression) and not is_parametric:
                signals.append(f"{indent}regc_used_o    = 1'b1;")
                signals.append(f"{indent}regc_mux_o     = REGC_S4;")
            return signals

    def _is_r4_type(self, op: FusedOperation) -> bool:
        """Check if this operation uses R4-type encoding.
        R4 if: 3+ register inputs, OR has immediate values encoded in rs3/rs2."""
        return op.n_inputs >= 3 or bool(op.imm_values)

    def generate_decoder_for_opcode(self, target_opcode: int) -> str:
        """Generate decoder case statements for a specific CUSTOM opcode space.

        R-type fusions (≤2 inputs) are decoded by matching the full
        ``instr[31:25]`` (funct7) field.

        R4-type fusions (3 inputs) use bits [31:27] for rs3, so the
        decoder matches only on ``instr[26:25]`` (funct2, 2 bits).
        This means R4-type ops within the same funct3 class are
        distinguished by their 2-bit funct2 value, allowing up to
        4 R4-type operations per funct3 class.
        """
        # Group operations by funct3, filtering to only those in target_opcode
        by_funct3: Dict[int, List[FusedOperation]] = {}
        for op in self.fused_ops:
            if op.opcode == target_opcode:
                by_funct3.setdefault(op.funct3, []).append(op)

        # Inject hwloop entries from the registry (if any share this opcode)
        hwloop_entries = getattr(self, "_hwloop_registry_entries", [])
        _hwloop_at_funct3: Dict[int, list] = {}
        for entry in hwloop_entries:
            if entry.opcode == target_opcode:
                _hwloop_at_funct3.setdefault(entry.funct3, []).append(entry)

        if not by_funct3 and not _hwloop_at_funct3:
            return ""

        lines = []
        lines.append("        unique case (instr_rdata_i[14:12])")

        for funct3 in sorted(by_funct3.keys()):
            ops = by_funct3[funct3]
            lines.append(f"          3'b{funct3:03b}: begin")

            # Separate R-type and R4-type ops within this funct3 class
            r_ops = [op for op in ops if not self._is_r4_type(op)]
            r4_ops = [op for op in ops if self._is_r4_type(op)]

            if r_ops and not r4_ops:
                # All R-type: match on full instr[31:25]
                self._emit_r_type_decoder(lines, r_ops)
            elif r4_ops and not r_ops:
                # All R4-type: match on instr[26:25] (funct2)
                self._emit_r4_type_decoder(lines, r4_ops)
            elif r_ops and r4_ops:
                # Mixed R-type and R4-type on same funct3.
                # R4-type uses instr[26:25] (funct2), R-type uses instr[31:25] (funct7).
                # R-type funct7 has bits[26:25] = funct2, so we can decode by funct2 first:
                # each R-type op's funct7[1:0] gives a unique funct2 value.
                # Emit all as funct2 cases — R-type ops use their funct7[1:0] as funct2.
                all_ops = r4_ops + r_ops
                self._emit_r4_type_decoder(lines, all_ops)

            lines.append("          end")

        # Add hwloop entries for funct3 values not covered by fused ops
        for f3 in sorted(_hwloop_at_funct3.keys()):
            if f3 in by_funct3:
                # Same funct3 as fused — need to merge into existing block
                # For now, hwloop entries at same funct3 as fused ops
                # are handled by adding them as additional funct2 cases
                # This requires reopening the funct3 block — skip for now
                # (the encoding allocator should avoid this)
                continue
            entries = _hwloop_at_funct3[f3]
            lines.append(f"          3'b{f3:03b}: begin")
            if len(entries) == 1:
                e = entries[0]
                for sig, val in sorted(e.signals.items()):
                    lines.append(f"            {sig:<30s} = {val};")
            else:
                lines.append("            unique case (instr_rdata_i[26:25])")
                for e in entries:
                    lines.append(f"              2'b{e.funct2:02b}: begin  // {e.name}")
                    for sig, val in sorted(e.signals.items()):
                        lines.append(f"                {sig:<30s} = {val};")
                    lines.append("              end")
                lines.append("              default: illegal_insn_o = 1'b1;")
                lines.append("            endcase")
            lines.append("          end")

        lines.append("          default: illegal_insn_o = 1'b1;")
        lines.append("        endcase")

        # If no fused ops but hwloop entries exist, we still need the case wrapper
        if not by_funct3 and _hwloop_at_funct3:
            pass  # lines already populated above

        return "\n".join(lines)

    def _emit_r_type_decoder(self, lines: List[str], ops: List[FusedOperation]) -> None:
        """Emit decoder logic for R-type operations (match on instr[31:25])."""
        if len(ops) == 1:
            op = ops[0]
            lines.append(f"            if (instr_rdata_i[31:25] == 7'b{op.funct7:07b}) begin // {op.description}")
            lines.extend(self._decoder_signals_for_op(op, "              "))
            lines.append("            end else begin")
            lines.append("              illegal_insn_o = 1'b1;")
            lines.append("            end")
        else:
            lines.append("            unique case (instr_rdata_i[31:25])")
            for op in ops:
                lines.append(f"              7'b{op.funct7:07b}: begin // {op.description}")
                lines.extend(self._decoder_signals_for_op(op, "                "))
                lines.append("              end")
            lines.append("              default: illegal_insn_o = 1'b1;")
            lines.append("            endcase")

    def _emit_r4_type_decoder(self, lines: List[str], ops: List[FusedOperation]) -> None:
        """Emit decoder logic for R4-type operations (match on instr[26:25]).

        R4-type layout: [rs3:31-27][funct2:26-25][rs2:24-20]
        [rs1:19-15][funct3:14-12][rd:11-7][opcode:6-0]
        The funct2 value comes from funct7[1:0] (the variant/low 2 bits).
        """
        if len(ops) == 1:
            op = ops[0]
            funct2 = op.funct7 & 0x03
            lines.append(f"            if (instr_rdata_i[26:25] == 2'b{funct2:02b}) begin // {op.description}")
            lines.extend(self._decoder_signals_for_op(op, "              "))
            lines.append("            end else begin")
            lines.append("              illegal_insn_o = 1'b1;")
            lines.append("            end")
        else:
            lines.append("            unique case (instr_rdata_i[26:25])")
            for op in ops:
                funct2 = op.funct7 & 0x03
                lines.append(f"              2'b{funct2:02b}: begin // {op.description}")
                lines.extend(self._decoder_signals_for_op(op, "                "))
                lines.append("              end")
            lines.append("              default: illegal_insn_o = 1'b1;")
            lines.append("            endcase")

    # ──────────────────────────────────────────────────────────────────
    #  ARVIS shared-imm-mux infrastructure (per-direction K-arm muxes)
    # ──────────────────────────────────────────────────────────────────

    def _arvis_first_param_step(self, op: FusedOperation) -> Optional[Tuple[str, int]]:
        """Identify the first chain step that uses the parametric immediate.

        Returns ``(direction, position)`` where:
          - ``direction`` ∈ {"shr", "shl", "shra", "andm"} indicates the
            kind of operation done on operand_a using ``fused_imm_i[4:0]``.
          - ``position`` is the index of that step in op.mnemonics.
        Returns None if the op has no parametric immediate or the
        parametric step isn't a value we want to share/hardcode.

        Only the FIRST parametric position (rs3 = fused_imm_i[4:0]) is
        considered.  Patterns with a second parametric immediate in
        rs2 (fused_imm_i[9:5]) keep that as inline AND-mask logic — it's
        already 1 LUT level and not worth muxing.
        """
        imm_enc = getattr(op, "imm_encoding", {})
        if not imm_enc:
            return None
        # Find the first mnemonic position that uses rs3_5bit (= fused_imm[4:0])
        param_positions = sorted(k for k, v in imm_enc.items() if v == "rs3_5bit")
        if not param_positions:
            return None
        first = param_positions[0]
        if first >= len(op.mnemonics):
            return None
        m = normalize_mnemonic(op.mnemonics[first])
        kind = {
            "srli": "shr",
            "srl": "shr",
            "slli": "shl",
            "sll": "shl",
            "srai": "shra",
            "sra": "shra",
            "andi": "andm",
        }.get(m)
        if kind is None:
            return None
        # Only handle the case where the parametric op is at position 0
        # (operates directly on operand_a_i). Position ≥ 1 means it's a
        # post-op which we leave inline (still cheap).
        if first != 0:
            return None
        return kind, first

    def _arvis_collect_shift_value_sets(self) -> Dict[str, Set[int]]:
        """Walk all fused ops and collect the per-direction value union.

        Returns a dict ``{"shr": set, "shl": set, "shra": set, "andm": set}``
        of the immediate values that appear at the parametric step of any
        op of that direction.  Empty sets are kept (no entries → no mux
        case for that direction).
        """
        sets: Dict[str, Set[int]] = {"shr": set(), "shl": set(), "shra": set(), "andm": set()}
        for op in self._alu_ops():
            info = self._arvis_first_param_step(op)
            if info is None:
                continue
            kind, _ = info
            # Skip K=1 ops: their value is already hardcoded inline by
            # _arvis_substitute_param_step, so they never read the shared
            # wire.  Including them here would emit a dead mux arm that
            # Vivado may or may not prune.  Drop them up front for a
            # smaller, cleaner shared mux.
            if len(op.used_imm_values) <= 1:
                continue
            sets[kind].update(op.used_imm_values)
        return sets

    def _arvis_substitute_param_step(self, op: FusedOperation) -> Optional[str]:
        """Return the SV expression that replaces ``operand_a_i <op> fused_imm_i[4:0]``.

        The result is one of:
          - ``"(operand_a_i >> 5'd<v>)"`` etc. when the op's used_imm_values has
            exactly one element (K=1 hardcoding).
          - ``"arvis_shr"`` / ``"arvis_shl"`` / ``"arvis_shra"`` /
            ``"arvis_andm"`` when K ≥ 2 (shared mux).
          - None when the op isn't a parametric-on-operand_a kind.

        Wraps the hardcoded form in parentheses so it composes with chained
        post-ops in the existing decomposition machinery.
        """
        info = self._arvis_first_param_step(op)
        if info is None:
            return None
        kind, _ = info
        values = sorted(op.used_imm_values)
        if len(values) == 1:
            v = values[0] & 0x1F
            if kind == "shr":
                return f"(operand_a_i >> 5'd{v})"
            if kind == "shl":
                return f"(operand_a_i << 5'd{v})"
            if kind == "shra":
                # Verilator quirk: $signed(unsigned_var) >>> N treats the
                # shift as logical, not arithmetic.  Use the explicit
                # sign-extend-then-unsigned-shift form which works
                # portably across simulators and FPGA synth tools.
                if v == 0:
                    return "operand_a_i"
                if v == 31:
                    return "{32{operand_a_i[31]}}"
                return f"{{{{{v}{{operand_a_i[31]}}}}, operand_a_i[31:{v}]}}"
            if kind == "andm":
                return f"(operand_a_i & 32'd{v})"
        # K ≥ 2 (or K == 0 — no value info, fall back to shared wire).
        return f"arvis_{kind}"

    def generate_alu_shared_imm_muxes(self) -> str:
        """Generate the ARVIS shared per-direction parametric-shift muxes.

        Emits a single ``always_comb`` block driving up to four output
        wires (``arvis_shr/shl/shra/andm``) keyed by ``fused_imm_i[4:0]``.
        Each output is the value of ``operand_a_i`` shifted (or masked)
        by a value drawn from the union of values used by any parametric
        ALU op of that direction.

        Vivado synthesises this as a 1-LUT-level mux per output bit
        (since each direction's case arm count fits in a 6-LUT).  All
        per-pattern result_mux/steering paths reference these wires
        instead of computing their own barrel shift, so the parametric
        path stays at 1 LUT level instead of the 5-level barrel shifter.

        Returns an empty string if no parametric ALU op is in the
        workload (so no shared wires are emitted).
        """
        sets = self._arvis_collect_shift_value_sets()
        if not any(sets.values()):
            return ""
        # Collect the union of values across all directions; each arm
        # of the case handles whichever directions contain that value.
        all_values = sorted(set().union(*sets.values()))

        lines: List[str] = []
        lines.append("  // ARVIS_FUSED_BEGIN: shared_imm_muxes")
        lines.append("  // ─ Shared per-direction parametric-shift / mask muxes ─")
        lines.append("  // For every value V in the union of immediate values used")
        lines.append("  // by parametric ALU fusions in this workload, expose")
        lines.append("  // operand_a_i shifted/masked by V on a wire shared by all")
        lines.append("  // patterns of that direction.  Vivado collapses each")
        lines.append("  // output to ~1 LUT level on fused_imm_i[4:0].")
        # Wire declarations
        if sets["shr"]:
            lines.append("  logic [31:0] arvis_shr;")
        if sets["shl"]:
            lines.append("  logic [31:0] arvis_shl;")
        if sets["shra"]:
            lines.append("  logic [31:0] arvis_shra;")
        if sets["andm"]:
            lines.append("  logic [31:0] arvis_andm;")
        lines.append("")
        lines.append("  always_comb begin")
        # Defaults
        if sets["shr"]:
            lines.append("    arvis_shr  = operand_a_i;")
        if sets["shl"]:
            lines.append("    arvis_shl  = operand_a_i;")
        if sets["shra"]:
            lines.append("    arvis_shra = operand_a_i;")
        if sets["andm"]:
            lines.append("    arvis_andm = '0;")
        lines.append("    unique case (fused_imm_i[4:0])")
        for v in all_values:
            v_lo = v & 0x1F
            arms: List[str] = []
            if v in sets["shr"]:
                arms.append(f"arvis_shr  = operand_a_i >> 5'd{v_lo};")
            if v in sets["shl"]:
                arms.append(f"arvis_shl  = operand_a_i << 5'd{v_lo};")
            if v in sets["shra"]:
                # Verilator quirk: $signed(unsigned_var) >>> N is computed
                # as a logical shift.  Use the explicit sign-extend form.
                if v_lo == 0:
                    arms.append("arvis_shra = operand_a_i;")
                elif v_lo == 31:
                    arms.append("arvis_shra = {32{operand_a_i[31]}};")
                else:
                    arms.append(f"arvis_shra = {{{{{v_lo}{{operand_a_i[31]}}}}, operand_a_i[31:{v_lo}]}};")
            if v in sets["andm"]:
                arms.append(f"arvis_andm = operand_a_i & 32'd{v_lo};")
            joined = " ".join(arms)
            lines.append(f"      5'd{v_lo}: begin {joined} end")
        lines.append("      default: ;")
        lines.append("    endcase")
        lines.append("  end")
        lines.append("// ARVIS_FUSED_END: shared_imm_muxes")
        return "\n".join(lines)

    def generate_alu_adder_steering(self) -> str:
        """Generate the always_comb block for the alu_adder_steering pragma.

        For each ALU op with hw_strategy="reuse_adder", emits case arms
        that steer fused_adder_op_a / fused_adder_op_b to the existing
        partitioned adder, and sets fused_adder_sel=1 so the baseline
        mux selects these values.

        The pre-adder computation (logic ops, constant shifts) is still
        expressed inline — these are cheap (2-3 LUTs / free wiring).
        Only the 32-bit addition itself reuses the existing HW.
        """
        reuse_ops = [op for op in self._alu_ops() if op.hw_strategy == "reuse_adder"]
        if not reuse_ops:
            # No reuse_adder ops → emit the default (no steering)
            return (
                "  // Default: no fused adder steering active\n"
                "  always_comb begin\n"
                "    fused_adder_sel      = 1'b0;\n"
                "    fused_adder_op_a     = '0;\n"
                "    fused_adder_op_b     = '0;\n"
                "    fused_adder_b_negate = 1'b0;\n"
                "  end"
            )

        lines = []
        lines.append("  // Fused adder steering: reuse existing partitioned adder")
        lines.append("  always_comb begin")
        lines.append("    // Defaults: no steering")
        lines.append("    fused_adder_sel      = 1'b0;")
        lines.append("    fused_adder_op_a     = '0;")
        lines.append("    fused_adder_op_b     = '0;")
        lines.append("    fused_adder_b_negate = 1'b0;")
        lines.append("")
        lines.append("    unique case (operator_i)")

        for op in reuse_ops:
            # Decompose the mnemonic chain to find the pre-adder expression
            # and the adder's two operand sources.
            adder_a_expr, adder_b_expr, is_sub = self._decompose_adder_steering(op)
            lines.append(f"      ALU_{op.name}: begin // {op.description} [reuse_adder]")
            lines.append("        fused_adder_sel      = 1'b1;")
            lines.append(f"        fused_adder_op_a     = {adder_a_expr};")
            lines.append(f"        fused_adder_op_b     = {adder_b_expr};")
            if is_sub:
                lines.append("        fused_adder_b_negate = 1'b1;")
            lines.append("      end")

        lines.append("      default: ;")
        lines.append("    endcase")
        lines.append("  end")
        return "\n".join(lines)

    def _decompose_adder_steering(self, op: FusedOperation) -> tuple:
        """Decompose a reuse_adder op into (adder_op_a_expr, adder_op_b_expr, is_sub).

        For a chain like [logic, add]:
          - adder_op_a = pre-adder inline expression (the logic part)
          - adder_op_b = the add's second operand
          - is_sub = False

        For a chain like [logic, sub]:
          - adder_op_a = pre-adder inline expression
          - adder_op_b = the sub's second operand (will be negated by HW)
          - is_sub = True

        For a chain like [slli(hc), add]:
          - adder_op_a = (operand_a_i << const)
          - adder_op_b = operand_b_i
          - is_sub = False
        """
        mnems = op.mnemonics
        imm_vals = op.imm_values

        # Build resolved list and b_sources (same logic as _build_sv_expression)
        _RUNTIME_B_PORTS = ["operand_b_i", "operand_c_i"]
        resolved = []
        b_sources = []
        runtime_b_idx = 0
        _param_imm_idx = 0
        imm_enc = getattr(op, "imm_encoding", {})
        for i, mnem in enumerate(mnems):
            base = normalize_mnemonic(mnem)
            imm_val = imm_vals.get(i)
            if imm_val is not None and (base, imm_val) in IMM_FOLDS:
                resolved.append(IMM_FOLDS[(base, imm_val)])
                b_sources.append("operand_b_i")
            else:
                resolved.append(base)
                sv_info = MNEMONIC_TO_SV.get(base, {})
                if imm_val is not None:
                    if imm_enc.get(i) == "rs3_5bit":
                        b_sources.append("fused_imm_i[4:0]" if _param_imm_idx == 0 else "fused_imm_i[9:5]")
                        _param_imm_idx += 1
                    elif imm_val < 0:
                        b_sources.append(f"32'h{imm_val & 0xFFFFFFFF:08X}")
                    else:
                        b_sources.append(f"32'd{imm_val}")
                elif sv_info.get("unary"):
                    b_sources.append("operand_b_i")
                else:
                    if runtime_b_idx < len(_RUNTIME_B_PORTS):
                        b_sources.append(_RUNTIME_B_PORTS[runtime_b_idx])
                        runtime_b_idx += 1
                    else:
                        b_sources.append("operand_c_i")

        # Find the adder step
        adder_idx = None
        for i, m in enumerate(resolved):
            if m in _ADDER_MNEMONICS:
                adder_idx = i
                break

        assert adder_idx is not None

        is_sub = resolved[adder_idx] == "sub"

        # Build the pre-adder expression (everything before the adder step)
        if adder_idx == 0:
            # Adder is the first op: adder_op_a = operand_a_i
            pre_expr = "operand_a_i"
        else:
            # Chain the pre-adder ops.  If step 0 is the parametric step
            # (e.g. ``srli + add`` where the shift amount comes from
            # fused_imm_i[4:0]), substitute the ARVIS shared-mux wire or
            # hardcoded constant instead of letting the chain emit
            # ``(operand_a_i >> fused_imm_i[4:0])`` — that would build a
            # full 32-bit barrel shifter on the parametric path.  See
            # _arvis_first_param_step / _arvis_substitute_param_step.
            arvis_sub = self._arvis_substitute_param_step(op) if adder_idx >= 1 else None
            if arvis_sub is not None:
                pre_expr = arvis_sub
                start_idx = 1
            else:
                info0 = MNEMONIC_TO_SV[resolved[0]]
                pre_expr = str(info0["expr"]).format(a="operand_a_i", b=b_sources[0])
                start_idx = 1
            for i in range(start_idx, adder_idx):
                info_i = MNEMONIC_TO_SV[resolved[i]]
                pre_expr = str(info_i["expr"]).format(a=pre_expr, b=b_sources[i])

            # Fix literal bit-selects
            import re as _re

            def _fix_lit(m: _re.Match) -> str:
                return f"5'd{int(m.group(1))}"

            pre_expr = _re.sub(r"32'd(\d+)\[4:0\]", _fix_lit, pre_expr)

            def _fix_hex(m: _re.Match) -> str:
                return f"5'd{int(m.group(1), 16) & 0x1F}"

            pre_expr = _re.sub(r"32'h([0-9A-Fa-f]+)\[4:0\]", _fix_hex, pre_expr)

        # Clean double bit-selects: fused_imm_i[4:0][4:0] → fused_imm_i[4:0]
        import re as _re2

        pre_expr = _re2.sub(r"(fused_imm_i\[\d+:\d+\])\[4:0\]", r"\1", pre_expr)

        # The adder's second operand (the {b} slot of the add/sub)
        adder_b_expr = b_sources[adder_idx]

        # Reversed variant: swap operands for non-commutative ops (sub)
        if is_sub and getattr(op, "variant", 0) != 0:
            return adder_b_expr, pre_expr, is_sub

        return pre_expr, adder_b_expr, is_sub

    def generate_alu_shifter_steering(self) -> str:
        """Generate the always_comb block for the alu_shifter_steering pragma.

        ARVIS Approach A: parametric fused ops no longer drive the shared
        barrel shifter — they read from the per-direction shared mux
        wires (``arvis_shr/shl/shra/andm``) generated by
        ``generate_alu_shared_imm_muxes``.  The shifter steering block
        therefore only emits the inert defaults so the existing
        ``shift_amt = fused_shift_sel ? ... : ...`` mux in the ALU
        always selects the baseline (non-fused) shifter inputs.

        Vivado constant-folds the inert default and removes the entire
        fused-shift override path during synthesis.  We keep the wire
        declarations and the mux structure so existing ALU SystemVerilog
        compiles without further surgery.
        """
        return (
            "  // ARVIS: fused shifter steering disabled — parametric ops use\n"
            "  // shared per-direction muxes (arvis_shr/shl/shra/andm) instead.\n"
            "  always_comb begin\n"
            "    fused_shift_sel        = 1'b0;\n"
            "    fused_shift_amt        = '0;\n"
            "    fused_shift_left       = 1'b0;\n"
            "    fused_shift_arithmetic = 1'b0;\n"
            "  end"
        )

    def generate_alu_cse_wires(self, text_context: str = "") -> str:
        """Generate explicit common sub-expression wires for fused ALU patterns.

        Vivado's CSE normally folds these automatically, but making them
        explicit:
          - guarantees single-evaluation
          - reduces fan-in of the final result mux by letting the synthesiser
            share the last mux stage across multiple case entries
          - improves linter readability

        Only emits wires whose underlying signals are present in the workload.
        ``operand_{a,b,c}_i`` are always present on the ALU. ``fused_imm_i`` is
        only added by the fused_imm_patcher when a fused op uses it, so the
        immediate-based wire is gated on its presence in ``text_context`` (the
        already-patched ALU text) or in the set of fused operations.

        Args:
            text_context: The current alu.sv text after result_mux patching.
                          Used to detect whether ``fused_imm_i`` is in scope.
        """
        lines = [
            "  // ARVIS FPGA: explicit common sub-expressions for fused ALU patterns.",
        ]

        # Check whether any fused op actually uses the sub-expressions we
        # would emit. Emitting unused wires is harmless (synth will trim),
        # but referencing fused_imm_i when it is not a port would be a
        # compile error, so gate that one explicitly.
        ab_and_used = any("operand_a_i & operand_b_i" in getattr(op, "sv_expression", "") for op in self._alu_ops())
        ab_xor_used = any("operand_a_i ^ operand_b_i" in getattr(op, "sv_expression", "") for op in self._alu_ops())
        ab_or_used = any("operand_a_i | operand_b_i" in getattr(op, "sv_expression", "") for op in self._alu_ops())
        imm_port_present = "fused_imm_i" in text_context or any(
            "fused_imm_i" in getattr(op, "sv_expression", "") for op in self._alu_ops()
        )
        imm_and_used = imm_port_present and any(
            "operand_a_i & fused_imm_i[4:0]" in getattr(op, "sv_expression", "") for op in self._alu_ops()
        )

        if ab_and_used:
            lines.append("  wire [31:0] fused_ab_and     = operand_a_i & operand_b_i;")
        if ab_xor_used:
            lines.append("  wire [31:0] fused_ab_xor     = operand_a_i ^ operand_b_i;")
        if ab_or_used:
            lines.append("  wire [31:0] fused_ab_or      = operand_a_i | operand_b_i;")
        if imm_and_used:
            lines.append("  wire [31:0] fused_a_and_imm5 = operand_a_i & {27'b0, fused_imm_i[4:0]};")

        # If no CSE wires apply for this workload, leave the pragma block
        # empty so nothing is injected.
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _apply_cse_rewrites(expr: str) -> str:
        """Rewrite common sub-expressions to their pre-computed wire names.

        ARVIS FPGA optimisation: replaces frequently reused patterns like
        ``(operand_a_i & operand_b_i)`` with the module-level CSE wire
        ``fused_ab_and`` declared by ``generate_alu_cse_wires()``.
        Functionally identical, but helps the synthesiser share the final
        mux stage across case entries.
        """
        import re as _re

        # Order matters: match widest patterns first
        rewrites = [
            (r"\(operand_a_i & operand_b_i\)", "fused_ab_and"),
            (r"\(operand_a_i \^ operand_b_i\)", "fused_ab_xor"),
            (r"\(operand_a_i \| operand_b_i\)", "fused_ab_or"),
            (r"\(operand_a_i & fused_imm_i\[4:0\]\)", "fused_a_and_imm5"),
        ]
        for pat, repl in rewrites:
            expr = _re.sub(pat, repl, expr)
        return expr

    def generate_alu_result_mux(self) -> str:
        """Generate ALU result_mux case entries for ALU-classified ops only.

        For ops with hw_strategy="reuse_adder", emits ``adder_result``
        (possibly wrapped in post-adder logic/shift ops) instead of the
        full inline expression — the adder inputs are steered by the
        alu_adder_steering pragma block.

        MULT-classified ops are handled by generate_mult_result_mux()
        and routed to cv32e40p_mult.sv instead.
        """
        lines = []
        for op in self._alu_ops():
            if op.hw_strategy == "reuse_adder":
                result_expr = self._build_reuse_adder_result_expr(op)
                lines.append(
                    f"      ALU_{op.name}: result_o = {self._apply_cse_rewrites(result_expr)};"
                    f" // {op.description} [reuse_adder]"
                )
            elif op.hw_strategy == "reuse_shifter":
                result_expr = self._build_reuse_shifter_result_expr(op)
                lines.append(
                    f"      ALU_{op.name}: result_o = {self._apply_cse_rewrites(result_expr)};"
                    f" // {op.description} [reuse_shifter]"
                )
            else:
                lines.append(
                    f"      ALU_{op.name}: result_o = {self._apply_cse_rewrites(op.sv_expression)}; // {op.description}"
                )
        return "\n".join(lines)

    def _build_reuse_shifter_result_expr(self, op: FusedOperation) -> str:
        """Build result expression for a reuse_shifter op.

        First op is a shift (reused via barrel shifter steering).
        Post-shift ops are applied to shift_result.
        """
        mnems = op.mnemonics
        imm_enc = getattr(op, "imm_encoding", {})
        _FIELDS = ["fused_imm_i[4:0]", "fused_imm_i[9:5]"]

        # ARVIS Approach A: replace the shared barrel-shifter result with
        # either a hardcoded constant shift (K=1) or a per-direction shared
        # mux output (K ≥ 2).  Falls back to ``shift_result`` only when
        # this op isn't a simple parametric shift on operand_a_i.
        arvis_sub = self._arvis_substitute_param_step(op)
        expr = arvis_sub if arvis_sub is not None else "shift_result"

        for i in range(1, len(mnems)):
            m = normalize_mnemonic(mnems[i])
            # Determine the "b" operand for this step
            if i in imm_enc:
                # Count how many imm fields came before this one
                field_idx = sum(1 for k in sorted(imm_enc) if k < i)
                b_src = _FIELDS[field_idx] if field_idx < len(_FIELDS) else "fused_imm_i[4:0]"
            elif i in op.imm_values:
                field_idx = sum(1 for k in sorted(imm_enc) if k < i)
                b_src = _FIELDS[field_idx] if field_idx < len(_FIELDS) else "fused_imm_i[4:0]"
            else:
                b_src = "operand_b_i"

            if m in ("and", "andi"):
                expr = f"({expr} & {b_src})"
            elif m in ("or", "ori"):
                expr = f"({expr} | {b_src})"
            elif m in ("xor", "xori"):
                expr = f"({expr} ^ {b_src})"
            elif m == "slli":
                expr = f"({expr} << {b_src})"
            elif m == "srli":
                expr = f"({expr} >> {b_src})"
            elif m == "srai":
                # Verilator quirk: $signed(...) >>> N is computed as
                # logical shift.  Use the explicit sign-extend form.
                expr = f"({{{{32{{({expr})[31]}}}}, ({expr})}} >> {b_src}[4:0])"
            else:
                return op.sv_expression  # fallback

        return expr

    def _build_reuse_adder_result_expr(self, op: FusedOperation) -> str:
        """Build the result expression for a reuse_adder op.

        If the adder is the last step, returns "adder_result".
        If there are post-adder ops (cheap logic/constant shifts),
        chains them on top of adder_result.

        Example: add+and → "(adder_result & operand_c_i)"
        Example: add+slli(4) → "(adder_result << 5'd4)"
        Example: and+add (no post-ops) → "adder_result"
        """
        mnems = op.mnemonics
        imm_vals = op.imm_values

        # Resolve mnemonics and find adder position
        _RUNTIME_B_PORTS = ["operand_b_i", "operand_c_i"]
        resolved = []
        b_sources = []
        runtime_b_idx = 0
        _param_imm_idx2 = 0
        imm_enc2 = getattr(op, "imm_encoding", {})
        for i, mnem in enumerate(mnems):
            base = normalize_mnemonic(mnem)
            imm_val = imm_vals.get(i)
            if imm_val is not None and (base, imm_val) in IMM_FOLDS:
                resolved.append(IMM_FOLDS[(base, imm_val)])
                b_sources.append("operand_b_i")
            else:
                resolved.append(base)
                sv_info = MNEMONIC_TO_SV.get(base, {})
                if imm_val is not None:
                    if imm_enc2.get(i) == "rs3_5bit":
                        b_sources.append("fused_imm_i[4:0]" if _param_imm_idx2 == 0 else "fused_imm_i[9:5]")
                        _param_imm_idx2 += 1
                    elif imm_val < 0:
                        b_sources.append(f"32'h{imm_val & 0xFFFFFFFF:08X}")
                    else:
                        b_sources.append(f"32'd{imm_val}")
                elif sv_info.get("unary"):
                    b_sources.append("operand_b_i")
                else:
                    if runtime_b_idx < len(_RUNTIME_B_PORTS):
                        b_sources.append(_RUNTIME_B_PORTS[runtime_b_idx])
                        runtime_b_idx += 1
                    else:
                        b_sources.append("operand_c_i")

        # Find adder step
        adder_idx = None
        for i, m in enumerate(resolved):
            if m in _ADDER_MNEMONICS:
                adder_idx = i
                break

        assert adder_idx is not None

        # If adder is the last step, just return adder_result
        if adder_idx == len(resolved) - 1:
            return "adder_result"

        # Chain post-adder ops on top of adder_result
        import re as _re

        expr = "adder_result"
        for i in range(adder_idx + 1, len(resolved)):
            info_i = MNEMONIC_TO_SV[resolved[i]]
            expr = str(info_i["expr"]).format(a=expr, b=b_sources[i])

        # Fix literal bit-selects
        def _fix_lit(m: _re.Match) -> str:
            return f"5'd{int(m.group(1))}"

        expr = _re.sub(r"32'd(\d+)\[4:0\]", _fix_lit, expr)

        def _fix_hex(m: _re.Match) -> str:
            return f"5'd{int(m.group(1), 16) & 0x1F}"

        expr = _re.sub(r"32'h([0-9A-Fa-f]+)\[4:0\]", _fix_hex, expr)

        # Clean double bit-selects: fused_imm_i[4:0][4:0] → fused_imm_i[4:0]
        expr = _re.sub(r"(fused_imm_i\[\d+:\d+\])\[4:0\]", r"\1", expr)

        return expr

    def generate_mult_result_mux(self) -> str:
        """Generate MULT result_mux case entries for MULT-classified ops.

        Operations that reuse existing opcodes (MUL_MAC32, MUL_MSU32)
        are already handled by the baseline int_result logic.  Only
        new MUL_FUSED_xxx opcodes need result mux entries here.

        For pre_compute patterns with mult input steering, the result
        is read from int_result[31:0] (the existing multiplier output)
        instead of an inline expression with *.
        """
        new_ops = self._mult_ops_needing_new_opcode()
        if not new_ops:
            return ""
        lines = []
        for op in new_ops:
            # ALL MULT patterns reuse the existing multiplier hardware.
            # The multiplier computes int_result = (pre_a * pre_b) + acc
            # We replace any multiply/MAC sub-expression with int_result.
            import re as _re

            mult_expr = op.sv_expression
            mult_expr = mult_expr.replace("operand_a_i", "op_a_i")
            mult_expr = mult_expr.replace("operand_b_i", "op_b_i")
            mult_expr = mult_expr.replace("operand_c_i", "op_c_i")

            # Replace MAC: ((a * b) + c) → int_result[31:0]
            mult_expr = _re.sub(
                r"\(\(op_[abc]_i\s*\*\s*op_[abc]_i\)\s*[+\-]\s*op_[abc]_i\)",
                "int_result[31:0]",
                mult_expr,
            )
            # Replace plain multiply: (a * b) → int_result[31:0]
            mult_expr = _re.sub(r"\(op_[abc]_i\s*\*\s*op_[abc]_i\)", "int_result[31:0]", mult_expr)
            # Replace pre_compute multiply: (EXPR * op_X) or (op_X * EXPR)
            # These are handled by pre_compute steering, result is int_result
            if "int_result" not in mult_expr and "*" in mult_expr:
                mult_expr = "int_result[31:0]"

            tag = op.mult_pattern or "fused"
            lines.append(
                f"      {op.mult_opcode_sv}: result_o = {mult_expr}; // {op.description} [{tag} → reuses multiplier]"
            )
        return "\n".join(lines)

    def _patch_between_pragmas(self, text: str, tag: str, new_content: str) -> str:
        """Replace content between ARVIS_FUSED_BEGIN/END pragma pairs."""
        begin_marker = f"// ARVIS_FUSED_BEGIN: {tag}"
        end_marker = f"// ARVIS_FUSED_END: {tag}"

        begin_idx = text.find(begin_marker)
        end_idx = text.find(end_marker)

        if begin_idx < 0 or end_idx < 0:
            raise ValueError(f"Pragma tag '{tag}' not found in text")

        # Find the end of the BEGIN line (position after its newline)
        begin_line_end = text.index("\n", begin_idx) + 1

        # Find the start of the END line (including its leading whitespace)
        end_line_start = text.rfind("\n", 0, end_idx)
        if end_line_start < 0:
            end_line_start = 0
        else:
            end_line_start += 1  # skip the newline itself

        # Replace everything between BEGIN line end and END line start
        if new_content:
            result = text[:begin_line_end] + new_content + "\n" + text[end_line_start:]
        else:
            result = text[:begin_line_end] + text[end_line_start:]

        return result

    def write(self, output_dir: Optional[str] = None):
        """
        Write patched RTL files.

        If a workspace is set, uses workspace.patch_between_pragmas().
        If output_dir is provided, writes to output_dir.
        Otherwise patches in-place on self.rtl_dir.
        """
        if self.workspace:
            # Use workspace pragma patching (workspace already has the copy)
            self.workspace.patch_between_pragmas("rtl/include/cv32e40p_pkg.sv", "alu_opcodes", self.generate_pkg())
            # Try decoder_custom0 first, fall back to decoder_cases
            dec_text = self.workspace.read_file("rtl/cv32e40p_decoder.sv")
            _OPCODE_NAMES_WS = {
                0x0B: "OPCODE_CUSTOM_0",
                0x2B: "OPCODE_CUSTOM_1",
                0x5B: "OPCODE_CUSTOM_2",
                0x7B: "OPCODE_CUSTOM_3",
            }
            hwloop_entries = getattr(self, "_hwloop_registry_entries", [])
            ws_opcodes = sorted(set([op.opcode for op in self.fused_ops] + [e.opcode for e in hwloop_entries]))
            if "ARVIS_FUSED_BEGIN: decoder_cases" in dec_text:
                blocks = []
                for opc in ws_opcodes:
                    opc_dec = self.generate_decoder_for_opcode(opc)
                    if opc_dec:
                        opc_name = _OPCODE_NAMES_WS.get(opc, f"7'h{opc:02x}")
                        blocks.append(f"      {opc_name}: begin\n{opc_dec}\n      end")
                self.workspace.patch_between_pragmas("rtl/cv32e40p_decoder.sv", "decoder_cases", "\n".join(blocks))
            elif "ARVIS_FUSED_BEGIN: decoder_custom0" in dec_text:
                self.workspace.patch_between_pragmas(
                    "rtl/cv32e40p_decoder.sv",
                    "decoder_custom0",
                    self.generate_decoder_for_opcode(OPCODE_CUSTOM_0),
                )
            self.workspace.patch_between_pragmas("rtl/cv32e40p_alu.sv", "result_mux", self.generate_alu_result_mux())
            # ARVIS FPGA: inject CSE wire declarations at module-level
            if "ARVIS_FUSED_BEGIN: alu_operations" in self.workspace.read_file("rtl/cv32e40p_alu.sv"):
                self.workspace.patch_between_pragmas(
                    "rtl/cv32e40p_alu.sv", "alu_operations", self.generate_alu_cse_wires()
                )
            print(f"  ✅ ISA Fusion: patched pkg + decoder + ALU ({len(self.fused_ops)} operations)")
            return

        if output_dir:
            out = Path(output_dir)
            pkg_out = out / "include" / "cv32e40p_pkg.sv"
            dec_out = out / "cv32e40p_decoder.sv"
            alu_out = out / "cv32e40p_alu.sv"
            # Copy original RTL tree first
            for src_file in self.rtl_dir.rglob("*.sv"):
                rel = src_file.relative_to(self.rtl_dir)
                dst = out / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)
        else:
            pkg_out = self.pkg_path
            dec_out = self.decoder_path
            alu_out = self.alu_path

        # Patch PKG
        pkg_text = pkg_out.read_text()
        pkg_new = self.generate_pkg()
        pkg_text = self._patch_between_pragmas(pkg_text, "alu_opcodes", pkg_new)
        pkg_out.write_text(pkg_text)
        print(f"  ✅ Patched {pkg_out}")

        # Patch Decoder — generate decoder blocks for ALL used CUSTOM opcode spaces
        dec_text = dec_out.read_text()

        # Collect all unique opcodes used by fused operations AND hwloop
        hwloop_entries = getattr(self, "_hwloop_registry_entries", [])
        used_opcodes = sorted(set([op.opcode for op in self.fused_ops] + [e.opcode for e in hwloop_entries]))

        # Opcode value → SV parameter name mapping
        _OPCODE_NAMES = {
            0x0B: "OPCODE_CUSTOM_0",
            0x2B: "OPCODE_CUSTOM_1",
            0x5B: "OPCODE_CUSTOM_2",
            0x7B: "OPCODE_CUSTOM_3",
        }

        if "ARVIS_FUSED_BEGIN: decoder_custom0" in dec_text:
            # Legacy: only CUSTOM_0 pragma exists
            c0_dec = self.generate_decoder_for_opcode(OPCODE_CUSTOM_0)
            dec_text = self._patch_between_pragmas(dec_text, "decoder_custom0", c0_dec)
        elif "ARVIS_FUSED_BEGIN: decoder_cases" in dec_text:
            # Specialized decoder: wrap each opcode in its own case
            blocks = []
            for opc in used_opcodes:
                opc_dec = self.generate_decoder_for_opcode(opc)
                if opc_dec:
                    opc_name = _OPCODE_NAMES.get(opc, f"7'h{opc:02x}")
                    blocks.append(f"      {opc_name}: begin\n{opc_dec}\n      end")
            dec_text = self._patch_between_pragmas(dec_text, "decoder_cases", "\n".join(blocks))
        else:
            raise ValueError("Neither 'decoder_custom0' nor 'decoder_cases' pragma found in decoder")
        dec_out.write_text(dec_text)
        print(f"  ✅ Patched {dec_out}")

        # Patch ALU result mux + adder steering (ALU-only ops)
        alu_text = alu_out.read_text()
        alu_new = self.generate_alu_result_mux()
        alu_text = self._patch_between_pragmas(alu_text, "result_mux", alu_new)

        # ARVIS FPGA: inject CSE wire declarations at module-level
        if "ARVIS_FUSED_BEGIN: alu_operations" in alu_text:
            alu_text = self._patch_between_pragmas(alu_text, "alu_operations", self.generate_alu_cse_wires())

        # Patch adder steering pragma (if it exists in the template)
        if "ARVIS_FUSED_BEGIN: alu_adder_steering" in alu_text:
            adder_steering = self.generate_alu_adder_steering()
            alu_text = self._patch_between_pragmas(alu_text, "alu_adder_steering", adder_steering)
            n_reuse = len([op for op in self._alu_ops() if op.hw_strategy == "reuse_adder"])
            if n_reuse:
                print(f"  ✅ Adder steering: {n_reuse} ops reuse existing partitioned adder")

        # Patch shifter steering (inject wires + mux if reuse_shifter ops exist)
        shifter_ops = [op for op in self._alu_ops() if op.hw_strategy == "reuse_shifter"]
        if shifter_ops and "fused_shift_sel" not in alu_text:
            import re as _re

            # Add steering wires after the adder reuse wires
            shift_wires = (
                "\n"
                "  // ── Fused-instruction shifter reuse wires (ARVIS auto-generated) ──\n"
                "  logic        fused_shift_sel;        // 1 = fused op steers the shifter\n"
                "  logic [31:0] fused_shift_amt;        // fused shift amount\n"
                "  logic        fused_shift_left;       // fused shift direction\n"
                "  logic        fused_shift_arithmetic; // fused arithmetic shift\n"
            )
            # Insert after fused_adder wires or after ARVIS_FUSED_END: alu_signals
            if "fused_adder_sel" in alu_text:
                alu_text = alu_text.replace(
                    "  logic [31:0] fused_adder_op_b;       // fused adder operand B",
                    "  logic [31:0] fused_adder_op_b;       // fused adder operand B" + shift_wires,
                    1,
                )
            else:
                alu_text = alu_text.replace(
                    "// ARVIS_FUSED_END: alu_signals",
                    shift_wires + "// ARVIS_FUSED_END: alu_signals",
                    1,
                )

            # Add steering always_comb block
            shifter_steering = self.generate_alu_shifter_steering()
            # Insert after adder steering block or after alu_signals pragma
            if "ARVIS_FUSED_BEGIN: alu_adder_steering" in alu_text:
                alu_text = alu_text.replace(
                    "// ARVIS_FUSED_END: alu_adder_steering",
                    "// ARVIS_FUSED_END: alu_adder_steering\n\n" + shifter_steering,
                    1,
                )
            else:
                alu_text = alu_text.replace(
                    "// ARVIS_FUSED_END: alu_signals",
                    "// ARVIS_FUSED_END: alu_signals\n\n" + shifter_steering,
                    1,
                )

            # Mux the shifter inputs: override shift_amt, shift_left, shift_arithmetic
            alu_text = alu_text.replace(
                "assign shift_amt = div_valid ? div_shift : operand_b_i;",
                "assign shift_amt = fused_shift_sel ? fused_shift_amt : (div_valid ? div_shift : operand_b_i);",
                1,
            )
            alu_text = _re.sub(
                r"assign shift_left = \(operator_i == ALU_SLL\)(.*?);",
                r"assign shift_left = fused_shift_sel ? fused_shift_left : ((operator_i == ALU_SLL)\1);",
                alu_text,
                count=1,
                flags=_re.DOTALL,
            )
            alu_text = _re.sub(
                r"assign shift_arithmetic = \(operator_i == ALU_SRA\)(.*?);",
                r"assign shift_arithmetic = fused_shift_sel ? fused_shift_arithmetic : ((operator_i == ALU_SRA)\1);",
                alu_text,
                count=1,
                flags=_re.DOTALL,
            )
            print(f"  ✅ Shifter steering: {len(shifter_ops)} ops reuse existing barrel shifter")

        # ── ARVIS shared per-direction parametric-shift muxes ──
        # Inject the always_comb block that drives arvis_shr/shl/shra/andm
        # immediately after the alu_signals pragma end marker so the wires
        # are in scope for the adder-steering and result-mux blocks below.
        # Vivado synthesises this as a single 1-LUT-level mux per output
        # bit; per-pattern logic just references the wires.
        shared_muxes = self.generate_alu_shared_imm_muxes()
        if shared_muxes and "ARVIS_FUSED_BEGIN: shared_imm_muxes" not in alu_text:
            marker = "// ARVIS_FUSED_END: alu_signals"
            if marker in alu_text:
                alu_text = alu_text.replace(
                    marker,
                    marker + "\n\n" + shared_muxes + "\n",
                    1,
                )
                print("  ✅ Shared imm muxes: injected (parametric ops use arvis_shr/shl/shra/andm)")

        alu_out.write_text(alu_text)
        n_alu = len(self._alu_ops())
        n_mult = len(self._mult_ops())
        print(f"  ✅ Patched {alu_out} ({n_alu} ALU ops)")

        # ── Patch MULT files if there are MULT-classified ops ──
        if self._mult_ops():
            mult_out = pkg_out.parent.parent / "cv32e40p_mult.sv"
            if not mult_out.exists():
                mult_out = self.rtl_dir / "cv32e40p_mult.sv"

            # Patch mul_opcodes in pkg (for new MUL_FUSED_xxx opcodes)
            mult_pkg_new = self.generate_mult_pkg()
            if mult_pkg_new:
                pkg_text = pkg_out.read_text()
                pkg_text = self._patch_between_pragmas(pkg_text, "mul_opcodes", mult_pkg_new)
                # Widen MUL_OP_WIDTH if new opcodes exceed 3-bit range
                new_ops = self._mult_ops_needing_new_opcode()
                if new_ops:
                    # Count: existing 7 (MUL_MAC32..MUL_H) + new ops
                    max_mul_val = 6 + len(new_ops)  # MUL_H=6, new start at 7
                    needed_width = max(3, max_mul_val.bit_length())
                    if needed_width > 3:
                        import re as _re

                        pkg_text = _re.sub(
                            r"parameter MUL_OP_WIDTH = \d+;",
                            f"parameter MUL_OP_WIDTH = {needed_width};",
                            pkg_text,
                        )

                        # Widen existing MUL_xxx = 3'bXXX to 4'b0XXX
                        def _widen_mul_literal(m: _re.Match) -> str:
                            name = m.group(1)
                            old_w = int(m.group(2))
                            bits = m.group(3)
                            if old_w < needed_width:
                                bits = bits.zfill(needed_width)
                            return f"MUL_{name} = {needed_width}'b{bits}"

                        pkg_text = _re.sub(
                            r"MUL_(\w+)\s*=\s*(\d+)'b([01]+)",
                            _widen_mul_literal,
                            pkg_text,
                        )
                        print(f"  ✅ Updated MUL_OP_WIDTH: 3 → {needed_width}")
                pkg_out.write_text(pkg_text)
                print(f"  ✅ Patched {pkg_out} (MUL opcodes: {len(self._mult_ops_needing_new_opcode())} new)")

            # Patch mult result mux (for new MUL_FUSED_xxx expressions)
            mult_mux_new = self.generate_mult_result_mux()
            if mult_mux_new and mult_out.exists():
                mult_text = mult_out.read_text()
                mult_text = self._patch_between_pragmas(mult_text, "mult_result_mux", mult_mux_new)
                mult_out.write_text(mult_text)
                print(f"  ✅ Patched {mult_out} ({len(self._mult_ops_needing_new_opcode())} new entries)")

            # Report reused MUL opcodes (zero new HW)
            reused = [op for op in self._mult_ops() if op.mult_opcode_sv in ("MUL_MAC32", "MUL_MSU32")]
            if reused:
                print(
                    f"  ✅ {len(reused)} MULT fusions reuse existing HW "
                    f"(MUL_MAC32/MUL_MSU32 — zero new multiplier logic)"
                )
            print(f"  Total: {n_alu} ALU + {n_mult} MULT = {n_alu + n_mult} fused operations")

    def summary(self) -> str:
        """Print summary of all generated operations."""
        alu_ops = self._alu_ops()
        mult_ops = self._mult_ops()
        reused_mult = [op for op in mult_ops if op.mult_opcode_sv in ("MUL_MAC32", "MUL_MSU32")]
        new_mult = self._mult_ops_needing_new_opcode()

        lines = [f"Generated {len(self.fused_ops)} fused operations ({len(alu_ops)} ALU, {len(mult_ops)} MULT):"]
        if reused_mult:
            lines.append(f"  MULT reusing existing HW: {len(reused_mult)} (zero new multiplier logic)")
        if new_mult:
            lines.append(f"  MULT needing new opcodes: {len(new_mult)}")

        for op in self.fused_ops:
            unit_tag = f"[{op.execution_unit.upper()}]"
            if op.execution_unit == "mult":
                unit_tag += f" → {op.mult_opcode_sv} ({op.mult_pattern})"
            lines.append(f"  {unit_tag} {op.name}: funct3={op.funct3:03b} funct7=0x{op.funct7:02x}")
            lines.append(f"    {op.description}")
            lines.append(f"    SV: {op.sv_expression}")
        return "\n".join(lines)


def generate_intrinsic_header(ops: List[FusedOperation], output_path: str) -> str:
    """Generate C header with inline asm intrinsics for all fused ops.

    Generates intrinsics for all port configurations:
    - 1-input/1-output: unary operation (only rs1 used)
    - 2-input/1-output: R-type (rs1, rs2)
    - 3-input/1-output: R4-type (rs1, rs2, rs3 via .insn r4)
    - 2-input/2-output: dual-write (result in rd, side-effect TBD)
    """
    lines = [
        "/* Auto-generated custom instruction intrinsics */",
        "#ifndef CUSTOM_FUSED_H",
        "#define CUSTOM_FUSED_H",
        "#include <stdint.h>",
        "",
    ]

    for op in ops:
        name_lower = op.name.lower()
        memory_type = getattr(op, "memory_type", "")
        has_memory = memory_type in ("load", "store", "load_compute")

        lines.append(f"/* {op.description} */")
        mem_note = f"  memory={memory_type}" if has_memory else ""
        opc = getattr(op, "opcode", OPCODE_CUSTOM_0)
        lines.append(
            f"/* opcode=0x{opc:02x} funct3={op.funct3} funct7=0x{op.funct7:02x}"
            f"  n_in={op.n_inputs} n_out={op.n_outputs}{mem_note} */"
        )

        if memory_type == "store" and op.n_outputs == 0:
            # Store-only operation: no output register
            if op.n_inputs >= 3:
                funct2 = op.funct7 & 0x03
                params = ", ".join(f"uint32_t {'abc'[i]}" for i in range(op.n_inputs))
                lines.append(f"static inline void custom_{name_lower}({params}) {{")
                lines.append("    __asm__ volatile (")
                insn_ops = ", ".join(f"%{i}" for i in range(op.n_inputs))
                lines.append(f'        ".insn r4 0x0b, {op.funct3}, {funct2}, x0, {insn_ops}"')
                lines.append("        :")
                input_list = ", ".join(f'"r"({"abc"[i]})' for i in range(op.n_inputs))
                lines.append(f"        : {input_list}")
                lines.append('        : "memory"')
                lines.append("    );")
                lines.append("}")
            else:
                lines.append(f"static inline void custom_{name_lower}(uint32_t a, uint32_t b) {{")
                lines.append("    __asm__ volatile (")
                lines.append(f'        ".insn r 0x0b, {op.funct3}, 0x{op.funct7:02x}, x0, %0, %1"')
                lines.append("        :")
                lines.append('        : "r"(a), "r"(b)')
                lines.append('        : "memory"')
                lines.append("    );")
                lines.append("}")

        elif op.n_inputs <= 1 and op.n_outputs <= 1:
            # 1-input/1-output: unary — rs2 is x0 (unused)
            lines.append(f"static inline uint32_t custom_{name_lower}(uint32_t a) {{")
            lines.append("    uint32_t result;")
            lines.append("    __asm__ volatile (")
            lines.append(f'        ".insn r 0x0b, {op.funct3}, 0x{op.funct7:02x}, %0, %1, x0"')
            lines.append('        : "=r"(result)')
            lines.append('        : "r"(a)')
            if has_memory:
                lines.append('        : "memory"')
            lines.append("    );")
            lines.append("    return result;")
            lines.append("}")

        elif op.n_inputs == 2 and op.n_outputs <= 1:
            # 2-input/1-output: standard R-type
            lines.append(f"static inline uint32_t custom_{name_lower}(uint32_t a, uint32_t b) {{")
            lines.append("    uint32_t result;")
            lines.append("    __asm__ volatile (")
            lines.append(f'        ".insn r 0x0b, {op.funct3}, 0x{op.funct7:02x}, %0, %1, %2"')
            lines.append('        : "=r"(result)')
            lines.append('        : "r"(a), "r"(b)')
            if has_memory:
                lines.append('        : "memory"')
            lines.append("    );")
            lines.append("    return result;")
            lines.append("}")

        elif op.n_inputs == 3 and op.n_outputs <= 1:
            # 3-input/1-output: R4-type encoding
            funct2 = op.funct7 & 0x03
            lines.append(f"static inline uint32_t custom_{name_lower}(uint32_t a, uint32_t b, uint32_t c) {{")
            lines.append("    uint32_t result;")
            lines.append("    __asm__ volatile (")
            lines.append(f'        ".insn r4 0x0b, {op.funct3}, {funct2}, %0, %1, %2, %3"')
            lines.append('        : "=r"(result)')
            lines.append('        : "r"(a), "r"(b), "r"(c)')
            if has_memory:
                lines.append('        : "memory"')
            lines.append("    );")
            lines.append("    return result;")
            lines.append("}")

        elif op.n_inputs == 2 and op.n_outputs == 2:
            # 2-input/2-output: dual-write
            lines.append(f"static inline uint32_t custom_{name_lower}(uint32_t a, uint32_t b) {{")
            lines.append("    uint32_t result;")
            lines.append("    __asm__ volatile (")
            lines.append(f'        ".insn r 0x0b, {op.funct3}, 0x{op.funct7:02x}, %0, %1, %2"')
            lines.append('        : "=r"(result)')
            lines.append('        : "r"(a), "r"(b)')
            if has_memory:
                lines.append('        : "memory"')
            lines.append("    );")
            lines.append("    return result;")
            lines.append("}")

        else:
            lines.append(f"/* UNSUPPORTED: {op.n_inputs}-input/{op.n_outputs}-output */")

        lines.append("")

    lines.append("#endif /* CUSTOM_FUSED_H */")
    lines.append("")

    text = "\n".join(lines)
    Path(output_path).write_text(text)
    return text


# ═══════════════════════════════════════════════════════════════════════════
# Convenience: generate from analysis results
# ═══════════════════════════════════════════════════════════════════════════
