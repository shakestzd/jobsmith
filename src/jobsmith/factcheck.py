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

# Connector tokens used for segment-splitting proper-noun fallback.
_CONNECTOR_SPLIT_RE = re.compile(r"\s+(?:and|of|the|in|for|at|on|to|&)\s+|,\s*", re.IGNORECASE)

# Salutation lines: "Dear BECU Hiring Team,", "Hello Hiring Manager:", etc.
# Matched as the first non-empty line or any line that opens with a greeting word.
_SALUTATION_RE = re.compile(
    r"^[ \t]*(Dear|Hello|Hi|Greetings|To Whom|To the)\b[^\n]*[,:][ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Closing lines: "Sincerely,", "Best regards,", "Warm regards,", "Thank you,",
# "Respectfully,", or a signature stub (single Capitalized word or two followed by comma).
_CLOSING_RE = re.compile(
    r"^[ \t]*(?:Sincerely|Regards|Best regards|Warm regards|Kind regards|"
    r"Respectfully|Thank you|Thanks|Yours truly|Yours sincerely|Best)[,.]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
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


def _strip_salutation_lines(text: str) -> str:
    """Return *text* with salutation and closing lines blanked out.

    Salutations ("Dear BECU Hiring Team,", "Hello Hiring Manager:") and
    closings ("Sincerely,", "Best regards,") are not factual claims — they
    are letter conventions. Blanking them prevents proper-noun extractors
    from treating greeting phrases as verifiable claims.

    Lines are replaced with a blank line so character offsets for the
    remaining body remain approximately correct and no claim text bleeds
    across the removed line boundary.
    """
    result = _SALUTATION_RE.sub("", text)
    result = _CLOSING_RE.sub("", result)
    return result


def extract_hard_claims(text: str) -> list[Claim]:
    """Pull every hard claim out of a draft.

    Order is deterministic — money → percent → years → counts → proper nouns —
    so tests and logs are stable. Duplicates are tolerated and dedup happens
    in `check_draft`.

    Salutation and closing lines are stripped before extraction so greeting
    phrases ("Dear BECU Hiring Team,") are never treated as factual claims.
    """
    text = _strip_salutation_lines(text)
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


_ACRONYM_CONNECTORS = r"(?:of|the|and|for|in|to|&|de|la|le)"


def _acronym_expansion_matches(acronym: str, haystack: str) -> bool:
    """Return True if haystack contains the spelled-out expansion of acronym.

    Matches a run of Capitalized words whose leading letters, in order, spell
    the acronym.  Up to 2 lowercase connector words (of, the, and, for, …) may
    appear between any two consecutive letter-words; each letter-word must start
    with the corresponding uppercase letter so "MIT" matches "Massachusetts
    Institute of Technology" but NOT "my interesting thing".

    Only pure-uppercase 2-5 char acronyms are expected as input (callers guard).
    """
    parts: list[str] = []
    for i, letter in enumerate(acronym):
        cap_word = rf"{re.escape(letter)}[a-zA-Z]+"
        if i == 0:
            parts.append(rf"\b{cap_word}")
        else:
            connector_run = rf"(?:\s+{_ACRONYM_CONNECTORS}\b){{0,2}}\s+"
            parts.append(rf"{connector_run}{cap_word}")
    parts[-1] += r"\b"
    pattern = re.compile("".join(parts))
    return bool(pattern.search(haystack))


def _proper_noun_segments_all_verified(claim: str, all_sources: dict[str, str]) -> bool:
    """Segment-fallback for proper-noun claims stitched by _CONNECTED_CAP_RE.

    Splits on connector words (" and ", " of ", etc.) and verifies that EVERY
    non-trivial segment (≥2 chars after stripping) appears as a case-insensitive
    substring in some source body. Scoped to proper_noun kind only — numeric
    claims never use this path.

    Returns True only if there is ≥1 segment AND all segments verify. An
    unverified segment means the whole claim remains failed (gate preserved).
    """
    segments = [s.strip() for s in _CONNECTOR_SPLIT_RE.split(claim)]
    meaningful = [s for s in segments if len(s) >= 2]
    if not meaningful:
        return False
    all_bodies = list(all_sources.values())
    for seg in meaningful:
        seg_lower = seg.lower()
        if not any(seg_lower in body.lower() for body in all_bodies):
            return False
    return True


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

    For proper-noun claims that fail the whole-phrase match, a segment-fallback
    is attempted: the claim is split on connector words and each segment must
    independently appear in some source. This resolves false positives from
    _CONNECTED_CAP_RE stitching across connectors (e.g. "Credit and Energy
    Community" from "...Investment Tax Credit and Energy Community...").
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
    # Segment-fallback for proper nouns only — money/percent/count/year stay exact.
    if effective_kind == "proper_noun" and _proper_noun_segments_all_verified(claim, master):
        return VerificationResult(
            claim=claim, kind=effective_kind, verified=True, source_file="segment-fallback"
        )
    # Initialism-match fallback: only for pure-uppercase 2-5 char acronyms.
    if effective_kind == "proper_noun" and re.fullmatch(r"[A-Z]{2,5}", claim):
        for name, body in master.items():
            if _acronym_expansion_matches(claim, body):
                return VerificationResult(
                    claim=claim, kind=effective_kind, verified=True,
                    source_file=f"initialism:{name}",
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
