"""RISC-V GCC toolchain implementation.

Concrete :class:`core.toolchain.Toolchain` for the rv32imc_zicsr
target via the standard ``riscv32-unknown-elf-*`` GNU toolchain.

This is the canonical ARVIS toolchain for the cv32e40p target.
A second implementation (LLVM-based, or a Docker-wrapped fork
with hwloop support) plugs in by subclassing :class:`Toolchain`
in a sibling module.

Phase 6.7 wires this into ``Pipeline.run()`` so workloads built
on demand (e.g. fusion analysis on a synthesised baseline)
go through the unified API.

Soft-skip policy
----------------

* Missing GCC / objdump on PATH -> raises :class:`ToolchainError`
  at construction (fail-fast).
* Compile errors -> raise :class:`CompileError` with the captured
  stderr.
* Disassembly errors -> empty string with a logged warning.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from arvis.core.isa import ISADescriptor
from arvis.core.toolchain import CompiledArtifact, Toolchain

logger = logging.getLogger(__name__)


class ToolchainError(RuntimeError):
    """Raised when the toolchain isn't usable (missing binaries)."""


class CompileError(RuntimeError):
    """Raised when GCC / GAS reports a non-zero exit."""


class RISCVGCCToolchain(Toolchain):
    """RISC-V GNU toolchain: GCC + binutils for rv32imc_zicsr.

    Parameters
    ----------
    gcc_binary:
        Optional explicit path to ``riscv32-unknown-elf-gcc``.
        ``None`` (the default) resolves via :func:`shutil.which`.
    objdump_binary:
        Optional explicit path to objdump.  When ``None``, derives
        from the GCC prefix (``riscv32-unknown-elf-objdump``).
    march:
        ``-march`` flag.  Default ``rv32imc_zicsr`` matches what
        every cv32e40p benchmark uses.
    mabi:
        ``-mabi`` flag.  Default ``ilp32``.
    extra_cflags:
        Toolchain-wide cflags prepended to every compile invocation.
        Empty by default; populate with ``("-O2", "-g")`` for
        debug-friendly builds.

    Raises
    ------
    ToolchainError
        When the GCC binary can't be located (fail-fast at
        construction so callers don't carry a half-broken
        toolchain through the pipeline).
    """

    DEFAULT_MARCH = "rv32imc_zicsr"
    DEFAULT_MABI = "ilp32"

    def __init__(
        self,
        *,
        gcc_binary: str | None = None,
        objdump_binary: str | None = None,
        march: str = DEFAULT_MARCH,
        mabi: str = DEFAULT_MABI,
        extra_cflags: Sequence[str] = (),
    ) -> None:
        self._march = march
        self._mabi = mabi
        self._extra_cflags = tuple(extra_cflags)

        # Resolve GCC.
        if gcc_binary is None:
            gcc_binary = shutil.which("riscv32-unknown-elf-gcc")
        if gcc_binary is None or (not Path(gcc_binary).exists() and not shutil.which(gcc_binary)):
            raise ToolchainError(
                f"RISC-V GCC not found: {gcc_binary or 'riscv32-unknown-elf-gcc'}. "
                "Install the RISC-V GNU toolchain or pass gcc_binary= explicitly."
            )
        self._gcc = gcc_binary

        # Resolve objdump.
        if objdump_binary is None:
            objdump_binary = (
                shutil.which("riscv32-unknown-elf-objdump")
                or shutil.which("riscv64-unknown-elf-objdump")
                or shutil.which("riscv-none-elf-objdump")
            )
        self._objdump = objdump_binary  # may be None; disassemble degrades

        # Build the canonical ISA descriptor for cv32e40p.
        self._isa = ISADescriptor(
            name="rv32imc_zicsr",
            xlen=32,
            standard_extensions=("i", "m", "c", "zicsr"),
        )

    # ── Toolchain protocol ───────────────────────────────────────

    @property
    def name(self) -> str:
        return "riscv32-unknown-elf-gcc"

    @property
    def isa(self) -> ISADescriptor:
        return self._isa

    @property
    def gcc_binary(self) -> str:
        """Resolved path to the GCC driver (read-only)."""
        return self._gcc

    @property
    def objdump_binary(self) -> str | None:
        """Resolved path to objdump, or ``None`` when unresolved."""
        return self._objdump

    def supports(self, extension: str) -> bool:
        """Extends ISADescriptor to recognise PULP / hwloop forks.

        The standard rv32imc_zicsr toolchain doesn't claim
        ``hwloop`` support; a future :class:`PULPGCCToolchain`
        subclass overrides this to return ``True`` for those
        names.
        """
        return self._isa.has(extension)

    # ── Compilation ──────────────────────────────────────────────

    def compile(
        self,
        workload,
        cflags: Sequence[str] = (),
        output_dir: Path | None = None,
    ) -> CompiledArtifact:
        """Compile a :class:`Workload` into an ELF.

        Parameters
        ----------
        workload:
            Must expose ``sources: list[Path]`` and ``cflags:
            list[str]``.  The :class:`BenchmarkWorkload` shim
            satisfies this.
        cflags:
            Additional flags appended to the workload's own.
            Use this for variant-specific overrides (e.g.
            ``-O3`` vs ``-Os``).
        output_dir:
            Where to drop the ELF.  Defaults to the workload's
            ``bench_dir``.

        Returns
        -------
        CompiledArtifact
            With ``elf_path`` populated.  ``hex_path`` is built
            via objcopy (best-effort).
        """
        sources = list(getattr(workload, "sources", []) or [])
        if not sources:
            raise CompileError(f"Workload {getattr(workload, 'name', '?')} has no sources")

        wl_cflags = tuple(getattr(workload, "cflags", ()) or ())

        out = Path(output_dir) if output_dir else _resolve_default_dir(workload)
        out.mkdir(parents=True, exist_ok=True)
        name = getattr(workload, "name", "workload")
        elf = out / f"{name}.elf"

        cmd = [self._gcc, *self._base_flags(), *self._extra_cflags, *wl_cflags, *cflags]
        cmd.extend(str(s) for s in sources)
        cmd.extend(["-o", str(elf)])

        logger.info("riscv-gcc compile: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise CompileError(
                f"GCC compile of {name} failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
            )

        hex_path = self._maybe_objcopy_to_hex(elf)
        return CompiledArtifact(
            elf_path=elf,
            hex_path=hex_path,
            cflags_used=(*self._base_flags(), *self._extra_cflags, *wl_cflags, *cflags),
        )

    def assemble(
        self,
        sources: Sequence[Path],
        cflags: Sequence[str] = (),
        output: Path | None = None,
    ) -> CompiledArtifact:
        """Assemble ``.s`` / ``.S`` files into an ELF (no C front-end).

        Used by the hwloop pipeline to assemble patched assembly
        files.  The output is linkable and runnable in Verilator;
        the user supplies link script + crt0 via ``cflags``.
        """
        if not sources:
            raise CompileError("assemble: no sources supplied")

        sources = list(sources)
        output = sources[0].with_suffix(".elf") if output is None else Path(output)

        cmd = [self._gcc, *self._base_flags(), *self._extra_cflags, *cflags]
        cmd.extend(str(s) for s in sources)
        cmd.extend(["-o", str(output)])

        logger.info("riscv-gcc assemble: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise CompileError(
                f"GCC assemble of {sources[0]} failed (exit {proc.returncode}):\n"
                f"{proc.stderr.strip()}"
            )

        hex_path = self._maybe_objcopy_to_hex(output)
        return CompiledArtifact(
            elf_path=output,
            hex_path=hex_path,
            cflags_used=(*self._base_flags(), *self._extra_cflags, *cflags),
        )

    def disassemble(self, elf_path: Path) -> str:
        """objdump -d the ELF and return the text.

        When objdump isn't available, returns an empty string
        with a logged warning.  Callers that need the
        disassembly should check for empty output.
        """
        if self._objdump is None:
            logger.warning(
                "objdump not available; disassemble(%s) returns empty",
                elf_path,
            )
            return ""

        try:
            proc = subprocess.run(
                [self._objdump, "-d", str(elf_path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.exception("objdump invocation raised")
            return ""

        if proc.returncode != 0:
            logger.warning(
                "objdump returned %d for %s: %s",
                proc.returncode,
                elf_path,
                proc.stderr.strip(),
            )
            return ""

        return proc.stdout

    # ── Internals ────────────────────────────────────────────────

    def _base_flags(self) -> list[str]:
        """The flags every cv32e40p compile uses.

        Mirrors what ``pipeline.gcc_compile.run`` and the
        legacy ``hwloop_test_specialized_working/`` Makefile pass.
        """
        return [
            f"-march={self._march}",
            f"-mabi={self._mabi}",
            "-static",
            "-nostdlib",
            "-nostartfiles",
            "-Wl,--gc-sections",
            "-Wl,--no-relax",
        ]

    def _maybe_objcopy_to_hex(self, elf: Path) -> Path | None:
        """Best-effort objcopy ELF -> hex (Verilog format).

        Mirrors the legacy ``riscv32-unknown-elf-objcopy -O verilog``
        invocation.  Returns ``None`` when objcopy isn't available
        or the conversion fails (callers tolerate hex_path=None).
        """
        if self._objdump is None:
            return None
        objcopy = (self._objdump or "").rsplit("-", 1)[0] + "-objcopy"
        if not Path(objcopy).exists() and not shutil.which(objcopy):
            objcopy = (
                shutil.which("riscv32-unknown-elf-objcopy")
                or shutil.which("riscv64-unknown-elf-objcopy")
                or shutil.which("riscv-none-elf-objcopy")
            )
            if objcopy is None:
                return None

        hex_path = elf.with_suffix(".hex")
        try:
            proc = subprocess.run(
                [objcopy, "-O", "verilog", str(elf), str(hex_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.warning("objcopy invocation raised", exc_info=True)
            return None

        if proc.returncode != 0:
            logger.warning(
                "objcopy returned %d for %s: %s",
                proc.returncode,
                elf,
                proc.stderr.strip(),
            )
            return None

        return hex_path


# ─── Helpers ──────────────────────────────────────────────────────


def _resolve_default_dir(workload) -> Path:
    """Try to derive a sensible default output directory from
    the workload (``bench_dir`` is the cv32e40p convention).

    Falls back to the current directory when no obvious
    candidate is available.
    """
    for attr in ("bench_dir", "_bench_dir", "build_dir"):
        candidate = getattr(workload, attr, None)
        if candidate is not None:
            return Path(candidate)
    return Path.cwd()


__all__ = [
    "CompileError",
    "RISCVGCCToolchain",
    "ToolchainError",
]
