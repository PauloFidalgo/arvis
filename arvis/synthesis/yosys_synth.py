"""
Yosys-based synthesis for area estimation.

Runs Yosys open-synthesis on baseline and pruned CV32E40P RTL,
parses cell/wire/area counts, and computes area savings from pruning.

Usage:
    from arvis.synthesis.yosys_synth import YosysSynthesizer, SynthConfig

    synth = YosysSynthesizer()
    if synth.available:
        result = synth.compare(
            baseline_rtl="targets/cv32e40p/rtl",
            pruned_rtl="output/<benchmark>_specialized/rtl_modified/rtl",
            prune_config=prune_config,
        )
        print(result.summary())
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from arvis.codegen.rtl.rtl_pruning import PruneConfig

# ═══════════════════════════════════════════════════════════════════════════
# SV preprocessor for Yosys compatibility
# ═══════════════════════════════════════════════════════════════════════════


def _preprocess_sv_for_yosys(text: str) -> str:
    """Preprocess SystemVerilog to work around Yosys limitations.

    Yosys doesn't support inline `import` in module declarations.
    CV32E40P uses two patterns:

      Pattern 1 (multi-line):
          module cv32e40p_alu
            import cv32e40p_pkg::*;
          (

      Pattern 2 (single-line):
          module cv32e40p_controller import cv32e40p_pkg::*;
          #(

    This function moves all such imports before the module declaration.
    """
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Pattern 2: "module NAME import PKG::*;" on a single line
        m2 = re.match(r"^(\s*module\s+\w+)\s+(import\s+\w+::\*\s*;)\s*$", line)
        if m2:
            result.append(m2.group(2))  # import before module
            result.append(m2.group(1))  # module declaration without import
            i += 1
            continue

        # Pattern 1: "module NAME" followed by "import PKG::*;" on next line(s)
        m1 = re.match(r"^(\s*module\s+\w+)\s*$", line)
        if m1:
            # Peek ahead to collect any import lines
            imports = []
            j = i + 1
            while j < len(lines):
                next_stripped = lines[j].strip()
                if re.match(r"^import\s+\w+(::\w+)?\s*::\*\s*;$", next_stripped):
                    imports.append(next_stripped)
                    j += 1
                elif next_stripped == "" or next_stripped.startswith("//"):
                    # Skip blank lines and comments between module and import
                    j += 1
                else:
                    break

            if imports:
                # Emit imports before module
                for imp in imports:
                    result.append(imp)
                result.append(line)  # module declaration
                i = j
                continue

        result.append(line)
        i += 1

    return "\n".join(result)


def _preprocess_rtl_dir(
    rtl_dir: str,
    defines: Optional[Dict[str, int]] = None,
) -> str:
    """Create a temp directory with Yosys-compatible Verilog files.

    Uses sv2v if available to convert SystemVerilog to Verilog (handles
    packages, typedefs, interfaces, etc.). Falls back to manual
    import-rewriting if sv2v is not installed.

    Returns path to the temp directory. Caller must clean up.
    """
    src = Path(rtl_dir)
    tmp = Path(tempfile.mkdtemp(prefix="yosys_rtl_"))

    sv2v_bin = shutil.which("sv2v")

    if sv2v_bin:
        # ── Use sv2v for proper SV→V conversion ──
        # Step 1: Copy all source files into tmp/ for consistent paths
        inc_dir = src / "include"
        tmp_inc = tmp / "include"
        if inc_dir.is_dir():
            shutil.copytree(str(inc_dir), str(tmp_inc))

        exclude = {
            "cv32e40p_register_file_latch.sv",  # duplicate of register_file_ff
            "cv32e40p_fp_wrapper.sv",  # needs fpnew_pkg (external)
            "cv32e40p_ispm.sv",  # custom SPM with $size() task
        }
        for sv_file in sorted(src.glob("*.sv")):
            if sv_file.name not in exclude:
                shutil.copy2(str(sv_file), str(tmp / sv_file.name))

        # Include behavioral clock gate (needed by sleep_unit)
        bhv_dir = src.parent / "bhv"
        clock_gate = bhv_dir / "cv32e40p_sim_clock_gate.sv"
        if clock_gate.exists():
            shutil.copy2(str(clock_gate), str(tmp / clock_gate.name))

        # Step 2: Patch parameter defaults for pruning
        # ALL synthesis parameters (COREV_PULP, FPU, ENABLE_*, NUM_MHPMCOUNTERS)
        # are RTL parameters, not preprocessor defines, so we must patch
        # their default values in the source before sv2v resolves them
        param_overrides = {}
        if defines:
            for k, v in defines.items():
                param_overrides[k] = v

        if param_overrides:
            for sv_file in tmp.glob("*.sv"):
                text = sv_file.read_text()
                modified = False
                for pname, pval in param_overrides.items():
                    # Match decimal, hex (32'hXXX), or binary values
                    new_text = re.sub(
                        rf"(parameter\s+{pname}\s*=\s*)"
                        rf"(?:\d+'[hHbBdD][\da-fA-F_]+|\d+)",
                        rf"\g<1>{pval}",
                        text,
                    )
                    if new_text != text:
                        text = new_text
                        modified = True
                if modified:
                    sv_file.write_text(text)

        # Step 3: Collect files from tmp/ for sv2v
        all_files = []
        if tmp_inc.is_dir():
            all_files.extend(sorted(tmp_inc.glob("*.sv")))
            all_files.extend(sorted(tmp_inc.glob("*.svh")))
        all_files.extend(sorted(tmp.glob("*.sv")))

        # Step 4: Run sv2v
        cmd = [sv2v_bin]
        if tmp_inc.is_dir():
            cmd.extend(["-I", str(tmp_inc)])
        if defines:
            for k, v in defines.items():
                cmd.append(f"-D{k}={v}")
        cmd.extend(str(f) for f in all_files)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and result.stdout.strip():
            # sv2v outputs a single combined Verilog file
            out_file = tmp / "cv32e40p_all.v"
            out_file.write_text(result.stdout)
            # Remove .sv files so Yosys only reads the .v
            for sv_file in list(tmp.glob("*.sv")):
                sv_file.unlink()
            if (tmp / "include").is_dir():
                shutil.rmtree(str(tmp / "include"))
            return str(tmp)
        else:
            # sv2v failed — fall through to manual preprocessing
            err = result.stderr[:200] if result.stderr else "empty output"
            print(f"    ⚠️  sv2v failed ({err}), falling back to manual preprocessing")

    # ── Fallback: manual import-rewriting ──
    # WARNING: sv2v is not available. This fallback uses Yosys's built-in SV
    # parser which has incomplete SV support. Results may differ from sv2v.
    # Install sv2v for reproducible synthesis: https://github.com/zachjs/sv2v
    print(
        "    ⚠️  sv2v not found — using Yosys built-in SV parser (less accurate).\n"
        "       Install sv2v for reproducible cell counts."
    )

    # Apply the same exclusions as the sv2v path to avoid duplicate module
    # definitions (register_file_latch vs register_file_ff) and files with
    # external dependencies that Yosys cannot resolve.
    fallback_exclude = {
        "cv32e40p_register_file_latch.sv",  # duplicate of register_file_ff
        "cv32e40p_fp_wrapper.sv",  # needs fpnew_pkg (external)
        "cv32e40p_ispm.sv",  # custom SPM with $size() task
    }

    inc_src = src / "include"
    inc_dst = tmp / "include"
    if inc_src.is_dir():
        shutil.copytree(str(inc_src), str(inc_dst), dirs_exist_ok=True)

    # Include behavioral clock gate
    bhv_dir = src.parent / "bhv"
    clock_gate = bhv_dir / "cv32e40p_sim_clock_gate.sv"
    if clock_gate.exists():
        shutil.copy2(str(clock_gate), str(tmp / clock_gate.name))

    for sv_file in sorted(src.glob("*.sv")):
        if sv_file.name in fallback_exclude:
            continue
        text = sv_file.read_text()
        # Apply parameter overrides (same as sv2v path)
        if defines:
            for pname, pval in defines.items():
                text = re.sub(
                    rf"(parameter\s+{pname}\s*=\s*)"
                    rf"(?:\d+'[hHbBdD][\da-fA-F_]+|\d+)",
                    rf"\g<1>{pval}",
                    text,
                )
        patched = _preprocess_sv_for_yosys(text)
        (tmp / sv_file.name).write_text(patched)

    vendor_src = src / "vendor"
    if vendor_src.is_dir():
        shutil.copytree(str(vendor_src), str(tmp / "vendor"))

    return str(tmp)


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class SynthStats:
    """Parsed statistics from a single Yosys synthesis run."""

    cells: int = 0
    wires: int = 0
    wire_bits: int = 0
    memories: int = 0
    memory_bits: int = 0
    processes: int = 0
    cell_breakdown: Dict[str, int] = field(default_factory=dict)
    raw_output: str = ""
    success: bool = False
    error: str = ""
    elapsed_seconds: float = 0.0

    @property
    def total_gates(self) -> int:
        """Rough gate-equivalent count (cells is the primary metric)."""
        return self.cells


@dataclass
class SynthComparison:
    """Comparison between baseline and pruned synthesis results."""

    baseline: SynthStats
    pruned: SynthStats
    defines_used: Dict[str, int] = field(default_factory=dict)

    @property
    def cell_savings(self) -> int:
        return self.baseline.cells - self.pruned.cells

    @property
    def cell_savings_pct(self) -> float:
        if self.baseline.cells == 0:
            return 0.0
        return 100.0 * self.cell_savings / self.baseline.cells

    @property
    def wire_savings(self) -> int:
        return self.baseline.wires - self.pruned.wires

    @property
    def wire_bit_savings(self) -> int:
        return self.baseline.wire_bits - self.pruned.wire_bits

    def summary(self) -> str:
        lines = [
            "Yosys Synthesis Comparison",
            "=" * 60,
            "",
            f"  {'Metric':<25} {'Baseline':>12} {'Pruned':>12} {'Savings':>12}",
            f"  {'-' * 25} {'-' * 12} {'-' * 12} {'-' * 12}",
        ]

        # Cells
        lines.append(
            f"  {'Cells':<25} {self.baseline.cells:>12,} "
            f"{self.pruned.cells:>12,} "
            f"{self.cell_savings:>+12,} ({self.cell_savings_pct:+.1f}%)"
        )

        # Wires
        lines.append(f"  {'Wires':<25} {self.baseline.wires:>12,} {self.pruned.wires:>12,} {self.wire_savings:>+12,}")

        # Wire bits
        lines.append(
            f"  {'Wire bits':<25} {self.baseline.wire_bits:>12,} "
            f"{self.pruned.wire_bits:>12,} "
            f"{self.wire_bit_savings:>+12,}"
        )

        lines.append("")

        # Cell breakdown (top types)
        if self.baseline.cell_breakdown or self.pruned.cell_breakdown:
            all_types = sorted(
                set(self.baseline.cell_breakdown) | set(self.pruned.cell_breakdown),
                key=lambda t: self.baseline.cell_breakdown.get(t, 0),
                reverse=True,
            )
            lines.append("  Cell type breakdown (top 15):")
            lines.append(f"    {'Type':<20} {'Baseline':>10} {'Pruned':>10} {'Delta':>10}")
            lines.append(f"    {'-' * 20} {'-' * 10} {'-' * 10} {'-' * 10}")
            for t in all_types[:15]:
                b = self.baseline.cell_breakdown.get(t, 0)
                p = self.pruned.cell_breakdown.get(t, 0)
                d = p - b
                if b > 0 or p > 0:
                    lines.append(f"    {t:<20} {b:>10,} {p:>10,} {d:>+10,}")
            lines.append("")

        # Defines used
        if self.defines_used:
            lines.append("  Synthesis defines:")
            for k, v in sorted(self.defines_used.items()):
                lines.append(f"    {k} = {v}")

        lines.append("")
        lines.append(
            f"  Time: baseline={self.baseline.elapsed_seconds:.1f}s, pruned={self.pruned.elapsed_seconds:.1f}s"
        )

        return "\n".join(lines)


@dataclass
class SynthConfig:
    """Configuration for Yosys synthesis."""

    top_module: str = "cv32e40p_core"
    timeout_seconds: int = 600
    flatten: bool = False
    extra_defines: Dict[str, int] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# Yosys output parser
# ═══════════════════════════════════════════════════════════════════════════


def _parse_yosys_stats(output: str) -> SynthStats:
    """Parse Yosys 'stat' command output into SynthStats.

    Yosys stat output looks like:
        === cv32e40p_core ===

           Number of wires:               4532
           Number of wire bits:           20145
           Number of public wires:         1234
           ...
           Number of cells:               12345
             $_AND_                          500
             $_NOT_                          200
             ...
    """
    stats = SynthStats(raw_output=output, success=True)

    # Parse "Number of cells: N" (older Yosys)
    m = re.search(r"Number of cells:\s+(\d+)", output)
    if m:
        stats.cells = int(m.group(1))

    # Parse "Number of wires: N"
    m = re.search(r"Number of wires:\s+(\d+)", output)
    if m:
        stats.wires = int(m.group(1))

    # Parse "Number of wire bits: N"
    m = re.search(r"Number of wire bits:\s+(\d+)", output)
    if m:
        stats.wire_bits = int(m.group(1))

    # Parse "Number of memories: N"
    m = re.search(r"Number of memories:\s+(\d+)", output)
    if m:
        stats.memories = int(m.group(1))

    # Parse "Number of memory bits: N"
    m = re.search(r"Number of memory bits:\s+(\d+)", output)
    if m:
        stats.memory_bits = int(m.group(1))

    # Parse "Number of processes: N"
    m = re.search(r"Number of processes:\s+(\d+)", output)
    if m:
        stats.processes = int(m.group(1))

    # Newer Yosys (0.40+) uses "=== design hierarchy ===" section with
    # "    NNNNN cells" format in the final stat. Look for the last
    # "design hierarchy" block.
    lines = output.splitlines()
    in_design_hierarchy = False
    last_design_cells = 0
    in_breakdown = False

    for i, line in enumerate(lines):
        # Detect "=== design hierarchy ===" section in stat output
        if "=== design hierarchy ===" in line:
            in_design_hierarchy = True
            in_breakdown = False
            continue

        if in_design_hierarchy:
            # "    42772 cells" line
            m_cells = re.match(r"\s+(\d+)\s+cells\s*$", line)
            if m_cells:
                last_design_cells = int(m_cells.group(1))
                in_breakdown = True
                continue

            # Cell type lines: "    13494   $_ANDNOT_" or "     1085   $_AND_"
            if in_breakdown:
                m_type = re.match(r"\s+(\d+)\s+([\$\w\\]+)\s*$", line)
                if m_type:
                    count = int(m_type.group(1))
                    ctype = m_type.group(2)
                    stats.cell_breakdown[ctype] = count
                    continue

            # End of section
            if line.strip().startswith("===") or line.strip().startswith("Warnings:"):
                in_design_hierarchy = False

    # Use design hierarchy total if we didn't get "Number of cells:"
    if stats.cells == 0 and last_design_cells > 0:
        stats.cells = last_design_cells

    # Also try to parse wires/wire bits from the same section
    # Look for "NNNNN wires" and "NNNNN wire bits" in stat sections
    for line in lines:
        if stats.wires == 0:
            m_w = re.match(r"\s+(\d+)\s+wires\s*$", line)
            if m_w:
                stats.wires = int(m_w.group(1))
        if stats.wire_bits == 0:
            m_wb = re.match(r"\s+(\d+)\s+wire bits\s*$", line)
            if m_wb:
                stats.wire_bits = int(m_wb.group(1))

    # Legacy: parse cell type breakdown from "Number of cells:" section
    if not stats.cell_breakdown:
        in_cells = False
        for line in lines:
            if "Number of cells:" in line:
                in_cells = True
                continue
            if in_cells:
                m2 = re.match(r"\s+([\$\w]+)\s+(\d+)", line)
                if m2:
                    stats.cell_breakdown[m2.group(1)] = int(m2.group(2))
                elif line.strip() == "" or line.strip().startswith("==="):
                    in_cells = False

    return stats


# ═══════════════════════════════════════════════════════════════════════════
# YosysSynthesizer
# ═══════════════════════════════════════════════════════════════════════════


class YosysSynthesizer:
    """Run Yosys open-synthesis to estimate area (cell counts).

    Provides:
      - synthesize(): run Yosys on a single RTL directory
      - compare(): synthesize baseline vs pruned and compute savings

    Yosys must be installed (brew install yosys / apt install yosys).
    """

    def __init__(self, yosys_bin: Optional[str] = None):
        self._bin = yosys_bin or shutil.which("yosys")

    @property
    def available(self) -> bool:
        """Check if Yosys is available on the system."""
        return self._bin is not None

    @property
    def version(self) -> Optional[str]:
        """Get Yosys version string."""
        if not self.available:
            return None
        try:
            result = subprocess.run(
                [str(self._bin), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _collect_sv_files(self, rtl_dir: str) -> Tuple[List[str], List[str]]:
        """Collect SystemVerilog files from an RTL directory.

        Returns (include_files, source_files) with includes first
        (packages, defines) and then the main source files.
        """
        rtl_path = Path(rtl_dir)
        includes = []
        sources = []

        # Include directory (packages, defines)
        inc_dir = rtl_path / "include"
        if inc_dir.is_dir():
            for f in sorted(inc_dir.glob("*.sv")):
                includes.append(str(f))
            for f in sorted(inc_dir.glob("*.svh")):
                includes.append(str(f))

        # Main RTL files
        for f in sorted(rtl_path.glob("*.sv")):
            sources.append(str(f))

        return includes, sources

    def _build_yosys_script(
        self,
        rtl_dir: str,
        config: SynthConfig,
        defines: Optional[Dict[str, int]] = None,
    ) -> str:
        """Build a Yosys TCL script for synthesis.

        Uses `read_verilog -sv` to read all files, then runs `synth`
        targeting generic gates, and `stat` for statistics.
        """
        includes, sources = self._collect_sv_files(rtl_dir)
        # Also collect .v files (from sv2v output)
        rtl_path = Path(rtl_dir)
        v_files = sorted(str(f) for f in rtl_path.glob("*.v"))
        all_files = includes + sources + v_files

        if not all_files:
            raise FileNotFoundError(f"No .sv/.v files found in {rtl_dir}")

        # Build define flags
        define_flags = []
        if defines:
            for k, v in defines.items():
                define_flags.append(f"-D{k}={v}")

        # Include path
        inc_dir = Path(rtl_dir) / "include"
        inc_flag = f"-I{inc_dir}" if inc_dir.is_dir() else ""

        # Build the script
        lines = []
        lines.append("# Auto-generated Yosys synthesis script")
        lines.append(f"# RTL dir: {rtl_dir}")
        lines.append(f"# Top module: {config.top_module}")
        lines.append("")

        # Read all files in a single command so Yosys resolves packages
        defs = " ".join(define_flags)
        file_list = " ".join(all_files)
        lines.append(f"read_verilog -sv {defs} {inc_flag} {file_list}")

        lines.append("")

        # Override module parameters on ALL modules in the design.
        # sv2v resolves top-level params (COREV_PULP) but keeps generate
        # blocks for sub-module params (ENABLE_MULH in cv32e40p_mult,
        # ENABLE_POPCNT in cv32e40p_alu, etc.) unresolved.
        # sv2v already patched parameter defaults in the .sv source
        # files before conversion. Yosys will resolve the generate
        # blocks during elaboration based on those defaults.
        # Do NOT use chparam here — sv2v renames modules and chparam
        # on sub-modules will fail. The patched defaults are enough.

        # Synthesize
        synth_opts = f"-top {config.top_module}"
        if config.flatten:
            synth_opts += " -flatten"
        lines.append(f"synth {synth_opts}")
        lines.append("")

        # Statistics
        lines.append("stat")

        return "\n".join(lines)

    def synthesize(
        self,
        rtl_dir: str,
        config: Optional[SynthConfig] = None,
        defines: Optional[Dict[str, int]] = None,
        label: str = "RTL",
    ) -> SynthStats:
        """Run Yosys synthesis on a single RTL directory.

        Args:
            rtl_dir: Path to directory containing .sv files
            config: Synthesis configuration
            defines: Verilog defines to pass (-D flags)
            label: Label for progress messages

        Returns:
            SynthStats with parsed results
        """
        if not self.available:
            return SynthStats(
                success=False,
                error="Yosys not found. Install with: brew install yosys",
            )

        if config is None:
            config = SynthConfig()

        import time

        start = time.time()

        # Preprocess RTL to fix Yosys-incompatible SV constructs
        sv2v_bin = shutil.which("sv2v")
        print(
            f"    sv2v: {'v' + subprocess.run([sv2v_bin, '--version'], capture_output=True, text=True).stdout.strip() if sv2v_bin else 'not found (fallback mode)'}"  # noqa: E501
        )
        preprocessed_dir = None
        try:
            preprocessed_dir = _preprocess_rtl_dir(rtl_dir, defines)
            effective_rtl_dir = preprocessed_dir
        except Exception as e:
            # Fall back to original dir if preprocessing fails
            effective_rtl_dir = rtl_dir
            print(f"    ⚠️  SV preprocessing failed, using original: {e}")

        try:
            script = self._build_yosys_script(effective_rtl_dir, config, defines)
        except FileNotFoundError as e:
            if preprocessed_dir:
                shutil.rmtree(preprocessed_dir, ignore_errors=True)
            return SynthStats(success=False, error=str(e))

        # Write script to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ys", delete=False, prefix="yosys_") as f:
            f.write(script)
            script_path = f.name

        try:
            print(f"    Running Yosys on {label}...")
            result = subprocess.run(
                [str(self._bin), "-s", script_path],
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
            )

            elapsed = time.time() - start
            output = result.stdout + "\n" + result.stderr

            if result.returncode != 0:
                # Check if stat output is still present (Yosys may warn but succeed)
                if "Number of cells:" in output:
                    stats = _parse_yosys_stats(output)
                    stats.elapsed_seconds = elapsed
                    return stats
                else:
                    # Extract meaningful error
                    err_lines = []
                    for line in output.splitlines():
                        if "ERROR" in line or "error" in line.lower():
                            err_lines.append(line.strip())
                    err_msg = "; ".join(err_lines[:5]) if err_lines else output[-500:]
                    return SynthStats(
                        success=False,
                        error=f"Yosys exited with code {result.returncode}: {err_msg}",
                        raw_output=output,
                        elapsed_seconds=elapsed,
                    )

            stats = _parse_yosys_stats(output)
            stats.elapsed_seconds = elapsed
            return stats

        except subprocess.TimeoutExpired:
            elapsed = time.time() - start
            return SynthStats(
                success=False,
                error=f"Yosys timed out after {config.timeout_seconds}s",
                elapsed_seconds=elapsed,
            )
        except FileNotFoundError:
            return SynthStats(
                success=False,
                error=f"Yosys binary not found at {self._bin}",
            )
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass
            if preprocessed_dir:
                shutil.rmtree(preprocessed_dir, ignore_errors=True)

    def compare(
        self,
        baseline_rtl: str,
        pruned_rtl: str,
        prune_config: Optional[PruneConfig] = None,
        synth_config: Optional[SynthConfig] = None,
        baseline_stats: Optional[SynthStats] = None,
    ) -> SynthComparison:
        """Synthesize baseline and pruned RTL, compare cell counts.

        Args:
            baseline_rtl: Path to baseline RTL directory
            pruned_rtl: Path to pruned RTL directory
            prune_config: PruneConfig to derive synthesis defines
            synth_config: Yosys synthesis configuration
            baseline_stats: Pre-computed baseline stats (avoids re-synthesis)
        """
        if synth_config is None:
            synth_config = SynthConfig()

        baseline_defines = {
            "COREV_PULP": 0,
            "FPU": 0,
        }

        pruned_defines = dict(baseline_defines)
        if prune_config is not None:
            params = prune_config.synthesis_parameters()
            pruned_defines.update(params)

        if baseline_stats is not None:
            baseline = baseline_stats
            print(f"    ✅ Baseline: {baseline.cells:,} cells (cached)")
        else:
            print(f"  Synthesizing baseline ({Path(baseline_rtl).name})...")
            baseline = self.synthesize(baseline_rtl, synth_config, baseline_defines, label="baseline")
            if baseline.success:
                print(f"    ✅ Baseline: {baseline.cells:,} cells ({baseline.elapsed_seconds:.1f}s)")
            else:
                print(f"    ❌ Baseline failed: {baseline.error[:100]}")

        print(f"  Synthesizing pruned ({Path(pruned_rtl).name})...")
        pruned = self.synthesize(pruned_rtl, synth_config, pruned_defines, label="pruned")
        if pruned.success:
            print(f"    ✅ Pruned:   {pruned.cells:,} cells ({pruned.elapsed_seconds:.1f}s)")
        else:
            print(f"    ❌ Pruned failed: {pruned.error[:100]}")

        return SynthComparison(
            baseline=baseline,
            pruned=pruned,
            defines_used=pruned_defines,
        )

    # ═══════════════════════════════════════════════════════════════════
    # Sky130 Standard Cell Synthesis
    # ═══════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════
    # OpenLane Full ASIC Flow (Fmax + Area + Power)
    # ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# OpenLane results
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Sky130-specific data classes and parser
# ═══════════════════════════════════════════════════════════════════════════
