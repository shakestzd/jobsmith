"""FastAPI application factory for the jobsmith HTTP API.

Usage
-----
From code::

    from jobsmith.api.main import create_app
    app = create_app()

As a uvicorn factory entry-point::

    uvicorn jobsmith.api.main:create_app --factory

Auth
----
All ``/api/*`` routes require a Bearer token (see ``jobsmith.api.auth``).
The ``/health`` endpoint is exempt so monitoring tools work without credentials.

Router mounting
---------------
Health router is mounted without auth (exempt).
API routers are mounted with ``dependencies=[Depends(verify_token)]``.

    # feat-401be81e  /api/master  (this slice — trk-9bb48a61)
    # feat-2c034b07  bearer-token auth (this slice)

    # feat-1e066d57  /api/applications/{slug}/events  SSE stream
    # feat-e592bd70  /api/applications/{slug}/snapshots  snapshots
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jobsmith.api.applications import router as applications_router
from jobsmith.api.artifacts import router as artifacts_router
from jobsmith.api.auth import verify_token
from jobsmith.api.master import router as master_router
from jobsmith.api.snapshots import router as snapshots_router


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application."""
    app = FastAPI(
        title="jobsmith API",
        description="HTTP interface for the jobsmith apply pipeline.",
        version="0.1.0",
    )

    # CORS — only allow the Vite dev server origin in development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Health endpoint (auth-exempt) ---
    @app.get("/health")
    def health() -> JSONResponse:
        """Liveness probe — no auth required."""
        return JSONResponse({"status": "ok"})

    # --- API routers (all require Bearer token) ---
    app.include_router(
        master_router,
        prefix="/api",
        dependencies=[Depends(verify_token)],
    )
    app.include_router(
        artifacts_router,
        prefix="/api",
        dependencies=[Depends(verify_token)],
    )
    app.include_router(
        applications_router,
        prefix="/api",
        dependencies=[Depends(verify_token)],
    )
    app.include_router(
        snapshots_router,
        prefix="/api",
        dependencies=[Depends(verify_token)],
    )

    return app
