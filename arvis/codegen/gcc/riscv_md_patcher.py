"""
Patcher for GCC's built-in riscv.md — local file approach.

Instead of running fragile sed/regex inside Docker, we:
1. Extract riscv.md from the Docker base image (once)
2. Patch it locally with Python (full control, testable)
3. Pass the patched file into Docker via COPY

The patched file moves the `(include "custom-fused.md")` to an earlier
position in riscv.md so that our fused instruction patterns get lower
insn codes than the builtin patterns they compete with.

Key insight: In GCC's insn recognizer, patterns with LOWER insn codes
(appearing earlier in .md files) have HIGHER priority.  The builtin
`*extend<SHORT:mode><SUPERQI:mode>2` is a `define_insn_and_split` that
handles both register and memory operands.  We CANNOT disable it
(that breaks memory loads like `lh`).  Instead, we move our patterns
BEFORE it so they win for register operands, while the builtin still
handles memory.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

# Path where we cache the extracted riscv.md locally
# NOTE: Must NOT be in a hidden directory — .dockerignore excludes dotfiles.
_CACHE_DIR = Path("tools/riscv-md-cache")
_ORIGINAL_MD = _CACHE_DIR / "riscv.md.original"
_PATCHED_MD = _CACHE_DIR / "riscv.md.patched"

# The include line we need to move
_INCLUDE_LINE = '(include "custom-fused.md")'


def extract_riscv_md(base_image: str = "riscv-gcc-base") -> Path:
    """Extract riscv.md from the Docker base image to a local cache.

    Only extracts once — returns cached path on subsequent calls.

    Returns path to the original (unpatched) riscv.md.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if _ORIGINAL_MD.exists() and _ORIGINAL_MD.stat().st_size > 10000:
        return _ORIGINAL_MD

    logger.info("Extracting riscv.md from Docker image '%s'...", base_image)
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                base_image,
                "cat",
                "/toolchain/gcc/gcc/config/riscv/riscv.md",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and len(result.stdout) > 10000:
            _ORIGINAL_MD.write_text(result.stdout)
            logger.info("Extracted riscv.md: %d bytes", len(result.stdout))
            return _ORIGINAL_MD
        else:
            logger.warning(
                "Failed to extract riscv.md (exit=%d, len=%d)",
                result.returncode,
                len(result.stdout),
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Cannot extract riscv.md: %s", e)

    raise FileNotFoundError(
        f"Could not extract riscv.md from '{base_image}'. "
        "Ensure the base image exists: docker build -f tools/Dockerfile.gcc-base -t riscv-gcc-base tools/"
    )


def _find_insert_point(lines: list[str]) -> int | None:
    """Find the line number BEFORE which we should insert the custom-fused include.

    We want to insert before the SIGN EXTENSION section, which contains
    the `define_insn_and_split` for `*extend<SHORT:mode><SUPERQI:mode>2`.

    The section header looks like:
        ;;  ....................
        ;;
        ;;      SIGN EXTENSION
        ;;
        ;;  ....................

    We insert BEFORE the `define_expand "extendsidi2"` or
    `define_expand "extend<SHORT:mode><SUPERQI:mode>2"` lines,
    whichever comes first.

    Returns the 0-based line index, or None if not found.
    """
    for i, line in enumerate(lines):
        # Look for the sign extension section header
        if "SIGN EXTENSION" in line:
            # Back up to the start of the comment block
            j = i
            while j > 0 and lines[j - 1].strip().startswith(";;"):
                j -= 1
            return j

    # Fallback: look for the first sign_extend define_expand
    for i, line in enumerate(lines):
        if re.match(r'\(define_expand\s+"extend', line):
            return i

    return None


def _find_zero_extend_insert_point(lines: list[str]) -> int | None:
    """Find the line number BEFORE the ZERO EXTENSION section.

    Similar to sign extend, but for zero_extend patterns.
    """
    for i, line in enumerate(lines):
        if "ZERO EXTENSION" in line:
            j = i
            while j > 0 and lines[j - 1].strip().startswith(";;"):
                j -= 1
            return j

    for i, line in enumerate(lines):
        if re.match(r'\(define_expand\s+"zero_extend', line):
            return i

    return None


def generate_patched_riscv_md(
    original_md: Path,
    output_path: Path,
    *,
    patch_sign_extend: bool = True,
    patch_zero_extend: bool = False,
) -> Tuple[Path, int]:
    """Generate a patched riscv.md by moving the custom-fused include earlier.

    The key issue: GCC's insn recognizer assigns insn codes in order of
    appearance in .md files.  Lower insn codes = higher priority.
    The builtin `*extend<SHORT:mode><SUPERQI:mode>2` at line ~1988 gets
    a lower insn code than our patterns included at line ~4830.

    The builtin is a `define_insn_and_split` that handles BOTH register
    and memory operands:
      - Register: outputs `#` (split into slli+srai)
      - Memory: outputs `lh` (load halfword signed)

    We CANNOT disable it (that breaks memory loads → ICE).  Instead, we
    MOVE the `(include "custom-fused.md")` to BEFORE the builtin so our
    `define_insn` patterns get lower insn codes and win for register
    operands, while the builtin still handles memory operands.

    Args:
        original_md: Path to the original riscv.md (from extract_riscv_md).
        output_path: Where to write the patched file.
        patch_sign_extend: Move include before sign-extend patterns.
        patch_zero_extend: Move include before zero-extend patterns.

    Returns:
        (output_path, n_patches_applied)
    """
    content = original_md.read_text()
    lines = content.splitlines(keepends=True)
    patches = 0

    # Find and remove existing include line(s)
    include_indices = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == _INCLUDE_LINE:
            include_indices.append(i)

    if not include_indices:
        logger.warning(
            "No '%s' found in %s — nothing to move",
            _INCLUDE_LINE,
            original_md,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        return output_path, 0

    # Determine target insert point
    insert_point = None
    if patch_sign_extend:
        insert_point = _find_insert_point(lines)
        if insert_point is not None:
            logger.info(
                "Found SIGN EXTENSION section at line %d — will insert custom-fused include before it",
                insert_point + 1,
            )
    if insert_point is None and patch_zero_extend:
        insert_point = _find_zero_extend_insert_point(lines)
        if insert_point is not None:
            logger.info(
                "Found ZERO EXTENSION section at line %d — will insert custom-fused include before it",
                insert_point + 1,
            )

    if insert_point is None:
        logger.warning(
            "Could not find sign/zero extension section in %s — leaving include in original position",
            original_md,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        return output_path, 0

    # Check if include is already before the insert point
    # (i.e., already patched)
    if include_indices[0] < insert_point:
        logger.info(
            "Include already at line %d (before target line %d) — already patched",
            include_indices[0] + 1,
            insert_point + 1,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)
        return output_path, 0

    # Remove existing include lines (from end to start to preserve indices)
    for idx in reversed(include_indices):
        # Also remove surrounding blank/comment lines if they're related
        removed_line = lines[idx]
        lines.pop(idx)
        # If the line before was a comment about custom fused, remove it too
        if idx > 0 and idx - 1 < len(lines):
            prev = lines[idx - 1].strip()
            if prev.startswith(";; Custom fused") or prev == "":
                lines.pop(idx - 1)
        patches += 1
        logger.info(
            "Removed include from line %d: %s",
            idx + 1,
            removed_line.strip(),
        )

    # Recalculate insert point after removals (it may have shifted)
    insert_point = None
    if patch_sign_extend:
        insert_point = _find_insert_point(lines)
    if insert_point is None and patch_zero_extend:
        insert_point = _find_zero_extend_insert_point(lines)

    if insert_point is None:
        logger.error("Lost insert point after removing includes — this is a bug")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("".join(lines))
        return output_path, 0

    # Insert the include at the new position
    insert_block = (
        "\n"
        ";; Custom fused instruction patterns (included BEFORE builtin extend\n"
        ";; patterns so our define_insn gets lower insn codes = higher priority\n"
        ";; for register operands; builtin define_insn_and_split still handles\n"
        ";; memory operands via lh/lb loads)\n"
        f"{_INCLUDE_LINE}\n"
        "\n"
    )
    lines.insert(insert_point, insert_block)
    logger.info(
        "Inserted include at line %d (before sign/zero extension section)",
        insert_point + 1,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(lines))

    logger.info(
        "Patched riscv.md: %d include(s) moved -> %s",
        patches,
        output_path,
    )
    return output_path, patches


def get_patched_riscv_md(
    base_image: str = "riscv-gcc-base",
    *,
    needs_sign_extend_patch: bool = True,
    needs_zero_extend_patch: bool = False,
) -> Path:
    """High-level API: get a patched riscv.md ready for Docker COPY.

    Extracts from Docker if needed, patches locally, returns path.

    Args:
        base_image: Docker image to extract original riscv.md from.
        needs_sign_extend_patch: True if workload has slli_srai(16,16) etc.
        needs_zero_extend_patch: True if workload has slli_srli(16,16) etc.

    Returns:
        Path to the patched riscv.md file (in tools/riscv-md-cache/).
    """
    if not needs_sign_extend_patch and not needs_zero_extend_patch:
        # No patch needed — return None so caller skips the COPY
        return None  # type: ignore[return-value]

    original = extract_riscv_md(base_image)
    patched, n = generate_patched_riscv_md(
        original,
        _PATCHED_MD,
        patch_sign_extend=needs_sign_extend_patch,
        patch_zero_extend=needs_zero_extend_patch,
    )

    if n == 0:
        logger.warning(
            "No patches applied to riscv.md! The include line or extension sections may have changed. Check %s",
            original,
        )

    return patched
