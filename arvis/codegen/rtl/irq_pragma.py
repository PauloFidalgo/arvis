"""
IRQ Pragma Generators.

Generators for ARVIS_IRQ_BEGIN/END pragmas.
When interrupts are unused: removes interrupt controller ports and connections.
When used: keeps everything.
"""

from __future__ import annotations

from typing import Callable, Dict

# Named generators for mixed IRQ expressions
IRQ_GENERATORS: Dict[str, Callable] = {
    "controller_exc_pc_mux_init": lambda level, indent: (
        f"{indent}exc_pc_mux_o           = EXC_PC_EXCEPTION;\n"
        if level == 0
        else None  # Keep original EXC_PC_IRQ when interrupts enabled
    ),
}
