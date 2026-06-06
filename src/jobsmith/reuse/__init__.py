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

  canonicalize.canonicalize  — (tag | None, normalized_phrase)
  taxonomy.load_taxonomy     — load versioned tag/alias seed
  taxonomy.resolve_tag       — alias → canonical tag
  match.match                — tiered matcher (exact_tag → phrase → fuzzy)
  match.MatchResult          — typed result for slice-4 and slice-5 consumers

  evidence_map.populate_from_bullet_selection — write req→bullet mapping rows
  evidence_map.lookup_mapped_bullet           — read fresh bullet for a requirement

  company_cache.normalize_company_key  — strip legal suffixes, produce stable slug
  company_cache.check_cache            — cache hit/miss with normalized key + TTL
  company_cache.write_cache            — write research to normalized cache path
  company_cache.record_company_research_metric — write reused/generated signal to run_metrics
"""
from __future__ import annotations

from jobsmith.reuse.canonicalize import canonicalize
from jobsmith.reuse.company_cache import (
    METRIC_KEY_COMPANY_RESEARCH_SOURCE,
    check_cache,
    normalize_company_key,
    record_company_research_metric,
    write_cache,
)
from jobsmith.reuse.evidence_map import lookup_mapped_bullet, populate_from_bullet_selection
from jobsmith.reuse.match import MatchResult, match
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
from jobsmith.reuse.taxonomy import load_taxonomy, resolve_tag

__all__ = [
    "METRIC_KEY_COMPANY_RESEARCH_SOURCE",
    "MatchResult",
    "canonicalize",
    "check_cache",
    "content_hash",
    "get_application_fingerprint",
    "get_canonical_requirement",
    "get_requirement_evidence",
    "get_run_metrics",
    "is_fresh",
    "load_taxonomy",
    "lookup_mapped_bullet",
    "match",
    "normalize_company_key",
    "populate_from_bullet_selection",
    "record_company_research_metric",
    "resolve_tag",
    "upsert_application_fingerprint",
    "upsert_canonical_requirement",
    "upsert_requirement_evidence",
    "upsert_run_metric",
    "write_cache",
]
