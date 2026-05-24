"""Reporter abstraction.

A :class:`Reporter` consumes a :class:`PipelineResult` and writes
out a human-readable artifact (HTML, JSON, Markdown, etc.).
Reporters do not modify the pipeline result; they are purely
output-side.

Today only HTML output exists (in :mod:`report.html_report`); a
future Phase 2 step will wrap that as :class:`HTMLReporter`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.core.pipeline import PipelineResult


class Reporter(ABC):
    """Abstract reporter.

    Concrete reporters know how to translate a
    :class:`PipelineResult` into an output file or set of files.
    They should be deterministic — same inputs produce the same
    output bytes — so that test runs can compare report contents.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (``"html"``, ``"json"``, ...)."""

    @abstractmethod
    def emit(self, result: "PipelineResult", out_path: Path) -> Path:
        """Write the report and return the path written.

        ``out_path`` is the directory the reporter should write
        into; the actual file name is reporter-specific.  Returning
        the final path lets the pipeline log it for the user.
        """
