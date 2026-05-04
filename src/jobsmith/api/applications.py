"""Read-only /api/applications router for the jobsmith HTTP API.

Endpoints
---------
GET /applications          → list[Application]  (this slice, feat-d08c5002)
GET /applications/{slug}   → Application         (slice 5, feat-e3b75a8a — extend here)
POST /applications         → CreateApplicationResponse  (feat-4d9cc3e5)

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

import base64
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from .schemas.applications import (
    Application,
    ApplicationDetail,
    CreateApplicationRequest,
    CreateApplicationResponse,
    RerunConflictResponse,
    RerunRequest,
    RerunResponse,
)
from .state import derive_application_detail, derive_application_state

router = APIRouter(tags=["applications"])


# ---------------------------------------------------------------------------
# Supervisor lazy-import helper (feat-9b3cfcfd)
#
# Using a callable indirection lets tests monkeypatch ``_supervisor`` at the
# module level without needing to import the real RunSupervisor at test time.
# ---------------------------------------------------------------------------


def _supervisor():  # type: ignore[return]
    """Return the process-wide RunSupervisor singleton (lazy import)."""
    from jobsmith.api.supervisor import get_supervisor

    return get_supervisor()


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
# Create endpoint (feat-4d9cc3e5)
# POST /api/applications
# ---------------------------------------------------------------------------


@router.post("/applications", response_model=CreateApplicationResponse, status_code=201)
async def create_application(
    body: CreateApplicationRequest,
    request: Request,
) -> CreateApplicationResponse:
    """Create a new application slug directory and queue a pipeline run.

    Exactly one of jd_url, jd_text, or jd_file_b64 must be provided.
    Returns 201 with slug, run_id, and events_url on success.
    Returns 400 for bad input, 409 if the slug already exists.
    """
    # --- Validate exactly one source is set ---
    sources = [body.jd_url, body.jd_text, body.jd_file_b64]
    set_count = sum(1 for s in sources if s is not None)
    if set_count == 0:
        raise HTTPException(
            status_code=400,
            detail="Exactly one of jd_url, jd_text, or jd_file_b64 must be set.",
        )
    if set_count > 1:
        raise HTTPException(
            status_code=400,
            detail="Exactly one of jd_url, jd_text, or jd_file_b64 must be set (got multiple).",
        )

    # --- Validate and decode base64 if provided ---
    jd_content: str | None = None
    if body.jd_file_b64 is not None:
        try:
            jd_content = base64.b64decode(body.jd_file_b64).decode("utf-8")
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"jd_file_b64 is not valid base64-encoded UTF-8 text: {exc}",
            ) from exc
    elif body.jd_text is not None:
        jd_content = body.jd_text

    # --- Resolve applications_dir ---
    apps_dir = _resolve_applications_dir(request)

    # --- Derive slug ---
    if body.jd_url is not None:
        from jobsmith.apply import derive_slug  # noqa: PLC0415

        slug = derive_slug(body.jd_url)
    else:
        # For pasted text / file upload, use a timestamp-based slug.
        slug = f"pasted-{datetime.now(tz=timezone.utc).strftime('%Y%m%d%H%M%S')}"

    # --- 409 if slug directory already exists ---
    slug_dir = apps_dir / slug
    if slug_dir.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Application slug already exists: {slug!r}",
        )

    # --- Create slug directory ---
    slug_dir.mkdir(parents=True, exist_ok=False)

    # --- Write jd.txt if content came from text/file ---
    jd_file: Path | None = None
    if jd_content is not None:
        jd_file = slug_dir / "jd.txt"
        jd_file.write_text(jd_content, encoding="utf-8")

    # --- Build argv for apply pipeline ---
    argv: list[str] = ["jobsmith", "apply"]
    if body.jd_url is not None:
        argv.append(body.jd_url)
    else:
        # cli.py:411 defines `url` as a required positional Argument. The CLI
        # re-derives its own slug from this positional via apply.derive_slug(),
        # which uses the URL's last path segment. To keep the CLI's slug
        # aligned with the slug we just created and returned to the client,
        # pass a synthetic URL whose final path segment IS that slug. (When
        # cli.py grows a --slug override flag, drop this hack.)
        argv.append(f"https://local.jobsmith/{slug}")
        argv += ["--jd-text-file", str(jd_file)]

    if body.skip_confirmations:
        argv.append("--yes")
    if body.force:
        argv.append("--force")
    if body.verbosity == "-vv":
        argv.append("-vv")
    elif body.verbosity == "-vvv":
        argv.append("-vvv")
    else:
        argv.append("-v")

    # --- Launch via supervisor ---
    supervisor = _supervisor()
    run_id = await supervisor.start(slug, argv, cwd=apps_dir.parent)

    events_url = f"/api/applications/{slug}/events?run_id={run_id}"

    return CreateApplicationResponse(slug=slug, run_id=run_id, events_url=events_url)


# ---------------------------------------------------------------------------
# Re-run endpoint (feat-9b3cfcfd)
# POST /api/applications/{slug}/run
# ---------------------------------------------------------------------------

# Placeholder URL used when re-running a text-based (jd.txt) application.
# The apply command requires a positional URL argument; for text-based runs
# a sentinel value is supplied and the real text is passed via --jd-text-file.
_JD_URL_PLACEHOLDER = "file://placeholder"


def _read_jd_url(slug_dir: Path) -> str | None:
    """Extract the original JD URL from .apply-state/jd-parsed.json.

    apply-jd-parser writes the URL under ``apply_url``. Try ``jd_url`` and
    ``source_url`` too for older runs.
    """
    jd_parsed = slug_dir / ".apply-state" / "jd-parsed.json"
    if jd_parsed.is_file():
        try:
            import json

            data = json.loads(jd_parsed.read_text(encoding="utf-8"))
            return data.get("apply_url") or data.get("jd_url") or data.get("source_url")
        except Exception:
            return None
    return None


@router.post(
    "/applications/{slug}/run",
    status_code=202,
    response_model=RerunResponse,
)
async def rerun_application(
    slug: str,
    body: RerunRequest,
    request: Request,
) -> RerunResponse:
    """Re-run the apply pipeline for an existing slug.

    Steps
    -----
    1. Validate slug (404 if not a directory under applications_dir).
    2. Determine JD source from .apply-state/jd-parsed.json or jd.txt (400 if neither).
    3. Check for an in-flight run (409 if already running).
    4. Build argv and dispatch to RunSupervisor.start().
    5. Return 202 with slug, run_id, and events_url.
    """
    apps_dir = _resolve_applications_dir(request)
    slug_dir = apps_dir / slug

    # Step 1: slug must exist
    if not slug_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Slug not found: {slug}")

    # Step 2: determine JD source
    jd_url = _read_jd_url(slug_dir)
    jd_txt_path = slug_dir / "jd.txt"
    text_based = False

    if jd_url is None:
        # Fall back to text-based run
        if jd_txt_path.is_file():
            text_based = True
        else:
            raise HTTPException(
                status_code=400,
                detail="Cannot determine JD source for re-run: no jd-parsed.json or jd.txt found",
            )

    # Step 3: 409 if a run is already in flight
    supervisor = _supervisor()
    existing_run_id = supervisor.get_active_for_slug(slug)
    if existing_run_id is not None:
        events_url = f"/api/applications/{slug}/events?run_id={existing_run_id}"
        raise HTTPException(
            status_code=409,
            detail=RerunConflictResponse(
                slug=slug,
                run_id=existing_run_id,
                status="running",
                events_url=events_url,
            ).model_dump(),
        )

    # Step 4: build argv
    argv: list[str] = ["jobsmith", "apply"]
    if text_based:
        argv += [_JD_URL_PLACEHOLDER, "--jd-text-file", str(jd_txt_path)]
    else:
        argv.append(jd_url)  # type: ignore[arg-type]

    if body.force:
        argv.append("--force")
    argv += ["--yes", body.verbosity]

    # Step 5: dispatch and return 202
    run_id = await supervisor.start(slug, argv, cwd=apps_dir.parent)
    events_url = f"/api/applications/{slug}/events?run_id={run_id}"
    return RerunResponse(slug=slug, run_id=run_id, events_url=events_url)


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
