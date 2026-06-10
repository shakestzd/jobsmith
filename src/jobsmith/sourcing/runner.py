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

logger = logging.getLogger("jobsmith.sourcing.runner")

CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_MAX_PER_SOURCE = 100
DEFAULT_GLOBAL_TIMEOUT_SEC = 300
DEFAULT_EXPIRY_DAYS = 21

# Rate limit between source fetches (seconds)
_INTER_SOURCE_SLEEP = 1.0


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
    """Stable dedup key for the postings store. SHA-256 of company|title|canonical_url."""
    title = (role.title or "").strip().lower()
    company = (role.company or "").strip().lower()
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
        Seam for slice 4 (feat-1602d64c). Currently a no-op — LLM rescoring
        is NOT done in this slice regardless of this flag.
    source_filter:
        When set, only crawl sources whose ``type/slug`` key matches this
        string (e.g. ``greenhouse/stripe``). Useful for ``--source X`` CLI.

    Returns
    -------
    dict with summary keys:
        run_id, sources_checked, error_counts, degraded_sources,
        roles_fetched, roles_upserted, roles_expired, aborted.
    """
    run_id = str(uuid.uuid4())
    summary: dict = {
        "run_id": run_id,
        "aborted": False,
        "sources_checked": [],
        "error_counts": {},
        "degraded_sources": [],
        "roles_fetched": 0,
        "roles_upserted": 0,
        "roles_expired": 0,
    }

    if not sources:
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
                if error_counts[key] >= CIRCUIT_BREAKER_THRESHOLD:
                    degraded.append(key)
                continue

            # Cap per-source pull
            if len(fetched) > max_per_source:
                fetched = fetched[:max_per_source]

            summary["sources_checked"].append(key)
            summary["roles_fetched"] += len(fetched)

            if not dry_run:
                for role in fetched:
                    dedup_key = role_dedup_key(role)
                    score_dict = score_role_fast(role.jd_text or "")
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
                        fast_score=float(score_dict.get("score_a", 0)) / 100.0,
                        llm_score=None,
                        specialty=score_dict.get("dominant_specialty") or None,
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
                    else:
                        updated_count += 1
                summary["roles_upserted"] += len(fetched)

            time.sleep(_INTER_SOURCE_SLEEP)

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
        raise
    finally:
        conn.close()

    return summary
