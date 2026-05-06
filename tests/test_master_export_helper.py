"""Tests for export_master_to_disk (bug-3d335f93, trk-eb70f385).

The headless companion to ``jobsmith master export``. Used by the API
supervisor to materialise master_content rows to disk YAML before
launching apply, so specialist agents read fresh content even when the
user only edited via the UI (DB write).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jobsmith.db import open_pipeline_db
from jobsmith.master_ingest import export_master_to_disk


def _seed_db(tmp_path: Path) -> Path:
    db_dir = tmp_path / "private"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "jobsmith.db"
    open_pipeline_db(db_path).close()
    return db_path


def _insert_row(db_path: Path, section: str, content_blob: str) -> None:
    conn = open_pipeline_db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO master_content "
            "(section, content_blob, etag, loaded_at) VALUES (?, ?, ?, ?)",
            (section, content_blob, "etag-" + section, datetime.now(tz=timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


class TestExportMasterToDisk:
    def test_writes_each_section_to_its_target_path(self, tmp_path: Path) -> None:
        db_path = _seed_db(tmp_path)
        _insert_row(db_path, "skill", "- title: Python\n  details: [Spark, Scala]\n")
        _insert_row(db_path, "work", "- title: Engineer\n")

        skill_path = tmp_path / "assets" / "content" / "skill.yml"
        work_path = tmp_path / "assets" / "content" / "work.yml"
        n = export_master_to_disk(
            db_path,
            section_paths={"skill": skill_path, "work": work_path},
        )

        assert n == 2
        assert "Spark" in skill_path.read_text(encoding="utf-8")
        assert "Engineer" in work_path.read_text(encoding="utf-8")

    def test_skips_sections_with_no_db_row(self, tmp_path: Path) -> None:
        db_path = _seed_db(tmp_path)
        _insert_row(db_path, "skill", "- title: Python\n")
        # education NOT in DB

        skill_path = tmp_path / "skill.yml"
        edu_path = tmp_path / "education.yml"
        n = export_master_to_disk(
            db_path,
            section_paths={"skill": skill_path, "education": edu_path},
        )

        assert n == 1
        assert skill_path.exists()
        assert not edu_path.exists()  # not touched

    def test_overwrites_existing_disk_file(self, tmp_path: Path) -> None:
        """Stale disk content (from a prior export) is replaced by current DB blob."""
        db_path = _seed_db(tmp_path)
        _insert_row(db_path, "skill", "FRESH FROM DB\n")

        skill_path = tmp_path / "skill.yml"
        skill_path.write_text("OLD ON DISK\n", encoding="utf-8")

        export_master_to_disk(db_path, section_paths={"skill": skill_path})
        assert skill_path.read_text(encoding="utf-8") == "FRESH FROM DB\n"

    def test_returns_zero_when_db_missing(self, tmp_path: Path) -> None:
        """No DB file → no-op, returns 0 instead of raising."""
        n = export_master_to_disk(
            tmp_path / "nonexistent.db",
            section_paths={"skill": tmp_path / "skill.yml"},
        )
        assert n == 0

    def test_creates_target_directory(self, tmp_path: Path) -> None:
        db_path = _seed_db(tmp_path)
        _insert_row(db_path, "skill", "x\n")

        # Target directory does not yet exist.
        skill_path = tmp_path / "deeply" / "nested" / "dir" / "skill.yml"
        assert not skill_path.parent.exists()

        n = export_master_to_disk(db_path, section_paths={"skill": skill_path})
        assert n == 1
        assert skill_path.exists()
