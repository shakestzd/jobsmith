"""Tests for the `jobsmith review <slug>` CLI subcommand.

Slice 4 scaffold — slug-only mode. Slice 10 adds URL form, --no-browser flag, etc.

Covers:
- test_review_slug_subcommand_exists
- test_review_unknown_slug_fails_fast
- test_review_known_slug_invokes_marimo
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from jobsmith.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Test 1 — subcommand exists and is accessible
# ---------------------------------------------------------------------------


def test_review_slug_subcommand_exists():
    """jobsmith review --help exits 0 and shows help text."""
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == 0, (
        f"Expected exit code 0, got {result.exit_code}. Output: {result.output}"
    )
    assert "review" in result.output.lower() or "slug" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 2 — unknown slug fails fast with meaningful message
# ---------------------------------------------------------------------------


def test_review_unknown_slug_fails_fast(tmp_path: Path):
    """Slug not in apply_runs → exits 2 with 'slug not found' message."""
    db_path = tmp_path / "private" / "jobsmith.db"
    db_path.parent.mkdir(parents=True)

    # Create a valid DB with schema but no rows
    from jobsmith.db import open_pipeline_db
    conn = open_pipeline_db(db_path)
    conn.close()

    config_text = (
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "output:\n"
        "  applications_dir: private/applications\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )
    (tmp_path / ".apply-config.yaml").write_text(config_text)

    with patch("jobsmith.cli.find_config", return_value=tmp_path / ".apply-config.yaml"):
        result = runner.invoke(app, ["review", "nonexistent-slug"])

    assert result.exit_code == 2, (
        f"Expected exit code 2, got {result.exit_code}. Output: {result.output}"
    )
    assert "not found" in result.output.lower() or "not found" in (result.stderr or "").lower()


# ---------------------------------------------------------------------------
# Test 3 — known slug invokes marimo edit
# ---------------------------------------------------------------------------


def test_review_known_slug_invokes_marimo(tmp_path: Path):
    """When slug exists in apply_runs, marimo edit is invoked."""
    import uuid as _uuid

    db_path = tmp_path / "private" / "jobsmith.db"
    db_path.parent.mkdir(parents=True)

    from jobsmith.db import insert_apply_run, open_pipeline_db
    conn = open_pipeline_db(db_path)
    insert_apply_run(
        conn,
        run_id=str(_uuid.uuid4()),
        slug="acme-swe",
        phase="render",
        started_at="2024-01-01T10:00:00+00:00",
        finished_at="2024-01-01T11:00:00+00:00",
        status="done",
    )
    conn.close()

    config_text = (
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "output:\n"
        "  applications_dir: private/applications\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )
    (tmp_path / ".apply-config.yaml").write_text(config_text)

    invocations = []

    def _fake_subprocess_run(cmd, **kwargs):
        invocations.append((cmd, kwargs))
        class _FakeResult:
            returncode = 0
        return _FakeResult()

    with (
        patch("jobsmith.cli.find_config", return_value=tmp_path / ".apply-config.yaml"),
        patch("jobsmith.cli.subprocess.run", _fake_subprocess_run),
    ):
        runner.invoke(app, ["review", "acme-swe"])

    assert len(invocations) == 1, f"Expected marimo to be invoked once; got {invocations}"
    cmd, kwargs = invocations[0]
    assert cmd[0] == "marimo", f"Expected marimo as first arg; got {cmd[0]!r}"
    assert cmd[1] == "edit", f"Expected 'edit' as second arg; got {cmd[1]!r}"
    assert "apply.py" in cmd[2], f"Expected apply.py in notebook path; got {cmd[2]!r}"

    # Regression for roborev #920 MEDIUM: marimo must be invoked with the
    # repo root as cwd AND JOBSMITH_REPO_ROOT in env so the notebook
    # resolves the right DB when invoked from a subdirectory.
    assert kwargs.get("cwd") == str(tmp_path), (
        f"Expected cwd={tmp_path}; got cwd={kwargs.get('cwd')}"
    )
    env = kwargs.get("env") or {}
    assert env.get("JOBSMITH_REPO_ROOT") == str(tmp_path), (
        f"Expected JOBSMITH_REPO_ROOT={tmp_path}; got {env.get('JOBSMITH_REPO_ROOT')}"
    )
