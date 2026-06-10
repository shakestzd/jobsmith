"""Mail.app adapter for email job alert ingestion (feat-b1bd050e).

Uses the installed `mail-app` CLI (external tool) to list messages from Apple
Mail's local message store.  Raw MIME source is retrieved via AppleScript
(osascript) since `mail-app` has no raw-source flag — the --body-only flag
returns plain-text rendering with all hrefs stripped.

Interface (probed from `mail-app --help`):
  mail-app messages list --account ACCT --mailbox MBOX --limit N --json
  (message source retrieved via osascript, not mail-app view)

Design:
  - list_messages() shells out to the `mail-app` CLI.
  - get_message_source() fetches the raw MIME source via AppleScript.
  - extract_html_from_mime() decodes the text/html MIME part using stdlib email.
  - fetch_mailapp_messages() combines list + source + MIME extraction.
  - ingest_mailapp_alerts() iterates configured alert senders, lists
    recent messages, extracts HTML from raw MIME, and delegates to parsers.
  - An unparseable alert is counted in degraded_senders but never crashes.
  - All subprocess calls use capture_output=True so stdout is clean.
  - _run_fn / osascript call are injectable for offline unit tests.
"""

from __future__ import annotations

import email as _email_stdlib
import email.policy
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
    *,
    _run_fn=None,
) -> list[dict]:
    """List messages from a Mail.app mailbox as JSON.

    Returns a list of message dicts (id, subject, sender, date_received, ...).
    Returns [] on error.

    Parameters
    ----------
    _run_fn:
        Injectable replacement for _run_mail_app (for unit tests).
    """
    run_fn = _run_fn or _run_mail_app
    rc, stdout, stderr = run_fn(
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


def extract_html_from_mime(raw_source: str) -> str | None:
    """Extract and decode the text/html part from a raw MIME message string.

    Handles quoted-printable and base64 transfer encodings.  Uses the stdlib
    ``email`` module exclusively.

    Parameters
    ----------
    raw_source:
        Full raw MIME source as returned by AppleScript ``source`` property.

    Returns
    -------
    Decoded HTML string, or None if no text/html part is found.
    """
    try:
        msg = _email_stdlib.message_from_string(raw_source, policy=email.policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_content()
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("MIME extraction failed: %s", exc)
        return None


def _as_script(
    account: str,
    mailbox: str,
    msg_id: str,
) -> str:
    """Build the AppleScript source to fetch a message's raw MIME.

    Embeds values using AppleScript double-quoted strings with any embedded
    double-quote characters escaped as ``\" & quote & \"``.  Single-quoted
    Python repr is *not* used because AppleScript treats unquoted apostrophes
    as string delimiters in some contexts, which breaks comparisons.
    """

    def _esc(value: str) -> str:
        # Escape embedded double-quotes: replace " with \" & quote & \"
        # (AppleScript string concatenation via &)
        if '"' not in value:
            return f'"{value}"'
        parts = value.split('"')
        return '("' + '" & quote & "'.join(parts) + '")'

    return (
        'tell application "Mail"\n'
        f'    set acct to first account whose name is {_esc(account)}\n'
        f'    set mbox to mailbox {_esc(mailbox)} of acct\n'
        '    set targetMsg to missing value\n'
        '    repeat with m in (messages of mbox)\n'
        f'        if (id of m as string) is {_esc(msg_id)} then\n'
        '            set targetMsg to m\n'
        '            exit repeat\n'
        '        end if\n'
        '    end repeat\n'
        '    if targetMsg is missing value then\n'
        '        return ""\n'
        '    end if\n'
        '    return source of targetMsg\n'
        'end tell'
    )


def get_message_source(
    message_id: str,
    account: str,
    mailbox: str,
    *,
    timeout: int = 60,
) -> str:
    """Fetch the raw MIME source of a message via AppleScript / osascript.

    Returns the raw MIME source string, or empty string on error.
    The returned string can be passed to ``extract_html_from_mime``.
    """
    script = _as_script(
        account=account,
        mailbox=mailbox,
        msg_id=str(message_id),
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(
                "osascript get_message_source failed (rc=%d) msg_id=%s: %s",
                proc.returncode,
                message_id,
                proc.stderr.strip(),
            )
            return ""
        return proc.stdout
    except subprocess.TimeoutExpired:
        logger.warning("osascript timed out after %ds for msg_id=%s", timeout, message_id)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("osascript error for msg_id=%s: %s", message_id, exc)
        return ""


def fetch_mailapp_messages(
    account: str,
    mailbox: str,
    limit: int = 20,
    *,
    _run_fn=None,
    _source_fn=None,
) -> list[tuple[str, str]]:
    """Fetch recent messages from a Mail.app mailbox as (message_id, html) pairs.

    Uses AppleScript to retrieve the raw MIME source for each message, then
    decodes the text/html part.  Returns empty list on any error.

    Parameters
    ----------
    _run_fn:
        Injectable replacement for _run_mail_app (for tests).  Defaults to
        the module-level ``_run_mail_app``.
    _source_fn:
        Injectable replacement for get_message_source (for tests).  Called as
        ``_source_fn(msg_id, account, mailbox)`` and should return raw MIME
        source or an already-decoded HTML string.  Defaults to the module-level
        ``get_message_source``.
    """
    run_fn = _run_fn or _run_mail_app
    source_fn = _source_fn or get_message_source

    messages = list_messages(account=account, mailbox=mailbox, limit=limit, _run_fn=run_fn)
    results = []
    for msg in messages:
        msg_id = str(msg.get("id", ""))
        if not msg_id:
            continue
        raw_source = source_fn(msg_id, account=account, mailbox=mailbox)
        if not raw_source:
            continue
        html = extract_html_from_mime(raw_source)
        if html:
            results.append((msg_id, html))
    return results


def ingest_mailapp_alerts(
    alert_senders: list[dict],
    *,
    max_per_sender: int = 20,
    _run_fn=None,   # injectable replacement for _run_mail_app (list calls)
    _source_fn=None,  # injectable replacement for get_message_source
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
        Injectable replacement for _run_mail_app (for unit tests — handles
        ``list`` sub-command).  Threaded into fetch_mailapp_messages; module
        globals are never mutated.
    _source_fn:
        Injectable replacement for get_message_source (for unit tests — called
        as ``_source_fn(msg_id, account, mailbox)`` and should return raw MIME
        source or an already-decoded HTML string).  Threaded as a parameter,
        not a global mutation.

    Returns
    -------
    (postings, degraded_senders)
        postings: list of posting dicts (title/company/location/url/external_id/source)
        degraded_senders: list of sender slugs that failed or produced no postings
    """
    from .parsers import parse_alert_html

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
            _run_fn=_run_fn,
            _source_fn=_source_fn,
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
