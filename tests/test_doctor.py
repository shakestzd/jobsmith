"""Tests for jobsmith.doctor — preflight environment checks."""

from __future__ import annotations

import json
import shutil
import sys
from io import StringIO
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from jobsmith.doctor import (
    CheckResult,
    check_anthropic_api_key,
    check_claude_auth,
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
# check_claude_auth
# ---------------------------------------------------------------------------

def _make_auth_proc(logged_in: bool, email: str = "user@example.com", plan: str = "max") -> object:
    """Return a mock CompletedProcess-like object for ``claude auth status``."""
    payload = {"loggedIn": logged_in, "email": email, "subscriptionType": plan}
    mock = MagicMock()
    mock.stdout = json.dumps(payload)
    mock.returncode = 0
    return mock


def test_check_claude_auth_pass_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude auth status returns loggedIn=true → PASS via OAuth."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with mock.patch("subprocess.run", return_value=_make_auth_proc(True, "alice@example.com", "max")):
        result = check_claude_auth()
    assert result.ok is True
    assert result.name == "claude_auth"
    assert "alice@example.com" in result.message
    assert "max" in result.message


def test_check_claude_auth_fail_logged_out_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """loggedIn=false, no API key → FAIL."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with mock.patch("subprocess.run", return_value=_make_auth_proc(False)):
        result = check_claude_auth()
    assert result.ok is False
    assert result.remediation is not None
    assert "claude /login" in result.remediation
    assert "ANTHROPIC_API_KEY" in result.remediation


def test_check_claude_auth_fallback_api_key_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude not on PATH (FileNotFoundError), API key set → PASS via fallback."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-key")
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        result = check_claude_auth()
    assert result.ok is True
    assert "ANTHROPIC_API_KEY" in result.message
    # Must not reveal the key value
    assert "sk-real-key" not in result.message


def test_check_claude_auth_fail_binary_missing_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude not on PATH, no API key → FAIL."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        result = check_claude_auth()
    assert result.ok is False
    assert result.remediation is not None


def test_check_claude_auth_fail_binary_missing_whitespace_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """claude not on PATH, API key is only whitespace → FAIL (F6 nit)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        result = check_claude_auth()
    assert result.ok is False


# Backwards-compat alias still importable and delegates correctly.
def test_check_anthropic_api_key_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        result = check_anthropic_api_key()
    assert result.ok is True
    assert result.name == "claude_auth"


# ---------------------------------------------------------------------------
# check_apply_config
# ---------------------------------------------------------------------------

def _scaffold_project(root: Path, *, masters: bool = True) -> Path:
    """Create a minimal valid jobsmith project at ``root``. Returns the config path."""
    config_file = root / ".apply-config.yaml"
    config_file.write_text("master:\n  work_yml: assets/content/work.yml\n")
    if masters:
        content = root / "assets" / "content"
        content.mkdir(parents=True, exist_ok=True)
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
            (content / name).write_text(f"# {name}\n")
    return config_file


def test_check_apply_config_pass(tmp_path: Path) -> None:
    _scaffold_project(tmp_path, masters=False)
    result = check_apply_config(cwd=tmp_path)
    assert result.ok is True
    assert result.name == "apply_config"


def test_check_apply_config_pass_from_subdir(tmp_path: Path) -> None:
    """Invoking from a subdirectory must walk up to find the config."""
    _scaffold_project(tmp_path, masters=False)
    subdir = tmp_path / "deep" / "nested"
    subdir.mkdir(parents=True)
    result = check_apply_config(cwd=subdir)
    assert result.ok is True
    assert ".apply-config.yaml" in result.message


def test_check_apply_config_fail(tmp_path: Path) -> None:
    result = check_apply_config(cwd=tmp_path)
    assert result.ok is False
    assert result.remediation is not None
    assert "jobsmith init" in result.remediation


# ---------------------------------------------------------------------------
# check_master_yaml
# ---------------------------------------------------------------------------

def test_check_master_yaml_pass_with_config(tmp_path: Path) -> None:
    """Scaffold a project under cwd; all configured master YAMLs present → pass."""
    _scaffold_project(tmp_path)
    result = check_master_yaml(cwd=tmp_path)
    assert result.ok is True
    assert result.name == "master_yaml"


def test_check_master_yaml_pass_from_subdir(tmp_path: Path) -> None:
    """Subdir invocation must still resolve master paths via the parent config."""
    _scaffold_project(tmp_path)
    subdir = tmp_path / "private" / "applications"
    subdir.mkdir(parents=True)
    result = check_master_yaml(cwd=subdir)
    assert result.ok is True


def test_check_master_yaml_fail_missing_files(tmp_path: Path) -> None:
    """Config exists but the configured master YAML files do not → fail listing missing."""
    _scaffold_project(tmp_path, masters=False)
    result = check_master_yaml(cwd=tmp_path)
    assert result.ok is False
    assert "missing master YAML" in result.message
    assert "assets/content/work.yml" in result.message


def test_check_master_yaml_fail_no_config(tmp_path: Path) -> None:
    """No config and no bare work.yml fallback → fail."""
    result = check_master_yaml(cwd=tmp_path)
    assert result.ok is False
    assert "no .apply-config.yaml" in result.message


def test_check_master_yaml_fallback_bare_workfile(tmp_path: Path) -> None:
    """No config but a plain work.yml in cwd → pass with a note."""
    (tmp_path / "work.yml").write_text("# bare master\n")
    result = check_master_yaml(cwd=tmp_path)
    assert result.ok is True
    assert "no .apply-config.yaml" in result.message


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
        "claude_auth",
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
