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
import uuid
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


def _extract_jd_fields(conn, run_id: str) -> tuple[str | None, str | None, str | None]:
    """Return (role, company, apply_url) from the jd-parsed artifact for *run_id*.

    All three values are None when the artifact is absent or unparseable.
    ``apply_url`` is read from the ``apply_url`` field in jd-parsed.json,
    which is written by the ``apply-jd-parser`` specialist.
    """
    row = conn.execute(
        "SELECT output_json FROM specialist_outputs WHERE run_id = ? AND kind = 'jd-parsed' LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None, None, None
    try:
        data = json.loads(row["output_json"])
        return data.get("position"), data.get("company"), data.get("apply_url")
    except (json.JSONDecodeError, KeyError):
        return None, None, None


def _derive_ui_phase(phase: str, status: str) -> str:
    """Map raw DB (phase, status) → UI-facing taxonomy.

    UI taxonomy:
    - ``running``  — pipeline is actively executing (status='running', any phase)
    - ``rendered`` — any completed run (status in done/backfilled). The CLI
      records full pipeline runs with phase='unknown' and status='done', so
      "completed" must not be gated on a specific phase value or those runs
      vanish from the rendered filter (roborev job 940).
    - ``failed``   — any run that ended in failure
    - ``unknown``  — catch-all
    """
    if status == "failed":
        return "failed"
    if status == "running":
        return "running"
    if status in ("done", "backfilled"):
        return "rendered"
    return "unknown"


def _row_to_application(row, conn) -> Application:
    role, company, _apply_url = _extract_jd_fields(conn, row["run_id"])
    return Application(
        slug=row["slug"],
        run_id=row["run_id"],
        phase=row["phase"],
        status=row["status"],
        ui_phase=_derive_ui_phase(row["phase"], row["status"]),
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


def _resolve_transcript_path(slug: str, cwd: Path) -> Path | None:
    """Return the slug's transcript.jsonl path under applications_dir.

    Used by the supervisor's terminal-phase guard (S6, feat-438090af) to
    synthesize a phase=failed SSE event when the apply subprocess dies
    without one.  Returns None when config can't be resolved — the
    supervisor degrades gracefully (no synth, regular log stream only).
    """
    try:
        from jobsmith.config import find_config, load_config
        from jobsmith.paths import resolve

        config_path = find_config(cwd)
        if config_path is None:
            return None
        config = load_config(path=config_path)
        apps_dir = resolve(config.output.applications_dir, cwd)
        return apps_dir / slug / ".apply-state" / "transcript.jsonl"
    except Exception:
        return None


async def _launch_run(
    supervisor: RunSupervisor,
    slug: str,
    url: str,
    cwd: Path,
    force: bool = False,
) -> str:
    """Build the apply argv and call supervisor.start(). Returns run_id.

    When *force* is true, ``--force`` is appended so the apply pipeline
    restarts from phase 1 even if prior artifacts exist for *slug*.

    Threads ``transcript_path`` through to the supervisor so the
    terminal-phase guard (S6, feat-438090af) can synth a phase=failed
    SSE event when the subprocess dies without emitting one.
    """
    # ``--yes`` is mandatory under the supervisor: the subprocess has stdin
    # wired to /dev/null, so the inter-phase ``click.confirm`` gate would
    # raise ``click.Abort`` and the whole pipeline would exit non-zero
    # immediately after phase 1 completes (trk-60217f9f live-test surfaced
    # this — the UI said --yes but the supervisor never propagated it).
    #
    # ``--run-id`` is mandatory when the supervisor's transcript tailer is
    # active (db_path != None). The tailer filters apply_state_log by
    # ``handle.run_id`` from migration 006; without sharing that id with
    # the subprocess, the renderer would tag rows with its own uuid4 and
    # the tailer would see zero structured transcript events (closes
    # roborev job 955 HIGH).
    run_id = uuid.uuid4().hex
    argv = [
        sys.executable,
        "-m",
        "jobsmith.cli",
        "apply",
        url,
        "--slug",
        slug,
        "--yes",
        "--run-id",
        run_id,
    ]
    if force:
        argv.append("--force")
    transcript_path = _resolve_transcript_path(slug, cwd)
    db_path = _resolve_db_path(cwd)
    return await supervisor.start(
        slug=slug,
        argv=argv,
        cwd=cwd,
        transcript_path=transcript_path,
        db_path=db_path,
        run_id=run_id,
    )


def _resolve_db_path(cwd: Path) -> Path | None:
    """Return the pipeline DB absolute path, or ``None`` on config miss.

    Threaded into ``supervisor.start`` so the new ``_tail_state_log``
    (trk-60217f9f Pass 4) can poll apply_state_log by row id instead of
    file offset. ``None`` falls back to the legacy file-tail path.

    Resolves ``config.output.jobsmith_db`` relative to ``config_path.parent``
    (the project root that ``find_config`` walked up to), not relative to
    the supervisor's ``cwd`` — otherwise an API started from a subdirectory
    would create or tail a DB at ``<subdir>/private/jobsmith.db`` while
    ``apply.py:_pipeline_db_path`` writes to ``<project_root>/private/jobsmith.db``,
    silently splitting the slug's apply_state_log between two files.
    """
    try:
        from jobsmith.config import find_config, load_config

        config_path = find_config(cwd)
        if config_path is None:
            return None
        config = load_config(config_path)
        return (config_path.parent / config.output.jobsmith_db).resolve()
    except Exception:
        return None


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
        role, company, apply_url = _extract_jd_fields(conn, run_id)
    finally:
        conn.close()
    artifacts = [_row_to_envelope(r) for r in artifact_rows]
    return ApplicationDetail(
        slug=run_row["slug"],
        run_id=run_row["run_id"],
        phase=run_row["phase"],
        status=run_row["status"],
        ui_phase=_derive_ui_phase(run_row["phase"], run_row["status"]),
        started_at=run_row["started_at"],
        finished_at=run_row["finished_at"],
        role=role,
        company=company,
        apply_url=apply_url,
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
    run_id = await _launch_run(supervisor, slug, body.url, cwd, force=body.force)

    return ApplicationCreated(slug=slug, run_id=run_id)


__all__ = ["router"]
