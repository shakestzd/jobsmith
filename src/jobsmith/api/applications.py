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

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from .schemas.applications import Application, ApplicationDetail
from .state import derive_application_detail, derive_application_state

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


@router.get("/applications/{slug}", response_model=ApplicationDetail)
def get_application(slug: str, request: Request) -> ApplicationDetail:
    """Return a rich ApplicationDetail for a single slug.

    Includes artifact tree, parsed JSON/YAML state files, prose drafts
    (size-guarded to 64 KB), and a safe config subset.
    """
    apps_dir = _resolve_applications_dir(request)
    slug_dir = apps_dir / slug
    if not slug_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Slug not found: {slug}")
    return derive_application_detail(slug_dir)


# ---------------------------------------------------------------------------
# Allowlist for /raw/{filename} endpoint
# ---------------------------------------------------------------------------

_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

_RAW_ALLOWLIST = frozenset(
    {
        "prose-draft.md",
        "cover-letter-draft.md",
        "_quarto.yml",
        "_variables.yml",
        "jd-parsed.json",
        "fact_check.json",
        "anchor_check.json",
        "bullet_selection.json",
    }
)

_CONTENT_TYPE_MAP = {
    ".md": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".json": "application/json",
}

# Files that live under .apply-state/ rather than slug root
_STATE_DIR_FILES = frozenset(
    {
        "prose-draft.md",
        "jd-parsed.json",
        "fact_check.json",
        "anchor_check.json",
        "bullet_selection.json",
    }
)


@router.get("/applications/{slug}/raw/{filename}")
def get_application_raw(slug: str, filename: str, request: Request) -> Response:
    """Return the raw content of an allowed file within the slug directory.

    Guards
    ------
    - filename must match ``^[A-Za-z0-9._-]+$`` (400 if not).
    - filename must be in the allowlist (400 if not).
    - resolved path must be under slug_dir (400 if path traversal detected).
    - File must exist (404 if missing).

    Returns ``text/plain`` for .md/.yml and ``application/json`` for .json.
    """
    # Guard 1: safe filename characters
    if not _FILENAME_PATTERN.match(filename):
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename!r}")

    # Guard 2: allowlist check
    if filename not in _RAW_ALLOWLIST:
        raise HTTPException(status_code=400, detail=f"File not allowed: {filename!r}")

    apps_dir = _resolve_applications_dir(request)
    slug_dir = apps_dir / slug
    if not slug_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Slug not found: {slug}")

    # Determine candidate path (some files live in .apply-state/)
    if filename in _STATE_DIR_FILES:
        candidate = slug_dir / ".apply-state" / filename
    else:
        candidate = slug_dir / filename

    # Guard 3: path traversal check
    try:
        resolved = candidate.resolve()
        slug_resolved = slug_dir.resolve()
        if not str(resolved).startswith(str(slug_resolved)):
            raise HTTPException(status_code=400, detail="Path traversal detected")
    except OSError:
        raise HTTPException(status_code=400, detail="Cannot resolve path")

    # Guard 4: file existence
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    suffix = candidate.suffix.lower()
    content_type = _CONTENT_TYPE_MAP.get(suffix, "text/plain; charset=utf-8")
    content = candidate.read_bytes()
    return Response(content=content, media_type=content_type)
