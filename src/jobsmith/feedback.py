"""Feedback capture loop for jobsmith — records, lists, prunes, and exports
per-application diff records so future runs can learn from user edits.

Public API
----------
record(slug, *, app_dir, feedback_dir) -> list[dict]
list_records(filter_kind, since, *, feedback_dir) -> list[dict]
prune(older_than_days, *, feedback_dir) -> int
export(*, feedback_dir) -> str
lesson_placeholder(before, after) -> str
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Minimum character delta to count as a "significant" edit.
_SIGNIFICANT_THRESHOLD = 5

# Default subdirectory names.
_DEFAULT_FEEDBACK_SUBDIR = Path("private") / "feedback"
_DEFAULT_APPLICATIONS_SUBDIR = Path("private") / "applications"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Best-effort repo root: walk up from cwd looking for .apply-config.yaml."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".apply-config.yaml").exists():
            return parent
    return cwd


def _is_significant(before: str, after: str) -> bool:
    """Return True when the edit is larger than the threshold and not whitespace-only."""
    delta = abs(len(after) - len(before))
    if delta < _SIGNIFICANT_THRESHOLD:
        return False
    # Reject pure-whitespace diffs.
    return before.strip() != after.strip()


def _diff_bullets(agent_text: str, user_text: str) -> list[tuple[str, str]]:
    """Return (before, after) pairs for prose-bullet lines that differ significantly.

    Lines are matched positionally; extra lines on either side are treated as
    additions/deletions and emitted if the change is significant.
    """
    def _bullets(text: str) -> list[str]:
        return [ln.rstrip() for ln in text.splitlines() if ln.strip()]

    agent_lines = _bullets(agent_text)
    user_lines = _bullets(user_text)

    edits: list[tuple[str, str]] = []
    max_len = max(len(agent_lines), len(user_lines))
    for i in range(max_len):
        before = agent_lines[i] if i < len(agent_lines) else ""
        after = user_lines[i] if i < len(user_lines) else ""
        if before != after and _is_significant(before, after):
            edits.append((before, after))
    return edits


def _diff_paragraphs(agent_text: str, user_text: str) -> list[tuple[str, str]]:
    """Return (before, after) pairs for cover-letter paragraphs that differ significantly.

    Paragraphs are delimited by blank lines.
    """
    def _paras(text: str) -> list[str]:
        raw = text.strip().split("\n\n")
        return [p.strip() for p in raw if p.strip()]

    agent_paras = _paras(agent_text)
    user_paras = _paras(user_text)

    edits: list[tuple[str, str]] = []
    max_len = max(len(agent_paras), len(user_paras))
    for i in range(max_len):
        before = agent_paras[i] if i < len(agent_paras) else ""
        after = user_paras[i] if i < len(user_paras) else ""
        if before != after and _is_significant(before, after):
            edits.append((before, after))
    return edits


def _write_record(
    feedback_dir: Path,
    slug: str,
    kind: str,
    before: str,
    after: str,
    context: dict | None = None,
) -> dict:
    """Construct a feedback record dict, write it as JSON, and return it."""
    ts = datetime.now(timezone.utc).isoformat()
    record: dict = {
        "slug": slug,
        "timestamp": ts,
        "kind": kind,
        "before": before,
        "after": after,
        "lesson": lesson_placeholder(before, after),
        "context": context,
    }
    feedback_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = ts.replace(":", "-").replace("+", "p")
    path = feedback_dir / f"{slug}-{safe_ts}.json"
    path.write_text(json.dumps(record, indent=2))
    return record


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lesson_placeholder(before: str, after: str) -> str:  # noqa: ARG001
    """Placeholder heuristic — returns empty string; wave-3 will auto-suggest.

    The function signature is stable so prose-writer (wave 3) can call it.
    """
    return ""


def record(
    slug: str,
    *,
    app_dir: Path | None = None,
    feedback_dir: Path | None = None,
) -> list[dict]:
    """Diff user edits against agent output and write feedback JSON records.

    Parameters
    ----------
    slug:
        Application slug (directory name under private/applications/).
    app_dir:
        Override the application directory (default: <repo_root>/private/applications/<slug>).
    feedback_dir:
        Override the feedback directory (default: <repo_root>/private/feedback/).

    Returns
    -------
    list[dict]
        The feedback records that were produced (one per significant edit).
        An empty list is returned when no significant edits are found.
    """
    repo_root = _repo_root()
    if app_dir is None:
        app_dir = repo_root / _DEFAULT_APPLICATIONS_SUBDIR / slug
    if feedback_dir is None:
        feedback_dir = repo_root / _DEFAULT_FEEDBACK_SUBDIR

    doc_dir = app_dir / "documents"
    records: list[dict] = []

    # --- prose-draft diff ---
    prose_user = doc_dir / "prose-draft.md"
    prose_agent = doc_dir / "prose-draft-agent.md"
    if prose_user.exists() and prose_agent.exists():
        edits = _diff_bullets(prose_agent.read_text(), prose_user.read_text())
        for before, after in edits:
            r = _write_record(feedback_dir, slug, "prose-bullet", before, after)
            records.append(r)

    # --- cover-letter diff ---
    cl_user = doc_dir / "cover-letter-final.md"
    cl_agent = doc_dir / "cover-letter-agent.md"
    if cl_user.exists() and cl_agent.exists():
        edits = _diff_paragraphs(cl_agent.read_text(), cl_user.read_text())
        for before, after in edits:
            r = _write_record(feedback_dir, slug, "cover-letter-paragraph", before, after)
            records.append(r)

    return records


def list_records(
    filter_kind: str | None,
    since: datetime | None,
    *,
    feedback_dir: Path | None = None,
) -> list[dict]:
    """Load all feedback JSON files and return them as a list of dicts.

    Parameters
    ----------
    filter_kind:
        If given, only records with a matching ``kind`` are returned.
        E.g. ``"prose-bullet"`` or ``"cover-letter-paragraph"``.
    since:
        If given, only records whose ``timestamp`` is at or after this
        datetime are returned.
    feedback_dir:
        Override the feedback directory (default: <repo_root>/private/feedback/).
    """
    repo_root = _repo_root()
    if feedback_dir is None:
        feedback_dir = repo_root / _DEFAULT_FEEDBACK_SUBDIR

    if not feedback_dir.exists():
        return []

    results: list[dict] = []
    for path in sorted(feedback_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if filter_kind is not None and data.get("kind") != filter_kind:
            continue

        if since is not None:
            ts_str = data.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                since_aware = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
                if ts < since_aware:
                    continue
            except ValueError:
                pass

        results.append(data)

    return results


def prune(
    older_than_days: int,
    *,
    feedback_dir: Path | None = None,
) -> int:
    """Delete feedback records older than N days (by file mtime).

    Parameters
    ----------
    older_than_days:
        Records whose mtime is more than this many days in the past are deleted.
    feedback_dir:
        Override the feedback directory (default: <repo_root>/private/feedback/).

    Returns
    -------
    int
        The number of records deleted.
    """
    repo_root = _repo_root()
    if feedback_dir is None:
        feedback_dir = repo_root / _DEFAULT_FEEDBACK_SUBDIR

    if not feedback_dir.exists():
        return 0

    cutoff = time.time() - older_than_days * 86400
    deleted = 0
    for path in feedback_dir.glob("*.json"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            deleted += 1

    return deleted


def export(
    *,
    feedback_dir: Path | None = None,
) -> str:
    """Produce a sanitized YAML summary of recurring patterns for cross-machine sync.

    Groups records by ``kind``, emits lesson texts. Drops slug and per-app
    metadata (company, context) so the output is safe to commit or share.

    Parameters
    ----------
    feedback_dir:
        Override the feedback directory (default: <repo_root>/private/feedback/).

    Returns
    -------
    str
        YAML-formatted string suitable for writing to a file or stdout.
    """
    all_records = list_records(None, None, feedback_dir=feedback_dir)

    grouped: dict[str, list[dict]] = {}
    for r in all_records:
        kind = r.get("kind", "unknown")
        grouped.setdefault(kind, []).append(r)

    summary: dict = {}
    for kind, recs in grouped.items():
        lessons = [r.get("lesson", "") for r in recs]
        # Include all lessons (even empty ones so counts are correct).
        summary[kind] = {
            "count": len(recs),
            "lessons": lessons,
        }

    return yaml.dump(summary, default_flow_style=False, allow_unicode=True)


__all__ = [
    "export",
    "lesson_placeholder",
    "list_records",
    "prune",
    "record",
]
