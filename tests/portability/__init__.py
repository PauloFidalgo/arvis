"""Portability layer test suite.

Pytest-runnable contract tests for the abstractions in ``core/``,
the cv32e40p target, and the strategy retrofits.

Run with::

    pytest tests/portability/

These tests are independent of the legacy ARVIS pipeline; they
exercise only the new portability layer and (where the strategies
delegate to legacy code) the same legacy entry points.
"""
