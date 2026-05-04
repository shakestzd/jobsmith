"""Tests for Phase 1 dual-write: after FS write, orchestrator PUTs to DB via API.

Coverage
--------
- ARTIFACT_READERS extended with 4 slug-root kinds (cover-letter-draft,
  quarto-config, variables, manifest)
- dual_write_phase_artifacts PUTs each loaded artifact to the client
- JOBSMITH_DUAL_WRITE=0 skips all PUT calls
- PUT failure logs WARNING but does not raise / fail the phase
- All 4 newly-mapped kinds get PUT correctly
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobsmith._state_readers import ARTIFACT_READERS

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """Minimal .apply-state/ with apply-state artifacts."""
    sdir = tmp_path / ".apply-state"
    sdir.mkdir()

    (sdir / "jd-parsed.json").write_text(
        json.dumps({"company": "Acme", "position": "SWE"})
    )
    (sdir / "fit-score.json").write_text(
        json.dumps({"score": 0.9, "rationale": "Great match"})
    )
    (sdir / "prose-draft.md").write_text("Dear Hiring Manager,\n\nI am excited...")

    (sdir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "run-abc",
                "slug": "acme-swe",
                "started_at": "2025-01-01T00:00:00Z",
                "invocations": [],
            }
        )
    )
    return sdir


@pytest.fixture()
def app_dir(state_dir: Path) -> Path:
    """App directory (parent of .apply-state/) with slug-root artifacts."""
    adir = state_dir.parent
    (adir / "cover-letter-draft.md").write_text("Dear Hiring Manager,\n\nCover letter.")
    (adir / "_quarto.yml").write_text("project:\n  type: default\n")
    (adir / "_variables.yml").write_text("slug: acme-swe\ncompany: Acme\n")
    return adir


# ---------------------------------------------------------------------------
# ARTIFACT_READERS extension tests
# ---------------------------------------------------------------------------


class TestArtifactReadersExtension:
    """ARTIFACT_READERS covers the 4 slug-root kinds."""

    def test_cover_letter_draft_in_readers(self, app_dir: Path):
        """cover-letter-draft.md loads from app_dir (state_dir.parent)."""
        state_dir = app_dir / ".apply-state"
        entry = ARTIFACT_READERS.get("cover-letter-draft.md")
        assert entry is not None, "cover-letter-draft.md must be in ARTIFACT_READERS"
        kind, reader = entry
        assert kind == "cover-letter-draft"
        result = reader(state_dir)
        assert result is not None
        # Text artifacts wrap as {"text": ...} or return raw str
        if isinstance(result, dict):
            assert "text" in result
            assert "Cover letter" in result["text"]
        else:
            assert "Cover letter" in result

    def test_quarto_yml_in_readers(self, app_dir: Path):
        """_quarto.yml loads from app_dir (state_dir.parent)."""
        state_dir = app_dir / ".apply-state"
        entry = ARTIFACT_READERS.get("_quarto.yml")
        assert entry is not None, "_quarto.yml must be in ARTIFACT_READERS"
        kind, reader = entry
        assert kind == "quarto-config"
        result = reader(state_dir)
        assert result is not None

    def test_variables_yml_in_readers(self, app_dir: Path):
        """_variables.yml loads from app_dir (state_dir.parent)."""
        state_dir = app_dir / ".apply-state"
        entry = ARTIFACT_READERS.get("_variables.yml")
        assert entry is not None, "_variables.yml must be in ARTIFACT_READERS"
        kind, reader = entry
        assert kind == "variables"
        result = reader(state_dir)
        assert result is not None

    def test_manifest_json_in_readers(self, state_dir: Path):
        """manifest.json loads from state_dir."""
        entry = ARTIFACT_READERS.get("manifest.json")
        assert entry is not None, "manifest.json must be in ARTIFACT_READERS"
        kind, reader = entry
        assert kind == "manifest"
        result = reader(state_dir)
        assert result is not None
        assert isinstance(result, dict)
        assert result.get("slug") == "acme-swe"

    def test_readers_return_none_when_files_absent(self, tmp_path: Path):
        """All 4 new readers return None when the source file is missing."""
        empty_state = tmp_path / ".apply-state"
        empty_state.mkdir()
        for filename in ("cover-letter-draft.md", "_quarto.yml", "_variables.yml", "manifest.json"):
            entry = ARTIFACT_READERS.get(filename)
            if entry is None:
                continue  # will fail in other tests if truly absent
            _, reader = entry
            result = reader(empty_state)
            assert result is None, f"{filename} reader must return None when file absent"


# ---------------------------------------------------------------------------
# dual_write_phase_artifacts tests
# ---------------------------------------------------------------------------


class TestDualWritePhaseArtifacts:
    """dual_write_phase_artifacts PUTs artifacts to the client after each phase."""

    def _make_mock_client(self) -> MagicMock:
        client = MagicMock()
        client.put_artifact.return_value = MagicMock()
        return client

    def test_put_called_for_each_loaded_artifact(
        self, app_dir: Path, state_dir: Path
    ):
        """For each artifact that loads non-None, put_artifact is called once."""
        from jobsmith.apply import dual_write_phase_artifacts

        mock_client = self._make_mock_client()
        slug = "acme-swe"
        run_id = "run-001"

        dual_write_phase_artifacts(
            client=mock_client,
            slug=slug,
            run_id=run_id,
            state_dir=state_dir,
        )

        assert mock_client.put_artifact.call_count >= 1
        # Verify call signature: (slug, run_id, kind, output)
        for c in mock_client.put_artifact.call_args_list:
            args = c.args
            assert args[0] == slug
            assert args[1] == run_id
            assert isinstance(args[2], str)  # kind
            # output must be a dict (text artifacts wrapped already)
            assert isinstance(args[3], dict)

    def test_slug_root_kinds_are_put(self, app_dir: Path, state_dir: Path):
        """cover-letter-draft, quarto-config, variables, manifest are PUT."""
        from jobsmith.apply import dual_write_phase_artifacts

        mock_client = self._make_mock_client()
        dual_write_phase_artifacts(
            client=mock_client,
            slug="acme-swe",
            run_id="run-002",
            state_dir=state_dir,
        )

        put_kinds = {c.args[2] for c in mock_client.put_artifact.call_args_list}
        # At least the slug-root kinds present in fixtures should be PUT
        assert "cover-letter-draft" in put_kinds
        assert "quarto-config" in put_kinds
        assert "variables" in put_kinds

    def test_put_failure_logs_warning_not_raise(
        self, app_dir: Path, state_dir: Path, caplog
    ):
        """When put_artifact raises, a WARNING is logged and no exception propagates."""
        from jobsmith.apply import dual_write_phase_artifacts

        mock_client = self._make_mock_client()
        mock_client.put_artifact.side_effect = RuntimeError("network error")

        with caplog.at_level(logging.WARNING, logger="jobsmith.apply"):
            # Must not raise
            dual_write_phase_artifacts(
                client=mock_client,
                slug="acme-swe",
                run_id="run-003",
                state_dir=state_dir,
            )

        # At least one WARNING should have been emitted
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1

    def test_no_put_when_artifact_absent(self, tmp_path: Path):
        """When all artifacts are missing, put_artifact is never called."""
        from jobsmith.apply import dual_write_phase_artifacts

        empty_state = tmp_path / ".apply-state"
        empty_state.mkdir()

        mock_client = self._make_mock_client()
        dual_write_phase_artifacts(
            client=mock_client,
            slug="empty-slug",
            run_id="run-004",
            state_dir=empty_state,
        )

        mock_client.put_artifact.assert_not_called()


# ---------------------------------------------------------------------------
# JOBSMITH_DUAL_WRITE env var gate tests
# ---------------------------------------------------------------------------


class TestDualWriteEnvGate:
    """JOBSMITH_DUAL_WRITE=0 disables all PUT calls."""

    def test_dual_write_enabled_by_default(
        self, app_dir: Path, state_dir: Path
    ):
        """Without JOBSMITH_DUAL_WRITE set, dual-write proceeds."""
        from jobsmith.apply import dual_write_phase_artifacts

        mock_client = MagicMock()
        mock_client.put_artifact.return_value = MagicMock()

        env_without_gate = {k: v for k, v in os.environ.items() if k != "JOBSMITH_DUAL_WRITE"}
        with patch.dict(os.environ, env_without_gate, clear=True):
            # Write a file so at least one PUT occurs
            dual_write_phase_artifacts(
                client=mock_client,
                slug="acme-swe",
                run_id="run-005",
                state_dir=state_dir,
            )

        # Should have been called (files exist in app_dir fixture)
        assert mock_client.put_artifact.call_count >= 1

    def test_dual_write_disabled_when_env_zero(
        self, app_dir: Path, state_dir: Path
    ):
        """JOBSMITH_DUAL_WRITE=0 skips all PUT calls."""
        from jobsmith.apply import dual_write_phase_artifacts

        mock_client = MagicMock()

        with patch.dict(os.environ, {"JOBSMITH_DUAL_WRITE": "0"}):
            dual_write_phase_artifacts(
                client=mock_client,
                slug="acme-swe",
                run_id="run-006",
                state_dir=state_dir,
            )

        mock_client.put_artifact.assert_not_called()

    def test_dual_write_enabled_when_env_one(
        self, app_dir: Path, state_dir: Path
    ):
        """JOBSMITH_DUAL_WRITE=1 (explicit) enables dual-write."""
        from jobsmith.apply import dual_write_phase_artifacts

        mock_client = MagicMock()
        mock_client.put_artifact.return_value = MagicMock()

        with patch.dict(os.environ, {"JOBSMITH_DUAL_WRITE": "1"}):
            dual_write_phase_artifacts(
                client=mock_client,
                slug="acme-swe",
                run_id="run-007",
                state_dir=state_dir,
            )

        assert mock_client.put_artifact.call_count >= 1


# ---------------------------------------------------------------------------
# _run_apply_phases integration: dual-write is wired in
# ---------------------------------------------------------------------------


class TestRunApplyPhasesDualWrite:
    """Verify _run_apply_phases calls dual_write_phase_artifacts after phase_complete."""

    def test_dual_write_called_after_phase_complete(self, tmp_path: Path):
        """After each phase_complete, dual_write_phase_artifacts is called with client."""
        import jobsmith.apply as apply_mod

        mock_client = MagicMock()
        mock_client.put_artifact.return_value = MagicMock()

        call_record: list[str] = []

        def fake_dual_write(*, client, slug, run_id, state_dir):
            call_record.append(slug)

        phase_complete_events = [
            MagicMock(type="phase_complete"),
        ]

        with (
            patch.object(apply_mod, "dual_write_phase_artifacts", side_effect=fake_dual_write),
            patch.object(apply_mod, "_build_client_if_enabled", return_value=mock_client),
            patch.object(apply_mod.headless, "run_phase", return_value=iter(phase_complete_events)),
            patch.object(apply_mod, "_run_step45_orchestration", return_value=0),
            patch.object(apply_mod, "_snapshot_phase_drafts"),
            patch.object(apply_mod, "_reconcile_canonical_slug", return_value=("acme-swe", False)),
            patch.object(apply_mod, "_apply_state_dir", return_value=tmp_path / ".apply-state"),
            patch.object(apply_mod, "_applications_dir", return_value=tmp_path / "apps"),
            patch.object(apply_mod, "_get_or_create_session_id", return_value="sess-001"),
            patch.object(apply_mod, "_build_paths", return_value={}),
            patch.object(apply_mod, "build_phase_prompt", return_value="prompt"),
            patch.object(apply_mod, "get_plugin_dir", return_value=tmp_path / "plugins"),
        ):
            # patch system_prompt existence check
            system_prompt_path = tmp_path / "plugins" / "system-prompts" / "phase-1-gather.md"
            system_prompt_path.parent.mkdir(parents=True, exist_ok=True)
            system_prompt_path.write_text("# Gather")

            # minimal renderer mock
            rdr = MagicMock()
            rdr.print_header = MagicMock()

            apply_mod._run_apply_phases(
                url="https://example.com/job",
                resolved_cwd=tmp_path,
                rdr=rdr,
                plugin_directory=tmp_path / "plugins",
                slug="acme-swe",
                apps_dir=tmp_path / "apps",
                session_id="sess-001",
                phase_done={"gather": False, "draft": True, "render": True},
                total_phases=3,
                skip_confirm=True,
                started_at=0.0,
                db_conn=None,
                db_run_id="run-xxx",
                db_slug_ref=["acme-swe"],
                jd_text_file=None,
            )

        # dual_write_phase_artifacts was called at least once (for gather)
        assert len(call_record) >= 1
        assert "acme-swe" in call_record
