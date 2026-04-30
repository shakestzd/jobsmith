"""Tests for jobsmith.factcheck — claim extraction + verification."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsmith.factcheck import (
    Claim,
    check_draft,
    extract_hard_claims,
    verify_claim,
)


# ---------- extract_hard_claims ----------


def test_extract_money_claim() -> None:
    claims = extract_hard_claims("Unlocked $250M in tax credits")
    assert any(c.kind == "money" and c.text == "$250M" for c in claims)


def test_extract_percent_claim() -> None:
    claims = extract_hard_claims("Reduced AP by 75%")
    assert any(c.kind == "percent" and c.text == "75%" for c in claims)


def test_extract_year_count_claim() -> None:
    claims = extract_hard_claims("For 3 years at Helios")
    assert any(c.kind == "year_count" for c in claims)


def test_extract_count_claim() -> None:
    claims = extract_hard_claims("Built 7 automated ETL pipelines")
    count_claims = [c for c in claims if c.kind == "count"]
    assert any("pipelines" in c.text for c in count_claims)


def test_extract_proper_noun_acronym() -> None:
    claims = extract_hard_claims("MIT Technology and Policy program")
    proper_nouns = [c.text for c in claims if c.kind == "proper_noun"]
    assert "MIT" in proper_nouns


def test_extract_camel_case_company_name() -> None:
    claims = extract_hard_claims("Worked at SunStrong on tax equity")
    proper_nouns = [c.text for c in claims if c.kind == "proper_noun"]
    assert "SunStrong" in proper_nouns


def test_extract_skips_grammar_capitals() -> None:
    """'The', 'Their', 'They' should not be flagged as proper nouns."""
    claims = extract_hard_claims("The team built systems. They scaled well.")
    proper_nouns = [c.text for c in claims if c.kind == "proper_noun"]
    assert "The" not in proper_nouns
    assert "They" not in proper_nouns


def test_extract_skips_generic_acronyms() -> None:
    """Generic skill acronyms (SQL, AI, ML) should not count as proper nouns."""
    claims = extract_hard_claims("Built ML and AI systems with SQL and Python")
    proper_nouns = [c.text for c in claims if c.kind == "proper_noun"]
    assert "SQL" not in proper_nouns
    assert "AI" not in proper_nouns
    assert "ML" not in proper_nouns


# ---------- verify_claim ----------


def test_verify_claim_finds_in_master(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    (content / "work.yml").write_text("- title: Engineer\n  details:\n    - Unlocked $250M in credits\n")
    result = verify_claim("$250M", content, kind="money")
    assert result.verified is True
    assert result.source_file == "work.yml"


def test_verify_claim_money_token_boundary(tmp_path: Path) -> None:
    """'$25' must NOT match '$250M' (whole-token match)."""
    content = tmp_path / "content"
    content.mkdir()
    (content / "work.yml").write_text("Unlocked $250M in credits")
    result = verify_claim("$25", content, kind="money")
    assert result.verified is False


def test_verify_claim_proper_noun_case_insensitive(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    (content / "education.yml").write_text("title: MIT Technology and Policy")
    result = verify_claim("mit technology", content, kind="proper_noun")
    assert result.verified is True


def test_verify_claim_returns_unverified_when_absent(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    (content / "work.yml").write_text("nothing relevant")
    result = verify_claim("$250M", content, kind="money")
    assert result.verified is False
    assert result.source_file is None


# ---------- check_draft ----------


def test_check_draft_passes_when_all_claims_verified(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    (content / "work.yml").write_text(
        "- title: Engineer\n  details:\n"
        "    - Unlocked $250M in tax credits at SunStrong\n"
        "    - Reduced AP processing by 75%\n"
    )
    draft = "I unlocked $250M in tax credits at SunStrong and reduced AP by 75%."
    result = check_draft(draft, content)
    assert result.passed is True
    assert result.failed_claims == []


def test_check_draft_fails_when_claim_fabricated(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    (content / "work.yml").write_text("- title: Engineer\n  details:\n    - Unlocked $250M in credits\n")
    draft = "I unlocked $250M in credits and led a team of 50 engineers."
    result = check_draft(draft, content)
    assert result.passed is False
    # The fabricated claim is the count "50 engineers" or proper-noun mismatch.
    assert any("50" in claim or "engineers" in claim for claim in result.failed_claims)


def test_check_draft_dedupes_identical_claims(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    (content / "work.yml").write_text("Unlocked $250M in credits")
    draft = "$250M, $250M, $250M"
    result = check_draft(draft, content)
    money_claims = [v for v in result.verified_claims if v.kind == "money"]
    assert len(money_claims) == 1
