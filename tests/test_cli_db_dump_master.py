"""Tests for `jobsmith db dump-master --section <name>` (bug-3d335f93).

This CLI command is the bash-callable read interface that apply-pipeline
specialists use to fetch master content from the DB. Without it,
specialists would Read disk YAML — which goes stale when users edit via
the UI (PUT writes to DB only).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobsmith.cli import app as cli_app
from jobsmith.db import open_pipeline_db


def _seed_project(tmp_path: Path) -> Path:
    """Create a minimal jobsmith project with .apply-config.yaml and seeded DB."""
    config = tmp_path / ".apply-config.yaml"
    config.write_text(
        "output:\n"
        "  jobsmith_db: private/jobsmith.db\n"
        "  applications_dir: private/applications\n"
        "  job_search_db: private/job_search.db\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "private"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "jobsmith.db"
    open_pipeline_db(db_path).close()
    return db_path


def _insert(db_path: Path, section: str, blob: str) -> None:
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


@pytest.fixture
def runner() -> CliRunner:
    # Newer Typer/Click no longer accepts mix_stderr — separate streams by default.
    return CliRunner()


class TestDbDumpMaster:
    def test_prints_blob_to_stdout(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = _seed_project(tmp_path)
        _insert(db_path, "skill", "- title: Python\n  details: [Spark, Scala]\n")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli_app, ["db", "dump-master", "--section", "skill"])
        assert result.exit_code == 0, result.stderr
        # Stdout is the raw blob, byte-for-byte. No trailing newline added.
        assert result.stdout == "- title: Python\n  details: [Spark, Scala]\n"

    def test_unknown_section_errors_to_stderr(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli_app, ["db", "dump-master", "--section", "bogus"])
        assert result.exit_code == 2
        assert "unknown section 'bogus'" in result.stderr
        assert result.stdout == ""

    def test_missing_db_row_errors_to_stderr(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_project(tmp_path)  # DB exists but no master_content rows
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli_app, ["db", "dump-master", "--section", "skill"])
        assert result.exit_code == 2
        assert "no master_content row for section 'skill'" in result.stderr
        assert result.stdout == ""

    def test_missing_config_errors_to_stderr(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        # No .apply-config.yaml at all
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli_app, ["db", "dump-master", "--section", "skill"])
        assert result.exit_code == 2
        assert "No .apply-config.yaml" in result.stderr

    def test_stdout_is_byte_clean_for_specialist_parsing(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        """Specialists parse stdout as YAML. No rich-formatting bytes, no
        ANSI codes, no trailing newline injected by the CLI."""
        db_path = _seed_project(tmp_path)
        # Blob with no trailing newline
        blob_no_newline = "x: 1"
        _insert(db_path, "author", blob_no_newline)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli_app, ["db", "dump-master", "--section", "author"])
        assert result.exit_code == 0
        assert result.stdout == blob_no_newline
        # No ANSI escape sequences leaked through.
        assert "\x1b[" not in result.stdout

    def test_all_four_sections_supported(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        # Roborev job 957 MEDIUM: ``benchmark`` was advertised but
        # never seeded by load-master / master_ingest, so prompts that
        # tried to read it via dump-master got a hard "no master_content
        # row" failure. Benchmark files are reference fixtures consumed
        # via the Paths block (``benchmarks.resume_qmd`` etc.), not
        # master content. The CLI now supports only the four real
        # master sections.
        db_path = _seed_project(tmp_path)
        sections = {
            "work": "WORK BLOB\n",
            "skill": "SKILL BLOB\n",
            "education": "EDU BLOB\n",
            "author": "AUTHOR BLOB\n",
        }
        for section, blob in sections.items():
            _insert(db_path, section, blob)
        monkeypatch.chdir(tmp_path)

        for section, expected in sections.items():
            result = runner.invoke(cli_app, ["db", "dump-master", "--section", section])
            assert result.exit_code == 0, f"{section}: {result.stderr}"
            assert result.stdout == expected, f"{section}: stdout mismatch"

    def test_benchmark_section_rejected(self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        """``benchmark`` is intentionally not a master section — exits 2."""
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli_app, ["db", "dump-master", "--section", "benchmark"])
        assert result.exit_code == 2
        assert "unknown section 'benchmark'" in (result.stderr or result.output)
