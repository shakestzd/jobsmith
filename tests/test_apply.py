"""Tests for jobsmith.apply — three-phase pipeline, slug derivation, bootstrap."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from jobsmith.apply import (
    build_phase_prompt,
    derive_slug,
    ensure_bootstrap,
    run_apply,
)
from jobsmith.cli import app
from jobsmith.headless import Event, deterministic_session_id
from jobsmith.render import ApplyRenderer

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


def _make_phase_failed_events(phase_name: str, reason: str | None = None) -> list[Event]:
    """Return a minimal event list ending with phase_failed."""
    return [
        Event(type="tool_use", tool_name="Bash", tool_input={"command": "echo hi"}),
        Event(type="phase_failed", name=phase_name, error=reason),
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
    # Stub the between-phase Step 4-5 helper — its behavior is exercised in
    # dedicated tests below.
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)

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
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
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


# ---------------------------------------------------------------------------
# 8. phase_failed event handling — distinct exit code, no further phases
# ---------------------------------------------------------------------------


def test_run_apply_phase_failed_event_aborts_with_distinct_exit(
    tmp_path: Path, monkeypatch
) -> None:
    """A phase_failed event aborts the pipeline with rc=3 (distinct from rc=2 errors)
    and prevents subsequent phases from running."""
    from jobsmith.apply import run_apply as run_apply_fn

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
        # First (and only) call: gather emits phase_failed
        return iter(_make_phase_failed_events("gather", reason="prerequisites-missing"))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply_fn("https://example.com/jobs/ml-engineer", cwd=tmp_path)

    assert rc == 3, f"phase_failed should produce rc=3, got {rc}"
    assert call_count[0] == 1, "subsequent phases must not run after phase_failed"


# ---------------------------------------------------------------------------
# 9. _run_step45_orchestration — anchor guard between gather and draft
# ---------------------------------------------------------------------------


def _scaffold_apply_config(root: Path, *, slug: str = "ml-engineer") -> Path:
    """Scaffold a minimal valid jobsmith project with an application directory."""
    import yaml

    config_file = root / ".apply-config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "master": {
                    "work_yml": "assets/content/work.yml",
                    "skill_yml": "assets/content/skill.yml",
                    "education_yml": "assets/content/education.yml",
                    "author_yml": "assets/content/author.yml",
                },
                "output": {
                    "applications_dir": "private/applications",
                },
            }
        )
    )
    content = root / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")
    apply_state = root / "private" / "applications" / slug / ".apply-state"
    apply_state.mkdir(parents=True, exist_ok=True)
    return apply_state


def test_step45_orchestration_passes_when_anchors_clean(
    tmp_path: Path, monkeypatch
) -> None:
    """Anchor guard returns exit_code=0 → bullet-decisions.json is written and rc=0."""
    from jobsmith.apply import _run_step45_orchestration
    from jobsmith.guard import GuardResult

    apply_state = _scaffold_apply_config(tmp_path)
    (apply_state / "bullet-selection.json").write_text('{"positions": []}')

    fake_result = GuardResult(
        exit_code=0,
        anchor_bullets=[],
        kept=[],
        dropped_with_reason=[],
        dropped_without_reason=[],
    )
    monkeypatch.setattr("jobsmith.apply.check_anchors", lambda *a, **kw: fake_result)

    rc = _run_step45_orchestration("ml-engineer", tmp_path)
    assert rc == 0
    decisions = apply_state / "bullet-decisions.json"
    assert decisions.exists(), "anchor guard must guarantee bullet-decisions.json exists"


def test_step45_orchestration_aborts_on_missing_selection(tmp_path: Path) -> None:
    """No bullet-selection.json from gather → rc=1 with clear remediation."""
    from jobsmith.apply import _run_step45_orchestration

    _scaffold_apply_config(tmp_path)  # apply-state dir exists but no selection

    rc = _run_step45_orchestration("ml-engineer", tmp_path)
    assert rc == 1


def test_step45_orchestration_aborts_on_anchor_violation(
    tmp_path: Path, monkeypatch
) -> None:
    """Anchors dropped without reason → rc=2; bullet-decisions.json is NOT written."""
    from jobsmith.apply import _run_step45_orchestration
    from jobsmith.guard import Bullet, GuardResult

    apply_state = _scaffold_apply_config(tmp_path)
    (apply_state / "bullet-selection.json").write_text('{"positions": []}')

    dropped = Bullet(
        bullet_id="abc123",
        text="Saved $40M in cloud costs",
        company="Acme",
        position_title="Eng",
        position_index=0,
        bullet_index=0,
        anchors=[],
    )
    fake_result = GuardResult(
        exit_code=1,
        anchor_bullets=[dropped],
        kept=[],
        dropped_with_reason=[],
        dropped_without_reason=[dropped],
    )
    monkeypatch.setattr("jobsmith.apply.check_anchors", lambda *a, **kw: fake_result)

    rc = _run_step45_orchestration("ml-engineer", tmp_path)
    assert rc == 2
    decisions = apply_state / "bullet-decisions.json"
    assert not decisions.exists(), "must not auto-create decisions when anchors violated"


# ---------------------------------------------------------------------------
# 10. _render_event handles phase_failed
# ---------------------------------------------------------------------------


def test_render_event_phase_failed_includes_reason() -> None:
    from jobsmith.apply import _render_event

    line = _render_event(Event(type="phase_failed", name="draft", error="prose-qa-max-iterations"))
    assert line is not None
    assert "draft" in line
    assert "prose-qa-max-iterations" in line
    assert "fail" in line.lower()


def test_render_event_phase_failed_no_reason() -> None:
    from jobsmith.apply import _render_event

    line = _render_event(Event(type="phase_failed", name="gather", error=None))
    assert line is not None
    assert "gather" in line
    assert "fail" in line.lower()


# ---------------------------------------------------------------------------
# 11. ApplyRenderer — rich rendering helpers
# ---------------------------------------------------------------------------


def _make_test_console() -> tuple[Console, io.StringIO]:
    """Return a (Console, buffer) pair for capturing rendered output in tests."""
    buf = io.StringIO()
    con = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        highlight=False,
        markup=True,
        width=120,
    )
    return con, buf


def test_renderer_tool_use_line() -> None:
    """Tool-use events render with → prefix and tool name."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="tool_use", tool_name="Bash", tool_input={"command": "echo hi"}))
    output = buf.getvalue()
    assert "Bash" in output
    assert "command" in output
    assert "echo hi" in output


def test_renderer_tool_result_line() -> None:
    """Tool-result events render with ← prefix dimmed."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="tool_result", tool_result="hello world"))
    output = buf.getvalue()
    assert "hello world" in output
    assert "←" in output


def test_renderer_tool_result_truncated() -> None:
    """Long tool results are truncated to max 100 chars + ellipsis."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    long_result = "x" * 200
    rdr.render_event(Event(type="tool_result", tool_result=long_result))
    output = buf.getvalue()
    # Should contain ellipsis
    assert "…" in output
    # The raw long string should NOT appear in full
    assert "x" * 150 not in output


def test_renderer_phase_complete_panel() -> None:
    """phase_complete events render a green success panel."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="phase_complete", name="gather"))
    output = buf.getvalue()
    assert "gather" in output
    assert "complete" in output.lower()
    assert "✓" in output


def test_renderer_phase_failed_panel() -> None:
    """phase_failed events render a red failure panel with reason."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(
        Event(type="phase_failed", name="draft", error="prose-qa-max-iterations")
    )
    output = buf.getvalue()
    assert "draft" in output
    assert "failed" in output.lower()
    assert "prose-qa-max-iterations" in output
    assert "✗" in output


def test_renderer_error_event() -> None:
    """Error events render with ✗ in red."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="error", error="claude exited with code 1"))
    output = buf.getvalue()
    assert "claude exited with code 1" in output
    assert "✗" in output


def test_renderer_phase_header() -> None:
    """Phase header renders a cyan panel with phase number and name."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.print_header(1, 3, "gather")
    output = buf.getvalue()
    assert "Phase 1" in output
    assert "3" in output
    assert "Gather" in output


def test_renderer_yes_mode_no_spinner() -> None:
    """In yes=True mode, _use_spinner is False; start_phase is a no-op."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    assert rdr._use_spinner is False
    # start_phase should not raise and should not create a Progress
    rdr.start_phase("gather")
    assert rdr._progress is None


def test_renderer_non_tty_no_spinner() -> None:
    """When console.is_terminal is False, spinner is suppressed."""
    con, buf = _make_test_console()
    # force_terminal=False means is_terminal returns False
    assert con.is_terminal is False
    rdr = ApplyRenderer(yes=False, console=con)
    assert rdr._use_spinner is False


def test_renderer_text_event_non_empty() -> None:
    """Non-empty text events are rendered as dim italic."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="text", text="Parsing job description…"))
    output = buf.getvalue()
    assert "Parsing job description" in output


def test_renderer_text_event_empty_skipped() -> None:
    """Empty text events produce no output."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="text", text=""))
    output = buf.getvalue()
    assert output.strip() == ""


# ---------------------------------------------------------------------------
# 12. run_apply with custom renderer — --yes path and non-TTY path
# ---------------------------------------------------------------------------


def _scaffold_plugin(tmp_path: Path) -> Path:
    plugin_fake = tmp_path / "plugin"
    sp_dir = plugin_fake / "system-prompts"
    sp_dir.mkdir(parents=True)
    for n, name in [(1, "gather"), (2, "draft"), (3, "render")]:
        (sp_dir / f"phase-{n}-{name}.md").write_text(f"# Phase {n}\n")
    return plugin_fake


def test_run_apply_yes_mode_renderer_used(tmp_path: Path, monkeypatch) -> None:
    """run_apply with skip_confirm=True uses ApplyRenderer(yes=True), no spinner."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("# config\n")

    plugin_fake = _scaffold_plugin(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)

    buf = io.StringIO()
    con = Console(file=buf, force_terminal=False, no_color=True, width=120)
    rdr = ApplyRenderer(yes=True, console=con)

    rc = run_apply(
        "https://example.com/jobs/ml-engineer",
        cwd=tmp_path,
        skip_confirm=True,
        renderer=rdr,
    )

    assert rc == 0
    output = buf.getvalue()
    # Phase headers should appear
    assert "Phase 1" in output
    assert "Phase 2" in output
    assert "Phase 3" in output
    # phase_complete panels should appear
    assert "complete" in output.lower()


def test_run_apply_non_tty_renderer(tmp_path: Path, monkeypatch) -> None:
    """run_apply with a non-TTY console renders events without spinner."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("# config\n")

    plugin_fake = _scaffold_plugin(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)

    buf = io.StringIO()
    # force_terminal=False → non-TTY simulation
    con = Console(file=buf, force_terminal=False, no_color=True, width=120)
    rdr = ApplyRenderer(yes=True, console=con)

    assert rdr._use_spinner is False

    rc = run_apply(
        "https://example.com/jobs/engineer",
        cwd=tmp_path,
        skip_confirm=True,
        renderer=rdr,
    )

    assert rc == 0
    output = buf.getvalue()
    # Tool calls should still appear
    assert "Bash" in output


# ---------------------------------------------------------------------------
# 13. Slug reconciliation after phase 1 (canonical slug, dir rename, threading)
# ---------------------------------------------------------------------------


def _scaffold_apply_config_for_reconcile(root: Path, *, slug: str) -> Path:
    """Scaffold a minimal jobsmith project with the given slug directory.

    Returns the .apply-state Path.
    """
    import yaml

    config_file = root / ".apply-config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "master": {
                    "work_yml": "assets/content/work.yml",
                    "skill_yml": "assets/content/skill.yml",
                    "education_yml": "assets/content/education.yml",
                    "author_yml": "assets/content/author.yml",
                },
                "output": {
                    "applications_dir": "private/applications",
                },
            }
        )
    )
    content = root / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")
    apply_state = root / "private" / "applications" / slug / ".apply-state"
    apply_state.mkdir(parents=True, exist_ok=True)
    return apply_state


def test_reconcile_canonical_slug_renames_when_different(tmp_path: Path) -> None:
    """Helper renames dir from url-slug to canonical and returns canonical slug."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    apply_state = _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "GTM Data Analyst"})
    )

    canonical_slug, session_id = _reconcile_canonical_slug("url-slug", tmp_path)

    assert canonical_slug == "clay-gtm-data-analyst"
    # Canonical dir must exist with the artifact
    canonical_state = tmp_path / "private" / "applications" / "clay-gtm-data-analyst" / ".apply-state"
    assert canonical_state.exists()
    assert (canonical_state / "jd-parsed.json").exists()
    # Original dir must be gone
    orig_dir = tmp_path / "private" / "applications" / "url-slug"
    assert not orig_dir.exists()
    # session_id matches canonical
    assert session_id == deterministic_session_id("clay-gtm-data-analyst")


def test_reconcile_canonical_slug_noop_when_already_canonical(tmp_path: Path) -> None:
    """No rename when active slug already equals canonical slug."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    apply_state = _scaffold_apply_config_for_reconcile(tmp_path, slug="acme-ml-engineer")
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Acme", "position": "ML Engineer"})
    )

    canonical_slug, session_id = _reconcile_canonical_slug("acme-ml-engineer", tmp_path)

    assert canonical_slug == "acme-ml-engineer"
    assert session_id == deterministic_session_id("acme-ml-engineer")
    # Dir must still exist
    assert (apply_state / "jd-parsed.json").exists()


def test_reconcile_canonical_slug_falls_back_to_alt_dir(tmp_path: Path) -> None:
    """When url-slug .apply-state is empty, finds jd-parsed.json in another dir."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    # Create URL-slug dir with empty .apply-state (no jd-parsed.json)
    _scaffold_apply_config_for_reconcile(tmp_path, slug="jobs")

    # Phase-1 wrote under canonical dir directly
    canonical_state = tmp_path / "private" / "applications" / "clay-gtm-data-analyst" / ".apply-state"
    canonical_state.mkdir(parents=True, exist_ok=True)
    (canonical_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "GTM Data Analyst"})
    )

    canonical_slug, session_id = _reconcile_canonical_slug("jobs", tmp_path)

    assert canonical_slug == "clay-gtm-data-analyst"
    assert session_id == deterministic_session_id("clay-gtm-data-analyst")
    # The canonical dir must still be intact (no rename needed — already correct name)
    assert (canonical_state / "jd-parsed.json").exists()


def test_reconcile_canonical_slug_missing_jd_parsed_returns_active(tmp_path: Path) -> None:
    """With no jd-parsed.json anywhere, returns (active_slug, session_id) unchanged."""
    from jobsmith.apply import _reconcile_canonical_slug

    _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")
    # No jd-parsed.json written anywhere

    canonical_slug, session_id = _reconcile_canonical_slug("url-slug", tmp_path)

    assert canonical_slug == "url-slug"
    assert session_id == deterministic_session_id("url-slug")


def test_run_apply_threads_canonical_slug_into_phase_2(tmp_path: Path, monkeypatch) -> None:
    """After gather, canonical slug is threaded into phase-2 and phase-3 prompts."""
    import json

    config_file = tmp_path / ".apply-config.yaml"
    import yaml
    config_file.write_text(
        yaml.safe_dump(
            {
                "master": {
                    "work_yml": "assets/content/work.yml",
                    "skill_yml": "assets/content/skill.yml",
                    "education_yml": "assets/content/education.yml",
                    "author_yml": "assets/content/author.yml",
                },
                "output": {
                    "applications_dir": "private/applications",
                },
            }
        )
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")

    plugin_fake = _scaffold_plugin(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    # URL derives to "job-id" slug; canonical will be "clay-gtm-data-analyst"
    url = "https://jobs.ashbyhq.com/clay/some-path/job-id"
    url_slug = derive_slug(url)  # "job-id"

    # Pre-create url-slug apply-state dir with jd-parsed.json so reconcile can find it
    url_apply_state = tmp_path / "private" / "applications" / url_slug / ".apply-state"
    url_apply_state.mkdir(parents=True, exist_ok=True)
    (url_apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "GTM Data Analyst"})
    )

    captured_prompts: list[tuple[str, str]] = []  # [(phase, prompt), ...]
    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        captured_prompts.append((phase, prompt))
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    # Stub step45 so it doesn't fail on missing bullet-selection.json
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)
    assert rc == 0, f"run_apply returned {rc}"

    # phase 2 (draft) and phase 3 (render) prompts must contain canonical slug
    draft_prompt = next(p for name, p in captured_prompts if name == "draft")
    render_prompt = next(p for name, p in captured_prompts if name == "render")

    assert "clay-gtm-data-analyst" in draft_prompt, (
        f"draft prompt should contain canonical slug; got: {draft_prompt!r}"
    )
    assert "clay-gtm-data-analyst" in render_prompt, (
        f"render prompt should contain canonical slug; got: {render_prompt!r}"
    )
    # url-slug must NOT appear in phase 2/3 prompts (it was reconciled away)
    assert url_slug not in draft_prompt, (
        f"draft prompt must not contain url slug {url_slug!r}; got: {draft_prompt!r}"
    )


# ---------------------------------------------------------------------------
# 14. ApplyRenderer UX fixes
# ---------------------------------------------------------------------------


def test_renderer_path_mid_truncation() -> None:
    """Long path args are mid-truncated preserving the filename."""
    from jobsmith.render import _truncate_path

    long_path = "/Users/shakes/DevProjects/jobsmith/.claude/worktr/system-prompts/phase-1-gather.md"
    result = _truncate_path(long_path, max_chars=50)
    # Filename must be preserved at the end
    assert result.endswith("phase-1-gather.md")
    # Mid-truncation marker must be present
    assert "…" in result
    # Result must be shorter than original
    assert len(result) < len(long_path)


def test_renderer_path_mid_truncation_in_format_tool_args() -> None:
    """_format_tool_args mid-truncates path values (not tail-truncates)."""
    from jobsmith.render import _format_tool_args

    long_path = "/Users/shakes/DevProjects/jobsmith/.claude/worktr/system-prompts/phase-1-gather.md"
    result = _format_tool_args({"path": long_path}, max_chars=120)
    # Filename must be preserved
    assert "phase-1-gather.md" in result
    # Should contain mid-truncation marker
    assert "…" in result


def test_renderer_tool_result_line_numbered_summary() -> None:
    """Line-numbered file content is summarised as '← N lines (M.K KB)'."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    line_content = "1 foo\n2 bar\n3 baz\n4 qux\n"
    rdr.render_event(Event(type="tool_result", tool_result=line_content))
    output = buf.getvalue()
    assert "4 lines" in output
    assert "KB" in output
    # Raw content should NOT appear
    assert "foo" not in output


def test_renderer_tool_result_json_summary() -> None:
    """JSON-shaped tool result is summarised as '← {N keys}'."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="tool_result", tool_result='{"a":1,"b":2,"c":3}'))
    output = buf.getvalue()
    assert "{3 keys}" in output


def test_renderer_tool_result_json_array_summary() -> None:
    """JSON array tool result is summarised as '← [N items]'."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(Event(type="tool_result", tool_result="[1,2,3,4]"))
    output = buf.getvalue()
    assert "[4 items]" in output


def test_renderer_filters_todowrite() -> None:
    """TodoWrite tool_use events produce no output; matching tool_result also silenced."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)

    # Construct a tool_use event for TodoWrite with an id in raw
    tool_use_id = "toolu_abc123"
    raw_payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "TodoWrite",
                    "input": {"todos": []},
                }
            ]
        },
    }
    tool_use_event = Event(
        type="tool_use",
        tool_name="TodoWrite",
        tool_input={"todos": []},
        raw=raw_payload,
    )
    rdr.render_event(tool_use_event)

    # Matching tool_result (tool_name == tool_use_id per headless.py convention)
    tool_result_event = Event(
        type="tool_result",
        tool_name=tool_use_id,
        tool_result="OK",
        raw={},
    )
    rdr.render_event(tool_result_event)

    output = buf.getvalue()
    assert "TodoWrite" not in output
    assert output.strip() == ""


def test_renderer_filters_toolsearch() -> None:
    """ToolSearch tool_use events produce no output; matching tool_result also silenced."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)

    tool_use_id = "toolu_xyz789"
    raw_payload = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "ToolSearch",
                    "input": {"query": "test"},
                }
            ]
        },
    }
    tool_use_event = Event(
        type="tool_use",
        tool_name="ToolSearch",
        tool_input={"query": "test"},
        raw=raw_payload,
    )
    rdr.render_event(tool_use_event)

    tool_result_event = Event(
        type="tool_result",
        tool_name=tool_use_id,
        tool_result="some search results",
        raw={},
    )
    rdr.render_event(tool_result_event)

    output = buf.getvalue()
    assert "ToolSearch" not in output
    assert output.strip() == ""


def test_renderer_agent_dispatch_indent() -> None:
    """Agent tool_use events are prefixed with '│ ' for visual nesting."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)
    rdr.render_event(
        Event(
            type="tool_use",
            tool_name="Agent",
            tool_input={"prompt": "do something"},
            raw={},
        )
    )
    output = buf.getvalue()
    assert "│ " in output  # │ prefix


def test_renderer_phase_summary_renders_known_artifacts(tmp_path: Path) -> None:
    """render_phase_summary emits content from all four artifact files when present."""
    import json

    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)

    apply_state_dir = tmp_path / ".apply-state"
    apply_state_dir.mkdir()

    # jd-parsed.json — must_have requirements
    (apply_state_dir / "jd-parsed.json").write_text(
        json.dumps(
            {
                "must_have": [
                    {"requirement": "Python 5+ years", "weight": "high"},
                    {"requirement": "ML experience", "weight": "medium"},
                ]
            }
        )
    )

    # fit-score.json — must_have_table with met/evidence
    (apply_state_dir / "fit-score.json").write_text(
        json.dumps(
            {
                "must_have_table": [
                    {"requirement": "Python 5+ years", "evidence": "10 years", "met": True},
                    {"requirement": "ML experience", "evidence": "PyTorch", "met": True},
                ]
            }
        )
    )

    # bullet-diff.md
    (apply_state_dir / "bullet-diff.md").write_text("kept 8 / dropped 3\n")

    # hm-snippet.md
    (apply_state_dir / "hm-snippet.md").write_text("HM is Jane Doe, VP Engineering\n")

    rdr.render_phase_summary("gather", apply_state_dir)
    output = buf.getvalue()

    # Should contain key strings from each artifact
    assert "Python 5+ years" in output
    assert "kept 8" in output or "8" in output
    assert "Jane Doe" in output


def test_renderer_phase_summary_tolerates_missing(tmp_path: Path) -> None:
    """render_phase_summary with empty dir raises no exception."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)

    apply_state_dir = tmp_path / ".apply-state"
    apply_state_dir.mkdir()

    # Should not raise; empty dir → no artifacts
    rdr.render_phase_summary("gather", apply_state_dir)


# ---------------------------------------------------------------------------
# 15. Path injection into phase prompts
# ---------------------------------------------------------------------------


_SAMPLE_PATHS = {
    "plugin_dir": "/abs/plugin",
    "config": "/abs/.apply-config.yaml",
    "specialist_contracts": "/abs/plugin/agents/apply/specialist-contracts.yaml",
    "agent_dir": "/abs/plugin/agents",
    "master.work_yml": "/abs/assets/content/work.yml",
    "master.skill_yml": "/abs/assets/content/skill.yml",
    "master.education_yml": "/abs/assets/content/education.yml",
    "master.author_yml": "/abs/assets/content/author.yml",
    "apply_state_dir": "/abs/private/applications/acme-mle/.apply-state",
}


def test_build_phase_prompt_includes_paths_block_when_provided() -> None:
    """When paths dict provided, prompt includes a Paths block with all keys+values."""
    prompt = build_phase_prompt(
        "gather", "acme-mle", "https://e.com/jobs/x", paths=_SAMPLE_PATHS
    )
    # Header appears exactly once
    assert prompt.count("Paths") == 1

    # Every key appears
    for key in _SAMPLE_PATHS:
        assert key in prompt, f"key {key!r} missing from prompt"

    # Every value appears verbatim
    for value in _SAMPLE_PATHS.values():
        assert value in prompt, f"value {value!r} missing from prompt"


def test_build_phase_prompt_omits_paths_when_empty() -> None:
    """Default paths={} (or absent kwarg) omits the Paths header from the prompt."""
    prompt_default = build_phase_prompt("gather", "acme-mle", "https://e.com/jobs/x")
    assert "Paths" not in prompt_default

    prompt_empty = build_phase_prompt(
        "gather", "acme-mle", "https://e.com/jobs/x", paths={}
    )
    assert "Paths" not in prompt_empty


def test_build_phase_prompt_paths_for_draft_and_render() -> None:
    """The same Paths block appears regardless of phase (gather / draft / render)."""
    for phase in ("gather", "draft", "render"):
        prompt = build_phase_prompt(
            phase, "acme-mle", "https://e.com/jobs/x", paths=_SAMPLE_PATHS
        )
        for key in _SAMPLE_PATHS:
            assert key in prompt, f"phase={phase}: key {key!r} missing"
        for value in _SAMPLE_PATHS.values():
            assert value in prompt, f"phase={phase}: value {value!r} missing"


def test_run_apply_threads_paths_to_each_phase(tmp_path: Path, monkeypatch) -> None:
    """run_apply injects absolute paths (config, plugin_dir, specialist_contracts,
    apply_state_dir) into every phase prompt."""
    import yaml

    # Scaffold a minimal but real config so _build_paths can resolve everything
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "master": {
                    "work_yml": "assets/content/work.yml",
                    "skill_yml": "assets/content/skill.yml",
                    "education_yml": "assets/content/education.yml",
                    "author_yml": "assets/content/author.yml",
                },
                "output": {
                    "applications_dir": "private/applications",
                },
            }
        )
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")

    plugin_fake = _scaffold_plugin(tmp_path)
    # Add specialist-contracts.yaml under agents/apply/
    agents_apply = plugin_fake / "agents" / "apply"
    agents_apply.mkdir(parents=True, exist_ok=True)
    (agents_apply / "specialist-contracts.yaml").write_text("frozen_at: 2025-01-01\n")

    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    url = "https://example.com/jobs/ml-engineer"

    captured_prompts: list[tuple[str, str]] = []
    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        captured_prompts.append((phase, prompt))
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)
    assert rc == 0, f"run_apply returned {rc}"

    expected_config = str(config_file.resolve())
    expected_plugin = str(plugin_fake.resolve())
    expected_contracts = str((plugin_fake / "agents" / "apply" / "specialist-contracts.yaml").resolve())

    for phase, prompt in captured_prompts:
        assert expected_config in prompt, (
            f"phase={phase}: config path missing from prompt"
        )
        assert expected_plugin in prompt, (
            f"phase={phase}: plugin_dir missing from prompt"
        )
        assert expected_contracts in prompt, (
            f"phase={phase}: specialist_contracts missing from prompt"
        )
        # apply_state_dir: for draft/render uses canonical slug (may match or differ)
        assert ".apply-state" in prompt, (
            f"phase={phase}: .apply-state dir missing from prompt"
        )


def test_build_phase_prompt_includes_uv_run_python_rule() -> None:
    """phase-1-gather.md contains 'uv run python' rule and filesystem-search prohibition."""
    import jobsmith

    gather_md = jobsmith.plugin_dir() / "system-prompts" / "phase-1-gather.md"
    content = gather_md.read_text()

    assert "uv run python" in content, (
        "phase-1-gather.md must contain 'uv run python' rule"
    )
    assert "Do NOT search the filesystem" in content or "do not search" in content.lower(), (
        "phase-1-gather.md must contain filesystem-search prohibition"
    )
    # No assertion on output content — just must not raise
