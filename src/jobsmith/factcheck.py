"""Fact checker for drafted cover letters and narrative answers.

When a cover letter or narrative drafter produces text containing hard
claims (dollar amounts, percentages, company names, year counts, count
metrics, proper nouns), this module greps every claim against the master
YAML files in `assets/content/`. Any claim that doesn't appear in ANY
master file is flagged as unverified.

**This is a safety gate, not an optimization.** Silent fabrication is
the worst failure mode for an automated application pipeline. The
checker is deliberately aggressive — false positives (rejecting true
claims) over false negatives (passing fabricated ones).

The CLI surface is `jobsmith fact-check`. The Python API is
`check_draft(draft_text, content_dir) -> FactCheckResult`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .anchors import _MONEY_RE, _PERCENT_RE

# ---------- types ----------


@dataclass
class Claim:
    """One hard claim extracted from a draft."""

    text: str
    kind: str  # 'money' | 'percent' | 'year_count' | 'count' | 'proper_noun'
    offset: int


@dataclass
class VerificationResult:
    """Outcome of verifying one claim against all master files."""

    claim: str
    kind: str
    verified: bool
    source_file: str | None = None


@dataclass
class FactCheckResult:
    """Aggregate result over all claims in a draft."""

    passed: bool
    verified_claims: list[VerificationResult] = field(default_factory=list)
    failed_claims: list[str] = field(default_factory=list)


# ---------- factcheck-specific extractors ----------
# (Money and percent come from jobsmith.anchors — single source of truth.)

# "for 3.5 years", "over 7 years", "10+ years"
_YEAR_COUNT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

# Multi-word proper nouns: two+ capitalized words in a row, min 4 chars
# each to avoid two-letter false positives.
_MULTI_CAP_RE = re.compile(r"\b[A-Z][a-zA-Z]{3,}(?:[ \t]+[A-Z][a-zA-Z]{3,})+\b")

# CamelCase company names — catches "SunStrong", "DagsterLabs", etc.
_CAMEL_NAME_RE = re.compile(r"\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b")

# Countable metrics: "7 pipelines", "200K assets", "7 automated ETL pipelines".
# Negative lookbehind prevents matching mid-number: "25B" inside "$4.25B",
# or "000" inside "3,000". The lookbehind covers digit, decimal, comma, and $.
_COUNT_RE = re.compile(
    r"(?<![\d.,$])\b\d+[KMB]?\+?\s+(?:[\w\-]+\s+){0,3}"
    r"(?:pipelines?|assets?|systems?|states?|clients?|"
    r"customers?|employees?|engineers?|applications?|reports?|dashboards?|"
    r"databases?|warehouses?|projects?|teams?|companies|markets?)\b",
    re.IGNORECASE,
)

# Multi-word names with lowercase connectors: "Technology and Policy",
# "State of Massachusetts", "Bureau of Labor Statistics".
_CONNECTED_CAP_RE = re.compile(
    r"\b[A-Z][a-zA-Z]{3,}"
    r"(?:[ \t]+(?:and|of|the|in|for|at|on|to|de|la|le|du|van|von)[ \t]+)"
    r"[A-Z][a-zA-Z]{3,}"
    r"(?:[ \t]+[A-Z][a-zA-Z]{3,})*"
    r"\b"
)

# Short acronyms (2-5 all-caps chars). Stoplist filters out generic skill
# acronyms so only claim-worthy ones (MIT, IBM, IRS) make it through.
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")


# ---------- stoplist ----------

_PROPER_NOUN_STOPLIST = {
    # Grammar
    "The", "This", "That", "These", "Those", "Their", "There", "Then",
    "They", "When", "Where", "What", "Which", "Who", "How", "Why",
    "Dear", "Hello", "Hi",
    "I", "We", "You",
    # Dates
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    "Sunday",
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
    # Generic skill acronyms
    "SQL", "AI", "ML", "ETL", "ELT", "API", "CLI", "GUI", "SAAS",
    "PDF", "YAML", "JSON", "XML", "HTML", "CSS", "JS", "TS", "TSX",
    "RAG", "LLM", "LLMS", "NLP", "CV", "OCR", "SDK", "IDE",
    "AWS", "GCP", "S3", "EC2",
    "CEO", "CTO", "CFO", "VP", "COO", "CIO",
    "USA", "US", "UK", "EU",
    "HR", "HQ", "IT", "QA", "PR",
    # Draft/letter boilerplate
    "Cover Letter", "Hiring Team",
    # Jobsmith resume section headers — emitted by prose-writer agent
    "Professional Summary", "Tailored Bullets", "Work Experience",
    "Technical Skills", "Core Competencies", "Selected Experience",
    "Key Achievements", "Selected Projects",
}


# ---------- public API ----------


def extract_hard_claims(text: str) -> list[Claim]:
    """Pull every hard claim out of a draft.

    Order is deterministic — money → percent → years → counts → proper nouns —
    so tests and logs are stable. Duplicates are tolerated and dedup happens
    in `check_draft`.
    """
    out: list[Claim] = []
    for m in _MONEY_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="money", offset=m.start()))
    for m in _PERCENT_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="percent", offset=m.start()))
    for m in _YEAR_COUNT_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="year_count", offset=m.start()))
    for m in _COUNT_RE.finditer(text):
        # Reduce "7 automated ETL pipelines" → "7 pipelines" for uniqueness.
        raw = m.group(0).strip()
        parts = raw.split()
        claim_text = f"{parts[0]} {parts[-1]}" if len(parts) >= 2 else raw
        out.append(Claim(text=claim_text, kind="count", offset=m.start()))
    for m in _CONNECTED_CAP_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="proper_noun", offset=m.start()))
    for m in _MULTI_CAP_RE.finditer(text):
        span = m.group(0).strip()
        if span in _PROPER_NOUN_STOPLIST:
            continue
        out.append(Claim(text=span, kind="proper_noun", offset=m.start()))
    for m in _CAMEL_NAME_RE.finditer(text):
        span = m.group(0).strip()
        if span in _PROPER_NOUN_STOPLIST:
            continue
        out.append(Claim(text=span, kind="proper_noun", offset=m.start()))
    for m in _ACRONYM_RE.finditer(text):
        span = m.group(0).strip()
        if span in _PROPER_NOUN_STOPLIST:
            continue
        out.append(Claim(text=span, kind="proper_noun", offset=m.start()))
    return out


# ---------- verifier ----------


def _load_master_content(content_dir: Path) -> dict[str, str]:
    """Read every .yml file in content_dir into a {name: text} dict."""
    out: dict[str, str] = {}
    if not content_dir.exists():
        return out
    for path in sorted(content_dir.glob("*.yml")):
        try:
            out[path.name] = path.read_text()
        except OSError:
            continue
    return out


def load_db_master_content() -> dict[str, str]:
    """Load canonical master blobs from ``master_content`` when a repo DB exists.

    Disk YAMLs can lag behind edits made in the web UI. The fact checker should
    still work in standalone fixture tests, so failures here intentionally return
    an empty mapping and let the caller fall back to file-backed sources.
    """
    try:
        from .config import find_config, load_config
        from .db import open_pipeline_db
        from .paths import repo_root_for

        config_path = find_config(Path.cwd())
        if config_path is None:
            return {}
        config = load_config(config_path)
        repo_root = repo_root_for()
        db_path = (repo_root / config.output.jobsmith_db).resolve()
        if not db_path.exists():
            return {}
        conn = open_pipeline_db(db_path)
        try:
            rows = conn.execute(
                "SELECT section, content_blob FROM master_content"
            ).fetchall()
        finally:
            conn.close()
        return {
            f"db:master_content:{row['section']}": row["content_blob"]
            for row in rows
            if row["content_blob"]
        }
    except Exception:
        return {}


def load_jd_context_for_draft(draft: Path) -> dict[str, str]:
    """Find the sibling ``jd-parsed.json`` for an application draft, if present."""
    candidates: list[Path] = []
    if draft.parent.name == ".apply-state":
        candidates.append(draft.parent / "jd-parsed.json")
    candidates.append(draft.parent / ".apply-state" / "jd-parsed.json")
    candidates.append(draft.parent.parent / ".apply-state" / "jd-parsed.json")

    out: dict[str, str] = {}
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        try:
            raw = path.read_text()
            parsed = json.loads(raw)
            compact = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            out[f"jd:{path.name}"] = raw + "\n" + compact
            return out
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _auto_detect_kind(claim: str) -> str:
    """Infer the claim kind from its shape."""
    s = claim.strip()
    if s.startswith("$") or s.endswith(("K", "M", "B")):
        return "money"
    if s.endswith("%"):
        return "percent"
    if re.search(r"\b(?:years?|yrs?)\b", s, re.IGNORECASE):
        return "year_count"
    return "proper_noun"


def _claim_matches(claim: str, haystack: str, kind: str) -> bool:
    if kind == "count":
        # Count claims are stored as "{numeric_token} {noun_lemma}" (e.g. "200K+ asset").
        # The two tokens may not be contiguous in the master ("200K+ solar asset portfolio"),
        # so verify by requiring BOTH the numeric token (whole-word) AND the noun lemma
        # (case-insensitive substring) to appear somewhere in the haystack.
        parts = claim.split(None, 1)
        if len(parts) < 2:
            # Single-token fallback — whole-token match.
            escaped = re.escape(claim)
            pattern = rf"(?:^|[\s'\"\[\(]){escaped}(?:[\s,.!?:;+'\"\]\)]|$)"
            return bool(re.search(pattern, haystack))
        numeric, noun = parts[0], parts[1]
        # Numeric token: whole-token match with trailing + allowed.
        num_escaped = re.escape(numeric)
        num_pattern = rf"(?:^|[\s'\"\[\(]){num_escaped}(?:[\s,.!?:;+'\"\]\)]|$)"
        if not re.search(num_pattern, haystack):
            return False
        # Noun: case-insensitive substring — accept singular or plural forms.
        noun_lower = noun.lower().rstrip("s")  # strip trailing 's' for base lemma
        return noun_lower in haystack.lower()
    if kind in {"money", "percent", "year_count"}:
        # Whole-token match — "$25" must not match "$250M".
        # Trailing boundary includes '+' so "$1B" matches "$1B+" in master,
        # while the leading boundary stays strict to prevent left-side mis-matches.
        escaped = re.escape(claim)
        pattern = rf"(?:^|[\s'\"\[\(]){escaped}(?:[\s,.!?:;+'\"\]\)]|$)"
        return bool(re.search(pattern, haystack))
    return claim.lower() in haystack.lower()


def verify_claim(
    claim: str,
    content_dir: Path,
    kind: str | None = None,
    extra_sources: dict[str, str] | None = None,
) -> VerificationResult:
    """Check a single claim against every master YAML file.

    Returns the first matching file (by sorted name), or a not-verified
    result if none match. When `kind` is None, the claim's shape is
    auto-detected — `$25`, `97%`, `3 years`, and `SunStrong` all route to
    the right matcher.
    """
    effective_kind = kind or _auto_detect_kind(claim)
    master = _load_master_content(content_dir)
    if extra_sources:
        master.update(extra_sources)
    for name, body in master.items():
        if _claim_matches(claim, body, effective_kind):
            return VerificationResult(
                claim=claim, kind=effective_kind, verified=True, source_file=name
            )
    return VerificationResult(claim=claim, kind=effective_kind, verified=False)


def check_draft(
    draft_text: str,
    content_dir: Path,
    extra_sources: dict[str, str] | None = None,
) -> FactCheckResult:
    """Extract every hard claim from the draft and verify against master files."""
    claims = extract_hard_claims(draft_text)
    verified: list[VerificationResult] = []
    failed: list[str] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        key = (claim.text, claim.kind)
        if key in seen:
            continue
        seen.add(key)
        v = verify_claim(
            claim.text,
            content_dir,
            kind=claim.kind,
            extra_sources=extra_sources,
        )
        verified.append(v)
        if not v.verified:
            failed.append(claim.text)
    return FactCheckResult(
        passed=not failed,
        verified_claims=verified,
        failed_claims=failed,
    )


__all__ = [
    "Claim",
    "FactCheckResult",
    "VerificationResult",
    "check_draft",
    "extract_hard_claims",
    "load_db_master_content",
    "load_jd_context_for_draft",
    "verify_claim",
]
