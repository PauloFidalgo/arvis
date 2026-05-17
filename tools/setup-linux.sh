#!/usr/bin/env bash
# ============================================================================
# ARVIS CV32E40P Specializer — Linux Setup Script
# ============================================================================
# Installs all dependencies for running the full pipeline on native Linux.
#
# Tested on: Ubuntu 22.04 / 24.04 (x86_64)
# Run as:    bash tools/setup-linux.sh
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 1. System packages ──────────────────────────────────────────────────────
info "Installing system packages..."
sudo apt-get update
sudo apt-get install -y \
    build-essential git curl wget \
    autoconf automake autotools-dev \
    python3 python3-pip python3-venv \
    libmpc-dev libmpfr-dev libgmp-dev gawk \
    texinfo libtool patchutils bc zlib1g-dev libexpat-dev \
    ninja-build pkg-config flex bison libfl-dev \
    ccache help2man device-tree-compiler \
    ca-certificates gnupg lsb-release

# ── 2. Docker ────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    warn "Log out and back in for Docker group to take effect, then re-run."
else
    info "Docker already installed: $(docker --version)"
fi

# ── 3. uv (Python package manager) ──────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    info "uv already installed: $(uv --version)"
fi

# ── 4. Verilator ────────────────────────────────────────────────────────────
VERILATOR_VERSION="5.044"
if ! command -v verilator &>/dev/null; then
    info "Building Verilator ${VERILATOR_VERSION} from source..."
    sudo apt-get install -y perl cpanminus
    sudo cpanm -n Bit::Vector
    cd /tmp
    git clone https://github.com/verilator/verilator.git -b "v${VERILATOR_VERSION}" --depth 1
    cd verilator
    autoconf
    ./configure --prefix=/usr/local
    make -j"$(nproc)"
    sudo make install
    cd /tmp && rm -rf verilator
    cd -
else
    info "Verilator already installed: $(verilator --version)"
fi

# ── 5. Yosys ────────────────────────────────────────────────────────────────
if ! command -v yosys &>/dev/null; then
    info "Building Yosys from source..."
    sudo apt-get install -y clang tcl-dev libreadline-dev libffi-dev
    cd /tmp
    git clone https://github.com/YosysHQ/yosys.git --depth 1
    cd yosys
    make config-clang
    make -j"$(nproc)"
    sudo make install
    cd /tmp && rm -rf yosys
    cd -
else
    info "Yosys already installed: $(yosys --version)"
fi

# ── 6. sv2v ──────────────────────────────────────────────────────────────────
if ! command -v sv2v &>/dev/null; then
    info "Installing sv2v..."
    SV2V_URL="https://github.com/zachjs/sv2v/releases/latest/download/sv2v-Linux.zip"
    cd /tmp
    wget -q "$SV2V_URL" -O sv2v.zip
    unzip -o sv2v.zip
    sudo mv sv2v /usr/local/bin/
    rm sv2v.zip
    cd -
else
    info "sv2v already installed: $(sv2v --version 2>&1 | head -1)"
fi

# ── 7. Python environment ───────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

info "Setting up Python environment with uv..."
uv sync

# ── 8. Docker images (GCC toolchains) ───────────────────────────────────────
# By default the script pulls pre-built images from Docker Hub
# (paulomfidalgo/arvis-*). If the pull fails the script falls back to
# building the image locally from the Dockerfiles in this directory.
# Set ARVIS_BUILD=1 to force a local build instead.

ARVIS_REGISTRY="${ARVIS_REGISTRY:-paulomfidalgo}"
ARVIS_BUILD="${ARVIS_BUILD:-0}"

# pull_or_build <local_tag> <registry_image> <build_command...>
pull_or_build() {
    local local_tag="$1"
    local registry_image="$2"
    shift 2
    if docker image inspect "$local_tag" &>/dev/null; then
        info "$local_tag already present locally — skipping"
        return 0
    fi
    if [ "$ARVIS_BUILD" != "1" ]; then
        info "Pulling $registry_image → $local_tag"
        if docker pull "$registry_image" >/dev/null 2>&1; then
            docker tag "$registry_image" "$local_tag"
            info "  done (pulled from $registry_image)"
            return 0
        fi
        warn "  pull failed for $registry_image, falling back to local build"
    fi
    info "Building $local_tag locally..."
    "$@"
}

info "Resolving Docker images for custom RISC-V GCC..."

# 8a. Base image (clone + configure, no compile)
pull_or_build "riscv-gcc-base" "${ARVIS_REGISTRY}/arvis-riscv-gcc-base" \
    docker build -f tools/Dockerfile.gcc-base -t riscv-gcc-base tools/

# 8b. Prebuilt image (binutils + newlib + stage1)
pull_or_build "riscv-gcc-prebuilt" "${ARVIS_REGISTRY}/arvis-riscv-gcc-prebuilt" \
    docker build -f tools/Dockerfile.gcc-prebuilt -t riscv-gcc-prebuilt tools/

# 8c. HW loop GCC (plain, no fused instructions)
build_hwloop_local() {
    local build_dir
    build_dir=$(mktemp -d)
    cp tools/hwloop-docker/gcc_hwloop_hooks.c "$build_dir/"
    cp tools/hwloop-docker/gcc_hwloop_md.md "$build_dir/"
    cp tools/hwloop-docker/gcc_apply_hwloop.sh "$build_dir/"
    cp tools/hwloop-docker/Dockerfile.hwloop "$build_dir/Dockerfile"
    docker build -t riscv-gcc-hwloop "$build_dir"
    rm -rf "$build_dir"
}
pull_or_build "riscv-gcc-hwloop" "${ARVIS_REGISTRY}/arvis-riscv-gcc-hwloop" \
    build_hwloop_local

# 8d. Pre-built per-pass fused images (used by the fusion phase).
# These are pulled from Docker Hub. Building them from source is also
# possible: see tools/Dockerfile.custom-gcc-pass and the pipeline driver
# in arvis/pipeline/gcc_compile.py. The pipeline rebuilds them on demand
# if they are missing locally.
for pass in 1 2 3; do
    pull_or_build "custom-riscv-gcc-pass${pass}" \
        "${ARVIS_REGISTRY}/arvis-gcc-pass${pass}" \
        docker build -f tools/Dockerfile.custom-gcc-pass \
            --build-arg "CUSTOM_FUSED_MD=tools/custom-fused-pass${pass}.md" \
            -t "custom-riscv-gcc-pass${pass}" .
done

# Note: custom-riscv-gcc-merged and riscv-gcc-merged-hwloop are built
# per-run by the pipeline (one tag per benchmark), not from the registry.

# ── 9. Verify ────────────────────────────────────────────────────────────────
echo ""
info "=== Verification ==="
echo -n "  Python:    "; python3 --version
echo -n "  uv:        "; uv --version
echo -n "  Verilator: "; verilator --version
echo -n "  Yosys:     "; yosys --version 2>&1 | head -1
echo -n "  sv2v:      "; sv2v --version 2>&1 | head -1
echo -n "  Docker:    "; docker --version

echo ""
info "Docker images:"
docker images --format "  {{.Repository}}:{{.Tag}}\t{{.Size}}" | grep -i "riscv\|gcc" || true

echo ""
# ── 10. Install ARVIS as a uv tool ──────────────────────────────────────────
# After this, the `arvis` command is on the user's PATH and can be invoked
# from any directory without the `uv run` prefix.
if command -v uv &>/dev/null; then
    info "Installing ARVIS as a uv tool..."
    if uv tool install . >/dev/null 2>&1; then
        info "  arvis is now on your PATH (try \`arvis check\`)"
    else
        warn "  uv tool install failed — fall back to \`uv run arvis ...\`"
    fi
fi

echo ""
info "=== Setup complete ==="
info "Verify the toolchain with:  arvis check"
info "Run the pipeline with:      arvis run --benchmark <name>"
info "(or \`uv run arvis ...\` from inside the repo if the global tool was not installed)"
