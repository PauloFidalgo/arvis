"""
Verilator simulation runner for CV32E40P.

Compiles RTL + firmware, runs simulation, captures:
- Cycle counts from performance counters
- PASS/FAIL result
"""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SimulationResult:
    """Simulation results."""

    success: bool = False
    test_passed: Optional[bool] = None
    total_cycles: int = 0
    sim_time_seconds: float = 0.0
    return_code: int = -1
    stdout: str = ""
    stderr: str = ""
    error_message: str = ""


class VerilatorRunner:
    """Manages Verilator compilation and simulation of CV32E40P."""

    def __init__(self, config):
        self.cfg = config

    def build_sim(self, rtl_dir: str, output_dir: str | None = None, extra_flags: list | None = None) -> tuple:
        """Build Verilator simulation from RTL. Returns (success, sim_binary_path, output)."""
        rtl_path = Path(rtl_dir).resolve()
        core_root = rtl_path.parent if rtl_path.name == "rtl" else rtl_path

        bhv_dir = core_root / "bhv"
        if not bhv_dir.exists():
            bhv_dir = Path(self.cfg.rtl_root).resolve() / "bhv"

        # Prefer modified TB from output dir (has debug pragmas applied)
        tb_dir = core_root / "example_tb" / "core"
        tb_verilator = tb_dir / "verilator"
        if not tb_verilator.exists():
            # Fallback to original targets TB
            tb_dir = Path(self.cfg.rtl_root).resolve() / "example_tb" / "core"
            tb_verilator = tb_dir / "verilator"

        # Output directory for obj_dir
        if output_dir:
            obj_dir = Path(output_dir).resolve() / "obj_dir"
            obj_dir.mkdir(parents=True, exist_ok=True)
        else:
            obj_dir = tb_verilator / "obj_dir"

        # RTL packages and sources (order matters)
        rtl_pkgs = [
            rtl_path / "include" / "cv32e40p_apu_core_pkg.sv",
            rtl_path / "include" / "cv32e40p_fpu_pkg.sv",
            rtl_path / "include" / "cv32e40p_pkg.sv",
        ]
        rtl_srcs = [
            rtl_path / f
            for f in [
                "cv32e40p_if_stage.sv",
                "cv32e40p_cs_registers.sv",
                "cv32e40p_register_file_ff.sv",
                "cv32e40p_load_store_unit.sv",
                "cv32e40p_id_stage.sv",
                "cv32e40p_aligner.sv",
                "cv32e40p_decoder.sv",
                "cv32e40p_compressed_decoder.sv",
                "cv32e40p_fifo.sv",
                "cv32e40p_prefetch_buffer.sv",
                "cv32e40p_prefetch_controller.sv",
                "cv32e40p_obi_interface.sv",
                "cv32e40p_alu.sv",
                "cv32e40p_alu_div.sv",
                "cv32e40p_ff_one.sv",
                "cv32e40p_popcnt.sv",
                "cv32e40p_mult.sv",
                "cv32e40p_int_controller.sv",
                "cv32e40p_ex_stage.sv",
                "cv32e40p_hwloop_regs.sv",
                "cv32e40p_controller.sv",
                "cv32e40p_sleep_unit.sv",
                "cv32e40p_core.sv",
                "cv32e40p_top.sv",
            ]
        ]

        tb_pkgs = [tb_dir / "include" / "perturbation_pkg.sv"]
        tb_srcs = [
            bhv_dir / "cv32e40p_sim_clock_gate.sv",
            tb_dir / "dp_ram.sv",
            tb_dir / "amo_shim.sv",
            tb_dir / "riscv_gnt_stall.sv",
            tb_dir / "riscv_rvalid_stall.sv",
            tb_dir / "mm_ram.sv",
            tb_verilator / "cv32e40p_tb_subsystem.sv",
            tb_verilator / "tb_top_verilator.sv",
        ]

        cmd = [
            self.cfg.verilator_bin,
            "--cc",
            "--exe",
            "--build",
            "-j",
            "0",
            "-Wno-fatal",
            "-Wno-WIDTH",
            "-Wno-CASEINCOMPLETE",
            "-Wno-UNOPTFLAT",
            "-Wno-MULTIDRIVEN",
            "-Wno-BLKANDNBLK",
            "-Wno-IMPLICIT",
            "--top-module",
            "tb_top",
            "--Mdir",
            str(obj_dir),
            f"+incdir+{rtl_path}/include",
            f"+incdir+{bhv_dir}/include",
        ]

        for f in rtl_pkgs + tb_pkgs + rtl_srcs + tb_srcs:
            if f.exists():
                cmd.append(str(f))

        # Add extra flags (e.g. -GHW_LOOP=1)
        if extra_flags:
            cmd.extend(extra_flags)

        sim_main = tb_verilator / "sim_main.cpp"
        if sim_main.exists():
            cmd.append(str(sim_main))

        sim_binary = obj_dir / "Vtb_top"

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(tb_verilator))
            output = result.stdout + result.stderr
            if result.returncode == 0 and sim_binary.exists():
                return True, str(sim_binary), output
            return False, None, output
        except FileNotFoundError:
            return False, None, f"{self.cfg.verilator_bin} not found"
        except subprocess.TimeoutExpired:
            return False, None, "Build timed out (300s)"

    def run_sim(self, sim_binary: str, firmware_hex: str, max_cycles: int | None = None) -> SimulationResult:
        """Run a built simulation with firmware."""
        result = SimulationResult()

        if not os.path.exists(sim_binary):
            result.error_message = f"Simulator not found: {sim_binary}"
            return result

        if not os.path.exists(firmware_hex):
            result.error_message = f"Firmware not found: {firmware_hex}"
            return result

        max_cycles = max_cycles or self.cfg.verilator_timeout_cycles

        cmd = [
            sim_binary,
            f"+firmware={firmware_hex}",
            f"+maxcycles={max_cycles}",
        ]

        start_time = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.cfg.sim_timeout_seconds)
            result.sim_time_seconds = time.time() - start_time
            result.return_code = proc.returncode
            result.stdout = (
                proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, bytes) else proc.stdout
            )
            result.stderr = (
                proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, bytes) else proc.stderr
            )
            result.success = proc.returncode == 0
            self._parse_output(result)
        except subprocess.TimeoutExpired:
            result.error_message = f"Simulation timed out ({self.cfg.sim_timeout_seconds}s)"

        return result

    def _parse_output(self, result: SimulationResult):
        """Parse simulation output for results."""
        output = result.stdout + result.stderr

        # Check PASS/FAIL from testbench
        if re.search(r"TESTS PASSED|EXIT SUCCESS", output, re.I):
            result.test_passed = True
        elif re.search(r"TESTS FAILED|EXIT FAILURE", output, re.I):
            result.test_passed = False
        elif re.search(r"PASS:\s*keys\s*match", output, re.I):
            result.test_passed = True
        elif re.search(r"FAIL:\s*keys", output, re.I):
            result.test_passed = False

        # Parse cycle count
        m = re.search(r"after\s+(\d+)\s+cycles", output, re.I)
        if m:
            result.total_cycles = int(m.group(1))

        m = re.search(r"Timeout after\s+(\d+)\s+cycles", output, re.I)
        if m:
            result.total_cycles = int(m.group(1))
