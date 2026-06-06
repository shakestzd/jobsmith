"""jobsmith.reuse.dedup — JD near-duplicate detection and artifact reuse.

Sits ABOVE the exact-input llm_cache (migration 008): catches near-duplicate
JDs that differ by minor rewording but describe the same role.

Design
------
- Fingerprint: SHA-256 content_hash of normalized JD text → application_fingerprints.
- Normalized JD text stored in run_metrics (metric_key="jd_normalized_text") so
  fuzzy comparisons can retrieve it without reversing the hash.  No new migration.
- Similarity: slice-2 _token_set_ratio — no new dependency.  Correctness-first:
  below dedup_threshold → regenerate.
- dedup_threshold always read from ReuseSettings — never hardcoded.
- Self-exclusion: current_slug is never returned as its own match.
- Slice 6 calls find_duplicate_jd; slice 7 uses a separate jd_overlap_warm_start_threshold.

Public API
----------
write_jd_fingerprint(conn, *, slug, jd_text) -> None
find_duplicate_jd(conn, *, jd_text, current_slug, cfg) -> DedupResult | None
load_prior_artifacts(*, state_dir) -> tuple[dict, dict]
DedupResult — dataclass: decision, matched_slug, similarity
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobsmith._state_readers import load_fit_score, load_jd_parsed
from jobsmith.config import ReuseSettings
from jobsmith.reuse.match import _token_set_ratio
from jobsmith.reuse.store import (
    content_hash,
    upsert_application_fingerprint,
    upsert_run_metric,
)

_WS_RE = re.compile(r"\s+")
_METRIC_KEY_JD_NORM = "jd_normalized_text"


def _normalize_jd(jd_text: str) -> str:
    return _WS_RE.sub(" ", jd_text.strip().lower())


@dataclass
class DedupResult:
    """Result of a near-duplicate JD lookup.

    decision:     "reuse" | "regenerate"
    matched_slug: prior application slug, or None when regenerating.
    similarity:   float 0.0–1.0 (1.0 = exact hash match).
    """

    decision: str
    matched_slug: str | None
    similarity: float


def write_jd_fingerprint(
    conn: sqlite3.Connection,
    *,
    slug: str,
    jd_text: str,
) -> None:
    """Persist slug → JD content_hash and normalized JD text.

    Two writes (both INSERT OR REPLACE):
    1. application_fingerprints: slug → SHA-256 of normalized JD.
    2. run_metrics: slug / "jd_normalized_text" → normalized text (for fuzzy lookup).
    """
    normalized = _normalize_jd(jd_text)
    jd_hash = content_hash(normalized)
    upsert_application_fingerprint(conn, slug=slug, content_hash=jd_hash)
    upsert_run_metric(conn, slug=slug, metric_key=_METRIC_KEY_JD_NORM, metric_value=normalized)


def find_duplicate_jd(
    conn: sqlite3.Connection,
    *,
    jd_text: str,
    current_slug: str,
    cfg: ReuseSettings,
) -> DedupResult | None:
    """Find a prior application whose JD is a near-duplicate of jd_text.

    Algorithm:
    1. Normalize jd_text; compute SHA-256 content_hash.
    2. Scan application_fingerprints (excluding current_slug).
    3. Exact hash match → return immediately (similarity=1.0).
    4. Otherwise: fuzzy token-set ratio vs stored normalized text from run_metrics.
    5. Return best match if score >= cfg.dedup_threshold, else None.

    Slice-6 callers: check ``result is not None`` to skip jd-parse/fit-score,
    then call load_prior_artifacts(state_dir=...) for the prior artifacts.
    Do NOT conflate cfg.dedup_threshold with cfg.jd_overlap_warm_start_threshold.
    """
    normalized = _normalize_jd(jd_text)
    current_hash = content_hash(normalized)

    rows = conn.execute(
        "SELECT slug, content_hash FROM application_fingerprints"
    ).fetchall()

    best_score = 0.0
    best_slug: str | None = None

    for row in rows:
        prior_slug = row[0]
        stored_hash = row[1]

        if prior_slug == current_slug:
            continue

        if stored_hash == current_hash:
            return DedupResult(decision="reuse", matched_slug=prior_slug, similarity=1.0)

        text_row = conn.execute(
            "SELECT metric_value FROM run_metrics WHERE slug = ? AND metric_key = ?",
            (prior_slug, _METRIC_KEY_JD_NORM),
        ).fetchone()

        if text_row is None:
            continue

        score = _token_set_ratio(normalized, text_row[0])
        if score > best_score:
            best_score = score
            best_slug = prior_slug

    if best_slug is not None and best_score >= cfg.dedup_threshold:
        return DedupResult(decision="reuse", matched_slug=best_slug, similarity=best_score)

    return None


def load_prior_artifacts(
    *,
    state_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (jd_parsed, fit_score) from a prior application's .apply-state dir.

    Both dicts are {} when the files are absent.
    """
    return load_jd_parsed(state_dir), load_fit_score(state_dir)


__all__ = [
    "DedupResult",
    "find_duplicate_jd",
    "load_prior_artifacts",
    "write_jd_fingerprint",
]
