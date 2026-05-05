"""S7 of trk-144d42b1: first-run UX warning when FS-only state is detected.

Verifies _detect_fs_only_apps and _maybe_warn_fs_only_state in jobsmith.api.main.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.api.main import _detect_fs_only_apps, _maybe_warn_fs_only_state
from jobsmith.db import open_pipeline_db


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Minimal jobsmith repo with applications_dir + DB stub."""
    config = tmp_path / ".apply-config.yaml"
    config.write_text(
        "output:\n"
        "  applications_dir: applications\n"
        "  jobsmith_db: private/jobsmith.db\n",
        encoding="utf-8",
    )
    (tmp_path / "applications").mkdir()
    (tmp_path / "private").mkdir()
    return tmp_path


@pytest.fixture()
def db_path(repo_root: Path) -> Path:
    db = repo_root / "private" / "jobsmith.db"
    open_pipeline_db(db).close()
    return db


def _make_state_dir(repo_root: Path, slug: str) -> None:
    """Create applications/<slug>/.apply-state/ to simulate FS-only state."""
    (repo_root / "applications" / slug / ".apply-state").mkdir(parents=True)


def _seed_run(db_path: Path, slug: str) -> None:
    conn = open_pipeline_db(db_path)
    try:
        conn.execute(
            "INSERT INTO apply_runs (run_id, slug, phase, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"run-{slug}", slug, "draft", "running", "2026-05-05T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


class TestDetectFsOnlyApps:
    def test_returns_empty_when_no_apps_dir(self, db_path: Path, tmp_path: Path):
        # No .apply-config.yaml
        empty = tmp_path / "empty"
        empty.mkdir()
        assert _detect_fs_only_apps(empty, db_path) == []

    def test_returns_empty_when_db_missing(self, repo_root: Path, tmp_path: Path):
        nonexistent_db = tmp_path / "no-db" / "jobsmith.db"
        assert _detect_fs_only_apps(repo_root, nonexistent_db) == []

    def test_detects_fs_state_without_db_row(self, repo_root: Path, db_path: Path):
        _make_state_dir(repo_root, "acme-swe")
        _make_state_dir(repo_root, "beta-eng")
        # Only one slug has a DB row
        _seed_run(db_path, "acme-swe")

        result = _detect_fs_only_apps(repo_root, db_path)
        assert result == ["beta-eng"]

    def test_returns_empty_when_all_slugs_in_db(self, repo_root: Path, db_path: Path):
        _make_state_dir(repo_root, "acme-swe")
        _seed_run(db_path, "acme-swe")
        assert _detect_fs_only_apps(repo_root, db_path) == []


class TestMaybeWarnFsOnlyState:
    def test_logs_warning_with_recovery_command(
        self, repo_root: Path, db_path: Path, caplog
    ):
        _make_state_dir(repo_root, "orphan-co")

        with caplog.at_level(logging.WARNING, logger="jobsmith.api.main"):
            _maybe_warn_fs_only_state(repo_root, db_path)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].message
        assert "orphan-co" in msg
        assert "jobsmith db backfill --all" in msg

    def test_no_warning_when_db_in_sync(
        self, repo_root: Path, db_path: Path, caplog
    ):
        _make_state_dir(repo_root, "synced-co")
        _seed_run(db_path, "synced-co")

        with caplog.at_level(logging.WARNING, logger="jobsmith.api.main"):
            _maybe_warn_fs_only_state(repo_root, db_path)

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_auto_backfill_runs_when_env_set(
        self, repo_root: Path, db_path: Path, monkeypatch
    ):
        _make_state_dir(repo_root, "auto-co")
        monkeypatch.setenv("JOBSMITH_AUTO_BACKFILL", "1")

        with patch("jobsmith.db_ingest.backfill_all") as mock_backfill:
            mock_backfill.return_value = {"auto-co": 3}
            _maybe_warn_fs_only_state(repo_root, db_path)

        mock_backfill.assert_called_once()


class TestDoDDocExists:
    def test_backend_dual_source_dod_doc_present(self):
        """The DoD doc must ship in the same PR (S7 contract)."""
        repo_root = Path(__file__).resolve().parents[1]
        doc = repo_root / "docs" / "contributing" / "backend-dual-source-dod.md"
        assert doc.exists(), f"backend-dual-source-dod.md missing at {doc}"
        text = doc.read_text(encoding="utf-8")
        assert "frontend-feature-dod.md" in text
        assert "DB is the runtime authority" in text
