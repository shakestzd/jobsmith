"""Tests for POST /api/applications/{slug}/cover-letter/apply (feat-fae0fda6).

Covers the propose -> diff -> apply mechanism's server-side apply contract:
- a passing draft (every hard claim verifiable against master content) is
  written to disk and returns ``applied: true``;
- a draft with a fabricated claim is rejected with HTTP 422,
  ``reason: "fact_check_failed"``, and is NOT written.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.applications import router as applications_router


@pytest.fixture()
def apply_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path, str]:
    """TestClient with app dir + master content dir stubbed into tmp_path."""
    slug = "acme-swe-2025"
    app_dir = tmp_path / "applications" / slug
    app_dir.mkdir(parents=True)
    cl_path = app_dir / "cover-letter-draft.md"
    cl_path.write_text("Original cover letter.\n", encoding="utf-8")

    # Master content the fact-checker verifies hard claims against.
    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / "work.yml").write_text(
        "roles:\n"
        "  - company: Globex\n"
        "    impact: Increased revenue by 42% over 3 years.\n",
        encoding="utf-8",
    )

    import jobsmith.api.applications as appmod

    monkeypatch.setattr(appmod, "_resolve_cover_letter", lambda s, conn: cl_path)
    monkeypatch.setattr(appmod, "_get_app_dir", lambda s: app_dir)
    monkeypatch.setattr(appmod, "_content_dir_for_slug", lambda s: content_dir)
    monkeypatch.setattr(appmod, "_get_db_path", lambda: tmp_path / "jobsmith.db")
    monkeypatch.setattr(appmod, "_open_conn", lambda p: _DummyConn())
    # No master DB / JD extra sources in the test sandbox.
    monkeypatch.setattr(
        "jobsmith.factcheck.load_db_master_content", lambda: {}
    )
    monkeypatch.setattr(
        "jobsmith.factcheck.load_jd_context_for_draft", lambda p: {}
    )

    app = FastAPI()
    app.include_router(applications_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=True), cl_path, slug


class _DummyConn:
    def close(self) -> None:  # pragma: no cover - trivial
        pass


class TestApplyCoverLetter:
    def test_passing_draft_is_applied_and_written(
        self, apply_client: tuple[TestClient, Path, str]
    ) -> None:
        client, cl_path, slug = apply_client
        # Claims here are all verifiable against the stubbed work.yml.
        new_content = (
            "At Globex I increased revenue by 42% over 3 years.\n"
        )
        resp = client.post(
            f"/api/applications/{slug}/cover-letter/apply",
            json={"new_content": new_content},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["applied"] is True
        assert data["words"] > 0
        assert "render" in data
        # File was actually written.
        assert cl_path.read_text(encoding="utf-8") == new_content

    def test_fabricated_claim_is_rejected_and_not_written(
        self, apply_client: tuple[TestClient, Path, str]
    ) -> None:
        client, cl_path, slug = apply_client
        original = cl_path.read_text(encoding="utf-8")
        # 99% is NOT in master content -> fabricated hard claim.
        new_content = (
            "At Globex I boosted conversion by 99% in a single quarter.\n"
        )
        resp = client.post(
            f"/api/applications/{slug}/cover-letter/apply",
            json={"new_content": new_content},
        )
        assert resp.status_code == 422, resp.text
        data = resp.json()
        assert data["applied"] is False
        assert data["reason"] == "fact_check_failed"
        assert any("99%" in c for c in data["failed_claims"])
        # File must be unchanged.
        assert cl_path.read_text(encoding="utf-8") == original

    def test_empty_content_rejected(
        self, apply_client: tuple[TestClient, Path, str]
    ) -> None:
        client, _cl_path, slug = apply_client
        resp = client.post(
            f"/api/applications/{slug}/cover-letter/apply",
            json={"new_content": "   "},
        )
        assert resp.status_code == 422
