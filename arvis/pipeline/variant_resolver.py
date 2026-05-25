"""CLI variant resolution.

Translates ``--variants <names>`` and ``--variant <label>=<atoms>`` flags
into a list of :class:`core.pipeline.VariantConfig` instances.

Two CLI flags compose freely:

``--variants name1,name2``
    Look up names in :attr:`TargetCore.standard_variants`.
    Case-insensitive.  Unknown names raise
    :class:`VariantResolutionError`.  Comma-separated list.

``--variant <label>=<atoms>``
    Inline custom variant.  ``<atoms>`` is a ``+``-joined list
    of decision atoms (``prune``, ``fuse``, ``loop``, ``width``,
    ``none``).  The atom set is target-agnostic; it maps to the
    canonical :class:`Decision` subclass names that
    :class:`Pipeline._emit_variant` filters on.  Repeatable.

Example::

    --variants pruned,fused_pruned
    --variant my_test=prune+loop
    --variant fusion_only=fuse
    # final list: [PRUNED, FUSED_PRUNED, my_test, fusion_only]

When neither flag is given, the resolver returns ``None``,
signalling the caller should fall back to the target's full
standard variant set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arvis.core.pipeline import VariantConfig
    from arvis.core.target import TargetCore


# Atom vocabulary: atom name -> Decision class name.
# These are the strings :attr:`VariantConfig.decision_kinds`
# expects (the ``type(d).__name__`` of each Decision instance).
_ATOM_TO_DECISION: dict[str, str] = {
    "prune": "PruneDecision",
    "fuse": "FusionDecision",
    "loop": "LoopDecision",
    "width": "WidthDecision",
    "none": "",  # special: empty set
}


class VariantResolutionError(ValueError):
    """Raised when a CLI variant flag can't be parsed or resolved."""


def parse_inline_variant(spec: str) -> VariantConfig:
    """Parse ``label=atom1+atom2+...`` into a :class:`VariantConfig`.

    Parameters
    ----------
    spec:
        Raw CLI string, e.g. ``"my_combo=prune+loop"``.

    Returns
    -------
    VariantConfig

    Raises
    ------
    VariantResolutionError
        On missing ``=``, empty label, or unknown atom.
    """
    from arvis.core.pipeline import VariantConfig

    if "=" not in spec:
        raise VariantResolutionError(f"--variant must be 'label=atoms' (got: {spec!r})")

    label, _, atoms_str = spec.partition("=")
    label = label.strip()
    atoms_str = atoms_str.strip()

    if not label:
        raise VariantResolutionError(f"--variant: empty label in {spec!r}")
    if not atoms_str:
        raise VariantResolutionError(
            f"--variant: empty atoms in {spec!r} (use 'none' for baseline)"
        )

    decision_kinds: set[str] = set()
    for atom in atoms_str.split("+"):
        atom = atom.strip().lower()
        if not atom:
            continue
        if atom not in _ATOM_TO_DECISION:
            valid = ", ".join(sorted(_ATOM_TO_DECISION))
            raise VariantResolutionError(
                f"--variant {spec!r}: unknown atom {atom!r} (valid: {valid})"
            )
        kind = _ATOM_TO_DECISION[atom]
        if kind:
            decision_kinds.add(kind)

    return VariantConfig(label=label, decision_kinds=frozenset(decision_kinds))


def resolve_named_variant(name: str, target: TargetCore) -> VariantConfig:
    """Look up a variant by label in the target's standard set.

    Case-insensitive.  Raises :class:`VariantResolutionError` when
    the name isn't in :attr:`TargetCore.standard_variants`.
    """
    name_norm = name.strip().lower()
    if not name_norm:
        raise VariantResolutionError("--variants: empty name")

    catalogue = {v.label.lower(): v for v in target.standard_variants}
    if name_norm not in catalogue:
        valid = ", ".join(sorted(catalogue))
        raise VariantResolutionError(
            f"--variants: unknown variant {name!r} (valid: {valid or '(target advertises none)'})"
        )
    return catalogue[name_norm]


def resolve_variants(
    *,
    names: str | None,
    inline_specs: list[str] | None,
    target: TargetCore,
) -> list[VariantConfig] | None:
    """Resolve CLI flags to a final variant list.

    Parameters
    ----------
    names:
        Raw value of ``--variants`` (comma-separated names or
        ``None`` when the flag wasn't given).
    inline_specs:
        Raw values of repeated ``--variant`` flags (list of
        ``label=atoms`` strings, or ``None`` / empty list).
    target:
        Target whose ``standard_variants`` is the lookup table
        for ``--variants`` names.

    Returns
    -------
    list[VariantConfig] | None
        The composed list, or ``None`` when neither flag was
        given (caller falls back to the target's full standard
        set).  Order: standard names in flag order, then inline
        variants in flag order.  Duplicates by label are
        deduplicated (last wins).
    """
    if not names and not inline_specs:
        return None

    resolved: list[VariantConfig] = []
    seen_labels: dict[str, int] = {}

    if names:
        for raw in names.split(","):
            v = resolve_named_variant(raw, target)
            _append_or_replace(resolved, seen_labels, v)

    if inline_specs:
        for spec in inline_specs:
            v = parse_inline_variant(spec)
            _append_or_replace(resolved, seen_labels, v)

    return resolved


def _append_or_replace(
    resolved: list,
    seen_labels: dict[str, int],
    variant,
) -> None:
    """Append ``variant``, replacing any earlier entry with the same label."""
    if variant.label in seen_labels:
        resolved[seen_labels[variant.label]] = variant
    else:
        seen_labels[variant.label] = len(resolved)
        resolved.append(variant)


__all__ = [
    "VariantResolutionError",
    "parse_inline_variant",
    "resolve_named_variant",
    "resolve_variants",
]
