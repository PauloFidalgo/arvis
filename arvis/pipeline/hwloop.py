"""
Hardware Loop pipeline phase.

Flow:
  1. Compile the benchmark's main translation unit three ways:
     a) LTO fused (custom-riscv-gcc-merged) — per-run merged patterns, no hwloop
     b) -mhwloop fused (riscv-gcc-merged-hwloop) — per-run merged patterns + hwloop
     c) LTO plain (riscv-gcc-hwloop) — no fused patterns, for hwloop-only hex
  2. Merge best-of-both for fused+hwloop assembly
  3. Patch: insert CUSTOM_3 hwloop setup instructions (both variants)
  4. Reassemble patched .s → ELF → HEX (both variants)
  5. Update ctx paths and changeset.hw_loop_count
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.config import ToolConfig
    from arvis.pipeline.context import PipelineContext
    from arvis.pipeline.rtl_changeset import RTLChangeSet

# Per-run merged image (built by gcc_compile.py with the run-specific custom-fused.md)
FUSED_IMAGE = "custom-riscv-gcc-merged"
# Per-run merged+hwloop image (built here, FROM FUSED_IMAGE + hwloop patches)
HWLOOP_IMAGE = "riscv-gcc-merged-hwloop"
# One-time plain-hwloop image (no fused patterns, for Step 4 hex)
HWLOOP_ONLY_IMAGE = "riscv-gcc-hwloop"

_HWLOOP_DOCKER_DIR = Path(__file__).resolve().parent.parent / "tools" / "hwloop-docker"


def _docker_image_exists(name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", name],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_hwloop_only_image() -> bool:
    """Build riscv-gcc-hwloop (plain hwloop, no fused) if not already present.

    This is a one-time build: it doesn't depend on per-run fusion patterns.
    FROM riscv-gcc-prebuilt → applies hwloop GCC patches → ~5 min.
    """
    from arvis.cli import print_info, print_success, print_warning

    if _docker_image_exists(HWLOOP_ONLY_IMAGE):
        return True

    dockerfile = _HWLOOP_DOCKER_DIR / "Dockerfile.hwloop"
    if not dockerfile.exists():
        print_warning(f"Dockerfile.hwloop not found: {dockerfile}")
        return False

    # Need riscv-gcc-prebuilt as base — delegate to gcc_compile helpers
    from arvis.pipeline.gcc_compile import _docker_image_exists as _gcc_img
    from arvis.pipeline.gcc_compile import _ensure_prebuilt_image

    specializer_dir = Path(__file__).resolve().parent.parent
    if not _gcc_img("riscv-gcc-prebuilt") and not _ensure_prebuilt_image(specializer_dir):
        print_warning("riscv-gcc-prebuilt unavailable — cannot build hwloop-only image")
        return False

    print_info(f"Building '{HWLOOP_ONLY_IMAGE}' (~5 min, one-time)...")
    cmd = [
        "docker",
        "build",
        "-t",
        HWLOOP_ONLY_IMAGE,
        "-f",
        str(dockerfile),
        str(_HWLOOP_DOCKER_DIR),
    ]
    try:
        ret = subprocess.call(cmd, timeout=1800)
        if ret == 0:
            print_success(f"'{HWLOOP_ONLY_IMAGE}' built successfully")
            return True
        print_warning(f"'{HWLOOP_ONLY_IMAGE}' build failed (exit {ret})")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print_warning(f"'{HWLOOP_ONLY_IMAGE}' build timed out or docker not found")
    return False


def _build_merged_hwloop_image() -> bool:
    """Build riscv-gcc-merged-hwloop FROM custom-riscv-gcc-merged + hwloop patches.

    Must be called AFTER gcc_compile.run() has built custom-riscv-gcc-merged.
    Rebuilt every pipeline run because the merged patterns change per run.

    FROM custom-riscv-gcc-merged → applies hwloop GCC patches (rebuilds cc1 from
    stage1 with the merged custom-fused.md already in place) → ~5 min.
    The fused_pass.so GIMPLE plugin is inherited from the merged image.
    """
    from arvis.cli import print_info, print_success, print_warning

    if not _docker_image_exists(FUSED_IMAGE):
        print_warning(f"'{FUSED_IMAGE}' not found — run fusion phase first")
        return False

    dockerfile = _HWLOOP_DOCKER_DIR / "Dockerfile.merged-hwloop"
    if not dockerfile.exists():
        print_warning(f"Dockerfile.merged-hwloop not found: {dockerfile}")
        return False

    print_info(f"Building '{HWLOOP_IMAGE}' FROM {FUSED_IMAGE} (~5 min)...")
    cmd = ["docker", "build", "-t", HWLOOP_IMAGE, "-f", str(dockerfile), str(_HWLOOP_DOCKER_DIR)]
    try:
        ret = subprocess.call(cmd, timeout=1800)
        if ret == 0:
            print_success(f"'{HWLOOP_IMAGE}' built successfully")
            return True
        print_warning(f"'{HWLOOP_IMAGE}' build failed (exit {ret})")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print_warning(f"'{HWLOOP_IMAGE}' build timed out or docker not found")
    return False


def _riscv_cmd(
    specializer_dir: Path,
    workdir: Path,
    image: str,
    tool: str,
    args: list[str],
) -> list[str]:
    """Build command list: Docker."""
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{specializer_dir}:/work",
        "-w",
        f"/work/{workdir.relative_to(specializer_dir)}",
        image,
        f"/opt/riscv/bin/{tool}",
        *args,
    ]


def _test_merged_functions(
    cand,
    bm_dir: Path,
    sim_bin: str,
    baseline_cycles: int,
    hw_loop: int,
    encoding=None,
    image: str | None = None,
    specializer_dir: Path | None = None,
) -> Path | None:
    """Test each merged function: does reverting it improve cycles?
    Returns path to optimized source if any functions were reverted, else None."""
    import re as _re

    from arvis.cli import print_info
    from arvis.codegen.hwloop.merge_asm import revert_function

    replaced_fns = getattr(cand, "_replaced_fns", None) or []
    standard_src = getattr(cand, "_standard_src", None)
    fused_src = getattr(cand, "fused_src", None)

    if not replaced_fns or not standard_src or not fused_src:
        return None

    print_info(f"  Testing {len(replaced_fns)} merged functions for benefit")

    reverted = []
    current_src = fused_src
    current_cycles = baseline_cycles

    for fn in replaced_fns:
        # Build version with this function reverted
        test_src = str(bm_dir / f"_merge_test_{fn}.s")
        revert_function(current_src, standard_src, fn, test_src)

        # Patch, assemble, run
        test_patched_name = f"_merge_test_{fn}_patched.s"
        patched, _ = _patch_asm(Path(test_src), test_patched_name, hw_loop, bm_dir, encoding=encoding)
        if not patched:
            continue

        tmp_elf = bm_dir / "_merge_test.elf"
        tmp_hex = bm_dir / "_merge_test.hex"
        if image and specializer_dir:
            ok = _assemble_patched(specializer_dir, bm_dir, patched, "_merge_test.elf", "_merge_test.hex", image=image)
        else:
            ok = (
                subprocess.call(
                    [
                        "riscv32-unknown-elf-gcc",
                        "-march=rv32imc_zicsr",
                        "-mabi=ilp32",
                        "-static",
                        "-nostdlib",
                        "-nostartfiles",
                        "-T",
                        "link_cv32e40p.ld",
                        "-Wl,--gc-sections",
                        "-o",
                        str(tmp_elf),
                        "crt0_cv32e40p.S",
                        str(patched),
                    ],
                    timeout=30,
                    cwd=str(bm_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                == 0
            )
            if ok:
                subprocess.call(
                    ["riscv32-unknown-elf-objcopy", "-O", "verilog", str(tmp_elf), str(tmp_hex)],
                    timeout=10,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

        if not ok or not tmp_hex.exists():
            continue

        r = subprocess.run(
            [sim_bin, f"+firmware={tmp_hex}", "+maxcycles=20000000"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        cycles = None
        for l in r.stdout.splitlines():
            m = _re.search(r"after (\d+) cycles", l)
            if m and "SUCCESS" in l:
                cycles = int(m.group(1))
                break

        if cycles is not None and cycles < current_cycles:
            print_info(f"    Revert {fn}: {current_cycles:,} → {cycles:,} (better, reverting)")
            current_src = test_src
            current_cycles = cycles
            reverted.append(fn)
        elif cycles is not None:
            print_info(f"    Keep {fn}: revert would be {cycles:,} vs {current_cycles:,}")
        else:
            print_info(f"    {fn}: revert FAIL/TIMEOUT — keeping merge")

    # Cleanup
    for fn in replaced_fns:
        for f in [f"_merge_test_{fn}.s", f"_merge_test_{fn}_patched.s"]:
            (bm_dir / f).unlink(missing_ok=True)
    for f in ["_merge_test.elf", "_merge_test.hex"]:
        (bm_dir / f).unlink(missing_ok=True)

    if reverted:
        print_info(f"  Reverted {len(reverted)}/{len(replaced_fns)} merged functions")
        return Path(current_src)
    return None


# Cache prevalidation results: (source_path, loop_key) → cycles or None
_preval_cache: dict = {}


def _prevalidate_loops(
    src_asm: Path,
    hw_loop: int,
    bm_dir: Path,
    sim_bin: str,
    link_script: str,
    crt0: str,
    encoding=None,
    image: str | None = None,
    specializer_dir: Path | None = None,
    extra_ldflags: list[str] | None = None,
    baseline_src: Path | None = None,
    fifo_depth: int | None = None,
) -> set:
    """Test each eligible loop individually. Return set of keys that PASS.
    baseline_src: if provided, use this for the no-loops baseline instead of src_asm.
    fifo_depth:   when set, loops with body smaller than fifo_depth*4 bytes
                  are rejected as ineligible (see AsmLoopDetector). Pass
                  None to skip the check (legacy callers)."""
    import re as _re

    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.cli import print_info
    from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator

    # Auto-detect extra_ldflags from benchmark config if not provided
    if extra_ldflags is None:
        from arvis.config import BENCHMARKS

        bm_name = bm_dir.name
        bm_cfg = BENCHMARKS.get(bm_name, {})
        _eld = bm_cfg.get("extra_ldflags", "")
        extra_ldflags = _eld.split() if _eld else None

    asm_text = src_asm.read_text()
    baseline_text = baseline_src.read_text() if baseline_src and baseline_src.exists() else asm_text
    det0 = AsmLoopDetector(str(src_asm), fifo_depth=fifo_depth)
    det0.find_all_loops()
    eligible = [l for l in det0.all_loops if l.hw_eligible]
    if not eligible:
        return set()

    keys = [(l.start_label, l.start_line, l.back_branch_insn) for l in eligible]
    valid = set()

    # Run baseline (no hwloop) to show reference cycles for this source
    tmp_s = bm_dir / "_preval.s"
    tmp_elf = bm_dir / "_preval.elf"
    tmp_hex = bm_dir / "_preval.hex"
    tmp_s.write_text(baseline_text)  # unpatched baseline source (pre-merge if available)
    _bl_ok = False
    if image and specializer_dir:
        _bl_ok = _assemble_patched(specializer_dir, bm_dir, tmp_s, "_preval.elf", "_preval.hex", image=image)
    else:
        ret = subprocess.call(
            [
                "riscv32-unknown-elf-gcc",
                "-march=rv32imc_zicsr",
                "-mabi=ilp32",
                "-static",
                "-nostdlib",
                "-nostartfiles",
                "-T",
                link_script,
                "-Wl,--gc-sections",
                "-o",
                str(tmp_elf),
                crt0,
                str(tmp_s),
                *(extra_ldflags or []),
            ],
            timeout=30,
            cwd=str(bm_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ret == 0:
            subprocess.call(
                ["riscv32-unknown-elf-objcopy", "-O", "verilog", str(tmp_elf), str(tmp_hex)],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _bl_ok = True
    if _bl_ok and tmp_hex.exists():
        _bl_r = subprocess.run(
            [sim_bin, f"+firmware={tmp_hex}", "+maxcycles=20000000"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        for _l in _bl_r.stdout.splitlines():
            _m = _re.search(r"after (\d+) cycles", _l)
            if _m and "SUCCESS" in _l:
                print_info(f"    Baseline (no loops): {int(_m.group(1)):,} cycles")
                break

    for i, (key, loop) in enumerate(zip(keys, eligible)):
        # Check cache
        cache_key = (str(src_asm), key)
        if cache_key in _preval_cache:
            cached = _preval_cache[cache_key]
            if cached is not None:
                print_info(f"    {loop.start_label}: PASS ({cached:,} cycles) [cached]")
                valid.add(key)
            else:
                print_info(f"    {loop.start_label}: FAIL/TIMEOUT — excluded [cached]")
            continue

        det = AsmLoopDetector(str(src_asm))
        det.find_all_loops()
        for l in det.all_loops:
            k = (l.start_label, l.start_line, l.back_branch_insn)
            l.hw_eligible = k == key
        gen = HWLoopGenerator(det, hw_loop=hw_loop, encoding=encoding)
        gen.generate()
        patched = AsmPatcher(gen, asm_text).patch()

        tmp_s = bm_dir / "_preval.s"
        tmp_elf = bm_dir / "_preval.elf"
        tmp_hex = bm_dir / "_preval.hex"
        tmp_s.write_text(patched)

        if image and specializer_dir:
            ok = _assemble_patched(specializer_dir, bm_dir, tmp_s, "_preval.elf", "_preval.hex", image=image)
            if not ok:
                print_info(f"    {loop.start_label}: ASM_ERR — excluded")
                continue
        else:
            ret = subprocess.call(
                [
                    "riscv32-unknown-elf-gcc",
                    "-march=rv32imc_zicsr",
                    "-mabi=ilp32",
                    "-static",
                    "-nostdlib",
                    "-nostartfiles",
                    "-T",
                    link_script,
                    "-Wl,--gc-sections",
                    "-o",
                    str(tmp_elf),
                    crt0,
                    str(tmp_s),
                    *(extra_ldflags or []),
                ],
                timeout=30,
                cwd=str(bm_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ret != 0:
                print_info(f"    {loop.start_label}: ASM_ERR — excluded")
                continue
            subprocess.call(
                ["riscv32-unknown-elf-objcopy", "-O", "verilog", str(tmp_elf), str(tmp_hex)],
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        ret = subprocess.run(
            [sim_bin, f"+firmware={tmp_hex}", "+maxcycles=20000000"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        passed = False
        for l in ret.stdout.splitlines():
            if "SUCCESS" in l:
                m = _re.search(r"after (\d+) cycles", l)
                _cycles = int(m.group(1)) if m else 0
                _preval_cache[cache_key] = _cycles
                print_info(f"    {loop.start_label}: PASS ({_cycles:,} cycles)")
                passed = True
                break
        if not passed:
            _preval_cache[cache_key] = None
            print_info(f"    {loop.start_label}: FAIL/TIMEOUT — excluded")
            continue
        valid.add(key)

    for f in ["_preval.s", "_preval.elf", "_preval.hex"]:
        (bm_dir / f).unlink(missing_ok=True)

    print_info(f"  Pre-validation: {len(valid)}/{len(keys)} loops passed")
    return valid


def _patch_asm(
    src_asm: Path,
    out_name: str,
    hw_loop: int,
    bm_dir: Path,
    encoding=None,
    valid_loops: set | None = None,
    fifo_depth: int | None = None,
):
    """Analyze loops and patch a .s file. Returns (patched_path, stats) or (None, None).
    fifo_depth: when set, loops with body smaller than fifo_depth*4 bytes
                are rejected as ineligible (see AsmLoopDetector)."""
    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator

    det = AsmLoopDetector(str(src_asm), fifo_depth=fifo_depth)
    det.find_all_loops()

    # Filter to only pre-validated loops
    if valid_loops is not None:
        for l in det.all_loops:
            k = (l.start_label, l.start_line, l.back_branch_insn)
            if k not in valid_loops:
                l.hw_eligible = False

    eligible = sum(1 for l in det.all_loops if l.hw_eligible)
    if eligible == 0:
        return None, None

    gen = HWLoopGenerator(det, hw_loop=hw_loop, encoding=encoding)
    gen.generate()
    patcher = AsmPatcher(gen, src_asm.read_text())
    patched = patcher.patch()
    stats = patcher.get_stats()

    patched_path = bm_dir / out_name
    patched_path.write_text(patched)
    return patched_path, stats


def _select_beneficial_loops(
    src_asm: Path,
    hw_loop: int,
    bm_dir: Path,
    sim_bin: str,
    link_script: str,
    crt0: str,
    out_name: str,
    image: str | None = None,
    extra_ldflags: list[str] | None = None,
    specializer_dir: Path | None = None,
    baseline_cycles: int | None = None,
    encoding=None,
    valid_loops: set | None = None,
) -> tuple:
    """GA-based loop selection + greedy refinement.

    Phase 1: Genetic algorithm explores on/off combinations (~300 evals)
    Phase 2: Re-enable disabled loops one at a time
    Phase 3: Greedy disable of ON loops one at a time

    Returns (patched_path, stats, n_beneficial, n_total) or (None, None, 0, 0).
    """
    import random
    import re as _re

    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.cli import print_info
    from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator

    # Auto-detect extra_ldflags from benchmark config if not provided
    if extra_ldflags is None:
        from arvis.config import BENCHMARKS

        bm_name = bm_dir.name
        bm_cfg = BENCHMARKS.get(bm_name, {})
        _eld = bm_cfg.get("extra_ldflags", "")
        extra_ldflags = _eld.split() if _eld else None

    asm_text = src_asm.read_text()

    det0 = AsmLoopDetector(str(src_asm))
    det0.find_all_loops()

    # Filter to only pre-validated loops
    if valid_loops is not None:
        for l in det0.all_loops:
            k = (l.start_label, l.start_line, l.back_branch_insn)
            if k not in valid_loops:
                l.hw_eligible = False

    eligible = [l for l in det0.all_loops if l.hw_eligible]
    if not eligible:
        return None, None, 0, 0

    keys = [(l.start_label, l.start_line, l.back_branch_insn) for l in eligible]
    N = len(keys)

    def _build_and_run(asm: str) -> int | None:
        tmp_s = bm_dir / "_loop_select.s"
        tmp_elf = bm_dir / "_loop_select.elf"
        tmp_hex = bm_dir / "_loop_select.hex"
        tmp_s.write_text(asm)
        if image and specializer_dir:
            ok = _assemble_patched(specializer_dir, bm_dir, tmp_s, "_loop_select.elf", "_loop_select.hex", image=image)
            if not ok:
                return None
        else:
            ret = subprocess.call(
                [
                    "riscv32-unknown-elf-gcc",
                    "-march=rv32imc_zicsr",
                    "-mabi=ilp32",
                    "-static",
                    "-nostdlib",
                    "-nostartfiles",
                    "-T",
                    link_script,
                    "-Wl,--gc-sections",
                    "-o",
                    str(tmp_elf),
                    crt0,
                    str(tmp_s),
                    *(extra_ldflags or []),
                ],
                timeout=30,
                cwd=str(bm_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ret != 0:
                return None
            subprocess.call(
                ["riscv32-unknown-elf-objcopy", "-O", "verilog", str(tmp_elf), str(tmp_hex)],
                timeout=10,
                cwd=str(bm_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        hex_path = str(bm_dir / "_loop_select.hex")
        ret = subprocess.run(
            [sim_bin, f"+firmware={hex_path}", "+maxcycles=20000000"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        for l in ret.stdout.splitlines():
            m = _re.search(r"after (\d+) cycles", l)
            if m and "SUCCESS" in l:
                return int(m.group(1))
        return None

    cache = {}

    def _eval_mask(mask):
        key = tuple(mask)
        if key in cache:
            return cache[key]
        det = AsmLoopDetector(str(src_asm))
        det.find_all_loops()
        enabled = set(keys[i] for i, b in enumerate(mask) if b)
        for l in det.all_loops:
            k = (l.start_label, l.start_line, l.back_branch_insn)
            l.hw_eligible = k in enabled
        gen = HWLoopGenerator(det, hw_loop=hw_loop, encoding=encoding)
        gen.generate()
        patcher = AsmPatcher(gen, asm_text)
        c = _build_and_run(patcher.patch())
        cache[key] = c
        return c

    # Baseline
    if baseline_cycles is not None:
        baseline = baseline_cycles
    else:
        baseline = _eval_mask([0] * N)
    if baseline is None:
        print_info("  Baseline eval FAILED — cannot run GA")
        return None, None, 0, 0

    all_on = _eval_mask([1] * N)
    if all_on is None:
        print_info("  All-loops eval FAILED — running per-loop pre-validation")
        # Pre-validate each loop individually to find survivors
        valid_i = []
        for i in range(N):
            mask = [0] * N
            mask[i] = 1
            c = _eval_mask(mask)
            lbl = eligible[i].start_label
            if c is not None:
                valid_i.append(i)
                print_info(f"    {lbl}: PASS ({c:,} cycles)")
            else:
                print_info(f"    {lbl}: FAIL/TIMEOUT — excluded")
        if not valid_i:
            print_info("  No loops survived pre-validation")
            return None, None, 0, N
        # Rebuild with survivors only
        keys = [keys[i] for i in valid_i]
        eligible = [eligible[i] for i in valid_i]
        N = len(keys)
        cache.clear()
        print_info(f"  {N} loops survived pre-validation")
        # Re-evaluate baseline and all-on with filtered loops
        baseline = _eval_mask([0] * N)
        if baseline is None:
            return None, None, 0, 0
        all_on = _eval_mask([1] * N)
        if all_on is None:
            print_info("  All-loops still FAIL after pre-validation")
            return None, None, 0, N
    if all_on >= baseline:
        print_info(f"  All-loops ({all_on:,}) >= baseline ({baseline:,}) — trying individual loops")
        # All-on is worse, but a subset might be better. Fall through to optimizer.

    print_info(f"  Baseline: {baseline:,} | All ON: {all_on:,} (saves {baseline - all_on:,})")

    print_info(f"  Baseline: {baseline:,} | All ON: {all_on:,} (saves {baseline - all_on:,})")

    # ── Optimizer: 3 tiers based on number of valid loops ──
    if N == 1:
        # Tier 1: single loop — just use it
        print_info("  Single loop — using directly")
        best_mask = [1]
        best_cost = all_on
    elif N <= 6:
        # Tier 2: exhaustive search (2^N combinations, max 64)
        print_info(f"  Exhaustive search ({2**N} combinations for {N} loops)")
        best_mask = [1] * N
        best_cost = all_on
        for bits in range(1, 2**N):
            mask = [(bits >> j) & 1 for j in range(N)]
            c = _eval_mask(mask)
            if c is not None and c < best_cost:
                best_cost = c
                best_mask = mask[:]
        print_info(f"  Exhaustive best: {best_cost:,} ({sum(best_mask)}/{N} loops, {len(cache)} evals)")
    else:
        # Tier 3: GA + greedy refinement
        print_info(f"  GA search ({N} loops)")
        POP = min(16, N)
        GENS = 10
        ELITE = 4
        MUT = 0.1

        pop = [[1] * N]
        for _ in range(POP - 1):
            pop.append([1 if random.random() < random.uniform(0.7, 0.95) else 0 for _ in range(N)])

        best_mask = [1] * N
        best_cost = all_on

        for gen_i in range(GENS):
            scored = []
            for m in pop:
                c = _eval_mask(m)
                scored.append((c if c is not None else baseline * 2, m))
            scored.sort(key=lambda x: x[0])
            if scored[0][0] < best_cost:
                best_cost = scored[0][0]
                best_mask = scored[0][1][:]
            elites = [m[:] for _, m in scored[:ELITE]]
            new_pop = elites[:]
            while len(new_pop) < POP:
                p1, p2 = random.sample(elites, 2)
                cx = random.randint(1, N - 1)
                child = p1[:cx] + p2[cx:]
                for j in range(N):
                    if random.random() < MUT:
                        child[j] = 1 - child[j]
                new_pop.append(child)
            pop = new_pop
            n_on = sum(best_mask)
            print_info(f"    Gen {gen_i + 1}/{GENS}: best={best_cost:,} ({n_on}/{N} loops ON, {len(cache)} evals)")

        print_info(f"  ✓ GA result: {best_cost:,} ({sum(best_mask)}/{N} loops, {len(cache)} evals)")

        # Phase 2: Re-enable disabled loops
        disabled = [i for i in range(N) if best_mask[i] == 0]
        re_enabled = 0
        if disabled:
            print_info(f"  ── Phase 2: Re-enable check ({len(disabled)} disabled loops) ──")
            for i in disabled:
                best_mask[i] = 1
                c = _eval_mask(best_mask)
                if c is not None and c < best_cost:
                    best_cost = c
                    re_enabled += 1
                    print_info(f"    + {eligible[i].start_label}: re-enable saves {best_cost - c:,}")
                else:
                    best_mask[i] = 0
            print_info(
                f"  ✓ Re-enabled {re_enabled} loops: {best_cost:,} ({sum(best_mask)}/{N} loops, {len(cache)} evals)"
            )

        # Phase 3: Greedy disable
        print_info("  ── Phase 3: Greedy disable ──")
        improved = True
        rounds = 0
        while improved:
            improved = False
            rounds += 1
            for i in range(N):
                if best_mask[i] == 0:
                    continue
                best_mask[i] = 0
                c = _eval_mask(best_mask)
                if c is not None and c < best_cost:
                    saved_by = best_cost - c
                    best_cost = c
                    improved = True
                    print_info(f"    - {eligible[i].start_label}: disable saves {saved_by:,}")
                else:
                    best_mask[i] = 1
        print_info(f"  ✓ Greedy done ({rounds} rounds): {best_cost:,} ({sum(best_mask)}/{N} loops, {len(cache)} evals)")

    saved = baseline - best_cost
    print_info(f"  ✓ Total savings: {saved:,} cycles ({saved / baseline * 100:.1f}%)")

    # Build final patched assembly with the best mask
    det_final = AsmLoopDetector(str(src_asm))
    det_final.find_all_loops()
    enabled_keys = set(keys[i] for i in range(N) if best_mask[i])
    for l in det_final.all_loops:
        k = (l.start_label, l.start_line, l.back_branch_insn)
        l.hw_eligible = k in enabled_keys
    gen_final = HWLoopGenerator(det_final, hw_loop=hw_loop, encoding=encoding)
    gen_final.generate()
    patcher_final = AsmPatcher(gen_final, asm_text)
    patched_text = patcher_final.patch()
    stats = patcher_final.get_stats()
    patched_path = bm_dir / out_name
    patched_path.write_text(patched_text)

    n_on = sum(best_mask)
    print_info(f"  Final: {n_on}/{N} loops, {saved:,} cycles saved ({saved / baseline * 100:.1f}%)")

    for f in ["_loop_select.s", "_loop_select.elf", "_loop_select.hex"]:
        p = bm_dir / f
        if p.exists():
            p.unlink()

    return patched_path, stats, n_on, N


def _assemble_patched(
    specializer_dir: Path,
    bm_dir: Path,
    patched_s: Path,
    elf_name: str,
    hex_name: str,
    image: str = FUSED_IMAGE,
    extra_ldflags: list[str] | None = None,
) -> bool:
    """Assemble patched .s → ELF → HEX using Docker."""
    from arvis.config import BENCHMARKS

    # Auto-detect extra_ldflags from benchmark config if not provided
    if extra_ldflags is None:
        bm_name = bm_dir.name
        bm_cfg = BENCHMARKS.get(bm_name, {})
        _eld = bm_cfg.get("extra_ldflags", "")
        extra_ldflags = _eld.split() if _eld else []

    asm_cmd = _riscv_cmd(
        specializer_dir,
        bm_dir,
        image,
        "riscv32-unknown-elf-gcc",
        [
            "-march=rv32imc_zicsr",
            "-mabi=ilp32",
            "-static",
            "-nostdlib",
            "-nostartfiles",
            "-T",
            "link_cv32e40p.ld",
            "-Wl,--gc-sections",
            "-o",
            elf_name,
            "crt0_cv32e40p.S",
            patched_s.name,
            *extra_ldflags,
        ],
    )
    try:
        ret = subprocess.run(asm_cmd, timeout=120, cwd=str(bm_dir), capture_output=True, text=True)
        if ret.returncode != 0:
            # Retry with custom-riscv-gcc-merged if lgcc missing
            if "lgcc" in ret.stderr and image != "custom-riscv-gcc-merged":
                asm_cmd_retry = _riscv_cmd(
                    specializer_dir,
                    bm_dir,
                    "custom-riscv-gcc-merged",
                    "riscv32-unknown-elf-gcc",
                    [
                        "-march=rv32imc_zicsr",
                        "-mabi=ilp32",
                        "-static",
                        "-nostdlib",
                        "-nostartfiles",
                        "-T",
                        "link_cv32e40p.ld",
                        "-Wl,--gc-sections",
                        "-o",
                        elf_name,
                        "crt0_cv32e40p.S",
                        patched_s.name,
                        *extra_ldflags,
                    ],
                )
                if (
                    subprocess.call(
                        asm_cmd_retry,
                        timeout=120,
                        cwd=str(bm_dir),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    != 0
                ):
                    return False
            else:
                return False
    except subprocess.TimeoutExpired:
        return False

    hex_cmd = _riscv_cmd(
        specializer_dir,
        bm_dir,
        image,
        "riscv32-unknown-elf-objcopy",
        ["-O", "verilog", elf_name, hex_name],
    )
    try:
        if (
            subprocess.call(
                hex_cmd,
                timeout=60,
                cwd=str(bm_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            != 0
        ):
            return False
    except subprocess.TimeoutExpired:
        return False

    return (bm_dir / elf_name).exists() and (bm_dir / hex_name).exists()


def run(
    cfg: "ToolConfig",
    ctx: "PipelineContext",
    changeset: "RTLChangeSet",
) -> None:
    """Execute HW Loop phase: dual-compile → merge → patch → reassemble.

    Produces TWO hex files:
      - fused+hwloop hex  → ctx.hwloop_hex_path  (for step 5: All)
      - hwloop-only hex   → ctx.hwloop_only_hex_path (for step 4: HWLoop+Pruned)
    """
    from arvis.cli import (
        print_section,
        print_step,
    )

    print_section("PHASE 4: HARDWARE LOOP ANALYSIS + PATCHING")

    specializer_dir = Path(__file__).resolve().parent.parent
    bm_dir = specializer_dir / cfg.benchmark_dir

    # ── Ensure Docker images ──
    # riscv-gcc-hwloop: one-time build (plain hwloop, for Step 4 plain hex)
    _ensure_hwloop_only_image()
    # riscv-gcc-merged-hwloop: per-run build (fused+hwloop, for Step 5 All hex)
    # Must run after gcc_compile.py has produced custom-riscv-gcc-merged.
    _build_merged_hwloop_image()

    patterns_json = Path(cfg.output_dir) / "patterns.json"
    plugin_flags: list[str] = []
    if patterns_json.exists():
        rel_path = str(patterns_json.resolve().relative_to(specializer_dir))
        plugin_flags = [
            "-fplugin=/opt/riscv/lib/gcc-plugin/fused_pass.so",
            f"-fplugin-arg-fused_pass-config=/work/{rel_path}",
        ]

    # ── Step 1: Triple compile ──
    print_step("1/5", "Compiling assembly (LTO fused + hwloop + plain)")

    # All benchmarks use the same approach: Docker triple compile + merge
    _run_generic_hwloop(cfg, ctx, changeset, specializer_dir, bm_dir, plugin_flags)
    return


def _extract_cflags_from_makefile(bm_dir: Path) -> list[str]:
    """Extract optimization flags from the benchmark Makefile's CFLAGS lines.
    Returns flags suitable for -S compilation (excludes -march, -mabi, linker flags)."""
    import re as _re

    makefile = bm_dir / "Makefile"
    if not makefile.exists():
        return ["-O3", "-fno-builtin", "-fno-common"]
    text = makefile.read_text()
    flags = []
    skip = {
        "-march",
        "-mabi",
        "-static",
        "-nostdlib",
        "-nostartfiles",
        "-Wall",
        "-Wextra",
        "-g",
        "-funroll-loops",
    }
    for m in _re.finditer(r"CFLAGS\s*\+?=\s*(.+)", text):
        line = m.group(1)
        if "filter-out" in line or "ASM_CFLAGS" in line:
            continue
        for token in line.split():
            if token.startswith("$(") or token.startswith("-T") or token.startswith("-Wl,"):
                continue
            if any(token.startswith(s) for s in skip):
                continue
            if token == "-flto":
                continue  # no LTO for assembly compilation
            if token not in flags:
                flags.append(token)
    # Extract -I flags from SRCDIR or compile recipes
    srcdir_m = _re.search(r"SRCDIR\s*=\s*(\S+)", text)
    if srcdir_m:
        flags.append(f"-I{srcdir_m.group(1)}")
    return flags or ["-O3", "-fno-builtin", "-fno-common"]


def _compile_merge_patch_variant(
    variant: str,  # "nounroll" or "unroll"
    specializer_dir: Path,
    bm_dir: Path,
    prog_name: str,
    src_file: str,
    makefile_cflags: list,
    plugin_flags: list,
    candidates: list,
    hwlp_enc_fused,
    hwlp_enc_plain,
) -> tuple:
    """Compile, merge, patch for one flag set. Returns (candidates_list, fused_src, plain_src)."""
    from arvis.cli import print_info
    from arvis.codegen.hwloop.merge_asm import merge_asm
    from arvis.pipeline.hwloop_sweep import HWLoopCandidate

    extra_flags = ["-funroll-loops"] if variant == "unroll" else []
    tag = f"_{variant}" if variant == "unroll" else ""
    results = []

    # ── Compile fused .s ──
    if variant == "unroll":
        make_s = bm_dir / f"{prog_name}.s"
        if make_s.exists() and make_s.stat().st_size > 0:
            fused_s = make_s
        else:
            fused_s_name = f"{prog_name}_fused_unroll.s"
            cmd = _riscv_cmd(
                specializer_dir,
                bm_dir,
                FUSED_IMAGE,
                "riscv32-unknown-elf-gcc",
                [
                    "-march=rv32imc_zicsr",
                    "-mabi=ilp32",
                    *makefile_cflags,
                    *extra_flags,
                    "-mcustom-fused",
                    *(plugin_flags or []),
                    "-S",
                    "-o",
                    fused_s_name,
                    src_file,
                ],
            )
            subprocess.call(cmd, timeout=120, cwd=str(bm_dir), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            fused_s = bm_dir / fused_s_name
    else:
        fused_s_name = f"{prog_name}_fused_nounroll.s"
        cmd = _riscv_cmd(
            specializer_dir,
            bm_dir,
            FUSED_IMAGE,
            "riscv32-unknown-elf-gcc",
            [
                "-march=rv32imc_zicsr",
                "-mabi=ilp32",
                *makefile_cflags,
                "-mcustom-fused",
                *(plugin_flags or []),
                "-S",
                "-o",
                fused_s_name,
                src_file,
            ],
        )
        subprocess.call(cmd, timeout=120, cwd=str(bm_dir), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        fused_s = bm_dir / fused_s_name

    if not fused_s.exists() or fused_s.stat().st_size == 0:
        return results, None, None

    # ── Compile auxiliary -mhwloop .s ──
    hwloop_s_name = f"{prog_name}_hwloop{tag}.s"
    hwloop_cmd = _riscv_cmd(
        specializer_dir,
        bm_dir,
        HWLOOP_IMAGE,
        "riscv32-unknown-elf-gcc",
        [
            "-march=rv32imc_zicsr",
            "-mabi=ilp32",
            *makefile_cflags,
            *extra_flags,
            "-mcustom-fused",
            "-mhwloop",
            *(plugin_flags or []),
            "-S",
            "-o",
            hwloop_s_name,
            src_file,
        ],
    )
    subprocess.call(hwloop_cmd, timeout=120, cwd=str(bm_dir), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    hwloop_s = bm_dir / hwloop_s_name if (bm_dir / hwloop_s_name).exists() else None

    # ── Plain .s (no fused): use baseline .s to match baseline hex ──
    # For unroll: use the system GCC .s.baseline (same as baseline hex)
    # For nounroll: compile via Docker without unroll
    baseline_s = bm_dir / f"{prog_name}.s.baseline"
    if variant == "unroll" and baseline_s.exists() and baseline_s.stat().st_size > 0:
        plain_s = baseline_s
    else:
        plain_s_name = f"{prog_name}_plain{tag}.s"
        plain_cmd = _riscv_cmd(
            specializer_dir,
            bm_dir,
            HWLOOP_ONLY_IMAGE,
            "riscv32-unknown-elf-gcc",
            [
                "-march=rv32imc_zicsr",
                "-mabi=ilp32",
                *makefile_cflags,
                *extra_flags,
                "-S",
                "-o",
                plain_s_name,
                src_file,
            ],
        )
        subprocess.call(
            plain_cmd,
            timeout=120,
            cwd=str(bm_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        plain_s = bm_dir / plain_s_name if (bm_dir / plain_s_name).exists() else None

    # ── Compile auxiliary plain -mhwloop .s ──
    hwloop_plain_name = f"{prog_name}_hwloop_nofused{tag}.s"
    hwloop_plain_cmd = _riscv_cmd(
        specializer_dir,
        bm_dir,
        HWLOOP_IMAGE,
        "riscv32-unknown-elf-gcc",
        [
            "-march=rv32imc_zicsr",
            "-mabi=ilp32",
            *makefile_cflags,
            *extra_flags,
            "-mhwloop",
            *(plugin_flags or []),
            "-S",
            "-o",
            hwloop_plain_name,
            src_file,
        ],
    )
    subprocess.call(
        hwloop_plain_cmd,
        timeout=120,
        cwd=str(bm_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    bm_dir / hwloop_plain_name if (bm_dir / hwloop_plain_name).exists() else None

    # ── Strip debug ──
    def _strip(src, name):
        if not src.exists():
            return src
        text = src.read_text()
        if ".debug" not in text[:10000]:
            return src
        out = bm_dir / name
        in_dbg = False
        with open(out, "w") as f:
            for line in text.splitlines(keepends=True):
                if line.strip().startswith(".section") and ".debug" in line:
                    in_dbg = True
                elif line.strip().startswith(".section") and ".debug" not in line:
                    in_dbg = False
                if not in_dbg:
                    f.write(line)
        return out

    fused_stripped = _strip(fused_s, f"{prog_name}_fused{tag}_nodebug.s")

    # ── Merge: standard .s + extra loops from -mhwloop ──
    replaced_fns = []
    if hwloop_s and hwloop_s.exists():
        hwloop_stripped = _strip(hwloop_s, f"{prog_name}_hwloop{tag}_nodebug.s")
        merged_s = bm_dir / f"{prog_name}_merged{tag}.s"
        _, replaced_fns = merge_asm(str(fused_stripped), str(hwloop_stripped), str(merged_s), max(candidates))
        fused_src = merged_s
    else:
        fused_src = fused_stripped

    # Plain source: use directly WITHOUT -mhwloop merge
    # The plain RTL is pruned for the plain hex — merged functions may use
    # instructions the plain RTL doesn't support
    plain_src = None
    if plain_s and plain_s.exists():
        plain_src = _strip(plain_s, f"{prog_name}_plain{tag}_nodebug.s") if plain_s.exists() else plain_s

    # ── Patch + assemble ──
    prev_loops = -1
    for hw_val in candidates:
        cand = HWLoopCandidate(hw_loop=hw_val)
        cand.fused_src = str(fused_src)
        cand.plain_src = str(plain_src) if plain_src else None
        cand._replaced_fns = replaced_fns
        cand._standard_src = str(fused_stripped)  # pre-merge source for per-function testing

        fused_patched, fused_stats = _patch_asm(
            fused_src,
            f"{prog_name}_hw{hw_val}{tag}_patched.s",
            hw_val,
            bm_dir,
            encoding=hwlp_enc_fused,
        )
        if fused_stats:
            cand.loops_patched = fused_stats.get("loops_patched", 0)
        if cand.loops_patched == prev_loops and prev_loops >= 0:
            continue
        prev_loops = cand.loops_patched

        if fused_patched:
            elf_name = f"{prog_name}_hw{hw_val}{tag}.elf"
            hex_name = f"{prog_name}_hw{hw_val}{tag}.hex"
            ok = _assemble_patched(specializer_dir, bm_dir, fused_patched, elf_name, hex_name, image=FUSED_IMAGE)
            if ok:
                cand.fused_hex = str(bm_dir / hex_name)
                cand.fused_elf = str(bm_dir / elf_name)

        if plain_src:
            plain_patched, _ = _patch_asm(
                plain_src,
                f"{prog_name}_hw{hw_val}_hwonly{tag}_patched.s",
                hw_val,
                bm_dir,
                encoding=hwlp_enc_plain,
            )
            if plain_patched:
                p_elf = f"{prog_name}_hw{hw_val}_hwonly{tag}.elf"
                p_hex = f"{prog_name}_hw{hw_val}_hwonly{tag}.hex"
                ok = _assemble_patched(specializer_dir, bm_dir, plain_patched, p_elf, p_hex, image=HWLOOP_ONLY_IMAGE)
                if ok:
                    cand.plain_hex = str(bm_dir / p_hex)
                    cand.plain_elf = str(bm_dir / p_elf)

        results.append(cand)
        print_info(
            f"  HW_LOOP={hw_val} ({variant}): {cand.loops_patched} loops, "
            f"fused_hex={'✓' if cand.fused_hex else '✗'}, "
            f"plain_hex={'✓' if cand.plain_hex else '✗'}"
        )

    return results, fused_src, plain_src


def _run_generic_hwloop(
    cfg: "ToolConfig",
    ctx: "PipelineContext",
    changeset: "RTLChangeSet",
    specializer_dir: Path,
    bm_dir: Path,
    plugin_flags: list[str],
) -> None:
    """HW loop: compile both flag sets, merge, patch, pick best."""
    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.cli import print_error, print_info, print_step, print_success
    from arvis.config import BENCHMARKS
    from arvis.pipeline.custom_insn_registry import build_registry_from_used_instructions
    from arvis.pipeline.hwloop_sweep import analyze_counter_width, get_candidates

    BENCHMARKS.get(cfg.benchmark_name, {})
    prog_name = cfg.benchmark_name.replace("embench_", "")
    makefile_cflags = _extract_cflags_from_makefile(bm_dir)

    # Determine source file
    import re as _re

    makefile = bm_dir / "Makefile"
    src_file = "main.c"
    if makefile.exists():
        m = _re.search(rf"^{_re.escape(prog_name)}\.s\s*:\s*(\S+)", makefile.read_text(), _re.MULTILINE)
        if m:
            src_file = m.group(1)

    # Detect eligible loops from the fused nounroll source (most loops)
    fused_s_check = bm_dir / f"{prog_name}_fused_nounroll.s"
    if not fused_s_check.exists():
        # Quick compile to check
        cmd = _riscv_cmd(
            specializer_dir,
            bm_dir,
            FUSED_IMAGE,
            "riscv32-unknown-elf-gcc",
            [
                "-march=rv32imc_zicsr",
                "-mabi=ilp32",
                *makefile_cflags,
                "-mcustom-fused",
                *(plugin_flags or []),
                "-S",
                "-o",
                f"{prog_name}_fused_nounroll.s",
                src_file,
            ],
        )
        subprocess.call(cmd, timeout=120, cwd=str(bm_dir), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    if fused_s_check.exists():
        det = AsmLoopDetector(str(fused_s_check))
        det.find_all_loops()
        eligible = [l for l in det.all_loops if l.hw_eligible]
        print_info(f"  {prog_name}: {len(eligible)} eligible HW loops")
        if not eligible:
            print_info("No eligible HW loops — HW_LOOP=0")
            changeset.hw_loop_count = 0
            return
    else:
        print_error(f"No fused .s available for {prog_name}")
        changeset.hw_loop_count = 0
        return

    candidates = get_candidates(str(fused_s_check))
    if not candidates:
        candidates = [1]
    print_info(f"  HW_LOOP candidates: {candidates}")

    # Compute encodings
    gcc_result = getattr(ctx, "gcc_compile_result", None)
    fused_next_slot = gcc_result.next_r4_slot if (gcc_result and gcc_result.used_instructions) else 0
    reg_fused = build_registry_from_used_instructions(hw_loop_count=max(candidates), next_r4_slot=fused_next_slot)
    hwlp_enc_fused = reg_fused.get_hwloop_encoding()
    reg_plain = build_registry_from_used_instructions(hw_loop_count=max(candidates), next_r4_slot=0)
    hwlp_enc_plain = reg_plain.get_hwloop_encoding()
    print_info(
        f"  HWLoop encoding (fused): bounds=0x{hwlp_enc_fused.bounds_opcode:02x}/f3={hwlp_enc_fused.bounds_funct3}, count=0x{hwlp_enc_fused.count_opcode:02x}/f3={hwlp_enc_fused.count_funct3}"  # noqa: E501
    )
    print_info(
        f"  HWLoop encoding (plain): bounds=0x{hwlp_enc_plain.bounds_opcode:02x}/f3={hwlp_enc_plain.bounds_funct3}, count=0x{hwlp_enc_plain.count_opcode:02x}/f3={hwlp_enc_plain.count_funct3}"  # noqa: E501
    )
    ctx._hwlp_enc_fused = hwlp_enc_fused
    ctx._hwlp_enc_plain = hwlp_enc_plain

    # ── Run both flag sets ──
    print_step("1/3", "Compiling + merging + patching (nounroll)")
    nounroll_cands, fused_src_nounroll, plain_src_nounroll = _compile_merge_patch_variant(
        "nounroll",
        specializer_dir,
        bm_dir,
        prog_name,
        src_file,
        makefile_cflags,
        plugin_flags,
        candidates,
        hwlp_enc_fused,
        hwlp_enc_plain,
    )

    print_step("2/3", "Compiling + merging + patching (unroll)")
    unroll_cands, fused_src_unroll, plain_src_unroll = _compile_merge_patch_variant(
        "unroll",
        specializer_dir,
        bm_dir,
        prog_name,
        src_file,
        makefile_cflags,
        plugin_flags,
        candidates,
        hwlp_enc_fused,
        hwlp_enc_plain,
    )

    # Combine all candidates
    for c in nounroll_cands:
        ctx.hwloop_candidates.append(c)
    for c in unroll_cands:
        ctx.hwloop_candidates.append(c)

    valid = [c for c in ctx.hwloop_candidates if c.fused_hex or c.plain_hex]
    if not valid:
        print_info("No valid HW loop candidates — HW_LOOP=0")
        changeset.hw_loop_count = 0
        return

    best = max(valid, key=lambda c: c.hw_loop)
    changeset.hw_loop_count = best.hw_loop

    # Counter width from all sources
    cnt_width = 8
    for src in [fused_src_nounroll, fused_src_unroll, plain_src_nounroll, plain_src_unroll]:
        if src and Path(src).exists():
            cnt_width = max(cnt_width, analyze_counter_width(str(src), best.hw_loop))
    changeset.hw_loop_cnt_width = cnt_width
    print_info(f"Counter width: {cnt_width} bits")

    # Address width from compiled ELFs (after patching). LP_start/end/last
    # only need to span the binary's text section, so we narrow the hwloop
    # registers to the smallest power of two that fits. Saves FFs and
    # shortens per-cycle PC-comparison CARRY chains in the controller,
    # aligner, and prefetch_controller.
    from arvis.pipeline.hwloop_sweep import analyze_addr_width
    addr_width = 12
    for elf in (best.fused_elf, best.plain_elf):
        if elf and Path(elf).exists():
            addr_width = max(addr_width, analyze_addr_width(str(elf)))
    changeset.hw_loop_addr_width = addr_width
    print_info(f"Address width: {addr_width} bits")

    ctx.hwloop_hex_path = best.fused_hex
    ctx.hwloop_elf_path = best.fused_elf
    ctx.hwloop_only_hex_path = best.plain_hex
    ctx.hwloop_only_elf_path = best.plain_elf
    # Save the original fused-only paths (before they get overwritten with
    # the fused+hwloop binary).  Pruning for the FUSED_PRUNED variant
    # needs to analyse the fused-only binary -- not the fused+hwloop one
    # -- because the FUSED_PRUNED RTL+binary combo is what runs at sim
    # time.  If we forget the fused-only path here, pruning may strip an
    # instruction that's only present in fused-only code (e.g., a plain
    # `xor` left outside a fusion that became a hwloop body in the
    # fused+hwloop binary), causing the FUSED_PRUNED simulator to trap.
    if ctx.fused_elf_path and not getattr(ctx, "fused_only_elf_path", None):
        ctx.fused_only_elf_path = ctx.fused_elf_path
        ctx.fused_only_hex_path = ctx.fused_hex_path
    ctx.fused_hex_path = best.fused_hex or ctx.fused_hex_path
    ctx.fused_elf_path = best.fused_elf or ctx.fused_elf_path

    # Build Docker baseline hex
    docker_baseline_s_name = f"{prog_name}_docker_baseline.s"
    bl_cmd = _riscv_cmd(
        specializer_dir,
        bm_dir,
        HWLOOP_ONLY_IMAGE,
        "riscv32-unknown-elf-gcc",
        [
            "-march=rv32imc_zicsr",
            "-mabi=ilp32",
            *makefile_cflags,
            "-S",
            "-o",
            docker_baseline_s_name,
            src_file,
        ],
    )
    subprocess.call(bl_cmd, timeout=120, cwd=str(bm_dir), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    docker_baseline_s = bm_dir / docker_baseline_s_name
    if docker_baseline_s.exists():
        bl_elf = f"{prog_name}_docker_baseline.elf"
        bl_hex = f"{prog_name}_docker_baseline.hex"
        ok = _assemble_patched(specializer_dir, bm_dir, docker_baseline_s, bl_elf, bl_hex, image=HWLOOP_ONLY_IMAGE)
        if ok:
            ctx.docker_baseline_hex = str(bm_dir / bl_hex)
            ctx.docker_baseline_elf = str(bm_dir / bl_elf)

    print_success(f"HW Loop done: {len(valid)} candidates, default HW_LOOP={best.hw_loop}")
