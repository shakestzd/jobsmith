"""Post-phase ingest + backfill tests.

Schema/writer tests live in test_db_schema.py.
"""
from __future__ import annotations

import json
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


def test_ingest_reads_real_manifest_invocations_format(tmp_path: Path):
    """Real apply-pipeline manifest uses flat invocations[]; ingest must find rows.

    Regression for roborev #920 HIGH: earlier code looked at
    manifest.phases.<phase>.specialists which the apply pipeline never writes.
    """
    state_dir = tmp_path / ".apply-state"
    state_dir.mkdir()

    state_dir.joinpath("jd-parsed.json").write_text(json.dumps({
        "company": "Acme",
        "position": "Engineer",
        "must_haves": ["Python"],
    }))
    state_dir.joinpath("fit-score.json").write_text(json.dumps({
        "score": 0.8,
        "rationale": "match",
    }))
    state_dir.joinpath("manifest.json").write_text(json.dumps({
        "run_id": "real-run",
        "slug": "real-slug",
        "started_at": "2024-01-01T10:00:00",
        "invocations": [
            {
                "specialist": "apply-jd-parser",
                "status": "ok",
                "started_at": "2024-01-01T10:00:01",
                "finished_at": "2024-01-01T10:00:02",
                "agent_id": "jd-1",
            },
            {
                "specialist": "apply-fit-scorer",
                "status": "ok",
                "started_at": "2024-01-01T10:00:03",
                "finished_at": "2024-01-01T10:00:04",
                "agent_id": "fit-1",
            },
            {
                "specialist": "apply-prose-writer",  # draft phase — must be filtered out
                "status": "ok",
                "started_at": "2024-01-01T10:01:00",
                "finished_at": "2024-01-01T10:01:30",
            },
        ],
    }))

    conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        conn,
        run_id=run_id,
        slug="real-slug",
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at=None,
        status="in-progress",
    )

    inserted = ingest_phase_outputs(
        conn,
        slug="real-slug",
        run_id=run_id,
        phase="gather",
        state_dir=state_dir,
    )

    rows = jobsmith_db.get_specialist_outputs(conn, run_id)
    conn.close()

    # Two gather artifacts existed on disk; the prose-writer invocation is
    # filtered out because it belongs to draft.
    assert inserted == 2, f"Expected 2 ingested rows; got {inserted}"
    specialists = {row["specialist"] for row in rows}
    assert specialists == {"apply-jd-parser", "apply-fit-scorer"}


def test_ingest_skips_failed_invocations(tmp_path: Path):
    """Invocations with status != 'ok' must not be ingested."""
    state_dir = tmp_path / ".apply-state"
    state_dir.mkdir()
    state_dir.joinpath("jd-parsed.json").write_text(json.dumps({"company": "X"}))
    state_dir.joinpath("manifest.json").write_text(json.dumps({
        "invocations": [
            {"specialist": "apply-jd-parser", "status": "failed"},
        ],
    }))

    conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
    run_id = str(uuid.uuid4())
    jobsmith_db.insert_apply_run(
        conn,
        run_id=run_id,
        slug="failed-slug",
        phase="gather",
        started_at="2024-01-01T10:00:00",
        finished_at=None,
        status="in-progress",
    )
    inserted = ingest_phase_outputs(
        conn,
        slug="failed-slug",
        run_id=run_id,
        phase="gather",
        state_dir=state_dir,
    )
    conn.close()
    assert inserted == 0


def test_backfill_reads_invocations_format(tmp_path: Path):
    """Roborev #921 MEDIUM: backfill must use real manifest.invocations.

    The legacy manifest.phases shape never existed in production; with that
    code path, backfill returned phase=unknown and inserted 0 rows.
    """
    from jobsmith.db import open_pipeline_db
    from jobsmith.db_ingest import backfill_slug

    apps = tmp_path / "applications"
    state_dir = apps / "real-slug" / ".apply-state"
    state_dir.mkdir(parents=True)

    # Drop all gather artifacts the readers know about
    state_dir.joinpath("jd-parsed.json").write_text(json.dumps({"company": "X"}))
    state_dir.joinpath("fit-score.json").write_text(json.dumps({"score": 0.5}))
    state_dir.joinpath("hm-snippet.md").write_text(
        "# HM dossier\ndetected: no\n"
    )
    state_dir.joinpath("bullet-selection.json").write_text(json.dumps({}))
    state_dir.joinpath("company-research.md").write_text("# Acme research")
    state_dir.joinpath("manifest.json").write_text(json.dumps({
        "run_id": "real-run",
        "slug": "real-slug",
        "invocations": [
            {"specialist": "apply-jd-parser",       "status": "ok"},
            {"specialist": "apply-fit-scorer",      "status": "ok"},
            {"specialist": "apply-hm-enricher",     "status": "ok"},
            {"specialist": "apply-bullet-selector", "status": "ok"},
            {"specialist": "apply-company-research","status": "ok"},
        ],
    }))

    conn = open_pipeline_db(tmp_path / "jobsmith.db")
    inserted = backfill_slug(conn, "real-slug", apps)

    # Every gather artifact should be ingested when the gather phase is
    # complete in invocations[].
    rows = conn.execute(
        "SELECT specialist FROM specialist_outputs"
    ).fetchall()
    conn.close()

    assert inserted >= 1, "backfill must ingest gather artifacts when invocations[] is complete"
    specialists = {r[0] for r in rows}
    assert "apply-jd-parser" in specialists
    assert "apply-fit-scorer" in specialists


def test_backfill_ingests_every_completed_phase(tmp_path: Path):
    """Backfill must ingest gather AND draft when both phases finished."""
    from jobsmith.db import open_pipeline_db
    from jobsmith.db_ingest import backfill_slug

    apps = tmp_path / "applications"
    state_dir = apps / "multi-phase" / ".apply-state"
    state_dir.mkdir(parents=True)

    # Gather artifacts
    state_dir.joinpath("jd-parsed.json").write_text(json.dumps({"company": "Y"}))
    state_dir.joinpath("fit-score.json").write_text(json.dumps({"score": 0.7}))
    state_dir.joinpath("hm-snippet.md").write_text("# HM\ndetected: no\n")
    state_dir.joinpath("bullet-selection.json").write_text(json.dumps({}))
    state_dir.joinpath("company-research.md").write_text("# research")
    # Draft artifacts
    state_dir.joinpath("prose-draft.md").write_text("Cover letter draft body")
    state_dir.joinpath("ai-tell-report.json").write_text(json.dumps({"iterations": []}))

    state_dir.joinpath("manifest.json").write_text(json.dumps({
        "invocations": [
            {"specialist": "apply-jd-parser",        "status": "ok"},
            {"specialist": "apply-fit-scorer",       "status": "ok"},
            {"specialist": "apply-hm-enricher",      "status": "ok"},
            {"specialist": "apply-bullet-selector",  "status": "ok"},
            {"specialist": "apply-company-research", "status": "ok"},
            {"specialist": "apply-prose-writer",     "status": "ok"},
            {"specialist": "apply-prose-qa",         "status": "ok"},
        ],
    }))

    conn = open_pipeline_db(tmp_path / "jobsmith.db")
    backfill_slug(conn, "multi-phase", apps)
    rows = conn.execute("SELECT specialist FROM specialist_outputs").fetchall()
    conn.close()
    specialists = {r[0] for r in rows}
    assert "apply-jd-parser" in specialists
    assert "apply-prose-writer" in specialists, (
        "draft artifacts must be backfilled when draft phase is complete"
    )

