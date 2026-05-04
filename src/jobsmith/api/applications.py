"""/api/applications router for the jobsmith HTTP API.

Endpoints
---------
GET /applications
    List all unique slugs from apply_runs (latest run per slug).

GET /applications/{slug}
    Return full detail for slug: latest run metadata + all artifacts
    from that run. Returns 404 when slug is not in the pipeline DB.

POST /applications
    Create a new application slug + queue a pipeline run (feat-3c354917).

POST /applications/{slug}/run
    Re-run an existing application (feat-3c354917).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from jobsmith.api.artifacts import _get_db_path, _row_to_envelope
from jobsmith.db import open_pipeline_db

from .schemas.applications import (
    Application,
    ApplicationDetail,
    CreateApplicationRequest,
    CreateApplicationResponse,
    RerunConflictResponse,
    RerunRequest,
    RerunResponse,
)

router = APIRouter(tags=["applications"])

# Slugs are joined into apps_dir; reject anything that could escape it or hit
# a hidden / case-collision filename. Mirrors events.py:_validate_slug_or_404.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Human-readable verbosity → CLI flag the apply pipeline understands.
_VERBOSITY_FLAG = {
    "normal": "-v",
    "verbose": "-vv",
    "debug": "-vvv",
}


def _verbosity_to_cli_flag(verbosity: str) -> str:
    """Translate the wire token into the CLI flag the orchestrator forwards."""
    return _VERBOSITY_FLAG.get(verbosity, "-v")

# Per-slug locks make supervisor.get_active_for_slug + supervisor.start atomic
# inside this process. Two concurrent re-run requests for the same slug now
# serialise; the second one sees the first's row when it acquires the lock.
_slug_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _open_conn(db_path: Path):
    """Open pipeline DB connection, raising 503 on failure."""
    try:
        return open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc


def _latest_run_per_slug(conn) -> list:
    """Return one apply_runs row per slug (most recent by started_at, rowid).

    ``started_at`` is text-formatted ISO at second resolution (see
    :func:`supervisor._now_iso`), so two runs for the same slug started
    in the same second can both match ``MAX(started_at)``. Tie-break on
    ``rowid`` (insertion order) to guarantee exactly one row per slug.
    """
    return conn.execute(
        """
        SELECT ar.* FROM apply_runs ar
        WHERE ar.rowid IN (
            SELECT MAX(inner_ar.rowid)
            FROM apply_runs inner_ar
            WHERE inner_ar.started_at = (
                SELECT MAX(started_at)
                FROM apply_runs
                WHERE slug = inner_ar.slug
            )
            GROUP BY inner_ar.slug
        )
        ORDER BY ar.started_at DESC, ar.rowid DESC
        """,
    ).fetchall()


def _row_to_application(row) -> Application:
    return Application(
        slug=row["slug"],
        run_id=row["run_id"],
        phase=row["phase"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _resolve_applications_dir(request: Request) -> Path:
    """Return the applications directory.

    Test injection: set ``app.state.applications_dir`` to a ``Path``.
    Production: falls back to the loaded config's output.applications_dir.
    """
    override: Path | None = getattr(request.app.state, "applications_dir", None)
    if override is not None:
        return override

    config = getattr(request.app.state, "config", None)
    if config is not None:
        from jobsmith.paths import resolve  # noqa: PLC0415

        return resolve(config.output.applications_dir)

    from jobsmith.config import load_config  # noqa: PLC0415
    from jobsmith.paths import resolve  # noqa: PLC0415

    cfg = load_config()
    return resolve(cfg.output.applications_dir)


def _resolve_supervisor(request: Request):
    """Return the RunSupervisor singleton (or test-injected override)."""
    from jobsmith.api.supervisor import RunSupervisor, get_supervisor  # noqa: PLC0415

    override = getattr(request.app.state, "run_supervisor", None)
    if isinstance(override, RunSupervisor):
        return override
    return get_supervisor()


# ---------------------------------------------------------------------------
# Routes — GET (keep stable)
# ---------------------------------------------------------------------------


@router.get("/applications", response_model=list[Application])
def list_applications() -> list[Application]:
    """Return the latest run summary for each known slug."""
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        rows = _latest_run_per_slug(conn)
    finally:
        conn.close()
    return [_row_to_application(r) for r in rows]


@router.get("/applications/{slug}", response_model=ApplicationDetail)
def get_application(slug: str) -> ApplicationDetail:
    """Return the latest run + all artifacts for *slug*.

    Raises 404 when *slug* has no apply_runs row.
    """
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        run_row = conn.execute(
            "SELECT * FROM apply_runs WHERE slug = ? ORDER BY started_at DESC LIMIT 1",
            (slug,),
        ).fetchone()
        if run_row is None:
            raise HTTPException(
                status_code=404, detail=f"No application found for slug {slug!r}"
            )
        run_id = run_row["run_id"]
        artifact_rows = conn.execute(
            "SELECT * FROM specialist_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    artifacts = [_row_to_envelope(r) for r in artifact_rows]
    return ApplicationDetail(
        slug=run_row["slug"],
        run_id=run_row["run_id"],
        phase=run_row["phase"],
        status=run_row["status"],
        started_at=run_row["started_at"],
        finished_at=run_row["finished_at"],
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# POST /api/applications — create + queue (feat-3c354917)
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
    # Validate exactly one source
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

    # Decode base64 if provided
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

    apps_dir = _resolve_applications_dir(request)

    # Derive slug
    if body.jd_url is not None:
        from jobsmith.apply import derive_slug  # noqa: PLC0415

        slug = derive_slug(body.jd_url)
    else:
        # Append a 6-char hex suffix so two paste/file submissions in the
        # same second don't 409 each other.
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        slug = f"pasted-{ts}-{secrets.token_hex(3)}"

    # Validate that the derived slug can't escape apps_dir or look like a
    # hidden file. derive_slug should return safe values, but exotic URLs
    # could in principle smuggle in `/`, `..`, or a leading `.`.
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail=f"Derived slug is not safe for filesystem use: {slug!r}",
        )

    # 409 if slug directory already exists
    slug_dir = apps_dir / slug
    if slug_dir.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Application slug already exists: {slug!r}",
        )

    # Create slug directory
    slug_dir.mkdir(parents=True, exist_ok=False)

    # Write jd.txt if content came from text/file
    jd_file: Path | None = None
    if jd_content is not None:
        jd_file = slug_dir / "jd.txt"
        jd_file.write_text(jd_content, encoding="utf-8")

    # Build argv for apply pipeline
    argv: list[str] = ["jobsmith", "apply"]
    if body.jd_url is not None:
        argv.append(body.jd_url)
    else:
        # CLI requires a positional URL; "pasted" is a sentinel, with real
        # content supplied via --jd-text-file.
        argv.append("pasted")
        argv += ["--jd-text-file", str(jd_file)]

    if body.skip_confirmations:
        argv.append("--yes")
    if body.force:
        argv.append("--force")
    argv.append(_verbosity_to_cli_flag(body.verbosity))

    # Dispatch via supervisor. If the dispatch fails, clean up the slug
    # directory we just created — otherwise the next retry sees a 409
    # forever and the user has no UI affordance to clear it.
    supervisor = _resolve_supervisor(request)
    try:
        run_id = await supervisor.start(slug, argv, cwd=apps_dir.parent)
    except Exception:
        shutil.rmtree(slug_dir, ignore_errors=True)
        raise

    events_url = f"/api/applications/{slug}/events"
    return CreateApplicationResponse(slug=slug, run_id=run_id, events_url=events_url)


# ---------------------------------------------------------------------------
# POST /api/applications/{slug}/run — re-run (feat-3c354917)
# ---------------------------------------------------------------------------

# Sentinel URL for text-based re-runs (CLI requires a positional URL arg).
_JD_URL_PLACEHOLDER = "file://placeholder"


def _read_apply_url(slug_dir: Path) -> str | None:
    """Extract the original JD URL from .apply-state/jd-parsed.json.

    apply-jd-parser writes the URL under ``apply_url``. Also tries ``jd_url``
    and ``source_url`` for compatibility with older runs. Returns None for
    expected I/O / decode / shape failures; other exceptions propagate so a
    real bug isn't masked.
    """
    jd_parsed = slug_dir / ".apply-state" / "jd-parsed.json"
    if not jd_parsed.is_file():
        return None
    try:
        data = json.loads(jd_parsed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("apply_url") or data.get("jd_url") or data.get("source_url")


@router.post(
    "/applications/{slug}/run",
    response_model=RerunResponse,
    status_code=202,
)
async def rerun_application(
    slug: str,
    request: Request,
    body: RerunRequest = RerunRequest(),  # noqa: B008
) -> RerunResponse:
    """Re-run the apply pipeline for an existing application slug.

    1. 404 if slug directory not found.
    2. Determine JD source from jd-parsed.json (apply_url) or jd.txt.
    3. 400 if no JD source can be located.
    4. 409 if a run is already in progress.
    5. Dispatch to RunSupervisor; return 202 with run_id and events_url.
    """
    # Validate the slug *before* we touch the filesystem — same invariant
    # as create_application's _SLUG_RE check.
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400, detail=f"Invalid slug: {slug!r}"
        )

    apps_dir = _resolve_applications_dir(request)
    slug_dir = apps_dir / slug

    # Step 1: slug must exist
    if not slug_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Slug not found: {slug!r}")

    # Step 2: determine JD source
    jd_url = _read_apply_url(slug_dir)
    jd_txt_path = slug_dir / "jd.txt"
    text_based = False

    if jd_url is None:
        if jd_txt_path.is_file():
            text_based = True
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot determine JD source for re-run: "
                    "no jd-parsed.json (apply_url) or jd.txt found"
                ),
            )

    supervisor = _resolve_supervisor(request)

    # Step 3: 409 if a run is already in flight. Hold a per-slug asyncio lock
    # across the get_active_for_slug → supervisor.start window so two concurrent
    # re-run requests can't both pass the check (TOCTOU fix).
    async with _slug_locks[slug]:
        existing_run_id = supervisor.get_active_for_slug(slug)
        if existing_run_id is not None:
            events_url = f"/api/applications/{slug}/events"
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

        if body.skip_confirmations:
            argv.append("--yes")
        if body.force:
            argv.append("--force")
        argv.append(_verbosity_to_cli_flag(body.verbosity))

        # Step 5: dispatch and return 202
        run_id = await supervisor.start(slug, argv, cwd=apps_dir.parent)
    events_url = f"/api/applications/{slug}/events"
    return RerunResponse(slug=slug, run_id=run_id, events_url=events_url)


__all__ = ["router"]
