"""/api/applications router for the jobsmith HTTP API.

Endpoints
---------
GET /applications
    List all unique slugs from apply_runs (latest run per slug).

GET /applications/{slug}
    Return full detail for slug: latest run metadata + all artifacts
    from that run. Returns 404 when slug is not in the pipeline DB.

POST /applications
    Launch a new apply run. Derives a slug from the URL (or uses the
    caller-supplied slug), checks for 409 conflict, then hands off to the
    supervisor. Returns 201 with {slug, run_id}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from jobsmith.api.artifacts import _get_db_path, _row_to_envelope
from jobsmith.api.supervisor import RunSupervisor, get_supervisor
from jobsmith.apply import derive_slug
from jobsmith.db import open_pipeline_db

from .schemas.applications import (
    Application,
    ApplicationCreate,
    ApplicationCreated,
    ApplicationDetail,
)

router = APIRouter(tags=["applications"])


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


def _extract_jd_fields(conn, run_id: str) -> tuple[str | None, str | None]:
    """Return (role, company) from the jd-parsed artifact for *run_id*, or (None, None)."""
    row = conn.execute(
        "SELECT output_json FROM specialist_outputs WHERE run_id = ? AND kind = 'jd-parsed' LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None, None
    try:
        data = json.loads(row["output_json"])
        return data.get("position"), data.get("company")
    except (json.JSONDecodeError, KeyError):
        return None, None


def _row_to_application(row, conn) -> Application:
    role, company = _extract_jd_fields(conn, row["run_id"])
    return Application(
        slug=row["slug"],
        run_id=row["run_id"],
        phase=row["phase"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        role=role,
        company=company,
    )


def _resolve_supervisor(request: Request) -> RunSupervisor:
    """Return the run supervisor (test-injected via app.state if present)."""
    override = getattr(request.app.state, "run_supervisor", None)
    if isinstance(override, RunSupervisor):
        return override
    return get_supervisor()


async def _launch_run(supervisor: RunSupervisor, slug: str, url: str, cwd: Path) -> str:
    """Build the apply argv and call supervisor.start(). Returns run_id."""
    argv = [sys.executable, "-m", "jobsmith.cli", "apply", url, "--slug", slug]
    return await supervisor.start(slug=slug, argv=argv, cwd=cwd)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/applications", response_model=list[Application])
def list_applications() -> list[Application]:
    """Return the latest run summary for each known slug."""
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        rows = _latest_run_per_slug(conn)
        return [_row_to_application(r, conn) for r in rows]
    finally:
        conn.close()


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
        role, company = _extract_jd_fields(conn, run_id)
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
        role=role,
        company=company,
        artifacts=artifacts,
    )


@router.post(
    "/applications",
    response_model=ApplicationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_application(
    body: ApplicationCreate,
    request: Request,
) -> ApplicationCreated:
    """Launch an apply run for the given URL.

    - Derives a slug from *url* when ``slug`` is not provided.
    - Returns 409 if a run for that slug is already active in the supervisor.
    - Launches ``jobsmith apply <url>`` asynchronously via the supervisor.
    - Returns 201 with ``{slug, run_id}`` so the caller can subscribe to
      ``/api/applications/{slug}/events``.
    """
    slug = body.slug if body.slug else derive_slug(body.url)

    supervisor = _resolve_supervisor(request)

    active_run_id = supervisor.get_active_for_slug(slug)
    if active_run_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A run for slug {slug!r} is already active (run_id={active_run_id!r}).",
        )

    cwd = Path.cwd()
    run_id = await _launch_run(supervisor, slug, body.url, cwd)

    return ApplicationCreated(slug=slug, run_id=run_id)


__all__ = ["router"]
