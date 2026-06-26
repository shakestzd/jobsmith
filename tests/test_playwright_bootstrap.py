"""Unit tests for the desktop Playwright Chromium bootstrap (feat-0c74180d).

The default suite never touches the network: ``status()`` is exercised against
a stub browser tree on disk. The real ``install()`` download is opt-in behind
``JOBSMITH_RUN_PW_INSTALL=1`` so CI/offline runs stay fast.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jobsmith.desktop import playwright_bootstrap as pw

_BROWSERS_ENV = "PLAYWRIGHT_BROWSERS_PATH"


def _stub_chromium(root: Path) -> Path:
    """Create a minimal ``chromium-<rev>/`` tree under *root*."""
    browser = root / "chromium-1097" / "chrome-linux"
    browser.mkdir(parents=True)
    (browser / "chrome").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def test_browsers_path_honours_env(monkeypatch, tmp_path):
    target = tmp_path / "ms-playwright"
    monkeypatch.setenv(_BROWSERS_ENV, str(target))
    assert pw.browsers_path() == target


def test_browsers_path_defaults_under_app_data(monkeypatch):
    monkeypatch.delenv(_BROWSERS_ENV, raising=False)
    path = pw.browsers_path()
    assert path.name == "ms-playwright"
    assert path.is_absolute()


def test_status_reports_not_installed_on_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(_BROWSERS_ENV, str(tmp_path / "ms-playwright"))
    snapshot = pw.status()
    assert snapshot["installed"] is False
    assert snapshot["path"] == str(tmp_path / "ms-playwright")


def test_status_reports_installed_when_chromium_tree_exists(monkeypatch, tmp_path):
    root = tmp_path / "ms-playwright"
    root.mkdir()
    _stub_chromium(root)
    monkeypatch.setenv(_BROWSERS_ENV, str(root))
    snapshot = pw.status()
    assert snapshot["installed"] is True
    assert snapshot["path"] == str(root)


def test_status_ignores_empty_chromium_dir(monkeypatch, tmp_path):
    root = tmp_path / "ms-playwright"
    (root / "chromium-1097").mkdir(parents=True)  # empty → aborted download
    monkeypatch.setenv(_BROWSERS_ENV, str(root))
    assert pw.status()["installed"] is False


def test_status_recognises_headless_shell_prefix(monkeypatch, tmp_path):
    root = tmp_path / "ms-playwright"
    leaf = root / "chromium_headless_shell-1097" / "chrome-linux"
    leaf.mkdir(parents=True)
    (leaf / "headless_shell").write_text("x", encoding="utf-8")
    monkeypatch.setenv(_BROWSERS_ENV, str(root))
    assert pw.status()["installed"] is True


def test_install_command_uses_current_interpreter():
    cmd = pw.install_command()
    assert cmd == [sys.executable, "-m", "playwright", "install", "chromium"]


def test_get_installer_is_singleton():
    assert pw.get_installer() is pw.get_installer()


@pytest.mark.skipif(
    os.environ.get("JOBSMITH_RUN_PW_INSTALL") != "1",
    reason="network-gated; set JOBSMITH_RUN_PW_INSTALL=1 to run the real download",
)
def test_install_downloads_chromium_into_path(monkeypatch, tmp_path):
    target = tmp_path / "ms-playwright"
    monkeypatch.setenv(_BROWSERS_ENV, str(target))
    assert pw.status()["installed"] is False
    result = pw.install()
    assert result["ok"], result["log"]
    # install path and runtime path are the SAME resolved dir (criterion 1).
    assert pw.status()["installed"] is True
    assert pw.browsers_path() == target
