"""Tests for bullet-level edit helpers in master_io.py.

TDD: these tests were written BEFORE the implementation.  Run them to confirm
they fail (ImportError / AttributeError) before the helpers are added, then
implement until they all pass.

Helpers under test:
  mark_anchor(role_index, bullet_index, *, drop_reason=None) -> None
  add_bullet(role_index, text, *, position=None) -> None
  remove_bullet(role_index, bullet_index, *, reason) -> None
  etag_for_section(section) -> str
"""

from __future__ import annotations

import hashlib
import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from jobsmith.master_io import (
    add_bullet,
    etag_for_section,
    mark_anchor,
    remove_bullet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_WORK = Path(__file__).parent / "fixtures" / "master_work.yml"

WORK_WITH_OBJECT_BULLETS = """\
# Position with mixed bullets
- title: "Senior Engineer"
  location: "Acme"
  date: "2023 - Present"
  description: "Remote"
  details:
    # anchor comment
    - bullet: "Unlocked $250M in ITC across 200K assets"
      anchor: true
      anchor_reason: "big dollar amount"
    - "Plain string bullet"
    - "Third bullet"
"""

WORK_PLAIN_BULLETS = """\
# Plain bullets only
- title: "Engineer"
  location: "Corp"
  date: "2022 - 2023"
  description: "Onsite"
  details:
    - "First bullet"
    - "Second bullet"
    - "Third bullet"
"""


def _rparse(text: str) -> Any:
    y = YAML()
    y.preserve_quotes = True
    return y.load(text)


def _dump(obj: Any) -> str:
    y = YAML()
    y.preserve_quotes = True
    buf = io.StringIO()
    y.dump(obj, buf)
    return buf.getvalue()


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# mark_anchor
# ---------------------------------------------------------------------------


class TestMarkAnchor:
    def test_mark_anchor_plain_string_becomes_object(self, tmp_path: Path) -> None:
        """mark_anchor converts a plain-string bullet to object form with anchor=True."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        mark_anchor(path, role_index=0, bullet_index=0)

        data = _rparse(path.read_text(encoding="utf-8"))
        entry = data[0]["details"][0]
        assert isinstance(entry, dict)
        assert entry["bullet"] == "First bullet"
        assert entry["anchor"] is True

    def test_mark_anchor_with_drop_reason(self, tmp_path: Path) -> None:
        """mark_anchor with drop_reason sets anchor=False and drop_when=reason."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        mark_anchor(path, role_index=0, bullet_index=1, drop_reason="too old")

        data = _rparse(path.read_text(encoding="utf-8"))
        entry = data[0]["details"][1]
        assert isinstance(entry, dict)
        assert entry["anchor"] is False
        assert entry.get("drop_when") == "too old"

    def test_mark_anchor_updates_existing_object_form(self, tmp_path: Path) -> None:
        """mark_anchor updates an already-object-form bullet in place."""
        path = tmp_path / "work.yml"
        _write(path, WORK_WITH_OBJECT_BULLETS)

        # bullet_index 0 is already anchor=True; mark it as non-anchor with reason
        mark_anchor(path, role_index=0, bullet_index=0, drop_reason="too niche")

        data = _rparse(path.read_text(encoding="utf-8"))
        entry = data[0]["details"][0]
        assert entry["anchor"] is False
        assert entry.get("drop_when") == "too niche"

    def test_mark_anchor_preserves_other_bullets(self, tmp_path: Path) -> None:
        """mark_anchor leaves other bullets in the same role untouched."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        mark_anchor(path, role_index=0, bullet_index=0)

        data = _rparse(path.read_text(encoding="utf-8"))
        # bullet_index 1 and 2 remain plain strings
        assert data[0]["details"][1] == "Second bullet"
        assert data[0]["details"][2] == "Third bullet"

    def test_mark_anchor_preserves_comments(self, tmp_path: Path) -> None:
        """mark_anchor preserves YAML comments in the file."""
        path = tmp_path / "work.yml"
        _write(path, WORK_WITH_OBJECT_BULLETS)

        mark_anchor(path, role_index=0, bullet_index=1)  # plain string bullet

        text = path.read_text(encoding="utf-8")
        assert "anchor comment" in text

    def test_mark_anchor_out_of_range_role(self, tmp_path: Path) -> None:
        """mark_anchor raises IndexError for out-of-range role_index."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        with pytest.raises(IndexError):
            mark_anchor(path, role_index=99, bullet_index=0)

    def test_mark_anchor_out_of_range_bullet(self, tmp_path: Path) -> None:
        """mark_anchor raises IndexError for out-of-range bullet_index."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        with pytest.raises(IndexError):
            mark_anchor(path, role_index=0, bullet_index=99)


# ---------------------------------------------------------------------------
# add_bullet
# ---------------------------------------------------------------------------


class TestAddBullet:
    def test_add_bullet_appends_by_default(self, tmp_path: Path) -> None:
        """add_bullet appends a new plain-string bullet when position is None."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        add_bullet(path, role_index=0, text="New bullet text")

        data = _rparse(path.read_text(encoding="utf-8"))
        details = data[0]["details"]
        assert details[-1] == "New bullet text"
        assert len(details) == 4

    def test_add_bullet_inserts_at_position(self, tmp_path: Path) -> None:
        """add_bullet inserts at the specified position."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        add_bullet(path, role_index=0, text="Inserted bullet", position=1)

        data = _rparse(path.read_text(encoding="utf-8"))
        details = data[0]["details"]
        assert details[1] == "Inserted bullet"
        assert details[0] == "First bullet"
        assert details[2] == "Second bullet"

    def test_add_bullet_at_position_zero(self, tmp_path: Path) -> None:
        """add_bullet inserts at position 0 (prepend)."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        add_bullet(path, role_index=0, text="Prepended bullet", position=0)

        data = _rparse(path.read_text(encoding="utf-8"))
        details = data[0]["details"]
        assert details[0] == "Prepended bullet"
        assert details[1] == "First bullet"

    def test_add_bullet_preserves_comments(self, tmp_path: Path) -> None:
        """add_bullet preserves YAML comments."""
        path = tmp_path / "work.yml"
        shutil.copy(FIXTURE_WORK, path)

        add_bullet(path, role_index=0, text="New bullet", position=None)

        text = path.read_text(encoding="utf-8")
        # Fixture has comments
        assert "#" in text

    def test_add_bullet_out_of_range_role(self, tmp_path: Path) -> None:
        """add_bullet raises IndexError for out-of-range role_index."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        with pytest.raises(IndexError):
            add_bullet(path, role_index=5, text="x")


# ---------------------------------------------------------------------------
# remove_bullet
# ---------------------------------------------------------------------------


class TestRemoveBullet:
    def test_remove_bullet_removes_by_index(self, tmp_path: Path) -> None:
        """remove_bullet removes the bullet at the given index."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        remove_bullet(path, role_index=0, bullet_index=1, reason="outdated")

        data = _rparse(path.read_text(encoding="utf-8"))
        details = data[0]["details"]
        assert len(details) == 2
        assert "Second bullet" not in [
            (e["bullet"] if isinstance(e, dict) else e) for e in details
        ]

    def test_remove_bullet_converts_to_drop_when_form(self, tmp_path: Path) -> None:
        """remove_bullet marks bullet as drop_when instead of hard-deleting when it has anchor info."""
        path = tmp_path / "work.yml"
        _write(path, WORK_WITH_OBJECT_BULLETS)

        # bullet 0 has anchor=True — must be soft-dropped, not deleted
        remove_bullet(path, role_index=0, bullet_index=0, reason="too niche")

        data = _rparse(path.read_text(encoding="utf-8"))
        entry = data[0]["details"][0]
        assert isinstance(entry, dict)
        assert entry.get("drop_when") == "too niche"
        # anchor field preserved
        assert "bullet" in entry

    def test_remove_bullet_plain_string_is_hard_deleted(self, tmp_path: Path) -> None:
        """remove_bullet hard-deletes a plain-string (non-anchor) bullet."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        original_count = 3
        remove_bullet(path, role_index=0, bullet_index=2, reason="irrelevant")

        data = _rparse(path.read_text(encoding="utf-8"))
        assert len(data[0]["details"]) == original_count - 1

    def test_remove_bullet_preserves_comments(self, tmp_path: Path) -> None:
        """remove_bullet preserves YAML comments in surviving bullets."""
        path = tmp_path / "work.yml"
        _write(path, WORK_WITH_OBJECT_BULLETS)

        # Remove the second (plain) bullet
        remove_bullet(path, role_index=0, bullet_index=1, reason="old")

        text = path.read_text(encoding="utf-8")
        assert "anchor comment" in text

    def test_remove_bullet_out_of_range(self, tmp_path: Path) -> None:
        """remove_bullet raises IndexError for out-of-range bullet_index."""
        path = tmp_path / "work.yml"
        _write(path, WORK_PLAIN_BULLETS)

        with pytest.raises(IndexError):
            remove_bullet(path, role_index=0, bullet_index=99, reason="x")


# ---------------------------------------------------------------------------
# etag_for_section
# ---------------------------------------------------------------------------


class TestEtagForSection:
    def test_etag_is_sha256_hex(self, tmp_path: Path) -> None:
        """etag_for_section returns a SHA-256 hex digest of the file content."""
        path = tmp_path / "work.yml"
        content = "- title: Test\n"
        _write(path, content)

        tag = etag_for_section(path)

        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert tag == expected

    def test_etag_empty_string_when_file_absent(self, tmp_path: Path) -> None:
        """etag_for_section returns '' when the file does not exist."""
        missing = tmp_path / "no_such.yml"
        assert etag_for_section(missing) == ""

    def test_etag_changes_after_write(self, tmp_path: Path) -> None:
        """etag_for_section returns a different value after the file changes."""
        path = tmp_path / "work.yml"
        _write(path, "- title: Before\n")
        before = etag_for_section(path)

        _write(path, "- title: After\n")
        after = etag_for_section(path)

        assert before != after

    def test_etag_same_content_same_tag(self, tmp_path: Path) -> None:
        """etag_for_section is deterministic: same content → same tag."""
        path = tmp_path / "work.yml"
        content = "- title: Stable\n"
        _write(path, content)

        tag1 = etag_for_section(path)
        tag2 = etag_for_section(path)
        assert tag1 == tag2
