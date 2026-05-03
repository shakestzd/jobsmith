"""Post-phase ingest + backfill tests.

Schema/writer tests live in test_db_schema.py.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from jobsmith import db as jobsmith_db
from jobsmith.db_ingest import (
    backfill_slug,
    ingest_phase_outputs,
    iter_backfillable_slugs,
)


def test_ingest_phase_outputs_post_phase_hook(tmp_path: Path, fixture_state_dir: Path):
    """ingest_phase_outputs reads .apply-state/ artifacts via manifest into the DB."""
    conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        conn,
        run_id=run_id,
        slug="hook-test",
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at=None,
        status="in-progress",
    )

    inserted = ingest_phase_outputs(
        conn,
        slug="hook-test",
        run_id=run_id,
        phase="gather",
        state_dir=fixture_state_dir,
    )
    rows = jobsmith_db.get_specialist_outputs(conn, run_id)
    conn.close()

    assert inserted >= 1
    kinds = {row["kind"] for row in rows}
    # fixture has jd-parsed, fit-score, bullet-selection — at least one ingests
    assert kinds & {"jd-parsed", "fit-score", "bullet-selection"}


def test_backfill_idempotent(tmp_path: Path, fixture_state_dir: Path):
    """Running backfill twice on the same slug produces the same row count."""
    app_dir = tmp_path / "applications" / "acme-swe"
    app_dir.mkdir(parents=True)
    shutil.copytree(fixture_state_dir, app_dir / ".apply-state")

    conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
    applications_dir = tmp_path / "applications"

    backfill_slug(conn, "acme-swe", applications_dir)
    runs_first = conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0]
    specs_first = conn.execute(
        "SELECT COUNT(*) FROM specialist_outputs"
    ).fetchone()[0]

    backfill_slug(conn, "acme-swe", applications_dir)
    runs_second = conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0]
    specs_second = conn.execute(
        "SELECT COUNT(*) FROM specialist_outputs"
    ).fetchone()[0]
    conn.close()

    assert runs_first == runs_second
    assert specs_first == specs_second


def test_iter_backfillable_slugs_skips_dotfiles_and_missing_state(tmp_path: Path):
    """Backfill discovery includes only directories with .apply-state/, ignores dotfiles."""
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "real-slug").mkdir()
    (apps / "real-slug" / ".apply-state").mkdir()
    (apps / "no-state-slug").mkdir()  # no .apply-state — skip
    (apps / ".hidden-slug").mkdir()  # dotfile — skip
    (apps / ".hidden-slug" / ".apply-state").mkdir()
    (apps / "regular-file.txt").write_text("not a slug dir")

    slugs = iter_backfillable_slugs(apps)
    assert slugs == ["real-slug"]


def test_iter_backfillable_slugs_returns_empty_for_missing_dir(tmp_path: Path):
    """Missing applications_dir yields an empty list, not an error."""
    assert iter_backfillable_slugs(tmp_path / "nonexistent") == []
