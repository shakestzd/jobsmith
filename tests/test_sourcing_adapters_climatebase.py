"""Tests for the Climatebase adapter (feat-5531c54b).

TDD: authored HTML fixture (no live recording). Design decision A4: climatebase
fixtures do not exist in the original repo — authored new here.
"""

from __future__ import annotations

from pathlib import Path

from jobsmith.sourcing.adapters.climatebase import ClimatebaseAdapter, parse_climatebase_html

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "ats" / "climatebase_response.html"
)


def test_parse_html_returns_expected_role_count() -> None:
    html = FIXTURE.read_text()
    roles = list(parse_climatebase_html(html, query="data engineer"))
    assert len(roles) == 2


def test_parse_html_extracts_titles() -> None:
    html = FIXTURE.read_text()
    roles = list(parse_climatebase_html(html, query="data engineer"))
    titles = {r.title for r in roles}
    assert "Senior Data Engineer" in titles
    assert "Data Platform Engineer" in titles


def test_parse_html_extracts_companies() -> None:
    html = FIXTURE.read_text()
    roles = list(parse_climatebase_html(html, query="data engineer"))
    companies = {r.company for r in roles}
    assert "Climate Co" in companies
    assert "Solar Analytics" in companies


def test_parse_html_url_uses_absolute_url() -> None:
    html = FIXTURE.read_text()
    roles = list(parse_climatebase_html(html, query="data engineer"))
    for r in roles:
        assert r.url.startswith("https://climatebase.org/")


def test_parse_html_source_fields() -> None:
    html = FIXTURE.read_text()
    roles = list(parse_climatebase_html(html, query="data engineer"))
    for r in roles:
        assert r.source == "climatebase"
        assert r.source_slug == "data engineer"


def test_parse_html_empty_returns_empty() -> None:
    roles = list(parse_climatebase_html("", query="data engineer"))
    assert roles == []


def test_parse_html_no_cards_returns_empty() -> None:
    html = "<html><body><div>No jobs here</div></body></html>"
    roles = list(parse_climatebase_html(html, query="data engineer"))
    assert roles == []


def test_adapter_name() -> None:
    a = ClimatebaseAdapter()
    assert a.name == "climatebase"
