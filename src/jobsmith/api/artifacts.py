"""/api/applications/{slug}/runs/{run_id}/artifacts router.

Endpoints
---------
GET /applications/{slug}/runs/{run_id}/artifacts
    List all specialist_outputs rows for a specific run_id.
    Returns 404 when the run_id has no outputs.

GET /applications/{slug}/runs/{run_id}/artifacts/{kind}
    Return a single specialist_outputs row by kind.
    Returns 404 when the kind is not present for that run.

PUT /applications/{slug}/runs/{run_id}/artifacts/{kind}
    Upsert a specialist output for a (run_id, kind) pair.
    Body: {output: dict, transcript_ref?: str, finished_at?: str}
    Response: ArtifactEnvelope (includes version counter)

    Concurrent-write semantics (option-version):
    - First write of a (run_id, kind): no If-Match required; version=1 is set.
    - Overwrite: If-Match header must equal current version; 409 on mismatch.
    - Each successful write increments version by 1.

DB access
---------
Both endpoints query the SQLite pipeline DB at the path resolved from
.apply-config.yaml → config.output.jobsmith_db.  The helper
``_get_db_path()`` is a module-level function so tests can monkeypatch it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db
from jobsmith.db_models import KIND_MODELS

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
    col_names = row.keys()
    version = row["version"] if "version" in col_names else 1
    return ArtifactEnvelope(
        run_id=row["run_id"],
        specialist=row["specialist"],
        kind=row["kind"],
        output=output,
        finished_at=row["finished_at"],
        transcript_ref=row["transcript_ref"],
        version=version,
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


def _require_run_exists(conn, run_id: str, slug: str | None = None) -> None:
    """Raise 404 if run_id is not in apply_runs (optionally scoped to *slug*).

    When *slug* is provided, also rejects mismatches between the requested
    slug and the run's actual slug. Without this guard, a PUT to
    ``/applications/foo/runs/<run-belonging-to-bar>/artifacts/...`` would
    silently write a row associated with bar but route through foo.
    """
    if slug is None:
        row = conn.execute(
            "SELECT run_id FROM apply_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT run_id FROM apply_runs WHERE run_id = ? AND slug = ?",
            (run_id, slug),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Run {run_id!r} not found"
        )


# ---------------------------------------------------------------------------
# Request body model
# ---------------------------------------------------------------------------


class PutArtifactBody(BaseModel):
    """Body for PUT /artifacts/{kind}."""

    output: dict[str, Any]
    specialist: str = "api"
    transcript_ref: str | None = None
    finished_at: str | None = None


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


@router.put(
    "/applications/{slug}/runs/{run_id}/artifacts/{kind}",
    response_model=ArtifactEnvelope,
    status_code=200,
)
def put_artifact(
    slug: str,
    run_id: str,
    kind: str,
    body: PutArtifactBody,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ArtifactEnvelope:
    """Upsert a specialist output with optimistic-concurrency version check.

    - First write (no existing row): ``If-Match`` header is not required.
      The created row has ``version=1``.
    - Overwrite (row already exists): ``If-Match`` must equal the current
      ``version``.  A mismatch returns **409 Conflict**.  Each successful
      overwrite increments ``version`` by 1.

    Returns the saved :class:`ArtifactEnvelope` including the new ``version``.
    """
    # Validate kind
    if kind not in KIND_MODELS:
        raise HTTPException(
            status_code=422, detail=f"Unknown artifact kind {kind!r}"
        )

    # Validate output payload against the kind's Pydantic model
    model_cls = KIND_MODELS[kind]
    try:
        model_cls.model_validate(body.output)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Output payload invalid for kind {kind!r}: {exc}",
        ) from exc

    db_path = _get_db_path()
    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

    try:
        # Verify run exists AND belongs to this slug (defends against the
        # frontend or CLI submitting a run_id that's actually owned by a
        # different slug, which would silently corrupt the slug directory).
        _require_run_exists(conn, run_id, slug)

        # Check for existing row (any specialist for this run+kind)
        existing = conn.execute(
            "SELECT version FROM specialist_outputs WHERE run_id = ? AND kind = ? LIMIT 1",
            (run_id, kind),
        ).fetchone()

        output_json = json.dumps(body.output)

        if existing is None:
            # First write — insert at version 1
            new_version = 1
            conn.execute(
                "INSERT INTO specialist_outputs "
                "(run_id, specialist, kind, output_json, transcript_ref, finished_at, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    body.specialist,
                    kind,
                    output_json,
                    body.transcript_ref,
                    body.finished_at,
                    new_version,
                ),
            )
            conn.commit()
        else:
            # Overwrite — require If-Match header
            current_version: int = existing["version"]
            if if_match is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Artifact kind {kind!r} already exists for run {run_id!r}. "
                        "Supply If-Match: <current-version> to overwrite."
                    ),
                )
            try:
                if_match_version = int(if_match)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"If-Match header must be an integer, got {if_match!r}",
                ) from exc
            if if_match_version != current_version:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Version mismatch for kind {kind!r}: "
                        f"expected {current_version}, got {if_match_version}"
                    ),
                )
            new_version = current_version + 1
            conn.execute(
                "UPDATE specialist_outputs "
                "SET specialist=?, output_json=?, transcript_ref=?, finished_at=?, version=? "
                "WHERE run_id=? AND kind=?",
                (
                    body.specialist,
                    output_json,
                    body.transcript_ref,
                    body.finished_at,
                    new_version,
                    run_id,
                    kind,
                ),
            )
            conn.commit()

        # Read back the saved row for the response
        row = conn.execute(
            "SELECT * FROM specialist_outputs WHERE run_id=? AND kind=? LIMIT 1",
            (run_id, kind),
        ).fetchone()
    finally:
        conn.close()

    # Hook for future SSE pubsub. Currently a no-op — DB poll in events.py
    # is authoritative; PUTs surface to SSE consumers within one poll
    # interval (default 0.25s). The call site is preserved as the future
    # broadcast point.
    _broadcast_artifact_event(slug=slug, run_id=run_id, kind=kind, version=new_version)

    return _row_to_envelope(row)


def _broadcast_artifact_event(
    *, slug: str, run_id: str, kind: str, version: int = 1
) -> None:
    """No-op placeholder for an in-process SSE pubsub.

    The DB poll loop in :mod:`jobsmith.api.events` is the authoritative
    surface for artifact-write notifications. SSE consumers see new
    rows within one poll interval, so a true broadcaster is not required
    today. This function exists as a stable call site in
    :func:`put_artifact` so a future change can wire an
    ``asyncio.Queue``-based broadcaster without touching the route.
    """


__all__ = ["router"]
