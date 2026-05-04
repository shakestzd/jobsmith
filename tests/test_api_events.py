"""Tests for GET /api/applications/{slug}/events SSE endpoint (feat-440324f1).

Schema reality
--------------
Per ``src/jobsmith/migrations/001_initial_schema.sql`` the pipeline DB exposes:

- ``apply_runs (run_id, slug, phase, started_at, finished_at, status)``
- ``specialist_outputs (run_id, specialist, kind, output_json,
  transcript_ref, finished_at)`` with composite PK
  ``(run_id, specialist, kind)``.

There is NO autoincrement ``id`` column. The events endpoint uses SQLite's
implicit ``rowid`` as the monotonic cursor for both tables. These tests
follow that contract.

Fixture pattern
---------------
We pass the pipeline DB path into ``create_app`` via the ``pipeline_db_path``
kwarg (mirrors the existing ``applications_dir`` injection point). Tests
write rows directly with ``open_pipeline_db`` so we don't depend on the
heavyweight apply pipeline.

Coverage (5 tests)
------------------
1. test_events_404_for_missing_slug
2. test_events_emits_specialist_row
3. test_events_filter_quiet
4. test_events_heartbeat
5. test_events_path_traversal_blocked
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    tmp_path: Path,
    *,
    poll_interval_s: float = 0.05,
    heartbeat_interval_s: float = 1.0,
    idle_timeout_s: float = 5.0,
) -> tuple[TestClient, Path, Path]:
    """Build a TestClient with both applications_dir and pipeline_db_path injected.

    Returns
    -------
    (client, applications_dir, pipeline_db_path)
    """
    from jobsmith.api.main import create_app

    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    db_path = tmp_path / "jobsmith.db"

    app = create_app(
        applications_dir=apps_dir,
        pipeline_db_path=db_path,
        events_poll_interval_s=poll_interval_s,
        events_heartbeat_interval_s=heartbeat_interval_s,
        events_idle_timeout_s=idle_timeout_s,
    )
    return TestClient(app), apps_dir, db_path


def _make_slug_dir(apps_dir: Path, slug: str) -> Path:
    d = apps_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_db(db_path: Path):
    from jobsmith.db import open_pipeline_db

    return open_pipeline_db(db_path)


def _insert_run(
    db_path: Path,
    *,
    run_id: str,
    slug: str,
    phase: str,
    status: str,
    started_at: str = "2026-05-04T12:00:00Z",
    finished_at: str | None = None,
) -> None:
    from jobsmith.db import insert_apply_run

    conn = _open_db(db_path)
    try:
        insert_apply_run(
            conn,
            run_id=run_id,
            slug=slug,
            phase=phase,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
        )
    finally:
        conn.close()


def _insert_specialist(
    db_path: Path,
    *,
    run_id: str,
    specialist: str,
    kind: str,
    finished_at: str = "2026-05-04T12:00:01Z",
) -> None:
    from jobsmith.db import insert_specialist_output

    conn = _open_db(db_path)
    try:
        insert_specialist_output(
            conn,
            run_id=run_id,
            specialist=specialist,
            kind=kind,
            output_json=json.dumps({"text": "ok"}),
            transcript_ref=None,
            finished_at=finished_at,
        )
    finally:
        conn.close()


def _read_sse_until(
    response_iter,
    *,
    deadline_s: float,
    stop_when,
) -> list[str]:
    """Drain the SSE stream until ``stop_when(buffer)`` is True or deadline hits.

    Returns the accumulated decoded lines.
    """
    buf: list[str] = []
    end = time.time() + deadline_s
    for chunk in response_iter:
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        buf.append(chunk)
        if stop_when(buf):
            return buf
        if time.time() >= end:
            return buf
    return buf


# ---------------------------------------------------------------------------
# 1. test_events_404_for_missing_slug
# ---------------------------------------------------------------------------


def test_events_404_for_missing_slug(tmp_path: Path) -> None:
    """Streaming a slug that has no slug-dir → 404 before the stream opens."""
    client, _apps_dir, _db_path = _make_client(tmp_path)

    resp = client.get("/api/applications/no-such-slug/events")

    assert resp.status_code == 404
    assert "no-such-slug" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. test_events_emits_specialist_row
# ---------------------------------------------------------------------------


def test_events_emits_specialist_row(tmp_path: Path) -> None:
    """A specialist_outputs insert mid-stream produces a `specialist` SSE event."""
    client, apps_dir, db_path = _make_client(
        tmp_path,
        poll_interval_s=0.05,
        heartbeat_interval_s=10.0,
        idle_timeout_s=5.0,
    )
    slug = "anthropic-applied-ai-2026-04"
    _make_slug_dir(apps_dir, slug)
    _insert_run(
        db_path,
        run_id="run-1",
        slug=slug,
        phase="gather",
        status="running",
    )

    # Insert a specialist row from a background thread shortly after the
    # stream connects so the polling loop can observe it.
    def _writer() -> None:
        time.sleep(0.2)
        _insert_specialist(
            db_path,
            run_id="run-1",
            specialist="apply-jd-parser",
            kind="jd-parsed",
        )

    t = threading.Thread(target=_writer, daemon=True)
    t.start()

    with client.stream(
        "GET",
        f"/api/applications/{slug}/events?verbosity=verbose",
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        chunks = _read_sse_until(
            resp.iter_text(),
            deadline_s=5.0,
            stop_when=lambda buf: any("event: specialist" in c for c in buf),
        )

    text = "".join(chunks)
    assert "event: specialist" in text
    assert "jd-parsed" in text
    assert "run-1" in text
    t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 3. test_events_filter_quiet
# ---------------------------------------------------------------------------


def test_events_filter_quiet(tmp_path: Path) -> None:
    """verbosity=quiet drops `specialist` events and only emits phase events."""
    client, apps_dir, db_path = _make_client(
        tmp_path,
        poll_interval_s=0.05,
        heartbeat_interval_s=10.0,
        idle_timeout_s=2.0,
    )
    slug = "quiet-co"
    _make_slug_dir(apps_dir, slug)
    _insert_run(
        db_path,
        run_id="run-q",
        slug=slug,
        phase="gather",
        status="running",
    )

    def _writer() -> None:
        time.sleep(0.2)
        # Insert a specialist row — should NOT appear under quiet.
        _insert_specialist(
            db_path,
            run_id="run-q",
            specialist="apply-jd-parser",
            kind="jd-parsed",
        )
        # Insert a second run — phase event SHOULD appear.
        time.sleep(0.2)
        _insert_run(
            db_path,
            run_id="run-q-2",
            slug=slug,
            phase="draft",
            status="running",
        )

    t = threading.Thread(target=_writer, daemon=True)
    t.start()

    with client.stream(
        "GET",
        f"/api/applications/{slug}/events?verbosity=quiet",
    ) as resp:
        chunks = _read_sse_until(
            resp.iter_text(),
            deadline_s=4.0,
            stop_when=lambda buf: any("event: phase" in c for c in buf),
        )

    text = "".join(chunks)
    assert "event: phase" in text, "expected at least one phase event under quiet"
    assert "event: specialist" not in text, "quiet must filter specialist events"
    t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 4. test_events_heartbeat
# ---------------------------------------------------------------------------


def test_events_heartbeat(tmp_path: Path) -> None:
    """With no DB writes, the stream emits at least one heartbeat comment."""
    client, apps_dir, _db_path = _make_client(
        tmp_path,
        poll_interval_s=0.05,
        heartbeat_interval_s=0.3,  # short so the test runs fast
        idle_timeout_s=2.0,
    )
    slug = "idle-co"
    _make_slug_dir(apps_dir, slug)

    with client.stream(
        "GET",
        f"/api/applications/{slug}/events?verbosity=normal",
    ) as resp:
        assert resp.status_code == 200
        chunks = _read_sse_until(
            resp.iter_text(),
            deadline_s=2.5,
            stop_when=lambda buf: any(": ping" in c for c in buf),
        )

    text = "".join(chunks)
    assert ": ping" in text, f"expected heartbeat comment, got: {text!r}"


# ---------------------------------------------------------------------------
# 5. test_events_path_traversal_blocked
# ---------------------------------------------------------------------------


def test_events_path_traversal_blocked(tmp_path: Path) -> None:
    """A slug that resolves outside applications_dir is rejected.

    Slugs containing ``/`` are routed by FastAPI as separate path segments and
    will not match the route at all. We additionally guard against ``..`` and
    other suspicious characters at the handler level by reusing the same
    ``slug_dir.is_dir()`` + resolve-and-startswith check the detail endpoint
    employs. An attacker-supplied slug like ``..`` resolves to the parent of
    ``applications_dir`` — which is not a slug dir — so the handler returns 404
    (or 400 if our validator rejects the format outright).
    """
    client, _apps_dir, _db_path = _make_client(tmp_path)

    # ".." literally — slug_dir is apps_dir/.. which is outside the root.
    resp = client.get("/api/applications/../events")
    # FastAPI normalises this URL — it most likely 404s at the route layer,
    # but in any case it MUST NOT 200 OK, MUST NOT leak content.
    assert resp.status_code in (400, 404, 405)

    # Suspicious URL-encoded slug with a percent sign — handler must reject.
    resp2 = client.get("/api/applications/..%25passwd/events")
    assert resp2.status_code in (400, 404)
