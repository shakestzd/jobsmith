"""/api/applications router for the jobsmith HTTP API.

Endpoints
---------
GET /applications
    List all unique slugs from apply_runs (latest run per slug).

GET /applications/{slug}
    Return full detail for slug: latest run metadata + all artifacts
    from that run. Returns 404 when slug is not in the pipeline DB.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from jobsmith.api.artifacts import _get_db_path, _row_to_envelope
from jobsmith.db import open_pipeline_db

from .schemas.applications import Application, ApplicationDetail

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
    """Return one apply_runs row per slug (most recent by started_at)."""
    return conn.execute(
        """
        SELECT * FROM apply_runs
        WHERE (slug, started_at) IN (
            SELECT slug, MAX(started_at) FROM apply_runs GROUP BY slug
        )
        ORDER BY started_at DESC
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


__all__ = ["router"]
