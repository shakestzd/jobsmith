"""Tests for the Ashby adapter (feat-5531c54b).

TDD: authored fixture (no live recording). Design decision A4: ashby/hn/climatebase
fixtures do not exist in the original repo — authored new here.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobsmith.sourcing.adapters.ashby import AshbyAdapter, parse_ashby_payload

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "ats" / "ashby_linear_response.json"
)


def test_parse_payload_returns_expected_role_count() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    assert len(roles) == 2


def test_parse_payload_extracts_title() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    titles = {r.title for r in roles}
    assert "Senior Software Engineer, Platform" in titles


def test_parse_payload_strips_html_in_jd_text() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    senior = next(r for r in roles if "Platform" in r.title)
    assert "<p>" not in senior.jd_text
    assert "Linear" in senior.jd_text


def test_parse_payload_uses_job_url() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    assert all(r.url.startswith("https://jobs.ashbyhq.com/linear/") for r in roles)


def test_parse_payload_extracts_location() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    assert all(r.location == "Remote" for r in roles)


def test_parse_payload_company_from_slug() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    assert all(r.company == "Linear" for r in roles)


def test_parse_payload_company_name_override() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(
        parse_ashby_payload(payload, source_slug="linear", company_name="Linear B.V.")
    )
    assert all(r.company == "Linear B.V." for r in roles)


def test_parse_payload_posted_date() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    senior = next(r for r in roles if "Platform" in r.title)
    assert senior.posted_date == "2026-05-01"


def test_parse_empty_jobs_returns_empty() -> None:
    roles = list(parse_ashby_payload({"jobs": []}, source_slug="linear"))
    assert roles == []


def test_adapter_name() -> None:
    a = AshbyAdapter()
    assert a.name == "ashby"


def test_role_id_format() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    for r in roles:
        assert r.id.startswith("ashby:linear:")


def test_role_source_field() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    assert all(r.source == "ashby" for r in roles)
