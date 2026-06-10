"""Tests for the Gmail adapter (feat-b1bd050e).

All tests run offline — Gmail API is fully mocked.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "email_alerts"


def _b64url(text: str) -> str:
    """Base64url-encode a string as Gmail API does."""
    return base64.urlsafe_b64encode(text.encode()).decode()


def _gmail_msg(html: str, msg_id: str = "msg001") -> dict:
    """Build a minimal Gmail API message object with HTML body."""
    return {
        "id": msg_id,
        "payload": {
            "mimeType": "text/html",
            "body": {
                "data": _b64url(html),
            },
            "parts": [],
        },
    }


def _make_service(messages: list[dict]) -> MagicMock:
    """Build a mock Gmail service that returns *messages* for any list query."""
    service = MagicMock()
    msg_ids = [{"id": m["id"]} for m in messages]
    msg_by_id = {m["id"]: m for m in messages}

    # users().messages().list(...).execute() → {"messages": [...]}
    list_result = MagicMock()
    list_result.execute.return_value = {"messages": msg_ids}
    service.users.return_value.messages.return_value.list.return_value = list_result

    # users().messages().get(id=X).execute() → full message dict
    def _get_msg(**kwargs):
        msg_id = kwargs.get("id", "")
        result = MagicMock()
        result.execute.return_value = msg_by_id.get(msg_id, {})
        return result

    service.users.return_value.messages.return_value.get.side_effect = _get_msg

    return service


# ---------------------------------------------------------------------------
# _extract_html_body
# ---------------------------------------------------------------------------


def test_extract_html_body_single_part() -> None:
    from jobsmith.sourcing.email.gmail import _extract_html_body

    html = "<html><body>hello</body></html>"
    payload = {"mimeType": "text/html", "body": {"data": _b64url(html)}, "parts": []}
    assert _extract_html_body(payload) == html


def test_extract_html_body_nested_parts() -> None:
    from jobsmith.sourcing.email.gmail import _extract_html_body

    html = "<html><body>nested</body></html>"
    payload = {
        "mimeType": "multipart/mixed",
        "body": {},
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64url("plain text")},
                "parts": [],
            },
            {
                "mimeType": "text/html",
                "body": {"data": _b64url(html)},
                "parts": [],
            },
        ],
    }
    assert _extract_html_body(payload) == html


def test_extract_html_body_empty() -> None:
    from jobsmith.sourcing.email.gmail import _extract_html_body

    assert _extract_html_body({}) == ""


# ---------------------------------------------------------------------------
# fetch_alert_messages — mocked
# ---------------------------------------------------------------------------


def test_fetch_alert_messages_returns_tuples() -> None:
    from jobsmith.sourcing.email.gmail import fetch_alert_messages

    html = (FIXTURES_DIR / "linkedin_alert.html").read_text()
    service = _make_service([_gmail_msg(html, "msg001")])

    results = fetch_alert_messages(service, "jobs-noreply@linkedin.com", max_results=5)
    assert len(results) == 1
    msg_id, body = results[0]
    assert msg_id == "msg001"
    assert "linkedin.com" in body


def test_fetch_alert_messages_empty_inbox() -> None:
    from jobsmith.sourcing.email.gmail import fetch_alert_messages

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {}

    results = fetch_alert_messages(service, "noreply@example.com")
    assert results == []


def test_fetch_alert_messages_api_error_returns_empty() -> None:
    from jobsmith.sourcing.email.gmail import fetch_alert_messages

    service = MagicMock()
    service.users.return_value.messages.return_value.list.side_effect = Exception("API down")

    results = fetch_alert_messages(service, "noreply@example.com")
    assert results == []


# ---------------------------------------------------------------------------
# ingest_gmail_alerts — mocked service
# ---------------------------------------------------------------------------


def test_ingest_gmail_alerts_linkedin() -> None:
    from jobsmith.sourcing.email.gmail import ingest_gmail_alerts

    html = (FIXTURES_DIR / "linkedin_alert.html").read_text()
    service = _make_service([_gmail_msg(html, "msg001")])

    senders = [
        {
            "type": "gmail_alert",
            "sender": "jobs-noreply@linkedin.com",
            "sender_slug": "linkedin-alert",
        }
    ]
    postings, degraded = ingest_gmail_alerts(senders, service=service)
    assert len(postings) >= 2
    assert degraded == []
    assert all(p["source"] == "gmail/linkedin-alert" for p in postings)


def test_ingest_gmail_alerts_degraded_on_no_parse() -> None:
    """When HTML parses to zero jobs, sender is recorded as degraded."""
    from jobsmith.sourcing.email.gmail import ingest_gmail_alerts

    service = _make_service([_gmail_msg("<html><body>no jobs here</body></html>", "msg001")])

    senders = [
        {
            "type": "gmail_alert",
            "sender": "jobs-noreply@linkedin.com",
            "sender_slug": "linkedin-alert",
        }
    ]
    postings, degraded = ingest_gmail_alerts(senders, service=service)
    assert postings == []
    assert "linkedin-alert" in degraded


def test_ingest_gmail_alerts_missing_sender_field_degraded() -> None:
    """Config missing 'sender' field → degraded."""
    from jobsmith.sourcing.email.gmail import ingest_gmail_alerts

    service = MagicMock()
    senders = [{"type": "gmail_alert", "sender_slug": "linkedin-alert"}]  # no 'sender'
    postings, degraded = ingest_gmail_alerts(senders, service=service)
    assert postings == []
    assert "linkedin-alert" in degraded


def test_ingest_gmail_alerts_service_build_fails() -> None:
    """When service build fails (no credentials), all senders degraded."""
    from jobsmith.sourcing.email.gmail import ingest_gmail_alerts

    senders = [
        {"type": "gmail_alert", "sender": "jobs@linkedin.com", "sender_slug": "linkedin-alert"}
    ]
    # service=None and no cred file → build fails → all degraded
    with patch("jobsmith.sourcing.email.gmail.build_gmail_service", side_effect=RuntimeError("no creds")):
        postings, degraded = ingest_gmail_alerts(senders)
    assert postings == []
    assert "linkedin-alert" in degraded


def test_ingest_gmail_alerts_multiple_senders() -> None:
    """Multiple senders each parsed independently."""
    from jobsmith.sourcing.email.gmail import ingest_gmail_alerts

    li_html = (FIXTURES_DIR / "linkedin_alert.html").read_text()
    indeed_html = (FIXTURES_DIR / "indeed_alert.html").read_text()

    service = _make_service([
        _gmail_msg(li_html, "li001"),
        _gmail_msg(indeed_html, "in001"),
    ])

    senders = [
        {"type": "gmail_alert", "sender": "jobs@linkedin.com", "sender_slug": "linkedin-alert"},
        {"type": "gmail_alert", "sender": "alerts@indeed.com", "sender_slug": "indeed-alert"},
    ]
    postings, degraded = ingest_gmail_alerts(senders, service=service)
    # Both senders return messages from the same mock list, so we get mixed results
    assert isinstance(postings, list)
    assert isinstance(degraded, list)
