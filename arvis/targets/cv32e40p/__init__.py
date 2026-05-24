"""CV32E40P target core.

The cv32e40p OpenHW core (RV32IMC + Zicsr, with optional PULP and
hwloop extensions) is the only target ARVIS specializes today.
This package wraps the existing ``targets/cv32e40p/rtl/`` template
tree behind the :class:`core.target.TargetCore` interface so the
new portability layer can drive it.

Phase 1 retrofit only — the rendering methods are still stubs;
the legacy :class:`pipeline.rtl_changeset.RTLChangeSet` continues
to do the actual file mutation.  Phase 3 will move the rendering
logic out of ``codegen/rtl/`` into ``targets/cv32e40p/render.py``.
"""

from arvis.targets.cv32e40p.core import CV32E40P
from arvis.targets.cv32e40p import variants

__all__ = ["CV32E40P", "variants"]
