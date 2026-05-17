"""
Phase 5: Fusion RTL Generation.

Takes the selected ALU-only single-cycle fusions from Phase 2 and
generates the RTL patches IN-PLACE on the pruned RTL (rtl_modified):
  1. ALU opcode enum entries in cv32e40p_pkg.sv
  2. CUSTOM-0 decoder cases in cv32e40p_decoder.sv
  3. ALU result_mux expressions in cv32e40p_alu.sv

Patches are inserted between ARVIS_FUSED_BEGIN/END pragma markers
that already exist in the RTL source files.

After this phase the RTL in ``output/<benchmark>_specialized/rtl_modified/``
contains both the pruning changes AND the fused instruction hardware.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext


from arvis.cli import print_error, print_info, print_success, print_warning


def run(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Execute Phase 5: Fusion RTL Generation."""
    from arvis.cli import print_section
    from arvis.codegen.rtl.isa_fusion.alu_single_cycle import (
        RTLGenerator,
        generate_intrinsic_header,
    )

    print_section("PHASE 5: FUSION RTL GENERATION")

    # ── Phase 5a: Generate GCC patterns from observed program n-grams ──
    # Take ALL n-gram fusion candidates found in the program, filter to
    # ALU-only compute-only, normalize immediate instructions (addi=add,
    # slli=sll, etc.), remove critical path conflicts (no consecutive
    # mul/div), and write as R4 GCC define_insn patterns.
    # No liveness filtering — GCC handles register allocation.
    # If too many for address space, select best subset by impact.
    from arvis.codegen.gcc.peephole_gen import generate_combined_md as _gen_combined

    md_path = str(Path(cfg.output_dir) / "custom-fused.md")
    print("\n[5a] Generating GCC patterns (2-gram exhaustive + 3-gram peephole2)...")
    if ctx.all_fusions:
        _, n_2g, n_3g = _gen_combined(ctx.all_fusions, md_path)
        n_patterns = n_2g + n_3g
        print_success(f"{n_patterns} ALU patterns ({n_2g} 2-gram + {n_3g} 3-gram) → {md_path}")

        # NOTE: GIMPLE plugin config (patterns.json) is disabled.
        # The .md file alone handles both 2-grams (define_insn/combine)
        # and 3-grams (define_peephole2/post-regalloc). The GIMPLE plugin
        # would conflict with peephole2 patterns for the same 3-grams.
    else:
        print_warning("No fusion candidates available — skipping GCC pattern generation")

    if not ctx.all_fusions:
        print("\n  No fusion candidates — skipping RTL generation")
        return

    # Filter to ALU-only compute candidates (no LSU fusions yet)
    from arvis.analysis.models import InstructionType

    alu_candidates = []
    lsu_skipped = 0
    for cand in ctx.all_fusions:
        inst_type = getattr(cand, "instruction_type", None)
        if inst_type in (InstructionType.LOAD_COMPUTE, InstructionType.COMPUTE_STORE):
            lsu_skipped += 1
        else:
            alu_candidates.append(cand)

    if not alu_candidates:
        print("\n  No ALU-only fusions — skipping RTL generation")
        return

    if lsu_skipped:
        print(f"\n  Skipped {lsu_skipped} LSU fusions (not yet supported)")
    print(f"\n  {len(alu_candidates)} ALU-only fusions to implement")

    # ── Determine RTL directory ──
    # Phase 3 (pruning) copies the entire cv32e40p tree to rtl_modified/
    # with structure: rtl_modified/rtl/include/cv32e40p_pkg.sv, etc.
    # We patch IN-PLACE so the final RTL has both pruning + fusions.
    rtl_dir: Path | None = None

    # Try pruned output first (rtl_modified/rtl/)
    if ctx.rtl_output_dir:
        candidate = Path(ctx.rtl_output_dir) / "rtl"
        if (candidate / "include" / "cv32e40p_pkg.sv").exists():
            rtl_dir = candidate
        elif (Path(ctx.rtl_output_dir) / "include" / "cv32e40p_pkg.sv").exists():
            rtl_dir = Path(ctx.rtl_output_dir)

    # Fallback to original RTL source
    if rtl_dir is None:
        candidate = Path(cfg.rtl_root) / "rtl"
        if (candidate / "include" / "cv32e40p_pkg.sv").exists():
            rtl_dir = candidate
        elif (Path(cfg.rtl_root) / "include" / "cv32e40p_pkg.sv").exists():
            rtl_dir = Path(cfg.rtl_root)
        else:
            print_error(f"Cannot find cv32e40p_pkg.sv in {cfg.rtl_root}")
            return

    # Verify pragma files exist
    pkg_path = rtl_dir / "include" / "cv32e40p_pkg.sv"
    dec_path = rtl_dir / "cv32e40p_decoder.sv"
    alu_path = rtl_dir / "cv32e40p_alu.sv"

    for path, name in [
        (pkg_path, "pkg"),
        (dec_path, "decoder"),
        (alu_path, "alu"),
    ]:
        if not path.exists():
            print_error(f"{name} file not found: {path}")
            return
        content = path.read_text()
        if "ARVIS_FUSED_BEGIN" not in content:
            print_warning(f"No ARVIS_FUSED pragmas in {path.name} — cannot patch")
            return

    print(f"  RTL directory: {rtl_dir}")

    # Create RTL generator pointing at the RTL directory
    gen = RTLGenerator(str(rtl_dir))
    gen.add_existing()  # Parse existing ARVIS entries to avoid conflicts

    # Add each selected fusion — track which candidates were actually added
    # so firmware patching can pair them correctly with fused_ops.
    added = 0
    added_candidates = []
    for cand in alu_candidates:
        pattern = cand.pattern
        lv = cand.liveness
        n_in = lv.min_read_ports
        n_out = lv.min_write_ports_eliminate

        # Determine immediate values to fold
        imm_vals = {}
        if hasattr(cand, "imm_should_hardcode") and cand.imm_should_hardcode:
            for i, (should_hc, hc_val) in enumerate(zip(cand.imm_should_hardcode, cand.hardcoded_imm_values)):
                if should_hc and hc_val is not None:
                    imm_vals[i] = hc_val

        # Only handle compute-only (ALU) candidates
        from arvis.analysis.models import InstructionType

        if hasattr(cand, "instruction_type") and cand.instruction_type:
            if cand.instruction_type != InstructionType.COMPUTE_ONLY:
                continue

        # Only allow single-write fusions (max 1 write port)
        if n_out > 1:
            continue

        # Handle immediate instructions that aren't unanimously
        # hardcodable.  When an immediate varies across instances
        # (different shift amounts, different masks, etc.), we treat
        # it as a runtime register input instead of a hardcoded
        # literal.  The R-type encoding delivers the value via rs2/rs3
        # register fields — the caller loads the immediate into a
        # register before invoking the fused instruction.
        #
        # We bump n_in for each non-hardcoded immediate position so
        # the encoding allocator picks the right funct3 class
        # (R-type vs R4-type) and the SV expression chains
        # operand_b_i / operand_c_i correctly.
        _IMM_MNEMONICS = {
            "slli",
            "srli",
            "srai",
            "addi",
            "andi",
            "ori",
            "xori",
        }
        extra_reg_inputs = 0
        imm_hc = getattr(cand, "imm_should_hardcode", None) or ()
        for idx_m, m in enumerate(pattern):
            if m in _IMM_MNEMONICS:
                should_hc = idx_m < len(imm_hc) and imm_hc[idx_m]
                if not should_hc:
                    # This immediate is variable — it becomes a
                    # runtime register input instead of a literal.
                    extra_reg_inputs += 1
        if extra_reg_inputs > 0:
            n_in = n_in + extra_reg_inputs

        try:
            gen.add_fusion(
                mnemonics=pattern,
                imm_values=imm_vals,
                n_inputs=n_in,
                n_outputs=n_out,
                description=(f"Fused {' → '.join(pattern)} (freq={cand.frequency:,})"),
            )
            added += 1
            added_candidates.append(cand)
        except (ValueError, KeyError):
            pass  # Encoding space full or unsupported — silently skip

    if added == 0:
        print("\n  No fusions could generate SV expressions")
        return

    # Generate C intrinsic header (for reference / manual use)
    header_path = Path(cfg.output_dir) / "custom_fused.h"
    if gen.fused_ops:
        generate_intrinsic_header(gen.fused_ops, str(header_path))

    print(f"  {added} SV expression templates prepared for Phase 5c")

    # Store the fused ops and candidates for Phase 5c filtering
    ctx._fusion_rtl_fused_ops = gen.fused_ops  # type: ignore[attr-defined]
    ctx._fusion_rtl_candidates = added_candidates  # type: ignore[attr-defined]
    ctx._fusion_rtl_dir = str(rtl_dir)  # type: ignore[attr-defined]


def _reclassify_mult_from_sv(mult_pat: str, sv_expr: str, unique_name: str) -> tuple:
    """Re-examine the final SV expression to detect MSU/MAC/pre_compute patterns.

    After operand-swap (reverse), the mnemonic-based classifier may have
    returned "mul_post_sub" for what is actually MSU (c - a*b).  This
    function inspects the actual SV expression (in MULT port names) to
    reclassify.

    The existing cv32e40p_mult.sv hardware computes:
      int_result = $signed(op_c_i) + $signed(int_op_b_msu) +
                   $signed(int_op_a_msu) * $signed(op_b_i)
    where:
      MUL_MAC32: int_op_a_msu = op_a_i,  int_op_b_msu = 0  → c + a*b
      MUL_MSU32: int_op_a_msu = ~op_a_i, int_op_b_msu = op_b_i → c - a*b

    For pre_compute patterns (ALU_OP + mul), we can steer the mult inputs:
      op_a_i = pre_computed_value, op_b_i = other_operand
    and use MUL_MAC32 with op_c_i = 0 (just multiply, no accumulate).

    Returns: (mult_pattern, mult_opcode_sv)
    """
    import re as _re

    # Translate ALU port names to MULT port names for matching
    mult_expr = sv_expr
    mult_expr = mult_expr.replace("operand_a_i", "op_a_i")
    mult_expr = mult_expr.replace("operand_b_i", "op_b_i")
    mult_expr = mult_expr.replace("operand_c_i", "op_c_i")

    # ── Check for MSU pattern: (op_c_i - (op_a_i * op_b_i)) ──
    # This is c - a*b, which is exactly what MUL_MSU32 computes.
    if _re.fullmatch(r"\(op_c_i\s*-\s*\(op_a_i\s*\*\s*op_b_i\)\)", mult_expr.strip()):
        print_info(f"MULT reclassify: {unique_name} → MUL_MSU32 (c - a*b, zero new HW)")
        return "msu", "MUL_MSU32"

    # ── Check for MAC pattern: ((op_a_i * op_b_i) + op_c_i) ──
    # Also matches MAC+shift patterns like $signed((a*b+c)) >>> N
    if _re.search(r"\(\(op_a_i\s*\*\s*op_b_i\)\s*\+\s*op_c_i\)", mult_expr):
        # Check if there's a post-MAC shift
        shift_match = _re.search(r">>>\s*(\d+'d)?(\d+)\)?$", mult_expr.strip())
        if shift_match:
            shift_match.group(2)
            print_info(f"MULT reclassify: {unique_name} → MAC+post_op (reuses MAC HW)")
            return "mac_shift", f"MUL_FUSED_{unique_name}"  # New opcode, reuses MAC HW + post-op
        print_info(f"MULT reclassify: {unique_name} → MUL_MAC32 (a*b + c, zero new HW)")
        return "mac", "MUL_MAC32"

    # ── pre_compute patterns (ALU_OP + mul): ((EXPR) * op_c_i) ──
    # These use mult input steering: the pre-computed value is fed as
    # the first multiplier operand, and the result reads int_result.
    # Still needs a MUL_FUSED_* opcode to trigger the steering case.
    if mult_pat == "pre_compute":
        print(f"  ℹ️  MULT pre_compute: {unique_name} → MUL_FUSED (reuses existing multiplier via input steering)")
        return mult_pat, f"MUL_FUSED_{unique_name}"

    # Fallback: keep the original classification
    if mult_pat in ("mac",):
        return mult_pat, "MUL_MAC32"
    elif mult_pat in ("msu",):
        return mult_pat, "MUL_MSU32"
    else:
        return mult_pat, f"MUL_FUSED_{unique_name}"


def _swap_outer_operands_sv(sv_expr: str, outer_op: str) -> str:
    """Swap the outer operation's operands for a reverse pattern.

    For a normal 2-gram: (inner_expr OP operand_c_i)
    For reverse:         (operand_c_i OP inner_expr)

    This handles non-commutative ops (sub, sll, srl, sra, slt, sltu)
    where operand order matters.
    """
    import re as _re

    # Map mnemonic to SV operator
    _SV_OPS = {
        "sub": "-",
        "add": "+",
        "and": "&",
        "or": "|",
        "xor": "^",
        "sll": "<<",
        "srl": ">>",
        "sra": ">>>",
        "mul": "*",
        "slt": "<",
        "sltu": "<",
    }
    op_char = _SV_OPS.get(outer_op, "")
    if not op_char:
        return sv_expr

    # Pattern: ((inner) OP operand_c_i)
    # We need to swap to: (operand_c_i OP (inner))
    # Find the outermost binary operation
    m = _re.match(
        r"\((.+?)\s*" + _re.escape(op_char) + r"\s*(operand_c_i)\)",
        sv_expr,
    )
    if m:
        inner = m.group(1).strip()
        return f"(operand_c_i {op_char} {inner})"

    return sv_expr


def _parse_chain_positions(comment: str) -> tuple:
    """Parse chain positions from md comment like 'sra:chain→rs2, mul:chain→rs1'.

    Returns tuple of (pos_op2, pos_op3) where 0=chain→rs1, 1=chain→rs2.
    Default is (0, 0) if not parseable.
    """
    import re as _re

    positions = []
    for m in _re.finditer(r"chain→rs(\d)", comment):
        pos = int(m.group(1)) - 1  # rs1→0, rs2→1
        positions.append(pos)
    if len(positions) >= 2:
        return (positions[0], positions[1])
    return (0, 0)  # default: both chain→rs1


# GIMPLE tree code → SV operator mapping
_GIMPLE_TO_SV = {
    "PLUS_EXPR": "+",
    "MINUS_EXPR": "-",
    "MULT_EXPR": "*",
    "BIT_AND_EXPR": "&",
    "BIT_IOR_EXPR": "|",
    "BIT_XOR_EXPR": "^",
    "LSHIFT_EXPR": "<<",
    "RSHIFT_EXPR": ">>",
}


def _build_3gram_sv_expression(pattern: dict) -> str:
    """Build SV expression from a 3-gram GIMPLE pattern.

    Chains 3 operations using hardcoded immediates where available,
    operand_a_i for the single register input.
    """
    gimple_codes = pattern["gimple_codes"]
    hc_imm = pattern.get("immediates", {}).get("hardcoded", {})

    # Map GIMPLE codes to SV operators
    ops = [_GIMPLE_TO_SV.get(gc, "+") for gc in gimple_codes]

    # For variable immediates, use raw instruction bits instead of register values.
    # rs2 field = instr_rdata_i[24:20] (5 bits, values 0-31)
    # rs3 field = instr_rdata_i[31:27] (5 bits, values 0-31) — R4-type only
    # Combined: {rs3, rs2} = 10 bits (values 0-1023)
    _IMM_FIELD_RS2 = "instr_rdata_i[24:20]"  # 5-bit immediate from rs2 field
    _IMM_FIELD_RS3 = "instr_rdata_i[31:27]"  # 5-bit immediate from rs3 field (R4)
    # For wider immediates: concatenate {rs3[4:0], rs2[4:0]} = 10 bits
    _IMM_FIELD_WIDE = "{instr_rdata_i[31:27], instr_rdata_i[24:20]}"

    # Build the expression chain: inner → middle → outer
    # Inner op (index 0): operand_a_i OP hardcoded_imm[0] or raw_immediate
    if "0" in hc_imm:
        imm_val = hc_imm["0"]
        if ops[0] in ("<<", ">>"):
            inner = f"(operand_a_i {ops[0]} 5'd{imm_val})"
        else:
            inner = f"(operand_a_i {ops[0]} 32'd{imm_val})"
    else:
        # Variable immediate: plugin encodes value as x<N> in rs2 field.
        # Decoder routes instr[24:20] to fused_imm_i[4:0].
        # No register file read — the bits ARE the value.
        if ops[0] in ("<<", ">>"):
            inner = f"(operand_a_i {ops[0]} fused_imm_i[4:0])"
        else:
            inner = f"(operand_a_i {ops[0]} {{27'b0, fused_imm_i[4:0]}})"

    # Middle op (index 1): chain OP hardcoded_imm[1] or operand
    if "1" in hc_imm:
        imm_val = hc_imm["1"]
        middle = f"({inner} {ops[1]} 32'd{imm_val})"
    else:
        middle = f"({inner} {ops[1]} operand_b_i)"

    # Chain remaining ops (index 2, 3, ...)
    _RUNTIME_PORTS = ["operand_c_i", "operand_d_i"]  # For 4-gram, 4th port is theoretical
    runtime_idx = 0
    expr = middle
    for i in range(2, len(gimple_codes)):
        if str(i) in hc_imm:
            imm_val = hc_imm[str(i)]
            if ops[i] in ("<<", ">>"):
                expr = f"({expr} {ops[i]} 5'd{imm_val})"
            else:
                expr = f"({expr} {ops[i]} 32'd{imm_val})"
        else:
            if runtime_idx < len(_RUNTIME_PORTS):
                port = _RUNTIME_PORTS[runtime_idx]
                runtime_idx += 1
            else:
                port = "operand_c_i"  # fallback
            expr = f"({expr} {ops[i]} {port})"

    return expr


def _load_trigram_ops(cfg: "ToolConfig") -> list:
    """Load 3-gram patterns from patterns.json and create FusedOperation objects.

    Returns list of FusedOperation for patterns that were actually used
    in the compiled binary (opcode 0x5B).
    """

    patterns_path = Path(cfg.output_dir) / "patterns.json"
    if not patterns_path.exists():
        return []

    # DISABLED: 3-gram RTL generation has bugs (wrong shift amounts, hardcoded immediates)
    # TODO: fix _build_3gram_sv_expression before re-enabling
    return []


def compute_filtered_ops(cfg: "ToolConfig", ctx: "PipelineContext") -> list:
    """Compute the filtered list of FusedOperations GCC actually used.

    Pure computation — no file I/O.  Matches the binary's used encodings
    against the Phase 5a candidates and returns a list of FusedOperation
    objects with correct encoding fields and SV expressions.

    Returns:
        List of FusedOperation objects (may be empty).
    """
    from arvis.cli import print_section
    from arvis.codegen.rtl.isa_fusion.alu_single_cycle import (
        FusedOperation,
        RTLGenerator,
        classify_execution_unit,
        classify_hw_strategy,
        classify_mult_pattern,
    )

    print_section("PHASE 5c: COMPUTE FILTERED FUSED OPS (no file I/O)")

    gcc_result = ctx.gcc_compile_result
    if not gcc_result or not gcc_result.used_instructions:
        print("  No used instructions from GCC compile — nothing to filter")
        return []

    # ── Also load 3-gram patterns from patterns.json ──
    trigram_ops = _load_trigram_ops(cfg)
    if trigram_ops:
        print(f"  Loaded {len(trigram_ops)} 3-gram patterns from patterns.json")

    # Get the full set of fused ops that Phase 5a generated
    all_fused_ops = getattr(ctx, "_fusion_rtl_fused_ops", [])
    rtl_dir_str = getattr(ctx, "_fusion_rtl_dir", "")

    if not all_fused_ops:
        print("  No fused ops from Phase 5a — creating from binary inspection + .md")

    if not rtl_dir_str:
        # Use original RTL dir as reference for SV expression building
        rtl_dir_str = str(Path(cfg.rtl_root) / "rtl")
        if not (Path(rtl_dir_str) / "include" / "cv32e40p_pkg.sv").exists():
            rtl_dir_str = cfg.rtl_root

    rtl_dir = Path(rtl_dir_str)

    # Build a set of used encodings from the hex inspection
    used_encodings: set = set()
    for ui in gcc_result.used_instructions:
        used_encodings.add((ui.opcode, ui.funct3, ui.funct7))

    print(f"  Used encodings from binary: {len(used_encodings)}")
    print(f"  Total fused ops from Phase 5a: {len(all_fused_ops)}")

    # Parse the md file to get the SV semantics for each used encoding
    from arvis.pipeline.gcc_compile import _build_encoding_map

    encoding_map = _build_encoding_map(cfg)

    used_ops: list = []
    seen_enc: set = set()
    for ui in gcc_result.used_instructions:
        # Deduplicate: R4 parametric patterns have varying rs3 (immediate value)
        # so different funct7 values map to the same funct2. For RR+RR patterns,
        # each unique funct7 is a different pattern — don't deduplicate those.
        is_parametric = "param" in ui.pattern_name.lower() if hasattr(ui, "pattern_name") else False
        if is_parametric and ui.is_r4:
            dedup_key = (ui.opcode, ui.funct3, ui.funct7 & 0x3)
        else:
            dedup_key = (ui.opcode, ui.funct3, ui.funct7)
        if dedup_key in seen_enc:
            continue
        seen_enc.add(dedup_key)

        fop = None
        enc_key = (ui.opcode, ui.funct3, ui.funct7)
        md_info = encoding_map.get(enc_key, {})
        if not md_info:
            r4_key = (ui.opcode, ui.funct3, ui.funct7 & 0x3)
            md_info = encoding_map.get(r4_key, {})
        mnems = tuple(md_info.get("mnemonics", ()))

        # If not in .md encoding map, check patterns.json (3-gram GIMPLE patterns)
        if not mnems or len(mnems) < 2:
            import json as _json
            from pathlib import Path as _Path

            pj = _Path(cfg.output_dir) / "patterns.json"
            if pj.exists():
                pj_data = _json.loads(pj.read_text())
                for p in pj_data.get("patterns", []):
                    p_enc = p.get("encoding", {})
                    if (
                        p_enc.get("opcode") == ui.opcode
                        and p_enc.get("funct3") == ui.funct3
                        and p_enc.get("funct7")
                        == (ui.funct7 & 0x3 if p_enc.get("encoding_type") == "R4-type" else ui.funct7)
                    ):
                        mnems = tuple(p.get("ops", []))
                        md_info = {
                            "mnemonics": mnems,
                            "name": p.get("name", ""),
                            "is_r4": p_enc.get("encoding_type") == "R4-type",
                        }
                        break

        if not mnems or len(mnems) < 2:
            if ui.opcode in (0x5B, 0x2B, 0x7B):
                print(
                    f"  ℹ️  GIMPLE plugin encoding 0x{ui.opcode:02x}/f3={ui.funct3}/f7=0x{ui.funct7:02x} "
                    f"({ui.count}x) — handled via patterns.json"
                )
            else:
                print(
                    f"  ⚠️  Skipped unknown encoding 0x{ui.opcode:02x}/f3={ui.funct3}/f7=0x{ui.funct7:02x} ({ui.count}x)"
                )
            continue

        # Try to find a matching Phase 5a op by mnemonic pattern
        # Normalize I-type mnemonics to base form for matching (slli→sll, addi→add, etc.)
        _I_TO_BASE = {
            "slli": "sll",
            "srli": "srl",
            "srai": "sra",
            "addi": "add",
            "andi": "and",
            "ori": "or",
            "xori": "xor",
            "slti": "slt",
            "sltiu": "sltu",
        }
        mnems_base = tuple(_I_TO_BASE.get(m, m) for m in mnems)
        matching_op = None
        for op in all_fused_ops:
            if op.mnemonics == mnems or op.mnemonics == mnems_base:
                matching_op = op
                break

        enc_suffix = f"_{ui.opcode:02x}_{ui.funct3}_{ui.funct7:02x}"
        unique_name = "_".join(m.upper() for m in mnems) + enc_suffix

        hc_imm_from_md: dict = md_info.get("hardcoded_imm", {})
        is_reverse = md_info.get("is_reverse", False)

        if matching_op:
            merged_imm = dict(matching_op.imm_values)
            merged_imm.update(hc_imm_from_md)
            is_param_match = "param" in md_info.get("name", "")
            if is_param_match:
                # Parametric: rebuild SV with fused_imm_i
                _I_MNEMS_P = {
                    "slli",
                    "srli",
                    "srai",
                    "addi",
                    "andi",
                    "ori",
                    "xori",
                    "slti",
                    "sltiu",
                }
                for idx_m, m in enumerate(mnems):
                    if m in _I_MNEMS_P and idx_m not in merged_imm:
                        merged_imm[idx_m] = 0
                try:
                    temp_gen = RTLGenerator(str(rtl_dir))
                    temp_op = temp_gen.add_fusion(
                        mnems, imm_values=merged_imm, n_inputs=3, n_outputs=1, parametric_imm=True
                    )
                    sv_expr = temp_op.sv_expression
                    n_inputs = 3
                except Exception:
                    sv_expr = matching_op.sv_expression
                    n_inputs = matching_op.n_inputs
            elif hc_imm_from_md and hc_imm_from_md != matching_op.imm_values:
                try:
                    temp_gen = RTLGenerator(str(rtl_dir))
                    sv_expr = temp_gen._build_sv_expression(mnems, merged_imm)
                    n_inputs = matching_op.n_inputs
                except (ValueError, KeyError):
                    sv_expr = matching_op.sv_expression
                    n_inputs = matching_op.n_inputs
            else:
                sv_expr = matching_op.sv_expression
                n_inputs = matching_op.n_inputs

            # DEBUG: Always print reverse status for mul+sub
            if "sub" in mnems:
                print(f"  DEBUG: {mnems} is_reverse={is_reverse} len={len(mnems)}")
            if is_reverse and len(mnems) == 2:
                old_sv = sv_expr
                sv_expr = _swap_outer_operands_sv(sv_expr, mnems[1])
                print_info(f"{' + '.join(mnems)}: reverse {old_sv} → {sv_expr}")

            if merged_imm:
                _IMM_MNEMONICS = {
                    "slli",
                    "srli",
                    "srai",
                    "addi",
                    "andi",
                    "ori",
                    "xori",
                }
                recomputed = 1
                for idx_m, m in enumerate(mnems):
                    if m in _IMM_MNEMONICS or (m.rstrip("i") + "i") in _IMM_MNEMONICS:
                        if idx_m not in merged_imm:
                            recomputed += 1
                    elif idx_m == 0:
                        recomputed += 1
                    else:
                        recomputed += 1
                if recomputed < n_inputs:
                    # Don't reduce below 3 for R4-encoded patterns
                    # (parametric imms use rs3 field, need n_inputs=3)
                    if not (is_param_match and recomputed < 3):
                        print(f"  ℹ️  {' + '.join(mnems)}: n_inputs {n_inputs}→{recomputed} (HC imms reduce ports)")
                        n_inputs = recomputed
            # ── Classify execution unit ──
            exec_unit = classify_execution_unit(mnems)
            mult_pat = ""
            mult_opc_sv = ""
            if exec_unit == "mult":
                mult_pat = classify_mult_pattern(mnems)
                # After reverse swap, check if SV expression matches MSU/MAC
                # pattern even if classify_mult_pattern says otherwise.
                # "c - a*b" = MSU, "(a*b) + c" = MAC
                mult_pat, mult_opc_sv = _reclassify_mult_from_sv(mult_pat, sv_expr, unique_name)

            # ── Classify hardware reuse strategy ──
            hw_strat = classify_hw_strategy(mnems, merged_imm) if exec_unit == "alu" else "inline"

            # Detect parametric from md name
            is_param_match = "param" in md_info.get("name", "")
            if is_param_match and not getattr(matching_op, "imm_encoding", {}):
                _I_MNEMS = {"slli", "srli", "srai", "addi", "andi", "ori", "xori", "slti", "sltiu"}
                param_enc = {}
                for idx_m, m in enumerate(mnems):
                    if m in _I_MNEMS:
                        param_enc[idx_m] = "rs3_5bit"
                        if idx_m not in merged_imm:
                            merged_imm[idx_m] = 0
                n_inputs = 3  # R4 encoding for parametric
            else:
                param_enc = getattr(matching_op, "imm_encoding", {})

            fop = FusedOperation(
                name=unique_name,
                mnemonics=mnems,
                imm_values=merged_imm,
                n_inputs=n_inputs,
                n_outputs=matching_op.n_outputs,
                sv_expression=sv_expr,
                funct7=ui.funct7,
                funct3=ui.funct3,
                opcode=ui.opcode,
                description=f"GCC-used: {' + '.join(mnems)} ({ui.count}x)",
                execution_unit=exec_unit,
                mult_pattern=mult_pat,
                mult_opcode_sv=mult_opc_sv,
                hw_strategy=hw_strat,
                imm_encoding=param_enc,
                variant=1 if is_reverse else 0,
                used_imm_values=set(getattr(ui, "used_imm_values", set())),
            )
        else:
            imm_vals = dict(hc_imm_from_md)
            is_parametric = "param" in md_info.get("name", "")
            print(f"    DEBUG else: {mnems} param={is_parametric} imm_vals={imm_vals}")
            # For parametric patterns, determine which positions have immediates
            if is_parametric and not imm_vals:
                _I_MNEMS = {"slli", "srli", "srai", "addi", "andi", "ori", "xori", "slti", "sltiu"}
                for idx_m, m in enumerate(mnems):
                    if m in _I_MNEMS:
                        imm_vals[idx_m] = 0  # placeholder for parametric
            try:
                temp_gen = RTLGenerator(str(rtl_dir))
                n_inputs = 3 if ui.is_r4 else 2
                if is_parametric:
                    # Use add_fusion which handles parametric imm encoding
                    try:
                        temp_op = temp_gen.add_fusion(
                            mnems,
                            imm_values=imm_vals,
                            n_inputs=n_inputs,
                            n_outputs=1,
                            parametric_imm=True,
                        )
                        sv_expr = temp_op.sv_expression
                        imm_enc = temp_op.imm_encoding
                    except Exception as _e2:
                        print(f"    DEBUG add_fusion failed for {mnems}: {_e2}")
                        sv_expr = temp_gen._build_sv_expression(mnems, imm_vals)
                        imm_enc = {}
                else:
                    sv_expr = temp_gen._build_sv_expression(mnems, imm_vals)
                    imm_enc = {}
            except (ValueError, KeyError):
                try:
                    chain_pos = _parse_chain_positions(md_info.get("name", ""))
                    sv_expr = temp_gen._build_sv_expression_3gram_reuse(mnems, chain_pos)
                    imm_enc = {}
                except Exception:
                    print(f"  ⚠️  Could not build SV for {mnems}, skipping")
                    continue

            # Apply reverse swap if needed (else branch has no Phase 5a match)
            if is_reverse and len(mnems) == 2:
                old_sv = sv_expr
                sv_expr = _swap_outer_operands_sv(sv_expr, mnems[1])
                print_info(f"{' + '.join(mnems)}: reverse {old_sv} → {sv_expr}")

            # ── Classify execution unit ──
            exec_unit = classify_execution_unit(mnems)
            mult_pat = ""
            mult_opc_sv = ""
            if exec_unit == "mult":
                mult_pat = classify_mult_pattern(mnems)
                mult_pat, mult_opc_sv = _reclassify_mult_from_sv(mult_pat, sv_expr, unique_name)

            # ── Classify hardware reuse strategy ──
            hw_strat = classify_hw_strategy(mnems, imm_vals) if exec_unit == "alu" else "inline"

            try:
                fop = FusedOperation(
                    name=unique_name,
                    mnemonics=mnems,
                    imm_values=imm_vals,
                    n_inputs=n_inputs,
                    n_outputs=1,
                    sv_expression=sv_expr,
                    funct7=ui.funct7,
                    funct3=ui.funct3,
                    opcode=ui.opcode,
                    description=f"GCC-used: {' + '.join(mnems)} ({ui.count}x)",
                    execution_unit=exec_unit,
                    mult_pattern=mult_pat,
                    mult_opcode_sv=mult_opc_sv,
                    hw_strategy=hw_strat,
                    imm_encoding=imm_enc if is_parametric else {},
                    variant=1 if is_reverse else 0,
                    used_imm_values=set(getattr(ui, "used_imm_values", set())),
                )
            except (ValueError, KeyError) as e:
                print_warning(f"Cannot build SV for {' + '.join(mnems)}: {e}")
                continue

        # ── CRITICAL VALIDATION ──
        if not fop:
            print(f"    DEBUG: fop is None for {mnems} matching_op={matching_op is not None}")
            continue
        is_r_type = not ui.is_r4
        if is_r_type and "operand_c_i" in fop.sv_expression:
            print(
                f"  ❌ REJECTED: ALU_{fop.name} — R-type but SV uses "
                f"operand_c_i (not connected). {' + '.join(mnems)} ({ui.count}x)"
            )
            print(f"     Expression: {fop.sv_expression}")
            continue

        if is_r_type and fop.n_inputs > 2:
            print(
                f"  ❌ REJECTED: ALU_{fop.name} — R-type with n_inputs="
                f"{fop.n_inputs} > 2 (only 2 HW ports). "
                f"{' + '.join(mnems)} ({ui.count}x)"
            )
            continue

        # Belt-and-suspenders: reject any mul fusion that did NOT cleanly
        # reduce to MAC32 or MSU32 after operand-swap reclassification.
        # This catches anything that slipped past the .md-level filter
        # (e.g. mul_post_sub variants that didn't reverse-swap to MSU).
        if fop.execution_unit == "mult" and fop.mult_pattern not in ("mac", "msu"):
            print(
                f"  ❌ REJECTED: {fop.name} — DSP-unfriendly mul "
                f"(mult_pattern={fop.mult_pattern!r}, "
                f"would need extra logic on the multiplier critical path). "
                f"{' + '.join(mnems)} ({ui.count}x)"
            )
            continue

        used_ops.append(fop)
        unit_tag = f"[{fop.execution_unit.upper()}]" if fop.execution_unit else "[ALU]"
        mult_info = ""
        if fop.execution_unit == "mult":
            mult_info = f" → {fop.mult_opcode_sv} ({fop.mult_pattern})"
        print(
            f"  ✅ USED: {unit_tag}{mult_info} {fop.name} opc=0x{ui.opcode:02x} "
            f"f3={ui.funct3} f7=0x{ui.funct7:02x} ({ui.count}x)"
        )

    # ── Add GIMPLE plugin ops (3-gram on 0x5B + Cat2 on 0x2B etc.) ──
    if trigram_ops:
        used_plugin_encodings = {
            (ui.opcode, ui.funct3, ui.funct7) for ui in gcc_result.used_instructions if ui.opcode in (0x5B, 0x2B, 0x7B)
        }
        for trop in trigram_ops:
            if (trop.opcode, trop.funct3, trop.funct7) in used_plugin_encodings:
                used_ops.append(trop)
                print(f"  ✅ GIMPLE: ALU_{trop.name} opc=0x{trop.opcode:02x} f3={trop.funct3} f7=0x{trop.funct7:02x}")

    if not used_ops:
        print_warning("\n  ⚠️  No usable fused ops from binary")
        return []

    print(f"\n  Filtering: {len(used_ops)} ops retained (2-gram + 3-gram)")

    # Generate artifacts (C header + selected .md) — these are data files, not RTL
    if used_ops:
        from arvis.codegen.rtl.isa_fusion.alu_single_cycle import generate_intrinsic_header

        header_path = Path(cfg.output_dir) / "custom_fused.h"
        generate_intrinsic_header(used_ops, str(header_path))
        print_success(f"Updated C header: {header_path}")

    if used_ops:
        from arvis.codegen.gcc.md_pattern_gen import generate_md_file

        selected_md_path = Path(cfg.output_dir) / "custom-fused-selected.md"
        generate_md_file(used_ops, str(selected_md_path))
        print_success(f"Updated GCC patterns (used only): {selected_md_path}")

    return used_ops


def _clear_fused_pragmas(rtl_dir: Path) -> None:
    """Clear content between ARVIS_FUSED_BEGIN/END pragmas in RTL files.

    This ensures no duplicate enum values when Phase 5c re-patches.
    """
    import re

    for filename in [
        "include/cv32e40p_pkg.sv",
        "cv32e40p_decoder.sv",
        "cv32e40p_alu.sv",
        "cv32e40p_mult.sv",
    ]:
        fpath = rtl_dir / filename
        if not fpath.exists():
            continue
        text = fpath.read_text()
        # Replace content between BEGIN and END pragmas with empty
        text = re.sub(
            r"(// ARVIS_FUSED_BEGIN: \w+\n).*?(// ARVIS_FUSED_END: \w+)",
            r"\1\2",
            text,
            flags=re.DOTALL,
        )
        fpath.write_text(text)
