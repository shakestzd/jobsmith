"""TDD tests for bug-58680458: factcheck gate false-rejects fully-anchored resumes.

Six root causes fixed:
  Bug 1 — mid-number extraction: _COUNT_RE matches inside larger numbers
  Bug 2 — count-claim reduction breaks matching (reduced string not contiguous)
  Bug 3 — money + suffix boundary: "$1B" fails to match "$1B+" in master
  Bug 4 — resume section headers extracted as proper-noun claims
  Bug 5 — backstop never threads JD context into the factcheck gate
  Bug 6 — _CONNECTED_CAP_RE stitches across connectors producing unverifiable
           compound phrases; proper-noun segment-fallback fixes this.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.factcheck import check_draft, extract_hard_claims

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

MASTER_BULLETS = """
positions:
  - company: "Sunnova Energy"
    title: "Senior Data Engineer"
    bullets:
      - "Sunnova Energy's 200K+ solar asset portfolio through automated pipelines"
      - "managing $1B+ in renewable energy investments"
      - "identifying 70K qualifying systems for program eligibility"
      - "500K+ solar asset portfolio through 7 automated ETL pipelines"
      - "788 MW of residential solar capacity ($4.25B Fair Market Value)"
      - "3,000 non-compliant systems out of the total fleet"
      - "Sunnova Energy's proprietary underwriting datasets"
"""

RESUME_WITH_SECTION_HEADERS = """
# Professional Summary

Data engineer with 8 years building energy analytics pipelines.

# Tailored Bullets

- Scaled Sunnova Energy's 200K+ solar asset portfolio through automated pipelines.
- Managed $1B+ in renewable energy investments for residential solar programs.
- Identified 70K qualifying systems for program eligibility under IRA rules.
- Processed 500K+ solar assets through 7 automated ETL pipelines.
- Maintained 788 MW of residential solar capacity ($4.25B Fair Market Value).
- Remediated 3,000 non-compliant systems detected in fleet audit.
"""


def _content_dir(tmp_path: Path) -> Path:
    content = tmp_path / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "work.yml").write_text(MASTER_BULLETS, encoding="utf-8")
    return content


# ---------------------------------------------------------------------------
# Bug 1: Mid-number extraction — _COUNT_RE must not fire inside larger numbers
# ---------------------------------------------------------------------------


class TestBug1MidNumberExtraction:
    """'25B' from '$4.25B' and '000' from '3,000' must NOT become standalone claims."""

    def test_mid_number_25b_not_extracted_as_standalone_count(self) -> None:
        text = "788 MW of residential solar capacity ($4.25B Fair Market Value)"
        claims = extract_hard_claims(text)
        count_texts = [c.text for c in claims if c.kind == "count"]
        # '25B' as a standalone count claim must never appear
        assert not any("25B" in t for t in count_texts), (
            f"'25B' should not be a standalone count claim; got count claims: {count_texts}"
        )

    def test_000_not_extracted_as_standalone_count(self) -> None:
        text = "Remediated 3,000 non-compliant systems detected in fleet audit."
        claims = extract_hard_claims(text)
        count_texts = [c.text for c in claims if c.kind == "count"]
        assert not any(t.strip() == "000" or t.strip().startswith("000 ") for t in count_texts), (
            f"'000' should not be a standalone count claim; got: {count_texts}"
        )

    def test_dollar_amount_captured_by_money_not_count(self) -> None:
        text = "($4.25B Fair Market Value)"
        claims = extract_hard_claims(text)
        money_texts = [c.text for c in claims if c.kind == "money"]
        # $4.25B should be captured by money regex
        assert any("4.25B" in t or "4.25" in t for t in money_texts), (
            f"$4.25B not found as money claim; money claims: {money_texts}"
        )


# ---------------------------------------------------------------------------
# Bug 2: Count-claim reduction — numeric token + noun must both verify
# ---------------------------------------------------------------------------


class TestBug2CountClaimReduction:
    """Reduced form '200K+ asset' from '200K+ solar asset' must verify correctly."""

    def test_200k_solar_asset_verifies(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Scaled Sunnova Energy's 200K+ solar asset portfolio through automated pipelines.",
            content,
        )
        assert "200K+ assets" not in result.failed_claims, (
            f"200K+ assets should verify; failed_claims={result.failed_claims}"
        )
        assert "200K+ asset" not in result.failed_claims, (
            f"200K+ asset should verify; failed_claims={result.failed_claims}"
        )

    def test_70k_qualifying_systems_verifies(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Identified 70K qualifying systems for program eligibility.",
            content,
        )
        assert "70K systems" not in result.failed_claims, (
            f"70K systems should verify; failed_claims={result.failed_claims}"
        )

    def test_500k_solar_asset_verifies(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Processed 500K+ solar assets through 7 automated ETL pipelines.",
            content,
        )
        assert "500K+ assets" not in result.failed_claims, (
            f"500K+ assets should verify; failed_claims={result.failed_claims}"
        )
        assert "500K+ asset" not in result.failed_claims, (
            f"500K+ asset should verify; failed_claims={result.failed_claims}"
        )

    def test_7_pipelines_verifies(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Processed data through 7 automated ETL pipelines.",
            content,
        )
        assert "7 pipelines" not in result.failed_claims, (
            f"7 pipelines should verify; failed_claims={result.failed_claims}"
        )


# ---------------------------------------------------------------------------
# Bug 3: Money + suffix boundary — "$1B" must match "$1B+" in master
# ---------------------------------------------------------------------------


class TestBug3MoneyPlusSuffix:
    """'$1B' in resume must verify against '$1B+' in master."""

    def test_one_billion_verifies_against_one_billion_plus_in_master(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Managed $1B in renewable energy investments.",
            content,
        )
        assert "$1B" not in result.failed_claims, (
            f"$1B should verify against $1B+ in master; failed_claims={result.failed_claims}"
        )

    def test_money_token_boundary_still_strict(self, tmp_path: Path) -> None:
        """'$25' must NOT match '$250M' — token boundary still enforced on the left."""
        content = tmp_path / "content"
        content.mkdir(parents=True, exist_ok=True)
        (content / "work.yml").write_text("Unlocked $250M in credits", encoding="utf-8")
        from jobsmith.factcheck import verify_claim
        result = verify_claim("$25", content, kind="money")
        assert result.verified is False, "'$25' must not match '$250M'"


# ---------------------------------------------------------------------------
# Bug 4: Section headers must not be extracted as proper-noun claims
# ---------------------------------------------------------------------------


class TestBug4SectionHeaders:
    """Jobsmith resume section headers must not appear as claims."""

    HEADERS_TO_SKIP = [
        "Professional Summary",
        "Tailored Bullets",
        "Work Experience",
        "Technical Skills",
        "Core Competencies",
        "Selected Experience",
        "Key Achievements",
    ]

    @pytest.mark.parametrize("header", HEADERS_TO_SKIP)
    def test_section_header_not_extracted(self, header: str) -> None:
        claims = extract_hard_claims(f"# {header}\n\nSome content here.")
        proper_noun_texts = [c.text for c in claims if c.kind == "proper_noun"]
        assert header not in proper_noun_texts, (
            f"Section header '{header}' should not be a proper-noun claim; got: {proper_noun_texts}"
        )

    def test_resume_with_headers_no_header_claims(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(RESUME_WITH_SECTION_HEADERS, content)
        assert "Professional Summary" not in result.failed_claims, (
            f"'Professional Summary' should not be a claim; failed={result.failed_claims}"
        )
        assert "Tailored Bullets" not in result.failed_claims, (
            f"'Tailored Bullets' should not be a claim; failed={result.failed_claims}"
        )


# ---------------------------------------------------------------------------
# Bug 5: Backstop must thread extra_sources through to the factcheck gate
# ---------------------------------------------------------------------------


class TestBug5BackstopJdContext:
    """JD-domain terms (IRA, Energy Community) verify when JD is an extra_source."""

    def test_ira_verifies_with_jd_extra_source(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        jd_text = (
            '{"title":"Senior Data Engineer",'
            '"description":"Work under IRA (Inflation Reduction Act) and Energy Community rules."}'
        )
        result = check_draft(
            "Applied IRA Energy Community eligibility rules to 70K qualifying systems.",
            content,
            extra_sources={"jd:jd-parsed.json": jd_text},
        )
        assert "IRA" not in result.failed_claims, (
            f"IRA should verify against JD extra_source; failed={result.failed_claims}"
        )

    def test_energy_community_verifies_with_jd_extra_source(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        jd_text = (
            '{"title":"Policy Analyst",'
            '"description":"Investment Tax Credit and Energy Community adder analysis."}'
        )
        result = check_draft(
            "Computed Investment Tax Credit and Energy Community adders for solar sites.",
            content,
            extra_sources={"jd:jd-parsed.json": jd_text},
        )
        # "Energy Community" or its fragments should not be in failed_claims
        failed = result.failed_claims
        assert not any("Energy" in c and "Community" in c for c in failed), (
            f"'Energy Community' should verify via JD; failed={failed}"
        )

    def test_backstop_threads_extra_sources(self, tmp_path: Path) -> None:
        """_gate_draft_text must forward extra_sources to _run_factcheck_gate."""
        from jobsmith.reuse.backstop import _gate_draft_text

        master = tmp_path / "work.yml"
        master.write_text("Built tooling\n", encoding="utf-8")
        content = tmp_path / "content"
        content.mkdir()
        (content / "work.yml").write_text("Built tooling\n", encoding="utf-8")
        selection = tmp_path / "sel.json"
        selection.write_text("{}", encoding="utf-8")

        captured: dict = {}

        def _fake_factcheck(draft_text: str, content_dir: Path, extra_sources=None):
            captured["extra_sources"] = extra_sources
            return True, []

        with (
            patch("jobsmith.reuse.backstop._run_anchor_gate", return_value=0),
            patch("jobsmith.reuse.backstop._run_factcheck_gate", side_effect=_fake_factcheck),
        ):
            _gate_draft_text(
                "draft text",
                content,
                master,
                selection,
                extra_sources={"jd:jd-parsed.json": "IRA rules"},
            )

        assert captured.get("extra_sources") == {"jd:jd-parsed.json": "IRA rules"}, (
            f"extra_sources not threaded through; captured={captured}"
        )


# ---------------------------------------------------------------------------
# Gate integrity: fabricated claims must STILL fail
# ---------------------------------------------------------------------------


class TestGateIntegrityFabricatedClaims:
    """The gate must still reject genuinely fabricated claims."""

    def test_fabricated_money_claim_fails(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Managed $9B in fabricated investment portfolio claims.",
            content,
        )
        assert result.passed is False, "Fabricated $9B must fail"
        assert any("9B" in c or "$9B" in c for c in result.failed_claims), (
            f"$9B should be in failed_claims; got: {result.failed_claims}"
        )

    def test_fabricated_count_claim_fails(self, tmp_path: Path) -> None:
        content = _content_dir(tmp_path)
        result = check_draft(
            "Managed 999K assets in the fabricated portfolio.",
            content,
        )
        assert result.passed is False, "Fabricated 999K assets must fail"

    def test_full_resume_with_all_bugs_fixed_passes(self, tmp_path: Path) -> None:
        """The full resume containing all the real false-positive claims must now pass."""
        content = _content_dir(tmp_path)
        jd_extra = {
            "jd:jd-parsed.json": (
                '{"title":"Senior Data Engineer",'
                '"description":"Work with IRA Inflation Reduction Act Energy Community '
                'Investment Tax Credit and Energy Community rules for solar portfolios."}'
            )
        }
        result = check_draft(RESUME_WITH_SECTION_HEADERS, content, extra_sources=jd_extra)
        # The specific claims from the bug report must not appear in failed_claims
        reported_fps = [
            "$1B", "200K+ asset", "200K+ assets", "70K systems",
            "500K+ asset", "7 pipelines", "25B Market", "000 systems",
            "Professional Summary", "Tailored Bullets",
        ]
        for fp in reported_fps:
            assert fp not in result.failed_claims, (
                f"'{fp}' is still a false positive; all failed_claims={result.failed_claims}"
            )


# ---------------------------------------------------------------------------
# Bug 6: Segment-fallback for stitched proper-noun compounds
# ---------------------------------------------------------------------------


class TestBug6SegmentFallback:
    """Proper-noun claims stitched across connectors verify via segment-fallback."""

    def test_stitched_compound_verifies_when_all_segments_anchored(self, tmp_path: Path) -> None:
        """'Credit and Energy Community' from '_CONNECTED_CAP_RE' must verify when
        both 'Credit' (via 'Investment Tax Credit') and 'Energy Community' are in sources."""
        content = tmp_path / "content"
        content.mkdir(parents=True, exist_ok=True)
        (content / "work.yml").write_text(
            "Analyzed Investment Tax Credit provisions for residential solar.\n"
            "Applied Energy Community adder rules to 70K qualifying systems.\n",
            encoding="utf-8",
        )
        result = check_draft(
            "Applied Investment Tax Credit and Energy Community provisions to solar fleets.",
            content,
        )
        assert "Credit and Energy Community" not in result.failed_claims, (
            f"'Credit and Energy Community' should verify via segment-fallback; "
            f"failed_claims={result.failed_claims}"
        )

    def test_stitched_compound_with_jd_extra_source(self, tmp_path: Path) -> None:
        """Segment-fallback works across master + extra_sources (JD may hold one segment)."""
        content = tmp_path / "content"
        content.mkdir(parents=True, exist_ok=True)
        # master has "Credit" context; JD has "Energy Community"
        (content / "work.yml").write_text(
            "Modeled Investment Tax Credit eligibility for solar programs.\n",
            encoding="utf-8",
        )
        jd_extra = {
            "jd:jd-parsed.json": (
                '{"description":"Energy Community adder under IRA rules."}'
            )
        }
        result = check_draft(
            "Leveraged Investment Tax Credit and Energy Community provisions.",
            content,
            extra_sources=jd_extra,
        )
        assert "Credit and Energy Community" not in result.failed_claims, (
            f"'Credit and Energy Community' should verify across master+JD; "
            f"failed_claims={result.failed_claims}"
        )

    def test_partially_fabricated_compound_still_fails(self, tmp_path: Path) -> None:
        """If one segment is absent, the stitched compound must still fail."""
        content = tmp_path / "content"
        content.mkdir(parents=True, exist_ok=True)
        # master has "Credit" but NOT "Foobar Industries"
        (content / "work.yml").write_text(
            "Analyzed Investment Tax Credit provisions.\n",
            encoding="utf-8",
        )
        result = check_draft(
            "Applied Investment Tax Credit and Foobar Industries provisions.",
            content,
        )
        # The stitched "Credit and Foobar Industries" must fail — "Foobar" not in master
        assert result.passed is False, (
            "Partially-fabricated compound must still fail the gate"
        )
        failed_texts = " ".join(result.failed_claims)
        assert "Foobar" in failed_texts or "Credit and Foobar" in failed_texts, (
            f"Expected 'Foobar Industries' segment to surface as failure; "
            f"failed_claims={result.failed_claims}"
        )

    def test_whole_phrase_match_still_wins_without_splitting(self, tmp_path: Path) -> None:
        """When the full compound IS in the master, it still verifies (no split needed)."""
        content = tmp_path / "content"
        content.mkdir(parents=True, exist_ok=True)
        (content / "work.yml").write_text(
            "Technology and Policy program at MIT.\n",
            encoding="utf-8",
        )
        result = check_draft(
            "Completed the Technology and Policy program at MIT.",
            content,
        )
        assert "Technology and Policy" not in result.failed_claims, (
            f"Whole-phrase match should still work; failed_claims={result.failed_claims}"
        )
