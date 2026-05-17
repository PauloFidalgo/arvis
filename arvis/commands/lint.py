"""Subcommand: format and lint the codebase with ruff.

Usage::

    arvis lint            # format + auto-fix lint
    arvis lint --check    # check only, do not modify (exit non-zero on findings)

This is a thin wrapper around `ruff format` and `ruff check`. Both tools
are part of the dev dependency group, so install them with::

    uv sync --group dev

The wrapper exists so contributors do not need to remember the exact ruff
invocations; running `arvis lint` is enough.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from arvis.cli import print_error, print_info, print_section, print_success


def _ruff_path() -> str | None:
    """Resolve the ruff binary, preferring the active Python environment."""
    candidate = Path(sys.executable).with_name("ruff")
    if candidate.is_file():
        return str(candidate)
    return shutil.which("ruff")


def _run(cmd: list[str]) -> int:
    print_info(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arvis lint",
        description="Format and lint the ARVIS codebase with ruff.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check only, do not modify files. Exits non-zero on any finding.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["arvis"],
        help="Paths to format and lint (default: arvis).",
    )
    args = parser.parse_args(argv)

    ruff = _ruff_path()
    if not ruff:
        print_error("ruff is not installed. Install the dev group with `uv sync --group dev`.")
        return 127

    print_section("ruff format" + (" (check)" if args.check else ""))
    fmt_cmd = [ruff, "format"] + (["--check"] if args.check else []) + args.paths
    fmt_rc = _run(fmt_cmd)

    print_section("ruff check" + (" (check)" if args.check else " (--fix)"))
    check_cmd = [ruff, "check"] + ([] if args.check else ["--fix"]) + args.paths
    check_rc = _run(check_cmd)

    rc = fmt_rc | check_rc
    if rc == 0:
        print_success("Lint clean.")
    else:
        print_error(f"Lint reported issues (format={fmt_rc}, check={check_rc}).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
