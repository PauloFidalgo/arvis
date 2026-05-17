# ARVIS

**Automated RISC-V Workload-Specific Specialisation**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

<p align="center">
  <img src="docs/arvis.svg" width="600" alt="ARVIS logo">
</p>


ARVIS is an automated toolchain that produces a workload-specialised
[CV32E40P](https://github.com/openhwgroup/cv32e40p) RISC-V core from
application source code, with no manual RTL editing. It composes three
optimisations on a shared baseline:

1. **Static pruning** — removes hardware features (functional units,
   decoder paths, register file ports, debug, interrupt logic) the workload
   never exercises.
2. **Compiler-integrated instruction fusion** — discovers frequently
   co-occurring instruction pairs in the binary, generates a per-benchmark
   GCC machine description with custom instructions, and extends the RTL
   to execute them in a single cycle.
3. **Parameterisable hardware loops** — patches the assembly to use a
   custom `hwloop` instruction set and synthesises a hardware loop unit
   with one to eight nesting levels.

The toolchain takes application source code, runs the analysis pipeline,
emits the modified RTL plus a ready-to-run binary, and reports
post-place-and-route results on both ASIC (SkyWater 130 nm) and
FPGA (Spartan-7).

---

## Repository layout

```
arvis/                  Main Python package
├── cli.py              Coloured CLI helpers
├── config.py           Tool configuration and benchmark registry
├── toolchain.py        External tool detection and prerequisites
├── core_descriptor.py  CV32E40P feature catalogue
├── main.py             Pipeline entry point
├── pipeline/           Orchestration and per-phase drivers
├── analysis/           Workload analysis (instructions, loops, fusion)
├── codegen/            RTL, GCC, hardware-loop code generation
├── simulation/         Verilator runner
├── synthesis/          Yosys synthesis driver
├── report/             HTML / text reports
└── commands/           CLI subcommands (e.g. add-benchmark)

targets/
├── cv32e40p/           Modified CV32E40P RTL with ARVIS pragmas
└── benchmarks/         Default `minimal` benchmark + space for your own.
                       Add new benchmarks with `arvis add-benchmark <name>`.

tools/
├── gcc-plugin/         GCC plugin (GPL-3, see its own LICENSE)
├── hwloop-docker/      Docker context for the hardware-loop GCC build
├── setup-linux.sh      One-shot installer for Ubuntu / Debian
├── setup-linux-native.sh   No-Docker variant of the installer
├── patch-riscv-md.sh   Helper that patches GCC's riscv.md for fused insns
└── *.Dockerfile        Containers for the cross-compile toolchain
```

---

## Quick install (Ubuntu / Debian)

A one-shot installer is provided for Ubuntu 22.04 and 24.04:

```bash
bash tools/setup-linux.sh
```

The script installs system packages (build toolchain, Python 3, Docker,
Verilator, Yosys, sv2v, `uv`) and pre-builds the Docker images for the
custom GCC and the hardware-loop GCC variant. Re-running it is safe: it
skips anything that is already installed.

If you would rather install everything natively (no Docker), use
`tools/setup-linux-native.sh` instead. That script builds the RISC-V
toolchains under `/opt/riscv` and takes longer but avoids the Docker
layer.

After the installer finishes, jump straight to [Quick start](#quick-start)
below.

## Manual prerequisites

If you cannot run the installer, the pipeline expects the following on
your `PATH`:

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.12 | Runs the pipeline |
| `uv` | recent | Python dependency manager |
| `riscv32-unknown-elf-gcc` | recent | Cross-compiler (`rv32imc_zicsr`, ABI `ilp32`) with newlib |
| Docker | ≥ 20 | Builds the custom GCC images for fusion and hardware loops |
| Verilator | ≥ 5.020 | RTL simulator |
| Yosys | ≥ 0.63 | Technology-independent synthesis area estimates |
| sv2v | ≥ 0.0.13 | SystemVerilog → Verilog conversion |
| `pyslang` | ≥ 10.0 | SystemVerilog parser (Python package, installed by `uv sync`) |

### Optional, only for full place-and-route

| Tool | Version | Purpose |
|---|---|---|
| LibreLane | ≥ 3.0.2 | ASIC flow on SkyWater 130 nm |
| SkyWater Open Source PDK | latest | Required by LibreLane (set `SKY130_PDK_ROOT`) |
| Vivado | 2025.2 | FPGA flow on Spartan-7 |

The SkyWater PDK is **not** bundled in this repository.

## Installation

Clone the repository:

```bash
git clone https://github.com/PauloFidalgo/arvis.git
cd arvis
```

Create a virtual environment and install Python dependencies:

```bash
uv venv
uv sync
```

(Or `python -m venv .venv && pip install -e .` if you do not use `uv`.)

Build the GCC plugin (only needed for the fusion phase):

```bash
make -C tools/gcc-plugin
```

### Putting `arvis` on your PATH

There are three ways to invoke the CLI:

1. **Install as a global `uv` tool (recommended).** This puts the
   `arvis` binary at `~/.local/bin/arvis` so you can run it from any
   directory:

   ```bash
   uv tool install .
   arvis check
   arvis run --benchmark minimal
   ```

   When you pull new commits, refresh the install with
   `uv tool install . --reinstall`.

   `tools/setup-linux.sh` and `tools/setup-linux-native.sh` already do
   this on your behalf at the end of the script.

2. **Activate the virtualenv** (per shell):

   ```bash
   source .venv/bin/activate
   arvis check
   ```

3. **Use the `uv run` prefix** without activating or installing:

   ```bash
   uv run arvis check
   ```

The rest of this README uses the bare `arvis ...` form. If you have not
installed it as a tool and have not activated the venv, prepend `uv run`
to every command.

---

## Quick start

The repository ships with a single `minimal` benchmark that acts as a
template. Before the first run, verify that all the external tools the
pipeline depends on are reachable on your `PATH`:

```bash
arvis check
```

This prints a green tick for each tool it finds (`riscv32-unknown-elf-gcc`,
Spike, Verilator, Yosys, sv2v, Docker, ...) and red crosses with concrete
install hints for anything missing. Optional tools (LibreLane, Vivado)
are reported but do not cause a non-zero exit. Add `-v` to see resolved
binary paths and version strings.

Once the check passes, run the full pipeline on `minimal`:

```bash
arvis run --benchmark minimal
```

`run` is the default subcommand, so this is equivalent:

```bash
arvis --benchmark minimal
```

Run only the pruning phase:

```bash
arvis run --benchmark minimal --phases pruning
```

Run pruning + fusion:

```bash
arvis run --benchmark minimal --phases fusion
```

### Adding your own benchmark

To scaffold a new benchmark from the `minimal` template:

```bash
arvis add-benchmark <name>
```

This creates `targets/benchmarks/<name>/` populated with `main.c`,
`crt0.S`, `crt0_spike.S`, `link.ld`, `link_spike.ld`, `Makefile`, and a
`benchmark.yaml` descriptor. The benchmark is auto-registered the next
time `arvis` runs because `arvis.config` discovers benchmarks by scanning
`targets/benchmarks/*/benchmark.yaml`.

Edit `main.c` with your workload, then run:

```bash
arvis run --benchmark <name>
```

### Formatting and linting

The repository is formatted and linted with [ruff](https://docs.astral.sh/ruff).
Run both in one step:

```bash
arvis lint
```

This applies `ruff format` and then `ruff check --fix`. To check without
modifying files (suitable for CI), use:

```bash
arvis lint --check
```

The lint configuration lives in `pyproject.toml` under the `[tool.ruff]`
sections.

---

## How it works

The pipeline runs as a sequence of phases (`arvis/pipeline/`):

1. **Profiling** — disassembles the compiled binary, builds a control-flow
   graph, detects software loops, and extracts memory and ALU usage.
2. **Selection** — chooses pruning targets and fusion candidates.
3. **Pruning** — applies parameter-based gating, AST-based case removal,
   and pragma-based logic replacement to produce a pruned RTL set.
4. **Fusion RTL** — identifies fusible instruction patterns (RR+RR and
   immediate variants), generates a GCC machine description, and extends
   the decoder, ALU, and multiplier modules with the matching custom
   instructions.
5. **Hardware loops** — patches the assembly with `hwloop.bounds` /
   `hwloop.count` instructions and synthesises a parameterisable hardware
   loop unit.
6. **Verification** — re-runs the benchmark on the modified core via
   Verilator and confirms the output matches the unmodified baseline.
7. **Reporting** — emits HTML and CSV reports with cycle counts, area,
   and frequency for each configuration.

Each phase reads from and writes to a per-benchmark output directory
(default: `output/<benchmark>_specialized/`).

---

## License

Most of this repository is licensed under the **Apache License 2.0**
(see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)).

Two carve-outs apply to specific subdirectories:

- `tools/gcc-plugin/` — distributed under the **GNU GPL v3.0 or later**
  because it links into GCC. See `tools/gcc-plugin/LICENSE`.
- `targets/cv32e40p/` — the OpenHW Group CV32E40P core is distributed
  under the **Solderpad Hardware License v2.1** (Apache 2.0 derivative).
  The original `LICENSE` file is preserved inside that directory.

See the `NOTICE` file for the full attribution list.


---

## Contact

Paulo Fidalgo · `up201806603@up.pt`
Faculty of Engineering, University of Porto.
