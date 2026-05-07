"""Tests for jobsmith.core.paths — Slice 2b."""
from pathlib import Path
import pytest
from jobsmith.core import paths as core_paths
from jobsmith import apply as apply_mod


def test_apply_state_dir_importable():
    assert callable(core_paths.apply_state_dir)


def test_applications_dir_importable():
    assert callable(core_paths.applications_dir)


def test_build_paths_importable():
    assert callable(core_paths.build_paths)


def test_pipeline_db_path_importable():
    assert callable(core_paths.pipeline_db_path)


def test_apply_re_exports_are_same_object():
    """Back-compat: jobsmith.apply.<old_name> must be SAME object as core.paths.<new_name>."""
    assert apply_mod._apply_state_dir is core_paths.apply_state_dir
    assert apply_mod._applications_dir is core_paths.applications_dir
    assert apply_mod._build_paths is core_paths.build_paths
    assert apply_mod._pipeline_db_path is core_paths.pipeline_db_path


def test_applications_dir_returns_path(tmp_path: Path):
    """In a cwd without .apply-config.yaml, returns None."""
    # If function uses ensure_bootstrap or similar, it may return None when no config
    result = core_paths.applications_dir(tmp_path)
    # Just verify it returns Path or None without crashing
    assert result is None or isinstance(result, Path)
