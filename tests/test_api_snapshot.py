"""Tests for POST /api/applications/{slug}/runs/{run_id}/snapshot.

Coverage:
- Happy path: all artifacts written to FS
- Returns SnapshotResult with file list + byte counts
- 404 for missing run_id
- kinds=[...] filter only writes those kinds
- target='apply-state' / 'slug-root' / 'both' selectors
- Atomic write: write failure leaves no half-written file
- hm-snippet serialisation round-trip
- text artifact serialisation
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobsmith.api.snapshots import (
    _APPLY_STATE_FILENAMES,
    _SLUG_ROOT_FILENAMES,
    _atomic_write,
    _serialise_artifact,
    _serialise_hm_snippet,
)
from jobsmith.api.snapshots import (
    router as snapshots_router,
)

# ---------------------------------------------------------------------------
# DB + filesystem fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def snapshot_env(tmp_path: Path):
    """Set up a pipeline DB with a run + artifacts, and an apps_dir.

    Returns (db_path, apps_dir, slug, run_id).
    """
    from jobsmith.db import open_pipeline_db

    db_path = tmp_path / "jobsmith.db"
    conn = open_pipeline_db(db_path)

    slug = "acme-swe-2025"
    run_id = "run-snap-001"

    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "draft", "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z", "done"),
    )
    # Apply-state artifacts
    for kind, output in [
        ("jd-parsed", {"company": "Acme", "position": "SWE"}),
        ("fit-score", {"score": 0.85, "rationale": "Good match"}),
        (
            "hm-snippet",
            {
                "detected": True,
                "name": "Alice",
                "source": "linkedin_post",
                "one_specific_signal": "Loves Python",
                "suggested_hook": None,
            },
        ),
        ("prose-draft", {"text": "Here is my draft cover letter."}),
    ]:
        conn.execute(
            "INSERT INTO specialist_outputs "
            "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, f"agent-{kind}", kind, json.dumps(output), None, "2025-01-01T10:02:00Z"),
        )
    # Slug-root artifact
    conn.execute(
        "INSERT INTO specialist_outputs "
        "(run_id, specialist, kind, output_json, transcript_ref, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "assembler",
            "variables",
            json.dumps({"slug": slug, "company": "Acme", "position": "SWE"}),
            None,
            "2025-01-01T10:03:00Z",
        ),
    )
    conn.commit()
    conn.close()

    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    (apps_dir / slug).mkdir()

    return db_path, apps_dir, slug, run_id


@pytest.fixture()
def client_snapshot(snapshot_env, monkeypatch: pytest.MonkeyPatch):
    """TestClient wired to snapshot router + real DB + temp apps_dir."""
    db_path, apps_dir, slug, run_id = snapshot_env

    monkeypatch.setattr("jobsmith.api.snapshots._get_db_path", lambda: db_path)
    monkeypatch.setattr("jobsmith.api.snapshots._get_apps_dir", lambda: apps_dir)

    _app = FastAPI()
    _app.include_router(snapshots_router, prefix="/api")
    tc = TestClient(_app, raise_server_exceptions=True)
    return tc, apps_dir, slug, run_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSnapshotHappyPath:
    def test_returns_200(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot")
        assert resp.status_code == 200, resp.text

    def test_result_schema(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        data = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot").json()
        assert data["slug"] == slug
        assert data["run_id"] == run_id
        assert isinstance(data["files"], list)
        assert isinstance(data["total_bytes"], int)
        assert data["total_bytes"] >= 0

    def test_files_written_to_disk(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        data = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot").json()
        for f in data["files"]:
            assert Path(f["path"]).is_file(), f"Expected {f['path']} to exist"

    def test_apply_state_artifacts_under_correct_dir(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        data = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot").json()
        apply_state = apps_dir / slug / ".apply-state"
        for f in data["files"]:
            if f["kind"] in _APPLY_STATE_FILENAMES:
                assert Path(f["path"]).parent == apply_state, (
                    f"Expected {f['kind']} under .apply-state/, got {f['path']}"
                )

    def test_slug_root_artifacts_at_slug_root(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        data = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot").json()
        slug_dir = apps_dir / slug
        for f in data["files"]:
            if f["kind"] in _SLUG_ROOT_FILENAMES:
                assert Path(f["path"]).parent == slug_dir, (
                    f"Expected {f['kind']} at slug root, got {f['path']}"
                )

    def test_bytes_written_matches_file_size(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        data = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot").json()
        for f in data["files"]:
            p = Path(f["path"])
            assert p.stat().st_size == f["bytes_written"]

    def test_total_bytes_is_sum(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        data = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot").json()
        assert data["total_bytes"] == sum(f["bytes_written"] for f in data["files"])


# ---------------------------------------------------------------------------
# 404 for missing run
# ---------------------------------------------------------------------------


class TestSnapshotNotFound:
    def test_404_for_unknown_run_id(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(f"/api/applications/{slug}/runs/no-such-run/snapshot")
        assert resp.status_code == 404, resp.text

    def test_404_detail_mentions_run_id(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(f"/api/applications/{slug}/runs/no-such-run/snapshot")
        assert "no-such-run" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# kinds filter
# ---------------------------------------------------------------------------


class TestSnapshotKindsFilter:
    def test_only_requested_kinds_written(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json={"kinds": ["jd-parsed", "fit-score"]},
        )
        assert resp.status_code == 200
        written_kinds = {f["kind"] for f in resp.json()["files"]}
        assert written_kinds == {"jd-parsed", "fit-score"}

    def test_empty_kinds_list_writes_nothing(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json={"kinds": []},
        )
        assert resp.status_code == 200
        assert resp.json()["files"] == []

    def test_single_kind_filter(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json={"kinds": ["prose-draft"]},
        )
        assert resp.status_code == 200
        written_kinds = {f["kind"] for f in resp.json()["files"]}
        assert written_kinds == {"prose-draft"}


# ---------------------------------------------------------------------------
# target selector
# ---------------------------------------------------------------------------


class TestSnapshotTarget:
    def test_apply_state_target_only_writes_apply_state_artifacts(
        self, client_snapshot
    ) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json={"target": "apply-state"},
        )
        assert resp.status_code == 200
        written_kinds = {f["kind"] for f in resp.json()["files"]}
        # No slug-root artifact (variables) should appear
        assert "variables" not in written_kinds
        # At least one apply-state artifact should be there
        assert len(written_kinds) > 0

    def test_slug_root_target_only_writes_slug_root_artifacts(
        self, client_snapshot
    ) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json={"target": "slug-root"},
        )
        assert resp.status_code == 200
        written_kinds = {f["kind"] for f in resp.json()["files"]}
        # No apply-state artifacts should appear
        for kind in written_kinds:
            assert kind in _SLUG_ROOT_FILENAMES, f"Unexpected kind {kind!r}"

    def test_both_target_writes_all(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        resp = tc.post(
            f"/api/applications/{slug}/runs/{run_id}/snapshot",
            json={"target": "both"},
        )
        assert resp.status_code == 200
        written_kinds = {f["kind"] for f in resp.json()["files"]}
        # Expect at least one from each tree
        has_apply_state = any(k in _APPLY_STATE_FILENAMES for k in written_kinds)
        has_slug_root = any(k in _SLUG_ROOT_FILENAMES for k in written_kinds)
        assert has_apply_state
        assert has_slug_root

    def test_default_target_is_both(self, client_snapshot) -> None:
        tc, apps_dir, slug, run_id = client_snapshot
        # No body → defaults to target='both'
        resp = tc.post(f"/api/applications/{slug}/runs/{run_id}/snapshot")
        assert resp.status_code == 200
        written_kinds = {f["kind"] for f in resp.json()["files"]}
        has_apply_state = any(k in _APPLY_STATE_FILENAMES for k in written_kinds)
        has_slug_root = any(k in _SLUG_ROOT_FILENAMES for k in written_kinds)
        assert has_apply_state
        assert has_slug_root


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "output.json"
        n = _atomic_write(dest, b'{"key": "value"}')
        assert dest.exists()
        assert n == len(b'{"key": "value"}')

    def test_atomic_write_content_correct(self, tmp_path: Path) -> None:
        dest = tmp_path / "data.json"
        data = b'{"hello": "world"}\n'
        _atomic_write(dest, data)
        assert dest.read_bytes() == data

    def test_atomic_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        dest = tmp_path / "a" / "b" / "c.json"
        _atomic_write(dest, b"{}")
        assert dest.exists()

    def test_atomic_write_no_partial_on_failure(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.json"
        # Simulate a rename failure (e.g. cross-device move). The atomic write
        # must clean up the temp file and not leave a partial dest behind.
        with (
            patch("jobsmith.api.snapshots.os.replace", side_effect=OSError("EXDEV")),
            pytest.raises(OSError),
        ):
            _atomic_write(dest, b"data")
        # The destination must NOT exist (rename failed before the move)
        assert not dest.exists()
        # No temp file leftover either — the except branch unlinks it
        leftovers = list(tmp_path.glob(".snap-*"))
        assert leftovers == [], f"temp file leaked: {leftovers}"

    def test_atomic_write_overwrites_existing(self, tmp_path: Path) -> None:
        dest = tmp_path / "file.json"
        dest.write_bytes(b"old content")
        _atomic_write(dest, b"new content")
        assert dest.read_bytes() == b"new content"

    def test_no_temp_file_left_after_success(self, tmp_path: Path) -> None:
        dest = tmp_path / "clean.json"
        _atomic_write(dest, b"hello")
        # Only the destination file should remain; no .snap-* temp files
        leftovers = list(tmp_path.glob(".snap-*"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# Serialiser unit tests
# ---------------------------------------------------------------------------


class TestSerialiseArtifact:
    def test_json_kind_produces_valid_json(self) -> None:
        data = _serialise_artifact("jd-parsed", {"company": "Acme"})
        parsed = json.loads(data)
        assert parsed["company"] == "Acme"

    def test_text_kind_prose_draft(self) -> None:
        data = _serialise_artifact("prose-draft", {"text": "Hello world"})
        assert data == b"Hello world"

    def test_text_kind_missing_text_is_empty(self) -> None:
        data = _serialise_artifact("prose-draft", {})
        assert data == b""

    def test_hm_snippet_round_trip(self) -> None:
        payload = {
            "detected": True,
            "name": "Bob",
            "source": "linkedin_post",
            "one_specific_signal": "Loves Rust",
            "suggested_hook": None,
        }
        data = _serialise_artifact("hm-snippet", payload)
        text = data.decode()
        assert "detected: yes" in text
        assert "name: Bob" in text
        assert "suggested_hook: null" in text

    def test_hm_snippet_false_detected(self) -> None:
        data = _serialise_hm_snippet({"detected": False, "name": None})
        text = data.decode()
        assert "detected: no" in text
        assert "name: null" in text

    def test_variables_kind_produces_yaml(self) -> None:
        data = _serialise_artifact("variables", {"slug": "test", "company": "Acme"})
        # Should be valid YAML-ish content
        assert b"slug:" in data
        assert b"Acme" in data

    def test_fit_score_json_output(self) -> None:
        payload = {"score": 0.9, "rationale": "Great match"}
        data = _serialise_artifact("fit-score", payload)
        parsed = json.loads(data)
        assert parsed["score"] == pytest.approx(0.9)
