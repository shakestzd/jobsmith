"""Tests for GET /api/feedback.

Coverage:
1. GET /api/feedback without Authorization header → 401
2. GET /api/feedback with valid token returns 200 with a JSON list
3. GET /api/feedback?kind=note passes filter through to list_records
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Reset cached token between tests."""
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


TOKEN = "test-feedback-token-abc"

_SAMPLE_RECORDS = [
    {
        "slug": "acme-corp-swe",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "kind": "prose-bullet",
        "before": "- Built pipelines",
        "after": "- Built scalable pipelines handling 10TB",
        "lesson": "",
        "context": None,
    },
    {
        "slug": "beta-inc-pm",
        "timestamp": "2026-02-01T00:00:00+00:00",
        "kind": "cover-letter-paragraph",
        "before": "I worked on products.",
        "after": "I led product strategy across three verticals.",
        "lesson": "Start with impact",
        "context": {"role_type": "PM"},
    },
]


@pytest.fixture()
def client():
    """TestClient with a known Bearer token set via env."""
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN}):
        app = create_app()
        yield TestClient(app, raise_server_exceptions=True)


def _auth(tok: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_get_feedback_no_auth_returns_401(client: TestClient) -> None:
    """Missing token → 401."""
    resp = client.get("/api/feedback")
    assert resp.status_code == 401


def test_get_feedback_wrong_token_returns_401(client: TestClient) -> None:
    """Wrong token → 401."""
    resp = client.get("/api/feedback", headers=_auth("bad-token"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_get_feedback_returns_200_with_list(client: TestClient) -> None:
    """Authenticated request → 200 with a JSON list."""
    with patch("jobsmith.api.feedback.list_records", return_value=_SAMPLE_RECORDS):
        resp = client.get("/api/feedback", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2


def test_get_feedback_returns_correct_fields(client: TestClient) -> None:
    """Each record has slug, timestamp, kind, before, after, lesson fields."""
    with patch("jobsmith.api.feedback.list_records", return_value=_SAMPLE_RECORDS[:1]):
        resp = client.get("/api/feedback", headers=_auth())
    item = resp.json()[0]
    assert item["slug"] == "acme-corp-swe"
    assert item["kind"] == "prose-bullet"
    assert "before" in item
    assert "after" in item
    assert "lesson" in item
    assert "timestamp" in item


def test_get_feedback_kind_filter_passed_to_list_records(client: TestClient) -> None:
    """?kind=note query param is forwarded to list_records as filter_kind."""
    with patch("jobsmith.api.feedback.list_records", return_value=[]) as mock_lr:
        resp = client.get("/api/feedback?kind=note", headers=_auth())
    assert resp.status_code == 200
    mock_lr.assert_called_once()
    call_kwargs = mock_lr.call_args
    # filter_kind is the first positional arg or keyword
    filter_kind = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("filter_kind")
    assert filter_kind == "note"


def test_get_feedback_since_filter_passed_to_list_records(client: TestClient) -> None:
    """?since=2026-01-01 query param is forwarded to list_records as since datetime."""
    with patch("jobsmith.api.feedback.list_records", return_value=[]) as mock_lr:
        resp = client.get("/api/feedback?since=2026-01-01T00:00:00", headers=_auth())
    assert resp.status_code == 200
    mock_lr.assert_called_once()
    call_kwargs = mock_lr.call_args
    since_val = call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs.get("since")
    assert since_val is not None


def test_get_feedback_empty_list_returns_200(client: TestClient) -> None:
    """Edge case: no feedback records → 200 with empty list."""
    with patch("jobsmith.api.feedback.list_records", return_value=[]):
        resp = client.get("/api/feedback", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_feedback_no_filters_passes_none(client: TestClient) -> None:
    """Without query params, list_records receives None for both filter args."""
    with patch("jobsmith.api.feedback.list_records", return_value=[]) as mock_lr:
        resp = client.get("/api/feedback", headers=_auth())
    assert resp.status_code == 200
    call_kwargs = mock_lr.call_args
    filter_kind = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("filter_kind")
    since_val = call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs.get("since")
    assert filter_kind is None
    assert since_val is None
