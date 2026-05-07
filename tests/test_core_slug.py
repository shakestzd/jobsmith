"""Tests for jobsmith.core.slug — Slice 2a."""
from jobsmith import apply as apply_mod
from jobsmith.core import slug as core_slug


def test_derive_slug_importable_from_core():
    assert callable(core_slug.derive_slug)
    assert core_slug.derive_slug("https://example.com/jobs/eng-001") == "eng-001"


def test_slugify_part_importable_from_core():
    assert core_slug._slugify_part("Senior Engineer!") == "senior-engineer"


def test_reconcile_canonical_slug_importable_from_core():
    assert callable(core_slug.reconcile_canonical_slug)


def test_resolve_canonical_slug_importable_from_core():
    assert callable(core_slug.resolve_canonical_slug)


def test_apply_re_exports_are_same_object():
    """jobsmith.apply.derive_slug must be the SAME object as core.slug.derive_slug
    so isinstance / monkeypatch tests across the boundary keep working."""
    assert apply_mod.derive_slug is core_slug.derive_slug
    # Reconcile re-exports under both old and new names for back-compat
    assert apply_mod._reconcile_canonical_slug is core_slug.reconcile_canonical_slug
    assert apply_mod.resolve_canonical_slug is core_slug.resolve_canonical_slug


def test_derive_slug_hash_fallback():
    s = core_slug.derive_slug("https://example.com/")
    assert len(s) == 12 and s.isalnum()
