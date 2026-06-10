"""Tests for the Lever adapter (feat-5531c54b).

TDD: tests written before implementation. Uses the representative fixture
ported from shakestzd/tests/fixtures/ats/lever_response.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobsmith.sourcing.adapters.lever import LeverAdapter, parse_lever_payload

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "ats" / "lever_response.json"
)


def test_parse_payload_returns_expected_role_count() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    assert len(roles) == 2


def test_parse_payload_uses_descriptionplain_when_present() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    senior = next(r for r in roles if r.title == "Senior Data Engineer")
    assert "<p>" not in senior.jd_text
    assert "Netflix" in senior.jd_text


def test_parse_payload_extracts_location_from_categories() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    senior = next(r for r in roles if r.title == "Senior Data Engineer")
    assert senior.location == "Remote"


def test_parse_payload_url_from_hosted_url() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    assert all(r.url.startswith("https://jobs.lever.co/netflix/") for r in roles)


def test_parse_payload_company_from_slug() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    assert all(r.company.lower() == "netflix" for r in roles)


def test_parse_payload_company_name_override() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(
        parse_lever_payload(payload, source_slug="netflix", company_name="Netflix Inc")
    )
    assert all(r.company == "Netflix Inc" for r in roles)


def test_parse_payload_converts_epoch_ms_to_date() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    senior = next(r for r in roles if r.title == "Senior Data Engineer")
    # createdAt: 1744228800000 → 2025-04-09
    assert senior.posted_date.startswith("2025-04")


def test_parse_payload_non_list_returns_empty() -> None:
    roles = list(parse_lever_payload({"jobs": []}, source_slug="x"))
    assert roles == []


def test_adapter_name() -> None:
    a = LeverAdapter()
    assert a.name == "lever"


def test_adapter_stores_company_name() -> None:
    a = LeverAdapter(company_name="Netflix")
    assert a.company_name == "Netflix"


def test_role_id_format() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    for r in roles:
        assert r.id.startswith("lever:netflix:")


def test_role_source_field() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_lever_payload(payload, source_slug="netflix"))
    assert all(r.source == "lever" for r in roles)
