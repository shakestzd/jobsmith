"""/api/onboard router — web onboarding flow (feat-c6ee77d4).

Endpoints
---------
POST /onboard
    Multipart upload: resume file, LinkedIn export, paste text, or URL.
    Launches the onboarding pipeline in-process via asyncio.to_thread +
    supervisor.register_run (mirrors apply's API path). Returns 202 with
    {run_id, status}. Enforces a 10 MB size cap → 413 on oversize.

GET /onboard/{run_id}
    Return the run handle status for an onboard run.

POST /onboard/{run_id}/answers
    Feed gap-interview answers back into a waiting pipeline via the
    pending-answers registry.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from threading import Event as ThreadEvent

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status  # noqa: B008

from jobsmith.api.supervisor import RunSupervisor, get_supervisor
from jobsmith.onboard.pipeline import run_onboard_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboard"])

# Max upload size: 10 MB
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Registry: run_id → threading.Event that signals answers are ready
_answer_events: dict[str, ThreadEvent] = {}
# Registry: run_id → answer dict provided by POST /onboard/{run_id}/answers
_pending_answers: dict[str, dict[str, str]] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_supervisor(request: Request) -> RunSupervisor:
    """Return the run supervisor (test-injected via app.state if present)."""
    override = getattr(request.app.state, "run_supervisor", None)
    if isinstance(override, RunSupervisor):
        return override
    return get_supervisor()


def _get_repo_root(request: Request) -> Path:
    """Return the repo root from app.state (cached at lifespan startup)."""
    return getattr(request.app.state, "repo_root", Path("."))


async def _launch_onboard(
    supervisor: RunSupervisor,
    run_id: str,
    repo_root: Path,
    *,
    resume_file: Path | None = None,
    linkedin_export: Path | None = None,
    linkedin_url: str | None = None,
    paste: str | None = None,
    paste_file: Path | None = None,
) -> None:
    """Register an in-process onboard run and launch it as an asyncio Task."""
    slug = "onboard"
    sink = supervisor.register_run(run_id=run_id, slug=slug)

    # Set up the answer event so the pipeline can block waiting for answers.
    answer_event = ThreadEvent()
    _answer_events[run_id] = answer_event

    def _answer_callback(questions) -> dict[str, str]:
        """Block until the API receives answers via POST /onboard/{run_id}/answers."""
        # Emit gap_questions event so the frontend receives the questions list.
        # The sink already had the event emitted inside run_onboard_pipeline
        # (before this callback fires), so we just wait for web answers.
        answer_event.wait(timeout=3600)  # 1-hour timeout
        return _pending_answers.pop(run_id, {})

    async def _run_wrapper() -> None:
        rc = 1
        try:
            rc = await asyncio.to_thread(
                run_onboard_pipeline,
                repo_root=repo_root,
                resume_file=resume_file,
                linkedin_export=linkedin_export,
                linkedin_url=linkedin_url,
                paste=paste,
                paste_file=paste_file,
                run_id=run_id,
                events=sink,
                answer_callback=_answer_callback,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "run_onboard_pipeline raised for run_id=%r", run_id
            )
            rc = 1
        finally:
            supervisor.on_run_complete(run_id, rc)
            _answer_events.pop(run_id, None)

    task = asyncio.create_task(_run_wrapper(), name=f"onboard-{run_id}")
    supervisor.set_task(run_id, task)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/onboard", status_code=status.HTTP_202_ACCEPTED)
async def start_onboard(  # noqa: B008
    request: Request,
    resume_file: UploadFile | None = File(default=None),  # noqa: B008
    linkedin_export: UploadFile | None = File(default=None),  # noqa: B008
    paste: str | None = Form(default=None),  # noqa: B008
    linkedin_url: str | None = Form(default=None),  # noqa: B008
) -> dict:
    """Launch an onboarding pipeline run.

    Accepts multipart form data with any combination of:
    - resume_file: PDF, DOCX, TXT or Markdown upload
    - linkedin_export: LinkedIn ZIP export upload
    - paste: raw pasted resume / profile text
    - linkedin_url: public LinkedIn profile URL

    Returns 202 with {run_id, status: 'running'}.
    Returns 413 when any uploaded file exceeds 10 MB.
    """
    supervisor = _resolve_supervisor(request)
    repo_root = _get_repo_root(request)

    # Concurrency guard: onboarding writes the shared master YAMLs, so only one
    # run may be active at a time. Mirror apply's launch guard → 409 Conflict.
    if supervisor.get_active_for_slug("onboard") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An onboarding run is already in progress; wait for it to finish.",
        )

    # Read and size-check uploaded files
    resume_bytes: bytes | None = None
    linkedin_bytes: bytes | None = None

    if resume_file is not None and resume_file.filename:
        resume_bytes = await resume_file.read()
        if len(resume_bytes) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"resume_file exceeds maximum size of {_MAX_UPLOAD_BYTES} bytes",
            )

    if linkedin_export is not None and linkedin_export.filename:
        linkedin_bytes = await linkedin_export.read()
        if len(linkedin_bytes) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"linkedin_export exceeds maximum size of {_MAX_UPLOAD_BYTES} bytes",
            )

    run_id = uuid.uuid4().hex

    # Write uploaded bytes to temp files that the pipeline can read
    upload_dir = repo_root / ".onboard-uploads" / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    resume_path: Path | None = None
    linkedin_path: Path | None = None

    if resume_bytes is not None:
        suffix = Path(resume_file.filename or "resume.bin").suffix or ".bin"
        resume_path = upload_dir / f"resume{suffix}"
        resume_path.write_bytes(resume_bytes)

    if linkedin_bytes is not None:
        suffix = Path(linkedin_export.filename or "linkedin.zip").suffix or ".zip"
        linkedin_path = upload_dir / f"linkedin{suffix}"
        linkedin_path.write_bytes(linkedin_bytes)

    await _launch_onboard(
        supervisor,
        run_id,
        repo_root,
        resume_file=resume_path,
        linkedin_export=linkedin_path,
        linkedin_url=linkedin_url or None,
        paste=paste or None,
    )

    return {"run_id": run_id, "status": "running"}


@router.get("/onboard/{run_id}")
def get_onboard_status(run_id: str, request: Request) -> dict:
    """Return run handle status for an onboard run.

    Returns 404 when the run_id is not registered with the supervisor.
    """
    supervisor = _resolve_supervisor(request)
    handle = supervisor.get(run_id)
    if handle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No onboard run found for run_id={run_id!r}",
        )
    return {
        "run_id": handle.run_id,
        "slug": handle.slug,
        "status": handle.status,
        "started_at": handle.started_at,
        "finished_at": handle.finished_at,
    }


@router.post("/onboard/{run_id}/answers")
def submit_onboard_answers(run_id: str, body: dict, request: Request) -> dict:
    """Feed gap-interview answers into a waiting onboard run.

    The pipeline's answer_callback blocks on an Event; this endpoint stores
    the answers and signals the event so the pipeline can continue.

    Body: {answers: {"section.field": "value", ...}}

    Returns 404 when run_id is unknown.
    Returns 200 with {accepted: true, run_id} on success.
    """
    supervisor = _resolve_supervisor(request)
    handle = supervisor.get(run_id)
    if handle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No onboard run found for run_id={run_id!r}",
        )

    answers = body.get("answers", {})
    if not isinstance(answers, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="body.answers must be an object",
        )

    _pending_answers[run_id] = answers
    event = _answer_events.get(run_id)
    if event is not None:
        event.set()

    return {"accepted": True, "run_id": run_id}


__all__ = ["router"]
