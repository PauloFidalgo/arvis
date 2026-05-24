"""Contract tests for :mod:`core.logging_config`.

These verify that:

* :func:`configure_logging` is idempotent without ``force=True``.
* :func:`configure_logging` re-applies cleanly with ``force=True``.
* :func:`parse_log_level` accepts the documented level names and
  rejects unknown names with ``ValueError``.

The integration with the rest of the codebase (loggers being
emitted from `passes.py`, `patches.py`, etc.) is covered by the
behaviour-level harnesses (``examples/portability_equivalence.py``).
"""

from __future__ import annotations

import io
import logging

import pytest

from arvis.core.logging_config import configure_logging, parse_log_level


def test_parse_log_level_known_names():
    assert parse_log_level("DEBUG") == logging.DEBUG
    assert parse_log_level("info") == logging.INFO
    assert parse_log_level(" Warning ") == logging.WARNING
    assert parse_log_level("ERROR") == logging.ERROR
    assert parse_log_level("CRITICAL") == logging.CRITICAL


def test_parse_log_level_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown log level"):
        parse_log_level("BOGUS")


def test_configure_logging_is_idempotent():
    """A second call without ``force=True`` does not re-add a handler."""
    # Capture the current state.
    root = logging.getLogger()
    before = len(root.handlers)

    configure_logging()
    after_first = len(root.handlers)

    configure_logging()  # Idempotent: no change.
    after_second = len(root.handlers)

    assert after_first >= before  # may have added 1 or 0
    assert after_second == after_first


def test_configure_logging_force_replaces_managed_handler():
    """``force=True`` removes our previously-managed handler before
    re-installing."""
    configure_logging(force=True)
    root = logging.getLogger()
    managed_handlers_first = [h for h in root.handlers if getattr(h, "_arvis_managed", False)]
    assert len(managed_handlers_first) == 1

    configure_logging(force=True)
    managed_handlers_second = [h for h in root.handlers if getattr(h, "_arvis_managed", False)]
    # Force re-application leaves exactly one managed handler.
    assert len(managed_handlers_second) == 1


def test_configured_logger_emits_with_canonical_format():
    """A `logger.info(...)` call goes through our handler with the
    documented format ``LEVEL    name: message``."""
    buffer = io.StringIO()
    configure_logging(stream=buffer, force=True)

    logger = logging.getLogger("arvis.test.logging_config")
    logger.setLevel(logging.INFO)
    logger.info("hello %s", "world")

    output = buffer.getvalue()
    assert "INFO" in output
    assert "arvis.test.logging_config" in output
    assert "hello world" in output
