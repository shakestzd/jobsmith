"""Tests for `jobsmith db {get,put,list,reset}-state` (trk-eb70f385).

The CLI surface specialists use to read/write pipeline state from the DB
instead of the file system. Replaces .apply-state/*.json reads/writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobsmith.cli import app as cli_app
from jobsmith.db import open_pipeline_db


def _seed_project(tmp_path: Path) -> Path:
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


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestPutGetState:
    def test_put_then_get_round_trip(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        blob = '{"company":"Reddit","position":"Senior Analytics Engineer"}'
        result = runner.invoke(
            cli_app,
            ["db", "put-state", "--slug", "reddit-sae", "--kind", "jd-parsed"],
            input=blob,
        )
        assert result.exit_code == 0, result.stderr

        result2 = runner.invoke(
            cli_app,
            ["db", "get-state", "--slug", "reddit-sae", "--kind", "jd-parsed"],
        )
        assert result2.exit_code == 0
        assert result2.stdout == blob

    def test_put_overwrites_existing_row(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        runner.invoke(
            cli_app, ["db", "put-state", "--slug", "s1", "--kind", "manifest"],
            input='{"v":1}',
        )
        runner.invoke(
            cli_app, ["db", "put-state", "--slug", "s1", "--kind", "manifest"],
            input='{"v":2}',
        )
        result = runner.invoke(
            cli_app, ["db", "get-state", "--slug", "s1", "--kind", "manifest"],
        )
        assert result.stdout == '{"v":2}'

    def test_get_unknown_slug_or_kind_errors(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli_app, ["db", "get-state", "--slug", "nope", "--kind", "manifest"],
        )
        assert result.exit_code == 2
        assert "no apply_state row" in result.stderr

    def test_put_get_isolated_per_slug(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        runner.invoke(
            cli_app, ["db", "put-state", "--slug", "a", "--kind", "spec"], input="A-spec",
        )
        runner.invoke(
            cli_app, ["db", "put-state", "--slug", "b", "--kind", "spec"], input="B-spec",
        )
        ra = runner.invoke(cli_app, ["db", "get-state", "--slug", "a", "--kind", "spec"])
        rb = runner.invoke(cli_app, ["db", "get-state", "--slug", "b", "--kind", "spec"])
        assert ra.stdout == "A-spec"
        assert rb.stdout == "B-spec"


class TestListState:
    def test_lists_kinds_for_slug(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for kind in ("manifest", "jd-parsed", "fit-score"):
            runner.invoke(
                cli_app, ["db", "put-state", "--slug", "s", "--kind", kind],
                input=f"<{kind}>",
            )

        result = runner.invoke(cli_app, ["db", "list-state", "--slug", "s"])
        assert result.exit_code == 0
        # Lines are "<kind>\t<updated_at>" — alphabetical kinds.
        kinds = [line.split("\t", 1)[0] for line in result.stdout.strip().splitlines()]
        assert kinds == ["fit-score", "jd-parsed", "manifest"]

    def test_unknown_slug_returns_empty(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli_app, ["db", "list-state", "--slug", "nope"])
        assert result.exit_code == 0
        assert result.stdout.strip() == ""


class TestResetState:
    def test_reset_with_yes_deletes_rows(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for kind in ("manifest", "jd-parsed"):
            runner.invoke(
                cli_app, ["db", "put-state", "--slug", "s", "--kind", kind], input="x",
            )

        result = runner.invoke(cli_app, ["db", "reset-state", "--slug", "s", "--yes"])
        assert result.exit_code == 0

        # DB confirms.
        conn = open_pipeline_db(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM apply_state WHERE slug = 's'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0

    def test_reset_without_yes_requires_confirmation(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            cli_app, ["db", "put-state", "--slug", "s", "--kind", "manifest"], input="x",
        )

        result = runner.invoke(cli_app, ["db", "reset-state", "--slug", "s"])
        assert result.exit_code == 1
        assert "Re-run with --yes" in result.stderr

    def test_reset_idempotent_on_empty_slug(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        # No rows for "fresh"; --yes path should succeed without prompting.
        result = runner.invoke(cli_app, ["db", "reset-state", "--slug", "fresh", "--yes"])
        assert result.exit_code == 0


class TestMigrationApplied:
    def test_apply_state_table_exists_after_open_pipeline_db(
        self, tmp_path: Path
    ) -> None:
        """Confirm the 005_apply_state migration runs on a fresh DB open."""
        db_path = tmp_path / "test.db"
        conn = open_pipeline_db(db_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
        finally:
            conn.close()
        assert "apply_state" in tables
        assert "apply_state_log" in tables
