"""Deterministic crawler orchestrator for jobsmith (feat-5531c54b).

Ports run_crawl from shakestzd/private/scripts/source_jobs.py into the
package with these changes:
  - Uses slice-1 store (upsert_posting, upsert_sourcing_run, finish_sourcing_run,
    set_posting_status) instead of JSONL file output + SQLite cache.
  - Auto-expires postings not re-sighted for expiry_days (default 21).
  - Accepts --no-llm flag (seam for slice 4, feat-1602d64c — currently a no-op).
  - Per-source exception isolation + circuit breaker (CIRCUIT_BREAKER_THRESHOLD).

Design notes
------------
- run_crawl is deterministic (given the same set of sources). Each adapter is
  called exactly once per source spec. Failures are isolated: a crashing adapter
  increments error_counts but does NOT stop other sources from being fetched.
- Circuit breaker: after CIRCUIT_BREAKER_THRESHOLD failures for one source key,
  the source is added to degraded_sources in the summary.
- Auto-expiry: after all upserts, any posting with last_seen_at older than
  expiry_days and status='sourced' or 'queued' is marked expired.
- Dedup via slice-1 store: upsert_posting uses INSERT OR IGNORE + UPDATE
  last_seen_at, so re-sighted postings are updated without creating duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..db import open_pipeline_db
from ..sourcing.adapters.base import ATSSourceAdapter, Role
from ..sourcing.scoring import score_role_fast
from ..sourcing.store import (
    finish_sourcing_run,
    set_posting_status,
    upsert_posting,
    upsert_sourcing_run,
)

# Email ingestion seam (feat-b1bd050e) — imported lazily in run_email_alerts()

logger = logging.getLogger("jobsmith.sourcing.runner")

CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_MAX_PER_SOURCE = 100
DEFAULT_GLOBAL_TIMEOUT_SEC = 300
DEFAULT_EXPIRY_DAYS = 21

# Rate limit between source fetches (seconds)
_INTER_SOURCE_SLEEP = 1.0


# ---------------------------------------------------------------------------
# Title + score filter (feat-e32cde37)
# ---------------------------------------------------------------------------


def apply_title_filters(
    roles: list[Role],
    *,
    exclude_patterns: list[str],
    include_patterns: list[str],
    min_fast_score: float,
    scored_roles: dict[str, float] | None,
) -> tuple[list[Role], int]:
    """Filter *roles* by title patterns and/or fast_score.

    Parameters
    ----------
    roles:
        Input list of Role objects to evaluate.
    exclude_patterns:
        Substring patterns (case-insensitive).  A role whose title contains
        any of these is dropped.
    include_patterns:
        Allowlist mode.  When non-empty, a role must match at least one
        pattern to be kept (after the exclude check).
    min_fast_score:
        Roles whose entry in *scored_roles* is below this value are dropped.
        0.0 disables score gating.  Roles with no score entry pass through.
    scored_roles:
        Mapping of ``role.id`` → fast_score (float in [0, 1]).  When None,
        score gating is skipped entirely.

    Returns
    -------
    (kept, filtered_count)
    """
    kept: list[Role] = []
    filtered = 0

    for role in roles:
        title_l = (role.title or "").lower()

        # 1. Exclude check (highest priority)
        if exclude_patterns and any(p in title_l for p in exclude_patterns):
            logger.debug("title-exclude: %s — %s", role.company, role.title)
            filtered += 1
            continue

        # 2. Include allowlist check
        if include_patterns and not any(p in title_l for p in include_patterns):
            logger.debug("title-include-miss: %s — %s", role.company, role.title)
            filtered += 1
            continue

        # 3. min_fast_score check (only when scoring map is provided)
        if min_fast_score > 0.0 and scored_roles is not None:
            score = scored_roles.get(role.id)
            if score is not None and score < min_fast_score:
                logger.debug(
                    "score-filter: %s — %s (score=%.3f < %.3f)",
                    role.company,
                    role.title,
                    score,
                    min_fast_score,
                )
                filtered += 1
                continue

        kept.append(role)

    return kept, filtered


# ---------------------------------------------------------------------------
# Canonical helpers (ported from source_jobs.py)
# ---------------------------------------------------------------------------


def canonical_url(url: str) -> str:
    """Strip query string + trailing slash + lowercase host for stable dedup."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.netloc or "").lower()
    path = (parts.path or "").rstrip("/")
    return urlunsplit((scheme, host, path, "", ""))


def role_dedup_key(role: Role) -> str:
    """Stable dedup key for the postings store.

    When an external_id is present (e.g. Indeed jk=, LinkedIn job id), it is
    included in the hash base so two jobs with the same company+title but
    different external IDs (e.g. two Indeed listings) produce distinct keys.
    Falls back to company|title|canonical_url when no external_id is available.
    """
    title = (role.title or "").strip().lower()
    company = (role.company or "").strip().lower()
    ext_id = (role.id or "").strip()
    if ext_id:
        base = f"{company}|{title}|{ext_id}"
    else:
        base = f"{company}|{title}|{canonical_url(role.url)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


def default_adapter_factory(spec: dict) -> ATSSourceAdapter | None:
    """Build an adapter for a source spec.

    Passes the canonical `company` (or `name`) from sourcing.yaml into the
    adapter as `company_name` so parsed Roles carry the human-readable company
    name ("Oscar Health") instead of slug.title() fallbacks ("Oscarhealth").
    """
    t = spec.get("type")
    canonical_name = spec.get("company") or spec.get("name") or None

    if t == "greenhouse":
        from .adapters.greenhouse import GreenhouseAdapter

        return GreenhouseAdapter(company_name=canonical_name)

    if t == "lever":
        from .adapters.lever import LeverAdapter

        return LeverAdapter(company_name=canonical_name)

    if t == "ashby":
        try:
            from .adapters.ashby import AshbyAdapter

            return AshbyAdapter(company_name=canonical_name)
        except Exception as exc:
            logger.warning("ashby adapter unavailable: %s", exc)
            return None

    if t == "hn_whos_hiring":
        try:
            from .adapters.hn_whos_hiring import HNWhosHiringAdapter

            return HNWhosHiringAdapter()
        except Exception as exc:
            logger.warning("hn adapter unavailable: %s", exc)
            return None

    if t == "climatebase":
        try:
            from .adapters.climatebase import ClimatebaseAdapter

            return ClimatebaseAdapter()
        except Exception as exc:
            logger.warning("climatebase adapter unavailable: %s", exc)
            return None

    logger.warning("unknown source type: %s", t)
    return None


# ---------------------------------------------------------------------------
# Auto-expiry
# ---------------------------------------------------------------------------


def expire_stale_postings(
    conn,
    *,
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
) -> int:
    """Mark postings with status='sourced'|'queued' as expired when not re-sighted.

    A posting is considered stale if last_seen_at < (now - expiry_days).
    dismissed/promoted postings are never touched.

    Returns the count of rows expired.
    """
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=expiry_days)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT id FROM postings
        WHERE status IN ('sourced', 'queued')
          AND last_seen_at < ?
        """,
        (cutoff,),
    ).fetchall()

    for row in rows:
        set_posting_status(conn, posting_id=row["id"], status="expired")

    return len(rows)


# ---------------------------------------------------------------------------
# Email alert ingestion (feat-b1bd050e)
# ---------------------------------------------------------------------------


def run_email_alerts(
    conn,
    alert_senders: list[dict],
    *,
    dry_run: bool = False,
    max_per_sender: int = 20,
    _gmail_ingest_fn: Callable | None = None,
    _mailapp_ingest_fn: Callable | None = None,
) -> tuple[int, list[int], list[str]]:
    """Ingest email job alerts and upsert postings into the DB.

    Dispatches to the Gmail or Mail.app adapter based on each sender's type
    field (gmail_alert or mailapp_alert). Email postings carry snippets only
    (title/company/location/url) — full JD text is fetched at promote time.

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    alert_senders:
        Enabled alert-sender config dicts from sourcing.yaml.
    dry_run:
        When True, parse but do not write to DB.
    max_per_sender:
        Cap on messages fetched per sender.

    Returns
    -------
    (upserted, new_posting_ids, degraded_senders)
        upserted: total posting rows written
        new_posting_ids: list of posting IDs for newly inserted rows (for LLM rescore seam)
        degraded_senders: list of sender slugs that failed to produce postings
    """
    from .email.gmail import ingest_gmail_alerts as _default_gmail_ingest
    from .email.mailapp import ingest_mailapp_alerts as _default_mailapp_ingest

    _gmail_fn = _gmail_ingest_fn or _default_gmail_ingest
    _mailapp_fn = _mailapp_ingest_fn or _default_mailapp_ingest

    gmail_senders = [s for s in alert_senders if s.get("type") == "gmail_alert"]
    mailapp_senders = [s for s in alert_senders if s.get("type") == "mailapp_alert"]

    all_postings: list[dict] = []
    all_degraded: list[str] = []

    if gmail_senders:
        try:
            postings, degraded = _gmail_fn(
                gmail_senders, max_per_sender=max_per_sender
            )
            all_postings.extend(postings)
            all_degraded.extend(degraded)
        except Exception as exc:
            logger.warning("Gmail ingestion failed (non-fatal): %s", exc)
            all_degraded.extend(
                s.get("sender_slug", "?") for s in gmail_senders
            )

    if mailapp_senders:
        try:
            postings, degraded = _mailapp_fn(
                mailapp_senders, max_per_sender=max_per_sender
            )
            all_postings.extend(postings)
            all_degraded.extend(degraded)
        except Exception as exc:
            logger.warning("Mail.app ingestion failed (non-fatal): %s", exc)
            all_degraded.extend(
                s.get("sender_slug", "?") for s in mailapp_senders
            )

    upserted = 0
    new_posting_ids: list[int] = []

    if not dry_run:
        for entry in all_postings:
            source = entry.get("source", "email/unknown")
            url = entry.get("url", "")
            title = entry.get("title", "")
            company = entry.get("company", "")
            location = entry.get("location", "")
            external_id = entry.get("external_id", "")

            # Build a Role-compatible dedup key from the snippet data
            from .adapters.base import Role as _Role
            role = _Role(
                id=external_id,
                source=source.split("/")[0],
                source_slug=source.split("/", 1)[-1] if "/" in source else source,
                company=company,
                title=title,
                location=location,
                url=url,
                jd_text="",
            )
            dedup_key = role_dedup_key(role)
            score_dict = score_role_fast("")  # snippets have no JD text

            posting_id = upsert_posting(
                conn,
                source=source,
                dedup_key=dedup_key,
                external_id=external_id,
                url=url,
                title=title,
                company=company,
                location=location,
                comp_text=None,
                posted_date=None,
                jd_text="",
                fast_score=float(score_dict.get("score_a", 0)) / 100.0,
                llm_score=None,
                specialty=None,
                rationale=None,
                evidence_json=None,
            )
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at FROM postings WHERE id = ?",
                (posting_id,),
            ).fetchone()
            if row and row["first_seen_at"] == row["last_seen_at"]:
                new_posting_ids.append(posting_id)
            upserted += 1

    return upserted, new_posting_ids, all_degraded


# ---------------------------------------------------------------------------
# Main crawl
# ---------------------------------------------------------------------------


def run_crawl(
    db_path: Path,
    sources: list[dict],
    *,
    adapter_factory: Callable[[dict], ATSSourceAdapter | None] = default_adapter_factory,
    max_per_source: int = DEFAULT_MAX_PER_SOURCE,
    global_timeout_sec: int = DEFAULT_GLOBAL_TIMEOUT_SEC,
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
    dry_run: bool = False,
    no_llm: bool = False,
    source_filter: str | None = None,
    # Title filters (feat-e32cde37) — applied after fast-score, before upsert.
    title_exclude_patterns: list[str] | None = None,
    title_include_patterns: list[str] | None = None,
    min_fast_score: float = 0.0,
    # Email alert ingestion (feat-b1bd050e) — list of alert-sender config dicts
    alert_senders: list[dict] | None = None,
    # Injectable for tests — replaces the entire run_email_alerts call
    # Signature: (conn, senders, *, dry_run, max_per_sender) -> (upserted, new_ids, degraded)
    _run_email_alerts_fn: Callable | None = None,
    # LLM rescore seam (feat-1602d64c) — injectable for tests; defaults to SDK
    _query_fn: Callable | None = None,
    _rescore_n_cap: int = 30,
    _rescore_budget_usd: float = 1.0,
) -> dict:
    """End-to-end crawl against the slice-1 postings store.

    Parameters
    ----------
    db_path:
        Path to the jobsmith.db pipeline database.
    sources:
        List of source spec dicts from sourcing.yaml (already filtered by
        the caller — disabled sources should be excluded before passing in).
    adapter_factory:
        Callable that returns an ATSSourceAdapter for a spec dict. Defaults
        to default_adapter_factory. Overridable in tests.
    max_per_source:
        Cap on roles fetched per source (prevents runaway sources).
    global_timeout_sec:
        Wall-clock deadline for the entire crawl (seconds).
    expiry_days:
        Postings not re-sighted within this many days are expired.
    dry_run:
        When True, adapters are called but no DB writes occur.
    no_llm:
        When True, skip the LLM triage rescore pass entirely (fast_score only).
    source_filter:
        When set, only crawl sources whose ``type/slug`` key matches this
        string (e.g. ``greenhouse/stripe``). Useful for ``--source X`` CLI.

    Returns
    -------
    dict with summary keys:
        run_id, sources_checked, error_counts, degraded_sources,
        roles_fetched, roles_upserted, roles_expired, aborted.
    """
    _exclude = [p.lower() for p in (title_exclude_patterns or [])]
    _include = [p.lower() for p in (title_include_patterns or [])]

    run_id = str(uuid.uuid4())
    summary: dict = {
        "run_id": run_id,
        "aborted": False,
        "sources_checked": [],
        "error_counts": {},
        "degraded_sources": [],
        "roles_fetched": 0,
        "roles_upserted": 0,
        "roles_filtered": 0,
        "roles_expired": 0,
    }

    _alert_senders = alert_senders or []
    if not sources and not _alert_senders:
        logger.info("no enabled sources — exiting clean")
        return summary

    conn = open_pipeline_db(db_path)
    try:
        if not dry_run:
            upsert_sourcing_run(conn, run_id=run_id)

        deadline = time.monotonic() + global_timeout_sec
        error_counts: dict[str, int] = {}
        degraded: list[str] = []
        new_count = 0
        updated_count = 0
        new_posting_ids: list[int] = []  # seam: track new rows for LLM rescore

        for spec in sources:
            if time.monotonic() > deadline:
                logger.warning("global timeout reached — stopping crawl")
                break

            key = f"{spec.get('type')}/{spec.get('slug')}"

            # --source filter
            if source_filter and key != source_filter:
                continue

            adapter = adapter_factory(spec)
            if adapter is None:
                error_counts[key] = error_counts.get(key, 0) + 1
                continue

            try:
                fetched: list[Role] = list(adapter.fetch(spec.get("slug", "")))
            except Exception as exc:
                logger.warning("adapter %s failed: %s", key, exc)
                error_counts[key] = error_counts.get(key, 0) + 1
                # Mark degraded immediately on any adapter failure within this run
                if key not in degraded:
                    degraded.append(key)
                continue

            # Cap per-source pull
            if len(fetched) > max_per_source:
                fetched = fetched[:max_per_source]

            summary["sources_checked"].append(key)
            summary["roles_fetched"] += len(fetched)

            if not dry_run:
                # Score all roles first so min_fast_score can apply
                scored: dict[str, float] = {}
                score_dicts: dict[str, dict] = {}
                for role in fetched:
                    sd = score_role_fast(role.jd_text or "")
                    score_dicts[role.id] = sd
                    scored[role.id] = float(sd.get("score_a", 0)) / 100.0

                # Apply title + score filters before upsert
                filtered_roles, n_filtered = apply_title_filters(
                    fetched,
                    exclude_patterns=_exclude,
                    include_patterns=_include,
                    min_fast_score=min_fast_score,
                    scored_roles=scored if min_fast_score > 0.0 else None,
                )
                summary["roles_filtered"] += n_filtered

                for role in filtered_roles:
                    dedup_key = role_dedup_key(role)
                    sd = score_dicts[role.id]
                    posting_id = upsert_posting(
                        conn,
                        source=f"{role.source}/{role.source_slug}",
                        dedup_key=dedup_key,
                        external_id=role.id,
                        url=role.url,
                        title=role.title,
                        company=role.company,
                        location=role.location,
                        comp_text=None,
                        posted_date=role.posted_date or None,
                        jd_text=role.jd_text,
                        fast_score=scored[role.id],
                        llm_score=None,
                        specialty=sd.get("dominant_specialty") or None,
                        rationale=None,
                        evidence_json=None,
                    )
                    # Check if this was a new insert or re-sight
                    row = conn.execute(
                        "SELECT first_seen_at, last_seen_at FROM postings WHERE id = ?",
                        (posting_id,),
                    ).fetchone()
                    if row and row["first_seen_at"] == row["last_seen_at"]:
                        new_count += 1
                        new_posting_ids.append(posting_id)  # seam: track new rows
                    else:
                        updated_count += 1
                summary["roles_upserted"] += len(filtered_roles)

            time.sleep(_INTER_SOURCE_SLEEP)

        # Email alert ingestion (feat-b1bd050e) — runs after ATS sources
        if _alert_senders:
            try:
                _email_fn = _run_email_alerts_fn or run_email_alerts
                email_upserted, email_new_ids, email_degraded = _email_fn(
                    conn,
                    _alert_senders,
                    dry_run=dry_run,
                    max_per_sender=max_per_source,
                )
                # Apply title filters to email postings (feat-e32cde37).
                # Email postings have no JD text; we filter purely by title.
                # We prune matching rows from the DB (dismiss them) and
                # remove their IDs from new_posting_ids so the LLM pass
                # doesn't bother with them.
                email_filtered = 0
                if not dry_run and ((_exclude or _include) or min_fast_score > 0.0):
                    filtered_email_ids: list[int] = []
                    kept_email_new_ids: list[int] = []
                    for pid in email_new_ids:
                        row = conn.execute(
                            "SELECT title, fast_score FROM postings WHERE id = ?", (pid,)
                        ).fetchone()
                        if row is None:
                            kept_email_new_ids.append(pid)
                            continue
                        title_l = (row["title"] or "").lower()
                        # Exclude check
                        if _exclude and any(p in title_l for p in _exclude):
                            filtered_email_ids.append(pid)
                            continue
                        # Include allowlist check
                        if _include and not any(p in title_l for p in _include):
                            filtered_email_ids.append(pid)
                            continue
                        # min_fast_score check for email postings
                        if min_fast_score > 0.0:
                            score = row["fast_score"] or 0.0
                            if score < min_fast_score:
                                filtered_email_ids.append(pid)
                                continue
                        kept_email_new_ids.append(pid)

                    if filtered_email_ids:
                        from .store import set_posting_status as _set_status

                        for pid in filtered_email_ids:
                            _set_status(conn, posting_id=pid, status="dismissed")
                        email_filtered = len(filtered_email_ids)
                        email_upserted -= email_filtered
                        email_new_ids = kept_email_new_ids

                summary["roles_upserted"] += email_upserted
                summary["roles_fetched"] += email_upserted + email_filtered
                summary["roles_filtered"] += email_filtered
                new_count += len(email_new_ids)
                new_posting_ids.extend(email_new_ids)
                degraded.extend(email_degraded)
                if (email_upserted + email_filtered) > 0:
                    summary["sources_checked"].append("email_alerts")
            except Exception as exc:
                logger.warning("email alert ingestion failed (non-fatal): %s", exc)
                degraded.append("email_alerts")

        # LLM triage rescore seam (feat-1602d64c)
        if not dry_run and not no_llm and new_posting_ids:
            from .llm_rescore import rescore_postings  # local import avoids hard dep

            try:
                rescore_results = rescore_postings(
                    conn,
                    posting_ids=new_posting_ids,
                    no_llm=False,
                    n_cap=_rescore_n_cap,
                    budget_usd=_rescore_budget_usd,
                    query_fn=_query_fn,
                )
                rescored_count = len(rescore_results)
                fallback_count = sum(1 for r in rescore_results if r.is_fallback)
                logger.info(
                    "LLM rescore: %d rescored, %d fallback",
                    rescored_count,
                    fallback_count,
                )
                summary["llm_rescored"] = rescored_count
                summary["llm_fallback"] = fallback_count
            except Exception as exc:
                logger.warning("LLM rescore pass failed (non-fatal): %s", exc)
                summary["llm_rescored"] = 0
                summary["llm_fallback"] = 0

        # Auto-expiry pass
        if not dry_run:
            expired = expire_stale_postings(conn, expiry_days=expiry_days)
            summary["roles_expired"] = expired

        # Finish the sourcing_run record
        if not dry_run:
            status = "degraded" if degraded else "done"
            finish_sourcing_run(
                conn,
                run_id=run_id,
                status=status,
                new_count=new_count,
                updated_count=updated_count,
                skipped_count=0,
                filtered_count=summary["roles_filtered"],
                degraded_sources=degraded or None,
            )

        summary["error_counts"] = error_counts
        summary["degraded_sources"] = degraded

    except Exception as exc:
        import contextlib

        logger.error("run_crawl failed: %s", exc, exc_info=True)
        summary["aborted"] = True
        if not dry_run:
            with contextlib.suppress(Exception):
                finish_sourcing_run(
                    conn,
                    run_id=run_id,
                    status="failed",
                    error=str(exc),
                )
        _notify_failure(str(exc))
        raise
    finally:
        conn.close()

    # Notify on degraded (non-fatal failure)
    if not dry_run and summary["degraded_sources"]:
        _notify_failure(
            f"{len(summary['degraded_sources'])} source(s) degraded: "
            + ", ".join(summary["degraded_sources"])
        )

    return summary


def _notify_failure(message: str) -> None:
    """Fire a macOS notification on sourcing failure (best-effort, non-fatal).

    Uses osascript to display a Notification Center alert.  Silently no-ops
    on non-macOS platforms or when osascript is unavailable.
    """
    import platform
    import subprocess as _sp

    if platform.system() != "Darwin":
        return
    try:
        # Escape backslashes first, then double quotes, to prevent AppleScript injection.
        safe_message = message[:200].replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'display notification '
            f'"{safe_message}" '
            'with title "jobsmith sourcing" '
            'subtitle "run failed or degraded"'
        )
        _sp.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except Exception as notify_exc:
        logger.debug("macOS notification failed (non-fatal): %s", notify_exc)
