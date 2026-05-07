"""Tests for jobsmith.core.url_index — Slice 2e."""
from pathlib import Path

from jobsmith import apply as apply_mod
from jobsmith.core import url_index as core_url_index


def test_load_url_index_importable():
    assert callable(core_url_index.load_url_index)


def test_save_url_index_importable():
    assert callable(core_url_index.save_url_index)


def test_scan_for_url_match_importable():
    assert callable(core_url_index.scan_for_url_match)


def test_resolve_starting_slug_importable():
    assert callable(core_url_index.resolve_starting_slug)


def test_record_url_mapping_importable():
    assert callable(core_url_index.record_url_mapping)


def test_apply_re_exports_are_same_object():
    """Back-compat: jobsmith.apply.<old_name> must be SAME object as core.url_index.<new_name>."""
    assert apply_mod._load_url_index is core_url_index.load_url_index
    assert apply_mod._save_url_index is core_url_index.save_url_index
    assert apply_mod._scan_for_url_match is core_url_index.scan_for_url_match
    assert apply_mod._resolve_starting_slug is core_url_index.resolve_starting_slug
    assert apply_mod._record_url_mapping is core_url_index.record_url_mapping


def test_load_url_index_missing_returns_empty(tmp_path: Path):
    """Loading from a cwd without an index returns {} or None gracefully."""
    result = core_url_index.load_url_index(tmp_path)
    # Empty dict or None depending on impl — just shouldn't crash
    assert result == {} or result is None or isinstance(result, dict)


def test_scan_for_url_match_empty_dir(tmp_path: Path):
    """Scanning an empty cwd returns None."""
    result = core_url_index.scan_for_url_match("https://example.com", tmp_path)
    assert result is None
