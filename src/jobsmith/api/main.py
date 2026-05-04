"""FastAPI application factory for the jobsmith HTTP API.

Usage
-----
From code:
    from jobsmith.api.main import create_app
    app = create_app()          # uses defaults / env-located config
    app = create_app(config)    # explicit JobsmithConfig instance

As a uvicorn factory entry-point:
    uvicorn jobsmith.api.main:create_app --factory

CORS
----
Only http://localhost:5173 (the web/ Vite dev server) is allowed.
Add production origins here when the frontend is deployed.

Router mounting
---------------
Health router is mounted below. Sibling slices mount their own routers here:

    # feat-401be81e  /api/master
    # from jobsmith.api.master import router as master_router
    # app.include_router(master_router, prefix="/api")

    # feat-d08c5002  /api/applications  (listing)
    # from jobsmith.api.applications import router as applications_router
    # app.include_router(applications_router, prefix="/api")

    # feat-e3b75a8a  /api/applications/{slug}  (detail)
    # (included in applications_router)

    # feat-440324f1  /api/events  (SSE)
    # from jobsmith.api.events import router as events_router
    # app.include_router(events_router, prefix="/api")
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jobsmith.api.applications import router as applications_router
from jobsmith.api.events import router as events_router
from jobsmith.api.health import router as health_router
from jobsmith.api.master import router as master_router


def create_app(  # noqa: ANN001
    config=None,
    *,
    applications_dir: Path | None = None,
    pipeline_db_path: Path | None = None,
    events_poll_interval_s: float | None = None,
    events_heartbeat_interval_s: float | None = None,
    events_idle_timeout_s: float | None = None,
) -> FastAPI:
    """Construct and return the configured FastAPI application.

    Parameters
    ----------
    config:
        Optional ``JobsmithConfig`` instance. When None, the health router
        resolves config lazily (via ``find_config`` at request time). Future
        routers that need the config at startup should accept it here and
        store it in ``app.state``.
    applications_dir:
        Optional explicit override for the slug directory root. Tests pass a
        ``tmp_path`` here so the ``/api/applications`` router can read from a
        fixture filesystem without touching the real config.
    pipeline_db_path:
        Optional explicit pipeline DB path used by the SSE events router.
        Tests inject a ``tmp_path`` SQLite file. When None, the events router
        derives the path from ``config.output.jobsmith_db``.
    events_poll_interval_s, events_heartbeat_interval_s, events_idle_timeout_s:
        Optional knobs for the SSE stream. Tests use small values so they run
        fast; production uses the module-level defaults.
    """
    app = FastAPI(
        title="jobsmith API",
        description="HTTP interface for the jobsmith apply pipeline.",
        version="0.1.0",
    )

    # CORS — only allow the Vite dev server origin.
    # Production deployments add their own origin to this list.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store config in app state for routers that need it.
    app.state.config = config
    app.state.applications_dir = applications_dir
    app.state.pipeline_db_path = pipeline_db_path
    if events_poll_interval_s is not None:
        app.state.events_poll_interval_s = events_poll_interval_s
    if events_heartbeat_interval_s is not None:
        app.state.events_heartbeat_interval_s = events_heartbeat_interval_s
    if events_idle_timeout_s is not None:
        app.state.events_idle_timeout_s = events_idle_timeout_s

    # --- Mount routers ---

    # Health check (this slice)
    app.include_router(health_router)

    # feat-401be81e: mount master router here
    app.include_router(master_router, prefix="/api")

    # feat-d08c5002: mount applications listing router here
    app.include_router(applications_router, prefix="/api")

    # feat-e3b75a8a: mount application detail router here (part of applications_router)

    # feat-440324f1: mount SSE events router here
    app.include_router(events_router, prefix="/api")

    return app
