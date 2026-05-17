"""
Global configuration for the ARVIS CV32E40P specialisation toolchain.

This is the single source of truth for all configurable parameters.
``arvis.main`` reads these and passes them to the relevant subsystems.

Benchmark registry
------------------
Benchmarks are auto-discovered. Every directory under ``targets/benchmarks/``
that contains a ``benchmark.yaml`` file is registered with the pipeline at
import time. The minimum YAML schema is:

    name: <benchmark_name>
    elf: <name>_spike.elf
    verilator_elf: <name>.elf
    hex: <name>.hex

Optional fields are passed through as-is and consumed by the pipeline:
``trace``, ``hwloop``, ``extra_ldflags``, ``verilator_timeout_cycles``.

Use ``arvis add-benchmark <name>`` to scaffold a new benchmark from the
``minimal`` template.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from arvis.core_descriptor import CoreDescriptor

# ── Project layout discovery ──
#
# When ARVIS is installed as a uv tool the package lives outside the
# repository, so we cannot derive the project root from ``__file__``.
# Instead we walk upward from the current working directory looking for
# the marker directory ``targets/cv32e40p`` (always part of the repo).
# An explicit override via the ``ARVIS_PROJECT_ROOT`` environment
# variable takes precedence.
_PKG_DIR = Path(__file__).resolve().parent  # arvis/
RTL_ROOT = "targets/cv32e40p"
DEFAULT_BENCHMARK = "minimal"
_PROJECT_MARKER = "targets/cv32e40p"


def _find_project_root() -> Optional[Path]:
    """Return the repository root, or ``None`` if it cannot be located."""
    override = os.environ.get("ARVIS_PROJECT_ROOT")
    if override:
        p = Path(override).resolve()
        if (p / _PROJECT_MARKER).is_dir():
            return p
        # Fall through with a warning printed lazily on first access.
        return p  # let downstream code surface the missing-marker error

    cwd = Path.cwd().resolve()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / _PROJECT_MARKER).is_dir():
            return ancestor

    # Last-resort fallback for editable / development installs where
    # the package sits under the repo root (arvis/ is a sibling of targets/).
    pkg_parent = _PKG_DIR.parent
    if (pkg_parent / _PROJECT_MARKER).is_dir():
        return pkg_parent
    return None


PROJECT_ROOT: Optional[Path] = _find_project_root()
BENCHMARK_ROOT: Optional[Path] = PROJECT_ROOT / "targets" / "benchmarks" if PROJECT_ROOT else None


def _discover_benchmarks() -> Dict[str, Dict[str, Any]]:
    """Scan targets/benchmarks/*/benchmark.yaml and build the registry."""
    if BENCHMARK_ROOT is None or not BENCHMARK_ROOT.is_dir():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required to load benchmark descriptors. Install it via `uv sync` or `pip install pyyaml`."
        ) from e

    registry: Dict[str, Dict[str, Any]] = {}
    for entry in sorted(BENCHMARK_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        yaml_path = entry / "benchmark.yaml"
        if not yaml_path.is_file():
            continue
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        name = data.get("name", entry.name)
        rel_dir = os.path.relpath(entry, PROJECT_ROOT)
        bm: Dict[str, Any] = {"dir": rel_dir}
        for k in ("elf", "verilator_elf", "hex", "trace", "hwloop", "extra_ldflags"):
            if k in data:
                bm[k] = data[k]
        if "verilator_timeout_cycles" in data:
            bm["verilator_timeout_cycles"] = int(data["verilator_timeout_cycles"])
        registry[name] = bm
    return registry


BENCHMARKS: Dict[str, Dict[str, Any]] = _discover_benchmarks()


def _no_project_root_message() -> str:
    cwd = Path.cwd()
    return (
        "ARVIS could not locate the project root.\n"
        f"  current directory: {cwd}\n"
        f"  expected marker:   {_PROJECT_MARKER}\n"
        "Run `arvis` from inside an ARVIS repository (the directory that "
        "contains `targets/cv32e40p/`), or set ARVIS_PROJECT_ROOT to point "
        "at one."
    )


def _is_suite(name: str) -> bool:
    """Return True if the benchmark is a multi-program suite."""
    bm = BENCHMARKS.get(name, {})
    return "programs" in bm


def _suite_program_configs(name: str) -> List[Dict[str, str]]:
    """Return per-program config dicts for a suite benchmark."""
    bm = BENCHMARKS.get(name, {})
    suite_dir = bm.get("dir", "")
    programs = bm.get("programs", [])
    result = []
    for p in programs:
        prog = dict(p)  # copy
        # Resolve relative dirs against suite dir
        if "dir" in prog and not prog["dir"].startswith("/"):
            prog["dir"] = f"{suite_dir}/{prog['dir']}"
        result.append(prog)
    return result


@dataclass
class ToolConfig:
    """Complete tool configuration resolved from defaults + CLI args."""

    # ── Paths ──
    benchmark_name: str = DEFAULT_BENCHMARK
    benchmark_dir: str = ""
    elf_path: str = ""
    trace_path: str = ""
    rtl_root: str = RTL_ROOT
    output_dir: str = ""

    # ── Analysis thresholds ──
    loop_hotness_threshold: float = 0.01
    fusion_min_frequency: int = 10
    fusion_min_n: int = 2
    fusion_max_n: int = 4
    fusion_max_candidates: int = 30
    max_custom_instructions: int = 20
    max_accelerators: int = 1

    # ── CV32E40P register file ──
    baseline_read_ports: int = 3
    baseline_write_ports: int = 2

    # ── Verilator ──
    verilator_top: str = "tb_top"
    verilator_timeout_cycles: int = 20_000_000
    sim_timeout_seconds: int = 600

    # ── Spike ──
    spike_isa: str = "rv32imc_zicsr"

    # ── Phase selection ──
    enabled_phases: set = field(
        default_factory=lambda: {
            "analysis",
            "pruning",
            "fusion",
            "verification",
        }
    )

    # ── Hardware resource pruning ──
    # These control whether optional register file ports are pruned.
    # Defaults are set by main.py based on --phases selection.
    prune_rf_read_c: bool = False  # 3rd read port (operand_c / rs3)
    prune_rf_write_b: bool = True  # 2nd write port
    enable_debug: bool = False  # Keep RISC-V debug (JTAG) infrastructure
    exhaustive_hwloop: bool = False  # Per-loop selection: test each loop, keep only beneficial

    # ── Core descriptor (loaded lazily from rtl_root) ──
    _core_descriptor: Optional["CoreDescriptor"] = field(default=None, repr=False, compare=False)

    # ── Detected toolchain (populated by toolchain.detect_toolchain) ──
    riscv_objdump: Optional[str] = None
    riscv_gcc: Optional[str] = None
    riscv_objcopy: Optional[str] = None
    spike_bin: Optional[str] = None
    verilator_bin: Optional[str] = None
    yosys_bin: Optional[str] = None

    def __post_init__(self):
        """Resolve paths from benchmark name if not explicitly set."""
        if not BENCHMARKS:
            raise RuntimeError(_no_project_root_message())
        if self.benchmark_name in BENCHMARKS:
            bm = BENCHMARKS[self.benchmark_name]
        elif DEFAULT_BENCHMARK in BENCHMARKS:
            bm = BENCHMARKS[DEFAULT_BENCHMARK]
        else:
            available = ", ".join(sorted(BENCHMARKS)) or "(none)"
            raise ValueError(f"Unknown benchmark: {self.benchmark_name!r}. Available: {available}.")
        if not self.benchmark_dir:
            self.benchmark_dir = bm["dir"]
        if not self.elf_path and "elf" in bm:
            self.elf_path = f"{self.benchmark_dir}/{bm['elf']}"
        if not self.trace_path:
            trace_rel = bm.get("trace", "traces/spike.log")
            self.trace_path = f"{self.benchmark_dir}/{trace_rel}"
        if not self.output_dir:
            self.output_dir = f"output/{self.benchmark_name}_specialized"

    @property
    def core_descriptor(self) -> "CoreDescriptor":
        """Lazy-load the core descriptor from the RTL root directory.

        Looks for ``core_descriptor.yaml`` in ``self.rtl_root``.
        Falls back to a hardcoded CV32E40P default if not found.
        """
        if self._core_descriptor is None:
            from arvis.core_descriptor import CoreDescriptor

            self._core_descriptor = CoreDescriptor.load_for_target(self.rtl_root)
        return self._core_descriptor

    @classmethod
    def from_benchmark(cls, name: str) -> "ToolConfig":
        """Create config for a specific benchmark."""
        if not BENCHMARKS:
            raise RuntimeError(_no_project_root_message())
        if name not in BENCHMARKS:
            available = ", ".join(sorted(BENCHMARKS)) or "(none)"
            raise ValueError(f"Unknown benchmark: {name}. Available: {available}.")
        bm = BENCHMARKS[name]
        kwargs = {"benchmark_name": name}
        if "verilator_timeout_cycles" in bm:
            kwargs["verilator_timeout_cycles"] = bm["verilator_timeout_cycles"]
        return cls(**kwargs)

    @property
    def is_suite(self) -> bool:
        """True if this benchmark is a multi-program suite."""
        return _is_suite(self.benchmark_name)

    @property
    def suite_programs(self) -> List[Dict[str, str]]:
        """Per-program configs for a suite. Empty for single-program benchmarks."""
        if not self.is_suite:
            return []
        return _suite_program_configs(self.benchmark_name)

    def for_program(self, prog: Dict[str, str]) -> "ToolConfig":
        """Create a single-program ToolConfig from a suite program entry.

        Used internally to run per-program analysis within a suite.
        """
        import copy as _copy
        import os as _os

        child = _copy.copy(self)
        child.benchmark_name = prog["name"]
        child.benchmark_dir = self.benchmark_dir
        child.trace_path = f"{self.benchmark_dir}/traces/{prog['name']}_spike.log"
        # Use Spike ELF for disassembly when trace exists (PCs must match)
        spike_elf = prog.get("spike_elf", "")
        if spike_elf and _os.path.exists(f"{self.benchmark_dir}/{spike_elf}") and _os.path.exists(child.trace_path):
            child.elf_path = f"{self.benchmark_dir}/{spike_elf}"
        else:
            child.elf_path = f"{self.benchmark_dir}/{prog['elf']}"
        # Keep the suite's output_dir — RTL is shared
        return child
