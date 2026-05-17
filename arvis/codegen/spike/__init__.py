"""Spike ISA simulator extension generator.

Provides two approaches for teaching Spike about custom fused instructions:

**Approach A  Native rebuild (recommended):**
  Generates native Spike instruction definitions (insns/*.h, encoding.h,
  opcodes) and a patch script.  Spike is rebuilt from source with the
  fused instructions baked in.  No --extlib needed.

**Approach B  RoCC extension (legacy):**
  Generates a dynamically-loadable shared library (.dylib/.so) using
  Spike's rocc_t API.

Usage in the pipeline:
    # Approach A (native):
    from arvis.codegen.spike import generate_native_spike_files
    native = generate_native_spike_files(fused_ops, output_dir)
    # Then: ./patch_spike.sh insns/ encoding_patch.h opcodes_patch

    # Approach B (legacy):
    from arvis.codegen.spike import generate_spike_extension, build_spike_extension
    cpp = generate_spike_extension(fused_ops, ext_dir)
    lib = build_spike_extension(cpp, ext_dir)
    # Then: spike --extlib=./libfused.dylib --extension=fused ...
"""

from .spike_extension_gen import (
    FusedEncoding,
    SpikeNativeFiles,
    build_spike_extension,
    generate_encoding_h_block,
    generate_insn_file,
    generate_native_spike_files,
    generate_opcodes_block,
    generate_patch_script,
    generate_spike_extension,
)

__all__ = [
    "FusedEncoding",
    "SpikeNativeFiles",
    "build_spike_extension",
    "generate_encoding_h_block",
    "generate_insn_file",
    "generate_native_spike_files",
    "generate_opcodes_block",
    "generate_patch_script",
    "generate_spike_extension",
]
