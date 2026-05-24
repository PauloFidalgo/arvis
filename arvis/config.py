"""
Global configuration for the CV32E40P Workload Specialization Tool.

This is the single source of truth for all configurable parameters.
main.py reads these and passes them to the relevant subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core_descriptor import CoreDescriptor

# ── Benchmark registry ──

BENCHMARKS: dict[str, dict[str, str]] = {
    "kyber": {
        "dir": "targets/benchmarks/kyber512_rv32",
        "elf": "kyber512_rv32.elf",
        "verilator_elf": "kyber512_cv32e40p.elf",
        "hex": "kyber512_cv32e40p.hex",
    },
    "conv2d": {
        "dir": "targets/benchmarks/conv2d",
        "elf": "conv2d_spike.elf",
        "verilator_elf": "conv2d.elf",
        "hex": "conv2d.hex",
    },
    "minimal": {
        "dir": "targets/benchmarks/minimal",
        "elf": "minimal_spike.elf",
        "verilator_elf": "minimal.elf",
        "hex": "minimal.hex",
    },
    "stress": {
        "dir": "targets/benchmarks/stress",
        "elf": "stress_spike.elf",
        "verilator_elf": "stress.elf",
        "hex": "stress.hex",
    },
    "divheavy": {
        "dir": "targets/benchmarks/divheavy",
        "elf": "divheavy_spike.elf",
        "verilator_elf": "divheavy.elf",
        "hex": "divheavy.hex",
    },
    "mulheavy": {
        "dir": "targets/benchmarks/mulheavy",
        "elf": "mulheavy_spike.elf",
        "verilator_elf": "mulheavy.elf",
        "hex": "mulheavy.hex",
    },
    "hwloop_test": {
        "dir": "targets/benchmarks/hwloop_test",
        "elf": "hwloop_test_spike.elf",
        "verilator_elf": "hwloop_test.elf",
        "hex": "hwloop_test.hex",
        "hwloop": True,
    },
    "pool": {
        "dir": "targets/benchmarks/depthwise_pool",
        "elf": "depthwise_pool_spike.elf",
        "verilator_elf": "depthwise_pool.elf",
        "hex": "depthwise_pool.hex",
    },
    "kyber_all": {
        "dir": "targets/benchmarks/kyber_all",
        "elf": "kyber_all_spike.elf",
        "verilator_elf": "kyber_all.elf",
        "hex": "kyber_all.hex",
    },
    "embench": {
        "dir": "targets/benchmarks/embench",
        "programs": [
            {
                "name": "aha-mont64",
                "elf": "aha-mont64.elf",
                "hex": "aha-mont64.hex",
                "spike_elf": "aha-mont64_spike.elf",
            },
            {
                "name": "crc32",
                "elf": "crc32.elf",
                "hex": "crc32.hex",
                "spike_elf": "crc32_spike.elf",
            },
            {
                "name": "depthconv",
                "elf": "depthconv.elf",
                "hex": "depthconv.hex",
                "spike_elf": "depthconv_spike.elf",
            },
            {"name": "edn", "elf": "edn.elf", "hex": "edn.hex", "spike_elf": "edn_spike.elf"},
            {
                "name": "matmult-int",
                "elf": "matmult-int.elf",
                "hex": "matmult-int.hex",
                "spike_elf": "matmult-int_spike.elf",
            },
            {
                "name": "nettle-aes",
                "elf": "nettle-aes.elf",
                "hex": "nettle-aes.hex",
                "spike_elf": "nettle-aes_spike.elf",
            },
            {
                "name": "nsichneu",
                "elf": "nsichneu.elf",
                "hex": "nsichneu.hex",
                "spike_elf": "nsichneu_spike.elf",
            },
            {
                "name": "picojpeg",
                "elf": "picojpeg.elf",
                "hex": "picojpeg.hex",
                "spike_elf": "picojpeg_spike.elf",
            },
            {"name": "slre", "elf": "slre.elf", "hex": "slre.hex", "spike_elf": "slre_spike.elf"},
            {"name": "ud", "elf": "ud.elf", "hex": "ud.hex", "spike_elf": "ud_spike.elf"},
            {
                "name": "xgboost",
                "elf": "xgboost.elf",
                "hex": "xgboost.hex",
                "spike_elf": "xgboost_spike.elf",
            },
        ],
    },
    "xgboost": {
        "dir": "targets/benchmarks/xgboost",
        "elf": "xgboost_spike.elf",
        "verilator_elf": "xgboost.elf",
        "hex": "xgboost.hex",
    },
    "mont64": {
        "dir": "targets/benchmarks/mont64",
        "elf": "mont64_spike.elf",
        "verilator_elf": "mont64.elf",
        "hex": "mont64.hex",
    },
    "crc32": {
        "dir": "targets/benchmarks/crc32",
        "elf": "crc32_spike.elf",
        "verilator_elf": "crc32.elf",
        "hex": "crc32.hex",
    },
    "depthconv": {
        "dir": "targets/benchmarks/depthconv",
        "elf": "depthconv_spike.elf",
        "verilator_elf": "depthconv.elf",
        "hex": "depthconv.hex",
    },
    "edn": {
        "dir": "targets/benchmarks/edn",
        "elf": "edn_spike.elf",
        "verilator_elf": "edn.elf",
        "hex": "edn.hex",
    },
    "matmulint": {
        "dir": "targets/benchmarks/matmulint",
        "elf": "matmulint_spike.elf",
        "verilator_elf": "matmulint.elf",
        "hex": "matmulint.hex",
    },
    "md5": {
        "dir": "targets/benchmarks/md5",
        "elf": "md5_spike.elf",
        "verilator_elf": "md5.elf",
        "hex": "md5.hex",
    },
    "huffbench": {
        "dir": "targets/benchmarks/huffbench",
        "elf": "huffbench_spike.elf",
        "verilator_elf": "huffbench.elf",
        "hex": "huffbench.hex",
    },
    "nettle_aes": {
        "dir": "targets/benchmarks/nettle_aes",
        "elf": "nettle_aes_spike.elf",
        "verilator_elf": "nettle_aes.elf",
        "hex": "nettle_aes.hex",
    },
    "nettle_sha": {
        "dir": "targets/benchmarks/nettle_sha",
        "elf": "nettle_sha_spike.elf",
        "verilator_elf": "nettle_sha.elf",
        "hex": "nettle_sha.hex",
    },
    "nsichneu": {
        "dir": "targets/benchmarks/nsichneu",
        "elf": "nsichneu_spike.elf",
        "verilator_elf": "nsichneu.elf",
        "hex": "nsichneu.hex",
    },
    "qrduino": {
        "dir": "targets/benchmarks/qrduino",
        "elf": "qrduino_spike.elf",
        "verilator_elf": "qrduino.elf",
        "hex": "qrduino.hex",
    },
    "sle": {
        "dir": "targets/benchmarks/sle",
        "elf": "sle_spike.elf",
        "verilator_elf": "sle.elf",
        "hex": "sle.hex",
    },
    "statemate": {
        "dir": "targets/benchmarks/statemate",
        "elf": "statemate_spike.elf",
        "verilator_elf": "statemate.elf",
        "hex": "statemate.hex",
    },
    "tarfind": {
        "dir": "targets/benchmarks/tarfind",
        "elf": "tarfind_spike.elf",
        "verilator_elf": "tarfind.elf",
        "hex": "tarfind.hex",
    },
    "ud": {
        "dir": "targets/benchmarks/ud",
        "elf": "ud_spike.elf",
        "verilator_elf": "ud.elf",
        "hex": "ud.hex",
    },
    "wikisort": {
        "dir": "targets/benchmarks/wikisort",
        "elf": "wikisort_spike.elf",
        "verilator_elf": "wikisort.elf",
        "hex": "wikisort.hex",
        "extra_ldflags": "-lgcc",
        "verilator_timeout_cycles": 100_000_000,
    },
    "dilithium": {
        "dir": "targets/benchmarks/dilithium",
        "elf": "dilithium_spike.elf",
        "verilator_elf": "dilithium.elf",
        "hex": "dilithium.hex",
        "verilator_timeout_cycles": 35_000_000,
    },
    "sglib": {
        "dir": "targets/benchmarks/sglib",
        "elf": "sglib_spike.elf",
        "verilator_elf": "sglib.elf",
        "hex": "sglib.hex",
    },
    "picojpeg": {
        "dir": "targets/benchmarks/picojpeg",
        "elf": "picojpeg_spike.elf",
        "verilator_elf": "picojpeg.elf",
        "hex": "picojpeg.hex",
    },
}

# Auto-register each embench program as an individual benchmark
for _p in BENCHMARKS.get("embench", {}).get("programs", []):
    _name = f"embench_{_p['name']}"
    if _name not in BENCHMARKS:
        BENCHMARKS[_name] = {
            "dir": "targets/benchmarks/embench",
            "elf": _p.get("spike_elf", _p["elf"]),
            "verilator_elf": _p["elf"],
            "hex": _p["hex"],
            "trace": f"traces/{_p['name']}_spike.log",
        }

DEFAULT_BENCHMARK = "kyber"
RTL_ROOT = "targets/cv32e40p"


def _is_suite(name: str) -> bool:
    """Return True if the benchmark is a multi-program suite."""
    bm = BENCHMARKS.get(name, {})
    return "programs" in bm


def _suite_program_configs(name: str) -> list[dict[str, str]]:
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

    # Phase 2.8: route RTL emission through the portable Pipeline
    # path (targets/cv32e40p/portability_shim).  When False (the
    # default), RTLChangeSet.apply uses its in-tree legacy
    # emission code.  Set by ``--use-portability`` on main.py;
    # also picked up via the ``ARVIS_USE_PORTABILITY=1`` env var.
    use_portability: bool = False

    # Phase 6: route the HW_LOOP sweep's per-candidate sim+synth
    # through HWLoopVariantEvaluator + Pipeline._emit_variant +
    # VerilatorVerifier + YosysSynthesisFlow.  Implies
    # ``use_portability=True``.  Set by ``--use-pipeline-runner``;
    # also picked up via ``ARVIS_USE_PIPELINE_RUNNER=1``.
    use_pipeline_runner: bool = False

    # ── Core descriptor (loaded lazily from rtl_root) ──
    _core_descriptor: CoreDescriptor | None = field(default=None, repr=False, compare=False)

    # ── Detected toolchain (populated by toolchain.detect_toolchain) ──
    riscv_objdump: str | None = None
    riscv_gcc: str | None = None
    riscv_objcopy: str | None = None
    spike_bin: str | None = None
    verilator_bin: str | None = None
    yosys_bin: str | None = None

    def __post_init__(self):
        """Resolve paths from benchmark name if not explicitly set."""
        bm = BENCHMARKS.get(self.benchmark_name, BENCHMARKS[DEFAULT_BENCHMARK])
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
    def core_descriptor(self) -> CoreDescriptor:
        """Lazy-load the core descriptor from the RTL root directory.

        Looks for ``core_descriptor.yaml`` in ``self.rtl_root``.
        Falls back to a hardcoded CV32E40P default if not found.
        """
        if self._core_descriptor is None:
            from core_descriptor import CoreDescriptor

            self._core_descriptor = CoreDescriptor.load_for_target(self.rtl_root)
        return self._core_descriptor

    @classmethod
    def from_benchmark(cls, name: str) -> ToolConfig:
        """Create config for a specific benchmark."""
        if name not in BENCHMARKS:
            raise ValueError(f"Unknown benchmark: {name}. Available: {list(BENCHMARKS.keys())}")
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
    def suite_programs(self) -> list[dict[str, str]]:
        """Per-program configs for a suite. Empty for single-program benchmarks."""
        if not self.is_suite:
            return []
        return _suite_program_configs(self.benchmark_name)

    def for_program(self, prog: dict[str, str]) -> ToolConfig:
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
        if (
            spike_elf
            and _os.path.exists(f"{self.benchmark_dir}/{spike_elf}")
            and _os.path.exists(child.trace_path)
        ):
            child.elf_path = f"{self.benchmark_dir}/{spike_elf}"
        else:
            child.elf_path = f"{self.benchmark_dir}/{prog['elf']}"
        # Keep the suite's output_dir — RTL is shared
        return child
