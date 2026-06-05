"""Web-dist locator and static-UI mount helper for jobsmith API (feat-9c980bef).

Precedence (first found wins):
  1. Bundled package path: <package_root>/web_dist/  — populated by slice-2 of
     plan-72ad5ccc when the wheel bundle is built.  May not exist in source installs.
  2. Repo-root sibling: web/dist/  — present for local ``pip install -e .`` /
     source checkouts where ``npm run build`` has been run.

If neither path exists the locator returns None and the caller should skip the
static mount entirely (API-only mode).  This is intentional: a missing UI must
never crash the server.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

_log = logging.getLogger(__name__)


def find_web_dist() -> Path | None:
    """Return the path to the built web distribution, or None if absent.

    The search order is documented in the module docstring.
    """
    # 1. Bundled package path (populated by wheel-bundle slice).
    package_root = Path(__file__).parent.parent  # src/jobsmith/
    bundled = package_root / "web_dist"
    if bundled.is_dir() and (bundled / "index.html").exists():
        return bundled

    # 2. Repo-root sibling: <repo>/web/dist/
    repo_root = package_root.parent.parent  # src/
    repo_web_dist = repo_root / "web" / "dist"
    if repo_web_dist.is_dir() and (repo_web_dist / "index.html").exists():
        return repo_web_dist

    return None


# Paths that must NOT be served by the SPA catch-all.  Any request whose path
# starts with one of these prefixes (or equals it exactly) is left to FastAPI's
# own routing — either a registered handler or a 404.
_EXCLUDED_PREFIXES = frozenset(
    [
        "/api/",
        "/assets/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health",
    ]
)


def _is_api_or_system_path(path: str) -> bool:
    """Return True if *path* should NOT fall back to the SPA."""
    for prefix in _EXCLUDED_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def mount_static_ui(app: FastAPI) -> None:
    """Mount StaticFiles + SPA catch-all onto *app*.

    Call this AFTER all API routers have been registered so that API routes
    take precedence.  If no web-dist directory is found, logs a one-line hint
    and returns without mounting (API-only mode — no crash).
    """
    dist = find_web_dist()

    if dist is None:
        _log.info(
            "jobsmith: no web-dist found; running in API-only mode. "
            "Run `npm run build` in the web/ directory or install the full wheel."
        )
        return

    _log.info("jobsmith: serving UI from %s", dist)

    # Hashed /assets/* — immutable, long-lived cache.
    app.mount(
        "/assets",
        StaticFiles(directory=str(dist / "assets")),
        name="ui_assets",
    )

    index_html = dist / "index.html"

    # SPA catch-all: serve index.html for any non-API, non-asset path.
    # Registered LAST so API routes and /assets always win.
    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa_fallback(request: Request, full_path: str) -> HTMLResponse:
        path = request.url.path

        # Delegate excluded paths back to FastAPI's 404 handling.
        if _is_api_or_system_path(path):
            from fastapi.responses import Response

            return Response(status_code=404)  # type: ignore[return-value]

        # Serve index.html with no-cache so the SPA shell is never pinned stale.
        content = index_html.read_bytes()
        return HTMLResponse(
            content=content,
            status_code=200,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
