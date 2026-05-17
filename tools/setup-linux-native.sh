#!/usr/bin/env bash
# ============================================================================
# ARVIS CV32E40P Specializer — Native Linux Setup (No Docker)
# ============================================================================
# Builds all RISC-V GCC toolchains natively under /opt/riscv/.
# Much faster than Docker — no layer overhead, direct filesystem access.
#
# Tested on: Ubuntu 22.04 / 24.04 (x86_64)
# Run as:    sudo bash tools/setup-linux-native.sh
# Time:      ~45 min first run (mostly GCC builds)
# Disk:      ~8 GB under /opt/riscv
# ============================================================================
set -euo pipefail

PREFIX="/opt/riscv"
TOOLCHAIN_SRC="/tmp/riscv-gnu-toolchain"
JOBS="$(nproc)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
timer() { date +%s; }

TOTAL_START=$(timer)

# ── 1. System packages ──────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update
apt-get install -y \
    build-essential git curl wget \
    autoconf automake autotools-dev \
    python3 python3-pip python3-venv \
    libmpc-dev libmpfr-dev libgmp-dev gawk \
    texinfo libtool patchutils bc zlib1g-dev libexpat-dev \
    ninja-build pkg-config flex bison libfl-dev \
    ccache help2man device-tree-compiler \
    perl cpanminus clang tcl-dev libreadline-dev libffi-dev \
    unzip

cpanm -n Bit::Vector 2>/dev/null || true

# ── 2. uv ────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    sudo -u "${SUDO_USER:-$USER}" bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

# ── 3. Verilator ────────────────────────────────────────────────────────────
if ! command -v verilator &>/dev/null; then
    info "Building Verilator..."
    T=$(timer)
    cd /tmp
    rm -rf verilator
    git clone https://github.com/verilator/verilator.git -b v5.044 --depth 1
    cd verilator
    autoconf && ./configure --prefix=/usr/local
    make -j"$JOBS" && make install
    cd /tmp && rm -rf verilator
    info "Verilator done ($(( $(timer) - T ))s)"
else
    info "Verilator: $(verilator --version)"
fi

# ── 4. Yosys ────────────────────────────────────────────────────────────────
if ! command -v yosys &>/dev/null; then
    info "Building Yosys..."
    T=$(timer)
    cd /tmp
    rm -rf yosys
    git clone https://github.com/YosysHQ/yosys.git --depth 1
    cd yosys
    make config-clang && make -j"$JOBS" && make install
    cd /tmp && rm -rf yosys
    info "Yosys done ($(( $(timer) - T ))s)"
else
    info "Yosys: $(yosys --version 2>&1 | head -1)"
fi

# ── 5. sv2v ──────────────────────────────────────────────────────────────────
if ! command -v sv2v &>/dev/null; then
    info "Installing sv2v..."
    cd /tmp
    wget -q https://github.com/zachjs/sv2v/releases/latest/download/sv2v-Linux.zip -O sv2v.zip
    unzip -o sv2v.zip && mv sv2v /usr/local/bin/ && rm sv2v.zip
fi

# ── 6. RISC-V GNU Toolchain (base) ──────────────────────────────────────────
if [ ! -f "$PREFIX/bin/riscv32-unknown-elf-gcc" ]; then
    info "Building RISC-V GCC toolchain (base)..."
    T=$(timer)

    rm -rf "$TOOLCHAIN_SRC"
    git clone https://github.com/riscv-collab/riscv-gnu-toolchain.git "$TOOLCHAIN_SRC"
    cd "$TOOLCHAIN_SRC"
    git submodule update --init gcc
    git submodule update --init --depth 1 binutils newlib

    # Add -mcustom-fused option
    echo '' >> gcc/gcc/config/riscv/riscv.md
    echo ';; Custom fused instruction patterns' >> gcc/gcc/config/riscv/riscv.md
    echo '(include "custom-fused.md")' >> gcc/gcc/config/riscv/riscv.md
    printf '\nmcustom-fused\nTarget Mask(CUSTOM_FUSED)\nEnable custom fused instruction patterns.\n' \
        >> gcc/gcc/config/riscv/riscv.opt
    echo ';; placeholder' > gcc/gcc/config/riscv/custom-fused.md

    ./configure --prefix="$PREFIX" \
        --with-arch=rv32imc --with-abi=ilp32 \
        --disable-linux --disable-gdb --disable-multilib \
        --enable-languages=c

    make -j"$JOBS"
    info "Base GCC done ($(( $(timer) - T ))s)"
else
    info "Base GCC already at $PREFIX"
fi

# ── 7. Apply -mhwloop patches ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! "$PREFIX/bin/riscv32-unknown-elf-gcc" --help=target 2>&1 | grep -q hwloop; then
    info "Applying -mhwloop patches to GCC..."
    T=$(timer)
    cd "$TOOLCHAIN_SRC"

    # 7a. Add -mhwloop option
    if ! grep -q "mhwloop" gcc/gcc/config/riscv/riscv.opt; then
        cat >> gcc/gcc/config/riscv/riscv.opt << 'OPTEOF'

mhwloop
Target Mask(HWLOOP)
Enable hardware loop support for CV32E40P-style zero-overhead loops.
OPTEOF
    fi

    # 7b. Add TARGET_HWLOOP macro
    if ! grep -q "TARGET_HWLOOP" gcc/gcc/config/riscv/riscv.h; then
        sed -i 's|#endif /\* ! GCC_RISCV_H \*/|#define TARGET_HWLOOP ((target_flags \& MASK_HWLOOP) != 0)\n\n#endif /* ! GCC_RISCV_H */|' \
            gcc/gcc/config/riscv/riscv.h
    fi

    # 7c. Insert doloop hooks into riscv.cc
    if ! grep -q "riscv_can_use_doloop_p" gcc/gcc/config/riscv/riscv.cc; then
        sed -i "/^struct gcc_target targetm = TARGET_INITIALIZER;/e cat $SCRIPT_DIR/hwloop-docker/gcc_hwloop_hooks.c" \
            gcc/gcc/config/riscv/riscv.cc
    fi

    # 7d. Append doloop patterns to riscv.md
    if ! grep -q "doloop_end" gcc/gcc/config/riscv/riscv.md; then
        cat "$SCRIPT_DIR/hwloop-docker/gcc_hwloop_md.md" >> gcc/gcc/config/riscv/riscv.md
    fi

    # 7e. Rebuild cc1 only (fast — ~2 min)
    cd build-gcc-newlib-stage1/gcc 2>/dev/null || cd build-gcc-newlib-stage2/gcc
    make -j"$JOBS" cc1 xgcc

    GCC_VER=$("$PREFIX/bin/riscv32-unknown-elf-gcc" -dumpversion)
    cp cc1 "$PREFIX/libexec/gcc/riscv32-unknown-elf/$GCC_VER/cc1"
    cp xgcc "$PREFIX/bin/riscv32-unknown-elf-gcc"

    info "hwloop patches done ($(( $(timer) - T ))s)"
else
    info "-mhwloop already available"
fi

# ── 8. Build GIMPLE fusion plugin ───────────────────────────────────────────
PLUGIN_SO="$PREFIX/lib/gcc-plugin/fused_pass.so"
if [ ! -f "$PLUGIN_SO" ]; then
    info "Building GIMPLE fusion plugin..."
    GCC_PLUGIN_DIR=$("$PREFIX/bin/riscv32-unknown-elf-gcc" -print-file-name=plugin)
    mkdir -p "$PREFIX/lib/gcc-plugin"
    g++ -shared -fPIC -fno-rtti \
        -I"${GCC_PLUGIN_DIR}/include" \
        -o "$PLUGIN_SO" \
        "$SCRIPT_DIR/gcc-plugin/fused_pass.c"
    info "Plugin built: $PLUGIN_SO"
else
    info "GIMPLE plugin already at $PLUGIN_SO"
fi

# ── 9. Smoke tests ──────────────────────────────────────────────────────────
info "Running smoke tests..."
GCC="$PREFIX/bin/riscv32-unknown-elf-gcc"

echo 'int f(int x){return (short)x;}' | \
    $GCC -xc - -S -o /dev/null -march=rv32imc -O2 -mcustom-fused && \
    info "  -mcustom-fused: OK" || warn "  -mcustom-fused: FAIL"

echo 'int f(int n){int s=0;for(int i=0;i<n;i++)s+=i;return s;}' | \
    $GCC -xc - -S -o /dev/null -march=rv32imc -O2 -mhwloop && \
    info "  -mhwloop: OK" || warn "  -mhwloop: FAIL"

echo 'int f(int a,int b){return a+b;}' | \
    $GCC -xc - -S -o /dev/null -march=rv32imc -O2 -mcustom-fused -mhwloop && \
    info "  -mcustom-fused -mhwloop: OK" || warn "  combined: FAIL"

# ── 10. Python environment ──────────────────────────────────────────────────
PROJ_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJ_DIR"
info "Setting up Python environment..."
sudo -u "${SUDO_USER:-$USER}" bash -c "cd $PROJ_DIR && uv sync"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
info "=== Verification ==="
echo "  Python:    $(python3 --version 2>&1)"
echo "  Verilator: $(verilator --version 2>&1)"
echo "  Yosys:     $(yosys --version 2>&1 | head -1)"
echo "  sv2v:      $(sv2v --version 2>&1 | head -1)"
echo "  GCC:       $($GCC --version | head -1)"
echo "  -mhwloop:  $($GCC --help=target 2>&1 | grep -c hwloop) match(es)"
echo "  Plugin:    $(test -f $PLUGIN_SO && echo OK || echo MISSING)"
echo ""
info "Total time: $(( $(timer) - TOTAL_START ))s"
info ""
info "Add to your shell profile:"
info "  export PATH=$PREFIX/bin:\$PATH"
info ""

# Install ARVIS as a uv tool so the `arvis` command is on PATH globally.
# Skipped when uv is unavailable (e.g. running this script as root before
# uv is installed for the calling user).
if command -v uv &>/dev/null; then
    info "Installing ARVIS as a uv tool..."
    if uv tool install . >/dev/null 2>&1; then
        info "  arvis is now on your PATH (try \`arvis check\`)"
    else
        warn "  uv tool install failed — fall back to \`uv run arvis ...\`"
    fi
fi
info ""
info "Verify the toolchain with:  arvis check"
info "Run pipeline with:          ARVIS_NO_DOCKER=1 arvis run --benchmark <name>"
