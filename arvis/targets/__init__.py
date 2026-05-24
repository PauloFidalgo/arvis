"""Target package — concrete :class:`core.target.TargetCore` implementations.

Each subdirectory hosts one core (cv32e40p, future ibex, etc.).
This top-level ``__init__`` only marks ``targets/`` as a Python
package and re-exports the registered targets for convenience.

A target is just a class — there is no plugin discovery magic.
Importing a target's module registers nothing; the user composes
a target into a pipeline explicitly.
"""

from arvis.targets.cv32e40p import CV32E40P

__all__ = ["CV32E40P"]
