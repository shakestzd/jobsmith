"""Tests for POST /onboard, GET /onboard/{run_id}, POST /onboard/{run_id}/answers.

TDD Protocol: these tests are written BEFORE the implementation.
They cover:
- POST /onboard accepts multipart and launches via in-process path
- 413 on >10MB upload
- GET /onboard/{run_id} status endpoint
- POST /onboard/{run_id}/answers feeds answers via the pending_answers mechanism
- Reuses slice-3 validator (not a duplicate)

All pipeline + LLM calls are mocked.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token, verify_token
from jobsmith.api.onboard_routes import router as onboard_router
from jobsmith.api.supervisor import RunSupervisor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TOKEN = "test-onboard-token-xyz"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


def _make_app(tmp_path, supervisor: RunSupervisor, monkeypatch) -> FastAPI:
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    _get_expected_token.cache_clear()

    app = FastAPI()
    app.include_router(
        onboard_router,
        prefix="/api",
        dependencies=[Depends(verify_token)],
    )
    app.state.run_supervisor = supervisor
    app.state.repo_root = tmp_path
    return app


@pytest.fixture()
def supervisor() -> RunSupervisor:
    return RunSupervisor(max_buffered_lines=100)


@pytest.fixture()
def client(tmp_path, supervisor, monkeypatch):
    app = _make_app(tmp_path, supervisor, monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def client_and_sup(tmp_path, supervisor, monkeypatch):
    app = _make_app(tmp_path, supervisor, monkeypatch)
    return TestClient(app, raise_server_exceptions=True), supervisor


# ---------------------------------------------------------------------------
# POST /onboard — happy-path: multipart launch
# ---------------------------------------------------------------------------


def test_post_onboard_accepts_paste_and_returns_run_id(client):
    """POST /onboard with paste text launches and returns {run_id, status}."""
    with (
        patch("jobsmith.api.onboard_routes.run_onboard_pipeline") as mock_pipe,
        patch("asyncio.create_task"),
    ):
        mock_pipe.return_value = 0
        resp = client.post(
            "/api/onboard",
            data={"paste": "My resume text here"},
            headers=AUTH_HEADER,
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "run_id" in body
    assert body["status"] == "running"


def test_post_onboard_accepts_resume_file(client, tmp_path):
    """POST /onboard with a file upload stores the file and launches."""
    resume_content = b"%PDF-1.4 fake pdf content"
    with (
        patch("jobsmith.api.onboard_routes.run_onboard_pipeline") as mock_pipe,
        patch("asyncio.create_task"),
    ):
        mock_pipe.return_value = 0
        resp = client.post(
            "/api/onboard",
            files={"resume_file": ("resume.pdf", io.BytesIO(resume_content), "application/pdf")},
            headers=AUTH_HEADER,
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "run_id" in body


def test_post_onboard_413_on_oversize_file(client):
    """POST /onboard returns 413 when the upload exceeds 10MB."""
    big_content = b"x" * (10 * 1024 * 1024 + 1)  # 10MB + 1 byte
    resp = client.post(
        "/api/onboard",
        files={"resume_file": ("big.pdf", io.BytesIO(big_content), "application/pdf")},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 413, resp.text


def test_post_onboard_accepts_linkedin_url(client):
    """POST /onboard with a LinkedIn URL launches."""
    with (
        patch("jobsmith.api.onboard_routes.run_onboard_pipeline") as mock_pipe,
        patch("asyncio.create_task"),
    ):
        mock_pipe.return_value = 0
        resp = client.post(
            "/api/onboard",
            data={"linkedin_url": "https://linkedin.com/in/testuser"},
            headers=AUTH_HEADER,
        )
    assert resp.status_code == 202, resp.text


def test_post_onboard_uses_in_process_path_not_subprocess(client):
    """POST /onboard must NOT spawn a subprocess; uses asyncio.to_thread."""
    with (
        patch("jobsmith.api.onboard_routes.run_onboard_pipeline") as mock_pipe,
        patch("asyncio.create_task"),
        patch("subprocess.Popen") as mock_popen,
    ):
        mock_pipe.return_value = 0
        resp = client.post(
            "/api/onboard",
            data={"paste": "test"},
            headers=AUTH_HEADER,
        )
    # subprocess.Popen must not be called
    mock_popen.assert_not_called()
    assert resp.status_code == 202, resp.text


def test_post_onboard_registers_run_with_supervisor(client_and_sup):
    """POST /onboard registers the new run with the supervisor."""
    client, sup = client_and_sup
    with (
        patch("jobsmith.api.onboard_routes.run_onboard_pipeline"),
        patch("asyncio.create_task"),
    ):
        resp = client.post(
            "/api/onboard",
            data={"paste": "test paste"},
            headers=AUTH_HEADER,
        )
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    # The run should be registered in the supervisor
    handle = sup.get(run_id)
    assert handle is not None
    assert handle.slug == "onboard"


def test_post_onboard_409_when_onboard_run_already_active(client_and_sup):
    """Concurrency guard (roborev job 982 #5): a second start returns 409 while
    an onboard run is active, since onboarding writes the shared master YAMLs."""
    client, sup = client_and_sup
    # Mark an onboard run as active.
    sup.register_run(run_id="already-running", slug="onboard")
    assert sup.get_active_for_slug("onboard") == "already-running"

    resp = client.post(
        "/api/onboard",
        data={"paste": "second run"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /onboard/{run_id} — status endpoint
# ---------------------------------------------------------------------------


def test_get_onboard_status_returns_run_handle(client_and_sup):
    """GET /onboard/{run_id} returns run status from supervisor."""
    client, sup = client_and_sup
    # Manually register a run so we can query it
    run_id = uuid.uuid4().hex
    sup.register_run(run_id=run_id, slug="onboard")

    resp = client.get(f"/api/onboard/{run_id}", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["status"] == "running"


def test_get_onboard_status_404_unknown_run(client):
    """GET /onboard/{run_id} returns 404 for unknown run_id."""
    resp = client.get("/api/onboard/unknown-run-id", headers=AUTH_HEADER)
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# POST /onboard/{run_id}/answers — gap-interview answer injection
# ---------------------------------------------------------------------------


def test_post_onboard_answers_stores_for_pending_run(client_and_sup):
    """POST /onboard/{run_id}/answers stores answers for pickup by the pipeline."""
    client, sup = client_and_sup
    run_id = uuid.uuid4().hex
    sup.register_run(run_id=run_id, slug="onboard")

    answers = {
        "author.name": "Jane Smith",
        "author.email": "jane@example.com",
        "work.entries": "Acme Corp, 2020-2023",
    }
    resp = client.post(
        f"/api/onboard/{run_id}/answers",
        json={"answers": answers},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["run_id"] == run_id


def test_post_onboard_answers_404_unknown_run(client):
    """POST /onboard/{run_id}/answers returns 404 for unknown run_id."""
    resp = client.post(
        "/api/onboard/no-such-run/answers",
        json={"answers": {"author.name": "X"}},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Validator reuse: ensure onboard_routes imports from the right place
# ---------------------------------------------------------------------------


def test_onboard_routes_imports_validate_from_pipeline_not_duplicate():
    """onboard_routes must import run_onboard_pipeline from onboard.pipeline, not duplicate it."""
    import inspect

    import jobsmith.api.onboard_routes as mod

    # The module should reference run_onboard_pipeline from onboard.pipeline
    source = inspect.getsource(mod)
    assert "from jobsmith.onboard.pipeline import" in source or \
           "jobsmith.onboard.pipeline" in source, \
        "onboard_routes must import from jobsmith.onboard.pipeline"

    # It must NOT define its own dispatch or run function that duplicates pipeline logic
    assert "def dispatch_onboard" not in source, \
        "onboard_routes must not duplicate dispatch_onboard_pipeline"
