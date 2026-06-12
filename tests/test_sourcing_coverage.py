"""Tests for jobsmith.sourcing.coverage (feat-6ec8c30a).

TDD: tests written before implementation.

Covers:
  - empty master content yields explicit empty marker
  - digest contains known bullets grouped by section
  - digest is deterministic (same content → identical output)
  - hard cap at ~2000 chars even with oversized input
  - oversized input is truncated sensibly (section grouping preserved)
  - only work/skill sections contribute (education/author omitted or minimal)
  - dict-form bullets (with 'bullet' key) are handled correctly
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jobsmith.db import open_pipeline_db
from jobsmith.sourcing.coverage import MAX_DIGEST_CHARS, build_master_digest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORK_BLOB = """\
- title: "Senior Data Engineer"
  location: "HeliosCo"
  date: "Aug 2024 - Present"
  description: "Remote"
  details:
    - "Built a $250M geospatial analytics platform"
    - "Shipped 7 ETL pipelines at 99.9% reliability"

- title: "Data Engineer"
  location: "HeliosCo"
  date: "Nov 2022 - Jul 2024"
  description: "Remote"
  details:
    - "Optimised $4.25B solar capacity allocation via CP-SAT"
    - "Cut AP processing time by 75%"
"""

SKILL_BLOB = """\
- title: "Programming"
  description: "Python, SQL"
  details:
    - "Python (Advanced)"
    - "SQL (Advanced)"

- title: "Data Engineering"
  description: "Dagster, DuckDB"
  details:
    - "Dagster"
    - "DuckDB"
"""


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal pipeline DB and return its path."""
    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    conn.close()
    return db_path


def _insert_section(db_path: Path, section: str, blob: str) -> None:
    conn = open_pipeline_db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO master_content "
            "(section, content_blob, etag, loaded_at) VALUES (?, ?, ?, ?)",
            (section, blob, "etag", datetime.now(tz=timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TestBuildMasterDigestEmpty
# ---------------------------------------------------------------------------


class TestBuildMasterDigestEmpty:
    def test_empty_db_returns_explicit_marker(self, tmp_path: Path) -> None:
        """Empty master_content table must return an unambiguous empty marker."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert "no master content" in result.lower()

    def test_empty_marker_is_short(self, tmp_path: Path) -> None:
        """Empty marker should itself be well under the cap."""
        db_path = _make_db(tmp_path)
        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert len(result) < 200


# ---------------------------------------------------------------------------
# TestBuildMasterDigestContent
# ---------------------------------------------------------------------------


class TestBuildMasterDigestContent:
    def test_digest_contains_work_bullets(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", WORK_BLOB)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert "$250M" in result or "250M" in result
        assert "ETL" in result

    def test_digest_contains_skill_details(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "skill", SKILL_BLOB)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert "Python" in result
        assert "Dagster" in result

    def test_digest_has_section_labels(self, tmp_path: Path) -> None:
        """Digest must group bullets by section with a visible label."""
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", WORK_BLOB)
        _insert_section(db_path, "skill", SKILL_BLOB)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        # Section labels should appear (case-insensitive check)
        lower = result.lower()
        assert "work" in lower
        assert "skill" in lower

    def test_dict_form_bullet_text_included(self, tmp_path: Path) -> None:
        """Bullets stored as {bullet: ..., anchor: true} must be extracted."""
        work_with_dict = """\
- title: "Data Analyst"
  location: "Atlas"
  date: "Jun 2020 - Oct 2022"
  description: "Remote"
  details:
    - bullet: "Built quarterly investor reporting pipelines (5 days to 4 hours)"
      anchor: true
      anchor_reason: "Story-of-impact"
      tags: [reporting]
"""
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", work_with_dict)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert "quarterly investor reporting" in result.lower() or "investor reporting" in result


# ---------------------------------------------------------------------------
# TestBuildMasterDigestDeterminism
# ---------------------------------------------------------------------------


class TestBuildMasterDigestDeterminism:
    def test_same_content_produces_identical_digest(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", WORK_BLOB)
        _insert_section(db_path, "skill", SKILL_BLOB)

        results = []
        for _ in range(3):
            conn = open_pipeline_db(db_path)
            try:
                results.append(build_master_digest(conn))
            finally:
                conn.close()

        assert results[0] == results[1] == results[2]


# ---------------------------------------------------------------------------
# TestBuildMasterDigestCap
# ---------------------------------------------------------------------------

_LONG_BULLET = "X" * 200  # 200-char bullet
_MANY_BULLETS_BLOB = (
    "- title: \"Job\"\n"
    "  location: \"Co\"\n"
    "  date: \"2020-2024\"\n"
    "  description: \"Remote\"\n"
    "  details:\n"
    + "".join(f'    - "{_LONG_BULLET} #{i}"\n' for i in range(50))
)


class TestBuildMasterDigestCap:
    def test_oversized_input_respects_char_cap(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", _MANY_BULLETS_BLOB)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert len(result) <= MAX_DIGEST_CHARS

    def test_oversized_still_has_section_grouping(self, tmp_path: Path) -> None:
        """Even when truncated, the digest should retain the section header."""
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", _MANY_BULLETS_BLOB)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        assert "work" in result.lower()

    def test_normal_content_well_under_cap(self, tmp_path: Path) -> None:
        """Representative real-world content must be well under the cap."""
        db_path = _make_db(tmp_path)
        _insert_section(db_path, "work", WORK_BLOB)
        _insert_section(db_path, "skill", SKILL_BLOB)

        conn = open_pipeline_db(db_path)
        try:
            result = build_master_digest(conn)
        finally:
            conn.close()

        # Should use the cap efficiently but not blow past it
        assert 50 < len(result) <= MAX_DIGEST_CHARS
