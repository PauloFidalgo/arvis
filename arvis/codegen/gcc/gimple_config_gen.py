"""Generate GIMPLE fusion plugin config (patterns.json) from analysis candidates.

Converts EnhancedFusionCandidate objects into the JSON format consumed by
the fused_pass.c GCC GIMPLE plugin.

For **2-grams**: handled by the combine pass (define_insn in .md files).
The GIMPLE plugin is NOT used for 2-grams — they work via RTL-level
pattern matching which is more reliable for 2-instruction chains.

For **3-grams**: the GIMPLE plugin matches tree-shaped IR before register
allocation, where 3-deep operation trees are naturally visible.

The plugin config includes:
  - GIMPLE tree codes (PLUS_EXPR, MINUS_EXPR, etc.)
  - Chain topology (which op feeds which, at which operand slot)
  - Commutativity flags per operation
  - Immediate handling (hardcoded values or variable via register)
  - Encoding (opcode, funct3, funct7) for the .insn directive
  - SV expression template for Verilog ALU generation

Usage:
    from arvis.codegen.gcc.gimple_config_gen import generate_gimple_config
    content, n = generate_gimple_config(all_fusions, "output/patterns.json")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Final, List, Set, Tuple

# ---------------------------------------------------------------------------
# RISC-V mnemonic → GIMPLE tree code mapping
# ---------------------------------------------------------------------------
# GCC GIMPLE uses tree_code enums for operations.
# The plugin matches these in the GIMPLE SSA IR.
#
# IMPORTANT: In GIMPLE, there are NO separate immediate variants.
# addi and add both map to PLUS_EXPR. The immediate appears as
# an INTEGER_CST operand node, not a different tree code.
#
# IMPORTANT: xori x, -1 is NOT BIT_XOR_EXPR with -1 in GIMPLE.
# GCC canonicalizes it to BIT_NOT_EXPR (a unary op). The plugin
# must match BIT_NOT_EXPR, not BIT_XOR_EXPR with const -1.

MNEMONIC_TO_GIMPLE: Final[Dict[str, Dict[str, Any]]] = {
    "add": {"gimple_code": "PLUS_EXPR", "commutative": True},
    "addi": {"gimple_code": "PLUS_EXPR", "commutative": True},
    "sub": {"gimple_code": "MINUS_EXPR", "commutative": False},
    "and": {"gimple_code": "BIT_AND_EXPR", "commutative": True},
    "andi": {"gimple_code": "BIT_AND_EXPR", "commutative": True},
    "or": {"gimple_code": "BIT_IOR_EXPR", "commutative": True},
    "ori": {"gimple_code": "BIT_IOR_EXPR", "commutative": True},
    "xor": {"gimple_code": "BIT_XOR_EXPR", "commutative": True},
    "xori": {"gimple_code": "BIT_XOR_EXPR", "commutative": True},
    "sll": {"gimple_code": "LSHIFT_EXPR", "commutative": False},
    "slli": {"gimple_code": "LSHIFT_EXPR", "commutative": False},
    "srl": {"gimple_code": "RSHIFT_EXPR", "commutative": False},
    "srli": {"gimple_code": "RSHIFT_EXPR", "commutative": False},
    "sra": {"gimple_code": "RSHIFT_EXPR", "commutative": False},  # signed shift
    "srai": {"gimple_code": "RSHIFT_EXPR", "commutative": False},
    "mul": {"gimple_code": "MULT_EXPR", "commutative": True},
}

# Compressed → base mapping (GIMPLE never sees compressed mnemonics)
COMPRESSED_TO_BASE: Final[Dict[str, str]] = {
    "c.add": "add",
    "c.sub": "sub",
    "c.and": "and",
    "c.or": "or",
    "c.xor": "xor",
    "c.addi": "addi",
    "c.slli": "slli",
    "c.srli": "srli",
    "c.srai": "srai",
    "c.andi": "andi",
    "c.li": "addi",
    "c.lui": "lui",
    "c.mv": "add",
}

# Operations that are GIMPLE-compatible (have a GIMPLE tree code)
GIMPLE_COMPATIBLE: Final[Set[str]] = set(MNEMONIC_TO_GIMPLE.keys())

# Operations to EXCLUDE from 3-gram fusion (critical path too long)
CRITICAL_PATH_EXCLUDE_3GRAM: Final[Set[str]] = {"mul"}

# Encoding: CUSTOM opcode space for 3-grams
# 3-grams use CUSTOM_2 (0x5B) to avoid overlap with 2-grams
# which use CUSTOM_0 (0x0B) and CUSTOM_1 (0x2B).
TRIGRAM_OPCODES: Final[List[int]] = [0x5B]


def normalize_mnemonic(mnemonic: str) -> str:
    """Normalize: compressed → base. Does NOT strip 'i' suffix.

    In GIMPLE, addi and add are the same (PLUS_EXPR), but we keep
    the distinction for the SV expression generator which needs to
    know if the second operand is a register or an immediate.
    """
    return COMPRESSED_TO_BASE.get(mnemonic, mnemonic)


def is_gimple_compatible(normalized: str) -> bool:
    """Check if normalized mnemonic has a GIMPLE plugin mapping."""
    return normalized in GIMPLE_COMPATIBLE


def is_alu_only(pattern: Tuple[str, ...]) -> bool:
    """Check if ALL instructions in pattern are ALU-only (GIMPLE-compatible)."""
    return all(is_gimple_compatible(normalize_mnemonic(m)) for m in pattern)


def _classify_3gram_encoding(
    n_external_inputs: int,
    n_hardcoded_imm: int,
    n_variable_imm: int,
) -> Dict[str, Any]:
    """Classify the encoding type for a 3-gram based on port requirements.

    Returns encoding info dict with:
      - encoding_type: "R-type" or "R4-type"
      - n_reg_inputs: number of register inputs in the encoding
      - notes: human-readable explanation
    """
    # Variable immediates consume a register slot
    effective_reg_inputs = n_external_inputs + n_variable_imm

    if effective_reg_inputs <= 2:
        return {
            "encoding_type": "R-type",
            "n_reg_inputs": effective_reg_inputs,
            "notes": (f"{n_external_inputs} regs + {n_hardcoded_imm} HC imm + {n_variable_imm} var imm"),
        }
    elif effective_reg_inputs <= 3:
        return {
            "encoding_type": "R4-type",
            "n_reg_inputs": effective_reg_inputs,
            "notes": (f"{n_external_inputs} regs + {n_hardcoded_imm} HC imm + {n_variable_imm} var imm"),
        }
    else:
        return {
            "encoding_type": "INFEASIBLE",
            "n_reg_inputs": effective_reg_inputs,
            "notes": f"Too many inputs: {effective_reg_inputs} > 3",
        }


def generate_gimple_config(
    all_fusions: list,
    output_path: str,
    max_3gram_patterns: int = 15,
    r4_slot_start: int = 0,
) -> Tuple[str, int]:
    """Generate patterns.json for the GIMPLE fusion plugin.

    Filters candidates to 3-gram ALU-only compute-only patterns,
    classifies their chain topology, immediate handling, and encoding,
    then writes the JSON config consumed by fused_pass.c.

    The config contains all information needed for:
    1. GIMPLE plugin to match the pattern in GCC IR
    2. SV expression generator to implement in Verilog
    3. Encoding allocator to assign funct3/funct7

    Args:
        all_fusions: List of EnhancedFusionCandidate objects from analysis.
        output_path: Where to write patterns.json.
        max_patterns: Maximum total patterns.
        max_3gram_patterns: Maximum 3-gram patterns (subset of max_patterns).

    Returns:
        (json_content, num_patterns)
    """
    seen: Dict[Tuple[str, ...], Dict[str, Any]] = {}

    for cand in all_fusions:
        pattern = cand.pattern
        n = len(pattern)

        # 2-grams with hardcoded immediates now handled by parametric .md patterns
        # Only 3-grams go to GIMPLE plugin
        if n == 2:
            continue  # All 2-grams handled by combine pass (.md)
        if n < 2 or n > 3:
            continue

        # Must be compute-only ALU
        if not is_alu_only(pattern):
            continue

        # Normalize mnemonics
        normalized = tuple(normalize_mnemonic(m) for m in pattern)

        # Verify all ops have GIMPLE mappings
        if not all(is_gimple_compatible(op) for op in normalized):
            continue

        # ARVIS DSP-unfriendly mul filter — reject ANY 3-gram containing
        # a mul-class mnemonic.  No 3-gram with mul fits cleanly into
        # the existing single-cycle MAC/MSU datapath, so emitting one
        # would force extra logic onto the multiplier critical path and
        # destroy FPGA Fmax.  Same predicate as the 2-gram .md filter.
        from arvis.codegen.rtl.isa_fusion.alu_single_cycle import is_dsp_unfriendly_mul_fusion

        if is_dsp_unfriendly_mul_fusion(tuple(normalized)):
            continue

        # Get signature info
        sig = getattr(cand, "signature", None)
        if sig is None:
            continue

        # Compute impact
        impact = getattr(cand, "impact", cand.frequency * cand.cycle_savings)

        # Get chain edges from signature
        chain_edges = []
        if hasattr(sig, "chain_edges"):
            chain_edges = [
                {
                    "producer": e.producer_idx,
                    "consumer": e.consumer_idx,
                    "consumer_slot": e.consumer_slot,
                }
                for e in sig.chain_edges
            ]

        # Filter: require a linear chain for GIMPLE matching.
        # Fan-in patterns (0→2, 1→2) are often false positives from
        # post-register-allocation register reuse. At GIMPLE level (SSA),
        # the ops may use different SSA variables despite sharing a register.
        # Linear chains (each op feeds the next) are reliably matchable.
        if n == 3:
            has_edge_01 = any(e["producer"] == 0 and e["consumer"] == 1 for e in chain_edges)
            has_edge_12 = any(e["producer"] == 1 and e["consumer"] == 2 for e in chain_edges)
            if not (has_edge_01 and has_edge_12):
                continue
        elif n == 2:
            has_edge_01 = any(e["producer"] == 0 and e["consumer"] == 1 for e in chain_edges)
            if not has_edge_01:
                continue

        # Build GIMPLE tree codes
        gimple_codes = []
        commutativity = []
        for m in normalized:
            info = MNEMONIC_TO_GIMPLE.get(m, {})
            gimple_codes.append(info.get("gimple_code", "UNKNOWN"))
            commutativity.append(info.get("commutative", False))

        # Immediate analysis
        imm_hc = getattr(cand, "imm_should_hardcode", ())
        imm_vals = getattr(cand, "hardcoded_imm_values", ())
        hardcoded_imm: Dict[str, int] = {}
        variable_imm_positions: List[int] = []
        n_hardcoded = 0
        n_variable = 0

        for i, m in enumerate(normalized):
            has_imm = m.endswith("i") and m not in ("mul",)
            if has_imm:
                should_hc = i < len(imm_hc) and imm_hc[i]
                if should_hc and i < len(imm_vals) and imm_vals[i] is not None:
                    hardcoded_imm[str(i)] = imm_vals[i]
                    n_hardcoded += 1
                else:
                    variable_imm_positions.append(i)
                    n_variable += 1

        # Classify encoding
        enc_info = _classify_3gram_encoding(
            n_external_inputs=sig.n_external_inputs,
            n_hardcoded_imm=n_hardcoded,
            n_variable_imm=n_variable,
        )
        if enc_info["encoding_type"] == "INFEASIBLE":
            continue

        # Filter: must have at least n-1 chain edges to form a complete tree
        # Without a complete chain, the GIMPLE plugin can't walk up the tree
        n_chain_edges = len(chain_edges)
        if n_chain_edges < n - 1:
            continue  # Incomplete chain — disconnected ops

        # NOTE: We do NOT filter by write_ports here.
        # The assembly-level liveness reports W=2 when intermediate
        # registers are live-out at SOME locations. But the GIMPLE
        # plugin checks single-use PER-INSTANCE at compile time —
        # it only matches when intermediates have exactly 1 use.
        # So we keep all patterns; the plugin handles liveness dynamically.

        # Deduplicate by normalized pattern (keep highest impact)
        if normalized not in seen or impact > seen[normalized]["impact"]:
            seen[normalized] = {
                "normalized": normalized,
                "impact": impact,
                "frequency": cand.frequency,
                "cycle_savings": cand.cycle_savings,
                "gimple_codes": gimple_codes,
                "commutativity": commutativity,
                "chain_edges": chain_edges,
                "is_linear_chain": sig.is_linear_chain if hasattr(sig, "is_linear_chain") else True,
                "has_fan_out": sig.has_fan_out if hasattr(sig, "has_fan_out") else False,
                "has_register_reuse": (sig.has_register_reuse if hasattr(sig, "has_register_reuse") else False),
                "n_external_inputs": sig.n_external_inputs,
                "n_external_outputs": sig.n_external_outputs,
                "imm_positions": list(sig.imm_positions),
                "hardcoded_imm": hardcoded_imm,
                "variable_imm_positions": variable_imm_positions,
                "encoding_type": enc_info["encoding_type"],
                "n_reg_inputs": enc_info["n_reg_inputs"],
                "encoding_notes": enc_info["notes"],
            }

    # Sort by impact, limit to max_3gram_patterns
    all_patterns = sorted(seen.values(), key=lambda x: x["impact"], reverse=True)
    if len(all_patterns) > max_3gram_patterns:
        all_patterns = all_patterns[:max_3gram_patterns]

    # Assign encodings — continue from where .md patterns left off
    json_patterns = []
    r4_slot = r4_slot_start  # Continue R4 allocation after .md patterns
    r_slot = 0  # R-type slot counter (not used by .md)

    for pat_info in all_patterns:
        normalized = pat_info["normalized"]
        is_r4 = pat_info["encoding_type"] == "R4-type"

        if is_r4:
            # Continuous R4 allocation across all CUSTOM opcodes
            _ALL_OPCODES = [0x0B, 0x2B, 0x5B, 0x7B]
            opc_idx = r4_slot // 16
            if opc_idx >= len(_ALL_OPCODES):
                break
            opcode = _ALL_OPCODES[opc_idx]
            local = r4_slot % 16
            funct3 = local // 4
            funct2 = local % 4
            funct7 = funct2
            r4_slot += 1
        else:
            # R-type: use slots after R4 range (funct3=4+) on the current opcode
            _ALL_OPCODES = [0x0B, 0x2B, 0x5B, 0x7B]
            opcode_idx = r_slot // (4 * 128)
            if opcode_idx >= len(_ALL_OPCODES):
                break
            opcode = _ALL_OPCODES[opcode_idx]
            local = r_slot % (4 * 128)
            funct3 = 4 + local // 128
            funct7 = local % 128
            r_slot += 1

        name = "fused3_" + "_".join(normalized)

        json_patterns.append(
            {
                "name": name,
                "ops": list(normalized),
                "gimple_codes": pat_info["gimple_codes"],
                "commutativity": pat_info["commutativity"],
                "chain_edges": pat_info["chain_edges"],
                "chain_topology": {
                    "is_linear": pat_info["is_linear_chain"],
                    "has_fan_out": pat_info["has_fan_out"],
                    "has_register_reuse": pat_info["has_register_reuse"],
                },
                "immediates": {
                    "positions": pat_info["imm_positions"],
                    "hardcoded": pat_info["hardcoded_imm"],
                    "variable_positions": pat_info["variable_imm_positions"],
                },
                "encoding": {
                    "opcode": opcode,
                    "funct3": funct3,
                    "funct7": funct7,
                    "type": pat_info["encoding_type"],
                    "is_r4": is_r4,
                    "n_reg_inputs": pat_info["n_reg_inputs"],
                },
                "register_ports": {
                    "n_external_inputs": pat_info["n_external_inputs"],
                    "n_external_outputs": pat_info["n_external_outputs"],
                },
                "metrics": {
                    "impact": pat_info["impact"],
                    "frequency": pat_info["frequency"],
                    "cycle_savings": pat_info["cycle_savings"],
                },
                "enabled": True,
            }
        )

    config = {
        "version": 2,
        "description": "3-gram GIMPLE fusion patterns for fused_pass.c plugin",
        "patterns": json_patterns,
    }
    content = json.dumps(config, indent=2)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(content)

    n_patterns = len(json_patterns)
    print(f"  Generated {n_patterns} GIMPLE 3-gram patterns -> {output_path}")
    for p in json_patterns[:5]:
        enc = p["encoding"]
        imm = p["immediates"]
        hc = len(imm["hardcoded"])
        var = len(imm["variable_positions"])
        print(
            f"    {p['name']}: {' -> '.join(p['ops'])} "
            f"[{enc['type']}] "
            f"impact={p['metrics']['impact']:,} "
            f"imm={hc}HC+{var}var"
        )
    if n_patterns > 5:
        print(f"    ... and {n_patterns - 5} more")

    return content, n_patterns
