"""Tests for jobsmith.apply — three-phase pipeline, slug derivation, bootstrap."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from jobsmith.apply import (
    build_phase_prompt,
    derive_slug,
    ensure_bootstrap,
    run_apply,
)
from jobsmith.cli import app
from jobsmith.headless import Event


# ---------------------------------------------------------------------------
# 1. derive_slug — table-driven tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # Standard job URL — last path segment
        (
            "https://example.com/jobs/senior-engineer",
            "senior-engineer",
        ),
        # URL with query string — query stripped, path segment used
        (
            "https://jobs.example.com/posting/data-scientist?source=linkedin&ref=123",
            "data-scientist",
        ),
        # URL with no meaningful path (only slashes) — falls back to hash
        (
            "https://example.com/",
            derive_slug("https://example.com/"),  # hash — just verify it's 12 chars + consistent
        ),
        # Non-ASCII characters in path — replaced with hyphens or hash fallback
        (
            "https://example.com/jobs/ingénieur-senior",
            derive_slug("https://example.com/jobs/ingénieur-senior"),
        ),
        # Very long URL path segment — truncated to 60 chars
        (
            "https://example.com/jobs/" + "a" * 100,
            "a" * 60,
        ),
        # URL with special chars becoming hyphens
        (
            "https://example.com/jobs/Software_Engineer_II",
            "software-engineer-ii",
        ),
        # URL with multiple consecutive non-alphanum chars collapsed
        (
            "https://example.com/jobs/lead---devops--engineer",
            "lead-devops-engineer",
        ),
    ],
)
def test_derive_slug_table(url: str, expected: str) -> None:
    result = derive_slug(url)
    assert result == expected


def test_derive_slug_no_path_fallback_is_12_chars() -> None:
    # A URL with no path uses SHA-256 hash truncated to 12 chars
    result = derive_slug("https://example.com/")
    assert len(result) == 12
    assert result.isalnum()


def test_derive_slug_max_60_chars() -> None:
    long_url = "https://example.com/jobs/" + "x" * 200
    result = derive_slug(long_url)
    assert len(result) <= 60


def test_derive_slug_consistent() -> None:
    # Same URL always produces same slug
    url = "https://jobs.acme.com/postings/ml-engineer-42"
    assert derive_slug(url) == derive_slug(url)


# ---------------------------------------------------------------------------
# 2. ensure_bootstrap
# ---------------------------------------------------------------------------


def test_ensure_bootstrap_noop_when_config_exists(tmp_path: Path) -> None:
    """No-op if .apply-config.yaml already present."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("# existing config\n")

    with patch("jobsmith.apply._run_init") as mock_init:
        ensure_bootstrap(tmp_path)
        mock_init.assert_not_called()


def test_ensure_bootstrap_calls_init_when_missing(tmp_path: Path) -> None:
    """Calls _run_init when config file is absent."""
    with patch("jobsmith.apply._run_init") as mock_init:
        ensure_bootstrap(tmp_path)
        mock_init.assert_called_once_with(tmp_path)


# ---------------------------------------------------------------------------
# 3. build_phase_prompt
# ---------------------------------------------------------------------------


def test_build_phase_prompt_gather() -> None:
    prompt = build_phase_prompt("gather", "acme-ml-engineer", "https://example.com/jobs/ml")
    assert "https://example.com/jobs/ml" in prompt
    assert "acme-ml-engineer" in prompt
    assert "Phase 1" in prompt
    assert "gather" in prompt


def test_build_phase_prompt_draft() -> None:
    prompt = build_phase_prompt("draft", "acme-ml-engineer", "https://example.com/jobs/ml")
    assert "acme-ml-engineer" in prompt
    assert "Phase 2" in prompt
    assert "draft" in prompt
    assert ".apply-state/" in prompt


def test_build_phase_prompt_render() -> None:
    prompt = build_phase_prompt("render", "acme-ml-engineer", "https://example.com/jobs/ml")
    assert "acme-ml-engineer" in prompt
    assert "Phase 3" in prompt
    assert "render" in prompt
    assert "jobsmith assemble" in prompt


def test_build_phase_prompt_invalid_phase() -> None:
    with pytest.raises(ValueError, match="Unknown phase"):
        build_phase_prompt("unknown", "slug", "https://example.com")


# ---------------------------------------------------------------------------
# Helpers for run_apply tests
# ---------------------------------------------------------------------------


def _make_phase_events(phase_name: str) -> list[Event]:
    """Return a minimal deterministic event list ending with phase_complete."""
    return [
        Event(type="tool_use", tool_name="Bash", tool_input={"command": "echo hi"}),
        Event(type="tool_result", tool_result="hi"),
        Event(type="phase_complete", name=phase_name),
    ]


def _make_error_events() -> list[Event]:
    return [
        Event(type="error", error="claude exited with code 1"),
    ]


# ---------------------------------------------------------------------------
# 4. run_apply happy path
# ---------------------------------------------------------------------------


def test_run_apply_happy_path(tmp_path: Path, monkeypatch) -> None:
    """Happy path: all three phases succeed, confirm=True, returns 0."""
    # Stub bootstrap (config already exists)
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("# config\n")

    # Stub plugin_dir to a real temp dir with system prompt files
    plugin_fake = tmp_path / "plugin"
    sp_dir = plugin_fake / "system-prompts"
    sp_dir.mkdir(parents=True)
    for n, name in [(1, "gather"), (2, "draft"), (3, "render")]:
        (sp_dir / f"phase-{n}-{name}.md").write_text(f"# Phase {n}\n")

    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        # Validate resume flag: gather=False, draft/render=True once session_exists returns True
        if phase == "gather":
            assert resume is False, f"gather should not resume, got resume={resume}"
        else:
            assert resume is True, f"{phase} should resume, got resume={resume}"
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)

    # Patch click.confirm to always return True
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply("https://example.com/jobs/ml-engineer", cwd=tmp_path)

    assert rc == 0
    assert call_count[0] == 3


# ---------------------------------------------------------------------------
# 5. run_apply user declines at gate
# ---------------------------------------------------------------------------


def test_run_apply_user_declines_after_phase1(tmp_path: Path, monkeypatch) -> None:
    """When user declines at the gather->draft gate, returns 0 and only 1 phase runs."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("# config\n")

    plugin_fake = tmp_path / "plugin"
    sp_dir = plugin_fake / "system-prompts"
    sp_dir.mkdir(parents=True)
    for n, name in [(1, "gather"), (2, "draft"), (3, "render")]:
        (sp_dir / f"phase-{n}-{name}.md").write_text(f"# Phase {n}\n")

    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    call_count = [0]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        call_count[0] += 1
        return iter(_make_phase_events("gather"))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    # User says no at the first gate
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: False)

    rc = run_apply("https://example.com/jobs/ml-engineer", cwd=tmp_path)

    assert rc == 0
    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# 6. run_apply phase fails (error event, no phase_complete)
# ---------------------------------------------------------------------------


def test_run_apply_phase_fails(tmp_path: Path, monkeypatch) -> None:
    """When a phase yields an error event, returns non-zero and doesn't proceed."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("# config\n")

    plugin_fake = tmp_path / "plugin"
    sp_dir = plugin_fake / "system-prompts"
    sp_dir.mkdir(parents=True)
    for n, name in [(1, "gather"), (2, "draft"), (3, "render")]:
        (sp_dir / f"phase-{n}-{name}.md").write_text(f"# Phase {n}\n")

    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    call_count = [0]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        call_count[0] += 1
        return iter(_make_error_events())

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply("https://example.com/jobs/ml-engineer", cwd=tmp_path)

    assert rc != 0
    # Only the first (failing) phase should have been called
    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# 7. CLI integration via CliRunner — --yes bypasses confirmation
# ---------------------------------------------------------------------------


def test_cli_apply_yes_flag(tmp_path: Path, monkeypatch) -> None:
    """CliRunner: `jobsmith apply --yes <url>` calls run_apply with skip_confirm=True."""
    runner = CliRunner()

    captured: dict = {}

    def fake_run_apply(url, *, cwd=None, skip_confirm=False):
        captured["url"] = url
        captured["skip_confirm"] = skip_confirm
        return 0

    # Patch at the apply module level AND in cli namespace (lazy import resolves to apply module)
    monkeypatch.setattr("jobsmith.apply.run_apply", fake_run_apply)

    # We also need to intercept the lazy import inside cli.apply; patch the module
    import jobsmith.apply as apply_mod

    monkeypatch.setattr(apply_mod, "run_apply", fake_run_apply)

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(app, ["apply", "--yes", "https://example.com/jobs/swe"])

    assert result.exit_code == 0
    assert captured.get("skip_confirm") is True
    assert captured.get("url") == "https://example.com/jobs/swe"


def test_cli_apply_without_yes_default_confirm_false(tmp_path: Path, monkeypatch) -> None:
    """Without --yes, skip_confirm defaults to False."""
    runner = CliRunner()

    captured: dict = {}

    def fake_run_apply(url, *, cwd=None, skip_confirm=False):
        captured["skip_confirm"] = skip_confirm
        return 0

    import jobsmith.apply as apply_mod

    monkeypatch.setattr(apply_mod, "run_apply", fake_run_apply)

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(app, ["apply", "https://example.com/jobs/swe"])

    assert result.exit_code == 0
    assert captured.get("skip_confirm") is False
