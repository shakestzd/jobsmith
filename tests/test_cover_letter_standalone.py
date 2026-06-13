"""Tests for the standalone, manually-triggered cover-letter step (feat-ebb7a7ee).

TDD protocol: written before the implementation. Covers:
- config: CoverLetterSettings.auto field, default False (opt-in)
- pipeline default flip: cover_letter=None + no explicit enable → CL specialists skipped
- manifest: remove_skipped_specialists strips only synthetic action=skipped entries
- core_run_cover_letter: validation failures exit 2 without spawning a phase;
  happy path runs the "cover-letter" phase and clears synthetic entries first
- CLI: `jobsmith cover-letter <slug>` command exists and forwards to the core
- API: POST /api/applications/{slug}/cover-letter launches a run (202) / 404
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. Config: auto field (opt-in default)
# ---------------------------------------------------------------------------


class TestCoverLetterAutoConfig:
    def test_auto_defaults_to_false(self) -> None:
        """Cover letter generation during apply is opt-in by default."""
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings()
        assert s.auto is False

    def test_auto_true_parses(self) -> None:
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings(auto=True)
        assert s.auto is True

    def test_auto_generate_requires_framework_and_auto(self) -> None:
        """auto_generate(): True only when auto=True AND framework != 'none'."""
        from jobsmith.config import CoverLetterSettings

        assert CoverLetterSettings(auto=True).auto_generate() is True
        assert CoverLetterSettings(auto=False).auto_generate() is False
        assert CoverLetterSettings(auto=True, framework="none").auto_generate() is False

    def test_cover_letter_enabled_unchanged(self) -> None:
        """cover_letter_enabled() keeps its framework-only semantics
        (used by the standalone trigger to know if CL is possible at all)."""
        from jobsmith.config import CoverLetterSettings

        assert CoverLetterSettings(auto=False).cover_letter_enabled() is True
        assert CoverLetterSettings(framework="none").cover_letter_enabled() is False


# ---------------------------------------------------------------------------
# 2. Pipeline default flip: None → opt-in (skip CL specialists)
# ---------------------------------------------------------------------------


class TestOptInDefault:
    def test_default_none_skips_cl_specialists(self, tmp_path: Path) -> None:
        """cover_letter=None with no config enable → CL specialists skipped.

        This is the behavioral flip from feat-bd7c2d23 (default-on) to
        opt-in: an apply run without --cover-letter does NOT generate one.
        """
        from jobsmith.core.pipeline import core_run_apply

        captured: dict = {}

        def fake_phase_runner(**kwargs):
            captured.update(kwargs)
            return 0

        with (
            patch("jobsmith.config.find_config", return_value=None),
            patch("jobsmith.core.pipeline.pipeline_db_path", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=None),
            patch("jobsmith.core.pipeline.load_url_index", return_value={}),
            patch(
                "jobsmith.core.pipeline.resolve_starting_slug",
                return_value=("test-slug", False),
            ),
            patch("jobsmith.core.pipeline.load_manifest", return_value=None),
        ):
            core_run_apply(
                "https://example.com/jobs/123",
                cwd=tmp_path,
                phase_runner=fake_phase_runner,
                cover_letter=None,
            )

        skip_specs = captured.get("skip_specialists", [])
        assert "apply-cover-letter-writer" in skip_specs
        assert "apply-company-research" in skip_specs

    def test_explicit_true_still_enables(self, tmp_path: Path) -> None:
        """--cover-letter (True) still wins over the opt-in default."""
        from jobsmith.core.pipeline import core_run_apply

        captured: dict = {}

        def fake_phase_runner(**kwargs):
            captured.update(kwargs)
            return 0

        with (
            patch("jobsmith.config.find_config", return_value=None),
            patch("jobsmith.core.pipeline.pipeline_db_path", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=None),
            patch("jobsmith.core.pipeline.load_url_index", return_value={}),
            patch(
                "jobsmith.core.pipeline.resolve_starting_slug",
                return_value=("test-slug", False),
            ),
            patch("jobsmith.core.pipeline.load_manifest", return_value=None),
        ):
            core_run_apply(
                "https://example.com/jobs/123",
                cwd=tmp_path,
                phase_runner=fake_phase_runner,
                cover_letter=True,
            )

        assert captured.get("skip_specialists") == []

    def test_config_auto_true_enables(self, tmp_path: Path) -> None:
        """cover_letter.auto: true in config restores automatic generation."""
        from jobsmith.core.pipeline import core_run_apply

        cfg = tmp_path / ".apply-config.yaml"
        cfg.write_text(
            "output:\n  jobsmith_db: jobsmith.db\n"
            "cover_letter:\n  auto: true\n",
            encoding="utf-8",
        )

        captured: dict = {}

        def fake_phase_runner(**kwargs):
            captured.update(kwargs)
            return 0

        with (
            patch("jobsmith.core.pipeline.pipeline_db_path", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=None),
            patch("jobsmith.core.pipeline.load_url_index", return_value={}),
            patch(
                "jobsmith.core.pipeline.resolve_starting_slug",
                return_value=("test-slug", False),
            ),
            patch("jobsmith.core.pipeline.load_manifest", return_value=None),
        ):
            core_run_apply(
                "https://example.com/jobs/123",
                cwd=tmp_path,
                phase_runner=fake_phase_runner,
                cover_letter=None,
            )

        assert captured.get("skip_specialists") == []


# ---------------------------------------------------------------------------
# 3. Manifest: remove_skipped_specialists
# ---------------------------------------------------------------------------


class TestRemoveSkippedSpecialists:
    def test_removes_synthetic_entries(self) -> None:
        from jobsmith.core.manifest import remove_skipped_specialists

        manifest = {
            "invocations": [
                {"specialist": "apply-jd-parser", "status": "ok"},
                {
                    "specialist": "apply-cover-letter-writer",
                    "status": "ok",
                    "action": "skipped",
                },
                {
                    "specialist": "apply-company-research",
                    "status": "ok",
                    "action": "skipped",
                },
            ]
        }
        out = remove_skipped_specialists(
            manifest, ["apply-cover-letter-writer", "apply-company-research"]
        )
        names = [inv["specialist"] for inv in out["invocations"]]
        assert names == ["apply-jd-parser"]

    def test_preserves_real_ok_entries(self) -> None:
        """A genuinely-run specialist (no action=skipped) is never removed."""
        from jobsmith.core.manifest import remove_skipped_specialists

        manifest = {
            "invocations": [
                {"specialist": "apply-company-research", "status": "ok"},
            ]
        }
        out = remove_skipped_specialists(manifest, ["apply-company-research"])
        assert len(out["invocations"]) == 1

    def test_tolerates_missing_invocations(self) -> None:
        from jobsmith.core.manifest import remove_skipped_specialists

        assert remove_skipped_specialists({}, ["x"]) == {"invocations": []}

    def test_roundtrip_with_inject(self) -> None:
        """inject → remove is a no-op on the invocations list."""
        from jobsmith.core.manifest import (
            inject_skipped_specialists,
            remove_skipped_specialists,
        )

        manifest = {"invocations": [{"specialist": "apply-jd-parser", "status": "ok"}]}
        inject_skipped_specialists(manifest, ["apply-cover-letter-writer"])
        remove_skipped_specialists(manifest, ["apply-cover-letter-writer"])
        names = [inv["specialist"] for inv in manifest["invocations"]]
        assert names == ["apply-jd-parser"]


# ---------------------------------------------------------------------------
# 4. core_run_cover_letter
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Minimal repo layout: config + DB + one completed application."""
    from jobsmith.db import open_pipeline_db, put_state

    cfg = tmp_path / ".apply-config.yaml"
    cfg.write_text(
        "output:\n"
        "  applications_dir: private/applications\n"
        "  jobsmith_db: private/jobsmith.db\n",
        encoding="utf-8",
    )
    (tmp_path / "private").mkdir()
    app_dir = tmp_path / "private" / "applications" / "acme-data-engineer"
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)
    (state_dir / "jd-parsed.json").write_text('{"company": "Acme"}', encoding="utf-8")

    db_path = tmp_path / "private" / "jobsmith.db"
    conn = open_pipeline_db(db_path)
    manifest = {
        "slug": "acme-data-engineer",
        "invocations": [
            {"specialist": "apply-jd-parser", "status": "ok"},
            {"specialist": "apply-fit-scorer", "status": "ok"},
            {"specialist": "apply-hm-enricher", "status": "ok"},
            {"specialist": "apply-bullet-selector", "status": "ok"},
            {"specialist": "apply-company-research", "status": "ok", "action": "skipped"},
            {"specialist": "apply-prose-writer", "status": "ok"},
            {"specialist": "apply-prose-qa", "status": "ok"},
            {"specialist": "apply-resume-renderer", "status": "ok"},
            {"specialist": "apply-cover-letter-writer", "status": "ok", "action": "skipped"},
            {"specialist": "apply-index-writer", "status": "ok"},
        ],
    }
    put_state(
        conn,
        slug="acme-data-engineer",
        kind="manifest",
        content_blob=json.dumps(manifest),
    )
    conn.close()
    return tmp_path


class TestCoreRunCoverLetter:
    def test_unknown_slug_exits_2_without_phase(self, tmp_path: Path) -> None:
        from jobsmith.core.pipeline import core_run_cover_letter

        _make_repo(tmp_path)
        calls: list = []

        rc = core_run_cover_letter(
            "no-such-slug",
            cwd=tmp_path,
            phase_runner=lambda **kw: calls.append(kw) or 0,
        )
        assert rc == 2
        assert calls == []

    def test_framework_none_exits_2(self, tmp_path: Path) -> None:
        from jobsmith.core.pipeline import core_run_cover_letter

        _make_repo(tmp_path)
        cfg = tmp_path / ".apply-config.yaml"
        cfg.write_text(
            cfg.read_text(encoding="utf-8") + "cover_letter:\n  framework: none\n",
            encoding="utf-8",
        )
        calls: list = []

        rc = core_run_cover_letter(
            "acme-data-engineer",
            cwd=tmp_path,
            phase_runner=lambda **kw: calls.append(kw) or 0,
        )
        assert rc == 2
        assert calls == []

    def test_missing_manifest_exits_2(self, tmp_path: Path) -> None:
        """An app dir without a manifest (never ran) cannot get a cover letter."""
        from jobsmith.core.pipeline import core_run_cover_letter

        _make_repo(tmp_path)
        orphan = tmp_path / "private" / "applications" / "orphan-app"
        orphan.mkdir()
        calls: list = []

        rc = core_run_cover_letter(
            "orphan-app",
            cwd=tmp_path,
            phase_runner=lambda **kw: calls.append(kw) or 0,
        )
        assert rc == 2
        assert calls == []

    def test_happy_path_runs_cover_letter_phase(self, tmp_path: Path) -> None:
        from jobsmith.core.pipeline import core_run_cover_letter

        _make_repo(tmp_path)
        captured: dict = {}

        def fake_phase_runner(**kwargs):
            captured.update(kwargs)
            return 0

        rc = core_run_cover_letter(
            "acme-data-engineer",
            cwd=tmp_path,
            phase_runner=fake_phase_runner,
        )
        assert rc == 0
        assert captured.get("phase_name") == "cover-letter"

    def test_synthetic_entries_cleared_before_phase(self, tmp_path: Path) -> None:
        """The action=skipped entries for both CL specialists are removed from
        the DB manifest BEFORE the phase runs, so the agent's manifest-based
        skip rule does not silently skip the work."""
        from jobsmith.core.pipeline import core_run_cover_letter
        from jobsmith.db import get_state, open_pipeline_db

        repo = _make_repo(tmp_path)
        db_path = repo / "private" / "jobsmith.db"
        seen_at_phase_time: dict = {}

        def fake_phase_runner(**kwargs):
            conn = open_pipeline_db(db_path)
            try:
                blob = get_state(conn, slug="acme-data-engineer", kind="manifest")
            finally:
                conn.close()
            seen_at_phase_time.update(json.loads(blob))
            return 0

        rc = core_run_cover_letter(
            "acme-data-engineer",
            cwd=tmp_path,
            phase_runner=fake_phase_runner,
        )
        assert rc == 0
        skipped = [
            inv
            for inv in seen_at_phase_time["invocations"]
            if inv.get("action") == "skipped"
        ]
        assert skipped == [], f"synthetic entries survived: {skipped}"
        # Real entries are untouched
        names = [inv["specialist"] for inv in seen_at_phase_time["invocations"]]
        assert "apply-jd-parser" in names
        assert "apply-resume-renderer" in names

    def test_phase_runner_failure_propagates(self, tmp_path: Path) -> None:
        from jobsmith.core.pipeline import core_run_cover_letter

        _make_repo(tmp_path)
        rc = core_run_cover_letter(
            "acme-data-engineer",
            cwd=tmp_path,
            phase_runner=lambda **kw: 1,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# 5. System prompt file exists for the new phase
# ---------------------------------------------------------------------------


class TestCoverLetterPhasePrompt:
    def test_prompt_file_ships_with_plugin(self) -> None:
        import jobsmith

        prompt = (
            Path(jobsmith.__file__).parent
            / "plugin"
            / "system-prompts"
            / "phase-4-cover-letter.md"
        )
        assert prompt.exists(), f"missing phase prompt: {prompt}"

    def test_prompt_scopes_to_cl_specialists(self) -> None:
        import jobsmith

        text = (
            Path(jobsmith.__file__).parent
            / "plugin"
            / "system-prompts"
            / "phase-4-cover-letter.md"
        ).read_text(encoding="utf-8")
        assert "apply-cover-letter-writer" in text
        assert "apply-company-research" in text
        # Must NOT be allowed to redo resume work
        assert "Do NOT invoke" in text


# ---------------------------------------------------------------------------
# 6. CLI command
# ---------------------------------------------------------------------------


class TestCliCoverLetterCommand:
    def test_command_registered(self) -> None:
        from typer.testing import CliRunner

        from jobsmith.cli import app

        result = CliRunner().invoke(app, ["cover-letter", "--help"])
        assert result.exit_code == 0
        assert "cover letter" in result.output.lower()

    def test_command_forwards_slug(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from jobsmith.cli import app

        with patch("jobsmith._cli_apply.run_cover_letter", return_value=0) as m:
            result = CliRunner().invoke(app, ["cover-letter", "acme-data-engineer"])
        assert result.exit_code == 0
        assert m.call_count == 1
        assert m.call_args.args[0] == "acme-data-engineer" or (
            m.call_args.kwargs.get("slug") == "acme-data-engineer"
        )


# ---------------------------------------------------------------------------
# 7. API route
# ---------------------------------------------------------------------------


class TestApiCoverLetterRoute:
    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from jobsmith.api.applications import router as applications_router

        repo = _make_repo(tmp_path)
        db_path = repo / "private" / "jobsmith.db"
        monkeypatch.setattr(
            "jobsmith.api.applications._get_db_path", lambda: db_path
        )
        app = FastAPI()
        app.include_router(applications_router, prefix="/api")
        return TestClient(app, raise_server_exceptions=True)

    def test_unknown_slug_404(self, client) -> None:
        resp = client.post("/api/applications/no-such-slug/cover-letter")
        assert resp.status_code == 404

    def test_launch_returns_run_id(self, client) -> None:
        async def fake_launch(*args, **kwargs):
            return "run-123"

        with patch(
            "jobsmith.api.applications._launch_cover_letter_run",
            side_effect=fake_launch,
        ) as m:
            resp = client.post(
                "/api/applications/acme-data-engineer/cover-letter"
            )
        assert resp.status_code == 202
        assert resp.json()["run_id"] == "run-123"
        assert m.call_count == 1
