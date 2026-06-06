"""jobsmith.reuse — pipeline result reuse and caching layer.

Public API (used by slices 2-9):
  store.content_hash       — stable SHA over normalized inputs
  store.is_fresh           — hash + TTL staleness check
  store.upsert_canonical_requirement
  store.get_canonical_requirement
  store.upsert_requirement_evidence
  store.get_requirement_evidence
  store.upsert_application_fingerprint
  store.get_application_fingerprint
  store.upsert_run_metric
  store.get_run_metrics
"""
from __future__ import annotations

from jobsmith.reuse.store import (
    content_hash,
    get_application_fingerprint,
    get_canonical_requirement,
    get_requirement_evidence,
    get_run_metrics,
    is_fresh,
    upsert_application_fingerprint,
    upsert_canonical_requirement,
    upsert_requirement_evidence,
    upsert_run_metric,
)

__all__ = [
    "content_hash",
    "is_fresh",
    "upsert_canonical_requirement",
    "get_canonical_requirement",
    "upsert_requirement_evidence",
    "get_requirement_evidence",
    "upsert_application_fingerprint",
    "get_application_fingerprint",
    "upsert_run_metric",
    "get_run_metrics",
]
