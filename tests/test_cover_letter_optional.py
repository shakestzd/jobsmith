"""Tests for optional cover-letter pipeline gate (feat-bd7c2d23).

TDD protocol: these tests were written before the implementation and cover:
- config: framework=none parses; default careerfair-io unchanged
- manifest: inject_skipped_specialists produces status=ok invocations
- manifest: phase_completed passes with skipped invocations
- pipeline: core_run_apply accepts cover_letter param
- pipeline: disabled run records skipped invocations, phases complete
- CLI: --no-cover-letter / --cover-letter flags accepted; default from config
- API: ApplicationCreate accepts cover_letter field; threads through _launch_run
- precedence: --cover-letter beats config none; --no-cover-letter beats config default
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. Config: framework=none parses; default unchanged
# ---------------------------------------------------------------------------


class TestCoverLetterConfig:
    def test_framework_none_parses(self) -> None:
        """framework='none' is accepted by CoverLetterSettings without error."""
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings(framework="none")
        assert s.framework == "none"

    def test_framework_default_is_careerfair_io(self) -> None:
        """Default framework is careerfair-io — existing behavior unchanged."""
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings()
        assert s.framework == "careerfair-io"

    def test_framework_none_means_disabled(self) -> None:
        """cover_letter_enabled() returns False when framework=='none'."""
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings(framework="none")
        assert s.cover_letter_enabled() is False

    def test_framework_careerfair_io_means_enabled(self) -> None:
        """cover_letter_enabled() returns True for careerfair-io."""
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings(framework="careerfair-io")
        assert s.cover_letter_enabled() is True

    def test_framework_minimal_means_enabled(self) -> None:
        """cover_letter_enabled() returns True for minimal."""
        from jobsmith.config import CoverLetterSettings

        s = CoverLetterSettings(framework="minimal")
        assert s.cover_letter_enabled() is True


# ---------------------------------------------------------------------------
# 2. Manifest: inject_skipped_specialists helper
# ---------------------------------------------------------------------------


class TestInjectSkippedSpecialists:
    """inject_skipped_specialists writes status=ok, action=skipped invocations."""

    def test_inject_adds_ok_invocations(self) -> None:
        """inject_skipped_specialists appends ok-status entries."""
        from jobsmith.core.manifest import inject_skipped_specialists

        manifest: dict = {"invocations": []}
        updated = inject_skipped_specialists(
            manifest,
            ["apply-company-research", "apply-cover-letter-writer"],
        )
        specialists = {inv["specialist"] for inv in updated["invocations"]}
        assert "apply-company-research" in specialists
        assert "apply-cover-letter-writer" in specialists

    def test_inject_sets_status_ok(self) -> None:
        from jobsmith.core.manifest import inject_skipped_specialists

        manifest: dict = {"invocations": []}
        updated = inject_skipped_specialists(manifest, ["apply-company-research"])
        inv = updated["invocations"][0]
        assert inv["status"] == "ok"

    def test_inject_sets_action_skipped(self) -> None:
        from jobsmith.core.manifest import inject_skipped_specialists

        manifest: dict = {"invocations": []}
        updated = inject_skipped_specialists(manifest, ["apply-company-research"])
        inv = updated["invocations"][0]
        assert inv.get("action") == "skipped"

    def test_inject_idempotent(self) -> None:
        """Calling twice does not duplicate entries."""
        from jobsmith.core.manifest import inject_skipped_specialists

        manifest: dict = {"invocations": []}
        m1 = inject_skipped_specialists(manifest, ["apply-company-research"])
        m2 = inject_skipped_specialists(m1, ["apply-company-research"])
        specialists = [inv["specialist"] for inv in m2["invocations"]]
        assert specialists.count("apply-company-research") == 1

    def test_inject_creates_invocations_key_when_missing(self) -> None:
        from jobsmith.core.manifest import inject_skipped_specialists

        manifest: dict = {}
        updated = inject_skipped_specialists(manifest, ["apply-cover-letter-writer"])
        assert "invocations" in updated

    def test_inject_preserves_existing_invocations(self) -> None:
        from jobsmith.core.manifest import inject_skipped_specialists

        existing = {"specialist": "apply-jd-parser", "status": "ok"}
        manifest: dict = {"invocations": [existing]}
        updated = inject_skipped_specialists(manifest, ["apply-company-research"])
        specialists = {inv["specialist"] for inv in updated["invocations"]}
        assert "apply-jd-parser" in specialists


# ---------------------------------------------------------------------------
# 3. Manifest: phase_completed passes with skipped invocations
# ---------------------------------------------------------------------------


class TestPhaseCompletedWithSkipped:
    def _full_gather_manifest(self) -> dict:
        """Build a manifest with all gather specialists ok (including skipped CL research)."""
        from jobsmith.core.manifest import PHASE_REQUIRED_SPECIALISTS

        invocations = [
            {"specialist": s, "status": "ok"}
            for s in PHASE_REQUIRED_SPECIALISTS["gather"]
        ]
        return {"invocations": invocations}

    def _full_render_manifest(self) -> dict:
        from jobsmith.core.manifest import PHASE_REQUIRED_SPECIALISTS

        invocations = [
            {"specialist": s, "status": "ok"}
            for s in PHASE_REQUIRED_SPECIALISTS["render"]
        ]
        return {"invocations": invocations}

    def test_gather_phase_completes_with_skipped_company_research(self) -> None:
        """phase_completed('gather') passes when apply-company-research is skipped."""
        from jobsmith.core.manifest import (
            PHASE_REQUIRED_SPECIALISTS,
            inject_skipped_specialists,
            phase_completed,
        )

        # All gather specialists except company-research
        other_gather = [
            s
            for s in PHASE_REQUIRED_SPECIALISTS["gather"]
            if s != "apply-company-research"
        ]
        manifest: dict = {"invocations": [{"specialist": s, "status": "ok"} for s in other_gather]}
        # Inject the synthetic skip
        manifest = inject_skipped_specialists(manifest, ["apply-company-research"])
        assert phase_completed(manifest, "gather") is True

    def test_render_phase_completes_with_skipped_cl_writer(self) -> None:
        """phase_completed('render') passes when apply-cover-letter-writer is skipped."""
        from jobsmith.core.manifest import (
            PHASE_REQUIRED_SPECIALISTS,
            inject_skipped_specialists,
            phase_completed,
        )

        other_render = [
            s
            for s in PHASE_REQUIRED_SPECIALISTS["render"]
            if s != "apply-cover-letter-writer"
        ]
        manifest: dict = {"invocations": [{"specialist": s, "status": "ok"} for s in other_render]}
        manifest = inject_skipped_specialists(manifest, ["apply-cover-letter-writer"])
        assert phase_completed(manifest, "render") is True


# ---------------------------------------------------------------------------
# 4. Pipeline: core_run_apply accepts cover_letter param
# ---------------------------------------------------------------------------


class TestCoreRunApplySignature:
    def test_accepts_cover_letter_param(self) -> None:
        """core_run_apply must accept cover_letter: bool | None kwarg."""
        import inspect

        from jobsmith.core.pipeline import core_run_apply

        sig = inspect.signature(core_run_apply)
        assert "cover_letter" in sig.parameters, (
            f"cover_letter not in core_run_apply params: {list(sig.parameters)}"
        )

    def test_cover_letter_defaults_to_none(self) -> None:
        """Default value of cover_letter param is None (meaning: use config)."""
        import inspect

        from jobsmith.core.pipeline import core_run_apply

        sig = inspect.signature(core_run_apply)
        default = sig.parameters["cover_letter"].default
        assert default is None


# ---------------------------------------------------------------------------
# 5. Pipeline: disabled run records skipped invocations
# ---------------------------------------------------------------------------


class TestCoverLetterSkipPipeline:
    """When cover_letter=False, the two CL-only specialists must be skipped."""

    def _make_db(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a minimal .apply-config.yaml + pipeline DB."""
        from jobsmith.db import open_pipeline_db

        config_path = tmp_path / ".apply-config.yaml"
        config_path.write_text("output:\n  jobsmith_db: jobsmith.db\n", encoding="utf-8")
        db_path = tmp_path / "jobsmith.db"
        open_pipeline_db(db_path).close()
        return config_path, db_path

    def test_skip_specialists_passed_to_phase_runner_when_disabled(self, tmp_path: Path) -> None:
        """When cover_letter=False, phase_runner receives the CL specialists in skip_specialists."""
        from jobsmith.core.pipeline import core_run_apply

        captured_kwargs: dict = {}

        def fake_phase_runner(**kwargs):
            captured_kwargs.update(kwargs)
            return 0

        self._make_db(tmp_path)

        # Patch heavy imports so no FS or DB scaffolding is needed
        with (
            patch("jobsmith.config.find_config", return_value=None),
            patch("jobsmith.core.pipeline.pipeline_db_path", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=None),
            patch("jobsmith.core.pipeline.load_url_index", return_value={}),
            patch("jobsmith.core.pipeline.resolve_starting_slug", return_value=("test-slug", False)),
            patch("jobsmith.core.pipeline.load_manifest", return_value=None),
        ):
            core_run_apply(
                "https://example.com/jobs/123",
                cwd=tmp_path,
                phase_runner=fake_phase_runner,
                cover_letter=False,
            )

        # phase_runner should have received skip_specialists with the two CL specialists
        skip_specs = captured_kwargs.get("skip_specialists", [])
        assert "apply-company-research" in skip_specs, (
            f"apply-company-research not in skip_specialists: {skip_specs}"
        )
        assert "apply-cover-letter-writer" in skip_specs, (
            f"apply-cover-letter-writer not in skip_specialists: {skip_specs}"
        )

    def test_skip_specialists_empty_when_cl_enabled(self, tmp_path: Path) -> None:
        """When cover_letter=True, phase_runner receives empty skip_specialists."""
        from jobsmith.core.pipeline import core_run_apply

        captured_kwargs: dict = {}

        def fake_phase_runner(**kwargs):
            captured_kwargs.update(kwargs)
            return 0

        self._make_db(tmp_path)

        with (
            patch("jobsmith.config.find_config", return_value=None),
            patch("jobsmith.core.pipeline.pipeline_db_path", return_value=None),
            patch("jobsmith.core.pipeline.applications_dir", return_value=None),
            patch("jobsmith.core.pipeline.load_url_index", return_value={}),
            patch("jobsmith.core.pipeline.resolve_starting_slug", return_value=("test-slug", False)),
            patch("jobsmith.core.pipeline.load_manifest", return_value=None),
        ):
            core_run_apply(
                "https://example.com/jobs/123",
                cwd=tmp_path,
                phase_runner=fake_phase_runner,
                cover_letter=True,
            )

        skip_specs = captured_kwargs.get("skip_specialists", [])
        assert skip_specs == [], (
            f"skip_specialists should be empty when cover_letter=True, got: {skip_specs}"
        )

    def test_manifest_injection_in_run_apply_phases(self, tmp_path: Path) -> None:
        """After a phase completes in _run_apply_phases, skipped specialists appear in manifest."""
        from jobsmith.core.manifest import inject_skipped_specialists, phase_completed

        # Simulate what happens in _run_apply_phases when skip_specialists is set:
        # the manifest on disk gains synthetic ok/skipped entries.
        state_dir = tmp_path / ".apply-state"
        state_dir.mkdir()

        # Build a manifest with all gather specialists EXCEPT company-research
        from jobsmith.core.manifest import PHASE_REQUIRED_SPECIALISTS

        other_gather = [
            s for s in PHASE_REQUIRED_SPECIALISTS["gather"] if s != "apply-company-research"
        ]
        manifest = {"invocations": [{"specialist": s, "status": "ok"} for s in other_gather]}
        manifest_path = state_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        # Simulate the inject step
        mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
        mdata = inject_skipped_specialists(mdata, ["apply-company-research"])
        manifest_path.write_text(json.dumps(mdata, indent=2), encoding="utf-8")

        # Verify phase_completed returns True now
        final = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert phase_completed(final, "gather") is True


# ---------------------------------------------------------------------------
# 6. CLI: --no-cover-letter / --cover-letter flags
# ---------------------------------------------------------------------------


class TestCliCoverLetterFlags:
    """The apply command must accept --no-cover-letter and --cover-letter flags."""

    def test_no_cover_letter_flag_accepted(self) -> None:
        """--no-cover-letter flag is in the apply command's option list."""
        import inspect

        from jobsmith.cli import apply as apply_cmd

        # Typer commands store options in the callback signature
        sig = inspect.signature(apply_cmd.callback if hasattr(apply_cmd, "callback") else apply_cmd)
        params = list(sig.parameters.keys())
        # The flag maps to either 'cover_letter' or 'no_cover_letter' in the sig
        assert any("cover_letter" in p.lower() for p in params), (
            f"No cover_letter param found in apply signature: {params}"
        )

    def test_cover_letter_kwarg_reaches_run_apply(self, tmp_path: Path) -> None:
        """When --no-cover-letter is passed, run_apply receives cover_letter=False."""
        from typer.testing import CliRunner

        from jobsmith import apply as apply_mod
        from jobsmith.cli import app

        runner = CliRunner()
        captured: dict = {}

        def fake_run_apply(url, **kwargs):
            captured.update(kwargs)
            return 0

        with patch.object(apply_mod, "run_apply", new=fake_run_apply):
            runner.invoke(app, ["apply", "--no-cover-letter", "https://example.com/job"])

        assert "cover_letter" in captured, (
            f"cover_letter kwarg not passed to run_apply. captured={captured!r}"
        )
        assert captured["cover_letter"] is False, (
            f"Expected cover_letter=False, got {captured['cover_letter']!r}"
        )

    def test_cover_letter_flag_passes_true(self, tmp_path: Path) -> None:
        """When --cover-letter is passed, run_apply receives cover_letter=True."""
        from typer.testing import CliRunner

        from jobsmith import apply as apply_mod
        from jobsmith.cli import app

        runner = CliRunner()
        captured: dict = {}

        def fake_run_apply(url, **kwargs):
            captured.update(kwargs)
            return 0

        with patch.object(apply_mod, "run_apply", new=fake_run_apply):
            runner.invoke(app, ["apply", "--cover-letter", "https://example.com/job"])

        assert captured.get("cover_letter") is True


# ---------------------------------------------------------------------------
# 7. CLI precedence: flags beat config
# ---------------------------------------------------------------------------


class TestCoverLetterPrecedence:
    def test_no_cover_letter_beats_default(self, tmp_path: Path) -> None:
        """--no-cover-letter forces cover_letter=False even if config enables it."""
        from typer.testing import CliRunner

        from jobsmith import apply as apply_mod
        from jobsmith.cli import app

        runner = CliRunner()
        captured: dict = {}

        def fake_run_apply(url, **kwargs):
            captured.update(kwargs)
            return 0

        # Default config has framework=careerfair-io (enabled), but flag overrides
        with (
            patch.object(apply_mod, "run_apply", new=fake_run_apply),
            patch("jobsmith.cli.find_config", return_value=None),
        ):
            runner.invoke(app, ["apply", "--no-cover-letter", "https://example.com/job"])

        assert captured.get("cover_letter") is False

    def test_cover_letter_flag_beats_config_none(self, tmp_path: Path) -> None:
        """--cover-letter forces cover_letter=True even if config has framework=none."""
        from typer.testing import CliRunner

        from jobsmith import apply as apply_mod
        from jobsmith.cli import app
        from jobsmith.config import CoverLetterSettings, JobsmithConfig

        runner = CliRunner()
        captured: dict = {}

        def fake_run_apply(url, **kwargs):
            captured.update(kwargs)
            return 0

        fake_config = JobsmithConfig()
        fake_config.cover_letter = CoverLetterSettings(framework="none")

        with (
            patch.object(apply_mod, "run_apply", new=fake_run_apply),
            patch("jobsmith.cli.find_config", return_value=tmp_path / ".apply-config.yaml"),
            patch("jobsmith.cli.load_config", return_value=fake_config),
        ):
            runner.invoke(app, ["apply", "--cover-letter", "https://example.com/job"])

        assert captured.get("cover_letter") is True

    def test_config_none_without_flag_disables_cl(self, tmp_path: Path) -> None:
        """When no CLI flag given and config has framework=none, cover_letter=False."""
        from typer.testing import CliRunner

        from jobsmith import apply as apply_mod
        from jobsmith.cli import app
        from jobsmith.config import CoverLetterSettings, JobsmithConfig

        runner = CliRunner()
        captured: dict = {}

        def fake_run_apply(url, **kwargs):
            captured.update(kwargs)
            return 0

        fake_config = JobsmithConfig()
        fake_config.cover_letter = CoverLetterSettings(framework="none")

        with (
            patch.object(apply_mod, "run_apply", new=fake_run_apply),
            patch("jobsmith.cli.find_config", return_value=tmp_path / ".apply-config.yaml"),
            patch("jobsmith.cli.load_config", return_value=fake_config),
        ):
            runner.invoke(app, ["apply", "https://example.com/job"])

        assert captured.get("cover_letter") is False


# ---------------------------------------------------------------------------
# 8. API: ApplicationCreate accepts cover_letter field
# ---------------------------------------------------------------------------


class TestApplicationCreateSchema:
    def test_cover_letter_field_exists(self) -> None:
        """ApplicationCreate must have a cover_letter: bool | None field."""
        from jobsmith.api.schemas.applications import ApplicationCreate

        obj = ApplicationCreate(url="https://example.com/job")
        assert hasattr(obj, "cover_letter"), "ApplicationCreate missing cover_letter field"

    def test_cover_letter_defaults_to_none(self) -> None:
        from jobsmith.api.schemas.applications import ApplicationCreate

        obj = ApplicationCreate(url="https://example.com/job")
        assert obj.cover_letter is None

    def test_cover_letter_false_accepted(self) -> None:
        from jobsmith.api.schemas.applications import ApplicationCreate

        obj = ApplicationCreate(url="https://example.com/job", cover_letter=False)
        assert obj.cover_letter is False

    def test_cover_letter_true_accepted(self) -> None:
        from jobsmith.api.schemas.applications import ApplicationCreate

        obj = ApplicationCreate(url="https://example.com/job", cover_letter=True)
        assert obj.cover_letter is True


# ---------------------------------------------------------------------------
# 9. API: cover_letter threads through _launch_run
# ---------------------------------------------------------------------------


class TestApiCoverLetterPropagation:
    """cover_letter=False on POST /api/applications must reach _launch_run."""

    _TOKEN = "test-cl-token-bd7c2d23"

    def _make_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Create a TestClient that mirrors the pattern in test_api_applications_post.py."""
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from jobsmith.api.applications import router
        from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token, verify_token
        from jobsmith.api.supervisor import RunSupervisor
        from jobsmith.db import open_pipeline_db

        db_path = tmp_path / "jobsmith.db"
        open_pipeline_db(db_path).close()

        monkeypatch.setenv(TOKEN_ENV_VAR, self._TOKEN)
        _get_expected_token.cache_clear()

        monkeypatch.setattr("jobsmith.api.applications._get_db_path", lambda: db_path)
        monkeypatch.setattr("jobsmith.api.applications.repo_root_for", lambda: tmp_path)

        supervisor = RunSupervisor(max_buffered_lines=100)

        fastapi_app = FastAPI()
        fastapi_app.include_router(router, prefix="/api", dependencies=[Depends(verify_token)])
        fastapi_app.state.run_supervisor = supervisor

        return TestClient(fastapi_app, raise_server_exceptions=True)

    def _auth_header(self):
        return {"Authorization": f"Bearer {self._TOKEN}"}

    def test_cover_letter_false_propagates_to_launch_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """body.cover_letter=False reaches _launch_run as cover_letter=False."""
        from jobsmith.api import applications as apps_mod
        from jobsmith.api.auth import _get_expected_token

        _get_expected_token.cache_clear()
        client = self._make_client(tmp_path, monkeypatch)
        captured: dict = {}

        async def fake_launch(supervisor, slug, url, cwd, force=False, jd_text=None,
                               start_from_phase=None, cover_letter=None):
            captured["cover_letter"] = cover_letter
            return "run-cl-001"

        with patch.object(apps_mod, "_launch_run", new=fake_launch):
            resp = client.post(
                "/api/applications",
                json={"url": "https://example.com/job", "cover_letter": False},
                headers=self._auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert captured.get("cover_letter") is False

    def test_cover_letter_none_propagates_as_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """body.cover_letter=None (default) reaches _launch_run as cover_letter=None."""
        from jobsmith.api import applications as apps_mod
        from jobsmith.api.auth import _get_expected_token

        _get_expected_token.cache_clear()
        client = self._make_client(tmp_path, monkeypatch)
        captured: dict = {}

        async def fake_launch(supervisor, slug, url, cwd, force=False, jd_text=None,
                               start_from_phase=None, cover_letter=None):
            captured["cover_letter"] = cover_letter
            return "run-cl-002"

        with patch.object(apps_mod, "_launch_run", new=fake_launch):
            resp = client.post(
                "/api/applications",
                json={"url": "https://example.com/job"},
                headers=self._auth_header(),
            )

        assert resp.status_code == 201, resp.text
        assert captured.get("cover_letter") is None
