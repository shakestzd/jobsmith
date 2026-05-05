"""Tests for POST /api/master/validate.

TDD: written BEFORE the route exists.  Run first to confirm FAIL, then implement.

Contract
--------
POST /api/master/validate
  - body: MasterPayload (work/skill/education/author sections, all optional)
  - returns: { ok: bool, errors: [{ field: str, message: str }] }

Auth
----
No token → 401
Valid token → 200 (regardless of content validity)

Validity cases
--------------
Valid master payload   → 200, ok=True, errors=[]
Invalid payload        → 200, ok=False, errors=[{field, message}, ...]
Malformed body (non-JSON or wrong types) → 422
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token, verify_token
from jobsmith.api.master import router

FIXTURE_WORK = Path(__file__).parent / "fixtures" / "master_work.yml"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-validate-token-xyz"

VALID_PAYLOAD = {
    "work": [
        {
            "title": "Senior Data Engineer",
            "location": "Acme Corp",
            "date": "Jan 2023 - Present",
            "description": "Remote",
            "details": [
                "Unlocked $250M in additional Investment Tax Credits",
                "Shipped 7 automated ETL pipelines at 99.9% reliability",
            ],
        }
    ],
    "skill": [
        {
            "title": "Languages",
            "description": "Python, SQL",
            "details": ["Python", "SQL"],
        }
    ],
    "education": [
        {
            "title": "State University",
            "location": "New York, NY",
            "date": "2015-2019",
            "description": "B.S. Computer Science",
            "details": [],
        }
    ],
    "author": {
        "name": "Jane Doe",
        "email": "jane@example.com",
    },
}

INVALID_PAYLOAD_EMPTY_WORK_TITLE = {
    "work": [
        {
            "title": "",  # empty title should fail validation
            "location": "Acme Corp",
            "date": "Jan 2023 - Present",
            "description": "Remote",
            "details": [],
        }
    ],
    "skill": [],
    "education": [],
    "author": None,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Reset the cached expected token between tests."""
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """Minimal repo with work.yml seeded from fixture."""
    (tmp_path / ".apply-config.yaml").write_text("", encoding="utf-8")
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    shutil.copy(FIXTURE_WORK, content_dir / "work.yml")
    return tmp_path


@pytest.fixture()
def client_no_auth(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client with auth dependency applied but no token in requests."""
    monkeypatch.chdir(repo_root)
    with patch.dict(os.environ, {TOKEN_ENV_VAR: TEST_TOKEN}):
        app = FastAPI()
        app.include_router(router, prefix="/api", dependencies=[Depends(verify_token)])
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client without auth dependency (direct router mount, no token required)."""
    monkeypatch.chdir(repo_root)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test: 401 without auth
# ---------------------------------------------------------------------------


class TestValidateAuth:
    def test_no_token_returns_401(self, client_no_auth: TestClient) -> None:
        """POST /api/master/validate without Bearer token returns 401."""
        resp = client_no_auth.post("/api/master/validate", json=VALID_PAYLOAD)
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Test: 200 with valid master content
# ---------------------------------------------------------------------------


class TestValidateValidContent:
    def test_valid_payload_returns_200(self, client: TestClient) -> None:
        """POST /api/master/validate with valid content returns 200."""
        resp = client.post("/api/master/validate", json=VALID_PAYLOAD)
        assert resp.status_code == 200, resp.text

    def test_valid_payload_ok_true(self, client: TestClient) -> None:
        """POST with valid payload returns ok=True."""
        resp = client.post("/api/master/validate", json=VALID_PAYLOAD)
        data = resp.json()
        assert data["ok"] is True

    def test_valid_payload_empty_errors(self, client: TestClient) -> None:
        """POST with valid payload returns errors=[]."""
        resp = client.post("/api/master/validate", json=VALID_PAYLOAD)
        data = resp.json()
        assert data["errors"] == []

    def test_empty_sections_are_valid(self, client: TestClient) -> None:
        """POST with all empty sections is still valid (no required fields at top level)."""
        resp = client.post(
            "/api/master/validate",
            json={"work": [], "skill": [], "education": [], "author": None},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Test: 200 with invalid content → ok=False + errors
# ---------------------------------------------------------------------------


class TestValidateInvalidContent:
    def test_invalid_payload_returns_200(self, client: TestClient) -> None:
        """POST with invalid content still returns 200 (not 4xx)."""
        resp = client.post(
            "/api/master/validate", json=INVALID_PAYLOAD_EMPTY_WORK_TITLE
        )
        assert resp.status_code == 200, resp.text

    def test_invalid_payload_ok_false(self, client: TestClient) -> None:
        """POST with invalid content returns ok=False."""
        resp = client.post(
            "/api/master/validate", json=INVALID_PAYLOAD_EMPTY_WORK_TITLE
        )
        data = resp.json()
        assert data["ok"] is False

    def test_invalid_payload_has_errors(self, client: TestClient) -> None:
        """POST with invalid content returns at least one error entry."""
        resp = client.post(
            "/api/master/validate", json=INVALID_PAYLOAD_EMPTY_WORK_TITLE
        )
        data = resp.json()
        assert len(data["errors"]) > 0

    def test_error_has_field_and_message(self, client: TestClient) -> None:
        """Each error entry has 'field' and 'message' keys."""
        resp = client.post(
            "/api/master/validate", json=INVALID_PAYLOAD_EMPTY_WORK_TITLE
        )
        data = resp.json()
        for error in data["errors"]:
            assert "field" in error, f"Missing 'field' in {error}"
            assert "message" in error, f"Missing 'message' in {error}"


# ---------------------------------------------------------------------------
# Test: 422 with malformed body
# ---------------------------------------------------------------------------


class TestValidateMalformedBody:
    def test_non_object_body_returns_422(self, client: TestClient) -> None:
        """POST with a JSON array (not object) body returns 422."""
        resp = client.post("/api/master/validate", json=[1, 2, 3])
        assert resp.status_code == 422, resp.text

    def test_wrong_type_for_work_returns_422(self, client: TestClient) -> None:
        """POST with work as a string (not list) returns 422."""
        resp = client.post(
            "/api/master/validate",
            json={"work": "not-a-list", "skill": [], "education": [], "author": None},
        )
        assert resp.status_code == 422, resp.text
