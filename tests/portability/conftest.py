"""Pytest configuration for the portability test suite.

Adds the repo root to ``sys.path`` so tests can import from ``core``,
``strategies``, ``targets``, etc. without installation.

Without this, ``pytest tests/portability/`` would only see the
``tests/`` directory and fail to import the package being tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
