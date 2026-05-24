#!/bin/bash
set -e

# 1. Add -mhwloop option
if ! grep -q "mhwloop" /toolchain/gcc/gcc/config/riscv/riscv.opt; then
    cat >> /toolchain/gcc/gcc/config/riscv/riscv.opt << 'EOF'

mhwloop
Target Mask(HWLOOP)
Enable hardware loop support for CV32E40P-style zero-overhead loops.
EOF
fi

# 2. Add TARGET_HWLOOP macro
if ! grep -q "TARGET_HWLOOP" /toolchain/gcc/gcc/config/riscv/riscv.h; then
    sed -i 's|#endif /\* ! GCC_RISCV_H \*/|#define TARGET_HWLOOP ((target_flags \& MASK_HWLOOP) != 0)\n\n#endif /* ! GCC_RISCV_H */|' \
        /toolchain/gcc/gcc/config/riscv/riscv.h
fi

# 3. Insert hooks into riscv.cc (before TARGET_INITIALIZER)
if ! grep -q "riscv_can_use_doloop_p" /toolchain/gcc/gcc/config/riscv/riscv.cc; then
    sed -i '/^struct gcc_target targetm = TARGET_INITIALIZER;/e cat /work/gcc_hwloop_hooks.c' \
        /toolchain/gcc/gcc/config/riscv/riscv.cc
fi

# 4. Append doloop patterns to riscv.md
if ! grep -q "doloop_end" /toolchain/gcc/gcc/config/riscv/riscv.md; then
    cat /work/gcc_hwloop_md.md >> /toolchain/gcc/gcc/config/riscv/riscv.md
fi

# 5. Rebuild cc1 and xgcc (driver)
cd /toolchain/build-gcc-newlib-stage1/gcc
make -j$(nproc) cc1 xgcc 2>&1 | tail -5
# Detect the installed GCC version directory (avoids hardcoding 15.2.0
# vs 16.1.0 vs whatever the base image happens to ship with).
CC1_DIR=$(dirname "$(find /opt/riscv/libexec/gcc/riscv32-unknown-elf -maxdepth 2 -name cc1 -type f 2>/dev/null | head -1)")
if [ -z "$CC1_DIR" ]; then
    # Fallback: take the first version dir under libexec
    CC1_DIR=$(find /opt/riscv/libexec/gcc/riscv32-unknown-elf -maxdepth 1 -mindepth 1 -type d | head -1)
fi
if [ -z "$CC1_DIR" ] || [ ! -d "$CC1_DIR" ]; then
    echo "ERROR: cannot find target cc1 directory under /opt/riscv/libexec/gcc/riscv32-unknown-elf/"
    ls -la /opt/riscv/libexec/gcc/riscv32-unknown-elf/ 2>&1
    exit 1
fi
echo "Installing cc1 to $CC1_DIR"
cp cc1 "$CC1_DIR/cc1"
cp xgcc /opt/riscv/bin/riscv32-unknown-elf-gcc

# 6. Rebuild fused_pass.so against the new cc1's plugin API
# Use stage1 build dir headers directly — they match the cc1 we just built.
STAGE1_GCC=/toolchain/build-gcc-newlib-stage1/gcc
GCC_PLUGIN_DIR=$(/opt/riscv/bin/riscv32-unknown-elf-gcc -print-file-name=plugin)
if [ -f /toolchain/fused_pass.c ]; then
    g++ -shared -fPIC -fno-rtti \
        -I${STAGE1_GCC} \
        -I${GCC_PLUGIN_DIR}/include \
        -o /opt/riscv/lib/gcc-plugin/fused_pass.so \
        /toolchain/fused_pass.c
    echo "=== fused_pass.so rebuilt ==="
fi

echo "=== Verify ==="
"$CC1_DIR/cc1" --help=target 2>&1 | grep hwloop
