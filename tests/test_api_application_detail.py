"""Tests for GET /api/applications/{slug} detail + raw endpoints.

TDD — written before implementation (Step 1). Tests use FastAPI TestClient
with tmp_path fixtures that replicate real artifact layouts.

9 tests total:
  1. test_detail_404_for_missing_slug
  2. test_detail_returns_full_payload
  3. test_detail_artifacts_tree_lists_files
  4. test_detail_truncates_large_prose_draft
  5. test_detail_handles_missing_artifacts
  6. test_raw_endpoint_returns_file_content
  7. test_raw_endpoint_rejects_path_traversal
  8. test_raw_endpoint_rejects_disallowed_filename
  9. test_raw_endpoint_404_for_missing_file
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


def _build_full_fixture(tmp_path: Path, slug: str = "acme-engineer") -> tuple[Path, Path, Path]:
    """Build a slug dir with all artifacts present. Returns (apps_dir, slug_dir, state_dir)."""
    apps_dir = tmp_path / "applications"
    slug_d = _slug_dir(apps_dir, slug)
    sd = _state_dir(slug_d)

    # Phase 3 artifacts
    _write_jd_parsed(sd, role="Software Engineer", company="Acme")
    (sd / "prose-draft.md").write_text("# Prose Draft\n\nThis is my application.")
    (slug_d / "cover-letter-draft.md").write_text("# Cover Letter\n\nDear Hiring Manager,")
    (slug_d / "_quarto.yml").write_text("project:\n  type: default\n")
    (slug_d / "_variables.yml").write_text("name: Jane Doe\nemail: jane@example.com\n")

    # .apply-state JSON files
    (sd / "fact_check.json").write_text(json.dumps({"flagged": 0, "items": []}))
    (sd / "anchor_check.json").write_text(json.dumps({"pass": 3, "total": 3, "items": []}))
    (sd / "bullet_selection.json").write_text(json.dumps({"bullets": ["bullet1", "bullet2"]}))

    # rendered PDF
    rendered_dir = slug_d / "rendered" / slug
    rendered_dir.mkdir(parents=True)
    (rendered_dir / "resume.pdf").write_bytes(b"%PDF-1.4")

    # .apply-config.yaml (minimal — only output + render sections)
    (slug_d / ".apply-config.yaml").write_text(
        "output:\n  applications_dir: private/applications\n"
        "render:\n  format: pdf\n"
    )

    return apps_dir, slug_d, sd


# ---------------------------------------------------------------------------
# 1. test_detail_404_for_missing_slug
# ---------------------------------------------------------------------------


def test_detail_404_for_missing_slug(tmp_path: Path) -> None:
    """GET /api/applications/nonexistent → 404."""
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    client = _make_app(apps_dir)

    resp = client.get("/api/applications/nonexistent-slug")

    assert resp.status_code == 404
    assert "nonexistent-slug" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. test_detail_returns_full_payload
# ---------------------------------------------------------------------------


def test_detail_returns_full_payload(tmp_path: Path) -> None:
    """Full fixture → all ApplicationDetail fields populated correctly."""
    slug = "acme-engineer"
    apps_dir, slug_d, sd = _build_full_fixture(tmp_path, slug)
    client = _make_app(apps_dir)

    resp = client.get(f"/api/applications/{slug}")

    assert resp.status_code == 200
    data = resp.json()

    # Base Application fields
    assert data["slug"] == slug
    assert data["role"] == "Software Engineer"
    assert data["company"] == "Acme"
    assert data["phase"] == 3
    assert data["status"] == "rendered"

    # ApplicationDetail-specific fields
    assert data["spec"] is not None
    assert data["spec"]["position"] == "Software Engineer"

    assert data["prose_draft"] is not None
    assert "Prose Draft" in data["prose_draft"]

    assert data["cover_letter_draft"] is not None
    assert "Cover Letter" in data["cover_letter_draft"]

    assert data["fact_check"] is not None
    assert data["fact_check"]["flagged"] == 0

    assert data["anchor_check"] is not None
    assert data["anchor_check"]["pass"] == 3

    assert data["bullet_selection"] is not None
    assert "bullets" in data["bullet_selection"]

    assert data["variables"] is not None
    assert data["variables"]["name"] == "Jane Doe"

    assert data["config"] is not None

    assert data["truncated"] is False

    assert "artifacts" in data


# ---------------------------------------------------------------------------
# 3. test_detail_artifacts_tree_lists_files
# ---------------------------------------------------------------------------


def test_detail_artifacts_tree_lists_files(tmp_path: Path) -> None:
    """ArtifactTree contains nodes with correct shape (name, path, size, mtime)."""
    slug = "beta-corp"
    apps_dir, slug_d, sd = _build_full_fixture(tmp_path, slug)
    client = _make_app(apps_dir)

    resp = client.get(f"/api/applications/{slug}")

    assert resp.status_code == 200
    data = resp.json()
    artifacts = data["artifacts"]

    assert "apply_state" in artifacts
    assert "rendered" in artifacts

    # apply_state should have entries (at minimum jd-parsed.json, prose-draft.md, etc.)
    assert len(artifacts["apply_state"]) > 0

    # Each node must have the required shape
    for node in artifacts["apply_state"]:
        assert "name" in node
        assert "path" in node
        assert "size" in node
        assert "mtime" in node
        assert isinstance(node["size"], int)
        # mtime must be an ISO 8601 UTC string
        assert "T" in node["mtime"]

    # rendered should have one entry: resume.pdf
    assert len(artifacts["rendered"]) == 1
    rendered_node = artifacts["rendered"][0]
    assert rendered_node["name"] == "resume.pdf"


# ---------------------------------------------------------------------------
# 4. test_detail_truncates_large_prose_draft
# ---------------------------------------------------------------------------


def test_detail_truncates_large_prose_draft(tmp_path: Path) -> None:
    """prose_draft > 256 KB → returned as first 64 KB, truncated=True."""
    slug = "large-prose"
    apps_dir = tmp_path / "applications"
    slug_d = _slug_dir(apps_dir, slug)
    sd = _state_dir(slug_d)

    _write_jd_parsed(sd)

    # Write a prose draft larger than 256 KB (256 * 1024 = 262144 bytes)
    big_prose = "A" * (256 * 1024 + 1000)
    (sd / "prose-draft.md").write_text(big_prose)

    client = _make_app(apps_dir)
    resp = client.get(f"/api/applications/{slug}")

    assert resp.status_code == 200
    data = resp.json()

    assert data["truncated"] is True
    # Content should be at most 64 KB
    assert len(data["prose_draft"].encode("utf-8")) <= 64 * 1024


# ---------------------------------------------------------------------------
# 5. test_detail_handles_missing_artifacts
# ---------------------------------------------------------------------------


def test_detail_handles_missing_artifacts(tmp_path: Path) -> None:
    """Fixture with only jd-parsed.json → optional fields are None."""
    slug = "minimal-slug"
    apps_dir = tmp_path / "applications"
    slug_d = _slug_dir(apps_dir, slug)
    sd = _state_dir(slug_d)

    _write_jd_parsed(sd)
    # No prose-draft, no cover-letter, no fact_check, etc.

    client = _make_app(apps_dir)
    resp = client.get(f"/api/applications/{slug}")

    assert resp.status_code == 200
    data = resp.json()

    assert data["prose_draft"] is None
    assert data["cover_letter_draft"] is None
    assert data["fact_check"] is None
    assert data["anchor_check"] is None
    assert data["bullet_selection"] is None
    assert data["variables"] is None
    assert data["truncated"] is False

    # spec should be populated since jd-parsed.json exists
    assert data["spec"] is not None


# ---------------------------------------------------------------------------
# 6. test_raw_endpoint_returns_file_content
# ---------------------------------------------------------------------------


def test_raw_endpoint_returns_file_content(tmp_path: Path) -> None:
    """GET /api/applications/{slug}/raw/prose-draft.md returns file content."""
    slug = "raw-test"
    apps_dir = tmp_path / "applications"
    slug_d = _slug_dir(apps_dir, slug)
    sd = _state_dir(slug_d)

    _write_jd_parsed(sd)
    prose_content = "# My Application\n\nThis is the full prose draft content."
    (sd / "prose-draft.md").write_text(prose_content)

    client = _make_app(apps_dir)
    resp = client.get(f"/api/applications/{slug}/raw/prose-draft.md")

    assert resp.status_code == 200
    assert prose_content in resp.text
    assert "text/plain" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# 7. test_raw_endpoint_rejects_path_traversal
# ---------------------------------------------------------------------------


def test_raw_endpoint_rejects_path_traversal(tmp_path: Path) -> None:
    """Path traversal filenames are rejected with 400.

    The filename regex ``^[A-Za-z0-9._-]+$`` blocks any filename that contains
    characters outside the safe set (e.g. ``%``, ``/``, backslash, ``:``, etc.).

    A URL-encoded slash ``%2F`` in the path is treated by httpx/FastAPI as a
    real path separator and does not reach this endpoint at all (404 from
    router — equally safe). We test at the application level by sending a
    filename that contains a percent-encoded ``%`` sign (``%25``), which decodes
    to ``%`` and therefore fails the regex.
    """
    apps_dir = tmp_path / "applications"
    slug = "secure-slug"
    _slug_dir(apps_dir, slug)

    client = _make_app(apps_dir)
    # '..%25passwd' url-decodes to '..%passwd' which contains '%' → fails regex
    resp = client.get(f"/api/applications/{slug}/raw/..%25passwd")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 8. test_raw_endpoint_rejects_disallowed_filename
# ---------------------------------------------------------------------------


def test_raw_endpoint_rejects_disallowed_filename(tmp_path: Path) -> None:
    """GET /api/applications/{slug}/raw/secret.env → 400."""
    apps_dir = tmp_path / "applications"
    slug = "secure-slug2"
    _slug_dir(apps_dir, slug)

    client = _make_app(apps_dir)
    resp = client.get(f"/api/applications/{slug}/raw/secret.env")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 9. test_raw_endpoint_404_for_missing_file
# ---------------------------------------------------------------------------


def test_raw_endpoint_404_for_missing_file(tmp_path: Path) -> None:
    """Allowed filename but file doesn't exist → 404."""
    apps_dir = tmp_path / "applications"
    slug = "no-file-slug"
    _slug_dir(apps_dir, slug)

    client = _make_app(apps_dir)
    # prose-draft.md is allowlisted but not present
    resp = client.get(f"/api/applications/{slug}/raw/prose-draft.md")

    assert resp.status_code == 404
