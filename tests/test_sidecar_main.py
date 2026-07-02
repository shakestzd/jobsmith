"""Unit tests for the desktop sidecar entry point (feat-b621a4ab, slice 1).

These tests exercise the env-preparation and port-selection helpers in
isolation — WITHOUT starting uvicorn — so the default suite stays fast.
"""

from __future__ import annotations

import io
from pathlib import Path

from jobsmith.desktop import sidecar_main


def test_prepare_env_pops_public_bind_and_dev(monkeypatch):
    monkeypatch.setenv("JOBSMITH_PUBLIC_BIND", "1")
    monkeypatch.setenv("JOBSMITH_DEV", "1")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    sidecar_main._prepare_env()

    assert "JOBSMITH_PUBLIC_BIND" not in __import__("os").environ
    assert "JOBSMITH_DEV" not in __import__("os").environ


def test_prepare_env_sets_desktop_flag(monkeypatch):
    monkeypatch.delenv("JOBSMITH_DESKTOP", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    sidecar_main._prepare_env()

    assert __import__("os").environ["JOBSMITH_DESKTOP"] == "1"


def test_prepare_env_sets_playwright_path_under_data_dir(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    data_dir = sidecar_main._prepare_env()

    pw = Path(__import__("os").environ["PLAYWRIGHT_BROWSERS_PATH"])
    assert pw == data_dir / "ms-playwright"
    assert pw.is_absolute()


def test_prepare_env_respects_existing_playwright_path(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom-browsers")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", custom)

    sidecar_main._prepare_env()

    # setdefault must NOT clobber a value supplied by the parent (Tauri/slice-3).
    assert __import__("os").environ["PLAYWRIGHT_BROWSERS_PATH"] == custom


def test_resolve_data_dir_is_jobsmith_path():
    data_dir = sidecar_main._resolve_data_dir()
    assert isinstance(data_dir, Path)
    assert data_dir.is_absolute()
    assert "jobsmith" in str(data_dir).lower()


def test_select_port_returns_bindable_int():
    port = sidecar_main._select_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535


def test_emit_port_writes_exact_sentinel_line():
    stream = io.StringIO()
    sidecar_main._emit_port(54321, stream=stream)
    assert "JOBSMITH_LISTENING_PORT=54321\n" in stream.getvalue()


def test_redact_token_filter_redacts_query_token():
    import logging

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1",
            "GET",
            "/api/x/events?verbosity=verbose&token=abc123-XYZ",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    assert sidecar_main._RedactTokenLogFilter().filter(record) is True
    # The secret value is gone; the redaction marker is present.
    assert "abc123-XYZ" not in record.args[2]
    assert "token=REDACTED" in record.args[2]
    # Non-string args (the integer status code) are passed through untouched.
    assert record.args[4] == 200


def test_redacting_log_config_attaches_filter_to_access_handler():
    config = sidecar_main._redacting_log_config()
    assert "redact_token" in config["filters"]
    assert "redact_token" in config["handlers"]["access"]["filters"]


# ---------------------------------------------------------------------------
# Regression: Finder-launch root resolution (feat-f4b197ac / roborev 1056)
# ---------------------------------------------------------------------------


def test_prepare_env_sets_jobsmith_repo_root(monkeypatch, tmp_path):
    """_prepare_env() must set JOBSMITH_REPO_ROOT to the platformdirs data dir.

    Simulates a Finder-launched .app where JOBSMITH_REPO_ROOT is absent:
    after _prepare_env() the env var must point at the resolved data dir so
    repo_root_for() tier-2 returns it rather than falling back to cwd ("/").
    """
    import os

    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    data_dir = sidecar_main._prepare_env()

    assert "JOBSMITH_REPO_ROOT" in os.environ
    assert Path(os.environ["JOBSMITH_REPO_ROOT"]) == data_dir
    assert data_dir.is_absolute()
    assert "jobsmith" in str(data_dir).lower()


def test_prepare_env_creates_data_dir(monkeypatch, tmp_path):
    """_prepare_env() must create the data dir so tier-2 is_dir() passes."""
    # Point platformdirs at a tmp location so we don't pollute the real store.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    data_dir = sidecar_main._prepare_env()

    assert data_dir.exists(), "_prepare_env must mkdir the data dir"
    assert data_dir.is_dir()


def test_prepare_env_respects_existing_repo_root(monkeypatch, tmp_path):
    """setdefault must NOT clobber a JOBSMITH_REPO_ROOT the caller already set."""
    import os

    custom_root = str(tmp_path / "my-custom-root")
    monkeypatch.setenv("JOBSMITH_REPO_ROOT", custom_root)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    sidecar_main._prepare_env()

    assert os.environ["JOBSMITH_REPO_ROOT"] == custom_root


def test_finder_launch_repo_root_resolution(monkeypatch, tmp_path):
    """Integration regression: simulates cwd='/' + JOBSMITH_DESKTOP=1.

    After _prepare_env() runs (as the sidecar binary would), repo_root_for()
    must return the platformdirs data dir — NOT '/' or a sample-data path.
    Also asserts the non-desktop path (JOBSMITH_REPO_ROOT unset, explicit cwd)
    still returns the cwd-fallback unchanged.
    """
    from importlib import reload
    from pathlib import Path

    import jobsmith.settings as settings_mod
    from jobsmith.paths import repo_root_for

    # --- Desktop / Finder branch ---
    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True)

    # Reload settings so XDG override takes effect (no persisted repo_root).
    reload(settings_mod)

    data_dir = sidecar_main._prepare_env()

    # JOBSMITH_REPO_ROOT is now set; repo_root_for with cwd='/' must return it.
    result = repo_root_for(cwd=Path("/"))
    assert result == data_dir, (
        f"Finder-launch: expected data_dir {data_dir!r}, got {result!r}"
    )
    assert result != Path("/"), "Must not fall back to filesystem root"

    # --- Non-desktop / CLI branch: clearing the env var restores legacy behaviour ---
    monkeypatch.delenv("JOBSMITH_REPO_ROOT", raising=False)
    # Re-reload settings to clear any cache.
    reload(settings_mod)  # noqa: PLE0605 — intentional second reload

    cli_cwd = tmp_path / "cli_workspace"
    cli_cwd.mkdir()
    cli_result = repo_root_for(cwd=cli_cwd)
    # No .apply-config.yaml, no env var, no settings — fallback is cwd itself.
    assert cli_result == cli_cwd, (
        f"CLI path must be unchanged; expected {cli_cwd!r}, got {cli_result!r}"
    )
