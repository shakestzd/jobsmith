"""Tests for jobsmith.core.session — Slice 2c."""
from pathlib import Path

from jobsmith import apply as apply_mod
from jobsmith.core import session as core_session


def test_claude_session_file_path_importable():
    assert callable(core_session.claude_session_file_path)


def test_get_or_create_session_id_importable():
    assert callable(core_session.get_or_create_session_id)


def test_apply_re_exports_are_same_object():
    """Back-compat: jobsmith.apply.<old_name> must be SAME object as core.session.<new_name>."""
    assert apply_mod._claude_session_file_path is core_session.claude_session_file_path
    assert apply_mod._get_or_create_session_id is core_session.get_or_create_session_id


def test_get_or_create_session_id_creates_new(tmp_path: Path):
    """First call creates a session id and persists it."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    sid1 = core_session.get_or_create_session_id(app_dir, tmp_path)
    assert sid1
    sid2 = core_session.get_or_create_session_id(app_dir, tmp_path)
    assert sid1 == sid2  # idempotent
