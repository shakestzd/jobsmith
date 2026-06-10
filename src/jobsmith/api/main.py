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
API routers are mounted with ``dependencies=[Depends(current_user)]``.

    # feat-401be81e  /api/master  (this slice — trk-9bb48a61)
    # feat-2c034b07  bearer-token auth (this slice)

    # feat-1e066d57  /api/applications/{slug}/events  SSE stream
    # feat-e592bd70  /api/applications/{slug}/snapshots  snapshots
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jobsmith.api.applications import router as applications_router
from jobsmith.api.artifacts import router as artifacts_router
from jobsmith.api.auth import current_user, current_user_or_query
from jobsmith.api.auth_routes import router as auth_router
from jobsmith.api.cache_routes import router as cache_router
from jobsmith.api.chat import router as chat_router
from jobsmith.api.config import router as config_router
from jobsmith.api.deps import upsert_or_load_user
from jobsmith.api.doctor import router as doctor_router
from jobsmith.api.events import router as events_router
from jobsmith.api.feedback import router as feedback_router
from jobsmith.api.jd_routes import router as jd_router
from jobsmith.api.master import router as master_router
from jobsmith.api.onboard_routes import router as onboard_router
from jobsmith.api.postings_routes import router as postings_router
from jobsmith.api.snapshots import router as snapshots_router
from jobsmith.api.staticui import mount_static_ui
from jobsmith.paths import repo_root_for

_log = logging.getLogger(__name__)


def _try_ingest_master(*, reload: bool = False) -> None:
    """Best-effort master YAML ingest at startup.

    Resolves the DB path and repo root via the shared resolver chain.
    Logs and swallows all errors so a missing config never prevents startup.
    """
    from jobsmith.config import find_config, load_config
    from jobsmith.master_ingest import ensure_master_loaded

    try:
        cwd = repo_root_for()
        config_path = find_config(cwd)
        if config_path is None:
            _log.debug("main: no .apply-config.yaml found, skipping master ingest")
            return
        config = load_config(path=config_path)
        repo_root = config_path.parent
        db_path = (repo_root / config.output.jobsmith_db).resolve()
        ensure_master_loaded(db_path, repo_root=repo_root, reload=reload)
    except Exception:
        _log.warning("master ingest at startup failed (non-fatal)", exc_info=True)


def _detect_fs_only_apps(repo_root: Path, db_path: Path) -> list[str]:
    """Return slugs that have .apply-state/ on disk but no apply_runs row.

    Best-effort: any error returns an empty list so a missing/broken DB
    doesn't crash startup.  S7 of trk-144d42b1 (feat-4c0c39e6).
    """
    if not db_path.exists():
        return []
    try:
        from jobsmith.config import load_config

        config_path = repo_root / ".apply-config.yaml"
        if not config_path.exists():
            return []
        config = load_config(path=config_path)
        from jobsmith.paths import resolve

        apps_dir = resolve(config.output.applications_dir, repo_root)
        if not apps_dir.is_dir():
            return []

        from jobsmith.db import open_pipeline_db

        conn = open_pipeline_db(db_path)
        try:
            db_slugs = {
                r["slug"]
                for r in conn.execute("SELECT DISTINCT slug FROM apply_runs").fetchall()
            }
        finally:
            conn.close()

        fs_slugs: list[str] = []
        for child in apps_dir.iterdir():
            if not child.is_dir():
                continue
            if (child / ".apply-state").is_dir() and child.name not in db_slugs:
                fs_slugs.append(child.name)
        return sorted(fs_slugs)
    except Exception:
        _log.debug("first-run detection failed (non-fatal)", exc_info=True)
        return []


def _maybe_warn_fs_only_state(repo_root: Path, db_path: Path) -> None:
    """Log a single WARNING when FS-only state is detected at startup."""
    fs_only = _detect_fs_only_apps(repo_root, db_path)
    if not fs_only:
        return
    sample = ", ".join(fs_only[:5])
    suffix = "" if len(fs_only) <= 5 else f" (and {len(fs_only) - 5} more)"
    _log.warning(
        "FS-only application state detected for %d slug(s): %s%s. "
        "Recovery: `jobsmith db backfill --all` to ingest. "
        "Master sections: `jobsmith db load-master` if also FS-only. "
        "Set JOBSMITH_AUTO_BACKFILL=1 to backfill on startup.",
        len(fs_only),
        sample,
        suffix,
    )

    if os.environ.get("JOBSMITH_AUTO_BACKFILL", "0") == "1":
        try:
            from jobsmith.config import load_config
            from jobsmith.db import open_pipeline_db
            from jobsmith.db_ingest import backfill_all
            from jobsmith.paths import resolve

            config = load_config(path=repo_root / ".apply-config.yaml")
            apps_dir = resolve(config.output.applications_dir, repo_root)
            conn = open_pipeline_db(db_path)
            try:
                results = backfill_all(conn, apps_dir)
            finally:
                conn.close()
            _log.warning(
                "JOBSMITH_AUTO_BACKFILL: backfilled %d slug(s)", len(results)
            )
        except Exception:
            _log.warning("JOBSMITH_AUTO_BACKFILL backfill failed", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan handler: ingest master YAML and warn on FS-only state."""
    # Cache the resolved root on app.state so all request handlers read from here.
    app.state.repo_root = repo_root_for()
    _log.info("repo_root resolved to %s", app.state.repo_root)

    reload_master = os.environ.get("JOBSMITH_RELOAD_MASTER", "0") == "1"
    _try_ingest_master(reload=reload_master)

    # First-run UX: warn if .apply-state/ dirs exist without DB rows (S7).
    try:
        cwd = app.state.repo_root
        from jobsmith.config import find_config, load_config

        config_path = find_config(cwd)
        if config_path is not None:
            config = load_config(path=config_path)
            repo_root = config_path.parent
            db_path = (repo_root / config.output.jobsmith_db).resolve()
            _maybe_warn_fs_only_state(repo_root, db_path)
            # Upsert the user from config into the users table (best-effort).
            try:
                from jobsmith.db import open_pipeline_db

                conn = open_pipeline_db(db_path)
                try:
                    upsert_or_load_user(conn, config)
                finally:
                    conn.close()
            except Exception:
                _log.warning(
                    "user upsert at startup failed (non-fatal)", exc_info=True
                )
    except Exception:
        _log.debug("first-run check failed (non-fatal)", exc_info=True)

    yield


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application."""
    app = FastAPI(
        title="jobsmith API",
        description="HTTP interface for the jobsmith apply pipeline.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # CORS — allow local Vite dev server origins used by humans and browser automation.
    # Redundant when serving same-origin (feat-9c980bef) but kept for --dev split mode
    # where the Vite dev server and the API run on separate ports.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:5174",
            "http://host.docker.internal:5173",
            "http://host.docker.internal:5174",
        ],
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
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        artifacts_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        applications_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        snapshots_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    # Events router uses the header-or-query auth dependency because browser
    # EventSource cannot set Authorization headers (roborev job 940 finding).
    app.include_router(
        events_router,
        prefix="/api",
        dependencies=[Depends(current_user_or_query)],
    )
    app.include_router(
        doctor_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        feedback_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        config_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        auth_router,
        prefix="/api/auth",
        tags=["auth"],
    )
    app.include_router(
        cache_router,
        prefix="/api",
        tags=["cache"],
    )
    app.include_router(
        jd_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        chat_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    app.include_router(
        onboard_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )
    # feat-827071e1  /api/postings  (sourcing inbox — trk-cf0dc034 slice 5)
    app.include_router(
        postings_router,
        prefix="/api",
        dependencies=[Depends(current_user)],
    )

    # OpenAPI: surface the HTTPBearer scheme so /docs has an Authorize button.
    _install_openapi_security(app)

    # Static UI + SPA catch-all — registered LAST so all API routes take
    # precedence.  Skipped silently (API-only mode) when no web-dist is found.
    # Also skipped in --dev mode (JOBSMITH_DEV=1) where the Vite dev server
    # serves the front-end on a separate port (feat-2423bbec slice-4).
    if os.environ.get("JOBSMITH_DEV") != "1":
        mount_static_ui(app)

    return app


def _install_openapi_security(app: FastAPI) -> None:
    """Register the HTTPBearer security scheme in the generated OpenAPI doc."""
    from fastapi.openapi.utils import get_openapi

    def _custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[
            "HTTPBearer"
        ] = {"type": "http", "scheme": "bearer"}
        schema["security"] = [{"HTTPBearer": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]
