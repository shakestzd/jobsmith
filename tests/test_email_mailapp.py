"""Tests for the Mail.app adapter (feat-b1bd050e).

All tests run offline — mail-app CLI calls are fully mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "email_alerts"


def _run_fn_list(account: str, mailbox: str, messages: list[dict]):
    """Return a _run_mail_app replacement that returns *messages* for list commands."""
    def _run(*args, **kwargs):
        args_list = list(args)
        if "list" in args_list:
            return 0, json.dumps(messages), ""
        # view command returns empty
        return 1, "", "not implemented in mock"
    return _run


def _run_fn_view(messages: list[dict], bodies: dict[str, str]):
    """Return a _run_mail_app replacement that handles both list and view commands."""
    def _run(*args, **kwargs):
        args_list = list(args)
        if "list" in args_list:
            return 0, json.dumps(messages), ""
        if "view" in args_list:
            # Find --id value
            try:
                id_idx = args_list.index("--id")
                msg_id = args_list[id_idx + 1]
                body = bodies.get(msg_id, "")
                return 0, body, ""
            except (ValueError, IndexError):
                return 1, "", "no --id"
        return 1, "", "unknown command"
    return _run


# ---------------------------------------------------------------------------
# list_messages
# ---------------------------------------------------------------------------


def test_list_messages_parses_json() -> None:
    import jobsmith.sourcing.email.mailapp as mailapp_mod
    from jobsmith.sourcing.email.mailapp import list_messages

    msgs = [{"id": "1", "subject": "Jobs for you"}]
    original = mailapp_mod._run_mail_app

    def mock_run(*args, **kwargs):
        return 0, json.dumps(msgs), ""

    mailapp_mod._run_mail_app = mock_run
    try:
        result = list_messages("myaccount@gmail.com", "Job Alerts", limit=5)
        assert result == msgs
    finally:
        mailapp_mod._run_mail_app = original


def test_list_messages_error_returns_empty() -> None:
    import jobsmith.sourcing.email.mailapp as mailapp_mod
    from jobsmith.sourcing.email.mailapp import list_messages

    original = mailapp_mod._run_mail_app

    def mock_run(*args, **kwargs):
        return 1, "", "permission denied"

    mailapp_mod._run_mail_app = mock_run
    try:
        result = list_messages("myaccount@gmail.com", "Job Alerts")
        assert result == []
    finally:
        mailapp_mod._run_mail_app = original


def test_list_messages_bad_json_returns_empty() -> None:
    import jobsmith.sourcing.email.mailapp as mailapp_mod
    from jobsmith.sourcing.email.mailapp import list_messages

    original = mailapp_mod._run_mail_app

    def mock_run(*args, **kwargs):
        return 0, "NOT JSON", ""

    mailapp_mod._run_mail_app = mock_run
    try:
        result = list_messages("myaccount@gmail.com", "Job Alerts")
        assert result == []
    finally:
        mailapp_mod._run_mail_app = original


# ---------------------------------------------------------------------------
# ingest_mailapp_alerts
# ---------------------------------------------------------------------------


def test_ingest_mailapp_alerts_linkedin() -> None:
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    li_html = (FIXTURES_DIR / "linkedin_alert.html").read_text()
    messages = [{"id": "101", "subject": "New jobs for you"}]
    bodies = {"101": li_html}

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]
    postings, degraded = ingest_mailapp_alerts(
        senders,
        _run_fn=_run_fn_view(messages, bodies),
    )
    assert len(postings) >= 2
    assert degraded == []
    assert all(p["source"] == "mailapp/linkedin-alert" for p in postings)


def test_ingest_mailapp_alerts_degraded_on_no_parse() -> None:
    """When HTML parses to zero jobs, sender is recorded as degraded."""
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    messages = [{"id": "101", "subject": "No jobs"}]
    bodies = {"101": "<html><body>no jobs here</body></html>"}

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]
    postings, degraded = ingest_mailapp_alerts(
        senders,
        _run_fn=_run_fn_view(messages, bodies),
    )
    assert postings == []
    assert "linkedin-alert" in degraded


def test_ingest_mailapp_alerts_incomplete_config_degraded() -> None:
    """Config missing account/mailbox → degraded."""
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            # missing account and mailbox
        }
    ]

    def _noop(*args, **kwargs):
        return 0, "[]", ""

    postings, degraded = ingest_mailapp_alerts(senders, _run_fn=_noop)
    assert postings == []
    assert "linkedin-alert" in degraded


def test_ingest_mailapp_alerts_empty_mailbox() -> None:
    """Empty mailbox → no postings, not degraded."""
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "linkedin-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]

    def _empty_list(*args, **kwargs):
        return 0, "[]", ""

    postings, degraded = ingest_mailapp_alerts(senders, _run_fn=_empty_list)
    assert postings == []
    assert degraded == []


def test_ingest_mailapp_alerts_indeed() -> None:
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    html = (FIXTURES_DIR / "indeed_alert.html").read_text()
    messages = [{"id": "201", "subject": "Indeed Job Alert"}]
    bodies = {"201": html}

    senders = [
        {
            "type": "mailapp_alert",
            "sender_slug": "indeed-alert",
            "account": "me@example.com",
            "mailbox": "Job Alerts",
        }
    ]
    postings, degraded = ingest_mailapp_alerts(
        senders,
        _run_fn=_run_fn_view(messages, bodies),
    )
    assert len(postings) >= 2
    assert degraded == []
    assert all(p["source"] == "mailapp/indeed-alert" for p in postings)
