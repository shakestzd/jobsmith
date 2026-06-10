"""Tests for email alert ingestion integrated into run_crawl (feat-b1bd050e).

Verifies:
  - run_email_alerts() upserts postings from parsed email entries
  - DEGRADED is recorded when no postings are parsed
  - run_crawl() accepts alert_senders and routes through email path
  - source add CLI records manual/linkedin postings
All tests run offline (fixtures + mocks).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from jobsmith.cli import app

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "email_alerts"

cli_runner = CliRunner()


@pytest.fixture()
def pipeline_db(tmp_path: Path):
    """Minimal pipeline DB for email ingestion tests."""
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
    return tmp_path, db_path


# ---------------------------------------------------------------------------
# run_email_alerts unit tests
# ---------------------------------------------------------------------------


def test_run_email_alerts_upserts_postings(pipeline_db) -> None:
    from jobsmith import db as jobsmith_db
    from jobsmith.sourcing.runner import run_email_alerts

    repo_root, db_path = pipeline_db
    conn = jobsmith_db.open_pipeline_db(db_path)

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]

    def _mock_ingest(senders, *, max_per_sender=20):
        return [
            {
                "source": "mailapp/linkedin-alert",
                "title": "Data Engineer",
                "company": "Acme Corp",
                "location": "Remote",
                "url": "https://www.linkedin.com/jobs/view/3001000001/",
                "external_id": "3001000001",
            }
        ], []

    upserted, new_count, degraded = run_email_alerts(
        conn, senders, _mailapp_ingest_fn=_mock_ingest
    )

    assert upserted == 1
    assert new_count == 1
    assert degraded == []

    row = conn.execute("SELECT * FROM postings WHERE external_id = '3001000001'").fetchone()
    assert row is not None
    assert row["source"] == "mailapp/linkedin-alert"
    conn.close()


def test_run_email_alerts_records_degraded(pipeline_db) -> None:
    from jobsmith import db as jobsmith_db
    from jobsmith.sourcing.runner import run_email_alerts

    _, db_path = pipeline_db
    conn = jobsmith_db.open_pipeline_db(db_path)

    senders = [
        {
            "type": "gmail_alert",
            "sender": "jobs@linkedin.com",
            "sender_slug": "linkedin-alert",
        }
    ]

    # ingest returns no postings and marks sender as degraded
    def _mock_ingest(senders, **kwargs):
        return [], ["linkedin-alert"]

    upserted, new_count, degraded = run_email_alerts(
        conn, senders, _gmail_ingest_fn=_mock_ingest
    )

    assert upserted == 0
    assert "linkedin-alert" in degraded
    conn.close()


def test_run_email_alerts_dry_run_no_writes(pipeline_db) -> None:
    from jobsmith import db as jobsmith_db
    from jobsmith.sourcing.runner import run_email_alerts

    _, db_path = pipeline_db
    conn = jobsmith_db.open_pipeline_db(db_path)

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]

    def _mock_ingest(senders, **kwargs):
        return [
            {
                "source": "mailapp/linkedin-alert",
                "title": "Data Engineer",
                "company": "Acme",
                "location": "Remote",
                "url": "https://www.linkedin.com/jobs/view/9999/",
                "external_id": "9999",
            }
        ], []

    upserted, new_count, degraded = run_email_alerts(
        conn, senders, dry_run=True, _mailapp_ingest_fn=_mock_ingest
    )

    # dry_run=True → no DB writes
    count = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    assert count == 0
    conn.close()


# ---------------------------------------------------------------------------
# run_crawl with alert_senders
# ---------------------------------------------------------------------------


def test_run_crawl_with_alert_senders(pipeline_db) -> None:
    from jobsmith.sourcing.runner import run_crawl

    repo_root, db_path = pipeline_db

    def _mock_mailapp(senders, **kwargs):
        # Return 2 postings with proper fields so upsert_posting can write them
        return [
            {
                "source": "mailapp/linkedin-alert",
                "title": "Data Engineer",
                "company": "Acme",
                "location": "Remote",
                "url": "https://www.linkedin.com/jobs/view/9991/",
                "external_id": "9991",
            },
            {
                "source": "mailapp/linkedin-alert",
                "title": "Senior Data Engineer",
                "company": "Beta",
                "location": "NYC",
                "url": "https://www.linkedin.com/jobs/view/9992/",
                "external_id": "9992",
            },
        ], []

    summary = run_crawl(
        db_path=db_path,
        sources=[],
        alert_senders=[
            {
                "type": "mailapp_alert",
                "sender_slug": "linkedin-alert",
                "account": "me@example.com",
                "mailbox": "Job Alerts",
            }
        ],
        no_llm=True,
        _run_email_alerts_fn=_mock_mailapp,
    )

    assert summary["roles_upserted"] == 2
    assert summary["aborted"] is False


def test_run_crawl_email_degraded_recorded(pipeline_db) -> None:
    from jobsmith.sourcing.runner import run_crawl

    _, db_path = pipeline_db

    def _mock_mailapp(senders, **kwargs):
        return [], ["linkedin-alert"]

    summary = run_crawl(
        db_path=db_path,
        sources=[],
        alert_senders=[
            {"type": "mailapp_alert", "sender_slug": "linkedin-alert",
             "account": "me@example.com", "mailbox": "Job Alerts"}
        ],
        no_llm=True,
        _run_email_alerts_fn=_mock_mailapp,
    )

    assert "linkedin-alert" in summary["degraded_sources"]


# ---------------------------------------------------------------------------
# source add CLI
# ---------------------------------------------------------------------------


def test_source_add_numeric_ids(pipeline_db) -> None:
    """jobsmith source add 3001000001 records a manual/linkedin posting."""
    import os

    repo_root, db_path = pipeline_db
    config_file = repo_root / ".apply-config.yaml"
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(repo_root)}

    with patch("jobsmith.cli.find_config", return_value=config_file):
        result = cli_runner.invoke(
            app,
            ["source", "add", "3001000001", "3001000002"],
            catch_exceptions=False,
            env=env,
        )
    assert result.exit_code == 0, result.output
    assert "recorded=2" in result.output

    from jobsmith import db as jobsmith_db
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT * FROM postings WHERE source='manual/linkedin'").fetchall()
        assert len(rows) == 2
        urls = {r["url"] for r in rows}
        assert "https://www.linkedin.com/jobs/view/3001000001/" in urls
        assert "https://www.linkedin.com/jobs/view/3001000002/" in urls
    finally:
        conn.close()


def test_source_add_full_url(pipeline_db) -> None:
    """jobsmith source add <linkedin-url> records the job."""
    import os

    repo_root, db_path = pipeline_db
    config_file = repo_root / ".apply-config.yaml"
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(repo_root)}

    url = "https://www.linkedin.com/jobs/view/3001000099/?trk=eml-something"
    with patch("jobsmith.cli.find_config", return_value=config_file):
        result = cli_runner.invoke(
            app,
            ["source", "add", url],
            catch_exceptions=False,
            env=env,
        )
    assert result.exit_code == 0, result.output
    assert "recorded=1" in result.output

    from jobsmith import db as jobsmith_db
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM postings WHERE external_id='3001000099'"
        ).fetchone()
        assert row is not None
        assert row["source"] == "manual/linkedin"
    finally:
        conn.close()


def test_source_add_dedup(pipeline_db) -> None:
    """Inserting the same ID twice is idempotent (upsert)."""
    import os

    repo_root, db_path = pipeline_db
    config_file = repo_root / ".apply-config.yaml"
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(repo_root)}

    with patch("jobsmith.cli.find_config", return_value=config_file):
        cli_runner.invoke(app, ["source", "add", "3001000001"], env=env)
        result = cli_runner.invoke(app, ["source", "add", "3001000001"], env=env)
    assert result.exit_code == 0

    from jobsmith import db as jobsmith_db
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM postings WHERE external_id='3001000001'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_source_add_invalid_identifier(pipeline_db) -> None:
    """Non-LinkedIn, non-numeric identifier is skipped (skip=1)."""
    import os

    repo_root, _ = pipeline_db
    config_file = repo_root / ".apply-config.yaml"
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(repo_root)}

    with patch("jobsmith.cli.find_config", return_value=config_file):
        result = cli_runner.invoke(
            app,
            ["source", "add", "not-a-valid-id"],
            catch_exceptions=False,
            env=env,
        )
    assert result.exit_code == 0
    assert "skipped=1" in result.output


def test_source_add_no_config_exits_2(tmp_path: Path) -> None:
    import os

    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(tmp_path)}
    with patch("jobsmith.cli.find_config", return_value=None):
        result = cli_runner.invoke(
            app,
            ["source", "add", "3001000001"],
            catch_exceptions=False,
            env=env,
        )
    assert result.exit_code == 2


def test_source_add_help() -> None:
    result = cli_runner.invoke(app, ["source", "add", "--help"])
    assert result.exit_code == 0
    assert "LinkedIn" in result.output or "manual" in result.output.lower() or "identifiers" in result.output.lower()


# ---------------------------------------------------------------------------
# Config schema: alert_senders
# ---------------------------------------------------------------------------


def test_sourcing_config_loads_alert_senders(tmp_path: Path) -> None:
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text(
        "expiry_days: 14\n"
        "sources: []\n"
        "alert_senders:\n"
        "  - type: gmail_alert\n"
        "    sender: jobs-noreply@linkedin.com\n"
        "    sender_slug: linkedin-alert\n"
        "  - type: mailapp_alert\n"
        "    sender_slug: indeed-alert\n"
        "    account: me@example.com\n"
        "    mailbox: Job Alerts\n"
    )
    cfg = load_sourcing_config(cfg_file)
    assert len(cfg.alert_senders) == 2
    assert cfg.alert_senders[0]["type"] == "gmail_alert"
    assert cfg.alert_senders[1]["type"] == "mailapp_alert"


def test_sourcing_config_alert_senders_respects_enabled(tmp_path: Path) -> None:
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text(
        "sources: []\n"
        "alert_senders:\n"
        "  - type: gmail_alert\n"
        "    sender: jobs@linkedin.com\n"
        "    sender_slug: linkedin-alert\n"
        "    enabled: false\n"
        "  - type: gmail_alert\n"
        "    sender: alerts@indeed.com\n"
        "    sender_slug: indeed-alert\n"
        "    enabled: true\n"
    )
    cfg = load_sourcing_config(cfg_file)
    assert len(cfg.alert_senders) == 1
    assert cfg.alert_senders[0]["sender_slug"] == "indeed-alert"


def test_sourcing_config_default_has_empty_alert_senders() -> None:
    from jobsmith.sourcing.config import default_sourcing_config

    cfg = default_sourcing_config()
    assert cfg.alert_senders == []
