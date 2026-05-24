"""Sweep HW_LOOP nesting depth measuring cycles + area.

Produces patched hex files for each candidate depth during the hwloop phase.
Evaluation (sim + synth) happens in verification where the real pruned RTL
is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class HWLoopCandidate:
    """Artifacts for one HW_LOOP candidate depth."""

    hw_loop: int
    loops_patched: int = 0
    fused_hex: Optional[str] = None
    fused_elf: Optional[str] = None
    plain_hex: Optional[str] = None
    plain_elf: Optional[str] = None
    fused_src: Optional[str] = None  # source .s used for fused patching
    plain_src: Optional[str] = None  # source .s used for plain patching
    _replaced_fns: list = None  # functions replaced by -mhwloop merge
    _standard_src: Optional[str] = None  # pre-merge source for per-function testing


@dataclass
class HWLoopSweepResult:
    """Result of evaluating one candidate in verification."""

    hw_loop: int
    loops_patched: int = 0
    cycles: int = 0
    cells: int = 0
    passed: bool = False

    @property
    def adp(self) -> float:
        if self.cycles > 0 and self.cells > 0:
            return self.cycles * self.cells / 1e9
        return float("inf")


def get_candidates(asm_path: str) -> List[int]:
    """Derive HW_LOOP candidates from max nesting depth in the assembly."""
    from arvis.analysis.hwloop import AsmLoopDetector

    det = AsmLoopDetector(asm_path)
    det.find_all_loops()
    max_depth = max((l.nesting_depth for l in det.all_loops if l.hw_eligible), default=0)
    return list(range(1, max_depth + 2)) if max_depth > 0 else [1]


@dataclass
class SweepWinners:
    """Best HW_LOOP values from the sweep."""

    best_adp: int = 0  # best area × delay product
    best_cycles: int = 0  # best raw performance
    same: bool = True  # True if both winners are the same
    best_adp_result: "HWLoopSweepResult | None" = None
    best_cycles_result: "HWLoopSweepResult | None" = None


def pick_best(results: List[HWLoopSweepResult]) -> Tuple[SweepWinners, List[HWLoopSweepResult]]:
    """Pick best HW_LOOP by ADP and by cycles.

    Returns (winners, results). winners has best_adp=0 and best_cycles=0 if none passed.
    """
    from arvis.cli import print_warning

    passing = [r for r in results if r.passed and r.cells > 0]
    winners = SweepWinners()
    if not passing:
        print_warning("No valid HW_LOOP results — disabling")
        return winners, results

    by_adp = min(passing, key=lambda r: r.adp)
    by_cycles = min(passing, key=lambda r: r.cycles)
    winners.best_adp = by_adp.hw_loop
    winners.best_cycles = by_cycles.hw_loop
    winners.best_adp_result = by_adp
    winners.best_cycles_result = by_cycles
    winners.same = winners.best_adp == winners.best_cycles

    print(f"\n  {'HW_LOOP':>7s}  {'Loops':>5s}  {'Cycles':>12s}  {'Cells':>8s}  {'ADP':>10s}")
    print(f"  {'─' * 7}  {'─' * 5}  {'─' * 12}  {'─' * 8}  {'─' * 10}")
    for r in results:
        if not r.passed:
            print(f"  {r.hw_loop:>7d}  {r.loops_patched:>5d}  {'FAIL':>12s}")
            continue
        markers = []
        if r.hw_loop == by_adp.hw_loop:
            markers.append("best ADP")
        if r.hw_loop == by_cycles.hw_loop:
            markers.append("best cycles")
        tag = f" ◀ {', '.join(markers)}" if markers else ""
        print(f"  {r.hw_loop:>7d}  {r.loops_patched:>5d}  {r.cycles:>12,}  {r.cells:>8,}  {r.adp:>10.2f}{tag}")

    if winners.same:
        print(f"\n  → HW_LOOP={winners.best_adp}: best ADP and best cycles")
    else:
        print(f"\n  → HW_LOOP={winners.best_adp}: best ADP")
        print(f"  → HW_LOOP={winners.best_cycles}: best cycles")

    return winners, results


def analyze_counter_width(asm_path: str, hw_loop: int, margin_bits: int = 1) -> int:
    """Determine minimum counter width from benchmark loop analysis.

    Analyzes all eligible loops, finds the max iteration count (constant
    or estimated), and returns the minimum bit width needed plus a safety
    margin.

    Args:
        asm_path: Path to the (merged) assembly.
        hw_loop: HW_LOOP nesting depth.
        margin_bits: Extra bits for safety (default 1).

    Returns:
        Optimal CNT_WIDTH (minimum 8, maximum 32).
    """
    from arvis.analysis.hwloop import AsmLoopDetector
    from arvis.codegen.hwloop.generator import HWLoopGenerator

    det = AsmLoopDetector(asm_path)
    det.find_all_loops()
    gen = HWLoopGenerator(det, hw_loop=hw_loop)
    gen.generate()

    max_count = 0
    for g in gen.groups:
        for cfg in g.configs:
            # Check count from setup instructions (li reg, N)
            for insn in cfg.count_calc_insns:
                if "li " in insn:
                    parts = insn.split(",")
                    if len(parts) >= 2:
                        try:
                            val = int(parts[-1].strip().split("#")[0].strip())
                            max_count = max(max_count, abs(val))
                        except ValueError:
                            pass
            # Also check IV init value (when count_reg reuses IV, no li is emitted)
            det_info = cfg.loop.constraint_results.get("deterministic_iterations", {})
            iv_init = det_info.get("iv_init", {})
            if iv_init.get("type") == "immediate":
                try:
                    val = abs(int(iv_init["value"]))
                    max_count = max(max_count, val)
                except (ValueError, KeyError):
                    pass

            # Also check estimated iteration count (covers bound-register reuse)
            est = gen._estimate_iterations(cfg.loop)
            if est is not None:
                max_count = max(max_count, est)

    if max_count == 0:
        # All dynamic counts — use conservative 16-bit default
        return 16

    width = max_count.bit_length() + margin_bits
    return max(8, min(32, width))


def analyze_addr_width(elf_path: str, margin_bits: int = 1) -> int:
    """Determine minimum HWLP_ADDR_WIDTH for a compiled binary.

    The hwloop_regs registers (LP_start, LP_end, LP_last) only need to
    hold addresses inside the binary's text section. Narrowing them
    saves FFs and shortens the per-cycle PC-comparison CARRY chains in
    the controller, aligner, and prefetch_controller.

    Strategy: read the ELF, find the highest virtual address used by
    code (top of .text or any other executable section), and return
    ``clog2(max_addr + 1) + margin``. The margin guards against
    relocation rounding or compiler tweaks producing slightly higher
    addresses on a re-build.

    Args:
        elf_path: Path to a compiled ELF that targets the same memory
            layout as the synthesis target.
        margin_bits: Extra bits beyond the strictly-required width.

    Returns:
        Optimal HWLP_ADDR_WIDTH (minimum 12 — the smallest realistic
        text section, maximum 32 — original behaviour).
    """
    import subprocess

    # Look at the highest end-of-section address among executable sections
    # (.text, .text.*, .init, .fini etc). objdump -h is portable across
    # different toolchain installs (we already shell out to riscv32-objdump
    # elsewhere in the pipeline, so it's available).
    try:
        out = subprocess.check_output(
            ["riscv32-unknown-elf-objdump", "-h", elf_path],
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return 32  # conservative fallback if objdump is missing

    max_end = 0
    for line in out.splitlines():
        # Lines look like:  N .text   00000DEADBEEF  00000000  00000000  ...
        # Section flags appear on the next line. We just grab any line
        # with a numeric size + LMA we can parse.
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            size = int(parts[2], 16)
            lma = int(parts[3], 16)
        except ValueError:
            continue
        # Heuristic filter: only count code-bearing sections that PC can
        # reach during execution (.text*, .init, .fini, .plt). Data
        # sections like .rodata/.data/.bss are not PC targets even if
        # they sit at higher addresses, so they don't constrain hwloop
        # address widths.
        name = parts[1]
        if not (name.startswith(".text") or name in (".init", ".fini", ".plt")):
            continue
        end = lma + size
        if end > max_end:
            max_end = end

    if max_end <= 0:
        return 32  # couldn't parse; play it safe

    width = max_end.bit_length() + margin_bits
    return max(12, min(32, width))
