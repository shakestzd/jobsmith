"""Read-only /api/applications router for the jobsmith HTTP API.

Endpoints
---------
GET /applications          → list[Application]  (this slice, feat-d08c5002)
GET /applications/{slug}   → Application         (slice 5, feat-e3b75a8a — extend here)

Behavior contract
-----------------
- 200 + empty list when applications_dir exists but contains no slug dirs.
- 200 + empty list when applications_dir does not exist (no error).
- Each entry that is a directory under applications_dir is treated as a slug.
- Non-directory entries are silently skipped.

Adding the detail endpoint (slice 5 / feat-e3b75a8a)
------------------------------------------------------
Import ``derive_application_state`` from ``.state`` and add::

    @router.get("/applications/{slug}", response_model=Application)
    def get_application(slug: str, request: Request) -> Application:
        apps_dir = _resolve_applications_dir(request)
        slug_dir = apps_dir / slug
        if not slug_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Slug not found: {slug}")
        return derive_application_state(slug_dir)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from .schemas.applications import Application
from .state import derive_application_state

router = APIRouter(tags=["applications"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_applications_dir(request: Request) -> Path:
    """Return the applications_dir from app.state, or fall back to config.

    When ``create_app(applications_dir=...)`` is called (e.g. in tests),
    that path is stored in ``app.state.applications_dir``. In production the
    path is derived from the loaded JobsmithConfig stored in ``app.state.config``.
    """
    # Injected directly (tests / explicit override)
    override: Path | None = getattr(request.app.state, "applications_dir", None)
    if override is not None:
        return override

    # Derive from config
    config = getattr(request.app.state, "config", None)
    if config is not None:
        from jobsmith.paths import resolve

        return resolve(config.output.applications_dir)

    # Last resort: use jobsmith default (private/applications/ relative to cwd)
    from jobsmith.config import load_config

    cfg = load_config()
    from jobsmith.paths import resolve

    return resolve(cfg.output.applications_dir)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/applications", response_model=list[Application])
def list_applications(request: Request) -> list[Application]:
    """Return all application records derived from slug directories.

    Walks <applications_dir>/ and calls ``derive_application_state`` for each
    immediate subdirectory. Results are sorted by slug name for stable ordering.
    """
    apps_dir = _resolve_applications_dir(request)
    if not apps_dir.is_dir():
        return []

    results: list[Application] = []
    for entry in sorted(apps_dir.iterdir()):
        if entry.is_dir():
            results.append(derive_application_state(entry))

    return results
