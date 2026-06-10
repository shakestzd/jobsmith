"""Gmail adapter for email job alert ingestion (feat-b1bd050e).

Reads email job alerts from Gmail using the Gmail API (read-only scope).
Credentials and token paths are configured via environment variables:
  JOBSMITH_GMAIL_CREDENTIALS_FILE  — path to OAuth2 client_secret.json
  JOBSMITH_GMAIL_TOKEN_FILE        — path to cached token.json (auto-created)

The Gmail API is accessed via google-api-python-client + google-auth-oauthlib.
Auth is interactive on first run (browser OAuth flow); subsequent runs use the
cached token (auto-refreshed via google-auth).

Design:
  - build_gmail_service() returns an authenticated Gmail Resource object.
    Raises RuntimeError when credentials are missing/invalid.
  - fetch_alert_messages() searches for messages FROM a configured sender
    address, returns (message_id, html_body) tuples.
  - ingest_gmail_alerts() is the top-level function: for each configured
    alert sender, fetches + parses HTML, returns a list of Role objects.
  - An unparseable alert is counted in degraded_senders but never crashes.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

logger = logging.getLogger("jobsmith.sourcing.email.gmail")

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Defaults resolved from env; callers may override.
_DEFAULT_CREDENTIALS = os.environ.get("JOBSMITH_GMAIL_CREDENTIALS_FILE", "")
_DEFAULT_TOKEN = os.environ.get("JOBSMITH_GMAIL_TOKEN_FILE", "")


def build_gmail_service(
    credentials_file: str | Path | None = None,
    token_file: str | Path | None = None,
):
    """Return an authenticated Gmail API Resource (read-only).

    Parameters
    ----------
    credentials_file:
        Path to OAuth2 client_secret.json.  Falls back to
        JOBSMITH_GMAIL_CREDENTIALS_FILE env var.
    token_file:
        Path to the cached token.json.  Falls back to
        JOBSMITH_GMAIL_TOKEN_FILE env var.

    Raises
    ------
    RuntimeError
        When credentials_file is not found or the OAuth flow fails.
    ImportError
        When google-api-python-client or google-auth-oauthlib are not installed.
    """
    from google.auth.transport.requests import Request  # type: ignore[import]
    from google.oauth2.credentials import Credentials  # type: ignore[import]
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import]
    from googleapiclient.discovery import build  # type: ignore[import]

    cred_path = Path(credentials_file or _DEFAULT_CREDENTIALS)
    tok_path = Path(token_file or _DEFAULT_TOKEN) if (token_file or _DEFAULT_TOKEN) else None

    creds = None
    if tok_path and tok_path.exists():
        creds = Credentials.from_authorized_user_file(str(tok_path), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cred_path.exists():
                raise RuntimeError(
                    f"Gmail credentials file not found: {cred_path}. "
                    "Set JOBSMITH_GMAIL_CREDENTIALS_FILE or pass credentials_file."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), _SCOPES)
            creds = flow.run_local_server(port=0)

        if tok_path:
            tok_path.parent.mkdir(parents=True, exist_ok=True)
            tok_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _extract_html_body(payload: dict) -> str:
    """Recursively extract the HTML body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _extract_html_body(part)
        if result:
            return result
    return ""


def fetch_alert_messages(
    service,
    sender_email: str,
    max_results: int = 20,
) -> list[tuple[str, str]]:
    """Fetch recent messages from *sender_email* in the Gmail inbox.

    Returns a list of (message_id, html_body) tuples.
    Empty list on any API error (caller records degraded).
    """
    try:
        query = f"from:{sender_email}"
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
        ).execute()

        messages = resp.get("messages", [])
        results = []
        for msg in messages:
            msg_id = msg["id"]
            detail = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full",
            ).execute()
            html = _extract_html_body(detail.get("payload", {}))
            if html:
                results.append((msg_id, html))
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_alert_messages failed for %s: %s", sender_email, exc)
        return []


def ingest_gmail_alerts(
    alert_senders: list[dict],
    *,
    service=None,
    credentials_file: str | Path | None = None,
    token_file: str | Path | None = None,
    max_per_sender: int = 20,
) -> tuple[list[dict], list[str]]:
    """Ingest job alerts from Gmail for each configured alert sender.

    Parameters
    ----------
    alert_senders:
        List of alert-sender config dicts from sourcing.yaml, e.g.::

            - type: gmail_alert
              sender: jobs-noreply@linkedin.com
              sender_slug: linkedin-alert

    service:
        Pre-built Gmail API Resource (for tests; built from credentials if None).
    credentials_file, token_file:
        Passed through to build_gmail_service() when service is None.
    max_per_sender:
        Cap on messages fetched per sender.

    Returns
    -------
    (postings, degraded_senders)
        postings: list of posting dicts (title/company/location/url/external_id/source)
        degraded_senders: list of sender slugs that failed to parse
    """
    from .parsers import parse_alert_html

    if service is None:
        try:
            service = build_gmail_service(
                credentials_file=credentials_file,
                token_file=token_file,
            )
        except Exception as exc:
            logger.error("Gmail service build failed: %s", exc)
            return [], [s.get("sender_slug", s.get("sender", "?")) for s in alert_senders]

    postings: list[dict] = []
    degraded: list[str] = []

    for sender_cfg in alert_senders:
        sender_email = sender_cfg.get("sender", "")
        sender_slug = sender_cfg.get("sender_slug", sender_email.split("@")[0])
        source_name = f"gmail/{sender_slug}"

        if not sender_email:
            logger.warning("alert sender config missing 'sender' field: %s", sender_cfg)
            degraded.append(sender_slug)
            continue

        messages = fetch_alert_messages(
            service, sender_email, max_results=max_per_sender
        )
        if not messages:
            # No messages is not degraded — inbox may simply be empty
            logger.debug("no messages from %s", sender_email)
            continue

        parsed_any = False
        for _msg_id, html in messages:
            entries = parse_alert_html(sender_slug, html)
            for entry in entries:
                entry["source"] = source_name
                postings.append(entry)
                parsed_any = True

        if not parsed_any and messages:
            logger.warning("no postings parsed from %s (%d messages)", sender_email, len(messages))
            degraded.append(sender_slug)

    return postings, degraded
