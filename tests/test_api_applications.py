"""Tests for GET /api/applications listing endpoint.

TDD — written before implementation. Tests use FastAPI TestClient with tmp_path
fixtures that replicate real artifact layouts under a temporary applications_dir.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(applications_dir: Path) -> TestClient:
    """Construct a TestClient with applications_dir injected."""
    from jobsmith.api.main import create_app

    app = create_app(applications_dir=applications_dir)
    return TestClient(app)


def _slug_dir(applications_dir: Path, slug: str) -> Path:
    d = applications_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_dir(slug_dir: Path) -> Path:
    d = slug_dir / ".apply-state"
    d.mkdir(exist_ok=True)
    return d


def _write_jd_parsed(state_dir: Path, role: str = "Engineer", company: str = "Acme") -> None:
    (state_dir / "jd-parsed.json").write_text(
        json.dumps({"position": role, "company": company})
    )


# ---------------------------------------------------------------------------
# Phase / Status tests
# ---------------------------------------------------------------------------


def test_phase_zero_queued(tmp_path: Path) -> None:
    """Empty slug dir → phase 0, status queued."""
    apps_dir = tmp_path / "applications"
    _slug_dir(apps_dir, "empty-co-engineer")

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    app = data[0]
    assert app["slug"] == "empty-co-engineer"
    assert app["phase"] == 0
    assert app["status"] == "queued"


def test_phase_one_gather_done(tmp_path: Path) -> None:
    """Only .apply-state/jd-parsed.json → phase 1, status gather."""
    apps_dir = tmp_path / "applications"
    sd = _state_dir(_slug_dir(apps_dir, "acme-swe"))
    _write_jd_parsed(sd)

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    assert app["phase"] == 1
    assert app["status"] == "gather"


def test_phase_two_draft_running(tmp_path: Path) -> None:
    """jd-parsed + prose-draft present, no cover-letter-draft → phase 2, status draft."""
    apps_dir = tmp_path / "applications"
    slug_d = _slug_dir(apps_dir, "beta-pm")
    sd = _state_dir(slug_d)
    _write_jd_parsed(sd)
    (sd / "prose-draft.md").write_text("Draft content here")

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    assert app["phase"] == 2
    assert app["status"] == "draft"


def test_phase_three_review(tmp_path: Path) -> None:
    """Both drafts + _quarto.yml, no rendered pdf → phase 3, status review."""
    apps_dir = tmp_path / "applications"
    slug_d = _slug_dir(apps_dir, "gamma-eng")
    sd = _state_dir(slug_d)
    _write_jd_parsed(sd)
    (sd / "prose-draft.md").write_text("prose")
    (slug_d / "cover-letter-draft.md").write_text("cover")
    (slug_d / "_quarto.yml").write_text("project:\n  type: default\n")

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    assert app["phase"] == 3
    assert app["status"] == "review"


def test_phase_three_rendered(tmp_path: Path) -> None:
    """Phase 3 + rendered/<slug>/*.pdf → status rendered."""
    apps_dir = tmp_path / "applications"
    slug = "delta-data"
    slug_d = _slug_dir(apps_dir, slug)
    sd = _state_dir(slug_d)
    _write_jd_parsed(sd)
    (sd / "prose-draft.md").write_text("prose")
    (slug_d / "cover-letter-draft.md").write_text("cover")
    (slug_d / "_quarto.yml").write_text("project:\n  type: default\n")
    rendered_dir = slug_d / "rendered" / slug
    rendered_dir.mkdir(parents=True)
    (rendered_dir / "resume.pdf").write_bytes(b"%PDF-1.4")

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    assert app["phase"] == 3
    assert app["status"] == "rendered"


def test_role_company_extracted_from_jd_parsed(tmp_path: Path) -> None:
    """role and company are read from .apply-state/jd-parsed.json."""
    apps_dir = tmp_path / "applications"
    sd = _state_dir(_slug_dir(apps_dir, "megacorp-cto"))
    _write_jd_parsed(sd, role="Chief Technology Officer", company="MegaCorp")

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    assert app["role"] == "Chief Technology Officer"
    assert app["company"] == "MegaCorp"


def test_missing_jd_parsed_role_company_none(tmp_path: Path) -> None:
    """No jd-parsed.json → role and company are null."""
    apps_dir = tmp_path / "applications"
    _slug_dir(apps_dir, "orphan-co-job")
    # No .apply-state created at all

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    assert app["role"] is None
    assert app["company"] is None


def test_renders_lists_pdfs(tmp_path: Path) -> None:
    """rendered/<slug>/ with multiple pdfs → all listed in renders field."""
    apps_dir = tmp_path / "applications"
    slug = "epsilon-analytics"
    slug_d = _slug_dir(apps_dir, slug)
    sd = _state_dir(slug_d)
    _write_jd_parsed(sd)
    (sd / "prose-draft.md").write_text("prose")
    (slug_d / "cover-letter-draft.md").write_text("cover")
    (slug_d / "_quarto.yml").write_text("project:\n  type: default\n")
    rendered_dir = slug_d / "rendered" / slug
    rendered_dir.mkdir(parents=True)
    (rendered_dir / "resume.pdf").write_bytes(b"%PDF")
    (rendered_dir / "cover-letter.pdf").write_bytes(b"%PDF")
    (rendered_dir / "not-a-pdf.html").write_text("<html/>")

    client = _make_app(apps_dir)
    resp = client.get("/api/applications")

    assert resp.status_code == 200
    app = resp.json()[0]
    renders = sorted(app["renders"])
    assert renders == ["cover-letter.pdf", "resume.pdf"]
