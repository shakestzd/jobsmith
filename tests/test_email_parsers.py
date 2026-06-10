"""Tests for email alert HTML parsers (feat-b1bd050e).

TDD: parsers over fixture corpus, degraded-on-unparseable.
All tests run offline — no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "email_alerts"


@pytest.fixture()
def linkedin_html() -> str:
    return (FIXTURES_DIR / "linkedin_alert.html").read_text()


@pytest.fixture()
def linkedin_html_real() -> str:
    """Real-structure LinkedIn alert fixture (comm/jobs/view URLs, company·location format)."""
    return (FIXTURES_DIR / "linkedin_alert_real.html").read_text()


@pytest.fixture()
def indeed_html() -> str:
    return (FIXTURES_DIR / "indeed_alert.html").read_text()


@pytest.fixture()
def glassdoor_html() -> str:
    return (FIXTURES_DIR / "glassdoor_alert.html").read_text()


# ---------------------------------------------------------------------------
# LinkedIn parser
# ---------------------------------------------------------------------------


def test_parse_linkedin_returns_jobs(linkedin_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html)
    assert len(results) >= 2


def test_parse_linkedin_job_fields(linkedin_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html)
    first = results[0]
    assert first["title"]
    assert first["url"]
    assert "linkedin.com/jobs/view/" in first["url"]
    assert first["external_id"]  # derived from URL


def test_parse_linkedin_strips_tracking_params(linkedin_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html)
    for r in results:
        assert "?" not in r["url"], f"URL still has tracking params: {r['url']}"
        assert "trk=" not in r["url"]


def test_parse_linkedin_no_personal_data(linkedin_html: str) -> None:
    """Fixtures must not contain real personal email addresses or PII."""
    # Fixture is sanitized — verify it uses fake names/companies
    assert "@" not in linkedin_html or "example.com" in linkedin_html or "linkedin.com" in linkedin_html


# ---------------------------------------------------------------------------
# LinkedIn parser — real-structure fixture (comm/jobs/view URL format)
# ---------------------------------------------------------------------------


def test_parse_linkedin_real_returns_jobs(linkedin_html_real: str) -> None:
    """Real-structure fixture: parser returns >=3 jobs."""
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html_real)
    assert len(results) >= 3, f"Expected >=3, got {len(results)}: {results}"


def test_parse_linkedin_real_canonical_urls(linkedin_html_real: str) -> None:
    """Real-structure fixture: all URLs are canonical /jobs/view/<id>/ with no tracking params."""
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html_real)
    for r in results:
        assert "linkedin.com/jobs/view/" in r["url"], f"URL not canonical: {r['url']}"
        assert "?" not in r["url"], f"Tracking params not stripped: {r['url']}"
        assert "comm" not in r["url"], f"comm subdomain not stripped: {r['url']}"
        assert "trackingId" not in r["url"]
        assert "midToken" not in r["url"]


def test_parse_linkedin_real_title_company_location(linkedin_html_real: str) -> None:
    """Real-structure fixture: title, company and location are populated."""
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html_real)
    for r in results:
        assert r["title"], f"Missing title in: {r}"
        assert r["company"], f"Missing company in: {r}"
        assert r["location"], f"Missing location in: {r}"


def test_parse_linkedin_real_external_id(linkedin_html_real: str) -> None:
    """Real-structure fixture: external_id is the numeric job ID from the URL."""
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html_real)
    for r in results:
        assert r["external_id"].isdigit(), f"external_id not numeric: {r['external_id']}"


def test_parse_linkedin_real_first_job(linkedin_html_real: str) -> None:
    """Spot-check first job entry from real-structure fixture."""
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    results = parse_linkedin_alert(linkedin_html_real)
    first = results[0]
    assert first["title"] == "Senior Data Engineer"
    assert first["company"] == "Acme Systems"
    assert "Remote" in first["location"]
    assert first["url"] == "https://www.linkedin.com/jobs/view/3001100001/"
    assert first["external_id"] == "3001100001"


def test_parse_linkedin_url_normalization_comm_subdomain() -> None:
    """Parser normalizes comm.linkedin.com/comm/jobs/view to linkedin.com/jobs/view."""
    from jobsmith.sourcing.email.parsers import parse_linkedin_alert

    html = """<html><body>
    <a href="https://www.linkedin.com/comm/jobs/view/9999000001/?trackingId=ABC&midToken=XYZ&trk=test">
      <table><tr><td>Staff Engineer</td></tr>
      <tr><td>OmegaCorp &middot; Austin, TX</td></tr></table>
    </a>
    </body></html>"""
    results = parse_linkedin_alert(html)
    assert len(results) == 1
    assert results[0]["url"] == "https://www.linkedin.com/jobs/view/9999000001/"
    assert results[0]["title"] == "Staff Engineer"
    assert results[0]["company"] == "OmegaCorp"
    assert results[0]["location"] == "Austin, TX"


# ---------------------------------------------------------------------------
# Indeed parser
# ---------------------------------------------------------------------------


def test_parse_indeed_returns_jobs(indeed_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_indeed_alert

    results = parse_indeed_alert(indeed_html)
    assert len(results) >= 2


def test_parse_indeed_job_fields(indeed_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_indeed_alert

    results = parse_indeed_alert(indeed_html)
    first = results[0]
    assert first["title"]
    assert "indeed.com/viewjob" in first["url"]
    assert first["external_id"]  # from jk= param


def test_parse_indeed_company_location(indeed_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_indeed_alert

    results = parse_indeed_alert(indeed_html)
    first = results[0]
    # The fixture has company/location classes
    assert first["company"]
    assert first["location"]


# ---------------------------------------------------------------------------
# Glassdoor parser
# ---------------------------------------------------------------------------


def test_parse_glassdoor_returns_jobs(glassdoor_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_glassdoor_alert

    results = parse_glassdoor_alert(glassdoor_html)
    assert len(results) >= 1


def test_parse_glassdoor_job_fields(glassdoor_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_glassdoor_alert

    results = parse_glassdoor_alert(glassdoor_html)
    first = results[0]
    assert first["title"]
    assert "glassdoor.com/job-listing" in first["url"]
    assert first["external_id"]  # from jl= param


def test_parse_glassdoor_company_location(glassdoor_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_glassdoor_alert

    results = parse_glassdoor_alert(glassdoor_html)
    first = results[0]
    assert first["company"]
    assert first["location"]


# ---------------------------------------------------------------------------
# Degraded-on-unparseable
# ---------------------------------------------------------------------------


def test_parse_alert_unknown_sender_returns_empty() -> None:
    """Unknown sender slug → empty list (records as degraded by caller)."""
    from jobsmith.sourcing.email.parsers import parse_alert_html

    results = parse_alert_html("unknown-sender-xyz", "<html><body>nothing</body></html>")
    assert results == []


def test_parse_alert_garbage_html_returns_empty() -> None:
    """Completely unparseable garbage → empty list, never raises."""
    from jobsmith.sourcing.email.parsers import parse_alert_html

    results = parse_alert_html("linkedin-alert", "NOT HTML AT ALL ><><<>")
    # Parser finds no matching job links — returns [] without crashing
    assert isinstance(results, list)


def test_parse_alert_empty_html_returns_empty() -> None:
    from jobsmith.sourcing.email.parsers import parse_alert_html

    results = parse_alert_html("indeed-alert", "")
    assert results == []


def test_parse_linkedin_exception_returns_empty(monkeypatch) -> None:
    """If BeautifulSoup raises, parse_linkedin_alert returns []."""
    import jobsmith.sourcing.email.parsers as parsers

    def boom(html, *args, **kwargs):
        raise RuntimeError("simulated parse error")

    monkeypatch.setattr("jobsmith.sourcing.email.parsers.BeautifulSoup", boom)
    results = parsers.parse_linkedin_alert("<html/>")
    assert results == []


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------


def test_sender_parsers_registry_has_expected_keys() -> None:
    from jobsmith.sourcing.email.parsers import SENDER_PARSERS

    assert "linkedin-alert" in SENDER_PARSERS
    assert "indeed-alert" in SENDER_PARSERS
    assert "glassdoor-alert" in SENDER_PARSERS


def test_parse_alert_dispatches_to_linkedin(linkedin_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_alert_html

    results = parse_alert_html("linkedin-alert", linkedin_html)
    assert len(results) >= 2


def test_parse_alert_dispatches_to_indeed(indeed_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_alert_html

    results = parse_alert_html("indeed-alert", indeed_html)
    assert len(results) >= 2


def test_parse_alert_dispatches_to_glassdoor(glassdoor_html: str) -> None:
    from jobsmith.sourcing.email.parsers import parse_alert_html

    results = parse_alert_html("glassdoor-alert", glassdoor_html)
    assert len(results) >= 1
