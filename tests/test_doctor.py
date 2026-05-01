"""Tests for jobsmith.doctor — preflight environment checks."""

from __future__ import annotations

import shutil
import sys
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from jobsmith.doctor import (
    CheckResult,
    check_anthropic_api_key,
    check_apply_config,
    check_claude_binary,
    check_master_yaml,
    check_plugin_dir_resolves,
    check_python_version,
    preflight,
    run_all_checks,
)
from jobsmith.cli import app


# ---------------------------------------------------------------------------
# check_claude_binary
# ---------------------------------------------------------------------------

def test_check_claude_binary_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/claude")
    result = check_claude_binary()
    assert result.ok is True
    assert result.name == "claude_binary"
    assert "/fake/claude" in result.message


def test_check_claude_binary_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = check_claude_binary()
    assert result.ok is False
    assert result.remediation is not None
    assert "npm install" in result.remediation


# ---------------------------------------------------------------------------
# check_anthropic_api_key
# ---------------------------------------------------------------------------

def test_check_anthropic_api_key_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = check_anthropic_api_key()
    assert result.ok is True
    assert result.name == "anthropic_api_key"
    # Must not reveal the key
    assert "sk-test-key" not in result.message


def test_check_anthropic_api_key_fail_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = check_anthropic_api_key()
    assert result.ok is False
    assert result.remediation is not None
    assert "ANTHROPIC_API_KEY" in result.remediation


def test_check_anthropic_api_key_fail_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = check_anthropic_api_key()
    assert result.ok is False


# ---------------------------------------------------------------------------
# check_apply_config
# ---------------------------------------------------------------------------

def test_check_apply_config_pass(tmp_path: Path) -> None:
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("master:\n  work_yml: assets/content/work.yml\n")
    result = check_apply_config(cwd=tmp_path)
    assert result.ok is True
    assert result.name == "apply_config"


def test_check_apply_config_fail(tmp_path: Path) -> None:
    result = check_apply_config(cwd=tmp_path)
    assert result.ok is False
    assert result.remediation is not None
    assert "jobsmith init" in result.remediation


# ---------------------------------------------------------------------------
# check_master_yaml
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["work.yml", "work.yaml", ".work.yaml"])
def test_check_master_yaml_pass(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("# master work yaml\n")
    result = check_master_yaml(cwd=tmp_path)
    assert result.ok is True
    assert result.name == "master_yaml"
    assert filename in result.message


def test_check_master_yaml_fail_none_present(tmp_path: Path) -> None:
    result = check_master_yaml(cwd=tmp_path)
    assert result.ok is False
    assert result.remediation is not None
    assert "work.yml" in result.remediation or "jobsmith init" in result.remediation


# ---------------------------------------------------------------------------
# check_python_version
# ---------------------------------------------------------------------------

def test_check_python_version_pass_current() -> None:
    """Current interpreter must be >= 3.10 (enforced by pyproject.toml)."""
    result = check_python_version(min_major=3, min_minor=10)
    assert result.ok is True
    assert result.name == "python_version"


def test_check_python_version_fail_too_high() -> None:
    """Simulate fail by requiring an impossibly high minor version."""
    result = check_python_version(min_major=3, min_minor=99)
    assert result.ok is False
    assert result.remediation is not None
    assert "3.99" in result.remediation


# ---------------------------------------------------------------------------
# check_plugin_dir_resolves
# ---------------------------------------------------------------------------

def test_check_plugin_dir_resolves_pass() -> None:
    """On a real editable install the plugin dir must exist with plugin.json."""
    result = check_plugin_dir_resolves()
    assert result.ok is True
    assert result.name == "plugin_dir"


def test_check_plugin_dir_resolves_fail_missing_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Simulate a broken install where plugin_dir() returns a non-existent path."""
    import jobsmith

    fake_dir = tmp_path / "nonexistent_plugin"
    monkeypatch.setattr(jobsmith, "plugin_dir", lambda: fake_dir)
    result = check_plugin_dir_resolves()
    assert result.ok is False
    assert result.remediation is not None
    assert "reinstall" in result.remediation


def test_check_plugin_dir_resolves_fail_no_plugin_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Simulate plugin dir exists but is missing plugin.json."""
    import jobsmith

    fake_dir = tmp_path / "plugin"
    fake_dir.mkdir()
    monkeypatch.setattr(jobsmith, "plugin_dir", lambda: fake_dir)
    result = check_plugin_dir_resolves()
    assert result.ok is False
    assert "plugin.json" in result.message


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

def test_run_all_checks_returns_six_results() -> None:
    results = run_all_checks()
    assert len(results) == 6
    assert all(isinstance(r, CheckResult) for r in results)


def test_run_all_checks_stable_order() -> None:
    """Verify the names appear in the defined stable order."""
    results = run_all_checks()
    names = [r.name for r in results]
    expected_names = [
        "python_version",
        "claude_binary",
        "anthropic_api_key",
        "apply_config",
        "master_yaml",
        "plugin_dir",
    ]
    assert names == expected_names


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def test_preflight_returns_true_when_all_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mock every check to pass and confirm preflight returns True."""
    passing = [
        CheckResult(name=f"check_{i}", ok=True, message="ok")
        for i in range(6)
    ]
    monkeypatch.setattr("jobsmith.doctor.run_all_checks", lambda cwd=None: passing)

    captured = StringIO()
    with mock.patch("sys.stderr", captured):
        result = preflight(cwd=tmp_path)

    assert result is True
    output = captured.getvalue()
    assert output.count("[PASS]") == 6
    assert "[FAIL]" not in output


def test_preflight_returns_false_when_any_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One failing check → preflight returns False and prints [FAIL] line."""
    checks = [
        CheckResult(name="pass_check", ok=True, message="ok"),
        CheckResult(name="fail_check", ok=False, message="broken", remediation="fix it"),
    ]
    monkeypatch.setattr("jobsmith.doctor.run_all_checks", lambda cwd=None: checks)

    captured = StringIO()
    with mock.patch("sys.stderr", captured):
        result = preflight(cwd=tmp_path)

    assert result is False
    output = captured.getvalue()
    assert "[PASS]" in output
    assert "[FAIL]" in output
    assert "→ fix it" in output


def test_preflight_stderr_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify exact format markers for pass and fail lines."""
    checks = [
        CheckResult(name="mycheck", ok=True, message="all good"),
        CheckResult(name="badcheck", ok=False, message="oops", remediation="do X"),
    ]
    monkeypatch.setattr("jobsmith.doctor.run_all_checks", lambda cwd=None: checks)

    captured = StringIO()
    with mock.patch("sys.stderr", captured):
        preflight()

    lines = captured.getvalue().splitlines()
    assert any(line.startswith("[PASS] mycheck:") for line in lines)
    assert any(line.startswith("[FAIL] badcheck:") for line in lines)
    assert any("→ do X" in line for line in lines)


# ---------------------------------------------------------------------------
# CLI integration via CliRunner
# ---------------------------------------------------------------------------

def test_cli_doctor_exit_0_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """``jobsmith doctor`` exits 0 when all checks pass."""
    monkeypatch.setattr("jobsmith.doctor.preflight", lambda cwd=None: True)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


def test_cli_doctor_exit_1_any_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """``jobsmith doctor`` exits 1 when any check fails."""
    monkeypatch.setattr("jobsmith.doctor.preflight", lambda cwd=None: False)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
