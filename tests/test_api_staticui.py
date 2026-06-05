"""Tests for static-UI serving from create_app (feat-9c980bef).

Coverage:
- TestServeSpaIndex          — GET / → index.html; GET /assets/<hash>.js → 200
- TestSpaFallbackDeepLink    — GET /applications/x → index.html 200
- TestApiAndHealthNotShadowed — GET /api/*, GET /health, GET /docs unaffected;
                                unknown /api/* → 404 not index.html
- TestApiOnlyModeWhenNoDist  — no dist dir → app boots, mount skipped
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dist(tmp_path: Path) -> Path:
    """Create a minimal Vite-style dist directory for testing."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><body>SPA</body></html>", encoding="utf-8"
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('app');", encoding="utf-8")
    return dist


def _make_client(dist: Path | None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient with the full create_app(), pointing the locator at
    *dist* (or None for API-only mode).  Uses monkeypatch so tests are isolated.
    """
    import jobsmith.api.staticui as staticui_mod

    monkeypatch.setattr(staticui_mod, "find_web_dist", lambda: dist)

    # Re-import create_app after the monkeypatch so it picks up the patched locator.
    from jobsmith.api.main import create_app

    app = create_app()
    # Disable lifespan (startup events hit real filesystem / DB).
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# TestServeSpaIndex
# ---------------------------------------------------------------------------


class TestServeSpaIndex:
    def test_root_returns_spa_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET / returns SPA index.html with 200."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "SPA" in resp.text

    def test_assets_hash_file_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /assets/<hash>.js returns 200 with the file content."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/assets/index-abc123.js")
        assert resp.status_code == 200
        assert "console.log" in resp.text

    def test_csp_allows_blob_frames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET / returns CSP with frame-src and object-src permitting blob: URLs.

        This allows resume.pdf (framed via blob: URL in documents tab) to render
        without CSP violation (bug-7a244253).
        """
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/")
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "frame-src 'self' blob:" in csp
        assert "object-src 'self' blob:" in csp
        # Ensure frame-ancestors 'none' is still present (no loosening).
        assert "frame-ancestors 'none'" in csp


# ---------------------------------------------------------------------------
# TestSpaFallbackDeepLink
# ---------------------------------------------------------------------------


class TestSpaFallbackDeepLink:
    def test_client_deep_link_returns_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /applications/some-slug falls back to index.html with 200."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/applications/some-slug")
        assert resp.status_code == 200
        assert "SPA" in resp.text

    def test_arbitrary_deep_link_returns_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /settings/profile falls back to index.html with 200."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/settings/profile")
        assert resp.status_code == 200
        assert "SPA" in resp.text


# ---------------------------------------------------------------------------
# TestApiAndHealthNotShadowed
# ---------------------------------------------------------------------------


class TestApiAndHealthNotShadowed:
    def test_health_not_shadowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /health returns JSON {status: ok}, not index.html."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_docs_not_shadowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /docs returns 200 with OpenAPI HTML, not the SPA."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/docs")
        assert resp.status_code == 200
        # FastAPI /docs returns HTML containing swagger-ui
        assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()

    def test_unknown_api_path_returns_404_not_spa(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /api/nonexistent returns 404 — NOT index.html (no shadowing)."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/api/nonexistent-endpoint")
        assert resp.status_code == 404
        # Must not be the SPA fallback
        assert "SPA" not in resp.text

    def test_openapi_json_not_shadowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /openapi.json returns 200 JSON schema."""
        dist = _make_dist(tmp_path)
        client = _make_client(dist, monkeypatch)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "openapi" in data


# ---------------------------------------------------------------------------
# TestApiOnlyModeWhenNoDist
# ---------------------------------------------------------------------------


class TestApiOnlyModeWhenNoDist:
    def test_app_boots_when_no_dist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When find_web_dist() returns None, the app boots without crashing."""
        import jobsmith.api.staticui as staticui_mod

        monkeypatch.setattr(staticui_mod, "find_web_dist", lambda: None)
        from jobsmith.api.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_root_not_found_in_api_only_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In API-only mode (no dist), GET / returns 404 (no SPA mounted)."""
        import jobsmith.api.staticui as staticui_mod

        monkeypatch.setattr(staticui_mod, "find_web_dist", lambda: None)
        from jobsmith.api.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/")
        assert resp.status_code == 404
