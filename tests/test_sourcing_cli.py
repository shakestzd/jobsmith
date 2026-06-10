"""Tests for `jobsmith source run` CLI (feat-5531c54b).

TDD: written before implementation.

Covers:
  - `jobsmith source run` exits 0 when no sources configured (not an error)
  - `jobsmith source run` exits 2 when no .apply-config.yaml found
  - `jobsmith source run --dry-run` makes no DB writes
  - `jobsmith source run` summary output contains expected fields
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from jobsmith.cli import app

runner = CliRunner()


@pytest.fixture()
def minimal_repo(tmp_path: Path):
    """Minimal repo with .apply-config.yaml and jobsmith.db."""
    from jobsmith import db as jobsmith_db

    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "  publication_yml: null\n"
        "output:\n"
        "  applications_dir: private/applications\n"
        "  job_search_db: private/job_search.db\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )
    db_dir = tmp_path / "private"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    conn.close()
    return tmp_path


def test_source_run_no_config_exits_2(tmp_path: Path) -> None:
    """Without a .apply-config.yaml, exit code is 2."""
    import os

    # Point JOBSMITH_REPO_ROOT to an empty tmp dir so no config is discoverable.
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(tmp_path)}
    result = runner.invoke(app, ["source", "run"], catch_exceptions=False, env=env)
    assert result.exit_code == 2


def test_source_run_no_sources_exits_0(minimal_repo: Path) -> None:
    """No sourcing.yaml configured → exits 0 (not an error)."""
    import os

    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = runner.invoke(
        app, ["source", "run"], catch_exceptions=False, env=env
    )
    # Exit 0 with 'No enabled sources' message
    assert result.exit_code == 0
    assert "No enabled sources" in result.output


def test_source_run_dry_run_no_db_writes(minimal_repo: Path) -> None:
    """--dry-run fetches but makes no DB postings writes."""
    import os

    from jobsmith.sourcing.adapters.base import Role

    # Create a sourcing.yaml with one source
    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        "expiry_days: 21\n"
        "sources:\n"
        "  - type: greenhouse\n"
        "    slug: testco\n"
        "    name: TestCo\n"
        "    company: TestCo\n"
        "    enabled: true\n"
    )

    mock_role = Role(
        id="greenhouse:testco:001",
        source="greenhouse",
        source_slug="testco",
        company="TestCo",
        title="Data Engineer",
        location="Remote",
        url="https://testco.com/jobs/1",
        jd_text="Build data pipelines.",
        posted_date="2026-06-01",
    )

    def mock_factory(spec):
        from collections.abc import Iterable

        from jobsmith.sourcing.adapters.base import ATSSourceAdapter

        class _Mock(ATSSourceAdapter):
            name = "mock"

            def fetch(self, slug: str) -> Iterable[Role]:
                return iter([mock_role])

        return _Mock()

    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}

    with patch(
        "jobsmith.sourcing.runner.default_adapter_factory",
        side_effect=mock_factory,
    ):
        result = runner.invoke(
            app,
            ["source", "run", "--dry-run"],
            catch_exceptions=False,
            env=env,
        )

    assert result.exit_code == 0, result.output

    # Verify no postings written
    from jobsmith import db as jobsmith_db

    db_path = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_source_run_help_output() -> None:
    """'jobsmith source run --help' exits 0 and shows expected option names."""
    result = runner.invoke(app, ["source", "run", "--help"])
    assert result.exit_code == 0
    assert "--no-llm" in result.output
    assert "--dry-run" in result.output
    assert "--source" in result.output
