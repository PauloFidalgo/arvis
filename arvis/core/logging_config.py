"""Logging helper for ARVIS.

The Phase 1+ packages use the standard :mod:`logging` module
exclusively.  Each module gets its own logger via
``logger = logging.getLogger(__name__)`` and emits messages at
the appropriate level.

The legacy code keeps its ``cli.print_info`` / ``cli.print_warning``
/ ``cli.print_success`` helpers; they're shimmed to write to
``stdout`` directly so the human-friendly pipeline banners stay
intact.  Going forward, new code uses structured logging because:

1. Log levels are filterable via ``--log-level``.
2. ``logger.exception(msg)`` automatically attaches the traceback,
   making the "soft-skip on render error" policy auditable.
3. Logger names are ``arvis.targets.cv32e40p.passes`` etc. -- so
   downstream tools can filter per-package.

Usage
-----

At the top of every module::

    import logging
    logger = logging.getLogger(__name__)

    def my_pass(...):
        logger.info("Starting %s", thing)
        try:
            ...
        except SomeError:
            logger.exception("Soft-skip on render error")

At the application entry point (``main.py``)::

    from arvis.core.logging_config import configure_logging
    configure_logging(level=logging.INFO)

Format
------

The canonical format is::

    LEVEL [logger.name] message

The handler is added once to the root logger; subsequent calls
to :func:`configure_logging` are idempotent.
"""

from __future__ import annotations

import logging
import sys
from typing import Final, TextIO

# Canonical format used across the project.  Aligns with what the
# legacy ``cli.print_*`` helpers emit (``  i  message`` etc.) so
# the visual output stays consistent when the legacy and new code
# co-exist.
_LOG_FORMAT: Final[str] = "%(levelname)-8s %(name)s: %(message)s"

_configured: bool = False


def configure_logging(
    level: int = logging.INFO,
    *,
    stream: TextIO | None = None,
    fmt: str = _LOG_FORMAT,
    force: bool = False,
) -> None:
    """Configure the root logger with a single ``StreamHandler``.

    Idempotent: a second call without ``force=True`` is a no-op.
    Pass ``force=True`` (or just call once at application start)
    to (re-)apply the configuration.

    Parameters
    ----------
    level:
        Default level for the root logger and the configured
        handler.  Per-module loggers can override via
        ``logging.getLogger("arvis.foo").setLevel(...)``.
    stream:
        File-like to write logs to.  Defaults to ``sys.stderr``,
        matching the standard library convention.
    fmt:
        Format string passed to :class:`logging.Formatter`.
    force:
        Reapply the configuration even if a previous call set up
        a handler.

    Notes
    -----
    Tests that need to assert against log output use
    :func:`logging.getLogger("arvis").addHandler` directly --
    we don't expose a "capture" helper here because pytest's
    ``caplog`` fixture already does the job.
    """
    global _configured
    if _configured and not force:
        return

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))

    root = logging.getLogger()
    # Remove handlers we own from a previous call (force-mode).
    if force:
        for h in list(root.handlers):
            if getattr(h, "_arvis_managed", False):
                root.removeHandler(h)
    handler._arvis_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)

    _configured = True


def parse_log_level(name: str) -> int:
    """Translate a CLI string (``"info"``, ``"DEBUG"``, ...) to a
    :mod:`logging` level integer.

    Raises ``ValueError`` for unknown names so the CLI fails fast
    instead of silently downgrading the verbosity.
    """
    candidate = name.upper().strip()
    levels: dict[str, int] = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
    }
    if candidate not in levels:
        raise ValueError(f"Unknown log level {name!r}; expected one of {sorted(levels)}")
    return levels[candidate]


__all__ = ["configure_logging", "parse_log_level"]
