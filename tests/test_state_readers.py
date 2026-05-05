"""Unit tests for _state_readers.py — focused on orphaned artifact readers.

Tests for the 4 artifact kinds identified in the 0.8 audit as orphaned
(never reaching the DB via backfill): cover-letter-draft, _quarto.yml,
_variables.yml, and .agent.md snapshots.
"""
from __future__ import annotations

import json
from pathlib import Path  # noqa: I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Return (app_dir, state_dir) with both directories created."""
    app_dir = tmp_path / "acme-swe"
    state_dir = app_dir / ".apply-state"
    app_dir.mkdir(parents=True)
    state_dir.mkdir()
    return app_dir, state_dir


# ---------------------------------------------------------------------------
# Reader unit tests
# ---------------------------------------------------------------------------


class TestCoverLetterDraftReader:
    """cover-letter-draft.md lives at the slug root (app_dir), not .apply-state/."""

    def test_reads_cover_letter_draft_from_slug_root(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        app_dir, state_dir = _make_app_dir(tmp_path)
        cover_letter = "Dear Hiring Manager,\n\nI am applying for this role..."
        (app_dir / "cover-letter-draft.md").write_text(cover_letter)

        kind, reader = ARTIFACT_READERS["cover-letter-draft.md"]
        assert kind == "cover-letter-draft"
        result = reader(state_dir)
        assert result == cover_letter

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        kind, reader = ARTIFACT_READERS["cover-letter-draft.md"]
        assert reader(state_dir) is None


class TestQuartoYmlReader:
    """_quarto.yml lives at the slug root; reader wraps raw text in {'content': ...}."""

    def test_reads_quarto_yml_from_slug_root(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        app_dir, state_dir = _make_app_dir(tmp_path)
        quarto_content = "project:\n  type: website\nformat:\n  html: default\n"
        (app_dir / "_quarto.yml").write_text(quarto_content)

        kind, reader = ARTIFACT_READERS["_quarto.yml"]
        assert kind == "quarto-config"
        result = reader(state_dir)
        assert result == {"content": quarto_content}

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        kind, reader = ARTIFACT_READERS["_quarto.yml"]
        assert reader(state_dir) is None


class TestVariablesYmlReader:
    """_variables.yml lives at the slug root; reader parses YAML and returns a dict."""

    def test_reads_and_parses_variables_yml(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        app_dir, state_dir = _make_app_dir(tmp_path)
        variables_content = (
            "company: Acme Corp\n"
            "position: Software Engineer\n"
            "fit: 0.82\n"
        )
        (app_dir / "_variables.yml").write_text(variables_content)

        kind, reader = ARTIFACT_READERS["_variables.yml"]
        assert kind == "variables"
        result = reader(state_dir)
        assert result == {"company": "Acme Corp", "position": "Software Engineer", "fit": 0.82}

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        kind, reader = ARTIFACT_READERS["_variables.yml"]
        assert reader(state_dir) is None

    def test_returns_none_when_not_a_dict(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        app_dir, state_dir = _make_app_dir(tmp_path)
        (app_dir / "_variables.yml").write_text("- item1\n- item2\n")

        kind, reader = ARTIFACT_READERS["_variables.yml"]
        assert reader(state_dir) is None


class TestAgentMdSnapshotReaders:
    """.agent.md snapshots live inside .apply-state/; reader returns raw text."""

    def test_reads_prose_draft_agent_md(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        snapshot_text = "# Prose draft (agent snapshot)\n\nThis is the original draft."
        (state_dir / "prose-draft.agent.md").write_text(snapshot_text)

        assert "prose-draft.agent.md" in ARTIFACT_READERS, (
            "No reader registered for prose-draft.agent.md"
        )
        kind, reader = ARTIFACT_READERS["prose-draft.agent.md"]
        assert kind == "prose-draft-agent"
        result = reader(state_dir)
        assert result == snapshot_text

    def test_reads_cover_letter_draft_agent_md(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        snapshot_text = "Dear Hiring Manager,\n\nThis is the original cover letter."
        (state_dir / "cover-letter-draft.agent.md").write_text(snapshot_text)

        assert "cover-letter-draft.agent.md" in ARTIFACT_READERS, (
            "No reader registered for cover-letter-draft.agent.md"
        )
        kind, reader = ARTIFACT_READERS["cover-letter-draft.agent.md"]
        assert kind == "cover-letter-draft-agent"
        result = reader(state_dir)
        assert result == snapshot_text

    def test_prose_draft_agent_returns_none_when_absent(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        kind, reader = ARTIFACT_READERS["prose-draft.agent.md"]
        assert reader(state_dir) is None

    def test_cover_letter_draft_agent_returns_none_when_absent(self, tmp_path: Path) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS

        _, state_dir = _make_app_dir(tmp_path)
        kind, reader = ARTIFACT_READERS["cover-letter-draft.agent.md"]
        assert reader(state_dir) is None


# ---------------------------------------------------------------------------
# STANDALONE_ARTIFACTS registry tests
# ---------------------------------------------------------------------------


class TestStandaloneArtifactsRegistry:
    """STANDALONE_ARTIFACTS must list every artifact that has no specialist."""

    def test_standalone_artifacts_exists(self) -> None:
        from jobsmith._state_readers import STANDALONE_ARTIFACTS

        assert isinstance(STANDALONE_ARTIFACTS, (list, tuple, set, frozenset))

    def test_standalone_artifacts_covers_orphaned_kinds(self) -> None:
        from jobsmith._state_readers import STANDALONE_ARTIFACTS

        expected = {
            "cover-letter-draft.md",
            "_quarto.yml",
            "_variables.yml",
            "prose-draft.agent.md",
            "cover-letter-draft.agent.md",
        }
        missing = expected - set(STANDALONE_ARTIFACTS)
        assert not missing, f"STANDALONE_ARTIFACTS missing: {missing}"

    def test_every_standalone_has_reader(self) -> None:
        from jobsmith._state_readers import ARTIFACT_READERS, STANDALONE_ARTIFACTS

        for filename in STANDALONE_ARTIFACTS:
            assert filename in ARTIFACT_READERS, (
                f"STANDALONE_ARTIFACTS entry '{filename}' has no reader in ARTIFACT_READERS"
            )


# ---------------------------------------------------------------------------
# Integration: backfill picks up all 4 orphaned kinds
# ---------------------------------------------------------------------------


class TestBackfillStandaloneArtifacts:
    """ingest_standalone_artifacts writes DB rows for all orphaned kinds."""

    def _make_full_fixture(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Return (apps_dir, app_dir, state_dir) with all orphaned files present."""
        apps_dir = tmp_path / "applications"
        app_dir = apps_dir / "acme-swe"
        state_dir = app_dir / ".apply-state"
        state_dir.mkdir(parents=True)

        # Slug-root artifacts
        (app_dir / "cover-letter-draft.md").write_text(
            "Dear Hiring Manager,\n\nI am excited to apply."
        )
        (app_dir / "_quarto.yml").write_text(
            "project:\n  type: website\n"
        )
        (app_dir / "_variables.yml").write_text(
            "company: Acme Corp\nposition: Software Engineer\n"
        )

        # .agent.md snapshots inside .apply-state/
        (state_dir / "prose-draft.agent.md").write_text(
            "# Original prose draft\n\nAgent-written version."
        )
        (state_dir / "cover-letter-draft.agent.md").write_text(
            "Dear Manager,\n\nAgent-written cover letter."
        )

        # Minimal manifest so backfill_slug can find phases
        (state_dir / "manifest.json").write_text(json.dumps({
            "invocations": [],
        }))

        return apps_dir, app_dir, state_dir

    def test_ingest_standalone_artifacts_writes_all_orphaned_kinds(
        self, tmp_path: Path
    ) -> None:
        from jobsmith import db as jobsmith_db
        from jobsmith.db_ingest import ingest_standalone_artifacts

        apps_dir, app_dir, state_dir = self._make_full_fixture(tmp_path)

        conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
        run_id = "test-standalone-run"
        jobsmith_db.insert_apply_run(
            conn,
            run_id=run_id,
            slug="acme-swe",
            phase="render",
            started_at="2024-01-01T10:00:00",
            finished_at=None,
            status="backfilled",
        )

        inserted = ingest_standalone_artifacts(conn, run_id=run_id, state_dir=state_dir)
        rows = jobsmith_db.get_specialist_outputs(conn, run_id)
        conn.close()

        assert inserted >= 4, f"Expected >=4 rows for orphaned kinds, got {inserted}"
        kinds = {row["kind"] for row in rows}
        assert "cover-letter-draft" in kinds, "cover-letter-draft kind missing"
        assert "quarto-config" in kinds, "quarto-config kind missing"
        assert "variables" in kinds, "variables kind missing"
        assert "prose-draft-agent" in kinds or "cover-letter-draft-agent" in kinds, (
            ".agent.md snapshot kind missing"
        )

    def test_backfill_slug_includes_orphaned_kinds(self, tmp_path: Path) -> None:
        """End-to-end: backfill_slug against a fixture dir writes rows for all kinds."""
        from jobsmith import db as jobsmith_db
        from jobsmith.db_ingest import backfill_slug

        apps_dir, app_dir, state_dir = self._make_full_fixture(tmp_path)

        conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
        backfill_slug(conn, "acme-swe", apps_dir)
        rows = conn.execute("SELECT kind FROM specialist_outputs").fetchall()
        conn.close()

        kinds = {r[0] for r in rows}
        assert "cover-letter-draft" in kinds, (
            "backfill_slug must include cover-letter-draft kind"
        )
        assert "quarto-config" in kinds, (
            "backfill_slug must include quarto-config kind"
        )
        assert "variables" in kinds, (
            "backfill_slug must include variables kind"
        )
        # At least one .agent.md snapshot
        agent_kinds = {"prose-draft-agent", "cover-letter-draft-agent"}
        assert kinds & agent_kinds, (
            "backfill_slug must include at least one .agent.md snapshot kind"
        )

    def test_ingest_standalone_artifacts_idempotent(self, tmp_path: Path) -> None:
        """Running ingest_standalone_artifacts twice is a no-op (INSERT OR IGNORE)."""
        from jobsmith import db as jobsmith_db
        from jobsmith.db_ingest import ingest_standalone_artifacts

        apps_dir, app_dir, state_dir = self._make_full_fixture(tmp_path)

        conn = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
        run_id = "idempotent-run"
        jobsmith_db.insert_apply_run(
            conn,
            run_id=run_id,
            slug="acme-swe",
            phase="render",
            started_at="2024-01-01T10:00:00",
            finished_at=None,
            status="backfilled",
        )

        first = ingest_standalone_artifacts(conn, run_id=run_id, state_dir=state_dir)
        second = ingest_standalone_artifacts(conn, run_id=run_id, state_dir=state_dir)
        conn.close()

        assert first > 0
        assert second == 0, "Second call must be a no-op (INSERT OR IGNORE)"
