# ARVIS GCC plugin

This directory contains the GCC plugin used by the ARVIS fusion phase to
recognise per-workload custom instruction patterns and to insert the
matching hardware-loop hooks during compilation.

## License

The C source files in this directory link into the GNU Compiler
Collection. They are therefore distributed under the **GNU General
Public License, version 3 or later**. See the `LICENSE` file alongside
this README for the full text.

This is a deliberate carve-out from the rest of the ARVIS repository,
which is licensed under Apache 2.0. The carve-out applies only to the
contents of this directory.

## Files

- `fused_pass.c` — GIMPLE pass that recognises fusion patterns described
  in a per-benchmark JSON configuration and emits the corresponding
  custom RISC-V instructions.
- `hwloop_pass.c` — RTL pass that inserts hardware-loop setup
  instructions around eligible loops.
- `Makefile` — builds both passes against the system GCC plugin headers.

## Building

```bash
make
```

This produces `fused_pass.so` and `hwloop_pass.so`. The pipeline loads
them via `-fplugin=` when compiling per-benchmark binaries inside the
`tools/Dockerfile.custom-gcc-*` containers.
