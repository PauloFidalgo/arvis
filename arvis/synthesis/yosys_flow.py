"""Concrete :class:`core.synthesis.SynthesisFlow` implementation backed by Yosys.

Bridges the abstract :class:`SynthesisFlow` interface
(``core/synthesis.py``) and the legacy
:class:`synthesis.yosys_synth.YosysSynthesizer`.

The legacy class returns a :class:`SynthStats` with separate
``cells``, ``wires``, ``elapsed_seconds`` etc. fields; this flow
projects those into the unified :class:`SynthResult` shape used
by the abstract :class:`Verifier` API.
"""

from __future__ import annotations

import logging
from pathlib import Path

from arvis.core.synthesis import SynthesisFlow, SynthResult

logger = logging.getLogger(__name__)


class YosysSynthesisFlow(SynthesisFlow):
    """Drive Yosys synthesis through the unified
    :class:`core.synthesis.SynthesisFlow` API.

    The default technology is ``"nangate45"``, which is what the
    legacy :class:`synthesis.yosys_synth.YosysSynthesizer` ships
    with.  Sky130 and ASAP7 are supported via the legacy class's
    :meth:`synthesize_sky130` etc. helpers; this flow only wires up
    the default.

    Idempotency
    -----------
    Two synthesis runs on the same RTL tree with the same
    technology produce identical :class:`SynthResult` (modulo
    ``elapsed_seconds`` which is filtered out of equality).
    """

    def __init__(self, *, yosys_bin: str | None = None) -> None:
        self._yosys_bin = yosys_bin

    @property
    def name(self) -> str:
        return "yosys"

    def synthesize(
        self,
        rtl_dir: Path,
        technology: str = "nangate45",
        constraints: Path | None = None,
    ) -> SynthResult:
        """Run Yosys synthesis on the given RTL tree.

        Parameters
        ----------
        rtl_dir:
            Path to the per-variant ``rtl/`` directory.
        technology:
            Technology library; only ``"nangate45"`` is currently
            wired up.  Other values fall through to the legacy
            class's default behaviour.
        constraints:
            Reserved; currently ignored.  The legacy class doesn't
            consume SDC files; for STA we'll need a separate
            timing flow.
        """
        del constraints  # not yet used

        from arvis.synthesis.yosys_synth import YosysSynthesizer

        synth = YosysSynthesizer(yosys_bin=self._yosys_bin)
        if not synth.available:
            logger.warning("Yosys not found on PATH; returning empty SynthResult")
            return SynthResult(technology=technology)

        try:
            stats = synth.synthesize(
                rtl_dir=str(rtl_dir),
                config=None,  # legacy default
                defines=None,
                label=Path(rtl_dir).parent.name,
            )
        except Exception:
            logger.exception("Soft-skip: Yosys synthesis raised")
            return SynthResult(technology=technology)

        if not stats.success:
            return SynthResult(
                technology=technology,
                extra={"error": stats.error},
            )

        return SynthResult(
            cell_count=stats.cells,
            technology=technology,
            extra={
                "wires": stats.wires,
                "wire_bits": stats.wire_bits,
                "memories": stats.memories,
                "elapsed_seconds": stats.elapsed_seconds,
                "cell_breakdown": dict(stats.cell_breakdown),
            },
        )


__all__ = ["YosysSynthesisFlow"]
