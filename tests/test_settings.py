"""Tests for jobsmith.settings — user-level settings store (feat-f85f4815)."""
from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# settings module basics
# ---------------------------------------------------------------------------


def test_settings_importable():
    from jobsmith import settings  # noqa: F401


def test_settings_config_path_returns_path(tmp_path, monkeypatch):
    """settings_config_path() returns a Path under the OS config dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    p = settings_mod.settings_config_path()
    assert isinstance(p, Path)
    assert p.name == "settings.toml"


def test_read_settings_missing_file(tmp_path, monkeypatch):
    """read_settings() returns empty dict when settings file does not exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    result = settings_mod.read_settings()
    assert result == {}


def test_write_and_read_repo_root(tmp_path, monkeypatch):
    """write_repo_root() persists a path; read_settings() returns it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    settings_mod.write_repo_root(tmp_path / "myrepo")
    data = settings_mod.read_settings()
    assert "repo_root" in data
    assert data["repo_root"] == str(tmp_path / "myrepo")


def test_clear_repo_root(tmp_path, monkeypatch):
    """clear_repo_root() removes the repo_root key."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    settings_mod.write_repo_root(tmp_path / "myrepo")
    settings_mod.clear_repo_root()
    data = settings_mod.read_settings()
    assert "repo_root" not in data


def test_read_repo_root_helper(tmp_path, monkeypatch):
    """read_repo_root() returns Path when set, None when absent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    assert settings_mod.read_repo_root() is None
    settings_mod.write_repo_root(tmp_path / "repo")
    assert settings_mod.read_repo_root() == tmp_path / "repo"


# ---------------------------------------------------------------------------
# repo_root_for() precedence tiers
# ---------------------------------------------------------------------------


def test_repo_root_for_explicit_param_wins(tmp_path):
    """Tier 1: explicit path param wins over all others."""
    repo = tmp_path / "explicit"
    repo.mkdir()
    (repo / ".apply-config.yaml").touch()

    from jobsmith.paths import repo_root_for

    result = repo_root_for(repo_root=repo)
    assert result == repo


def test_repo_root_for_env_var_wins_over_settings(tmp_path, monkeypatch):
    """Tier 2: JOBSMITH_REPO_ROOT env var wins over settings.toml."""
    env_repo = tmp_path / "from_env"
    env_repo.mkdir()
    (env_repo / ".apply-config.yaml").touch()

    settings_repo = tmp_path / "from_settings"
    settings_repo.mkdir()
    (settings_repo / ".apply-config.yaml").touch()

    monkeypatch.setenv("JOBSMITH_REPO_ROOT", str(env_repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)
    settings_mod.write_repo_root(settings_repo)

    from jobsmith.paths import repo_root_for
    result = repo_root_for(cwd=tmp_path)
    assert result == env_repo


def test_repo_root_for_settings_toml_wins_over_walk(tmp_path, monkeypatch):
    """Tier 3: settings.toml repo_root wins over filesystem walk-up."""
    settings_repo = tmp_path / "from_settings"
    settings_repo.mkdir()
    (settings_repo / ".apply-config.yaml").touch()

    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)
    settings_mod.write_repo_root(settings_repo)

    cwd_no_config = tmp_path / "subdir"
    cwd_no_config.mkdir()

    from jobsmith.paths import repo_root_for
    result = repo_root_for(cwd=cwd_no_config)
    assert result == settings_repo


def test_repo_root_for_walk_up_fallback(tmp_path, monkeypatch):
    """Tier 4: filesystem walk-up finds .apply-config.yaml."""
    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".apply-config.yaml").touch()
    subdir = repo / "sub" / "deep"
    subdir.mkdir(parents=True)

    from jobsmith.paths import repo_root_for
    result = repo_root_for(cwd=subdir)
    assert result == repo


def test_repo_root_for_error_when_no_config(tmp_path, monkeypatch):
    """Tier 5: raises RepoRootNotFoundError when nothing resolves."""
    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    cwd = tmp_path / "empty"
    cwd.mkdir()

    from jobsmith.paths import RepoRootNotFoundError, repo_root_for
    with pytest.raises(RepoRootNotFoundError):
        repo_root_for(cwd=cwd, require=True)


def test_repo_root_for_no_error_mode(tmp_path, monkeypatch):
    """When require=False (default), missing config returns cwd-based fallback."""
    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    cwd = tmp_path / "empty"
    cwd.mkdir()

    from jobsmith.paths import repo_root_for
    # Should not raise, returns something (old compat behaviour)
    result = repo_root_for(cwd=cwd)
    assert isinstance(result, Path)


def test_repo_root_for_explicit_without_config_still_usable(tmp_path):
    """Explicit path that lacks .apply-config.yaml is accepted (caller's problem)."""
    repo = tmp_path / "bare"
    repo.mkdir()

    from jobsmith.paths import repo_root_for
    result = repo_root_for(repo_root=repo)
    assert result == repo


# ---------------------------------------------------------------------------
# CLI: config sub-commands
# ---------------------------------------------------------------------------


def test_cli_config_set_repo_root(tmp_path, monkeypatch):
    """jobsmith config set-repo-root <path> stores the path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    repo = tmp_path / "myrepo"
    repo.mkdir()

    from typer.testing import CliRunner

    from jobsmith.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["config", "set-repo-root", str(repo)])
    assert result.exit_code == 0, result.output

    assert settings_mod.read_repo_root() == repo


def test_cli_config_show(tmp_path, monkeypatch):
    """jobsmith config show prints repo_root when set."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    settings_mod.write_repo_root(tmp_path / "myrepo")

    from typer.testing import CliRunner

    from jobsmith.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "myrepo" in result.output


def test_cli_config_clear_repo_root(tmp_path, monkeypatch):
    """jobsmith config clear-repo-root removes the stored path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    from importlib import reload

    import jobsmith.settings as settings_mod
    reload(settings_mod)

    settings_mod.write_repo_root(tmp_path / "myrepo")

    from typer.testing import CliRunner

    from jobsmith.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["config", "clear-repo-root"])
    assert result.exit_code == 0

    assert settings_mod.read_repo_root() is None
