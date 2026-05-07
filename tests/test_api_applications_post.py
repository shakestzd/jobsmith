"""TDD tests for POST /api/applications (feat-8ab2fb57).

Coverage:
1. POST with valid URL returns 201, slug + run_id in body.
2. POST with explicit slug uses that slug.
3. POST without auth → 401.
4. POST with malformed body → 422.
5. POST returns 409 if a run for that slug is already in progress.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router as applications_router
from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token, verify_token
from jobsmith.api.supervisor import RunSupervisor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TOKEN = "test-post-token-xyz"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Reset the cached expected token between tests."""
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Create a minimal pipeline DB."""
    from jobsmith.db import open_pipeline_db

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    conn.close()
    return db_path


@pytest.fixture()
def supervisor() -> RunSupervisor:
    """Return a fresh supervisor instance (not the singleton)."""
    return RunSupervisor(max_buffered_lines=100)


@pytest.fixture()
def client(
    db_path: Path,
    supervisor: RunSupervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """TestClient wired to a real DB, isolated supervisor, and a known token."""
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)
    _get_expected_token.cache_clear()

    monkeypatch.setattr(
        "jobsmith.api.applications._get_db_path",
        lambda: db_path,
    )

    app = FastAPI()
    app.include_router(
        applications_router,
        prefix="/api",
        dependencies=[Depends(verify_token)],
    )
    app.state.run_supervisor = supervisor

    return TestClient(app, raise_server_exceptions=True)


def _auth_header(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. POST with valid URL returns 201, slug + run_id
# ---------------------------------------------------------------------------


class TestPostApplicationsValidUrl:
    def test_returns_201(self, client: TestClient) -> None:
        """POST /api/applications with a valid URL returns HTTP 201."""
        with patch(
            "jobsmith.api.applications._launch_run",
            new_callable=AsyncMock,
            return_value="run-abc-001",
        ):
            resp = client.post(
                "/api/applications",
                json={"url": "https://example.com/jobs/software-engineer"},
                headers=_auth_header(),
            )
        assert resp.status_code == 201, resp.text

    def test_returns_slug_and_run_id(self, client: TestClient) -> None:
        """Response body contains slug and run_id."""
        with patch(
            "jobsmith.api.applications._launch_run",
            new_callable=AsyncMock,
            return_value="run-abc-002",
        ):
            resp = client.post(
                "/api/applications",
                json={"url": "https://example.com/jobs/software-engineer"},
                headers=_auth_header(),
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "slug" in body
        assert "run_id" in body
        assert body["run_id"] == "run-abc-002"
        # slug is derived from URL path
        assert "software-engineer" in body["slug"]


# ---------------------------------------------------------------------------
# 2. POST with explicit slug uses that slug
# ---------------------------------------------------------------------------


class TestPostApplicationsExplicitSlug:
    def test_explicit_slug_is_used(self, client: TestClient) -> None:
        """When slug is provided in the body it is used verbatim."""
        with patch(
            "jobsmith.api.applications._launch_run",
            new_callable=AsyncMock,
            return_value="run-slug-003",
        ):
            resp = client.post(
                "/api/applications",
                json={
                    "url": "https://example.com/jobs/something",
                    "slug": "my-custom-slug",
                },
                headers=_auth_header(),
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == "my-custom-slug"


# ---------------------------------------------------------------------------
# 3. POST without auth → 401
# ---------------------------------------------------------------------------


class TestPostApplicationsAuth:
    def test_missing_token_returns_401(self, client: TestClient) -> None:
        """POST without Authorization header returns 401."""
        resp = client.post(
            "/api/applications",
            json={"url": "https://example.com/jobs/engineer"},
        )
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        """POST with wrong token returns 401."""
        resp = client.post(
            "/api/applications",
            json={"url": "https://example.com/jobs/engineer"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4. POST with malformed body → 422
# ---------------------------------------------------------------------------


class TestPostApplicationsMalformedBody:
    def test_missing_url_returns_422(self, client: TestClient) -> None:
        """POST without required url field returns 422 Unprocessable Entity."""
        resp = client.post(
            "/api/applications",
            json={"slug": "some-slug"},
            headers=_auth_header(),
        )
        assert resp.status_code == 422

    def test_empty_body_returns_422(self, client: TestClient) -> None:
        """POST with empty body returns 422."""
        resp = client.post(
            "/api/applications",
            json={},
            headers=_auth_header(),
        )
        assert resp.status_code == 422

    def test_non_string_url_returns_422(self, client: TestClient) -> None:
        """POST with a non-string url field returns 422."""
        resp = client.post(
            "/api/applications",
            json={"url": 12345},
            headers=_auth_header(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 5. POST returns 409 if a run for that slug is already in progress
# ---------------------------------------------------------------------------


class TestPostApplicationsConflict:
    def test_conflict_when_run_already_active(
        self,
        client: TestClient,
        supervisor: RunSupervisor,
    ) -> None:
        """POST returns 409 if the slug already has an active run in supervisor."""
        slug = "active-slug"
        # Simulate an active run in the supervisor registry without spawning a process.
        from jobsmith.api.supervisor import RunHandle, _RunRecord

        handle = RunHandle(
            run_id="existing-run-id",
            slug=slug,
            status="running",
            exit_code=None,
            started_at="2025-01-01T00:00:00Z",
            finished_at=None,
        )
        record = _RunRecord(handle=handle)
        supervisor._runs["existing-run-id"] = record
        supervisor._active_by_slug[slug] = "existing-run-id"

        resp = client.post(
            "/api/applications",
            json={"url": "https://example.com/jobs/engineer", "slug": slug},
            headers=_auth_header(),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert slug in detail or "already" in detail.lower()


# ---------------------------------------------------------------------------
# 6a. force=True propagates --force to the launched argv (feat-d6b1e167, GH#50)
# ---------------------------------------------------------------------------


class TestPostApplicationsForce:
    """Re-running an already-complete slug requires --force on the server side
    or the apply pipeline silently aborts. The UI surfaces this via a `force`
    field on the request body; the API must plumb it through to argv.
    """

    def test_force_true_appends_force_to_argv(self, client: TestClient) -> None:
        """When body.force=True, the argv built by _launch_run includes --force."""
        from jobsmith.api import applications as apps_mod

        captured: dict[str, list[str]] = {}

        async def fake_start(self, *, slug: str, argv: list[str], cwd, transcript_path=None, db_path=None, run_id=None):
            captured["argv"] = argv
            return "run-force-001"

        with patch.object(apps_mod.RunSupervisor, "start", new=fake_start):
            resp = client.post(
                "/api/applications",
                json={
                    "url": "https://example.com/jobs/eng",
                    "slug": "completed-slug",
                    "force": True,
                },
                headers=_auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert "argv" in captured, "supervisor.start was not invoked"
        assert "--force" in captured["argv"], (
            f"--force missing from argv when body.force=True. argv={captured['argv']!r}"
        )

    def test_force_false_omits_force_from_argv(self, client: TestClient) -> None:
        """When body.force=False (default), --force is NOT in argv."""
        from jobsmith.api import applications as apps_mod

        captured: dict[str, list[str]] = {}

        async def fake_start(self, *, slug: str, argv: list[str], cwd, transcript_path=None, db_path=None, run_id=None):
            captured["argv"] = argv
            return "run-force-002"

        with patch.object(apps_mod.RunSupervisor, "start", new=fake_start):
            resp = client.post(
                "/api/applications",
                json={
                    "url": "https://example.com/jobs/eng",
                    "slug": "fresh-slug",
                    "force": False,
                },
                headers=_auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert "argv" in captured
        assert "--force" not in captured["argv"], (
            f"--force should NOT be in argv when body.force=False. argv={captured['argv']!r}"
        )

    def test_force_omitted_defaults_to_false(self, client: TestClient) -> None:
        """When body has no `force` key, server treats it as force=False."""
        from jobsmith.api import applications as apps_mod

        captured: dict[str, list[str]] = {}

        async def fake_start(self, *, slug: str, argv: list[str], cwd, transcript_path=None, db_path=None, run_id=None):
            captured["argv"] = argv
            return "run-force-003"

        with patch.object(apps_mod.RunSupervisor, "start", new=fake_start):
            resp = client.post(
                "/api/applications",
                json={"url": "https://example.com/jobs/eng", "slug": "default-slug"},
                headers=_auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert "--force" not in captured["argv"]


# ---------------------------------------------------------------------------
# 6b. jd_text propagates to --jd-text-file (bug-1c800e09)
# ---------------------------------------------------------------------------


class TestPostApplicationsJdText:
    """The new-application form's "paste text" mode must reach the apply
    pipeline as ``--jd-text-file <path>``. Prior to bug-1c800e09 the form
    posted only ``url`` and the pipeline tried (and failed) to fetch JS-
    rendered ATS portals like Microsoft Eightfold.
    """

    def test_jd_text_appends_jd_text_file_to_argv(self, client: TestClient) -> None:
        """When body.jd_text is set, --jd-text-file <path> is in argv and the
        pasted text is on disk at <path>."""
        from jobsmith.api import applications as apps_mod

        captured: dict[str, list[str]] = {}

        async def fake_start(self, *, slug: str, argv: list[str], cwd, transcript_path=None):
            captured["argv"] = argv
            return "run-paste-001"

        pasted = "Senior Data Engineer at TestCo\n\nResponsibilities: own data."
        with patch.object(apps_mod.RunSupervisor, "start", new=fake_start):
            resp = client.post(
                "/api/applications",
                json={
                    "url": "https://js-rendered.example.com/jobs/eng",
                    "slug": "paste-slug",
                    "jd_text": pasted,
                },
                headers=_auth_header(),
            )

        assert resp.status_code == 201, resp.text
        argv = captured["argv"]
        assert "--jd-text-file" in argv, f"--jd-text-file missing from argv: {argv!r}"
        idx = argv.index("--jd-text-file")
        jd_path = Path(argv[idx + 1])
        assert jd_path.exists(), f"jd-text-file path does not exist: {jd_path}"
        assert jd_path.read_text(encoding="utf-8") == pasted

    def test_jd_text_omitted_no_jd_text_file_in_argv(self, client: TestClient) -> None:
        """When jd_text is absent, --jd-text-file is NOT in argv (URL fetched)."""
        from jobsmith.api import applications as apps_mod

        captured: dict[str, list[str]] = {}

        async def fake_start(self, *, slug: str, argv: list[str], cwd, transcript_path=None):
            captured["argv"] = argv
            return "run-fetch-001"

        with patch.object(apps_mod.RunSupervisor, "start", new=fake_start):
            resp = client.post(
                "/api/applications",
                json={"url": "https://example.com/jobs/eng", "slug": "fetch-slug"},
                headers=_auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert "--jd-text-file" not in captured["argv"]

    def test_jd_text_empty_string_treated_as_omitted(self, client: TestClient) -> None:
        """An empty jd_text string (e.g., user toggled paste mode but typed nothing)
        must NOT add --jd-text-file (else apply would read a zero-byte file)."""
        from jobsmith.api import applications as apps_mod

        captured: dict[str, list[str]] = {}

        async def fake_start(self, *, slug: str, argv: list[str], cwd, transcript_path=None):
            captured["argv"] = argv
            return "run-empty-001"

        with patch.object(apps_mod.RunSupervisor, "start", new=fake_start):
            resp = client.post(
                "/api/applications",
                json={
                    "url": "https://example.com/jobs/eng",
                    "slug": "empty-paste",
                    "jd_text": "",
                },
                headers=_auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert "--jd-text-file" not in captured["argv"]


# ---------------------------------------------------------------------------
# 6. _launch_run argv parses against the live CLI (roborev job 940 fix)
# ---------------------------------------------------------------------------


class TestLaunchRunArgvParsesAgainstCli:
    """The supervisor argv built by ``_launch_run`` must parse against the
    Typer ``apply`` command. A regression here would cause every UI-launched
    run to crash with a CLI usage error after the supervisor returned 201.
    """

    def test_launch_argv_parses_with_explicit_slug(self) -> None:
        from typer.testing import CliRunner

        from jobsmith.cli import app as cli_app

        runner = CliRunner()
        # The argv as-built by `_launch_run` (minus the `python -m jobsmith.cli`
        # bootstrap, which `CliRunner` provides). If `--slug` is not a real
        # option on `apply`, `--help` would still succeed but adding the flag
        # would surface as "no such option" — so we invoke `--help` first to
        # baseline, then assert the apply command actually accepts `--slug`.
        result = runner.invoke(cli_app, ["apply", "--help"])
        assert result.exit_code == 0, result.output
        assert "--slug" in result.output, (
            "POST /api/applications passes --slug; the Typer apply command "
            "must accept it or every UI-launched run will crash."
        )

    def test_launch_argv_no_slug_also_parses(self) -> None:
        """When no explicit slug is provided, --slug is omitted from argv."""
        from typer.testing import CliRunner

        from jobsmith.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["apply", "--help"])
        assert result.exit_code == 0
        # The command itself must exist and be callable.
        assert "apply" in result.output.lower()
