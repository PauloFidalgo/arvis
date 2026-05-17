#!/usr/bin/env python3
"""Merge assembly files: take specific functions from -mhwloop build,
everything else from standard build. Then patch the merged result."""

import re
import sys

from arvis.analysis.hwloop import AsmLoopDetector
from arvis.codegen.hwloop.generator import AsmPatcher, HWLoopGenerator


def extract_functions(asm_path):
    """Extract function name -> (start_line, end_line) from assembly."""
    lines = open(asm_path).readlines()
    funcs = {}
    current_fn = None
    fn_start = None
    for i, line in enumerate(lines):
        m = re.match(r"^(\w[\w.]+):", line)
        if m and not line.strip().startswith("."):
            current_fn = m.group(1)
            fn_start = i
        if re.match(r"\s*\.size\s+" + re.escape(current_fn or "") + r"\b", line) and current_fn:
            end = i
            if end + 1 < len(lines) and lines[end + 1].strip() == ".option pop":
                end += 1
            funcs[current_fn] = (fn_start, end)
            current_fn = None
    return funcs, lines


def find_benefit_functions(standard_asm, hwloop_asm, hw_loop=2):
    """Find functions where -mhwloop adds new configured loops.

    No fusion-loss filtering — the exhaustive search will determine
    which loops are actually beneficial.
    """
    d1 = AsmLoopDetector(standard_asm)
    d1.find_all_loops()
    g1 = HWLoopGenerator(d1, hw_loop=hw_loop)
    g1.generate()
    no_cfg = {}
    for g in g1.groups:
        no_cfg.setdefault(g.root.function, 0)
        no_cfg[g.root.function] += len(g.configs)

    d2 = AsmLoopDetector(hwloop_asm)
    d2.find_all_loops()
    g2 = HWLoopGenerator(d2, hw_loop=hw_loop)
    g2.generate()
    hw_cfg = {}
    for g in g2.groups:
        hw_cfg.setdefault(g.root.function, 0)
        hw_cfg[g.root.function] += len(g.configs)

    benefit = set()
    for fn in set(hw_cfg) | set(no_cfg):
        extra_loops = hw_cfg.get(fn, 0) - no_cfg.get(fn, 0)
        if extra_loops > 0:
            benefit.add(fn)
    return benefit


def merge_asm(standard_asm, hwloop_asm, output_asm, hw_loop=2):
    """Merge: take benefit functions from hwloop, rest from standard."""
    benefit_fns = find_benefit_functions(standard_asm, hwloop_asm, hw_loop)
    print(f"Functions to take from -mhwloop ({len(benefit_fns)}):")
    for fn in sorted(benefit_fns):
        print(f"  {fn}")

    std_funcs, std_lines = extract_functions(standard_asm)
    hw_funcs, hw_lines = extract_functions(hwloop_asm)

    # Collect all global symbols defined in the standard .s
    std_symbols = set(std_funcs.keys())
    for line in std_lines:
        m = re.match(r"^(\w[\w.]+):", line)
        if m:
            std_symbols.add(m.group(1))

    # Filter: allow replacement if all call targets exist in standard .s
    # AND no calls to .constprop variants (numbering differs between builds)
    safe_fns = set()
    for fn in benefit_fns:
        if fn not in hw_funcs or fn not in std_funcs:
            continue
        hw_start, hw_end = hw_funcs[fn]
        calls = set()
        for hl in hw_lines[hw_start : hw_end + 1]:
            m = re.match(r"\s+(?:call|tail)\s+(\w[\w.]+)", hl)
            if m and m.group(1) != fn:
                calls.add(m.group(1))
        missing = calls - std_symbols
        constprop = [c for c in calls if ".constprop" in c]
        if missing:
            print(f"  Skipped: {fn} (calls missing: {', '.join(sorted(missing))})")
        elif constprop:
            print(f"  Skipped: {fn} (constprop calls: {', '.join(sorted(constprop))})")
        elif ".constprop" in fn:
            print(f"  Skipped: {fn} (is constprop variant — unsafe cross-build)")
        else:
            safe_fns.add(fn)

    # Build output: standard assembly with benefit functions replaced
    result = []
    skip_until = -1
    for i, line in enumerate(std_lines):
        if i < skip_until:
            continue
        replaced = False
        for fn in safe_fns:
            if fn in std_funcs and std_funcs[fn][0] == i:
                # Replace with hwloop version, renaming labels to avoid conflicts
                if fn in hw_funcs:
                    hw_start, hw_end = hw_funcs[fn]
                    # Collect all .L labels in this function
                    labels = set()
                    for hl in hw_lines[hw_start : hw_end + 1]:
                        for m2 in re.finditer(r"\.L(\d+)", hl):
                            labels.add(m2.group(1))
                    # Rename .Lnnn -> .Lh_nnn to avoid conflicts
                    for hl in hw_lines[hw_start : hw_end + 1]:
                        for lbl in labels:
                            hl = hl.replace(f".L{lbl}", f".Lh_{lbl}")
                        result.append(hl)
                    skip_until = std_funcs[fn][1] + 1
                    replaced = True
                    print(f"  Replaced: {fn}")
                break
        if not replaced and i >= skip_until:
            result.append(line)

    merged = "".join(result)

    # Now patch the merged assembly
    with open(output_asm, "w") as f:
        f.write(merged)

    replaced_fns = [fn for fn in safe_fns if fn in hw_funcs]
    print(f"\nMerged assembly: {output_asm} ({len(replaced_fns)} functions replaced)")
    return output_asm, replaced_fns


if __name__ == "__main__":
    standard = sys.argv[1]  # e.g. <benchmark>_nohwloop.s
    hwloop = sys.argv[2]  # e.g. <benchmark>_hwloop.s
    output = sys.argv[3]  # e.g. <benchmark>_merged.s
    hw_loop = int(sys.argv[4]) if len(sys.argv) > 4 else 2

    merged = merge_asm(standard, hwloop, output, hw_loop)

    # Patch the merged file
    print(f"\nPatching {merged}...")
    patched_output = output.replace(".s", "_hw.s")

    det = AsmLoopDetector(merged)
    det.find_all_loops()
    gen = HWLoopGenerator(det, hw_loop=hw_loop)
    gen.generate()
    patcher = AsmPatcher(gen, open(merged).read())
    patched = patcher.patch()

    with open(patched_output, "w") as f:
        f.write(patched)

    stats = patcher.get_stats()
    print(f"Loops patched: {stats['loops_patched']}")
    print(f"Output: {patched_output}")


def revert_function(merged_asm: str, standard_asm: str, fn_name: str, output_asm: str) -> str:
    """Replace one function in the merged .s with its version from the standard .s.
    Returns output path."""
    merged_funcs, merged_lines = extract_functions(merged_asm)
    std_funcs, std_lines = extract_functions(standard_asm)

    if fn_name not in merged_funcs or fn_name not in std_funcs:
        # Can't revert — just copy merged
        with open(output_asm, "w") as f:
            f.writelines(merged_lines)
        return output_asm

    m_start, m_end = merged_funcs[fn_name]
    s_start, s_end = std_funcs[fn_name]

    result = merged_lines[:m_start] + std_lines[s_start : s_end + 1] + ["\n"] + merged_lines[m_end + 1 :]
    with open(output_asm, "w") as f:
        f.writelines(result)
    return output_asm
