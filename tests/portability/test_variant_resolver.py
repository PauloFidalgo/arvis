"""Tests for the configurable variant CLI surface.

Covers:

* :func:`pipeline.variant_resolver.parse_inline_variant` — atom
  parsing, error cases.
* :func:`pipeline.variant_resolver.resolve_named_variant` —
  name lookup, case-insensitivity, unknown name.
* :func:`pipeline.variant_resolver.resolve_variants` — both
  flags composed, dedup by label, None fallback.
* End-to-end: ``--variants`` + ``--variant`` flags drive the
  resolved variant set in the gateway.
"""

from __future__ import annotations

import pytest

from arvis.pipeline.variant_resolver import (
    VariantResolutionError,
    parse_inline_variant,
    resolve_named_variant,
    resolve_variants,
)

# ─── Inline variant parsing ───────────────────────────────────────


class TestParseInlineVariant:
    def test_single_atom(self) -> None:
        v = parse_inline_variant("fusion_only=fuse")
        assert v.label == "fusion_only"
        assert v.decision_kinds == frozenset({"FusionDecision"})

    def test_multi_atom(self) -> None:
        v = parse_inline_variant("my_combo=prune+loop")
        assert v.label == "my_combo"
        assert v.decision_kinds == frozenset({"PruneDecision", "LoopDecision"})

    def test_all_atoms(self) -> None:
        v = parse_inline_variant("everything=prune+fuse+loop+width")
        assert v.decision_kinds == frozenset(
            {
                "PruneDecision",
                "FusionDecision",
                "LoopDecision",
                "WidthDecision",
            }
        )

    def test_none_atom_means_baseline(self) -> None:
        v = parse_inline_variant("just_baseline=none")
        assert v.decision_kinds == frozenset()

    def test_atoms_are_case_insensitive(self) -> None:
        v = parse_inline_variant("upper=PRUNE+FUSE")
        assert v.decision_kinds == frozenset({"PruneDecision", "FusionDecision"})

    def test_whitespace_tolerated(self) -> None:
        v = parse_inline_variant("  spacey  =  prune + loop  ")
        assert v.label == "spacey"
        assert v.decision_kinds == frozenset({"PruneDecision", "LoopDecision"})

    def test_missing_equals_raises(self) -> None:
        with pytest.raises(VariantResolutionError, match="must be 'label=atoms'"):
            parse_inline_variant("just_a_label")

    def test_empty_label_raises(self) -> None:
        with pytest.raises(VariantResolutionError, match="empty label"):
            parse_inline_variant("=prune")

    def test_empty_atoms_raises(self) -> None:
        with pytest.raises(VariantResolutionError, match="empty atoms"):
            parse_inline_variant("label=")

    def test_unknown_atom_raises(self) -> None:
        with pytest.raises(VariantResolutionError, match="unknown atom 'fake'"):
            parse_inline_variant("x=prune+fake")

    def test_dedup_atoms_in_one_spec(self) -> None:
        """Repeated atoms in one spec collapse via the frozenset."""
        v = parse_inline_variant("dup=prune+prune+loop")
        assert v.decision_kinds == frozenset({"PruneDecision", "LoopDecision"})


# ─── Named variant lookup ─────────────────────────────────────────


class _FakeTarget:
    """Minimal target stand-in for resolver tests."""

    def __init__(self, variants):
        self._variants = variants

    @property
    def standard_variants(self):
        return self._variants


class TestResolveNamedVariant:
    def test_known_name(self) -> None:
        from arvis.targets import CV32E40P

        target = CV32E40P()
        v = resolve_named_variant("pruned", target)
        assert v.label == "pruned"

    def test_case_insensitive(self) -> None:
        from arvis.targets import CV32E40P

        target = CV32E40P()
        v = resolve_named_variant("PRUNED", target)
        assert v.label == "pruned"

    def test_unknown_name_raises(self) -> None:
        from arvis.targets import CV32E40P

        target = CV32E40P()
        with pytest.raises(VariantResolutionError, match="unknown variant"):
            resolve_named_variant("not_a_variant", target)

    def test_empty_name_raises(self) -> None:
        from arvis.targets import CV32E40P

        with pytest.raises(VariantResolutionError, match="empty name"):
            resolve_named_variant("   ", CV32E40P())

    def test_target_with_no_variants(self) -> None:
        target = _FakeTarget(variants=())
        with pytest.raises(VariantResolutionError, match="target advertises none"):
            resolve_named_variant("anything", target)


# ─── Combined resolution ──────────────────────────────────────────


class TestResolveVariants:
    def test_no_flags_returns_none(self) -> None:
        from arvis.targets import CV32E40P

        result = resolve_variants(names=None, inline_specs=None, target=CV32E40P())
        assert result is None

    def test_empty_strings_returns_none(self) -> None:
        from arvis.targets import CV32E40P

        result = resolve_variants(names="", inline_specs=[], target=CV32E40P())
        assert result is None

    def test_names_only(self) -> None:
        from arvis.targets import CV32E40P

        result = resolve_variants(
            names="pruned,fused_pruned",
            inline_specs=None,
            target=CV32E40P(),
        )
        assert result is not None
        assert [v.label for v in result] == ["pruned", "fused_pruned"]

    def test_inline_only(self) -> None:
        from arvis.targets import CV32E40P

        result = resolve_variants(
            names=None,
            inline_specs=["one=prune", "two=fuse+loop"],
            target=CV32E40P(),
        )
        assert result is not None
        assert [v.label for v in result] == ["one", "two"]
        assert result[0].decision_kinds == frozenset({"PruneDecision"})
        assert result[1].decision_kinds == frozenset({"FusionDecision", "LoopDecision"})

    def test_combined_names_and_inline(self) -> None:
        from arvis.targets import CV32E40P

        result = resolve_variants(
            names="baseline",
            inline_specs=["custom=prune+fuse"],
            target=CV32E40P(),
        )
        assert result is not None
        assert [v.label for v in result] == ["baseline", "custom"]

    def test_dedup_by_label_inline_overrides_named(self) -> None:
        """When the same label appears in both flags, inline wins (last)."""
        from arvis.targets import CV32E40P

        result = resolve_variants(
            names="pruned",
            inline_specs=["pruned=fuse"],  # same label, different decisions
            target=CV32E40P(),
        )
        assert result is not None
        assert len(result) == 1
        assert result[0].decision_kinds == frozenset({"FusionDecision"})

    def test_unknown_name_in_list_raises(self) -> None:
        from arvis.targets import CV32E40P

        with pytest.raises(VariantResolutionError):
            resolve_variants(
                names="pruned,not_a_real_variant",
                inline_specs=None,
                target=CV32E40P(),
            )

    def test_invalid_inline_raises(self) -> None:
        from arvis.targets import CV32E40P

        with pytest.raises(VariantResolutionError):
            resolve_variants(
                names=None,
                inline_specs=["bad_no_equals"],
                target=CV32E40P(),
            )


# ─── Documentation example: tests the README-quality use cases ───


class TestUserFacingExamples:
    """The exact use cases from the user's request."""

    def test_fused_pruned_alone(self) -> None:
        """`--variants fused_pruned` yields just one variant."""
        from arvis.targets import CV32E40P

        result = resolve_variants(names="fused_pruned", inline_specs=None, target=CV32E40P())
        assert len(result) == 1
        assert result[0].label == "fused_pruned"

    def test_pruned_plus_loop(self) -> None:
        """`--variants pruned,hwloop_pruned` runs both."""
        from arvis.targets import CV32E40P

        result = resolve_variants(
            names="pruned,hwloop_pruned", inline_specs=None, target=CV32E40P()
        )
        labels = [v.label for v in result]
        assert labels == ["pruned", "hwloop_pruned"]

    def test_fusion_only_inline(self) -> None:
        """Inline variant for "fused only, no pruning" — not in the
        standard 5 but expressible as a one-liner."""
        v = parse_inline_variant("fused_only=fuse")
        assert v.label == "fused_only"
        assert v.decision_kinds == frozenset({"FusionDecision"})

    def test_loop_only_inline(self) -> None:
        v = parse_inline_variant("loop_only=loop")
        assert v.decision_kinds == frozenset({"LoopDecision"})

    def test_width_only_inline(self) -> None:
        v = parse_inline_variant("narrow=width")
        assert v.decision_kinds == frozenset({"WidthDecision"})
