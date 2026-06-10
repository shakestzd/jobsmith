"""Tests for the Greenhouse adapter (feat-5531c54b).

TDD: tests written before implementation. Uses the representative fixture
ported from shakestzd/tests/fixtures/ats/greenhouse_stripe_response.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobsmith.sourcing.adapters.greenhouse import (
    GreenhouseAdapter,
    parse_greenhouse_payload,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "ats"
    / "greenhouse_stripe_response.json"
)


def test_parse_payload_returns_expected_role_count() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    assert len(roles) == 3


def test_parse_payload_title_present() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    titles = {r.title for r in roles}
    assert "Senior Data Engineer, Payments Platform" in titles


def test_parse_payload_strips_html_to_jd_text() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    senior = next(r for r in roles if "Payments" in r.title)
    assert "<p>" not in senior.jd_text
    assert "<ul>" not in senior.jd_text
    assert "data pipelines" in senior.jd_text.lower() or "Senior Data Engineer" in senior.jd_text


def test_parse_payload_extracts_url_and_location() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    senior = next(r for r in roles if "Payments" in r.title)
    assert senior.url.startswith("https://boards.greenhouse.io/stripe/jobs/")
    assert "Remote" in senior.location


def test_parse_payload_sets_company_from_slug() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    assert all(r.company.lower() == "stripe" for r in roles)


def test_parse_payload_uses_canonical_company_name_when_provided() -> None:
    """company_name kwarg overrides slug.title() fallback."""
    payload = json.loads(FIXTURE.read_text())
    roles = list(
        parse_greenhouse_payload(
            payload,
            source_slug="oscarhealth",
            company_name="Oscar Health",
        )
    )
    assert all(r.company == "Oscar Health" for r in roles)
    assert all(r.source_slug == "oscarhealth" for r in roles)


def test_parse_payload_falls_back_to_slug_title_when_no_canonical() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(
        parse_greenhouse_payload(payload, source_slug="stripe", company_name=None)
    )
    assert all(r.company == "Stripe" for r in roles)


def test_parse_payload_handles_missing_content_field() -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Engineer",
                "absolute_url": "https://x/y",
                "location": {"name": "Remote"},
            }
        ]
    }
    roles = list(parse_greenhouse_payload(payload, source_slug="x"))
    assert len(roles) == 1
    assert roles[0].jd_text == ""


def test_adapter_name() -> None:
    a = GreenhouseAdapter()
    assert a.name == "greenhouse"


def test_adapter_stores_company_name() -> None:
    a = GreenhouseAdapter(company_name="Oscar Health")
    assert a.company_name == "Oscar Health"


def test_adapter_default_company_name_is_none() -> None:
    a = GreenhouseAdapter()
    assert a.company_name is None


def test_role_id_format() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    for r in roles:
        assert r.id.startswith("greenhouse:stripe:")


def test_role_source_field() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    assert all(r.source == "greenhouse" for r in roles)


def test_role_posted_date_extracted() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_greenhouse_payload(payload, source_slug="stripe"))
    senior = next(r for r in roles if "Payments" in r.title)
    assert senior.posted_date == "2026-04-10"
