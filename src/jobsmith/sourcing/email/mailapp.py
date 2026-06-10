"""Mail.app adapter for email job alert ingestion (feat-b1bd050e).

Uses the installed `mail-app` CLI (external tool) to read messages from Apple
Mail's local message store.  This is the fallback path for users who receive
job alert emails in Mail.app but have not configured Gmail API credentials.

Interface (probed from `mail-app --help`):
  mail-app messages list --account ACCT --mailbox MBOX --limit N --json
  mail-app messages view --id ID --account ACCT --mailbox MBOX --body-only

Design:
  - fetch_mailapp_messages() shells out to the `mail-app` CLI.
  - ingest_mailapp_alerts() iterates configured alert senders, lists
    recent messages from the configured mailbox, fetches body HTML,
    and delegates to the per-sender HTML parsers.
  - An unparseable alert is counted in degraded_senders but never crashes.
  - All subprocess calls use capture_output=True so stdout is clean.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

logger = logging.getLogger("jobsmith.sourcing.email.mailapp")

_MAIL_APP_BIN = "mail-app"


def _mail_app_available() -> bool:
    """Return True when the mail-app binary is on PATH."""
    return shutil.which(_MAIL_APP_BIN) is not None


def _run_mail_app(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run mail-app with *args*, return (returncode, stdout, stderr)."""
    cmd = [_MAIL_APP_BIN, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 1, "", "mail-app not found on PATH"
    except subprocess.TimeoutExpired:
        return 1, "", f"mail-app timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def list_messages(
    account: str,
    mailbox: str,
    limit: int = 20,
) -> list[dict]:
    """List messages from a Mail.app mailbox as JSON.

    Returns a list of message dicts (id, subject, sender, date_received, ...).
    Returns [] on error.
    """
    rc, stdout, stderr = _run_mail_app(
        "messages", "list",
        "--account", account,
        "--mailbox", mailbox,
        "--limit", str(limit),
        "--json",
    )
    if rc != 0:
        logger.warning("mail-app list failed (rc=%d): %s", rc, stderr.strip())
        return []
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            return data
        # Some versions wrap in {"messages": [...]}
        if isinstance(data, dict):
            return data.get("messages", [])
        return []
    except json.JSONDecodeError as exc:
        logger.warning("mail-app list JSON parse error: %s", exc)
        return []


def get_message_body(
    message_id: str,
    account: str,
    mailbox: str,
) -> str:
    """Fetch the body of a message by its mail-app numeric ID.

    Returns empty string on error.
    """
    rc, stdout, stderr = _run_mail_app(
        "messages", "view",
        "--id", str(message_id),
        "--account", account,
        "--mailbox", mailbox,
        "--body-only",
    )
    if rc != 0:
        logger.warning("mail-app view failed (rc=%d): %s", rc, stderr.strip())
        return ""
    return stdout


def fetch_mailapp_messages(
    account: str,
    mailbox: str,
    limit: int = 20,
) -> list[tuple[str, str]]:
    """Fetch recent messages from a Mail.app mailbox.

    Returns a list of (message_id, body_html) tuples.
    Empty list on any error.
    """
    messages = list_messages(account=account, mailbox=mailbox, limit=limit)
    results = []
    for msg in messages:
        msg_id = str(msg.get("id", ""))
        if not msg_id:
            continue
        body = get_message_body(msg_id, account=account, mailbox=mailbox)
        if body:
            results.append((msg_id, body))
    return results


def ingest_mailapp_alerts(
    alert_senders: list[dict],
    *,
    max_per_sender: int = 20,
    _run_fn=None,  # injectable for tests
) -> tuple[list[dict], list[str]]:
    """Ingest job alerts from Mail.app for each configured alert sender.

    Parameters
    ----------
    alert_senders:
        List of alert-sender config dicts from sourcing.yaml, e.g.::

            - type: mailapp_alert
              sender_slug: linkedin-alert
              account: shakestzd@gmail.com
              mailbox: Job Alerts

    max_per_sender:
        Cap on messages fetched per sender.
    _run_fn:
        Injectable replacement for _run_mail_app (for unit tests).

    Returns
    -------
    (postings, degraded_senders)
        postings: list of posting dicts (title/company/location/url/external_id/source)
        degraded_senders: list of sender slugs that failed or produced no postings
    """
    from .parsers import parse_alert_html

    if _run_fn is not None:
        import jobsmith.sourcing.email.mailapp as _self
        original = _self._run_mail_app

        def _patched(*args, **kwargs):
            return _run_fn(*args, **kwargs)

        _self._run_mail_app = _patched  # type: ignore[attr-defined]

    try:
        postings: list[dict] = []
        degraded: list[str] = []

        for sender_cfg in alert_senders:
            sender_slug = sender_cfg.get("sender_slug", "")
            account = sender_cfg.get("account", "")
            mailbox = sender_cfg.get("mailbox", "")
            source_name = f"mailapp/{sender_slug}"

            if not sender_slug or not account or not mailbox:
                logger.warning(
                    "mailapp_alert config incomplete (needs sender_slug, account, mailbox): %s",
                    sender_cfg,
                )
                degraded.append(sender_slug or "?")
                continue

            messages = fetch_mailapp_messages(
                account=account,
                mailbox=mailbox,
                limit=max_per_sender,
            )
            if not messages:
                logger.debug("no messages in mailbox %r for %s", mailbox, sender_slug)
                continue

            parsed_any = False
            for _msg_id, html in messages:
                entries = parse_alert_html(sender_slug, html)
                for entry in entries:
                    entry["source"] = source_name
                    postings.append(entry)
                    parsed_any = True

            if not parsed_any and messages:
                logger.warning(
                    "no postings parsed from mailbox %r (%d messages) for %s",
                    mailbox, len(messages), sender_slug,
                )
                degraded.append(sender_slug)

        return postings, degraded
    finally:
        if _run_fn is not None:
            import jobsmith.sourcing.email.mailapp as _self
            _self._run_mail_app = original  # type: ignore[attr-defined]
