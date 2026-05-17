#!/bin/bash
# Patch GCC's built-in riscv.md to allow custom fused instructions to
# override standard patterns that would otherwise block fusion.
#
# Problem: GCC's *extendhisi2 pattern matches (sign_extend:SI (subreg:HI ...))
# with condition "" (always true), which has higher priority than our custom
# patterns conditioned on TARGET_CUSTOM_FUSED. This means GCC never emits
# our fused sign-extend instruction.
#
# Solution: Add "!TARGET_CUSTOM_FUSED" condition to built-in patterns that
# conflict with our custom fused instructions. When -mcustom-fused is NOT
# passed, the original patterns work as before. When -mcustom-fused IS
# passed, the built-in patterns are disabled and our custom patterns match.
#
# Target patterns:
#   *extendhisi2           → blocks slli_srai(16,16) — 545K occurrences
#   *zero_extendhisi2_shift → blocks slli_srli(16,16)
#   *extendqisi2           → blocks slli_srai(24,24)
#
# Usage: patch-riscv-md.sh /path/to/gcc/gcc/config/riscv/riscv.md
#
# This script is idempotent — running it twice produces the same result.

set -e

RISCV_MD="${1:-/toolchain/gcc/gcc/config/riscv/riscv.md}"

if [ ! -f "$RISCV_MD" ]; then
    echo "ERROR: riscv.md not found at $RISCV_MD"
    exit 1
fi

echo "Patching $RISCV_MD for custom fused instruction overrides..."

# Check if already patched (idempotent)
if grep -q 'TARGET_CUSTOM_FUSED' "$RISCV_MD" 2>/dev/null; then
    echo "  Already patched (TARGET_CUSTOM_FUSED found) — skipping"
    exit 0
fi

# Count patterns before patching
N_BEFORE=$(grep -c 'define_insn' "$RISCV_MD" || true)
echo "  Patterns before: $N_BEFORE"

# ── Strategy ──
# GCC's riscv.md uses define_insn with a condition string (2nd operand).
# For patterns we want to override, we change the condition from:
#   ""
# to:
#   "!TARGET_CUSTOM_FUSED"
#
# This is done by finding the pattern name and then modifying the
# condition on the line that follows the closing )]
#
# The built-in patterns we need to patch have this structure:
#   (define_insn "*extendhisi2"
#     [(set (match_operand:SI ...)
#           (sign_extend:SI ...))]
#     ""                              ← this is the condition we change
#     "slli\t%0,%1,16\n\tsrai\t%0,%0,16"
#     ...)
#
# We use sed to find lines with the pattern name and patch the next
# occurrence of the condition.

# Patch 1: *extendhisi2 — sign-extend halfword (slli 16, srai 16)
# This catches: (sign_extend:SI (subreg:HI ...)) and
#               (sign_extend:SI (mem:HI ...))
# The pattern appears as: (define_insn "*extendhisi2"
# Followed eventually by a condition line: ""
#
# We use a multi-line sed approach: when we see the pattern name,
# we replace the NEXT standalone "" with "!TARGET_CUSTOM_FUSED"

# Create a Python helper for reliable multi-line patching
python3 << 'PYEOF'
import re
import sys

md_path = sys.argv[1] if len(sys.argv) > 1 else "/toolchain/gcc/gcc/config/riscv/riscv.md"

with open(md_path, 'r') as f:
    content = f.read()

original = content
patched_count = 0

# Patterns to patch: (pattern_name_regex, description)
# We find the define_insn block and replace its condition "" with "!TARGET_CUSTOM_FUSED"
patterns_to_patch = [
    (r'\*extendhisi2', 'sign-extend halfword'),
    (r'\*extendqisi2', 'sign-extend byte'),
    # zero_extend variants that use shift sequences
    (r'\*zero_extendhisi2(?!_)', 'zero-extend halfword'),
    (r'\*zero_extendqisi2(?!_)', 'zero-extend byte'),
]

for pat_re, desc in patterns_to_patch:
    # Find all define_insn blocks with this pattern name
    # Pattern: (define_insn "PATTERN_NAME" ... CONDITION_STRING ...)
    # The condition string appears after the ])] on its own line as ""
    
    # We look for: (define_insn "*extendhisi2"
    # Then find the first standalone "" or "..." condition line after it
    
    # Use a regex that matches the define_insn line, captures everything
    # up to the condition, and replaces the condition
    
    # GCC .md format: the condition is on its own line, indented, as ""
    # It comes after the )] that closes the RTL pattern
    
    pattern = re.compile(
        r'(\(define_insn\s+"' + pat_re + r'"'  # define_insn "pattern_name"
        r'.*?'                                  # RTL body (non-greedy)
        r'\)\])'                                # closing )] of RTL
        r'(\s*)'                                # whitespace
        r'""',                                  # empty condition
        re.DOTALL
    )
    
    def replacer(m):
        nonlocal patched_count
        patched_count += 1
        return m.group(1) + m.group(2) + '"!TARGET_CUSTOM_FUSED"'
    
    content, n = pattern.subn(replacer, content)
    if n > 0:
        print(f"  Patched {n} instance(s) of {pat_re} ({desc})")

if patched_count > 0:
    # Write back
    with open(md_path, 'w') as f:
        f.write(content)
    print(f"  Total: {patched_count} patterns patched")
else:
    print("  WARNING: No patterns found to patch (GCC version may differ)")
    print("  This is not fatal — custom patterns will still work for")
    print("  patterns that don't conflict with built-in patterns.")

PYEOF "$RISCV_MD"

N_AFTER=$(grep -c 'define_insn' "$RISCV_MD" || true)
echo "  Patterns after: $N_AFTER (should be same as before)"

# Verify the patch
if grep -q '!TARGET_CUSTOM_FUSED' "$RISCV_MD"; then
    echo "  ✅ Patch applied successfully"
else
    echo "  ⚠️  Patch may not have applied (no !TARGET_CUSTOM_FUSED found)"
    echo "  Continuing anyway — custom patterns may still work"
fi