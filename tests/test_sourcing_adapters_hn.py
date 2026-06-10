"""Tests for the HN Who's Hiring adapter (feat-5531c54b).

TDD: authored fixture (no live recording). Design decision A4: hn fixtures
do not exist in the original repo — authored new here.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobsmith.sourcing.adapters.hn_whos_hiring import (
    HNWhosHiringAdapter,
    parse_hn_payload,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "ats"
    / "hn_whos_hiring_response.json"
)


def test_parse_payload_returns_expected_role_count() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_hn_payload(payload, thread_id="43022668"))
    # All 3 hits have text, so 3 roles
    assert len(roles) == 3


def test_parse_payload_strips_html() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_hn_payload(payload, thread_id="43022668"))
    for r in roles:
        assert "<p>" not in r.jd_text
        assert "<br>" not in r.jd_text


def test_parse_payload_remote_detection() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_hn_payload(payload, thread_id="43022668"))
    # First role has REMOTE in text
    acme = next(r for r in roles if "Acme" in r.company)
    assert acme.location == "Remote"


def test_parse_payload_url_format() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_hn_payload(payload, thread_id="43022668"))
    for r in roles:
        assert r.url.startswith("https://news.ycombinator.com/item?id=")


def test_parse_payload_source_fields() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_hn_payload(payload, thread_id="43022668"))
    for r in roles:
        assert r.source == "hn_whos_hiring"
        assert r.source_slug == "43022668"


def test_parse_payload_id_format() -> None:
    payload = json.loads(FIXTURE.read_text())
    roles = list(parse_hn_payload(payload, thread_id="43022668"))
    for r in roles:
        assert r.id.startswith("hn:43022668:")


def test_parse_empty_hits_returns_empty() -> None:
    roles = list(parse_hn_payload({"hits": []}, thread_id="43022668"))
    assert roles == []


def test_adapter_name() -> None:
    a = HNWhosHiringAdapter()
    assert a.name == "hn_whos_hiring"


def test_adapter_empty_slug_returns_empty() -> None:
    """Empty slug is the case when thread ID not yet populated in sourcing.yaml."""
    a = HNWhosHiringAdapter()
    roles = list(a.fetch(""))
    assert roles == []
