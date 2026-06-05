"""Tests for GET /api/applications and GET /api/applications/{slug} endpoints.

Coverage:
- role + company fields are extracted from the jd-parsed artifact and
  surfaced in both the list and detail responses.
- ui_phase derived field follows the documented taxonomy mapping raw DB
  (phase, status) pairs to UI-facing states: running, review, rendered, failed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router as applications_router
from jobsmith.db import open_pipeline_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, *, with_jd_parsed: bool = True) -> tuple[Path, str, str]:
    """Create pipeline DB with one run. Optionally includes a jd-parsed artifact."""
    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)

    slug = "acme-swe-2025"
    run_id = "run-abc123"

    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "gather", "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z", "done"),
    )
    if with_jd_parsed:
        conn.execute(
            "INSERT INTO specialist_outputs "
            "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "apply-jd-parser",
                "jd-parsed",
                json.dumps({"company": "Acme Corp", "position": "Senior SWE"}),
                None,
                "2025-01-01T10:02:00Z",
            ),
        )
    conn.commit()
    conn.close()

    return db_path, slug, run_id


@pytest.fixture()
def client_with_jd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, str]:
    """TestClient wired to a DB that has a jd-parsed artifact."""
    db_path, slug, run_id = _make_db(tmp_path, with_jd_parsed=True)
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), slug, run_id


@pytest.fixture()
def client_no_jd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, str]:
    """TestClient wired to a DB that has NO jd-parsed artifact."""
    db_path, slug, run_id = _make_db(tmp_path, with_jd_parsed=False)
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), slug, run_id


# ---------------------------------------------------------------------------
# List endpoint — role + company
# ---------------------------------------------------------------------------


class TestListApplicationsRoleCompany:
    def test_role_and_company_present_in_list(
        self, client_with_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications includes role + company from jd-parsed artifact."""
        client, slug, _ = client_with_jd
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["role"] == "Senior SWE"
        assert row["company"] == "Acme Corp"

    def test_list_hides_superseded_starting_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        rows = [
            ("run-start", "job-boards-8472178002-2026-05", "gather", "2026-05-11T19:30:00Z", None, "failed"),
            ("run-canon", "gitlab-senior-data-analyst-marketing-analytics", "render", "2026-05-12T01:00:00Z", "2026-05-12T01:04:00Z", "done"),
        ]
        conn.executemany(
            "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        for run_id, *_ in rows:
            conn.execute(
                "INSERT INTO specialist_outputs "
                "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    "apply-jd-parser",
                    "jd-parsed",
                    json.dumps({"company": "GitLab", "position": "Senior Data Analyst, Marketing Analytics"}),
                    None,
                    "2026-05-12T01:02:00Z",
                ),
            )
        conn.commit()
        conn.close()
        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")

        resp = TestClient(app, raise_server_exceptions=True).get("/api/applications")

        assert resp.status_code == 200, resp.text
        slugs = [row["slug"] for row in resp.json()]
        assert "gitlab-senior-data-analyst-marketing-analytics" in slugs
        assert "job-boards-8472178002-2026-05" not in slugs

    def test_role_company_null_when_no_jd_parsed(
        self, client_no_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications returns null role + company when jd-parsed absent."""
        client, _, _ = client_no_jd
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["role"] is None
        assert row["company"] is None


# ---------------------------------------------------------------------------
# Detail endpoint — role + company
# ---------------------------------------------------------------------------


class TestGetApplicationRoleCompany:
    def test_role_and_company_in_detail(
        self, client_with_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications/{slug} includes role + company from jd-parsed artifact."""
        client, slug, _ = client_with_jd
        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["role"] == "Senior SWE"
        assert data["company"] == "Acme Corp"

    def test_role_company_null_in_detail_when_no_jd_parsed(
        self, client_no_jd: tuple[TestClient, str, str]
    ) -> None:
        """GET /api/applications/{slug} returns null role + company when jd-parsed absent."""
        client, slug, _ = client_no_jd
        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["role"] is None
        assert data["company"] is None

    def test_detail_reads_canonical_disk_artifacts_when_db_ingest_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failed in-flight runs can have files but no specialist_outputs rows."""
        db_path = tmp_path / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        starting_slug = "job-boards-8472178002-2026-05"
        canonical_slug = "gitlab-senior-data-analyst-marketing-analytics"
        run_id = "run-gitlab"
        conn.execute(
            "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, starting_slug, "gather", "2026-05-11T17:33:17Z", None, "running"),
        )
        conn.execute(
            "INSERT INTO apply_state_log (run_id, slug, ts, payload) VALUES (?, ?, ?, ?)",
            (
                run_id,
                canonical_slug,
                "2026-05-11T17:34:00Z",
                json.dumps({
                    "type": "tool_result",
                    "content": (
                        "jobsmith db rekey-slug --from job-boards-8472178002-2026-05 "
                        "--to gitlab-senior-data-analyst-marketing-analytics"
                    ),
                }),
            ),
        )
        conn.commit()
        conn.close()

        apps_root = tmp_path / "applications"
        canonical_state = apps_root / canonical_slug / ".apply-state"
        canonical_state.mkdir(parents=True)
        (canonical_state / "jd-parsed.json").write_text(
            json.dumps({
                "company": "GitLab",
                "position": "Senior Data Analyst, Marketing Analytics",
                "apply_url": "https://job-boards.greenhouse.io/gitlab/jobs/8472178002",
            }),
            encoding="utf-8",
        )
        (canonical_state / "fit-score.json").write_text(
            json.dumps({"score": 0.28, "rationale": "domain miss"}),
            encoding="utf-8",
        )

        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda requested_slug: apps_root / requested_slug,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")

        resp = TestClient(app, raise_server_exceptions=True).get(
            f"/api/applications/{starting_slug}"
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["role"] == "Senior Data Analyst, Marketing Analytics"
        assert data["company"] == "GitLab"
        artifacts = {(a["specialist"], a["kind"]) for a in data["artifacts"]}
        assert ("apply-jd-parser", "jd-parsed") in artifacts
        assert ("apply-fit-scorer", "fit-score") in artifacts


class TestReviewCoverLetterResolution:
    def test_review_reads_root_cover_letter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, slug, _ = _make_db(tmp_path, with_jd_parsed=True)
        app_dir = tmp_path / "applications" / slug
        app_dir.mkdir(parents=True)
        (app_dir / "cover-letter-draft.md").write_text("Dear Acme,\n", encoding="utf-8")
        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda requested_slug: app_dir if requested_slug == slug else None,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")

        resp = TestClient(app, raise_server_exceptions=True).get(f"/api/applications/{slug}/review")

        assert resp.status_code == 200, resp.text
        assert resp.json()["cover_letter"] == "Dear Acme,\n"

    def test_review_reads_apply_state_cover_letter_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path, slug, _ = _make_db(tmp_path, with_jd_parsed=True)
        app_dir = tmp_path / "applications" / slug
        (app_dir / ".apply-state").mkdir(parents=True)
        (app_dir / ".apply-state" / "cover-letter-draft.md").write_text(
            "Hello from state,\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda requested_slug: app_dir if requested_slug == slug else None,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")

        resp = TestClient(app, raise_server_exceptions=True).get(f"/api/applications/{slug}/review")

        assert resp.status_code == 200, resp.text
        assert resp.json()["cover_letter"] == "Hello from state,\n"


# ---------------------------------------------------------------------------
# ui_phase taxonomy mapping
# ---------------------------------------------------------------------------


def _make_db_with_phase(
    tmp_path: Path, *, phase: str, status: str, suffix: str = ""
) -> tuple[Path, str, str]:
    """Create a pipeline DB with a single run using specified phase + status."""
    db_path = tmp_path / f"jobsmith{suffix}.db"
    conn = open_pipeline_db(db_path)
    slug = f"test-slug{suffix}"
    run_id = f"run-test{suffix}"
    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, phase, "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z", status),
    )
    conn.commit()
    conn.close()
    return db_path, slug, run_id


def _make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, phase: str, status: str, suffix: str = ""
) -> tuple[TestClient, str]:
    db_path, slug, _ = _make_db_with_phase(tmp_path, phase=phase, status=status, suffix=suffix)
    monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), slug


class TestUiPhaseMapping:
    def test_gather_phase_maps_to_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw phase='gather' + status='running' → ui_phase='running'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="gather", status="running")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "running"

    def test_render_phase_maps_to_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw phase='render' + status='running' → ui_phase='running'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="render", status="running")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "running"

    def test_render_done_maps_to_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw phase='render' + status='done' → ui_phase='rendered'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="render", status="done")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "rendered"

    def test_failed_status_maps_to_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """raw status='failed' → ui_phase='failed' regardless of phase."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="gather", status="failed")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "failed"

    def test_failed_specialist_halt_maps_to_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Specialist halt results are user-review work, not a generic failure."""
        db_path, slug, _run_id = _make_db_with_phase(
            tmp_path, phase="gather", status="failed", suffix="-halt"
        )
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT INTO apply_state (slug, kind, content_blob, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                slug,
                "apply-bullet-selector-result",
                json.dumps({
                    "status": "halt",
                    "reason": "UNCOVERED_MUST_HAVE",
                    "must_have": "Hands-on dbt expertise",
                }),
                "2026-05-11T15:05:00Z",
            ),
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")

        row = TestClient(app, raise_server_exceptions=True).get("/api/applications").json()[0]
        assert row["ui_phase"] == "review"
        assert row["status"] == "review"

    def test_detail_endpoint_exposes_ui_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/applications/{slug} also includes ui_phase field."""
        client, slug = _make_client(tmp_path, monkeypatch, phase="render", status="done")
        resp = client.get(f"/api/applications/{slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "ui_phase" in data
        assert data["ui_phase"] == "rendered"

    def test_unknown_phase_done_maps_to_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI full pipeline records phase='unknown' + status='done' for
        completed runs. These must map to ui_phase='rendered' so they show up
        in the rendered dashboard filter (roborev job 940 finding).
        """
        client, _ = _make_client(tmp_path, monkeypatch, phase="unknown", status="done")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "rendered"

    def test_backfilled_status_maps_to_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status='backfilled' (any phase) → ui_phase='rendered'."""
        client, _ = _make_client(tmp_path, monkeypatch, phase="unknown", status="backfilled")
        resp = client.get("/api/applications")
        assert resp.status_code == 200, resp.text
        row = resp.json()[0]
        assert row["ui_phase"] == "rendered"


# ---------------------------------------------------------------------------
# Rekey-aware slug resolution (bug-f63a7dd7)
# ---------------------------------------------------------------------------


class TestRekeySlugResolution:
    """Tests for slug rekey: stale URL-derived slug → canonical slug resolution.

    Simulates the scenario where:
      1.  POST /applications returns starting_slug = "becu-Sr-Data-Analyst_R-13411-2026-06"
      2.  The jd-parser rekeys the apply_runs.slug to canonical = "becu-sr-data-analyst"
      3.  apply_state_log contains the rekey command payload
      4.  Artifacts (resume.pdf, cover-letter-draft.md) live under canonical dir

    Asserts that GET /applications/{starting_slug},
    GET /applications/{starting_slug}/documents, and
    GET /applications/{starting_slug}/review all resolve correctly — not 404.
    """

    @staticmethod
    def _make_rekeyed_db(tmp_path: Path) -> tuple[Path, str, str, str]:
        """Create a DB + filesystem layout that mirrors a post-rekey apply run.

        Uses the REAL pipeline evidence shape: the agentic orchestrator stores
        a manifest record in ``apply_state`` under the *canonical* slug whose
        JSON body has a ``"slug"`` field preserving the original pre-rekey slug.
        No ``--from ... --to ...`` string is written to ``apply_state_log``
        in normal agentic runs (only present in synthetic/legacy fixtures).

        Returns (db_path, starting_slug, canonical_slug, run_id).
        """
        starting_slug = "becu-Sr-Data-Analyst_R-13411-2026-06"
        canonical_slug = "becu-sr-data-analyst"
        run_id = "run-becu-rekey"

        db_path = tmp_path / "jobsmith.db"
        conn = open_pipeline_db(db_path)

        # apply_runs.slug is the canonical slug (updated mid-run by the pipeline).
        conn.execute(
            "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                canonical_slug,
                "render",
                "2026-06-01T10:00:00Z",
                "2026-06-01T10:15:00Z",
                "done",
            ),
        )
        # jd-parsed artifact in specialist_outputs (used by _extract_jd_fields).
        conn.execute(
            "INSERT INTO specialist_outputs "
            "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "apply-jd-parser",
                "jd-parsed",
                json.dumps({
                    "company": "BECU",
                    "position": "Sr Data Analyst",
                    "apply_url": "https://careers.becu.org/jobs/R-13411",
                }),
                None,
                "2026-06-01T10:02:00Z",
            ),
        )
        # apply_state manifest under canonical slug, body preserves starting_slug.
        # This is the REAL signal the pipeline writes: reconcile_canonical_slug
        # renames the directory and the orchestrator writes the manifest under
        # the canonical slug with the original "slug" field intact.
        conn.execute(
            "INSERT INTO apply_state (slug, kind, content_blob, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                canonical_slug,
                "manifest",
                json.dumps({
                    "run_id": run_id,
                    "slug": starting_slug,  # original pre-rekey slug preserved here
                    "started_at": "2026-06-01T10:00:00Z",
                    "slug_derived_from": "slug_override",
                    "role_type": "data-analyst",
                    "tier": "deep",
                    "fit_score": 0.85,
                }),
                "2026-06-01T10:01:00Z",
            ),
        )
        conn.commit()
        conn.close()

        return db_path, starting_slug, canonical_slug, run_id

    def test_detail_resolves_rekeyed_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /applications/{stale_slug} returns 200 (not 404) after rekey."""
        db_path, starting_slug, canonical_slug, _ = self._make_rekeyed_db(tmp_path)
        apps_root = tmp_path / "applications"
        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda s: apps_root / s,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/api/applications/{starting_slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # The response slug should be the canonical one (from the DB row).
        assert data["slug"] == canonical_slug
        assert data["status"] == "done"
        assert data["company"] == "BECU"

    def test_list_documents_resolves_rekeyed_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /applications/{stale_slug}/documents returns resume.pdf after rekey."""
        db_path, starting_slug, canonical_slug, _ = self._make_rekeyed_db(tmp_path)
        apps_root = tmp_path / "applications"

        # Create the resume.pdf in the canonical slug's documents/ dir.
        docs_dir = apps_root / canonical_slug / "documents"
        docs_dir.mkdir(parents=True)
        (docs_dir / "resume.pdf").write_bytes(b"%PDF-1.4 stub")

        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda s: apps_root / s,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/api/applications/{starting_slug}/documents")
        assert resp.status_code == 200, resp.text
        assert "resume.pdf" in resp.json()

    def test_review_resolves_rekeyed_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /applications/{stale_slug}/review returns cover letter after rekey."""
        db_path, starting_slug, canonical_slug, _ = self._make_rekeyed_db(tmp_path)
        apps_root = tmp_path / "applications"

        # Create cover-letter-draft.md under the canonical slug directory.
        canonical_dir = apps_root / canonical_slug
        canonical_dir.mkdir(parents=True)
        (canonical_dir / "cover-letter-draft.md").write_text(
            "Dear BECU hiring team,\n", encoding="utf-8"
        )

        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda s: apps_root / s,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/api/applications/{starting_slug}/review")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["cover_letter"] == "Dear BECU hiring team,\n"
        # canonical_slug is returned in the response
        assert data["canonical_slug"] == canonical_slug

    def test_canonical_slug_still_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /applications/{canonical_slug}/documents is unaffected by the fix."""
        db_path, _, canonical_slug, _ = self._make_rekeyed_db(tmp_path)
        apps_root = tmp_path / "applications"

        docs_dir = apps_root / canonical_slug / "documents"
        docs_dir.mkdir(parents=True)
        (docs_dir / "resume.pdf").write_bytes(b"%PDF-1.4 stub")

        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda s: apps_root / s,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/api/applications/{canonical_slug}/documents")
        assert resp.status_code == 200, resp.text
        assert "resume.pdf" in resp.json()

    def test_detail_resolves_rekeyed_slug_via_log_command_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy: apply_state_log payload with '--from X --to Y' still resolves.

        This covers synthetic fixtures and any older pipeline runs that wrote
        the rekey command as a literal string in apply_state_log.
        """
        starting_slug = "widget-corp-engineer-2025"
        canonical_slug = "widget-corp-senior-engineer"
        run_id = "run-legacy-rekey"

        db_path = tmp_path / "jobsmith.db"
        conn = open_pipeline_db(db_path)
        conn.execute(
            "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, canonical_slug, "render", "2025-09-01T10:00:00Z", "2025-09-01T10:15:00Z", "done"),
        )
        conn.execute(
            "INSERT INTO specialist_outputs "
            "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "apply-jd-parser",
                "jd-parsed",
                json.dumps({"company": "Widget Corp", "position": "Senior Engineer"}),
                None,
                "2025-09-01T10:02:00Z",
            ),
        )
        # Legacy shape: literal "--from X --to Y" in apply_state_log payload.
        conn.execute(
            "INSERT INTO apply_state_log (run_id, slug, ts, payload) VALUES (?, ?, ?, ?)",
            (
                run_id,
                canonical_slug,
                "2025-09-01T10:01:30Z",
                json.dumps({
                    "type": "tool_result",
                    "content": (
                        f"jobsmith db rekey-slug --from {starting_slug} --to {canonical_slug}"
                    ),
                }),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr(
            "jobsmith.api.applications._get_app_dir",
            lambda s: tmp_path / "applications" / s,
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.get(f"/api/applications/{starting_slug}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["slug"] == canonical_slug
        assert data["company"] == "Widget Corp"
