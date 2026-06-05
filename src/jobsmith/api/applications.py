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

import asyncio
import importlib.util
import json
import logging
import re
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

from jobsmith.api.artifacts import _get_db_path, _row_to_envelope
from jobsmith.api.schemas.artifacts import ArtifactEnvelope
from jobsmith.api.supervisor import RunSupervisor, get_supervisor
from jobsmith.apply import derive_slug
from jobsmith.db import open_pipeline_db
from jobsmith.paths import repo_root_for

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


def _is_empty_scaffold_row(row, conn) -> bool:
    """True for pre-canonical slug rows that have no user-visible payload."""
    role, company, _ = _extract_jd_fields(conn, row["run_id"])
    if role or company:
        return False
    app_dir = _get_app_dir(row["slug"])
    if app_dir is None or not app_dir.exists() or not app_dir.is_dir():
        return False
    files = [
        p for p in app_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".DS_Store")
    ]
    return len(files) == 0


def _is_superseded_starting_slug(row, conn, slugs: set[str]) -> bool:
    """True when a starting URL slug has been replaced by its canonical slug."""
    role, company, _ = _extract_jd_fields(conn, row["run_id"])
    if not role or not company:
        return False
    canonical = f"{_to_slug(company)}-{_to_slug(role)}"
    return canonical != row["slug"] and canonical in slugs


def _extract_jd_fields_from_db(conn, run_id: str) -> tuple[str | None, str | None, str | None]:
    """Return (role, company, apply_url) from specialist_outputs for *run_id*.

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


def _extract_jd_fields(conn, run_id: str) -> tuple[str | None, str | None, str | None]:
    """Return (role, company, apply_url), falling back to in-flight disk state.

    Failed gather runs can produce ``.apply-state/jd-parsed.json`` before the
    post-phase DB ingest step runs. The frontend still needs the real company
    and role in that state, so we resolve the canonical slug from the rekey log
    and read the file directly when ``specialist_outputs`` is empty.
    """
    role, company, apply_url = _extract_jd_fields_from_db(conn, run_id)
    if role or company or apply_url:
        return role, company, apply_url

    row = conn.execute("SELECT slug FROM apply_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None, None, None

    for candidate in _slug_candidates_for_run(conn, run_id, row["slug"]):
        app_dir = _get_app_dir(candidate)
        if app_dir is None:
            continue
        jd_path = app_dir / ".apply-state" / "jd-parsed.json"
        if not jd_path.is_file():
            continue
        try:
            data = json.loads(jd_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data.get("position"), data.get("company"), data.get("apply_url")

    return None, None, None


def _slug_from_rekey_log(conn, run_id: str) -> str | None:
    """Infer the canonical slug from apply_state_log rekey messages."""
    rows = conn.execute(
        "SELECT payload FROM apply_state_log WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    for row in rows:
        payload = row["payload"] or ""
        match = re.search(r"--to\s+([a-z0-9][a-z0-9-]*)", payload)
        if match:
            return match.group(1)
        match = re.search(r"→\s*'([^']+)'", payload)
        if match:
            return match.group(1)
    return None


def _slug_candidates_for_run(conn, run_id: str, starting_slug: str) -> list[str]:
    """Return likely filesystem slugs for a run, preserving priority order."""
    candidates: list[str] = [starting_slug]

    role, company, _ = _extract_jd_fields_from_db(conn, run_id)
    if company and role:
        candidates.append(f"{_to_slug(company)}-{_to_slug(role)}")

    rekeyed = _slug_from_rekey_log(conn, run_id)
    if rekeyed:
        candidates.append(rekeyed)

    result: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _disk_artifact_envelopes(conn, run_id: str, starting_slug: str) -> list[ArtifactEnvelope]:
    """Read in-flight artifact files when DB ingest has not run yet."""
    from jobsmith._state_readers import ARTIFACT_READERS, SPECIALIST_TO_ARTIFACT

    envelopes: list[ArtifactEnvelope] = []
    seen: set[tuple[str, str]] = set()

    for candidate in _slug_candidates_for_run(conn, run_id, starting_slug):
        app_dir = _get_app_dir(candidate)
        if app_dir is None:
            continue
        state_dir = app_dir / ".apply-state"
        if not state_dir.is_dir():
            continue
        for specialist, filename in SPECIALIST_TO_ARTIFACT.items():
            path = state_dir / filename
            if not path.is_file():
                continue
            reader_entry = ARTIFACT_READERS.get(filename)
            if reader_entry is None:
                continue
            kind, reader = reader_entry
            key = (specialist, kind)
            if key in seen:
                continue
            try:
                data = reader(state_dir)
            except Exception:  # noqa: BLE001
                logger.debug("failed to read disk artifact %s", path, exc_info=True)
                continue
            if data is None:
                continue
            output = {"text": data} if isinstance(data, str) else data
            if not isinstance(output, dict):
                output = {"value": output}
            envelopes.append(
                ArtifactEnvelope(
                    run_id=run_id,
                    specialist=specialist,
                    kind=kind,
                    output=output,
                    finished_at=None,
                    transcript_ref=None,
                    version=1,
                )
            )
            seen.add(key)
    return envelopes


def _derive_ui_phase(phase: str, status: str) -> str:
    """Map raw DB (phase, status) → UI-facing taxonomy.

    UI taxonomy:
    - ``running``    — pipeline is actively executing (status='running', any phase)
    - ``rendered``   — all phases completed (status in done/backfilled)
    - ``incomplete`` — run terminated but only some phases completed (manifest check)
    - ``failed``     — run ended in failure with no phases completed
    - ``unknown``    — catch-all
    """
    if status == "running":
        return "running"
    if status in ("done", "backfilled"):
        return "rendered"
    if status == "failed":
        return "failed"
    return "unknown"


def _is_review_halt_blob(blob: str | None) -> bool:
    """True when a specialist result says the run needs human review."""
    if not blob:
        return False
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    status = str(data.get("status") or "").lower()
    reason = str(data.get("reason") or data.get("error") or "").lower()
    return status == "halt" or reason in {
        "uncovered_must_have",
        "restoration_stale",
        "restoration_limit",
    }


def _has_review_halt(conn, run_id: str, slug: str) -> bool:
    """Detect terminal specialist halts that should be shown as review work."""
    for candidate in _slug_candidates_for_run(conn, run_id, slug):
        rows = conn.execute(
            """
            SELECT content_blob
            FROM apply_state
            WHERE slug = ?
              AND (kind LIKE 'apply-%-result' OR kind LIKE '%-result')
            """,
            (candidate,),
        ).fetchall()
        if any(_is_review_halt_blob(row["content_blob"]) for row in rows):
            return True

        app_dir = _get_app_dir(candidate)
        if app_dir is None:
            continue
        state_dir = app_dir / ".apply-state"
        if not state_dir.is_dir():
            continue
        for path in state_dir.glob("*-result.json"):
            try:
                if _is_review_halt_blob(path.read_text(encoding="utf-8")):
                    return True
            except OSError:
                continue
    return False


def _display_status(raw_status: str, ui_phase: str) -> str:
    """Return the user-facing status badge value for API consumers."""
    if ui_phase in {"incomplete", "review"}:
        return ui_phase
    return raw_status


def _load_manifest_for_run(conn, run_id: str, starting_slug: str) -> dict | None:
    """Load the apply_state manifest, trying the starting slug then canonical."""
    from jobsmith.db import get_state

    blob = get_state(conn, slug=starting_slug, kind="manifest")
    if not blob:
        role, company, _ = _extract_jd_fields(conn, run_id)
        if company and role:
            canonical = f"{_to_slug(company)}-{_to_slug(role)}"
            if canonical != starting_slug:
                blob = get_state(conn, slug=canonical, kind="manifest")
    if not blob:
        return None
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _ui_phase_with_manifest(raw_ui_phase: str, conn, run_id: str, slug: str) -> str:
    """Overlay 'incomplete' when a terminal run has only partial phase coverage."""
    if raw_ui_phase == "failed" and _has_review_halt(conn, run_id, slug):
        return "review"
    if raw_ui_phase not in ("failed", "rendered"):
        return raw_ui_phase
    from jobsmith.core.manifest import PHASE_REQUIRED_SPECIALISTS, phase_completed

    manifest = _load_manifest_for_run(conn, run_id, slug)
    if manifest is None:
        return raw_ui_phase
    phases_done = [phase_completed(manifest, p) for p in PHASE_REQUIRED_SPECIALISTS]
    if not all(phases_done) and any(phases_done):
        return "incomplete"
    return raw_ui_phase


def _row_to_application(row, conn) -> Application:
    role, company, _apply_url = _extract_jd_fields(conn, row["run_id"])
    ui_phase = _derive_ui_phase(row["phase"], row["status"])
    ui_phase = _ui_phase_with_manifest(ui_phase, conn, row["run_id"], row["slug"])
    return Application(
        slug=row["slug"],
        run_id=row["run_id"],
        phase=row["phase"],
        status=_display_status(row["status"], ui_phase),
        ui_phase=ui_phase,
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


async def _launch_run(
    supervisor: RunSupervisor,
    slug: str,
    url: str,
    cwd: Path,
    force: bool = False,
    jd_text: str | None = None,
    start_from_phase: str | None = None,
) -> str:
    """Register an in-process apply run and launch it. Returns run_id.

    Slice 4 (trk-ad6d8227): No subprocess is spawned. Instead:
    1. A run_id is minted and a RunHandle + EventSink allocated via
       ``supervisor.register_run``.
    2. ``core_run_apply`` is wrapped in ``asyncio.to_thread`` (it is a
       blocking/synchronous function) and launched as an asyncio Task.
    3. A completion callback calls ``supervisor.on_run_complete`` when the
       task resolves so subscribers receive the end-of-stream sentinel.

    Args:
        supervisor: The RunSupervisor singleton (or test double).
        slug: Application slug (pre-derived by the caller).
        url: Job description URL.
        cwd: Working directory for the apply pipeline.
        force: When True, the pipeline ignores prior artifacts and reruns.
        jd_text: Optional out-of-band JD text (passed directly to
            ``core_run_apply`` which handles temp-file management).

    Returns:
        The minted ``run_id`` string.
    """
    from jobsmith import apply as apply_mod

    run_id = uuid.uuid4().hex
    sink = supervisor.register_run(run_id=run_id, slug=slug)

    # The renderer's emit() also forwards to our supervisor sink so SSE
    # subscribers see PipelineEvents in real time. This is the "API gets
    # the same events the CLI does" wiring the in-process pipeline needs.
    from jobsmith.render import ApplyRenderer

    rdr = ApplyRenderer(yes=True, verbosity=0)
    _orig_emit = rdr.emit

    def _emit_and_broadcast(event):
        sink.emit(event)
        try:
            _orig_emit(event)
        except Exception:  # noqa: BLE001 — never fail the pipeline on a render side-effect
            logger.exception("renderer.emit raised for run_id=%r", run_id)

    rdr.emit = _emit_and_broadcast  # type: ignore[method-assign]

    cancel_event = supervisor.get_cancel_event(run_id)

    async def _run_wrapper() -> None:
        """Run apply.run_apply in a thread; finalise supervisor on completion."""
        try:
            rc = await asyncio.to_thread(
                apply_mod.run_apply,
                url=url,
                cwd=cwd,
                skip_confirm=True,
                force=force,
                jd_text=jd_text,
                slug=slug,
                run_id=run_id,
                renderer=rdr,
                cancel_event=cancel_event,
                start_from_phase=start_from_phase,
            )
        except Exception:  # noqa: BLE001 — task must not propagate unhandled
            logger.exception(
                "core_run_apply task raised for run_id=%r slug=%r", run_id, slug
            )
            rc = 1
        finally:
            supervisor.on_run_complete(run_id, rc)

    task = asyncio.create_task(_run_wrapper(), name=f"apply-{run_id}")
    supervisor.set_task(run_id, task)
    return run_id


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
        slugs = {r["slug"] for r in rows}
        return [
            _row_to_application(r, conn)
            for r in rows
            if not _is_empty_scaffold_row(r, conn)
            and not _is_superseded_starting_slug(r, conn, slugs)
        ]
    finally:
        conn.close()


@router.get("/applications/{slug}", response_model=ApplicationDetail)
def get_application(slug: str) -> ApplicationDetail:
    """Return the latest run + all artifacts for *slug*.

    Raises 404 when *slug* has no apply_runs row (even after rekey resolution).
    Uses :func:`_find_run_row_for_slug` so a stale URL-derived slug (pinned in
    the browser after POST /applications) continues to resolve after
    ``apply_runs.slug`` was rekeyed to the canonical slug mid-run.
    """
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        run_row = _find_run_row_for_slug(conn, slug)
        if run_row is None:
            raise HTTPException(
                status_code=404, detail=f"No application found for slug {slug!r}"
            )
        run_id = run_row["run_id"]
        artifact_rows = conn.execute(
            "SELECT * FROM specialist_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        disk_artifacts = _disk_artifact_envelopes(conn, run_id, run_row["slug"])
        role, company, apply_url = _extract_jd_fields(conn, run_id)
        ui_phase = _derive_ui_phase(run_row["phase"], run_row["status"])
        ui_phase = _ui_phase_with_manifest(ui_phase, conn, run_id, run_row["slug"])
    finally:
        conn.close()
    artifacts = [_row_to_envelope(r) for r in artifact_rows]
    present = {(a.specialist, a.kind) for a in artifacts}
    artifacts.extend(
        a for a in disk_artifacts if (a.specialist, a.kind) not in present
    )
    return ApplicationDetail(
        slug=run_row["slug"],
        run_id=run_row["run_id"],
        phase=run_row["phase"],
        status=_display_status(run_row["status"], ui_phase),
        ui_phase=ui_phase,
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

    cwd = repo_root_for()
    run_id = await _launch_run(
        supervisor, slug, body.url, cwd,
        force=body.force, jd_text=body.jd_text,
        start_from_phase=body.start_from_phase,
    )

    return ApplicationCreated(slug=slug, run_id=run_id)


@router.post("/applications/{slug}/runs/{run_id}/cancel")
async def cancel_application_run(slug: str, run_id: str, request: Request) -> dict:
    """Cancel an active in-process apply run."""
    supervisor = _resolve_supervisor(request)
    cancelled = await supervisor.kill(run_id)
    return {"slug": slug, "run_id": run_id, "cancelled": cancelled}


@router.post("/applications/{slug}/launch-review", status_code=status.HTTP_410_GONE)
async def launch_review_gone(slug: str) -> dict:
    """Removed in feat-95e9bb2d — review UI moved to React frontend."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Review UI moved to React frontend",
    )


def _get_app_dir(slug: str) -> Path | None:
    """Resolve the application directory for *slug*, or None if config missing."""
    from jobsmith.config import find_config, load_config

    search_start = repo_root_for()
    config_path = find_config(search_start)
    if config_path is None:
        return None
    config = load_config(path=config_path)
    repo_root = config_path.parent
    apps_dir = (repo_root / config.output.applications_dir).resolve()
    return apps_dir / slug


def _find_run_row_for_slug(conn, slug: str):
    """Return the latest apply_runs row for *slug*, resolving through slug rekeys.

    Direct lookup first (works when slug is already canonical or was never
    rekeyed).  Falls back to the ``apply_state_log`` rekey trail: the
    jd-parser records a log line whose ``payload`` contains
    ``--from <starting-slug> --to <canonical>``; scanning that table lets us
    map a stale URL-derived slug → run_id → current apply_runs row even after
    ``apply_runs.slug`` was updated to the canonical slug mid-run.

    Returns the ``apply_runs`` row (sqlite3.Row) or None.
    """
    row = conn.execute(
        "SELECT * FROM apply_runs WHERE slug = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (slug,),
    ).fetchone()
    if row is not None:
        return row

    # Rekey fallback: search apply_state_log for rekey commands that name
    # *slug* as the --from side.  We anchor the pattern with a trailing space
    # (or end-of-string) to avoid partial matches such as
    # "acme-foo" matching against "--from acme-foo-bar".
    for pattern in (f"%--from {slug} %", f"%--from {slug}"):
        log_row = conn.execute(
            "SELECT run_id FROM apply_state_log "
            "WHERE payload LIKE ? ORDER BY id DESC LIMIT 1",
            (pattern,),
        ).fetchone()
        if log_row is not None:
            row = conn.execute(
                "SELECT * FROM apply_runs WHERE run_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (log_row["run_id"],),
            ).fetchone()
            if row is not None:
                return row

    return None


_ALLOWED_DOC_SUFFIXES = {".pdf", ".md", ".txt", ".typ"}


def _to_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _has_render_outputs(docs: Path) -> bool:
    """True if *docs* contains at least one rendered output (PDF or cover letter)."""
    return (docs / "resume.pdf").exists() or any(
        f.suffix == ".pdf" for f in docs.iterdir() if f.is_file()
    )


_COVER_LETTER = "cover-letter-draft.md"
_FINAL_COVER_LETTER = "cover-letter.md"


def _cover_letter_candidates(app_dir: Path) -> list[Path]:
    return [
        app_dir / _COVER_LETTER,
        app_dir / ".apply-state" / _COVER_LETTER,
        app_dir / "documents" / _FINAL_COVER_LETTER,
    ]


def _resolve_cover_letter(slug: str, conn) -> Path | None:
    """Return the path to cover-letter-draft.md for *slug*, or None if not found.

    Tries the original-slug directory first, then all candidate slugs derived
    from the run row (jd-parsed company+position, rekey log).  Uses
    :func:`_find_run_row_for_slug` so a stale URL-derived slug is resolved even
    after ``apply_runs.slug`` was updated to the canonical slug mid-run.
    """
    app_dir = _get_app_dir(slug)
    if app_dir is not None:
        for cl in _cover_letter_candidates(app_dir):
            if cl.is_file():
                return cl

    # Rekey-aware fallback: find the run row (works even if apply_runs.slug
    # was updated to canonical) then check every candidate slug directory.
    run_row = _find_run_row_for_slug(conn, slug)
    if run_row is None:
        return None
    run_id = run_row["run_id"]
    current_slug = run_row["slug"]

    for candidate in _slug_candidates_for_run(conn, run_id, current_slug):
        if candidate == slug:
            continue  # already tried above
        candidate_dir = _get_app_dir(candidate)
        if candidate_dir is None:
            continue
        for cl in _cover_letter_candidates(candidate_dir):
            if cl.is_file():
                return cl
    return None


def _resolve_docs_dir(slug: str, conn) -> Path | None:
    """Return the ``documents/`` directory for *slug*, trying the canonical slug on miss.

    The gather phase creates documents/ early (for YAML stubs) so we must
    check for actual render outputs, not just directory existence.

    Uses :func:`_find_run_row_for_slug` so a stale URL-derived slug (e.g.
    ``becu-Sr-Data-Analyst_R-13411-2026-06``) is resolved to the run row even
    after ``apply_runs.slug`` was rekeyed to the canonical form mid-run.
    """
    app_dir = _get_app_dir(slug)
    if app_dir is not None:
        docs = app_dir / "documents"
        if docs.exists() and _has_render_outputs(docs):
            return docs

    # The gather phase rekeyed apply_runs.slug to a canonical slug — resolve
    # via the rekey-aware run-row lookup, then derive canonical from jd-parsed
    # company + position.  Also consult the rekey log itself as a further
    # fallback (_slug_candidates_for_run covers all three paths).
    run_row = _find_run_row_for_slug(conn, slug)
    if run_row is None:
        return None
    run_id = run_row["run_id"]
    current_slug = run_row["slug"]

    for candidate in _slug_candidates_for_run(conn, run_id, current_slug):
        if candidate == slug:
            continue  # already tried above
        candidate_dir = _get_app_dir(candidate)
        if candidate_dir is None:
            continue
        docs = candidate_dir / "documents"
        if docs.exists() and _has_render_outputs(docs):
            return docs

    return None


@router.get("/applications/{slug}/documents")
def list_documents(slug: str) -> list[str]:
    """Return available rendered document filenames for *slug*."""
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        docs_dir = _resolve_docs_dir(slug, conn)
        cl_path = _resolve_cover_letter(slug, conn)
    finally:
        conn.close()

    names: set[str] = set()
    if docs_dir is not None:
        names = {
            f.name
            for f in docs_dir.iterdir()
            if f.is_file() and f.suffix in _ALLOWED_DOC_SUFFIXES and not f.name.startswith(".")
        }

    # cover-letter-draft.md lives at the app root — include it even when
    # documents/ has no PDFs yet (cover-letter-only applications).
    if cl_path is not None:
        names.add(cl_path.name)

    return sorted(names)


@router.get("/applications/{slug}/documents/{filename}")
def get_document(slug: str, filename: str) -> FileResponse:
    """Serve a rendered document (resume.pdf, cover-letter-draft.md, …)."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if Path(filename).suffix not in _ALLOWED_DOC_SUFFIXES:
        raise HTTPException(status_code=400, detail="File type not allowed")

    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        docs_dir = _resolve_docs_dir(slug, conn)
        cl_path = _resolve_cover_letter(slug, conn) if filename == _COVER_LETTER else None
    finally:
        conn.close()

    # cover-letter-draft.md is resolved directly (original or canonical slug),
    # independent of whether documents/ has any PDFs.
    if filename == _COVER_LETTER:
        if cl_path is None:
            raise HTTPException(status_code=404, detail=f"Document {filename!r} not found")
        return FileResponse(str(cl_path), media_type="text/plain; charset=utf-8")

    if docs_dir is None:
        raise HTTPException(status_code=404, detail=f"No documents directory for {slug!r}")

    file_path = (docs_dir / filename).resolve()
    if not str(file_path).startswith(str(docs_dir.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Document {filename!r} not found")

    media = "application/pdf" if file_path.suffix == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(str(file_path), media_type=media)


@router.get("/applications/{slug}/transcript")
def get_transcript(slug: str) -> list[dict]:
    """Return stored apply_state_log rows for the latest run of *slug*.

    Tries the starting slug first, then the canonical company-position slug
    (same resolution logic as _resolve_docs_dir). Strips ``raw`` from each
    payload so the response stays compact — raw is the untruncated original
    that the renderer uses only during a live run.
    """
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        run_row = _find_run_row_for_slug(conn, slug)
        if run_row is None:
            return []
        run_id = run_row["run_id"]
        rows = conn.execute(
            "SELECT id, payload FROM apply_state_log WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
            if isinstance(payload, dict):
                payload = {k: v for k, v in payload.items() if k != "raw"}
        except (ValueError, TypeError):
            continue
        result.append({"run_id": run_id, "payload": payload})
    return result


def _find_canonical_run(conn, slug: str) -> str | None:
    """Look up the run_id via canonical company-position slug resolution."""
    all_rows = conn.execute(
        "SELECT run_id FROM apply_runs ORDER BY started_at DESC, rowid DESC LIMIT 100",
    ).fetchall()
    for row in all_rows:
        role, company, _ = _extract_jd_fields(conn, row["run_id"])
        if not company or not role:
            continue
        if f"{_to_slug(company)}-{_to_slug(role)}" == slug:
            return row["run_id"]
    return None


@router.post("/applications/{slug}/reveal")
def reveal_application(slug: str) -> dict:
    """Open the application directory in Finder (macOS only)."""
    app_dir = _get_app_dir(slug)
    if app_dir is None or not app_dir.exists():
        raise HTTPException(status_code=404, detail=f"Application directory not found for slug {slug!r}")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(app_dir)])
    return {"revealed": str(app_dir)}


# ---------------------------------------------------------------------------
# Review endpoints
# ---------------------------------------------------------------------------

_REVIEW_STATUS_KIND = "review-status"
_COVER_LETTER_FILENAME = "cover-letter-draft.md"


def _resolve_canonical_slug(conn, starting_slug: str, run_id: str) -> str:
    """Return the canonical slug for an application (used for apply_state lookups)."""
    role, company, _ = _extract_jd_fields(conn, run_id)
    if company and role:
        canonical = f"{_to_slug(company)}-{_to_slug(role)}"
        if canonical != starting_slug:
            return canonical
    return starting_slug


@router.get("/applications/{slug}/review")
def get_review(slug: str) -> dict:
    """Return review state: cover letter text, fit score, review status.

    Uses :func:`_find_run_row_for_slug` so a stale URL-derived slug is resolved
    to the canonical slug's data even after ``apply_runs.slug`` was rekeyed.
    """
    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        run_row = _find_run_row_for_slug(conn, slug)
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"No application found for slug {slug!r}")
        run_id = run_row["run_id"]
        canonical = _resolve_canonical_slug(conn, slug, run_id)

        # Fit score from specialist_outputs
        fit_row = conn.execute(
            "SELECT output_json FROM specialist_outputs WHERE run_id = ? AND kind = 'fit-score' LIMIT 1",
            (run_id,),
        ).fetchone()
        fit_data: dict = {}
        if fit_row:
            try:
                fit_data = json.loads(fit_row["output_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Review status from apply_state
        from jobsmith.db import get_state, open_pipeline_db
        db_conn2 = open_pipeline_db(db_path)
        try:
            review_blob = get_state(db_conn2, slug=canonical, kind=_REVIEW_STATUS_KIND)
            if not review_blob:
                review_blob = get_state(db_conn2, slug=slug, kind=_REVIEW_STATUS_KIND)
        finally:
            db_conn2.close()
        review_status_data = {}
        if review_blob:
            try:
                review_status_data = json.loads(review_blob)
            except (json.JSONDecodeError, TypeError):
                pass
    finally:
        conn.close()

    cover_letter_text: str | None = None
    conn = _open_conn(db_path)
    try:
        cl_path = _resolve_cover_letter(slug, conn)
        if cl_path is not None and cl_path.exists():
            cover_letter_text = cl_path.read_text(encoding="utf-8")
    finally:
        conn.close()

    return {
        "slug": slug,
        "canonical_slug": canonical,
        "cover_letter": cover_letter_text,
        "fit_score": fit_data.get("score"),
        "fit_rationale": fit_data.get("rationale"),
        "fit_concerns": fit_data.get("concerns", []),
        "review_status": review_status_data.get("status", "pending"),
        "reviewed_at": review_status_data.get("reviewed_at"),
    }


@router.put("/applications/{slug}/cover-letter")
def save_cover_letter(slug: str, body: dict) -> dict:
    """Save edited cover letter text to disk (atomic write)."""
    text = body.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=422, detail="body.text must be a string")

    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        cl_path = _resolve_cover_letter(slug, conn)
        if cl_path is None:
            app_dir = _get_app_dir(slug)
            if app_dir is not None and app_dir.exists():
                cl_path = app_dir / _COVER_LETTER_FILENAME
    finally:
        conn.close()

    if cl_path is None:
        raise HTTPException(status_code=404, detail=f"No application directory for {slug!r}")

    tmp_path = cl_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.rename(cl_path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    words = len(text.split())
    return {"saved": True, "words": words}


@router.post("/applications/{slug}/review-status")
def set_review_status(slug: str, body: dict) -> dict:
    """Set review status ('approved', 'needs-revision', 'pending') in apply_state."""
    new_status = body.get("status", "")
    valid = {"approved", "needs-revision", "pending"}
    if new_status not in valid:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid}")

    db_path = _get_db_path()
    conn = _open_conn(db_path)
    try:
        run_row = _find_run_row_for_slug(conn, slug)
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"No application found for slug {slug!r}")
        run_id = run_row["run_id"]
        canonical = _resolve_canonical_slug(conn, slug, run_id)
    finally:
        conn.close()

    from datetime import datetime, timezone
    from jobsmith.db import open_pipeline_db, put_state
    db_conn2 = open_pipeline_db(db_path)
    try:
        put_state(
            db_conn2,
            slug=canonical,
            kind=_REVIEW_STATUS_KIND,
            content_blob=json.dumps({
                "status": new_status,
                "reviewed_at": datetime.now(tz=timezone.utc).isoformat(),
            }),
        )
    finally:
        db_conn2.close()

    return {"status": new_status}


__all__ = ["router"]
