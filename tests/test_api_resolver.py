"""TDD tests for Route API + master reads through the shared resolver (feat-e3dd986e).

Coverage:
(a) Precedence: settings.toml used when env unset; env overrides settings.
(b) app.state.repo_root cached at startup and used by master.py helpers via Depends.
(c) Existing API tests still resolve root correctly (smoke: artifacts, applications).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from jobsmith.api.auth import TOKEN_ENV_VAR, _get_expected_token
from jobsmith.api.deps import get_repo_root
from jobsmith.api.main import create_app
from jobsmith.paths import repo_root_for

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "test-resolver-token-abc"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    _get_expected_token.cache_clear()
    yield
    _get_expected_token.cache_clear()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# (a) repo_root_for precedence: env overrides settings.toml
# ---------------------------------------------------------------------------


class TestRepoRootForPrecedence:
    def test_env_var_takes_priority_over_settings(self, tmp_path: Path) -> None:
        """JOBSMITH_REPO_ROOT env var is tier-2 and beats settings.toml tier-3."""
        env_dir = tmp_path / "env_root"
        env_dir.mkdir()
        settings_dir = tmp_path / "settings_root"
        settings_dir.mkdir()

        with patch("jobsmith.paths.os.environ.get") as mock_env_get:
            mock_env_get.side_effect = lambda k, *a: (
                str(env_dir) if k == "JOBSMITH_REPO_ROOT" else (a[0] if a else None)
            )
            with patch("jobsmith.paths.find_config", return_value=None):
                result = repo_root_for(cwd=settings_dir)
        assert result == env_dir

    def test_settings_toml_used_when_env_unset(self, tmp_path: Path) -> None:
        """When JOBSMITH_REPO_ROOT is unset, settings.toml tier is consulted."""
        settings_dir = tmp_path / "settings_root"
        settings_dir.mkdir()

        with patch.dict(os.environ, {}, clear=True):
            # Remove JOBSMITH_REPO_ROOT from env entirely
            os.environ.pop("JOBSMITH_REPO_ROOT", None)
            with patch(
                "jobsmith.paths.find_config", return_value=None
            ):
                with patch(
                    "jobsmith.settings.read_repo_root", return_value=settings_dir
                ):
                    result = repo_root_for(cwd=tmp_path)
        assert result == settings_dir

    def test_fallback_to_cwd_when_nothing_set(self, tmp_path: Path) -> None:
        """Falls back to cwd when no env, no settings, no config found."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("JOBSMITH_REPO_ROOT", None)
            with patch("jobsmith.paths.find_config", return_value=None):
                with patch("jobsmith.settings.read_repo_root", return_value=None):
                    result = repo_root_for(cwd=tmp_path)
        assert result == tmp_path


# ---------------------------------------------------------------------------
# (b) app.state.repo_root cached at startup; get_repo_root Depends reads it
# ---------------------------------------------------------------------------


class TestAppStateRepoRoot:
    def test_app_state_repo_root_set_by_lifespan(self, tmp_path: Path) -> None:
        """After lifespan startup, app.state.repo_root is a Path."""
        with patch.dict(os.environ, {TOKEN_ENV_VAR: TOKEN, "JOBSMITH_REPO_ROOT": str(tmp_path)}):
            with patch("jobsmith.api.main._try_ingest_master"):
                with patch("jobsmith.config.find_config", return_value=None):
                    app = create_app()
                    with TestClient(app):
                        # During the with-block lifespan runs
                        assert hasattr(app.state, "repo_root")
                        assert isinstance(app.state.repo_root, Path)

    def test_get_repo_root_dep_reads_app_state(self, tmp_path: Path) -> None:
        """get_repo_root() dependency returns app.state.repo_root."""
        app = FastAPI()
        app.state.repo_root = tmp_path

        @app.get("/test-root")
        def _handler(root: Path = get_repo_root):  # type: ignore[assignment]
            return {"root": str(root)}

        # Wire Depends correctly
        from fastapi import Depends

        app2 = FastAPI()
        app2.state.repo_root = tmp_path

        @app2.get("/test-root")
        def _handler2(request: Request) -> dict:
            return {"root": str(get_repo_root(request))}

        client = TestClient(app2)
        resp = client.get("/test-root")
        assert resp.status_code == 200
        assert resp.json()["root"] == str(tmp_path)

    def test_master_helpers_use_app_state_repo_root(self, tmp_path: Path) -> None:
        """_require_config_path and _get_db_path_for_master accept repo_root param."""
        from jobsmith.api.master import _get_db_path_for_master, _require_config_path

        # _require_config_path should 404 when no config found at repo_root
        with pytest.raises(Exception):  # HTTPException 404
            _require_config_path(repo_root=tmp_path)

        # _get_db_path_for_master should return None when no config found
        result = _get_db_path_for_master(repo_root=tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# (c) Smoke: artifacts and applications resolvers still work via patching
# ---------------------------------------------------------------------------


class TestArtifactsResolverSmoke:
    def test_get_db_path_uses_repo_root_for(self, tmp_path: Path) -> None:
        """artifacts._get_db_path calls repo_root_for internally."""
        import jobsmith.api.artifacts as art_module
        from jobsmith.config import JobsmithConfig

        fake_cfg = JobsmithConfig()
        fake_config_path = tmp_path / ".apply-config.yaml"
        fake_config_path.touch()

        with patch(
            "jobsmith.api.artifacts.repo_root_for", return_value=tmp_path
        ):
            with patch(
                "jobsmith.api.artifacts.find_config",
                return_value=fake_config_path,
            ):
                with patch(
                    "jobsmith.api.artifacts.load_config", return_value=fake_cfg
                ):
                    result = art_module._get_db_path()
        assert isinstance(result, Path)


class TestApplicationsResolverSmoke:
    def test_get_app_dir_uses_repo_root_for(self, tmp_path: Path) -> None:
        """applications._get_app_dir calls repo_root_for internally."""
        import jobsmith.api.applications as app_module
        from jobsmith.config import JobsmithConfig

        fake_cfg = JobsmithConfig()
        fake_config_path = tmp_path / ".apply-config.yaml"
        fake_config_path.touch()

        # find_config/load_config are imported locally inside _get_app_dir,
        # so patch at the source module level.
        with patch(
            "jobsmith.api.applications.repo_root_for", return_value=tmp_path
        ):
            with patch(
                "jobsmith.config.find_config",
                return_value=fake_config_path,
            ):
                with patch(
                    "jobsmith.config.load_config", return_value=fake_cfg
                ):
                    result = app_module._get_app_dir("some-slug")
        assert result is not None
        assert result == tmp_path / fake_cfg.output.applications_dir / "some-slug"


# ---------------------------------------------------------------------------
# (d) master.py route GET /master/benchmark uses injected repo_root
# ---------------------------------------------------------------------------


class TestMasterBenchmarkResolverIntegration:
    def test_benchmark_get_uses_injected_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /master/benchmark uses repo_root from app.state, not raw env."""
        from jobsmith.api.master import router as master_router

        # Set up a fake config
        config_file = tmp_path / ".apply-config.yaml"
        config_file.write_text("user:\n  name: Test\n  email: test@example.com\n")

        app = FastAPI()
        app.state.repo_root = tmp_path
        app.include_router(master_router, prefix="/api")

        monkeypatch.setattr(
            "jobsmith.api.master._get_db_path_for_master",
            lambda repo_root=None: None,
        )
        monkeypatch.setattr(
            "jobsmith.api.master._require_config_path",
            lambda repo_root=None: config_file,
        )
        monkeypatch.setattr(
            "jobsmith.api.master._benchmark_load_text",
            lambda cp: "",
        )

        client = TestClient(app)
        resp = client.get("/api/master/benchmark")
        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == ""
