"""/api/applications/{slug}/runs/{run_id}/artifacts router.

Endpoints
---------
GET /applications/{slug}/runs/{run_id}/artifacts
    List all specialist_outputs rows for a specific run_id.
    Returns 404 when the run_id has no outputs.

GET /applications/{slug}/runs/{run_id}/artifacts/{kind}
    Return a single specialist_outputs row by kind.
    Returns 404 when the kind is not present for that run.

DB access
---------
Both endpoints query the SQLite pipeline DB at the path resolved from
.apply-config.yaml → config.output.jobsmith_db.  The helper
``_get_db_path()`` is a module-level function so tests can monkeypatch it.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db

from .schemas.artifacts import ArtifactEnvelope

router = APIRouter(tags=["artifacts"])


# ---------------------------------------------------------------------------
# Internal helpers (module-level so tests can patch them)
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    """Resolve the pipeline DB path from the nearest .apply-config.yaml.

    Raises 404 when no config is found up the cwd tree.
    """
    config_path = find_config(Path.cwd())
    if config_path is None:
        raise HTTPException(status_code=404, detail="No .apply-config.yaml found")
    config = load_config(path=config_path)
    repo_root = config_path.parent
    return (repo_root / config.output.jobsmith_db).resolve()


def _row_to_envelope(row) -> ArtifactEnvelope:
    """Convert a ``specialist_outputs`` sqlite3.Row to ArtifactEnvelope."""
    raw_json = row["output_json"]
    try:
        output = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        output = {}
    if not isinstance(output, dict):
        output = {"value": output}
    return ArtifactEnvelope(
        run_id=row["run_id"],
        specialist=row["specialist"],
        kind=row["kind"],
        output=output,
        finished_at=row["finished_at"],
        transcript_ref=row["transcript_ref"],
    )


def _require_run_outputs(db_path: Path, run_id: str) -> list:
    """Return specialist_outputs rows for run_id, or raise 404."""
    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc
    try:
        rows = conn.execute(
            "SELECT * FROM specialist_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"No artifacts for run_id {run_id!r}"
        )
    return rows


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/applications/{slug}/runs/{run_id}/artifacts",
    response_model=list[ArtifactEnvelope],
)
def list_artifacts(slug: str, run_id: str) -> list[ArtifactEnvelope]:
    """Return all specialist outputs for *slug* / *run_id*."""
    db_path = _get_db_path()
    rows = _require_run_outputs(db_path, run_id)
    return [_row_to_envelope(r) for r in rows]


@router.get(
    "/applications/{slug}/runs/{run_id}/artifacts/{kind}",
    response_model=ArtifactEnvelope,
)
def get_artifact(slug: str, run_id: str, kind: str) -> ArtifactEnvelope:
    """Return the specialist output for *kind* within *slug* / *run_id*.

    Raises 404 when the kind is not present in that run.
    """
    db_path = _get_db_path()
    rows = _require_run_outputs(db_path, run_id)
    for row in rows:
        if row["kind"] == kind:
            return _row_to_envelope(row)
    raise HTTPException(
        status_code=404,
        detail=f"Artifact kind {kind!r} not found in run {run_id!r}",
    )


__all__ = ["router"]
