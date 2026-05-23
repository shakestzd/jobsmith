"""Tests for `jobsmith onboard` command + pipeline scaffold (feat-19e2d594).

TDD Protocol: tests written FIRST, implementation follows.

Coverage:
  (a) clobber guard: aborts when masters non-empty, proceeds with --force/--merge
  (b) repo bootstrap: scaffolds .apply-config.yaml when absent
  (c) extracted init scaffold lib fn produces same result as `jobsmith init`
  (d) input flags accepted and artifacts land in .onboard-state/
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from jobsmith.cli import app
from jobsmith._init import scaffold_repo


# ---------------------------------------------------------------------------
# (c) Extracted init scaffold lib fn
# ---------------------------------------------------------------------------


class TestScaffoldRepoLibFn:
    """scaffold_repo() produces the same layout that `jobsmith init` does."""

    def test_creates_apply_config_yaml(self, tmp_path: Path):
        scaffold_repo(tmp_path)
        assert (tmp_path / ".apply-config.yaml").exists()

    def test_creates_assets_content_dir(self, tmp_path: Path):
        scaffold_repo(tmp_path)
        assert (tmp_path / "assets" / "content").is_dir()

    def test_creates_private_applications_dir(self, tmp_path: Path):
        scaffold_repo(tmp_path)
        assert (tmp_path / "private" / "applications").is_dir()

    def test_creates_profile_yaml(self, tmp_path: Path):
        scaffold_repo(tmp_path)
        assert (tmp_path / "private" / "capacity" / "profile.yaml").exists()

    def test_creates_gitignore(self, tmp_path: Path):
        scaffold_repo(tmp_path)
        assert (tmp_path / ".gitignore").exists()

    def test_idempotent_when_called_twice(self, tmp_path: Path):
        scaffold_repo(tmp_path)
        # Second call should not raise or overwrite
        scaffold_repo(tmp_path)
        assert (tmp_path / ".apply-config.yaml").exists()

    def test_does_not_overwrite_existing_config_by_default(self, tmp_path: Path):
        """Without force=True, existing .apply-config.yaml is left unchanged."""
        config = tmp_path / ".apply-config.yaml"
        config.write_text("# existing\n")
        scaffold_repo(tmp_path)
        assert config.read_text() == "# existing\n"

    def test_force_overwrites_existing_config(self, tmp_path: Path):
        """With force=True, existing .apply-config.yaml is overwritten."""
        config = tmp_path / ".apply-config.yaml"
        config.write_text("# existing\n")
        scaffold_repo(tmp_path, force=True)
        assert config.read_text() != "# existing\n"
        assert "master" in config.read_text()


# ---------------------------------------------------------------------------
# (b) Repo bootstrap: scaffolds .apply-config.yaml when absent
# ---------------------------------------------------------------------------


class TestOnboardRepoBootstrap:
    """Phase 0: ensure repo is bootstrapped before running onboard."""

    def test_onboard_bootstraps_when_no_config(self, tmp_path: Path):
        """When .apply-config.yaml absent, onboard scaffolds it first."""
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                ["onboard", "--resume-file", "/dev/null", "--repo-root", str(tmp_path)],
            )
        # config should have been scaffolded
        assert (tmp_path / ".apply-config.yaml").exists(), (
            f"Expected .apply-config.yaml to be scaffolded. Output: {result.output}"
        )

    def test_onboard_skips_bootstrap_when_config_exists(self, tmp_path: Path):
        """When .apply-config.yaml already exists, bootstrap is skipped."""
        (tmp_path / ".apply-config.yaml").write_text("# existing\n")
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            runner.invoke(
                app,
                ["onboard", "--resume-file", "/dev/null", "--repo-root", str(tmp_path)],
            )
        # config should be unchanged
        assert (tmp_path / ".apply-config.yaml").read_text() == "# existing\n"


# ---------------------------------------------------------------------------
# (a) Clobber guard
# ---------------------------------------------------------------------------


class TestClobberGuard:
    """When master YAMLs are non-empty, onboard aborts without --force/--merge."""

    def _make_non_empty_masters(self, repo_root: Path) -> None:
        """Seed non-empty master YAML stubs."""
        content = repo_root / "assets" / "content"
        content.mkdir(parents=True, exist_ok=True)
        (content / "work.yml").write_text("- title: Engineer\n  details: []\n")

    def _make_config(self, repo_root: Path) -> None:
        (repo_root / ".apply-config.yaml").write_text(
            "master:\n"
            "  work_yml: assets/content/work.yml\n"
            "  skill_yml: assets/content/skill.yml\n"
            "  education_yml: assets/content/education.yml\n"
            "  author_yml: assets/content/author.yml\n"
            "output:\n"
            "  applications_dir: private/applications\n"
        )

    def test_aborts_when_masters_non_empty(self, tmp_path: Path):
        """Should exit non-zero and print guidance when masters have content."""
        self._make_config(tmp_path)
        self._make_non_empty_masters(tmp_path)
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                ["onboard", "--resume-file", "/dev/null", "--repo-root", str(tmp_path)],
            )
        assert result.exit_code != 0
        assert "already" in result.output.lower() or "--force" in result.output or "--merge" in result.output

    def test_proceeds_with_force_flag(self, tmp_path: Path):
        """--force should bypass the clobber guard and proceed."""
        self._make_config(tmp_path)
        self._make_non_empty_masters(tmp_path)
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--resume-file", "/dev/null",
                    "--repo-root", str(tmp_path),
                    "--force",
                ],
            )
        mock_dispatch.assert_called_once()

    def test_proceeds_with_merge_flag(self, tmp_path: Path):
        """--merge should bypass the clobber guard and proceed."""
        self._make_config(tmp_path)
        self._make_non_empty_masters(tmp_path)
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--resume-file", "/dev/null",
                    "--repo-root", str(tmp_path),
                    "--merge",
                ],
            )
        mock_dispatch.assert_called_once()

    def test_empty_masters_do_not_trigger_guard(self, tmp_path: Path):
        """Empty YAML stubs (from init) should not trigger the clobber guard."""
        self._make_config(tmp_path)
        content = tmp_path / "assets" / "content"
        content.mkdir(parents=True, exist_ok=True)
        (content / "work.yml").write_text("# Populate me with your master content\n")
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                ["onboard", "--resume-file", "/dev/null", "--repo-root", str(tmp_path)],
            )
        mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# (d) Input flags + .onboard-state/ artifacts
# ---------------------------------------------------------------------------


class TestOnboardInputFlags:
    """All input flag variants are accepted; artifacts land in .onboard-state/."""

    def _make_config(self, repo_root: Path) -> None:
        (repo_root / ".apply-config.yaml").write_text(
            "master:\n"
            "  work_yml: assets/content/work.yml\n"
            "  skill_yml: assets/content/skill.yml\n"
            "  education_yml: assets/content/education.yml\n"
            "  author_yml: assets/content/author.yml\n"
            "output:\n"
            "  applications_dir: private/applications\n"
        )

    def test_resume_file_flag(self, tmp_path: Path):
        """--resume-file path is accepted and passed through."""
        self._make_config(tmp_path)
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"fake pdf")
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--resume-file", str(resume),
                    "--repo-root", str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs.get("resume_file") == resume

    def test_linkedin_url_flag(self, tmp_path: Path):
        """--linkedin-url is accepted."""
        self._make_config(tmp_path)
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--linkedin-url", "https://linkedin.com/in/testuser",
                    "--repo-root", str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs.get("linkedin_url") == "https://linkedin.com/in/testuser"

    def test_paste_flag(self, tmp_path: Path):
        """--paste text is accepted."""
        self._make_config(tmp_path)
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--paste", "Some resume text",
                    "--repo-root", str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs.get("paste") == "Some resume text"

    def test_paste_file_flag(self, tmp_path: Path):
        """--paste-file path is accepted."""
        self._make_config(tmp_path)
        paste_file = tmp_path / "paste.txt"
        paste_file.write_text("Some resume text from file")
        runner = CliRunner()
        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline") as mock_dispatch:
            mock_dispatch.return_value = 0
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--paste-file", str(paste_file),
                    "--repo-root", str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        call_kwargs = mock_dispatch.call_args.kwargs
        assert call_kwargs.get("paste_file") == paste_file

    def test_onboard_state_dir_created(self, tmp_path: Path):
        """After a dispatch call, .onboard-state/ should be created."""
        self._make_config(tmp_path)
        runner = CliRunner()

        def fake_dispatch(**kwargs):
            # Simulate pipeline creating .onboard-state/
            state_dir = kwargs["repo_root"] / ".onboard-state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "run.json").write_text('{"status": "started"}')
            return 0

        with patch("jobsmith.onboard.pipeline.dispatch_onboard_pipeline", side_effect=fake_dispatch):
            result = runner.invoke(
                app,
                [
                    "onboard",
                    "--resume-file", "/dev/null",
                    "--repo-root", str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".onboard-state").is_dir()


# ---------------------------------------------------------------------------
# Onboard state path helper
# ---------------------------------------------------------------------------


class TestOnboardStatePath:
    """core/paths.py exposes onboard_state_dir() parallel to apply_state_dir."""

    def test_onboard_state_dir_helper_exists(self):
        """onboard_state_dir must be importable from jobsmith.core.paths."""
        from jobsmith.core.paths import onboard_state_dir  # noqa: F401

    def test_onboard_state_dir_returns_path(self, tmp_path: Path):
        from jobsmith.core.paths import onboard_state_dir

        path = onboard_state_dir(tmp_path)
        # Returns a Path under tmp_path (may not yet exist)
        assert isinstance(path, Path)
        assert str(tmp_path) in str(path)

    def test_onboard_state_dir_name(self, tmp_path: Path):
        """The returned path should use the .onboard-state directory name."""
        from jobsmith.core.paths import onboard_state_dir

        path = onboard_state_dir(tmp_path)
        assert ".onboard-state" in str(path)


# ---------------------------------------------------------------------------
# Pipeline module callable (API path stub)
# ---------------------------------------------------------------------------


class TestOnboardPipelineCallable:
    """The pipeline module exposes run_onboard_pipeline for slice-6 API use."""

    def test_run_onboard_pipeline_importable(self):
        from jobsmith.onboard.pipeline import run_onboard_pipeline  # noqa: F401

    def test_dispatch_onboard_pipeline_importable(self):
        from jobsmith.onboard.pipeline import dispatch_onboard_pipeline  # noqa: F401

    def test_run_onboard_pipeline_is_callable(self):
        from jobsmith.onboard.pipeline import run_onboard_pipeline

        assert callable(run_onboard_pipeline)

    def test_dispatch_onboard_pipeline_is_callable(self):
        from jobsmith.onboard.pipeline import dispatch_onboard_pipeline

        assert callable(dispatch_onboard_pipeline)
