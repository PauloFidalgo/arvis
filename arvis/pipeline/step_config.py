"""Pipeline step configuration — single source of truth for RTL, sim, and run parameters.

Architecture:
    RTLConfig  — patched RTL files (4 variants: 2x2 matrix of fused × hwloop)
    SimConfig  — Verilator binary for a specific (RTL, HW_LOOP) pair
    RunConfig  — a hex file to simulate on a specific sim

RTL is patched once per config.  Sim is built once per (RTL, hw_loop).
Multiple runs can share the same sim.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.codegen.hwloop.generator import HWLoopEncoding


# ── Docker image names ──────────────────────────────────────────────────────

IMG_PREBUILT = "riscv-gcc-prebuilt"
IMG_FUSED = "custom-riscv-gcc-merged"
IMG_HWLOOP = "riscv-gcc-merged-hwloop"
IMG_HWLOOP_ONLY = "riscv-gcc-hwloop"


# ── Compilation flag presets ────────────────────────────────────────────────

# Baseline / fusion: Makefile's own flags (includes -funroll-loops)
FLAGS_MAKEFILE = "makefile"

# HWLoop .s compilation: hardcoded, no -funroll-loops
FLAGS_HWLOOP = ["-O3", "-fno-builtin", "-fno-common"]


# ── RTL Config ──────────────────────────────────────────────────────────────


@dataclass
class RTLConfig:
    """Defines a patched RTL variant.  Patching is binary for hwloop (on/off).

    4 possible variants (2×2 matrix):
        baseline_pruned:  fused=False, hwloop=False
        fused_pruned:     fused=True,  hwloop=False
        hwloop_pruned:    fused=False, hwloop=True
        all:              fused=True,  hwloop=True
    """

    name: str
    has_fused: bool
    has_hwloop: bool
    hwloop_encoding: HWLoopEncoding | None = None
    prune_from_elf: str | None = None  # elf path to derive prune config from
    rtl_dir: str = ""  # filled after apply()

    @property
    def dir_name(self) -> str:
        return f"rtl_{self.name}"

    def __repr__(self) -> str:
        return f"RTL({self.name}, fused={self.has_fused}, hwloop={self.has_hwloop}, rtl_dir={self.rtl_dir or '?'})"


# ── Sim Config ──────────────────────────────────────────────────────────────


@dataclass
class SimConfig:
    """Verilator sim binary for a specific (RTL, HW_LOOP) pair.

    The RTL files are identical across HW_LOOP values — only the Verilator
    -GHW_LOOP=N parameter changes.  But HW_LOOP=0 means no hwloop in RTL.
    """

    rtl: RTLConfig
    hw_loop: int  # Verilator -GHW_LOOP=N
    sim_bin: str = ""  # filled after build
    sim_dir: str = ""  # filled after build

    @property
    def label(self) -> str:
        return f"{self.rtl.name}_hw{self.hw_loop}"

    def resolve_sim_dir(self, output_dir: str) -> str:
        self.sim_dir = os.path.join(output_dir, f"sim_{self.label}")
        return self.sim_dir

    def __repr__(self) -> str:
        return f"Sim({self.label}, bin={'✓' if self.sim_bin else '✗'})"


# ── Run Config ──────────────────────────────────────────────────────────────


# ── Sim Cache ───────────────────────────────────────────────────────────────


class SimCache:
    """Build sim once per (RTL, hw_loop) pair, reuse across runs."""

    def __init__(self) -> None:
        self._cache: dict[str, SimConfig] = {}

    def get_or_create(self, rtl: RTLConfig, hw_loop: int) -> SimConfig:
        key = f"{rtl.name}_hw{hw_loop}"
        if key not in self._cache:
            self._cache[key] = SimConfig(rtl=rtl, hw_loop=hw_loop)
        return self._cache[key]

    def all(self) -> list[SimConfig]:
        return list(self._cache.values())


# ── Pipeline Steps ──────────────────────────────────────────────────────────
