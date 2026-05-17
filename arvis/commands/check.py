"""Subcommand: verify that all external tools ARVIS depends on are installed.

Usage::

    arvis check        # run all checks, exit non-zero if any required tool missing
    arvis check -v     # also print the resolved binary path for every tool

Each entry in :data:`_TOOLS` defines a binary name, whether it is required,
optional version-extraction regex and minimum version, and a one-line
install hint shown when the tool is missing. The check tolerates tools
that simply do not respond to ``--version``: existence on PATH is enough
for those.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from arvis.cli import (
    Colors,
    print_error,
    print_info,
    print_section,
    print_success,
)


@dataclass
class Tool:
    name: str
    candidates: list[str]
    required: bool = True
    version_arg: str = "--version"
    version_re: str | None = None
    min_version: tuple[int, ...] | None = None
    install_hint: str = ""
    # Some tools (Docker daemon) need a runtime check beyond a path lookup.
    runtime_check: list[str] | None = field(default=None)


# Order matters: surface required tools first so the user sees the first
# blocker at the top of the output.
_TOOLS: list[Tool] = [
    Tool(
        name="Python",
        candidates=["python3", "python"],
        version_arg="--version",
        version_re=r"Python (\d+)\.(\d+)\.(\d+)",
        min_version=(3, 12, 0),
        install_hint="Install Python 3.12 or newer (e.g. `sudo apt install python3.12`).",
    ),
    Tool(
        name="uv",
        candidates=["uv"],
        version_arg="--version",
        version_re=r"uv (\d+)\.(\d+)\.(\d+)",
        install_hint="Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`",
    ),
    Tool(
        name="RISC-V GCC",
        candidates=[
            "riscv32-unknown-elf-gcc",
            "riscv64-unknown-elf-gcc",
            "riscv-none-elf-gcc",
            "riscv-none-embed-gcc",
            "riscv32-none-elf-gcc",
        ],
        version_arg="--version",
        version_re=r"\bgcc[^ ]* \(.*\) (\d+)\.(\d+)\.(\d+)",
        install_hint=(
            "Install the RISC-V cross-compiler. "
            "Use `tools/setup-linux.sh` or build from "
            "https://github.com/riscv-collab/riscv-gnu-toolchain "
            "(target rv32imc, ABI ilp32)."
        ),
    ),
    Tool(
        name="RISC-V objdump",
        candidates=[
            "riscv32-unknown-elf-objdump",
            "riscv64-unknown-elf-objdump",
            "riscv-none-elf-objdump",
            "riscv-none-embed-objdump",
            "riscv32-none-elf-objdump",
        ],
        version_arg="--version",
        install_hint="Comes with the RISC-V GCC binutils package.",
    ),
    Tool(
        name="RISC-V objcopy",
        candidates=[
            "riscv32-unknown-elf-objcopy",
            "riscv64-unknown-elf-objcopy",
            "riscv-none-elf-objcopy",
            "riscv-none-embed-objcopy",
            "riscv32-none-elf-objcopy",
        ],
        version_arg="--version",
        install_hint="Comes with the RISC-V GCC binutils package.",
    ),
    Tool(
        name="Spike",
        candidates=["spike"],
        version_arg="--help",
        install_hint=("Build the Spike ISA simulator: https://github.com/riscv-software-src/riscv-isa-sim"),
    ),
    Tool(
        name="Verilator",
        candidates=["verilator"],
        version_arg="--version",
        version_re=r"Verilator (\d+)\.(\d+)",
        min_version=(5, 0),
        install_hint=(
            "Install Verilator >= 5.020 "
            "(`sudo apt install verilator` on recent Ubuntu, "
            "or build from https://github.com/verilator/verilator)."
        ),
    ),
    Tool(
        name="Yosys",
        candidates=["yosys"],
        version_arg="-V",
        version_re=r"Yosys (\d+)\.(\d+)",
        min_version=(0, 30),
        install_hint=(
            "Install Yosys >= 0.63 (`sudo apt install yosys` or build from https://github.com/YosysHQ/yosys)."
        ),
    ),
    Tool(
        name="sv2v",
        candidates=["sv2v"],
        version_arg="--version",
        install_hint=(
            "Install sv2v: download a release from https://github.com/zachjs/sv2v/releases or `cabal install sv2v`."
        ),
    ),
    Tool(
        name="Docker (CLI)",
        candidates=["docker"],
        version_arg="--version",
        version_re=r"Docker version (\d+)\.(\d+)",
        min_version=(20, 0),
        install_hint=(
            "Install Docker: `curl -fsSL https://get.docker.com | sudo sh` and add yourself to the `docker` group."
        ),
        runtime_check=["docker", "info"],
    ),
    Tool(
        name="make",
        candidates=["make"],
        version_arg="--version",
        required=False,
        install_hint="Only needed to rebuild the GCC plugin: `sudo apt install build-essential`.",
    ),
    Tool(
        name="LibreLane",
        candidates=["librelane", "openlane2"],
        version_arg="--version",
        required=False,
        install_hint=(
            "Optional, only for the ASIC place-and-route flow. "
            "Install LibreLane: https://github.com/librelane/librelane"
        ),
    ),
    Tool(
        name="Vivado",
        candidates=["vivado"],
        version_arg="-version",
        required=False,
        install_hint=("Optional, only for the FPGA place-and-route flow. Install AMD/Xilinx Vivado >= 2025.2."),
    ),
]


def _find(candidates: list[str]) -> str | None:
    for c in candidates:
        path = shutil.which(c)
        if path:
            return path
    return None


def _read_version(path: str, arg: str, regex: str | None) -> tuple[str, tuple[int, ...] | None]:
    """Run `<path> <arg>` and extract the version. Returns (raw_text, parsed)."""
    try:
        out = subprocess.run([path, arg], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ("", None)
    raw = (out.stdout or out.stderr or "").splitlines()
    raw_text = raw[0] if raw else ""
    if not regex:
        return (raw_text, None)
    m = re.search(regex, "\n".join(raw))
    if not m:
        return (raw_text, None)
    return (raw_text, tuple(int(x) for x in m.groups()))


def _ge(parsed: tuple[int, ...] | None, minimum: tuple[int, ...]) -> bool:
    if parsed is None:
        return True  # we could not parse, give the benefit of the doubt
    pad = max(len(parsed), len(minimum))
    p = parsed + (0,) * (pad - len(parsed))
    m = minimum + (0,) * (pad - len(minimum))
    return p >= m


def _check_one(tool: Tool, verbose: bool) -> tuple[str, str | None]:
    """Check a single tool. Returns (status, error_message).

    status: "ok", "old", "missing", "missing-optional", "no-daemon"
    """
    path = _find(tool.candidates)
    if path is None:
        return ("missing-optional" if not tool.required else "missing", None)

    raw, parsed = _read_version(path, tool.version_arg, tool.version_re)

    if tool.runtime_check:
        try:
            rc = subprocess.run(tool.runtime_check, capture_output=True, text=True, timeout=10).returncode
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            rc = 1
        if rc != 0:
            return ("no-daemon", path)

    if tool.min_version and not _ge(parsed, tool.min_version):
        return ("old", f"{path} ({raw or 'version unknown'})")

    suffix = path
    if verbose and raw:
        suffix = f"{path}  ({raw})"
    return ("ok", suffix)


def _icon(status: str) -> str:
    return {
        "ok": f"{Colors.GREEN}✓{Colors.END}",
        "old": f"{Colors.YELLOW}⚠{Colors.END}",
        "missing": f"{Colors.RED}✗{Colors.END}",
        "missing-optional": f"{Colors.DIM}○{Colors.END}",
        "no-daemon": f"{Colors.YELLOW}⚠{Colors.END}",
    }[status]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arvis check",
        description="Verify that all external tools ARVIS depends on are installed.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print resolved paths and versions.")
    args = parser.parse_args(argv)

    print_section("ARVIS prerequisite check")

    failures: list[Tool] = []
    warnings: list[tuple[Tool, str]] = []

    for tool in _TOOLS:
        status, info = _check_one(tool, verbose=args.verbose)
        icon = _icon(status)
        label = f"{tool.name:<20}"
        if status == "ok":
            print(f"  {icon} {label} {info or ''}")
        elif status == "old":
            min_v = ".".join(str(x) for x in (tool.min_version or ()))
            print(f"  {icon} {label} found but older than {min_v} — {info or ''}")
            warnings.append((tool, f"requires >= {min_v}"))
        elif status == "no-daemon":
            print(f"  {icon} {label} CLI present but daemon not reachable")
            warnings.append((tool, "Docker daemon not running. Start it or add yourself to the `docker` group."))
        elif status == "missing-optional":
            print(f"  {icon} {label} not installed (optional)")
        else:  # missing
            print(f"  {icon} {label} NOT FOUND")
            failures.append(tool)

    print()
    if failures:
        print_error(f"{len(failures)} required tool(s) missing.")
        for tool in failures:
            cands = ", ".join(tool.candidates[:2]) + ("..." if len(tool.candidates) > 2 else "")
            print(f"  • {Colors.BOLD}{tool.name}{Colors.END}  (looked for: {cands})")
            if tool.install_hint:
                print(f"    {tool.install_hint}")
        print()
        print_info("Tip: on Ubuntu / Debian, run `tools/setup-linux.sh` to install everything in one step.")
        return 1

    if warnings:
        print(f"{Colors.YELLOW}{len(warnings)} warning(s):{Colors.END}")
        for tool, msg in warnings:
            print(f"  • {tool.name}: {msg}")
            if tool.install_hint:
                print(f"    {tool.install_hint}")
        print()
        # Warnings still allow continuing; exit 0 unless required min-versions matter.
        # Treat outdated required tools as a failure for safety.
        for tool, _ in warnings:
            if tool.required and tool.min_version is not None:
                print_error("A required tool is older than the supported minimum.")
                return 2

    print_success("All required tools are installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
