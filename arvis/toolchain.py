"""
RISC-V toolchain auto-detection and benchmark build helpers.

Finds riscv-*-objdump, riscv-*-gcc, riscv-*-objcopy, and spike on PATH.
Provides functions to build benchmarks and generate execution traces.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from arvis.cli import print_error, print_success, print_warning
from arvis.config import ToolConfig


def _find_tool(candidates: list) -> Optional[str]:
    """Find first available tool from candidates list."""
    for c in candidates:
        if shutil.which(c):
            return c
    return None


def detect_toolchain(cfg: ToolConfig) -> None:
    """Auto-detect all RISC-V tools and update config in-place."""
    cfg.riscv_objdump = _find_tool(
        [
            "riscv32-unknown-elf-objdump",
            "riscv64-unknown-elf-objdump",
            "riscv-none-elf-objdump",
            "riscv-none-embed-objdump",
            "riscv32-none-elf-objdump",
        ]
    )
    cfg.riscv_gcc = _find_tool(
        [
            "riscv32-unknown-elf-gcc",
            "riscv64-unknown-elf-gcc",
            "riscv-none-elf-gcc",
            "riscv-none-embed-gcc",
            "riscv32-none-elf-gcc",
        ]
    )
    cfg.riscv_objcopy = _find_tool(
        [
            "riscv32-unknown-elf-objcopy",
            "riscv64-unknown-elf-objcopy",
            "riscv-none-elf-objcopy",
            "riscv-none-embed-objcopy",
            "riscv32-none-elf-objcopy",
        ]
    )
    cfg.spike_bin = _find_spike()
    cfg.verilator_bin = shutil.which("verilator")
    cfg.yosys_bin = shutil.which("yosys")


def _find_spike() -> Optional[str]:
    """Find the Spike ISA simulator binary.

    Checks PATH first, then common Homebrew Cellar paths and /tmp build dirs.
    """
    # 1. On PATH
    on_path = shutil.which("spike")
    if on_path:
        return on_path

    # 2. Homebrew (macOS) — brew --prefix is slow, check common paths first
    for path in [
        "/opt/homebrew/Cellar/riscv-isa-sim/main/bin/spike",
        "/opt/homebrew/bin/spike",
        "/usr/local/bin/spike",
    ]:
        if os.path.exists(path):
            return path

    # 3. Try brew --prefix (slower but handles non-standard installs)
    try:
        result = subprocess.run(
            ["brew", "--prefix", "riscv-isa-sim"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            spike = os.path.join(result.stdout.strip(), "bin", "spike")
            if os.path.exists(spike):
                return spike
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 4. /tmp build directories (from native Spike rebuild)
    for path in [
        "/tmp/spike-src/build/spike",
        "/tmp/spike-build/spike",
    ]:
        if os.path.exists(path):
            return path

    return None


def build_benchmark(cfg: ToolConfig) -> bool:
    """Build the benchmark ELF if it doesn't exist."""
    if os.path.exists(cfg.elf_path):
        size = os.path.getsize(cfg.elf_path)
        print_success(f"ELF already built: {cfg.elf_path} ({size:,} bytes)")
        return True

    print_warning(f"ELF not found: {cfg.elf_path}")
    print("  Attempting to build...")

    makefile = os.path.join(cfg.benchmark_dir, "Makefile")
    src_dir = os.path.join(cfg.benchmark_dir, "src")

    if os.path.exists(makefile):
        return _build_with_makefile(cfg)
    elif os.path.isdir(src_dir) and any(f.endswith(".c") for f in os.listdir(src_dir)):
        return _build_manually(cfg)
    else:
        print_error(f"No source code found in {cfg.benchmark_dir}")
        return False


def _build_with_makefile(cfg: ToolConfig) -> bool:
    if not cfg.riscv_gcc:
        print_error("Cannot build: no RISC-V GCC found")
        return False
    prefix = cfg.riscv_gcc.replace("gcc", "")
    print(f"  Building with Makefile (CROSS_COMPILE={prefix})...")
    try:
        result = subprocess.run(
            ["make", "-C", cfg.benchmark_dir, "all", f"CROSS_COMPILE={prefix}"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and os.path.exists(cfg.elf_path):
            size = os.path.getsize(cfg.elf_path)
            print_success(f"Build successful: {cfg.elf_path} ({size:,} bytes)")
            return True
        print_error("Build failed:")
        if result.stderr:
            for line in result.stderr.strip().splitlines()[-20:]:
                print(f"     {line}")
        return False
    except FileNotFoundError:
        print_error("'make' not found")
        return False
    except subprocess.TimeoutExpired:
        print_error("Build timed out (120s)")
        return False


def _build_manually(cfg: ToolConfig) -> bool:
    if not cfg.riscv_gcc:
        print_error("Cannot build: no RISC-V GCC found")
        return False

    src_dir = os.path.join(cfg.benchmark_dir, "src")
    inc_dir = os.path.join(cfg.benchmark_dir, "include")
    build_dir = os.path.join(cfg.benchmark_dir, "build")
    os.makedirs(build_dir, exist_ok=True)

    c_files = sorted(Path(src_dir).glob("*.c"))
    main_c = Path(cfg.benchmark_dir) / "main.c"
    if main_c.exists():
        c_files.append(main_c)
    asm_files = list(Path(cfg.benchmark_dir).glob("*.S"))

    if not c_files:
        print_error(f"No .c files found in {src_dir}")
        return False

    print(f"  Compiling {len(c_files)} C files + {len(asm_files)} ASM files...")
    cflags = [
        "-march=rv32imc",
        "-mabi=ilp32",
        "-Os",
        "-g",
        "-Wall",
        "-ffunction-sections",
        "-fdata-sections",
        f"-I{inc_dir}",
        "-nostdlib",
        "-ffreestanding",
    ]

    obj_files = []
    for src in c_files:
        obj = os.path.join(build_dir, src.stem + ".o")
        cmd = [cfg.riscv_gcc] + cflags + ["-c", str(src), "-o", obj]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print_error(f"Failed to compile {src.name}:")
            for line in result.stderr.strip().splitlines()[-10:]:
                print(f"     {line}")
            return False
        obj_files.append(obj)

    for src in asm_files:
        obj = os.path.join(build_dir, src.stem + ".o")
        cmd = [cfg.riscv_gcc, "-march=rv32imc", "-mabi=ilp32", "-c", str(src), "-o", obj]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print_error(f"Failed to assemble {src.name}:")
            for line in result.stderr.strip().splitlines()[-5:]:
                print(f"     {line}")
            return False
        obj_files.append(obj)

    link_script = os.path.join(cfg.benchmark_dir, "link.ld")
    ldflags = [
        "-march=rv32imc",
        "-mabi=ilp32",
        "-nostartfiles",
        "-nostdlib",
        "-Wl,--gc-sections",
        "-lgcc",
    ]
    if os.path.exists(link_script):
        ldflags.insert(0, f"-T{link_script}")

    print("  Linking...")
    cmd = [cfg.riscv_gcc] + ldflags + obj_files + ["-o", cfg.elf_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print_error("Link failed:")
        for line in result.stderr.strip().splitlines()[-10:]:
            print(f"     {line}")
        return False

    if os.path.exists(cfg.elf_path):
        size = os.path.getsize(cfg.elf_path)
        print_success(f"Build successful: {cfg.elf_path} ({size:,} bytes)")
        return True
    return False


def generate_trace(cfg: ToolConfig) -> bool:
    """Generate execution trace using Spike if available."""
    if os.path.exists(cfg.trace_path):
        lines = sum(1 for _ in open(cfg.trace_path, "r", errors="ignore"))
        if lines > 100:
            print_success(f"Trace already exists: {cfg.trace_path} ({lines:,} lines)")
            return True
        print_warning(f"Trace exists but looks too short ({lines} lines), regenerating...")

    if not cfg.spike_bin:
        print_warning("Spike not found — cannot generate trace")
        print("     Will use static estimation instead.")
        return False

    if not os.path.exists(cfg.elf_path):
        print_error(f"Cannot generate trace: ELF not found at {cfg.elf_path}")
        return False

    traces_dir = os.path.join(cfg.benchmark_dir, "traces")
    os.makedirs(traces_dir, exist_ok=True)

    print("  Running Spike to generate execution trace...")
    print(f"    Spike: {cfg.spike_bin}")
    print(f"    ISA:   {cfg.spike_isa}")
    print(f"    ELF:   {cfg.elf_path}")

    pk_path = _find_tool(["pk"]) or shutil.which("riscv32-unknown-elf-pk")
    if not pk_path:
        for candidate in [
            "/opt/riscv/riscv32-unknown-elf/bin/pk",
            "/usr/local/share/riscv-tests/pk",
            os.path.expanduser("~/.local/share/riscv/pk"),
        ]:
            if os.path.exists(candidate):
                pk_path = candidate
                break

    approaches = []
    if pk_path:
        approaches.append(
            {
                "name": "Spike + proxy kernel",
                "cmd": [
                    cfg.spike_bin,
                    f"--isa={cfg.spike_isa}",
                    "-l",
                    "--log-commits",
                    pk_path,
                    cfg.elf_path,
                ],
            }
        )
    approaches.extend(
        [
            {
                "name": "Spike bare-metal (0x80000000)",
                "cmd": [
                    cfg.spike_bin,
                    f"--isa={cfg.spike_isa}",
                    "-l",
                    "--log-commits",
                    "-m0x80000000:0x400000",
                    cfg.elf_path,
                ],
            },
            {
                "name": "Spike bare-metal (0x10000)",
                "cmd": [
                    cfg.spike_bin,
                    f"--isa={cfg.spike_isa}",
                    "-l",
                    "--log-commits",
                    "-m0x10000:0x200000",
                    cfg.elf_path,
                ],
            },
            {
                "name": "Spike bare-metal (default memory map)",
                "cmd": [
                    cfg.spike_bin,
                    f"--isa={cfg.spike_isa}",
                    "-l",
                    "--log-commits",
                    cfg.elf_path,
                ],
            },
        ]
    )

    for approach in approaches:
        print(f"    Trying: {approach['name']}...")
        try:
            result = subprocess.run(
                approach["cmd"],
                capture_output=True,
                text=True,
                timeout=120,
                errors="replace",
            )
            log_content = result.stderr
            commit_lines = [line for line in log_content.splitlines() if re.match(r"core\s+\d+:\s+0x[0-9a-f]+", line)]
            if len(commit_lines) > 100:
                with open(cfg.trace_path, "w") as f:
                    f.write(log_content)
                n_mem_lines = sum(1 for line in commit_lines if " mem " in line)
                n_reg_lines = sum(1 for line in commit_lines if re.search(r"x\d+\s", line))
                print_success(f"    ✅ Trace generated: {len(commit_lines):,} instructions")
                print(f"       Register writebacks: {n_reg_lines:,}")
                print(f"       Memory accesses:     {n_mem_lines:,}")
                return True
            else:
                if result.returncode != 0:
                    err_preview = (result.stderr[:200] + result.stdout[:200]).strip()
                    if err_preview:
                        print(f"       Exit code {result.returncode}: {err_preview[:100]}")
                else:
                    print(f"       Only {len(commit_lines)} commit lines — too few")
        except subprocess.TimeoutExpired:
            print("       Timed out after 120s (program may not terminate)")
        except FileNotFoundError:
            print("       Spike binary not found")
            break

    print_warning("Could not generate trace with Spike")
    print("     Will use static estimation instead.")
    return False


def check_prerequisites(cfg: ToolConfig) -> bool:
    """Verify and prepare everything needed. Returns True if trace is available."""
    from arvis.cli import print_section

    print_section("CHECKING PREREQUISITES")

    detect_toolchain(cfg)
    errors = []

    if cfg.riscv_objdump:
        print_success(f"objdump:   {cfg.riscv_objdump}")
    else:
        print_error("objdump:   not found")
        errors.append("RISC-V objdump not found.")

    if cfg.riscv_gcc:
        print_success(f"gcc:       {cfg.riscv_gcc}")
    else:
        print_warning("gcc:       not found (cannot build benchmark)")

    if cfg.spike_bin:
        print_success(f"spike:     {cfg.spike_bin}")
    else:
        print_warning("spike:     not found (cannot generate trace)")

    if cfg.verilator_bin:
        print_success(f"verilator: {cfg.verilator_bin}")
    else:
        print_warning("verilator: not found (simulation will be skipped)")

    if cfg.yosys_bin:
        print_success(f"yosys:     {cfg.yosys_bin}")
    else:
        print_warning("yosys:     not found (synthesis area estimation will be skipped)")

    rtl_dir = os.path.join(cfg.rtl_root, "rtl")
    if os.path.isdir(rtl_dir):
        sv_count = len([f for f in os.listdir(rtl_dir) if f.endswith(".sv")])
        print_success(f"RTL:       {rtl_dir} ({sv_count} .sv files)")
    else:
        print_error(f"RTL:       {rtl_dir} NOT FOUND")
        errors.append(f"CV32E40P RTL not found at {rtl_dir}")

    if errors:
        import sys

        print(f"\n{'─' * 70}")
        print("  ERRORS — fix before running:")
        for e in errors:
            print(f"\n  {e}")
        sys.exit(1)

    print()
    from arvis.cli import print_subsection

    print_subsection("BENCHMARK BUILD")
    if cfg.is_suite:
        # Suite: verify all program ELFs exist (built by Makefile)
        missing = []
        for prog in cfg.suite_programs:
            elf = os.path.join(cfg.benchmark_dir, prog["elf"])
            if not os.path.exists(elf):
                missing.append(prog["name"])
        if missing:
            print_warning(f"Missing ELFs: {missing}")
            print(f"  Building with: make -C {cfg.benchmark_dir} all")
            ret = subprocess.run(
                ["make", "-C", cfg.benchmark_dir, "all"],
                capture_output=True,
                timeout=120,
            )
            if ret.returncode != 0:
                print_error("Build failed")
                import sys

                sys.exit(1)
        print_success(f"Suite: {len(cfg.suite_programs)} program ELFs ready")
    else:
        elf_ok = build_benchmark(cfg)
        if not elf_ok:
            import sys

            print("\n  Cannot proceed without ELF binary.")
            sys.exit(1)

    print()
    from arvis.cli import print_subsection

    print_subsection("TRACE GENERATION")
    if cfg.is_suite:
        print("  Suite mode: trace generation skipped (static analysis only)")
        return False
    trace_ok = generate_trace(cfg)

    print()
    return trace_ok
