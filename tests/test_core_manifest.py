"""Tests for jobsmith.core.manifest — Slice 2d."""
from pathlib import Path
import pytest
from jobsmith.core import manifest as core_manifest
from jobsmith import apply as apply_mod


def test_load_manifest_importable():
    assert callable(core_manifest.load_manifest)


def test_phase_completed_importable():
    assert callable(core_manifest.phase_completed)


def test_phase_required_specialists_dict_present():
    assert isinstance(core_manifest.PHASE_REQUIRED_SPECIALISTS, dict)
    # gather/draft/render at minimum
    assert "gather" in core_manifest.PHASE_REQUIRED_SPECIALISTS
    assert "draft" in core_manifest.PHASE_REQUIRED_SPECIALISTS
    assert "render" in core_manifest.PHASE_REQUIRED_SPECIALISTS


def test_apply_re_exports_are_same_object():
    """Back-compat: jobsmith.apply.<old_name> must be SAME object as core.manifest.<new_name>."""
    assert apply_mod._load_manifest is core_manifest.load_manifest
    assert apply_mod._phase_completed is core_manifest.phase_completed
    # PHASE_REQUIRED_SPECIALISTS — apply.py should re-export
    assert apply_mod.PHASE_REQUIRED_SPECIALISTS is core_manifest.PHASE_REQUIRED_SPECIALISTS


def test_phase_completed_handles_none_manifest():
    """phase_completed with None manifest returns False (not crash)."""
    assert core_manifest.phase_completed(None, "gather") is False


def test_phase_completed_partial_manifest():
    """phase_completed returns False for unfinished phase."""
    manifest = {"phases": {"gather": {"status": "in_progress"}}}
    assert core_manifest.phase_completed(manifest, "gather") is False
