"""Tests for slug normalization in db_ingest.

Covers the four malformation cases reported in feat-c63021d8:
- numeric-only slugs
- single-word slugs
- duplicated leading token (e.g. linear-linear-...)
- already-clean slugs pass through unchanged
"""
from __future__ import annotations

from jobsmith.db_ingest import normalize_slug


class TestNumericOnly:
    """numeric-only input → falls back to a non-numeric slug."""

    def test_all_digits_falls_back(self):
        result = normalize_slug("12345")
        assert not result.isdigit(), f"Expected non-numeric slug, got {result!r}"

    def test_all_digits_fallback_contains_apply_prefix(self):
        result = normalize_slug("12345")
        assert result.startswith("apply-"), (
            f"Numeric-only slug should start with 'apply-', got {result!r}"
        )

    def test_numeric_only_with_dashes_falls_back(self):
        """Slugs like '123-456' (all numeric tokens) should also fall back."""
        result = normalize_slug("123-456")
        assert result.startswith("apply-"), (
            f"All-numeric-token slug should start with 'apply-', got {result!r}"
        )

    def test_numeric_slug_with_metadata_uses_company_role(self):
        """When company+role+date are provided, fallback uses them."""
        result = normalize_slug(
            "12345",
            company="Acme",
            role="engineer",
            date="2026-05",
        )
        assert result == "acme-engineer-2026-05", (
            f"Expected 'acme-engineer-2026-05', got {result!r}"
        )


class TestSingleWord:
    """single-word input → falls back to a normalized slug."""

    def test_single_word_falls_back(self):
        result = normalize_slug("engineer")
        assert "-" in result, (
            f"Single-word slug should be expanded with a separator, got {result!r}"
        )

    def test_single_word_with_metadata_uses_company_role(self):
        result = normalize_slug(
            "engineer",
            company="Linear",
            role="product-engineer",
            date="2026-05",
        )
        assert result == "linear-product-engineer-2026-05", (
            f"Expected 'linear-product-engineer-2026-05', got {result!r}"
        )

    def test_single_word_without_metadata_has_apply_prefix(self):
        result = normalize_slug("engineer")
        assert result.startswith("apply-"), (
            f"Single-word slug without metadata should start with 'apply-', got {result!r}"
        )


class TestDuplicatedPrefix:
    """duplicated leading token → de-duplicated."""

    def test_immediate_duplicate_prefix_removed(self):
        result = normalize_slug("linear-linear-product-engineer-2026-05")
        assert result == "linear-product-engineer-2026-05", (
            f"Expected 'linear-product-engineer-2026-05', got {result!r}"
        )

    def test_triple_duplicate_prefix_removed(self):
        result = normalize_slug("foo-foo-foo-bar-baz")
        assert result == "foo-bar-baz", f"Expected 'foo-bar-baz', got {result!r}"

    def test_partial_match_not_removed(self):
        """'acme-acmecorp-engineer' should not be mangled — only exact duplicate token."""
        result = normalize_slug("acme-acmecorp-engineer")
        assert result == "acme-acmecorp-engineer", (
            f"Non-duplicate prefix should be unchanged, got {result!r}"
        )

    def test_different_second_token_not_removed(self):
        result = normalize_slug("google-alphabet-engineer")
        assert result == "google-alphabet-engineer", (
            f"No duplicate, should be unchanged, got {result!r}"
        )


class TestCleanSlugPassThrough:
    """already-clean slugs pass through unchanged."""

    def test_canonical_slug_unchanged(self):
        assert normalize_slug("acme-software-engineer") == "acme-software-engineer"

    def test_slug_with_date_unchanged(self):
        assert normalize_slug("stripe-backend-engineer-2026-05") == "stripe-backend-engineer-2026-05"

    def test_multi_word_hyphenated_unchanged(self):
        assert normalize_slug("openai-research-scientist") == "openai-research-scientist"

    def test_empty_string_falls_back(self):
        result = normalize_slug("")
        assert result.startswith("apply-"), (
            f"Empty string should produce apply- fallback, got {result!r}"
        )
