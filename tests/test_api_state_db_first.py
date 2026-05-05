"""Tests for DB-first derive_application_state (feat-fe07e6ff).

Coverage
--------
- Phase 0 (queued): no apply_runs row for slug
- Phase 0 (queued): apply_runs row exists with status='queued'
- Phase 1 (gather): specialist_outputs has 'jd-parsed' kind
- Phase 1 (gather): specialist_outputs has 'bullet-selection' kind
- Phase 2 (draft):  specialist_outputs has 'prose-draft' kind
- Phase 3 (render): specialist_outputs has 'cover-letter-draft' kind
- Status pulled from apply_runs.status when set
- JOBSMITH_FS_FALLBACK=0 means no FS reads attempted
- JOBSMITH_FS_FALLBACK=1 (default) reads FS when kind missing AND logs WARNING
- Latest run is used when multiple runs exist for same slug
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.db import open_pipeline_db

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

BASE_TS = "2025-01-01T10:00:00Z"
FINISH_TS = "2025-01-01T10:05:00Z"


def _insert_run(conn, *, run_id: str, slug: str, phase: str = "gather", status: str = "done", started_at: str = BASE_TS, finished_at: str | None = FINISH_TS) -> None:
    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, phase, started_at, finished_at, status),
    )
    conn.commit()


def _insert_output(conn, *, run_id: str, kind: str, output: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO specialist_outputs (run_id, specialist, kind, output_json, transcript_ref, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "test-specialist", kind, json.dumps(output or {}), None, FINISH_TS),
    )
    conn.commit()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Empty pipeline DB."""
    db = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db)
    conn.close()
    return db


def _make_state_fn(db_path: Path, *, fs_fallback: bool = False):
    """Return derive_application_state bound to db_path with given FS flag."""
    from jobsmith.api import state as state_mod

    def _db_path_fn():
        return db_path

    with (
        patch.object(state_mod, "_get_db_path", _db_path_fn),
        patch.dict(os.environ, {"JOBSMITH_FS_FALLBACK": "1" if fs_fallback else "0"}),
    ):
        # return a callable that patches at call time via a closure
        pass  # patches active only during with block — use inline approach instead

    # Return a wrapper that applies patches each call
    def _call(slug: str):
        with (
            patch.object(state_mod, "_get_db_path", _db_path_fn),
            patch.dict(os.environ, {"JOBSMITH_FS_FALLBACK": "1" if fs_fallback else "0"}),
        ):
            return state_mod.derive_application_state(slug)

    return _call


# ---------------------------------------------------------------------------
# Phase derivation from DB kinds
# ---------------------------------------------------------------------------


class TestPhaseDerivation:
    def test_phase0_no_run_row(self, db_path: Path):
        """No apply_runs row → phase 0 (queued)."""
        fn = _make_state_fn(db_path)
        result = fn("unknown-slug")
        assert result["phase"] == 0
        assert result["status"] == "queued"

    def test_phase0_run_status_queued(self, db_path: Path):
        """apply_runs row with status='queued' → phase 0."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-001", slug="queued-co-swe", phase="gather", status="queued", finished_at=None)
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("queued-co-swe")
        assert result["phase"] == 0
        assert result["status"] == "queued"

    def test_phase1_jd_parsed_kind(self, db_path: Path):
        """specialist_outputs has 'jd-parsed' → phase 1 (gather)."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-002", slug="acme-swe", status="done")
        _insert_output(conn, run_id="run-002", kind="jd-parsed", output={"company": "Acme"})
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("acme-swe")
        assert result["phase"] == 1

    def test_phase1_bullet_selection_kind(self, db_path: Path):
        """specialist_outputs has 'bullet-selection' → phase 1 (gather)."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-003", slug="beta-eng", status="done")
        _insert_output(conn, run_id="run-003", kind="bullet-selection")
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("beta-eng")
        assert result["phase"] == 1

    def test_phase2_prose_draft_kind(self, db_path: Path):
        """specialist_outputs has 'prose-draft' → phase 2 (draft)."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-004", slug="gamma-pm", status="done")
        _insert_output(conn, run_id="run-004", kind="jd-parsed")
        _insert_output(conn, run_id="run-004", kind="prose-draft", output={"text": "draft text"})
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("gamma-pm")
        assert result["phase"] == 2

    def test_phase3_cover_letter_draft_kind(self, db_path: Path):
        """specialist_outputs has 'cover-letter-draft' → phase 3 (render)."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-005", slug="delta-ds", status="done")
        _insert_output(conn, run_id="run-005", kind="jd-parsed")
        _insert_output(conn, run_id="run-005", kind="prose-draft")
        _insert_output(conn, run_id="run-005", kind="cover-letter-draft", output={"text": "cover letter"})
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("delta-ds")
        assert result["phase"] == 3

    def test_status_from_apply_runs_done(self, db_path: Path):
        """status='done' is surfaced in result."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-006", slug="epsilon-sre", status="done")
        _insert_output(conn, run_id="run-006", kind="jd-parsed")
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("epsilon-sre")
        assert result["status"] == "done"

    def test_status_from_apply_runs_failed(self, db_path: Path):
        """status='failed' is surfaced in result."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-007", slug="zeta-ml", status="failed")
        _insert_output(conn, run_id="run-007", kind="jd-parsed")
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("zeta-ml")
        assert result["status"] == "failed"
        assert result["phase"] == 1

    def test_latest_run_used_when_multiple_runs(self, db_path: Path):
        """When slug has multiple runs, the most recent (by started_at) is used."""
        conn = open_pipeline_db(db_path)
        # Older run has prose-draft (phase 2)
        _insert_run(conn, run_id="run-old", slug="multi-slug", status="done", started_at="2025-01-01T09:00:00Z")
        _insert_output(conn, run_id="run-old", kind="prose-draft")
        # Newer run only has jd-parsed (phase 1)
        _insert_run(conn, run_id="run-new", slug="multi-slug", status="done", started_at="2025-01-02T09:00:00Z")
        _insert_output(conn, run_id="run-new", kind="jd-parsed")
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("multi-slug")
        # Latest run (run-new) determines phase → 1
        assert result["phase"] == 1
        assert result["run_id"] == "run-new"


# ---------------------------------------------------------------------------
# FS fallback was removed in S3 (feat-eb6c99cb, trk-144d42b1).  The TestFsFallback
# class that used to live here has been deleted — see test_api_state_db_only.py
# for the no-FS-fallback assertions.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Result shape / contract
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_result_has_required_keys(self, db_path: Path):
        """Result dict always includes phase, status, run_id, slug."""
        conn = open_pipeline_db(db_path)
        _insert_run(conn, run_id="run-shape", slug="shape-co", status="done")
        conn.close()

        fn = _make_state_fn(db_path)
        result = fn("shape-co")
        assert "phase" in result
        assert "status" in result
        assert "run_id" in result
        assert "slug" in result

    def test_no_run_row_result_has_required_keys(self, db_path: Path):
        """Even when no run row, result dict has phase, status, run_id, slug."""
        fn = _make_state_fn(db_path)
        result = fn("no-run-slug")
        assert "phase" in result
        assert "status" in result
        assert result["run_id"] is None
        assert result["slug"] == "no-run-slug"
