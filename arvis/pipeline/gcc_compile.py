"""
Phase 5b: Custom GCC Build + Benchmark Compilation + Hex Inspection.

Implements a multi-pass GCC pattern matching pipeline for 2-gram fusions:

  **Passes 1-3** (Docker: ``custom-riscv-gcc-passN``, built ONCE, reused):
    Static ``tools/custom-fused-passN.md`` — RR+RR 2-gram ALU patterns.
    Each pass covers a batch of the 485 exhaustive 2-gram patterns.
    Compile benchmark → inspect binary → collect used patterns.

  **HC Pass** (no Docker build needed):
    Hardcoded-immediate patterns from profiling analysis — specialized
    2-gram patterns where the immediate is always the same value.

  **Merge → Final Docker** (``custom-riscv-gcc-merged``, built per-run):
    Merge used patterns from Pass 1-3 + HC into single ``custom-fused.md``.
    Build new Docker → compile → inspect → report used instructions.

  **3-gram patterns** are NOT handled here — they will be matched by the
  GIMPLE plugin (``fused_pass.so``) which operates on tree-level IR before
  register allocation, where 3-deep operation chains are naturally visible.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

_RISCV_PREFIX = os.environ.get("ARVIS_RISCV_PREFIX", "/opt/riscv")

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext

from arvis.cli import print_error, print_step, print_success, print_warning  # noqa: E402

logger = logging.getLogger(__name__)


# Global flag: set to True only during the final/merged compile
_enable_plugin_flags = False


def _get_plugin_flags(cfg: "ToolConfig") -> List[str]:
    """Return GCC GIMPLE plugin flags if patterns.json config exists.

    Only returns flags when _enable_plugin_flags is True (set during
    the final/merged compile). Pass 1-3 Docker images don't have the
    plugin compiled, so we must not add -fplugin for those compiles.
    """
    if not _enable_plugin_flags:
        return []
    patterns_json = Path(cfg.output_dir) / "patterns.json"
    if patterns_json.exists():
        specializer_dir = str(Path(cfg.output_dir).parent.parent)
        if not os.path.isdir(specializer_dir):
            specializer_dir = "."
        rel_path = os.path.relpath(str(patterns_json), specializer_dir)
        return [
            "-fplugin=/opt/riscv/lib/gcc-plugin/fused_pass.so",
            f"-fplugin-arg-fused_pass-config=/work/{rel_path}",
            "-fdump-tree-fused_pass-details",
        ]
    return []


# Known CUSTOM opcode values used for fused instructions
CUSTOM_OPCODES: Set[int] = {
    0x0B,
    0x2B,
    0x5B,
    0x7B,
    0x07,
    0x1B,
    0x1F,
    0x27,
    0x2F,
    0x3B,
    0x3F,
    0x43,
    0x47,
    0x4B,
    0x4F,
    0x53,
    0x57,
    0x5F,
    0x6B,
    0x77,
    0x7F,
}

# Docker image names for each pass
PASS1_IMAGE = "custom-riscv-gcc-pass1"
PASS2_IMAGE = "custom-riscv-gcc-pass2"
PASS3_IMAGE = "custom-riscv-gcc-pass3"
MERGED_IMAGE = "custom-riscv-gcc-merged"

STATIC_PASSES: List[Tuple[str, str, str, str]] = [
    ("custom-fused-pass1.md", PASS1_IMAGE, "Pass 1", "RR+RR 2-grams batch 1"),
    ("custom-fused-pass2.md", PASS2_IMAGE, "Pass 2", "RR+RR 2-grams batch 2"),
    ("custom-fused-pass3.md", PASS3_IMAGE, "Pass 3", "RR+RR 2-grams batch 3 (remaining)"),
]


@dataclass
class UsedCustomInstruction:
    """A custom instruction encoding found in the compiled binary."""

    opcode: int
    funct3: int
    funct7: int
    count: int
    is_r4: bool = False
    pattern_name: str = ""
    mnemonics: Tuple[str, ...] = ()
    # For R4 parametric patterns, the set of distinct values seen in
    # instr[31:27] (rs3 field, fused_imm_i[4:0]) across all instances.
    # Empty for non-parametric or non-R4 encodings.
    used_imm_values: Set[int] = field(default_factory=set)


@dataclass
class GCCCompileResult:
    """Result of the GCC compile pipeline stage."""

    gcc_build_ok: bool = False
    compile_ok: bool = False
    fused_elf_path: str = ""
    fused_hex_path: str = ""
    used_instructions: List[UsedCustomInstruction] = field(default_factory=list)
    total_fused_count: int = 0
    unique_patterns: int = 0
    next_r4_slot: int = 0  # next available R4 encoding slot (for hwloop)
    # Per-pass results: list of (pass_label, used_instructions)
    per_pass_used: List[Tuple[str, List[UsedCustomInstruction]]] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _get_specializer_dir(cfg: "ToolConfig") -> str:
    """Resolve the cv32e40p_specializer root directory."""
    candidate = str(Path(cfg.output_dir).parent.parent)
    if os.path.isdir(candidate):
        return candidate
    return "."


def _show_md_stats(md_path: str) -> None:
    """Print quick stats about an .md file."""
    content = Path(md_path).read_text()
    r4 = content.count(".insn r4")
    r_total = content.count(".insn r ") + content.count(".insn r 0x")
    # Subtract r4 false positives from R count
    r = r_total - r4 if r_total > r4 else r_total
    hc = content.count("hardcoded(")
    n_insn = content.count("define_insn")
    print(f"  Patterns: {n_insn} define_insn (R4={r4}, R~={r}, HC={hc})")


def _used_to_encoding_tuples(
    used: List[UsedCustomInstruction],
) -> List[Tuple[int, int, int, bool]]:
    """Convert UsedCustomInstruction list to (opcode, f3, f7, is_r4) tuples."""
    return [(u.opcode, u.funct3, u.funct7, u.is_r4) for u in used]


def _build_parametric_patterns(cfg) -> list:
    """Build parametric HC patterns by scanning the original binary.

    Disassembles the spike ELF and finds all 2-gram pairs with immediates
    (consecutive + non-consecutive). Returns list of ((mnem1, mnem2), count).
    """
    import re
    import subprocess
    from collections import Counter

    from arvis.codegen.fusion.asm_patcher import parse_asm_instructions

    elf = getattr(cfg, "elf_path", None)
    print(f"  [DBG] parametric scan: elf_path attr = {elf}")
    if not elf or not Path(elf).exists():
        # Try spike ELF from benchmark dir
        from arvis.config import BENCHMARKS

        bm = BENCHMARKS.get(cfg.benchmark_name, {})
        elf = str(Path(cfg.benchmark_dir) / bm.get("elf", ""))
        print(f"  [DBG] parametric scan: fallback elf = {elf}")
    if not elf or not Path(elf).exists():
        print("  [DBG] parametric scan: ELF not found, returning empty")
        return []

    print(f"  [DBG] parametric scan: disassembling {elf}")
    r = subprocess.run(["riscv32-unknown-elf-objdump", "-d", elf], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"  [DBG] parametric scan: objdump failed (rc={r.returncode})")
        return []

    lines = []
    for line in r.stdout.splitlines():
        m = re.match(r"\s*[0-9a-f]+:\s+[0-9a-f]+\s+(\S+)\s*(.*)", line)
        if m:
            ops = m.group(2).strip().split("#")[0].strip()
            lines.append(f"\t{m.group(1)}\t{ops}")
        elif "<" in line and ">:" in line:
            lbl = re.search(r"<(.+)>:", line)
            if lbl:
                lines.append(f"{lbl.group(1)}:")

    insns = parse_asm_instructions(lines)
    print(f"  [DBG] parametric scan: {len(insns)} instructions parsed")
    BRANCH = {
        "beq",
        "bne",
        "blt",
        "bge",
        "bltu",
        "bgeu",
        "bgtu",
        "bleu",
        "j",
        "jal",
        "jalr",
        "ret",
        "beqz",
        "bnez",
    }

    counts = Counter()
    n_alu_pairs = 0
    n_with_dep = 0
    n_with_imm = 0
    n_imm_ok = 0

    # Consecutive
    for j in range(len(insns) - 1):
        a, b = insns[j], insns[j + 1]
        if not a.is_alu or not b.is_alu:
            continue
        n_alu_pairs += 1
        if not a.rd or (a.rd != b.rs1 and a.rd != b.rs2):
            continue
        n_with_dep += 1
        if a.imm is None and b.imm is None:
            continue
        n_with_imm += 1
        a_ok = a.imm is None or (0 <= a.imm <= 31)
        b_ok = b.imm is None or (0 <= b.imm <= 31)
        if a_ok and b_ok:
            n_imm_ok += 1
            counts[(a.base_mnem, b.base_mnem)] += 1

    # Non-consecutive (gap 1-3)
    for j in range(len(insns)):
        a = insns[j]
        if not a.is_alu or not a.rd or a.imm is None:
            continue
        if not (0 <= a.imm <= 31):
            continue
        for k in range(j + 2, min(j + 5, len(insns))):
            b = insns[k]
            if not b.is_alu or not b.rd:
                continue
            if a.rd != b.rs1 and a.rd != b.rs2:
                continue
            if b.imm is not None and not (0 <= b.imm <= 31):
                continue
            ok = True
            for g in range(j + 1, k):
                gi = insns[g]
                if gi.rd == a.rd or gi.base_mnem in BRANCH:
                    ok = False
                    break
                if gi.rs1 == a.rd or gi.rs2 == a.rd:
                    ok = False
                    break
                if gi.raw.strip().endswith(":"):
                    ok = False
                    break
            if not ok:
                continue
            counts[(a.base_mnem, b.base_mnem)] += 1

    result = [(pat, cnt) for pat, cnt in sorted(counts.items(), key=lambda x: -x[1]) if cnt >= 3]
    # Filter out DSP-unfriendly mul fusions (anything mul-related except mul+add and mul+sub).
    # These wreck FPGA Fmax by dropping shift/AND/OR logic onto the multiplier critical path.
    from arvis.codegen.rtl.isa_fusion.alu_single_cycle import is_dsp_unfriendly_mul_fusion

    before = len(result)
    rejected = [pat for pat, _ in result if is_dsp_unfriendly_mul_fusion(pat)]
    result = [(pat, cnt) for pat, cnt in result if not is_dsp_unfriendly_mul_fusion(pat)]
    if rejected:
        print(
            f"  [DBG] parametric scan: dropped {before - len(result)} DSP-unfriendly mul fusions: "
            f"{', '.join('+'.join(p) for p in rejected)}"
        )
    print(
        f"  [DBG] parametric scan: {n_alu_pairs} ALU pairs, {n_with_dep} with dep, {n_with_imm} with imm, {n_imm_ok} imm in range"  # noqa: E501
    )
    print(f"  [DBG] parametric scan: {len(counts)} unique patterns, {len(result)} surviving (cnt>=3)")
    for pat, cnt in sorted(counts.items(), key=lambda x: -x[1])[:5]:
        print(f"  [DBG]   {pat[0]}+{pat[1]}: {cnt}x {'OK' if cnt >= 3 else 'DROPPED'}")
    return result


# ─────────────────────────────────────────────────────────────────────
# Single-pass execution
# ─────────────────────────────────────────────────────────────────────


def _run_pass(
    cfg: "ToolConfig",
    md_path: str,
    image_name: str,
    pass_label: str,
    specializer_dir: str,
) -> Optional[List[UsedCustomInstruction]]:
    """Run one pass: ensure Docker image → compile → inspect → return used.

    Returns None on failure, empty list if no custom instructions found.
    """
    # Check / build Docker image
    print_step(pass_label, f"Checking Docker image '{image_name}'...")
    if _docker_image_exists(image_name):
        print_success(f"'{image_name}' exists — skipping build")
    else:
        print(f"  Building '{image_name}' (~10-30 min)...")
        ok = _build_custom_gcc(cfg, Path(md_path), image_name=image_name)
        if not ok:
            print_error(f"{pass_label} Docker build failed")
            return None

    # Compile benchmark
    print_step(pass_label, "Compiling benchmark...")
    compile_ok, elf_path, hex_path = _compile_benchmark(cfg, image=image_name)
    if not compile_ok:
        print_error(f"{pass_label} compilation failed")
        return None

    # Inspect binary for custom instructions
    print_step(pass_label, "Inspecting binary...")
    # We need to build the encoding map from the PASS-specific .md,
    # not from the output directory's custom-fused.md.
    used = _inspect_binary_with_md(cfg, elf_path, md_path, image=image_name)

    n = len(used)
    t = sum(u.count for u in used)
    print(f"  {pass_label}: {n} unique patterns, {t} instances")
    for u in sorted(used, key=lambda x: -x.count)[:10]:
        tag = " (R4)" if u.is_r4 else ""
        print(f"     {u.count:>5}x 0x{u.opcode:02x}/f3={u.funct3}/f7=0x{u.funct7:02x}{tag}  {u.pattern_name}")
    if n > 10:
        print(f"     ... and {n - 10} more")

    return used


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────


def run(cfg: "ToolConfig", ctx: "PipelineContext") -> None:
    """Execute Phase 5b: multi-pass GCC Build + Compile + Hex Inspection.

    Runs all static passes (2-gram + 3-gram), collects HC patterns from
    profiling, merges used patterns, builds final Docker, compiles, and
    inspects the binary.

    Caches results: if the merged .md and fused ELF already exist from a
    previous run, skips all passes and reloads the cached result.
    """
    import json as _json

    n_passes = len(STATIC_PASSES)
    from arvis.cli import print_section

    print_section(f"PHASE 5b: {n_passes}-PASS GCC BUILD + COMPILE + HEX INSPECTION")

    result = GCCCompileResult()
    specializer_dir = _get_specializer_dir(cfg)

    # ── Check for cached results from previous run ──
    merged_md_path = str(Path(cfg.output_dir) / "custom-fused.md")
    cache_path = Path(cfg.output_dir) / ".gcc_compile_cache.json"
    fused_elf_name = f"{cfg.benchmark_name}_fused.elf"
    fused_elf_path = os.path.join(cfg.output_dir, fused_elf_name)

    if cache_path.exists() and os.path.exists(fused_elf_path):
        print("  Checking cache...")
        try:
            cache = _json.loads(cache_path.read_text())

            # Restore the merged .md if the cache has it
            # (Phase 5a may have overwritten it with HC-only content)
            merged_md_content = cache.get("merged_md_content")
            if merged_md_content:
                Path(merged_md_path).parent.mkdir(parents=True, exist_ok=True)
                Path(merged_md_path).write_text(merged_md_content)

            result.gcc_build_ok = True
            result.compile_ok = True
            result.fused_elf_path = fused_elf_path
            result.fused_hex_path = cache.get("fused_hex_path", "")
            result.total_fused_count = cache.get("total_fused_count", 0)
            result.unique_patterns = cache.get("unique_patterns", 0)
            result.next_r4_slot = cache.get("next_r4_slot", 0)
            for ui_data in cache.get("used_instructions", []):
                result.used_instructions.append(
                    UsedCustomInstruction(
                        opcode=ui_data["opcode"],
                        funct3=ui_data["funct3"],
                        funct7=ui_data["funct7"],
                        count=ui_data["count"],
                        is_r4=ui_data.get("is_r4", False),
                        pattern_name=ui_data.get("pattern_name", ""),
                        mnemonics=tuple(ui_data.get("mnemonics", ())),
                        used_imm_values=set(ui_data.get("used_imm_values", [])),
                    )
                )
            print_success("Cached results loaded — skipping pass compilation")
            print(f"     Merged .md: {merged_md_path}")
            print(f"     Fused ELF:  {fused_elf_path}")
            print(f"     {result.unique_patterns} patterns, {result.total_fused_count} instances")
            ctx.gcc_compile_result = result
            ctx.fused_hex_path = result.fused_hex_path
            ctx.fused_elf_path = result.fused_elf_path
            return
        except (KeyError, ValueError, _json.JSONDecodeError):
            print_warning("Cache corrupted — re-running passes")

    # ══════════════════════════════════════════════════════════════════
    # STATIC PASSES: Run each pass, collect used patterns
    # ══════════════════════════════════════════════════════════════════
    all_pass_used: List[Tuple[str, str, List[UsedCustomInstruction]]] = []

    for md_filename, image_name, label, description in STATIC_PASSES:
        md_path = os.path.join(specializer_dir, "tools", md_filename)
        if not os.path.exists(md_path):
            print_warning(f"\n  ⚠️  {label} .md not found: {md_path} — skipping")
            continue

        print(f"\n  ══ {label}: {description} ══")
        print(f"  Using: {md_path}")
        _show_md_stats(md_path)

        used = _run_pass(cfg, md_path, image_name, label, specializer_dir)
        if used is None:
            print_warning(f"{label} failed — continuing with remaining passes")
            used = []

        all_pass_used.append((label, md_path, used))
        result.per_pass_used.append((label, used))

    total_pass_found = sum(len(u) for _, _, u in all_pass_used)
    total_instances = sum(sum(x.count for x in u) for _, _, u in all_pass_used)
    print(
        f"\n  ── Pass summary: {total_pass_found} unique patterns, "
        f"{total_instances} total instances across {len(all_pass_used)} passes ──"
    )

    # ══════════════════════════════════════════════════════════════════
    # HC PASS: Hardcoded-immediate patterns from profiling
    # ══════════════════════════════════════════════════════════════════
    print("\n  ══ HC PASS: Hardcoded-immediate patterns ══")
    hc_candidates = getattr(ctx, "all_fusions", None) or []
    n_hc_candidates = len(hc_candidates)
    n_with_hc = sum(1 for c in hc_candidates if getattr(c, "has_hardcoded_imm", False))
    print(f"  Candidates from profiling: {n_hc_candidates} ({n_with_hc} with HC imm)")

    # ══════════════════════════════════════════════════════════════════
    # 3-GRAM PASS: Generate GIMPLE plugin config (patterns.json)
    # ══════════════════════════════════════════════════════════════════
    n_3gram = 0
    print("\n  ══ 3-GRAM PASS: GIMPLE-level pattern discovery ══")
    patterns_json_path = str(Path(cfg.output_dir) / "patterns.json")
    n_3gram = 0  # Will be set after .md generation (needs next_r4_slot)

    # ══════════════════════════════════════════════════════════════════
    # MERGE: Combine all used patterns + HC into final .md
    # ══════════════════════════════════════════════════════════════════
    print("\n  ══ MERGE: Combining used patterns ══")

    # Check parametric patterns from static binary scan (always runs)
    parametric = _build_parametric_patterns(cfg)
    if parametric:
        print(f"  Parametric HC patterns: {len(parametric)}")
        for pat, cnt in parametric[:10]:
            print(f"    {'→'.join(pat):20s} {cnt:>5}x")
    else:
        print("  Parametric HC patterns: 0 (no patterns found in binary)")

    if total_pass_found == 0 and n_with_hc == 0 and n_3gram == 0 and not parametric:
        print_warning("No patterns found in any pass, no immediates, and no 3-grams — nothing to do")
        ctx.gcc_compile_result = result
        return

    # If we only have 3-grams (no 2-grams), create an empty merged .md
    # so the Docker image builds successfully. The GIMPLE plugin will
    # handle the 3-grams via patterns.json.

    from arvis.codegen.gcc.peephole_gen import generate_merged_md

    # Build per-pass encoding tuples and md paths
    pass_encodings: List[Tuple[str, List[Tuple[int, int, int, bool]]]] = []
    pass_md_paths: List[Tuple[str, str]] = []
    for label, md_path, used in all_pass_used:
        pass_encodings.append((md_path, _used_to_encoding_tuples(used)))
        pass_md_paths.append((label, md_path))

    merged_md_path = str(Path(cfg.output_dir) / "custom-fused.md")

    content, n_merged, next_r4_slot = generate_merged_md(
        pass_used_list=pass_encodings,
        hc_candidates=None,
        output_path=merged_md_path,
        parametric_patterns=parametric,
    )
    print(f"  Merged .md: {merged_md_path}")
    print(f"  Total merged patterns: {n_merged}")
    for label, _, used in all_pass_used:
        print(f"    {label}: {len(used)} used")
    print("    HC (profiling): included in merge")

    # ── 3-gram patterns: generate AFTER .md to get correct slot allocation ──
    try:
        from arvis.codegen.gcc.gimple_config_gen import generate_gimple_config

        _, n_3gram = generate_gimple_config(
            hc_candidates,
            patterns_json_path,
            max_3gram_patterns=15,
            r4_slot_start=next_r4_slot,
        )
        if n_3gram > 0:
            print(f"  3-gram GIMPLE patterns: {n_3gram} (slots {next_r4_slot}-{next_r4_slot + n_3gram - 1})")
        else:
            print("  3-gram GIMPLE patterns: 0")
    except Exception as e:
        print_warning(f"3-gram generation failed: {e}")
        n_3gram = 0

    if n_merged == 0:
        print_warning("No patterns in merged .md — skipping final compile")
        ctx.gcc_compile_result = result
        return

    # ══════════════════════════════════════════════════════════════════
    # FINAL: Build merged Docker → compile → inspect
    # ══════════════════════════════════════════════════════════════════
    print("\n  ══ FINAL: Merged Docker compile ══")
    print(f"  Using: {merged_md_path}")
    _show_md_stats(merged_md_path)

    # Always rebuild the merged image (benchmark-specific)
    print(f"  Building '{MERGED_IMAGE}' (~10-30 min)...")
    ok = _build_custom_gcc(cfg, Path(merged_md_path), image_name=MERGED_IMAGE)
    if not ok:
        print_error("Merged Docker build failed")
        ctx.gcc_compile_result = result
        return

    print("  Compiling benchmark with merged GCC...")
    global _enable_plugin_flags
    _enable_plugin_flags = n_3gram > 0  # Enable plugin only if we have 3-gram patterns
    compile_ok, elf_path, hex_path = _compile_benchmark(cfg)
    _enable_plugin_flags = False
    if not compile_ok:
        print_error("Final compilation failed")
        ctx.gcc_compile_result = result
        return

    result.gcc_build_ok = True
    result.compile_ok = True
    result.fused_elf_path = elf_path
    result.fused_hex_path = hex_path
    result.next_r4_slot = next_r4_slot + n_3gram

    print("  Inspecting final binary for custom instructions...")
    used = _inspect_binary_with_md(cfg, elf_path, merged_md_path)
    result.used_instructions = used
    result.total_fused_count = sum(u.count for u in used)
    result.unique_patterns = len(used)

    if used:
        print_success(f"\n  ✅ Found {result.unique_patterns} unique custom instruction patterns")
        print(f"     Total fused instruction instances: {result.total_fused_count}")
        for ui in sorted(used, key=lambda x: -x.count):
            r4_tag = " (R4)" if ui.is_r4 else ""
            print(
                f"     {ui.count:>5}x opcode=0x{ui.opcode:02x} "
                f"funct3={ui.funct3} funct7=0x{ui.funct7:02x}"
                f"{r4_tag}  {ui.pattern_name}"
            )
        # Print 3-gram summary
        trigram_used = [ui for ui in used if ui.opcode == 0x5B]
        twogram_used = [ui for ui in used if ui.opcode != 0x5B]
        n_trigram_instances = sum(ui.count for ui in trigram_used)
        n_twogram_instances = sum(ui.count for ui in twogram_used)
        print(f"\n     2-gram (CUSTOM_0/1): {len(twogram_used)} patterns, {n_twogram_instances} instances")
        print(f"     3-gram (CUSTOM_2):   {len(trigram_used)} patterns, {n_trigram_instances} instances")
    else:
        print_warning("No custom instructions found in final binary")

    ctx.gcc_compile_result = result
    ctx.fused_hex_path = result.fused_hex_path
    ctx.fused_elf_path = result.fused_elf_path

    # ── Save cache for future re-runs ──
    try:
        import json as _json

        # Read the merged .md to save in cache (Phase 5a may overwrite it)
        merged_md_content = ""
        if os.path.exists(merged_md_path):
            merged_md_content = Path(merged_md_path).read_text()

        cache_data = {
            "fused_hex_path": result.fused_hex_path,
            "fused_elf_path": result.fused_elf_path,
            "total_fused_count": result.total_fused_count,
            "unique_patterns": result.unique_patterns,
            "next_r4_slot": result.next_r4_slot,
            "merged_md_content": merged_md_content,
            "used_instructions": [
                {
                    "opcode": ui.opcode,
                    "funct3": ui.funct3,
                    "funct7": ui.funct7,
                    "count": ui.count,
                    "is_r4": ui.is_r4,
                    "pattern_name": ui.pattern_name,
                    "mnemonics": list(ui.mnemonics),
                    "used_imm_values": sorted(ui.used_imm_values),
                }
                for ui in result.used_instructions
            ],
        }
        cache_path = Path(cfg.output_dir) / ".gcc_compile_cache.json"
        cache_path.write_text(_json.dumps(cache_data, indent=2))
    except Exception:
        pass  # Cache is best-effort


# ─────────────────────────────────────────────────────────────────────
# Docker helpers
# ─────────────────────────────────────────────────────────────────────


def _docker_image_exists(image_name: str) -> bool:
    """Check if a Docker image exists locally."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Alias for cleaner code in _build_custom_gcc
_image_exists = _docker_image_exists


# Name of the shared base image with pre-cloned & configured toolchain
_BASE_IMAGE = "riscv-gcc-base"


_PREBUILT_IMAGE = "riscv-gcc-prebuilt"


def _ensure_prebuilt_image(specializer_dir: Path) -> bool:
    """Build the prebuilt Docker image (binutils+newlib+stage1) if needed.

    This image is built ONCE from riscv-gcc-base. It pre-compiles
    binutils, newlib, and GCC stage1 so that pass-specific builds
    only need to rebuild GCC stage2 (~60s instead of ~500s).
    """
    if _docker_image_exists(_PREBUILT_IMAGE):
        return True

    if not _ensure_base_image(specializer_dir):
        return False

    prebuilt_dockerfile = specializer_dir / "tools" / "Dockerfile.gcc-prebuilt"
    if not prebuilt_dockerfile.exists():
        return False

    print(f"  Building '{_PREBUILT_IMAGE}' (binutils + newlib + stage1, ~10 min)...")
    cmd = [
        "docker",
        "build",
        "-t",
        _PREBUILT_IMAGE,
        "-f",
        str(prebuilt_dockerfile),
        str(specializer_dir / "tools"),
    ]
    try:
        ret = subprocess.call(cmd, timeout=3600)
        if ret == 0:
            print_success(f"'{_PREBUILT_IMAGE}' built successfully")
            return True
        print_warning(f"Prebuilt image build failed (exit {ret})")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print_warning("Prebuilt image build failed")
    return False


def _ensure_base_image(specializer_dir: Path) -> bool:
    """Build the base Docker image if it doesn't exist.

    The base image contains the riscv-gnu-toolchain source fully cloned,
    submodules initialized, and configured — but NOT compiled.
    It is built ONCE and reused by all pass-specific images.

    Returns True if the base image is available.
    """
    if _docker_image_exists(_BASE_IMAGE):
        return True

    base_dockerfile = specializer_dir / "tools" / "Dockerfile.gcc-base"
    if not base_dockerfile.exists():
        print_warning(f"Dockerfile.gcc-base not found at {base_dockerfile}")
        return False

    print(f"  Building base image '{_BASE_IMAGE}' (clone + configure, ~15 min)...")
    cmd = [
        "docker",
        "build",
        "-t",
        _BASE_IMAGE,
        "-f",
        str(base_dockerfile),
        str(specializer_dir / "tools"),
    ]

    try:
        ret = subprocess.call(cmd, timeout=3600)
        if ret != 0:
            print_error(f"Base image build failed (exit code {ret})")
            return False
        print_success(f"Base image '{_BASE_IMAGE}' built successfully")
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print_error("Base image build failed")
        return False


def _md_needs_builtin_patch(md_path: Path) -> bool:
    """Check if the .md file contains patterns that need the riscv.md builtin patch.

    The patch is needed when the .md includes sign_extend or zero_extend
    override patterns (e.g., fused_hc_slli_srai_0eq16_1eq16) that conflict
    with GCC's built-in *extendhisi2 etc.

    Returns True if the patch should be applied for this workload.
    """
    if not md_path.exists():
        return False
    content = md_path.read_text()
    # Check for canonical sign/zero-extend RTL patterns in the .md file
    return "sign_extend:SI" in content or "zero_extend:SI" in content


def _build_custom_gcc(cfg: "ToolConfig", md_path: Path, image_name: str = MERGED_IMAGE) -> bool:
    """Build custom RISC-V GCC with fused patterns via Docker.

    Uses a 2-stage approach for speed:
    1. Base image (riscv-gcc-base): clone + configure (built ONCE)
    2. Pass image: copy .md + make (per-pass, ~10 min instead of ~30)

    Falls back to the monolithic Dockerfile.custom-gcc-full if the
    base image cannot be built.
    """
    if not shutil.which("docker"):
        print_error("Docker not found on PATH")
        return False

    specializer_dir = Path(_get_specializer_dir(cfg))
    relative_md = os.path.relpath(str(md_path), str(specializer_dir))
    print(f"  Docker build context: {specializer_dir}")
    print(f"  Patterns file: {relative_md}")

    # Detect if this .md needs the builtin riscv.md patch
    # (only when sign_extend/zero_extend patterns are present — typically
    # only in the merged/final build, not in Pass 1-3 which are RR+RR only)
    patched_riscv_md_arg = "tools/riscv-md-noop.txt"  # default: no patch
    if _md_needs_builtin_patch(md_path):
        from arvis.codegen.gcc.riscv_md_patcher import get_patched_riscv_md

        patched_path = get_patched_riscv_md(
            base_image=_BASE_IMAGE,
            needs_sign_extend_patch=True,
            needs_zero_extend_patch=True,
        )
        if not patched_path or not patched_path.exists():
            raise RuntimeError(
                "riscv.md patcher failed: patched file not generated. "
                "The merged .md contains sign/zero-extend patterns that need "
                "the builtin riscv.md override. Cannot proceed without it."
            )
        patched_riscv_md_arg = os.path.relpath(str(patched_path), str(specializer_dir))
        print(f"  📋 Builtin pattern override: ENABLED → {patched_riscv_md_arg}")

    # Try fast build: FROM riscv-gcc-prebuilt (only GCC stage2, ~60-120s)
    # Auto-build prebuilt image if it doesn't exist yet
    fast_dockerfile = specializer_dir / "tools" / "Dockerfile.gcc-fast"
    if fast_dockerfile.exists() and (_image_exists(_PREBUILT_IMAGE) or _ensure_prebuilt_image(specializer_dir)):
        cmd = [
            "docker",
            "build",
            "-t",
            image_name,
            "-f",
            str(fast_dockerfile),
            "--build-arg",
            f"CUSTOM_FUSED_MD={relative_md}",
            "--build-arg",
            f"PATCHED_RISCV_MD={patched_riscv_md_arg}",
            str(specializer_dir),
        ]
        print(f"  Running: docker build -t {image_name} (FAST: stage2 only)...")
        try:
            ret = subprocess.call(cmd, timeout=3600)
            if ret == 0:
                print_success(f"Docker image '{image_name}' built (fast mode)")
                return True
            print_warning(f"Fast build failed (exit {ret}), falling back...")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print_warning("Fast build failed, falling back...")

    # Try 2-stage build: base image + pass Dockerfile (full make)
    pass_dockerfile = specializer_dir / "tools" / "Dockerfile.custom-gcc-pass"
    if pass_dockerfile.exists() and _ensure_base_image(specializer_dir):
        cmd = [
            "docker",
            "build",
            "-t",
            image_name,
            "-f",
            str(pass_dockerfile),
            "--build-arg",
            f"CUSTOM_FUSED_MD={relative_md}",
            "--build-arg",
            f"PATCHED_RISCV_MD={patched_riscv_md_arg}",
            str(specializer_dir),
        ]
        print(f"  Running: docker build -t {image_name} (FROM base, make only)...")
        try:
            ret = subprocess.call(cmd, timeout=3600)
            if ret == 0:
                print_success(f"Docker image '{image_name}' built successfully")
                return True
            print_warning(f"Pass build failed (exit {ret}), trying monolithic...")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            print_error("Pass build failed")
            return False

    # No monolithic fallback — if fast and pass builds both fail, the .md
    # file likely contains invalid RTL syntax.  Fail hard so the error is
    # visible and can be debugged from the Docker build log above.
    print_error("No suitable Dockerfile found or all build strategies failed")
    return False


# ─────────────────────────────────────────────────────────────────────
# Benchmark compilation
# ─────────────────────────────────────────────────────────────────────


def _compile_benchmark(cfg: "ToolConfig", image: str = MERGED_IMAGE) -> Tuple[bool, str, str]:
    """Compile the current benchmark with the specified custom GCC Docker image.

    Returns (success, elf_path, hex_path).
    """
    from arvis.config import BENCHMARKS

    bm = BENCHMARKS.get(cfg.benchmark_name)
    if not bm:
        print_error(f"Unknown benchmark: {cfg.benchmark_name}")
        return False, "", ""

    benchmark_dir = cfg.benchmark_dir
    output_dir = cfg.output_dir
    fused_elf_name = f"{cfg.benchmark_name}_fused.elf"
    fused_hex_name = f"{cfg.benchmark_name}_fused.hex"

    specializer_dir = _get_specializer_dir(cfg)

    plugin_flags = _get_plugin_flags(cfg)
    extra = " ".join(plugin_flags)
    compile_ok = _compile_generic(
        specializer_dir,
        benchmark_dir,
        fused_elf_name,
        fused_hex_name,
        bm,
        extra_cflags=extra,
        image=image,
    )

    if not compile_ok:
        return False, "", ""

    # Copy results to output directory
    os.makedirs(output_dir, exist_ok=True)
    fused_elf_src = os.path.join(benchmark_dir, fused_elf_name)
    fused_hex_src = os.path.join(benchmark_dir, fused_hex_name)
    fused_elf_dst = os.path.join(output_dir, fused_elf_name)
    fused_hex_dst = os.path.join(output_dir, fused_hex_name)

    if os.path.exists(fused_elf_src):
        shutil.copy2(fused_elf_src, fused_elf_dst)
    if os.path.exists(fused_hex_src):
        shutil.copy2(fused_hex_src, fused_hex_dst)

    # Save the fused .s to output dir for the hwloop step
    prog_name = cfg.benchmark_name.replace("embench_", "")
    fused_s_src = os.path.join(benchmark_dir, f"{prog_name}.s")
    fused_s_dst = os.path.join(output_dir, f"{prog_name}_fused.s")
    if os.path.exists(fused_s_src):
        shutil.copy2(fused_s_src, fused_s_dst)

    if not os.path.exists(fused_hex_dst):
        print_error(f"Fused hex not found: {fused_hex_dst}")
        return False, "", ""

    if os.path.exists(fused_elf_dst):
        print(f"  Fused ELF: {fused_elf_dst} ({os.path.getsize(fused_elf_dst):,} bytes)")
    print(f"  Fused HEX: {fused_hex_dst} ({os.path.getsize(fused_hex_dst):,} bytes)")
    return True, fused_elf_dst, fused_hex_dst


def _compile_generic(
    specializer_dir: str,
    benchmark_dir: str,
    elf_name: str,
    hex_name: str,
    bm_config: dict,
    extra_cflags: str = "",
    image: str = MERGED_IMAGE,
) -> bool:
    """Compile a generic benchmark with custom GCC using its Makefile."""
    abs_bm = os.path.join(os.path.abspath(specializer_dir), benchmark_dir)
    makefile = os.path.join(abs_bm, "Makefile")
    if not os.path.exists(makefile):
        print_error(f"No Makefile in {benchmark_dir}")
        return False

    # Step 0: Preserve original (baseline) hex/elf before make clean
    # overwrites them. The baseline sim needs the un-fused hex later.
    verilator_elf = bm_config.get("verilator_elf", "")
    hex_file = bm_config.get("hex", "")
    if hex_file:
        orig_hex = os.path.join(abs_bm, hex_file)
        saved_hex = orig_hex + ".baseline"
        if os.path.exists(orig_hex) and not os.path.exists(saved_hex):
            shutil.copy2(orig_hex, saved_hex)
            print(f"  📦 Preserved baseline hex: {saved_hex}")
    if verilator_elf:
        orig_elf = os.path.join(abs_bm, verilator_elf)
        saved_elf = orig_elf + ".baseline"
        if os.path.exists(orig_elf) and not os.path.exists(saved_elf):
            shutil.copy2(orig_elf, saved_elf)

    # Step 1: clean first so make rebuilds with the custom GCC
    clean_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{os.path.abspath(specializer_dir)}:/work",
        "-w",
        f"/work/{benchmark_dir}",
        image,
        "make",
        "-f",
        "Makefile",
        "clean",
    ]
    subprocess.run(clean_cmd, capture_output=True, timeout=60)

    # Step 2: compile with custom GCC
    make_vars = [
        "all",
        f"PREFIX={_RISCV_PREFIX}/bin/riscv32-unknown-elf-",
        f"CC={_RISCV_PREFIX}/bin/riscv32-unknown-elf-gcc",
        f"OBJCOPY={_RISCV_PREFIX}/bin/riscv32-unknown-elf-objcopy",
    ]

    # Check if Makefile uses EXTRA_CFLAGS (embench-style) or needs full CFLAGS override
    makefile_text = ""
    makefile_path = os.path.join(abs_bm, "Makefile")
    if os.path.exists(makefile_path):
        makefile_text = open(makefile_path).read()

    if "EXTRA_CFLAGS" in makefile_text:
        fused_flags = "-mcustom-fused" + (" " + extra_cflags if extra_cflags else "")
        make_vars.append(f"EXTRA_CFLAGS={fused_flags}")
    else:
        make_vars.append(
            "CFLAGS=-march=rv32imc_zicsr -mabi=ilp32 -O3 "
            "-mcustom-fused -fno-builtin -fno-common -static -nostdlib -ffreestanding"
            + (" " + extra_cflags if extra_cflags else "")
        )

    # Add extra linker flags if specified (e.g., -lgcc for soft-float)
    extra_ldflags = bm_config.get("extra_ldflags", "")
    if extra_ldflags:
        make_vars.append(f"LDFLAGS={extra_ldflags}")

    compile_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{os.path.abspath(specializer_dir)}:/work",
        "-w",
        f"/work/{benchmark_dir}",
        image,
        "make",
        "-f",
        "Makefile",
        *make_vars,
    ]

    print(f"  Compiling {benchmark_dir} with custom GCC...")
    try:
        ret = subprocess.call(compile_cmd, timeout=300, cwd=abs_bm)
        if ret != 0:
            print_warning("make failed")
            return False
    except subprocess.TimeoutExpired:
        print_error("Compilation timed out")
        return False

    verilator_elf = bm_config.get("verilator_elf", "")
    hex_file = bm_config.get("hex", "")

    if verilator_elf and os.path.exists(os.path.join(abs_bm, verilator_elf)):
        shutil.copy2(os.path.join(abs_bm, verilator_elf), os.path.join(abs_bm, elf_name))
    if hex_file and os.path.exists(os.path.join(abs_bm, hex_file)):
        shutil.copy2(os.path.join(abs_bm, hex_file), os.path.join(abs_bm, hex_name))

    if os.path.exists(os.path.join(abs_bm, elf_name)):
        print_success("Benchmark compiled with custom fused GCC")
        return True

    print_error(f"Expected output not found: {elf_name}")
    return False


# ─────────────────────────────────────────────────────────────────────
# Binary inspection
# ─────────────────────────────────────────────────────────────────────


def _inspect_binary_with_md(
    cfg: "ToolConfig",
    elf_path: str,
    md_path: str,
    image: str = MERGED_IMAGE,
) -> List[UsedCustomInstruction]:
    """Inspect compiled ELF for custom instructions using a specific .md file.

    Uses objdump (via Docker or local) to disassemble the binary and
    scans for instruction encodings that match CUSTOM opcode spaces.
    The md_path is used to build the encoding→pattern_name mapping.
    """
    if not os.path.exists(elf_path):
        print_error(f"ELF not found: {elf_path}")
        return []

    specializer_dir = _get_specializer_dir(cfg)
    rel_elf = os.path.relpath(elf_path, specializer_dir)

    objdump_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{os.path.abspath(specializer_dir)}:/work",
        "-w",
        "/work",
        image,
        "/opt/riscv/bin/riscv32-unknown-elf-objdump",
        "-d",
        rel_elf,
    ]

    try:
        result = subprocess.run(objdump_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            if cfg.riscv_objdump:
                result = subprocess.run(
                    [cfg.riscv_objdump, "-d", elf_path],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            if result.returncode != 0:
                print_error(f"objdump failed: {result.stderr[-200:]}")
                return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print_error("objdump failed or timed out")
        return []

    return _parse_objdump_for_custom(result.stdout, md_path)


def _parse_objdump_for_custom(objdump_output: str, md_path: str) -> List[UsedCustomInstruction]:
    """Parse objdump output to find custom instruction encodings."""
    encoding_map = _build_encoding_map_from_file(md_path)

    insn_pattern = re.compile(r"^\s*[0-9a-f]+:\s+([0-9a-f]{8})\s+")

    # raw_insns is (opcode, funct3, funct7, funct2, rs3); rs3 = instr[31:27]
    raw_insns: List[Tuple[int, int, int, int, int]] = []
    for line in objdump_output.splitlines():
        m = insn_pattern.match(line)
        if not m:
            continue
        raw_hex = int(m.group(1), 16)
        opcode = raw_hex & 0x7F
        if opcode not in CUSTOM_OPCODES:
            continue
        funct3 = (raw_hex >> 12) & 0x07
        funct7 = (raw_hex >> 25) & 0x7F
        funct2 = (raw_hex >> 25) & 0x03
        rs3 = (raw_hex >> 27) & 0x1F  # fused_imm_i[4:0] for R4 parametric
        raw_insns.append((opcode, funct3, funct7, funct2, rs3))

    counts: Dict[Tuple[int, int, int, bool], int] = defaultdict(int)
    imm_sets: Dict[Tuple[int, int, int, bool], Set[int]] = defaultdict(set)
    for opcode, funct3, funct7, funct2, rs3 in raw_insns:
        r4_key = (opcode, funct3, funct2)
        r_key = (opcode, funct3, funct7)

        # Check encoding map to determine R4 vs R-type
        is_r4 = r4_key in encoding_map and encoding_map[r4_key].get("is_r4", False)

        if is_r4:
            dedup_key = (opcode, funct3, funct2, True)
        elif r_key in encoding_map:
            dedup_key = (opcode, funct3, funct7, False)
        else:
            # Unknown — guess R4 if funct2 key exists in map, else R-type
            dedup_key = (opcode, funct3, funct2, True)
        counts[dedup_key] += 1
        # Only meaningful for R4 parametric patterns; for R-type the rs3
        # field is a real register index and shouldn't be aggregated.
        if dedup_key[3]:  # is_r4
            imm_sets[dedup_key].add(rs3)

    results = []
    for (opcode, funct3, func_val, is_r4), count in sorted(counts.items(), key=lambda x: -x[1]):
        enc_key = (opcode, funct3, func_val)
        pattern_info = encoding_map.get(enc_key, {})
        is_param = "param" in pattern_info.get("name", "").lower()
        results.append(
            UsedCustomInstruction(
                opcode=opcode,
                funct3=funct3,
                funct7=func_val,
                count=count,
                is_r4=is_r4,
                pattern_name=pattern_info.get("name", ""),
                mnemonics=tuple(pattern_info.get("mnemonics", ())),
                # Restrict to parametric patterns: for fixed-imm R4 patterns
                # rs3 is a real register and shouldn't appear here.
                used_imm_values=set(imm_sets[(opcode, funct3, func_val, is_r4)]) if (is_r4 and is_param) else set(),
            )
        )

    return results


# ─────────────────────────────────────────────────────────────────────
# Encoding map builders
# ─────────────────────────────────────────────────────────────────────


def _build_encoding_map(cfg: "ToolConfig") -> Dict[Tuple[int, int, int], dict]:
    """Build encoding map from the output directory's custom-fused.md."""
    md_path = str(Path(cfg.output_dir) / "custom-fused.md")
    return _build_encoding_map_from_file(md_path)


def _build_encoding_map_from_file(
    md_path: str,
) -> Dict[Tuple[int, int, int], dict]:
    """Build a mapping from instruction encoding to pattern info.

    Parses the .md file to extract the encoding from each .insn directive
    and maps it to the pattern name/mnemonics.
    """
    if not os.path.exists(md_path):
        return {}

    encoding_map: Dict[Tuple[int, int, int], dict] = {}
    content = Path(md_path).read_text()

    r4_pattern = re.compile(r"\.insn\s+r4\s+0x([0-9a-f]+),\s*(\d+),\s*(\d+)")
    r_pattern = re.compile(r"\.insn\s+r\s+0x([0-9a-f]+),\s*(\d+),\s*(?:0x)?([0-9a-f]+)")
    current_comment = ""
    for line in content.splitlines():
        if line.startswith(";;"):
            current_comment = line[2:].strip()
            continue

        m_r4 = r4_pattern.search(line)
        if m_r4:
            opcode = int(m_r4.group(1), 16)
            funct3 = int(m_r4.group(2))
            funct2 = int(m_r4.group(3))
            key = (opcode, funct3, funct2)
            mnemonics = _parse_mnemonics_from_comment(current_comment)
            hc_imm = _parse_hardcoded_imm_from_comment(current_comment)
            encoding_map[key] = {
                "name": current_comment[:40],
                "mnemonics": mnemonics,
                "is_r4": True,
                "is_reverse": " rev " in current_comment or current_comment.endswith(" rev"),
                "hardcoded_imm": hc_imm,
            }
            continue

        m_r = r_pattern.search(line)
        if m_r:
            opcode = int(m_r.group(1), 16)
            funct3 = int(m_r.group(2))
            funct7 = int(m_r.group(3), 16)
            key = (opcode, funct3, funct7)
            mnemonics = _parse_mnemonics_from_comment(current_comment)
            hc_imm = _parse_hardcoded_imm_from_comment(current_comment)
            encoding_map[key] = {
                "name": current_comment[:40],
                "mnemonics": mnemonics,
                "is_r4": False,
                "hardcoded_imm": hc_imm,
            }

    return encoding_map


def _parse_mnemonics_from_comment(comment: str) -> List[str]:
    """Extract mnemonic names from a pattern comment.

    Handles formats:
      'sra -> mul (chain info) impact=N'     -> ['sra', 'mul']
      'add+sub RR+RR slot=1'                 -> ['add', 'sub']
      'addi+sub RI+RR slot=r3'              -> ['addi', 'sub']
      'add+sra hardcoded(pos1=16) freq=167'  -> ['add', 'sra']
    """
    parts = re.split(r"\s+(?:RR|RI|IR|II|slot|impact|freq|hardcoded|\()", comment)[0].strip()
    mnemonics = [m.strip() for m in re.split(r"\s*(?:->|→|\+)\s*", parts)]
    result = []
    for m in mnemonics:
        if not m:
            continue
        m = m.split()[0]
        if m and not m.startswith("impact") and not m.startswith("freq"):
            result.append(m)
    return result


def _parse_hardcoded_imm_from_comment(comment: str) -> Dict[int, int]:
    """Extract hardcoded immediate values from a pattern comment.

    Parses format: 'add+sra hardcoded(pos0=5, pos1=16) freq=167'
    Returns: {0: 5, 1: 16}.
    """
    m = re.search(r"hardcoded\(([^)]+)\)", comment)
    if not m:
        return {}
    result: Dict[int, int] = {}
    for entry in m.group(1).split(","):
        entry = entry.strip()
        pm = re.match(r"pos(\d+)\s*=\s*(-?\d+)", entry)
        if pm:
            result[int(pm.group(1))] = int(pm.group(2))
    return result
