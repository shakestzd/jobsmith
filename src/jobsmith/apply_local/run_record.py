"""Pipeline DB run-record for the code_local LOCAL apply (feat-d1ef000b).

Records the local apply in the pipeline ``apply_runs`` table so the run-history /
health surfaces see it — the cloud path already does this; the LOCAL path did not
(roborev 1061 finding 1). Fully GUARDED: with no ``.apply-config.yaml`` (tests /
scratch repos) the pipeline DB is simply absent and every call is a clean no-op.

Mirrors the cloud finalize in ``core/pipeline.py`` (raw
``UPDATE apply_runs SET status=?, finished_at=?, slug=? WHERE run_id=?``).
"""
from __future__ import annotations

import logging
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RENDER_PHASE = "render"


def open_run_record(
    repo_root: Any,
    *,
    slug: str,
    run_id: str | None = None,
    phase: str = RENDER_PHASE,
) -> tuple[Any, str]:
    """Open the pipeline DB and insert a ``running`` apply_runs row.

    Returns ``(conn_or_None, run_id)`` — ``conn`` is None when no config DB is
    resolvable. ``run_id`` is a fresh uuid4 when not supplied. Best-effort: a DB
    error degrades to a no-op record, never raising into the apply.
    """
    rid = run_id or str(uuid.uuid4())
    conn = _open_db(repo_root)
    if conn is not None:
        from jobsmith.core.pipeline import _db_now_iso
        from jobsmith.db import insert_apply_run

        with suppress(Exception):
            insert_apply_run(
                conn,
                run_id=rid,
                slug=slug,
                phase=phase,
                started_at=_db_now_iso(),
                finished_at=None,
                status="running",
            )
    return conn, rid


def finalize_run(conn: Any, run_id: str, slug: str, status: str) -> None:
    """Set the apply_runs row to a terminal ``status`` then commit + close.

    No-op when ``conn`` is None (no config DB). Best-effort throughout.
    """
    if conn is None:
        return
    from jobsmith.core.pipeline import _db_now_iso

    try:
        with suppress(Exception):
            conn.execute(
                "UPDATE apply_runs SET status=?, finished_at=?, slug=? WHERE run_id=?",
                (status, _db_now_iso(), slug, run_id),
            )
            conn.commit()
    finally:
        with suppress(Exception):
            conn.close()


def _open_db(repo_root: Any):
    if repo_root is None:
        return None
    from jobsmith.core.pipeline import _open_pipeline_db_for_run

    with suppress(Exception):
        return _open_pipeline_db_for_run(Path(repo_root))
    return None


__all__ = ["RENDER_PHASE", "finalize_run", "open_run_record"]
