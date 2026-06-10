"""Tests for the Mail.app adapter (feat-b1bd050e).

All tests run offline — mail-app CLI calls and osascript calls are fully mocked.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "email_alerts"

# ---------------------------------------------------------------------------
# Minimal MIME raw source for osascript / MIME extraction tests
# ---------------------------------------------------------------------------

_HTML_JOBS = """\
<html><head><meta charset="utf-8"></head><body>
<table>
<tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/3001100001/?trackingId=R&amp;midToken=R&amp;trk=t0">
</a>
</td><td>
<a href="https://www.linkedin.com/comm/jobs/view/3001100001/?trackingId=R&amp;midToken=R&amp;trk=t1">
<table><tr><td>Senior Data Engineer</td></tr>
<tr><td>Acme Systems &#xB7; United States (Remote)</td></tr></table>
</a>
</td></tr>
<tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/3001100002/?trackingId=R&amp;midToken=R&amp;trk=t2">
</a>
</td><td>
<a href="https://www.linkedin.com/comm/jobs/view/3001100002/?trackingId=R&amp;midToken=R&amp;trk=t3">
<table><tr><td>Machine Learning Engineer</td></tr>
<tr><td>Beta Solutions &#xB7; Remote, CA</td></tr></table>
</a>
</td></tr>
</table>
</body></html>"""

_ENCODED_HTML = base64.b64encode(_HTML_JOBS.encode("utf-8")).decode("ascii")

_RAW_MIME = f"""MIME-Version: 1.0
Date: Wed, 10 Jun 2026 15:41:14 -0700
From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>
To: test@example.com
Subject: New jobs for you
Message-ID: <test-fixture-001@linkedin.com>
Content-Type: multipart/alternative; boundary="boundary123"

--boundary123
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 7bit

Senior Data Engineer at Acme Systems.

--boundary123
Content-Type: text/html; charset=UTF-8
Content-Transfer-Encoding: base64

{_ENCODED_HTML}

--boundary123--
"""


def _run_fn_list(account: str, mailbox: str, messages: list[dict]):
    """Return a _run_mail_app replacement that returns *messages* for list commands."""
    def _run(*args, **kwargs):
        args_list = list(args)
        if "list" in args_list:
            return 0, json.dumps(messages), ""
        return 1, "", "not implemented in mock"
    return _run


def _run_fn_view(messages: list[dict], bodies: dict[str, str]):
    """Return (_run_fn, _source_fn) pair for ingest_mailapp_alerts offline tests.

    _run_fn handles the ``list`` sub-command (returns *messages*).
    _source_fn handles per-message source lookup (returns body HTML from *bodies*).

    ``bodies`` values are returned directly as the MIME source; since they are
    already decoded HTML strings, extract_html_from_mime will find no text/html
    part.  We therefore wrap each body in a minimal MIME envelope so the stdlib
    email decoder returns the HTML correctly.
    """
    import base64

    def _run(*args, **kwargs):
        args_list = list(args)
        if "list" in args_list:
            return 0, json.dumps(messages), ""
        return 1, "", "unknown command"

    def _source(msg_id: str, account: str, mailbox: str) -> str:
        html_body = bodies.get(msg_id, "")
        if not html_body:
            return ""
        # Wrap in a minimal MIME envelope so extract_html_from_mime works
        encoded = base64.b64encode(html_body.encode("utf-8")).decode("ascii")
        return (
            "MIME-Version: 1.0\n"
            "Content-Type: multipart/alternative; boundary=\"b\"\n\n"
            "--b\n"
            "Content-Type: text/html; charset=UTF-8\n"
            "Content-Transfer-Encoding: base64\n\n"
            f"{encoded}\n\n"
            "--b--\n"
        )

    # Return as a tuple so callers can unpack _run_fn and _source_fn
    return _run, _source


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
    run_fn, source_fn = _run_fn_view(messages, bodies)

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
        _run_fn=run_fn,
        _source_fn=source_fn,
    )
    assert len(postings) >= 2
    assert degraded == []
    assert all(p["source"] == "mailapp/linkedin-alert" for p in postings)


def test_ingest_mailapp_alerts_degraded_on_no_parse() -> None:
    """When HTML parses to zero jobs, sender is recorded as degraded."""
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    messages = [{"id": "101", "subject": "No jobs"}]
    bodies = {"101": "<html><body>no jobs here</body></html>"}
    run_fn, source_fn = _run_fn_view(messages, bodies)

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
        _run_fn=run_fn,
        _source_fn=source_fn,
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
    run_fn, source_fn = _run_fn_view(messages, bodies)

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
        _run_fn=run_fn,
        _source_fn=source_fn,
    )
    assert len(postings) >= 2
    assert degraded == []
    assert all(p["source"] == "mailapp/indeed-alert" for p in postings)


# ---------------------------------------------------------------------------
# MIME extraction via osascript (new raw-source path)
# ---------------------------------------------------------------------------


def test_extract_html_from_raw_mime_base64() -> None:
    """extract_html_from_mime correctly decodes a base64-encoded text/html part."""
    from jobsmith.sourcing.email.mailapp import extract_html_from_mime

    html = extract_html_from_mime(_RAW_MIME)
    assert html is not None
    assert "Senior Data Engineer" in html
    assert "linkedin.com/comm/jobs/view/" in html


def test_extract_html_from_raw_mime_no_html_part() -> None:
    """Returns None when MIME has no text/html part."""
    from jobsmith.sourcing.email.mailapp import extract_html_from_mime

    plain_only = (
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n\n"
        "Just plain text."
    )
    result = extract_html_from_mime(plain_only)
    assert result is None


def test_get_message_source_calls_osascript(monkeypatch) -> None:
    """get_message_source() calls osascript and returns its stdout."""
    import jobsmith.sourcing.email.mailapp as mailapp_mod

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        class FakeProc:
            returncode = 0
            stdout = _RAW_MIME
            stderr = ""
        return FakeProc()

    monkeypatch.setattr(mailapp_mod.subprocess, "run", fake_run)
    result = mailapp_mod.get_message_source("42", "me@example.com", "Job Alerts")
    assert result == _RAW_MIME
    assert "osascript" in captured["cmd"]


def test_get_message_source_returns_empty_on_error(monkeypatch) -> None:
    """get_message_source() returns '' when osascript fails."""
    import jobsmith.sourcing.email.mailapp as mailapp_mod

    def fake_run(cmd, **kwargs):
        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = "AppleScript error"
        return FakeProc()

    monkeypatch.setattr(mailapp_mod.subprocess, "run", fake_run)
    result = mailapp_mod.get_message_source("99", "me@example.com", "Job Alerts")
    assert result == ""


def test_fetch_mailapp_messages_uses_raw_source(monkeypatch) -> None:
    """fetch_mailapp_messages returns parsed HTML from MIME source via osascript."""
    import jobsmith.sourcing.email.mailapp as mailapp_mod

    messages = [{"id": "301", "subject": "LinkedIn jobs"}]

    def fake_list(*args, **kwargs):
        return 0, json.dumps(messages), ""

    def fake_source(msg_id, account, mailbox):
        return _RAW_MIME

    monkeypatch.setattr(mailapp_mod, "_run_mail_app", fake_list)
    monkeypatch.setattr(mailapp_mod, "get_message_source", fake_source)

    results = mailapp_mod.fetch_mailapp_messages("me@example.com", "Job Alerts")
    assert len(results) == 1
    msg_id, html = results[0]
    assert msg_id == "301"
    assert "Senior Data Engineer" in html
    assert "<html" in html.lower()


def test_ingest_mailapp_alerts_real_structure_fixture() -> None:
    """ingest_mailapp_alerts parses the real-structure LinkedIn fixture correctly."""
    from jobsmith.sourcing.email.mailapp import ingest_mailapp_alerts

    real_html = (FIXTURES_DIR / "linkedin_alert_real.html").read_text()
    messages = [{"id": "101", "subject": "Jobs for you"}]
    bodies = {"101": real_html}
    run_fn, source_fn = _run_fn_view(messages, bodies)

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
        _run_fn=run_fn,
        _source_fn=source_fn,
    )
    assert len(postings) >= 3, f"expected >=3 postings, got {len(postings)}"
    assert degraded == []
    for p in postings:
        assert "linkedin.com/jobs/view/" in p["url"], f"bad url: {p['url']}"
        assert "?" not in p["url"], f"tracking params in url: {p['url']}"
        assert p["title"]
        assert p["company"]
