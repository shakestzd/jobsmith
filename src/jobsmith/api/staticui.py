"""Web-dist locator and static-UI mount helper for jobsmith API (feat-9c980bef).

Precedence (first found wins):
  1. Bundled package path: <package_root>/web_dist/  — populated by slice-2 of
     plan-72ad5ccc when the wheel bundle is built.  May not exist in source installs.
  2. Repo-root sibling: web/dist/  — present for local ``pip install -e .`` /
     source checkouts where ``npm run build`` has been run.

If neither path exists the locator returns None and the caller should skip the
static mount entirely (API-only mode).  This is intentional: a missing UI must
never crash the server.

Localhost auto-auth (feat-16257e94 / slice-3 of plan-72ad5ccc):
When the API serves index.html on a LOCALHOST bind (127.0.0.1, ::1, or the
hostname "localhost"), injects a window.__JOBSMITH__ runtime shim with the
resolved static bearer token so the SPA authenticates immediately without
requiring the user to configure a token.  On a public bind the shim is NOT
injected; explicit auth is required as before.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

_log = logging.getLogger(__name__)

# Restrictive CSP for the served index.html.  Allows only same-origin scripts
# (the hashed /assets/* bundle) and same-origin connections so the injected
# inline shim is the only non-bundle JS surface.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'"
)

# Hostnames / IP literals that qualify as localhost.
_LOCALHOST_NAMES = frozenset(["localhost", "127.0.0.1", "::1", "[::1]"])


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


def _is_localhost_request(request: Request) -> bool:
    """Return True if *request* originates from a localhost bind.

    Checks the Host header hostname (stripping any port).  Returns True for
    127.0.0.1, ::1 (IPv6 loopback), and the hostname "localhost".
    """
    host_header = request.headers.get("host", "")
    # Strip port: "127.0.0.1:8000" → "127.0.0.1", "[::1]:8000" → "[::1]"
    if host_header.startswith("["):
        # IPv6 literal: "[::1]:port" or "[::1]"
        bracket_end = host_header.find("]")
        host = host_header[: bracket_end + 1] if bracket_end != -1 else host_header
    elif ":" in host_header:
        host = host_header.rsplit(":", 1)[0]
    else:
        host = host_header
    return host.lower() in _LOCALHOST_NAMES


def _build_shim_script(token: str, api_base: str) -> str:
    """Return an HTML ``<script>`` tag that assigns window.__JOBSMITH__.

    The token and apiBase are JSON-encoded and HTML-escaped so that any
    HTML-special characters in the token cannot break out of the script
    context (defense-in-depth on top of localhost-only injection).
    """
    payload = html.escape(json.dumps({"token": token, "apiBase": api_base}))
    return f"<script>window.__JOBSMITH__ = {payload};</script>"


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

        # Read the SPA shell.
        content = index_html.read_text(encoding="utf-8")

        # Localhost auto-auth (feat-16257e94): inject the runtime token shim
        # ONLY when the request comes from a localhost bind.  On a public bind
        # no shim is emitted; callers must supply explicit auth.
        if _is_localhost_request(request):
            from jobsmith.api.auth import _get_expected_token  # noqa: PLC0415

            token = _get_expected_token()
            api_base = str(request.base_url).rstrip("/")
            shim = _build_shim_script(token, api_base)
            # Inject right before </head> so the token is available to the
            # bundle's top-level module code.
            content = content.replace("</head>", f"{shim}</head>", 1)

        resp_headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Security-Policy": _CSP,
        }
        return HTMLResponse(
            content=content,
            status_code=200,
            headers=resp_headers,
        )
