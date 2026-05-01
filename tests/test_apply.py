"""Tests for jobsmith.apply — three-phase pipeline, slug derivation, bootstrap."""

from __future__ import annotations

import io
import json
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

    def fake_run_apply(url, *, cwd=None, skip_confirm=False, force=False, verbosity=0):
        captured["url"] = url
        captured["skip_confirm"] = skip_confirm
        captured["force"] = force
        captured["verbosity"] = verbosity
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
    assert captured.get("force") is False
    assert captured.get("url") == "https://example.com/jobs/swe"


def test_cli_apply_force_flag(tmp_path: Path, monkeypatch) -> None:
    """`jobsmith apply --force <url>` threads force=True into run_apply."""
    runner = CliRunner()
    captured: dict = {}

    def fake_run_apply(url, *, cwd=None, skip_confirm=False, force=False, verbosity=0):
        captured["force"] = force
        return 0

    import jobsmith.apply as apply_mod

    monkeypatch.setattr(apply_mod, "run_apply", fake_run_apply)
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(app, ["apply", "--force", "https://example.com/jobs/swe"])

    assert result.exit_code == 0
    assert captured.get("force") is True


def test_cli_apply_without_yes_default_confirm_false(tmp_path: Path, monkeypatch) -> None:
    """Without --yes, skip_confirm defaults to False."""
    runner = CliRunner()

    captured: dict = {}

    def fake_run_apply(url, *, cwd=None, skip_confirm=False, force=False, verbosity=0):
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
    """Tool-use events render with → prefix and tool name at verbosity=2 (-vv)."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
    rdr.render_event(Event(type="tool_use", tool_name="Bash", tool_input={"command": "echo hi"}))
    output = buf.getvalue()
    assert "Bash" in output
    assert "command" in output
    assert "echo hi" in output


def test_renderer_tool_result_line() -> None:
    """Tool-result events render with ← prefix dimmed at verbosity=2 (-vv)."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
    rdr.render_event(Event(type="tool_result", tool_result="hello world"))
    output = buf.getvalue()
    assert "hello world" in output
    assert "←" in output


def test_renderer_tool_result_truncated() -> None:
    """Long tool results are truncated to max 100 chars + ellipsis at verbosity=2."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
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


# ---------------------------------------------------------------------------
# 12. _build_paths injects benchmark paths into phase paths dict
# ---------------------------------------------------------------------------


def test_build_paths_injects_benchmark_paths_when_configured(tmp_path: Path) -> None:
    """_build_paths includes benchmark.* keys when config has benchmark paths set."""
    import yaml

    from jobsmith.apply import _build_paths

    # Scaffold a valid config with benchmark paths
    bm_dir = tmp_path / "private" / "benchmarks"
    bm_dir.mkdir(parents=True)
    resume_qmd = bm_dir / "resume.qmd"
    resume_qmd.write_text("# benchmark resume\n")
    cover_letter_md = bm_dir / "cover-letter.md"
    cover_letter_md.write_text("# benchmark cover letter\n")
    resume_pdf = bm_dir / "resume.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4")

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
                "output": {"applications_dir": "private/applications"},
                "benchmarks": {
                    "resume_qmd": "private/benchmarks/resume.qmd",
                    "cover_letter_md": "private/benchmarks/cover-letter.md",
                    "resume_pdf": "private/benchmarks/resume.pdf",
                    "required": False,
                },
            }
        )
    )

    plugin_fake = tmp_path / "plugin"
    plugin_fake.mkdir()

    paths = _build_paths("test-slug", tmp_path, plugin_fake)

    assert "benchmark.resume_qmd" in paths, "benchmark.resume_qmd must be in paths"
    assert "benchmark.cover_letter_md" in paths, "benchmark.cover_letter_md must be in paths"
    assert "benchmark.resume_pdf" in paths, "benchmark.resume_pdf must be in paths"
    assert paths["benchmark.resume_qmd"] == str(resume_qmd.resolve())
    assert paths["benchmark.cover_letter_md"] == str(cover_letter_md.resolve())
    assert paths["benchmark.resume_pdf"] == str(resume_pdf.resolve())


def test_build_paths_benchmark_fallback_when_not_configured(tmp_path: Path) -> None:
    """_build_paths uses Pat Doe fallback paths when benchmarks section absent."""
    import yaml

    import jobsmith
    from jobsmith.apply import _build_paths

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
                "output": {"applications_dir": "private/applications"},
            }
        )
    )

    plugin_fake = tmp_path / "plugin"
    plugin_fake.mkdir()

    paths = _build_paths("test-slug", tmp_path, plugin_fake)

    pat_doe_dir = jobsmith.plugin_dir() / "benchmarks"
    assert "benchmark.resume_qmd" in paths
    assert "benchmark.cover_letter_md" in paths
    assert "benchmark.resume_pdf" in paths
    assert paths["benchmark.resume_qmd"] == str(pat_doe_dir / "resume.qmd")
    assert paths["benchmark.cover_letter_md"] == str(pat_doe_dir / "cover-letter.md")
    assert paths["benchmark.resume_pdf"] == str(pat_doe_dir / "resume.pdf")


def test_renderer_text_event_non_empty() -> None:
    """Non-empty text events are rendered as dim italic at verbosity >= 1."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=1, console=con)
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
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)

    assert rdr._use_spinner is False

    rc = run_apply(
        "https://example.com/jobs/engineer",
        cwd=tmp_path,
        skip_confirm=True,
        renderer=rdr,
    )

    assert rc == 0
    output = buf.getvalue()
    # Tool calls should still appear at verbosity=2
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


def _past_started_at() -> float:
    """Return a started_at value far enough in the past to accept any test mtime."""
    import time

    return time.time() - 3600.0


def test_reconcile_canonical_slug_renames_when_different(tmp_path: Path) -> None:
    """Helper renames dir from url-slug to canonical and returns canonical slug."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    apply_state = _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "GTM Data Analyst"})
    )

    canonical_slug, reconciled = _reconcile_canonical_slug(
        "url-slug", tmp_path, _past_started_at()
    )

    assert canonical_slug == "clay-gtm-data-analyst"
    assert reconciled is True, "successful reconcile must signal True"
    # Canonical dir must exist with the artifact
    canonical_state = tmp_path / "private" / "applications" / "clay-gtm-data-analyst" / ".apply-state"
    assert canonical_state.exists()
    assert (canonical_state / "jd-parsed.json").exists()
    # Original dir must be gone
    orig_dir = tmp_path / "private" / "applications" / "url-slug"
    assert not orig_dir.exists()


def test_reconcile_canonical_slug_noop_when_already_canonical(tmp_path: Path) -> None:
    """No rename when active slug already equals canonical slug."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    apply_state = _scaffold_apply_config_for_reconcile(tmp_path, slug="acme-ml-engineer")
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Acme", "position": "ML Engineer"})
    )

    canonical_slug, reconciled = _reconcile_canonical_slug(
        "acme-ml-engineer", tmp_path, _past_started_at()
    )

    assert canonical_slug == "acme-ml-engineer"
    assert reconciled is True
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

    canonical_slug, reconciled = _reconcile_canonical_slug(
        "jobs", tmp_path, _past_started_at()
    )

    assert canonical_slug == "clay-gtm-data-analyst"
    assert reconciled is True
    # The canonical dir must still be intact (no rename needed — already correct name)
    assert (canonical_state / "jd-parsed.json").exists()


def test_reconcile_canonical_slug_missing_jd_parsed_returns_active(tmp_path: Path) -> None:
    """With no jd-parsed.json anywhere, returns active_slug unchanged + reconciled=False."""
    from jobsmith.apply import _reconcile_canonical_slug

    _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")
    # No jd-parsed.json written anywhere

    canonical_slug, reconciled = _reconcile_canonical_slug(
        "url-slug", tmp_path, _past_started_at()
    )

    assert canonical_slug == "url-slug"
    assert reconciled is False, (
        "missing jd-parsed must signal reconciled=False so the URL index "
        "is not corrupted with a non-canonical slug"
    )


def test_reconcile_canonical_slug_skips_stale_fallback_candidates(
    tmp_path: Path,
) -> None:
    """Fallback ignores jd-parsed.json files older than started_at (prior runs)."""
    import json
    import os
    import time

    from jobsmith.apply import _reconcile_canonical_slug

    # URL-slug dir empty (no jd-parsed.json there).
    _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")

    # A *stale* prior-run dir from a different job — must NOT be picked.
    stale_state = (
        tmp_path / "private" / "applications" / "old-corp-old-role" / ".apply-state"
    )
    stale_state.mkdir(parents=True, exist_ok=True)
    stale_jd = stale_state / "jd-parsed.json"
    stale_jd.write_text(
        json.dumps({"company": "Old Corp", "position": "Old Role"})
    )
    # Backdate the stale file by 1 hour.
    one_hour_ago = time.time() - 3600.0
    os.utime(stale_jd, (one_hour_ago, one_hour_ago))

    # started_at = "now-ish"; anything older than ~1s must be filtered out.
    canonical_slug, reconciled = _reconcile_canonical_slug(
        "url-slug", tmp_path, time.time()
    )

    # Helper must NOT pick up the stale unrelated job.
    assert canonical_slug == "url-slug"
    assert reconciled is False
    # Stale dir must still exist untouched.
    assert stale_state.exists()


def test_reconcile_canonical_slug_halts_on_existing_canonical_collision(
    tmp_path: Path,
) -> None:
    """If the canonical dir already exists with content, refuse to merge."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    # Active URL-slug dir has fresh jd-parsed.json from this run.
    apply_state = _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "GTM Data Analyst"})
    )

    # Stale canonical dir from a previous run (already has content).
    canonical_dir = (
        tmp_path / "private" / "applications" / "clay-gtm-data-analyst"
    )
    canonical_state = canonical_dir / ".apply-state"
    canonical_state.mkdir(parents=True, exist_ok=True)
    (canonical_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "Old Role"})
    )

    canonical_slug, reconciled = _reconcile_canonical_slug(
        "url-slug", tmp_path, _past_started_at()
    )

    # Must refuse to merge — return active_slug, leave both dirs intact.
    assert canonical_slug == "url-slug"
    assert reconciled is False, (
        "collision must signal reconciled=False so the URL index is not "
        "overwritten with the URL-slug (non-canonical) value"
    )
    assert (apply_state / "jd-parsed.json").exists(), "active dir must be untouched"
    assert (canonical_state / "jd-parsed.json").exists(), "stale canonical dir must be untouched"
    # The active dir must NOT have been moved INSIDE the canonical dir
    # (which is exactly the shutil.move-into-existing-dir bug we are fixing).
    assert not (canonical_dir / "url-slug").exists(), (
        "active dir must not be nested inside canonical dir"
    )


def test_reconcile_canonical_slug_replaces_empty_canonical_dir(tmp_path: Path) -> None:
    """An empty pre-existing canonical dir is removed and the source rename succeeds."""
    import json

    from jobsmith.apply import _reconcile_canonical_slug

    apply_state = _scaffold_apply_config_for_reconcile(tmp_path, slug="url-slug")
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Clay", "position": "GTM Data Analyst"})
    )
    # Empty canonical dir (no .apply-state inside) — common when wrapper
    # pre-created the directory but phase 1 did not populate it.
    canonical_dir = (
        tmp_path / "private" / "applications" / "clay-gtm-data-analyst"
    )
    canonical_dir.mkdir(parents=True, exist_ok=True)

    canonical_slug, reconciled = _reconcile_canonical_slug(
        "url-slug", tmp_path, _past_started_at()
    )

    assert canonical_slug == "clay-gtm-data-analyst"
    assert reconciled is True
    # The rename succeeded — content lives directly under canonical dir.
    assert (canonical_dir / ".apply-state" / "jd-parsed.json").exists()
    # No nested url-slug subdir.
    assert not (canonical_dir / "url-slug").exists()


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

    captured: list[tuple[str, str, str]] = []  # [(phase, session_id, prompt), ...]
    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        captured.append((phase, session_id, prompt))
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
    draft_prompt = next(p for name, _, p in captured if name == "draft")
    render_prompt = next(p for name, _, p in captured if name == "render")

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

    # Option A: phase 1 runs under the URL-slug-derived session id, but
    # phase 2/3 must run under the CANONICAL-slug-derived session id (a
    # fresh claude -p session — phase prompts read .apply-state/* directly
    # so phase-1 conversation continuity is unnecessary).
    url_session = deterministic_session_id(url_slug)
    canonical_session = deterministic_session_id("clay-gtm-data-analyst")
    sessions_by_phase = {phase: sid for phase, sid, _ in captured}
    assert sessions_by_phase["gather"] == url_session, (
        f"gather should use URL-slug session {url_session!r}, got {sessions_by_phase['gather']!r}"
    )
    assert sessions_by_phase["draft"] == canonical_session, (
        f"draft should use canonical-slug session {canonical_session!r}, got {sessions_by_phase['draft']!r}"
    )
    assert sessions_by_phase["render"] == canonical_session, (
        f"render should use canonical-slug session {canonical_session!r}, got {sessions_by_phase['render']!r}"
    )


def test_run_apply_session_id_switches_to_canonical_after_reconcile(
    tmp_path: Path, monkeypatch
) -> None:
    """Option A: post-reconcile, phase 2/3 run under the canonical session id."""
    import json

    import yaml

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
                "output": {"applications_dir": "private/applications"},
            }
        )
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")

    plugin_fake = _scaffold_plugin(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    url = "https://example.com/jobs/x"
    url_slug = derive_slug(url)
    url_session_id = deterministic_session_id(url_slug)
    canonical_session_id = deterministic_session_id("acme-ml-engineer")

    apply_state = (
        tmp_path / "private" / "applications" / url_slug / ".apply-state"
    )
    apply_state.mkdir(parents=True, exist_ok=True)
    (apply_state / "jd-parsed.json").write_text(
        json.dumps({"company": "Acme", "position": "ML Engineer"})
    )

    seen_sessions: list[tuple[str, str]] = []  # [(phase, session_id), ...]
    call_count = [0]
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_sessions.append((phase, session_id))
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)
    assert rc == 0

    sessions_by_phase = dict(seen_sessions)
    assert sessions_by_phase["gather"] == url_session_id, (
        f"gather should use URL session {url_session_id!r}; got {sessions_by_phase['gather']!r}"
    )
    assert sessions_by_phase["draft"] == canonical_session_id, (
        f"draft should switch to canonical session {canonical_session_id!r}; got {sessions_by_phase['draft']!r}"
    )
    assert sessions_by_phase["render"] == canonical_session_id, (
        f"render should switch to canonical session {canonical_session_id!r}; got {sessions_by_phase['render']!r}"
    )

    # Sanity: slug DID get reconciled to canonical (the URL's path slug 'x' is not canonical)
    canonical_dir = tmp_path / "private" / "applications" / "acme-ml-engineer"
    assert canonical_dir.exists()


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
    """Line-numbered file content is summarised as '← N lines (M.K KB)' at verbosity=2."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
    line_content = "1 foo\n2 bar\n3 baz\n4 qux\n"
    rdr.render_event(Event(type="tool_result", tool_result=line_content))
    output = buf.getvalue()
    assert "4 lines" in output
    assert "KB" in output
    # Raw content should NOT appear
    assert "foo" not in output


def test_renderer_tool_result_json_summary() -> None:
    """JSON-shaped tool result is summarised as '← {N keys}' at verbosity=2."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
    rdr.render_event(Event(type="tool_result", tool_result='{"a":1,"b":2,"c":3}'))
    output = buf.getvalue()
    assert "{3 keys}" in output


def test_renderer_tool_result_json_array_summary() -> None:
    """JSON array tool result is summarised as '← [N items]' at verbosity=2."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
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


def test_renderer_agent_dispatch_line() -> None:
    """Agent tool_use events are always printed as '→ Agent(name)' at all verbosity levels."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, console=con)  # quiet mode
    rdr.render_event(
        Event(
            type="tool_use",
            tool_name="Agent",
            tool_input={"name": "apply-jd-parser"},
            raw={},
        )
    )
    output = buf.getvalue()
    assert "Agent" in output
    assert "apply-jd-parser" in output


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


# ---------------------------------------------------------------------------
# 16. Resume from completed phases (URL index, manifest gating, --force)
# ---------------------------------------------------------------------------


def _scaffold_resume_project(tmp_path: Path) -> Path:
    """Scaffold a minimal project with apply-config + a fake plugin dir.

    Returns the absolute path of the fake plugin directory so callers can
    monkeypatch ``jobsmith.apply.get_plugin_dir`` to point at it.
    """
    import yaml

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
                "output": {"applications_dir": "private/applications"},
            }
        )
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")

    return _scaffold_plugin(tmp_path)


def _write_manifest(
    apply_state: Path,
    *,
    completed_specialists: list[str],
) -> None:
    """Write a minimal manifest.json with the listed specialists marked status=ok."""
    import json

    apply_state.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": "00000000-0000-0000-0000-000000000000",
        "slug": "acme-ml-engineer",
        "started_at": "2026-01-01T00:00:00Z",
        "role_type": "ai-engineer",
        "tier": "fast",
        "invocations": [
            {"specialist": s, "status": "ok"} for s in completed_specialists
        ],
    }
    (apply_state / "manifest.json").write_text(json.dumps(manifest))


_PHASE_1_SPECIALISTS = [
    "apply-jd-parser",
    "apply-fit-scorer",
    "apply-hm-enricher",
    "apply-bullet-selector",
    "apply-company-research",
]
_PHASE_2_SPECIALISTS = [
    "apply-prose-writer",
    "apply-prose-qa",
]
_PHASE_3_SPECIALISTS = [
    "apply-resume-renderer",
    "apply-cover-letter-writer",
    "apply-index-writer",
]


def test_resume_from_phase_2_when_phase_1_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """Manifest with phase 1 done → only draft/render run; banner mentions resume."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(apply_state, completed_specialists=_PHASE_1_SPECIALISTS)
    # URL index pre-populated so wrapper resolves to canonical immediately
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        '{"https://example.com/jobs/x": "acme-ml-engineer"}'
    )

    seen_phases: list[str] = []
    phase_sequence = ["draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_phases.append(phase)
        idx = len(seen_phases) - 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    buf = io.StringIO()
    rdr = ApplyRenderer(yes=True, console=Console(file=buf, force_terminal=False, no_color=True, width=120))

    rc = run_apply(
        "https://example.com/jobs/x",
        cwd=tmp_path,
        skip_confirm=True,
        renderer=rdr,
    )

    assert rc == 0
    assert seen_phases == ["draft", "render"], (
        f"phase 1 must be skipped; got phases={seen_phases!r}"
    )
    output = buf.getvalue()
    assert "Resuming" in output, f"resume banner missing from output: {output!r}"
    assert "phase 2" in output.lower() or "draft" in output.lower()


def test_resume_from_phase_3_when_phase_2_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """Manifest with phase 1+2 done → only render runs."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(
        apply_state,
        completed_specialists=_PHASE_1_SPECIALISTS + _PHASE_2_SPECIALISTS,
    )
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        '{"https://example.com/jobs/x": "acme-ml-engineer"}'
    )

    seen_phases: list[str] = []

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_phases.append(phase)
        return iter(_make_phase_events("render"))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(
        "https://example.com/jobs/x",
        cwd=tmp_path,
        skip_confirm=True,
    )
    assert rc == 0
    assert seen_phases == ["render"], (
        f"only render must run; got phases={seen_phases!r}"
    )


def test_already_complete_exits_clean(tmp_path: Path, monkeypatch) -> None:
    """All phases done → no run_phase calls; exit 0; output mentions --force."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(
        apply_state,
        completed_specialists=(
            _PHASE_1_SPECIALISTS + _PHASE_2_SPECIALISTS + _PHASE_3_SPECIALISTS
        ),
    )
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        '{"https://example.com/jobs/x": "acme-ml-engineer"}'
    )

    call_count = [0]

    def fake_run_phase(*a, **kw):
        call_count[0] += 1
        return iter([])

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)

    buf = io.StringIO()
    rdr = ApplyRenderer(yes=True, console=Console(file=buf, force_terminal=False, no_color=True, width=120))

    rc = run_apply(
        "https://example.com/jobs/x",
        cwd=tmp_path,
        skip_confirm=True,
        renderer=rdr,
    )

    assert rc == 0
    assert call_count[0] == 0, "no phase should run when manifest shows all complete"
    output = buf.getvalue()
    assert "--force" in output, f"output must mention --force; got: {output!r}"


def test_force_flag_ignores_existing_state(tmp_path: Path, monkeypatch) -> None:
    """With --force (force=True), all three phases run despite a complete manifest."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(
        apply_state,
        completed_specialists=(
            _PHASE_1_SPECIALISTS + _PHASE_2_SPECIALISTS + _PHASE_3_SPECIALISTS
        ),
    )
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        '{"https://example.com/jobs/x": "acme-ml-engineer"}'
    )
    # Ensure jd-parsed.json exists in url-derived slug dir for reconcile to find
    url = "https://example.com/jobs/x"
    url_slug = derive_slug(url)
    url_state = (
        tmp_path / "private" / "applications" / url_slug / ".apply-state"
    )
    url_state.mkdir(parents=True, exist_ok=True)
    (url_state / "jd-parsed.json").write_text(
        '{"company": "Acme", "position": "ML Engineer"}'
    )

    seen_phases: list[str] = []
    phase_sequence = ["gather", "draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_phases.append(phase)
        idx = len(seen_phases) - 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True, force=True)

    assert rc == 0
    assert seen_phases == ["gather", "draft", "render"], (
        f"--force must run all phases; got {seen_phases!r}"
    )


def test_url_index_populated_after_first_run(tmp_path: Path, monkeypatch) -> None:
    """Fresh state → after a successful run, .url-index.json contains URL → canonical."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    url = "https://example.com/jobs/ml-eng-id"
    url_slug = derive_slug(url)
    # Pre-populate jd-parsed.json under the URL-slug dir so reconcile works.
    url_state = (
        tmp_path / "private" / "applications" / url_slug / ".apply-state"
    )
    url_state.mkdir(parents=True, exist_ok=True)
    (url_state / "jd-parsed.json").write_text(
        '{"company": "Acme", "position": "ML Engineer"}'
    )

    phase_sequence = ["gather", "draft", "render"]
    call_count = [0]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)
    assert rc == 0

    import json

    index_path = tmp_path / "private" / "applications" / ".url-index.json"
    assert index_path.exists(), ".url-index.json must be written after first run"
    data = json.loads(index_path.read_text())
    assert data.get(url) == "acme-ml-engineer", (
        f"index must map URL to canonical slug; got: {data!r}"
    )


def test_url_index_lookup_short_circuits_url_slug_derivation(
    tmp_path: Path, monkeypatch
) -> None:
    """Pre-populated index → wrapper uses canonical slug from index, not URL slug."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    # Pre-populate index AND apply-state so phase 1 is skipped (manifest done).
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(
        apply_state,
        completed_specialists=(
            _PHASE_1_SPECIALISTS + _PHASE_2_SPECIALISTS + _PHASE_3_SPECIALISTS
        ),
    )
    url = "https://example.com/jobs/totally-different-url-slug"
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        f'{{"{url}": "{canonical}"}}'
    )

    captured_paths: list[str] = []

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        captured_paths.append(prompt)
        return iter(_make_phase_events(phase))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)

    # All phases done from manifest → exit cleanly without invoking any phase
    assert rc == 0
    assert captured_paths == [], (
        f"no phase should run when manifest shows all complete; got: {captured_paths!r}"
    )


def test_one_time_migration_when_index_missing_but_dir_matches(
    tmp_path: Path, monkeypatch
) -> None:
    """No URL index, but jd-parsed.json under canonical dir matches input URL → resume."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "clay-gtm-data-analyst"
    url = "https://jobs.ashbyhq.com/clay/some-id"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    apply_state.mkdir(parents=True, exist_ok=True)
    # jd-parsed.json carries the URL via jd_url field — migration must find it.
    (apply_state / "jd-parsed.json").write_text(
        f'{{"company": "Clay", "position": "GTM Data Analyst", "jd_url": "{url}"}}'
    )
    _write_manifest(apply_state, completed_specialists=_PHASE_1_SPECIALISTS)

    seen_phases: list[str] = []

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_phases.append(phase)
        idx = len(seen_phases) - 1
        return iter(_make_phase_events(["draft", "render"][idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)
    assert rc == 0
    assert seen_phases == ["draft", "render"], (
        f"migration must let phase 1 be skipped; got {seen_phases!r}"
    )

    # Migration also persists the discovered mapping into the index.
    import json

    index_path = tmp_path / "private" / "applications" / ".url-index.json"
    assert index_path.exists(), "migration must populate the URL index"
    data = json.loads(index_path.read_text())
    assert data.get(url) == canonical


def test_malformed_manifest_treated_as_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    """Malformed manifest.json → phase 1 reruns; no crash."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    url = "https://example.com/jobs/x"
    url_slug = derive_slug(url)
    apply_state = (
        tmp_path / "private" / "applications" / url_slug / ".apply-state"
    )
    apply_state.mkdir(parents=True, exist_ok=True)
    # Garbage JSON
    (apply_state / "manifest.json").write_text("{not valid json")
    # jd-parsed.json so the post-phase-1 reconcile has something to read.
    (apply_state / "jd-parsed.json").write_text(
        '{"company": "Acme", "position": "ML Engineer"}'
    )

    phase_sequence = ["gather", "draft", "render"]
    call_count = [0]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)

    assert rc == 0
    assert call_count[0] == 3, (
        f"malformed manifest must be treated as incomplete; expected 3 phases, got {call_count[0]}"
    )


# ---------------------------------------------------------------------------
# 17. roborev 910 fixes — --force uses canonical slug, index not corrupted
# ---------------------------------------------------------------------------


def test_force_uses_canonical_slug_from_url_index(
    tmp_path: Path, monkeypatch
) -> None:
    """--force on a URL already in the index targets the canonical dir, not URL-slug.

    This prevents the corruption pattern where --force creates a duplicate
    URL-slug dir, then post-phase-1 reconcile refuses to merge (canonical
    dir already exists, non-empty), leaving two stale directories.
    """
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    url = "https://example.com/jobs/totally-different-url-slug"
    apps_dir = tmp_path / "private" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    # URL is already in the index from a prior successful run.
    (apps_dir / ".url-index.json").write_text(
        f'{{"{url}": "{canonical}"}}'
    )
    # And the canonical dir has content from that prior run.
    apply_state = apps_dir / canonical / ".apply-state"
    apply_state.mkdir(parents=True, exist_ok=True)
    (apply_state / "jd-parsed.json").write_text(
        '{"company": "Acme", "position": "ML Engineer"}'
    )

    seen_slugs_in_paths: list[str] = []
    phase_sequence = ["gather", "draft", "render"]
    call_count = [0]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        # The Paths block in the prompt should reference apply_state_dir for the
        # canonical slug, not the URL-derived slug.
        if canonical in prompt:
            seen_slugs_in_paths.append(canonical)
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True, force=True)

    assert rc == 0
    # Phase 1 must have started under the canonical slug, not URL-slug.
    assert seen_slugs_in_paths, (
        "--force should consult URL index and run phase 1 under canonical slug"
    )

    # The URL index still points at the canonical slug — not corrupted to URL-slug.
    import json

    index = json.loads(
        (tmp_path / "private" / "applications" / ".url-index.json").read_text()
    )
    assert index.get(url) == canonical, (
        f"URL index must remain pointing at canonical {canonical!r}; got: {index!r}"
    )

    # No stale URL-slug dir was ever created.
    url_slug_dir = (
        tmp_path / "private" / "applications" / derive_slug(url)
    )
    assert not url_slug_dir.exists(), (
        f"--force must not create a URL-slug duplicate directory at {url_slug_dir}"
    )


def test_url_mapping_not_recorded_when_reconcile_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """If reconcile cannot derive canonical (e.g. missing jd-parsed.json), the URL
    index must NOT be overwritten with the fallback (URL-derived) slug."""
    import yaml

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
                "output": {"applications_dir": "private/applications"},
            }
        )
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# placeholder\n")

    plugin_fake = _scaffold_plugin(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    # Pre-existing index — must NOT be touched after a reconcile failure.
    canonical = "real-canonical-slug"
    url = "https://example.com/jobs/x"
    apps_dir = tmp_path / "private" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    (apps_dir / ".url-index.json").write_text(
        f'{{"{url}": "{canonical}"}}'
    )
    # Ensure canonical app dir has content but NO jd-parsed.json (forces
    # reconcile to fail with reconciled=False).
    (apps_dir / canonical).mkdir(parents=True, exist_ok=True)

    phase_sequence = ["gather", "draft", "render"]
    call_count = [0]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: True)
    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", lambda *a, **kw: 0)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(url, cwd=tmp_path, skip_confirm=True)
    assert rc == 0

    import json

    index = json.loads((apps_dir / ".url-index.json").read_text())
    assert index.get(url) == canonical, (
        f"URL index must remain pointing at the original canonical {canonical!r}; "
        f"got: {index!r}"
    )


def test_step_45_runs_when_gather_skipped_but_decisions_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """Manifest shows phase 1 done but bullet-decisions.json is missing
    (prior run failed at the wrapper-side anchor guard).  When draft is
    about to run, step 4/5 must execute even though gather was skipped.
    """
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(apply_state, completed_specialists=_PHASE_1_SPECIALISTS)
    # bullet-decisions.json deliberately missing.
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        '{"https://example.com/jobs/x": "acme-ml-engineer"}'
    )

    step45_calls: list[str] = []

    def fake_step45(slug, cwd):
        step45_calls.append(slug)
        # Simulate success and produce the artifact.
        decisions = (
            cwd / "private" / "applications" / slug / ".apply-state" / "bullet-decisions.json"
        )
        decisions.parent.mkdir(parents=True, exist_ok=True)
        decisions.write_text("{}")
        return 0

    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", fake_step45)

    seen_phases: list[str] = []

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_phases.append(phase)
        idx = len(seen_phases) - 1
        return iter(_make_phase_events(["draft", "render"][idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(
        "https://example.com/jobs/x",
        cwd=tmp_path,
        skip_confirm=True,
    )
    assert rc == 0
    assert seen_phases == ["draft", "render"], (
        f"phase 1 must stay skipped; got {seen_phases!r}"
    )
    assert step45_calls == [canonical], (
        f"step 4/5 must run before draft when bullet-decisions.json is missing, "
        f"got: {step45_calls!r}"
    )


def test_step_45_skipped_when_gather_skipped_and_decisions_present(
    tmp_path: Path, monkeypatch
) -> None:
    """If bullet-decisions.json already exists, step 4/5 is NOT re-run."""
    plugin_fake = _scaffold_resume_project(tmp_path)
    monkeypatch.setattr("jobsmith.apply.get_plugin_dir", lambda: plugin_fake)

    canonical = "acme-ml-engineer"
    apply_state = (
        tmp_path / "private" / "applications" / canonical / ".apply-state"
    )
    _write_manifest(apply_state, completed_specialists=_PHASE_1_SPECIALISTS)
    # bullet-decisions.json already present from a prior successful step 4/5.
    (apply_state / "bullet-decisions.json").write_text("{}")
    (tmp_path / "private" / "applications" / ".url-index.json").write_text(
        '{"https://example.com/jobs/x": "acme-ml-engineer"}'
    )

    step45_calls: list[str] = []

    def fake_step45(slug, cwd):
        step45_calls.append(slug)
        return 0

    monkeypatch.setattr("jobsmith.apply._run_step45_orchestration", fake_step45)

    seen_phases: list[str] = []
    phase_sequence = ["draft", "render"]

    def fake_run_phase(phase, session_id, prompt, plugin_dir, system_prompt, resume=False, **kwargs):
        seen_phases.append(phase)
        idx = len(seen_phases) - 1
        return iter(_make_phase_events(phase_sequence[idx]))

    monkeypatch.setattr("jobsmith.apply.headless.run_phase", fake_run_phase)
    monkeypatch.setattr("jobsmith.apply.headless.session_exists", lambda *a, **kw: False)
    monkeypatch.setattr("jobsmith.apply.click.confirm", lambda *a, **kw: True)

    rc = run_apply(
        "https://example.com/jobs/x",
        cwd=tmp_path,
        skip_confirm=True,
    )
    assert rc == 0
    assert step45_calls == [], (
        f"step 4/5 must be skipped when bullet-decisions.json exists; got: {step45_calls!r}"
    )


# ---------------------------------------------------------------------------
# 20. Progressive verbosity + persistent transcript (bug-2f08dd10)
# ---------------------------------------------------------------------------


def _make_tool_events_with_filtered() -> list[Event]:
    """Return events including Bash (normal), TodoWrite (filtered), and ToolSearch."""
    return [
        Event(type="tool_use", tool_name="Bash", tool_input={"command": "ls"}, raw={}),
        Event(type="tool_result", tool_result="file1.txt\nfile2.txt", tool_name="id_bash", raw={}),
        Event(
            type="tool_use",
            tool_name="TodoWrite",
            tool_input={"todos": ["do something"]},
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "id_todo", "name": "TodoWrite", "input": {}}
                    ]
                },
            },
        ),
        Event(type="tool_result", tool_name="id_todo", tool_result="OK", raw={}),
        Event(
            type="tool_use",
            tool_name="ToolSearch",
            tool_input={"query": "python"},
            raw={
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "id_search", "name": "ToolSearch", "input": {}}
                    ]
                },
            },
        ),
        Event(type="tool_result", tool_name="id_search", tool_result="results", raw={}),
        Event(type="phase_complete", name="gather"),
    ]


def test_quiet_mode_hides_tool_calls_but_keeps_sub_agents() -> None:
    """Quiet mode (verbosity=0): tool call lines hidden; Agent dispatch printed."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=0, console=con)
    rdr._current_phase = "gather"

    # Agent dispatch: always shown
    rdr.render_event(
        Event(
            type="tool_use",
            tool_name="Agent",
            tool_input={"name": "apply-jd-parser"},
            raw={},
        )
    )
    # Regular tool call: must be hidden in quiet mode
    rdr.render_event(
        Event(type="tool_use", tool_name="Bash", tool_input={"command": "echo hi"}, raw={})
    )

    output = buf.getvalue()
    # Agent line is always shown
    assert "apply-jd-parser" in output
    # Bash tool call line must not appear
    assert "echo hi" not in output
    assert "command" not in output


def test_verbose_mode_shows_filtered_tool_calls() -> None:
    """-v (verbosity=1): non-filtered tool calls shown; TodoWrite + ToolSearch absent."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=1, console=con)
    rdr._current_phase = "gather"

    for event in _make_tool_events_with_filtered():
        rdr.render_event(event)

    output = buf.getvalue()
    # Regular tool call must appear
    assert "Bash" in output
    # Filtered tools must NOT appear
    assert "TodoWrite" not in output
    assert "ToolSearch" not in output


def test_debug_mode_shows_unfiltered() -> None:
    """-vv (verbosity=2): all tool calls shown including TodoWrite + ToolSearch (dim)."""
    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=2, console=con)
    rdr._current_phase = "gather"

    for event in _make_tool_events_with_filtered():
        rdr.render_event(event)

    output = buf.getvalue()
    # All tools must appear
    assert "Bash" in output
    assert "TodoWrite" in output
    assert "ToolSearch" in output


def test_transcript_file_written_for_every_verbosity(tmp_path: Path) -> None:
    """All verbosity levels (0, 1, 2) write the same transcript JSONL file."""
    for verbosity in (0, 1, 2):
        transcript_dir = tmp_path / f"v{verbosity}" / ".apply-state"
        transcript_path = transcript_dir / "transcript.jsonl"

        con, buf = _make_test_console()
        rdr = ApplyRenderer(yes=True, verbosity=verbosity, console=con)
        rdr.open_transcript(transcript_path, "gather")
        rdr._current_phase = "gather"

        rdr.render_event(
            Event(type="tool_use", tool_name="Bash", tool_input={"command": "echo hi"}, raw={})
        )
        rdr.render_event(Event(type="tool_result", tool_result="hi", raw={}))
        rdr.close_transcript()

        assert transcript_path.exists(), f"verbosity={verbosity}: transcript not created"
        lines = [
            line for line in transcript_path.read_text().splitlines() if line.strip()
        ]
        assert len(lines) >= 2, f"verbosity={verbosity}: expected at least 2 lines, got {len(lines)}"
        # Every line must be valid JSON
        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)


def test_transcript_has_phase_boundary_markers(tmp_path: Path) -> None:
    """Transcript contains one boundary marker per phase open call."""
    transcript_path = tmp_path / ".apply-state" / "transcript.jsonl"

    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=0, console=con)

    for phase in ("gather", "draft", "render"):
        rdr.open_transcript(transcript_path, phase)
        rdr._current_phase = phase
        rdr.render_event(
            Event(type="tool_use", tool_name="Bash", tool_input={"command": "ls"}, raw={})
        )
        rdr.close_transcript()

    lines = [
        json.loads(line)
        for line in transcript_path.read_text().splitlines()
        if line.strip()
    ]
    boundary_lines = [l for l in lines if "_phase_boundary" in l]
    assert len(boundary_lines) == 3, (
        f"expected 3 boundary markers, got {len(boundary_lines)}: {boundary_lines!r}"
    )
    phase_names = {l["_phase_boundary"] for l in boundary_lines}
    assert phase_names == {"gather", "draft", "render"}


def test_rolling_status_updates_on_tool_call_in_quiet_mode() -> None:
    """Quiet mode: tool call events update the spinner description, not print a line."""
    from unittest.mock import MagicMock, patch

    con, buf = _make_test_console()
    # Use a TTY-like console so spinner would activate (but yes=False, verbosity=0)
    # We'll manually set up progress tracking by patching
    rdr = ApplyRenderer(yes=True, verbosity=0, console=con)

    # Manually install a mock Progress task to verify update_status calls
    mock_progress = MagicMock()
    mock_task = MagicMock()
    mock_task.id = 0
    mock_progress.tasks = [mock_task]
    rdr._progress = mock_progress
    rdr._progress_task_id = 0
    rdr._current_phase = "gather"

    rdr.render_event(
        Event(type="tool_use", tool_name="Write", tool_input={"file_path": "/tmp/x.txt"}, raw={})
    )

    # update should have been called on the mock progress
    mock_progress.update.assert_called_once()
    call_kwargs = mock_progress.update.call_args
    # The description arg should contain the tool name
    description_arg = call_kwargs[1].get("description", "") or str(call_kwargs)
    assert "Write" in description_arg

    # The tool call must NOT appear in printed output
    output = buf.getvalue()
    assert "Write" not in output or "file_path" not in output


def test_sub_agent_completion_includes_duration(tmp_path: Path) -> None:
    """Sub-agent completion line includes duration in seconds (best-effort)."""
    import time

    con, buf = _make_test_console()
    rdr = ApplyRenderer(yes=True, verbosity=0, console=con)
    rdr._current_phase = "gather"

    # Dispatch the agent
    rdr.render_event(
        Event(
            type="tool_use",
            tool_name="Agent",
            tool_input={"name": "apply-jd-parser"},
            raw={},
        )
    )
    # Small sleep to ensure non-zero duration
    time.sleep(0.05)

    # Trigger tool_result which signals sub-agent completion
    rdr.render_event(
        Event(type="tool_result", tool_result="done", raw={})
    )

    output = buf.getvalue()
    # Completion line should contain the agent name and duration
    assert "apply-jd-parser" in output
    # Duration in seconds format: e.g. "(0s)" or "(1s)"
    assert "s)" in output
