#!/usr/bin/env python3
"""Fact checker for drafted cover letters and narrative answers.

Part of the /prepare-application slice 8 workflow. When a cover letter
drafter or narrative drafter produces text containing hard claims
(dollar amounts, percentages, company names, year counts, proper
nouns), this module greps every claim against the master YAML files
in assets/content/. Any claim that doesn't appear in ANY master file
is flagged as unverified — the draft is rejected and the prepare
orchestrator must regenerate it without the fabricated content.

**This is a safety gate, not an optimization.** Silent fabrication is
the single worst failure mode for an automated application pipeline.
The fact checker is deliberately aggressive — it prefers false
positives (rejecting true claims) over false negatives (passing
fabricated ones). Operators can always regenerate or manually verify.

Usage:
    uv run python private/scripts/fact_check_draft.py \\
        --draft private/applications/stripe/cover-letter-draft.md

CLI exits 0 if all claims verified, 1 if any fail.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTENT_DIR = REPO_ROOT / "assets" / "content"

logger = logging.getLogger("fact_check_draft")


@dataclass
class Claim:
    text: str
    kind: str  # "money" | "percent" | "year_count" | "proper_noun"
    offset: int


@dataclass
class VerificationResult:
    claim: str
    kind: str
    verified: bool
    source_file: str | None = None


@dataclass
class FactCheckResult:
    passed: bool
    verified_claims: list[VerificationResult] = field(default_factory=list)
    failed_claims: list[str] = field(default_factory=list)


# ---------- extractors ----------


# $250M, $1B, $50.5K, $120,000, $120K, $132
_MONEY_RE = re.compile(
    r"\$\d+(?:[.,]\d+)*\s*[KMBk]?",
)

# 97.3%, 99.9%, 100%, 72%
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?%")

# "for 3.5 years", "over 7 years", "10+ years"
_YEAR_COUNT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

# Multi-word proper nouns: two+ capitalized words in a row
# (minimum 4 chars each, to avoid two-letter false positives).
# Also matches single word proper nouns of 6+ chars that are not
# at sentence start (approximate via preceding non-period char).
_MULTI_CAP_RE = re.compile(
    r"\b[A-Z][a-zA-Z]{3,}(?:\s+[A-Z][a-zA-Z]{3,})+\b",
)

# Explicit company-style single words (CamelCase or starts with capital
# and contains an internal capital — catches SunStrong, DagsterLabs, etc).
_CAMEL_NAME_RE = re.compile(r"\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b")

# Countable metrics like "7 pipelines", "200K assets", "7 automated ETL pipelines".
# Allow up to 3 words between the number and the unit (adjectives).
_COUNT_RE = re.compile(
    r"\b\d+[KMB]?\+?\s+(?:[\w\-]+\s+){0,3}"
    r"(?:pipelines?|assets?|systems?|states?|clients?|"
    r"customers?|employees?|engineers?|applications?|reports?|dashboards?|"
    r"databases?|warehouses?|projects?|teams?|companies|markets?)\b",
    re.IGNORECASE,
)

# Multi-word proper nouns with lowercase connectors allowed: "Technology
# and Policy", "State of Massachusetts", "Bureau of Labor Statistics".
# Requires 4+ char opening word so 3-char acronyms like MIT/SQL are left
# for _ACRONYM_RE to handle separately (otherwise "MIT in Technology"
# would greedy-match and obscure the real "Technology and Policy" phrase).
_CONNECTED_CAP_RE = re.compile(
    r"\b[A-Z][a-zA-Z]{3,}"
    r"(?:\s+(?:and|of|the|in|for|at|on|to|de|la|le|du|van|von)\s+)"
    r"[A-Z][a-zA-Z]{3,}"
    r"(?:\s+[A-Z][a-zA-Z]{3,})*"
    r"\b"
)

# Short acronyms (2-5 all-caps chars). The stoplist below filters out
# generic skills/cliches so only real claim-worthy acronyms (MIT, IBM,
# IRS, etc.) make it through.
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")

# Stoplist — words/acronyms that look like proper nouns but are generic
# skills, cliches, or grammar and shouldn't be fact-checked.
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
    # Generic skill acronyms — common knowledge, not worth fact-checking
    "SQL", "AI", "ML", "ETL", "ELT", "API", "CLI", "GUI", "SAAS",
    "PDF", "YAML", "JSON", "XML", "HTML", "CSS", "JS", "TS", "TSX",
    "RAG", "LLM", "LLMS", "NLP", "CV", "OCR", "SDK", "IDE",
    "AWS", "GCP", "S3", "EC2",
    "CEO", "CTO", "CFO", "VP", "COO", "CIO",
    "USA", "US", "UK", "EU",
    "HR", "HQ", "IT", "QA", "PR",
}


def extract_hard_claims(text: str) -> list[Claim]:
    """Pull every hard claim out of a draft.

    Order is deterministic — money → percent → years → counts →
    proper nouns — so tests and logs are stable. Duplicates are
    tolerated at extract time and dedup happens in check_draft.
    """
    out: list[Claim] = []
    for m in _MONEY_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="money", offset=m.start()))
    for m in _PERCENT_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="percent", offset=m.start()))
    for m in _YEAR_COUNT_RE.finditer(text):
        out.append(Claim(text=m.group(0).strip(), kind="year_count", offset=m.start()))
    for m in _COUNT_RE.finditer(text):
        # Extract just the leading number as the claim — e.g. "7 automated
        # ETL pipelines" → claim="7 pipelines" (noise stripped). We keep
        # the final unit word for uniqueness.
        raw = m.group(0).strip()
        parts = raw.split()
        if len(parts) >= 2:
            claim_text = f"{parts[0]} {parts[-1]}"
        else:
            claim_text = raw
        out.append(Claim(text=claim_text, kind="count", offset=m.start()))
    for m in _CONNECTED_CAP_RE.finditer(text):
        span = m.group(0).strip()
        out.append(Claim(text=span, kind="proper_noun", offset=m.start()))
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


def _auto_detect_kind(claim: str) -> str:
    """Infer the claim kind from its shape. Used when the caller doesn't pass one."""
    s = claim.strip()
    if s.startswith("$") or s.endswith(("K", "M", "B")):
        return "money"
    if s.endswith("%"):
        return "percent"
    if re.search(r"\b(?:years?|yrs?)\b", s, re.IGNORECASE):
        return "year_count"
    return "proper_noun"


def _claim_matches(claim: str, haystack: str, kind: str) -> bool:
    if kind in {"money", "percent", "year_count", "count"}:
        # Whole-token match — "$25" must not match "$250M"
        escaped = re.escape(claim)
        pattern = rf"(?:^|[\s'\"\[\(]){escaped}(?:[\s,.!?:;'\"\]\)]|$)"
        return bool(re.search(pattern, haystack))
    # Proper nouns: case-insensitive substring
    return claim.lower() in haystack.lower()


def verify_claim(
    claim: str,
    content_dir: Path,
    kind: str | None = None,
) -> VerificationResult:
    """Check a single claim against every master YAML file.

    Returns the first matching file (by sorted name), or a
    not-verified result if none match. When `kind` is None, the
    claim's shape is auto-detected — `$25`, `97%`, `3 years`, and
    `SunStrong` all route to the right matcher.
    """
    effective_kind = kind or _auto_detect_kind(claim)
    master = _load_master_content(content_dir)
    for name, body in master.items():
        if _claim_matches(claim, body, effective_kind):
            return VerificationResult(
                claim=claim, kind=effective_kind, verified=True, source_file=name
            )
    return VerificationResult(claim=claim, kind=effective_kind, verified=False)


def check_draft(
    draft_text: str,
    content_dir: Path,
) -> FactCheckResult:
    """Extract every hard claim from the draft and verify against the master files."""
    claims = extract_hard_claims(draft_text)
    verified: list[VerificationResult] = []
    failed: list[str] = []
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        key = (claim.text, claim.kind)
        if key in seen:
            continue
        seen.add(key)
        v = verify_claim(claim.text, content_dir, kind=claim.kind)
        verified.append(v)
        if not v.verified:
            failed.append(claim.text)
    return FactCheckResult(
        passed=not failed,
        verified_claims=verified,
        failed_claims=failed,
    )


# ---------- CLI ----------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True, help="draft markdown file")
    parser.add_argument(
        "--master-content-dir",
        type=Path,
        default=DEFAULT_CONTENT_DIR,
        help="directory containing master YAML files (default: assets/content/)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if not args.draft.exists():
        print(f"ERROR: draft file not found: {args.draft}", file=sys.stderr)
        return 1

    draft_text = args.draft.read_text()
    result = check_draft(draft_text, args.master_content_dir)

    if result.passed:
        print(f"✓ fact check passed — {len(result.verified_claims)} claims verified")
        if args.verbose:
            for v in result.verified_claims:
                print(f"  [{v.kind:12s}] {v.claim!r} → {v.source_file}")
        return 0

    print(
        f"✗ fact check FAILED — {len(result.failed_claims)} unverified claim(s):",
        file=sys.stderr,
    )
    for claim in result.failed_claims:
        print(f"  ✗ {claim!r}", file=sys.stderr)
    if args.verbose:
        print(
            f"\nVerified ({len([v for v in result.verified_claims if v.verified])}):",
            file=sys.stderr,
        )
        for v in result.verified_claims:
            if v.verified:
                print(f"  ✓ [{v.kind}] {v.claim!r} → {v.source_file}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
