"""Tests for bug-88fcc597: warm-start and gather-specialist reuse replay
wired into the primary _run_apply_phases path.

Covers:
  1. test_warmstart_suffix_appended_to_draft_prompt
       When reuse_plan.draft.decision == "warm-start", the draft prompt
       passed to headless.run_phase must include the warm-start suffix
       (contains "Warm-start mode").

  2. test_warmstart_suffix_not_appended_when_regenerate
       When draft.decision == "regenerate", the draft prompt must NOT
       include "Warm-start mode".

  3. test_gather_replay_copies_jd_parsed_when_reuse
       When reuse_plan.jd_parse.decision == "reuse" and a prior matched
       slug exists, jd-parsed.json from the prior state dir is copied
       into the current state dir BEFORE the gather phase agent call.

  4. test_gather_replay_copies_fit_score_when_reuse
       Same gate for fit-score.json.

  5. test_gather_replay_skipped_when_no_reuse_flag
       When no_reuse=True (--no-reuse), no artifacts are replayed even
       if a matched_slug exists.

  6. test_gather_replay_skipped_when_regenerate_decision
       When jd_parse.decision == "regenerate", no artifacts are replayed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from jobsmith.apply import _PHASES, derive_slug, run_apply
from jobsmith.headless import Event
from jobsmith.render import ApplyRenderer
from jobsmith.reuse.planner import PhaseDecision, ReusePlan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_phase_events(phase_name: str) -> list[Event]:
    return [
        Event(type="text", text=f"Running {phase_name}..."),
        Event(type="phase_complete", name=phase_name),
    ]


def _fake_run_phase_factory(phase_event_map: dict[str, list[Event]]):
    def _fake_run_phase(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        events = phase_event_map.get(phase, [Event(type="phase_complete", name=phase)])
        yield from events

    return _fake_run_phase


def _minimal_repo(tmp_path: Path) -> Path:
    config = tmp_path / ".apply-config.yaml"
    config.write_text(
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "output:\n"
        "  applications_dir: private/applications\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )
    content = tmp_path / "assets" / "content"
    content.mkdir(parents=True)
    for name in ("work.yml", "skill.yml", "education.yml", "author.yml"):
        (content / name).write_text("# stub\n")
    (tmp_path / "private" / "applications").mkdir(parents=True)
    return tmp_path


def _mock_plugin_dir(tmp_path: Path) -> Path:
    pdir = tmp_path / "plugin"
    sp_dir = pdir / "system-prompts"
    sp_dir.mkdir(parents=True)
    for phase_name, phase_num in _PHASES:
        (sp_dir / f"phase-{phase_num}-{phase_name}.md").write_text(
            f"# {phase_name}\n"
        )
    return pdir


def _seed_prior_state(
    apps_dir: Path,
    prior_slug: str,
    jd_parsed: dict | None = None,
    fit_score: dict | None = None,
) -> Path:
    """Create prior application's .apply-state with optional artifacts."""
    state_dir = apps_dir / prior_slug / ".apply-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if jd_parsed is not None:
        (state_dir / "jd-parsed.json").write_text(
            json.dumps(jd_parsed), encoding="utf-8"
        )
    if fit_score is not None:
        (state_dir / "fit-score.json").write_text(
            json.dumps(fit_score), encoding="utf-8"
        )
    # Seed bullet-selection.json and prose-draft.md for warm-start
    (state_dir / "bullet-selection.json").write_text(
        json.dumps({"positions": []}), encoding="utf-8"
    )
    (state_dir / "prose-draft.md").write_text("Prior prose draft.\n", encoding="utf-8")
    return state_dir


def _warm_start_plan(prior_slug: str) -> ReusePlan:
    return ReusePlan(
        jd_parse=PhaseDecision(decision="regenerate", source=None, score=0.0),
        fit_score=PhaseDecision(decision="regenerate", source=None, score=0.0),
        company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
        bullet_map={},
        matched_slug=prior_slug,
        draft=PhaseDecision(decision="warm-start", source=prior_slug, score=0.85),
        jd_overlap_score=0.85,
    )


def _reuse_plan(prior_slug: str) -> ReusePlan:
    return ReusePlan(
        jd_parse=PhaseDecision(decision="reuse", source=prior_slug, score=0.98),
        fit_score=PhaseDecision(decision="reuse", source=prior_slug, score=0.98),
        company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
        bullet_map={},
        matched_slug=prior_slug,
        draft=PhaseDecision(decision="regenerate", source=None, score=0.0),
        jd_overlap_score=0.98,
    )


def _regenerate_plan() -> ReusePlan:
    return ReusePlan(
        jd_parse=PhaseDecision(decision="regenerate", source=None, score=0.0),
        fit_score=PhaseDecision(decision="regenerate", source=None, score=0.0),
        company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
        bullet_map={},
        matched_slug=None,
        draft=PhaseDecision(decision="regenerate", source=None, score=0.0),
        jd_overlap_score=0.0,
    )


# ---------------------------------------------------------------------------
# 1. Warm-start suffix appended to draft prompt
# ---------------------------------------------------------------------------


def test_warmstart_suffix_appended_to_draft_prompt(tmp_path: Path) -> None:
    """When draft.decision == 'warm-start', draft prompt must contain warm-start suffix."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/warm-start-role"
    slug = derive_slug(url)
    prior_slug = "prior-warm-app"

    apps_dir = repo / "private" / "applications"
    _seed_prior_state(apps_dir, prior_slug)

    # Seed current state dir with jd-parsed.json so warm-start prompt builder works
    current_state = apps_dir / slug / ".apply-state"
    current_state.mkdir(parents=True, exist_ok=True)
    (current_state / "jd-parsed.json").write_text(
        json.dumps({"must_haves": [], "nice_to_haves": []}), encoding="utf-8"
    )

    captured_prompts: dict[str, str] = {}
    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        captured_prompts[phase] = kwargs.get("prompt", "")
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    plan = _warm_start_plan(prior_slug)

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith._cli_apply._compute_pipeline_reuse_plan", return_value=plan),
    ):
        rc = run_apply(url, cwd=repo, skip_confirm=True, force=True, renderer=rdr)

    assert rc == 0, f"run_apply returned {rc}"
    assert "draft" in captured_prompts, "draft phase was not called"
    draft_prompt = captured_prompts["draft"]
    assert "Warm-start mode" in draft_prompt, (
        f"Draft prompt missing warm-start suffix when plan.draft.decision=='warm-start'.\n"
        f"Got prompt prefix: {draft_prompt[:300]!r}"
    )


# ---------------------------------------------------------------------------
# 2. No warm-start suffix when decision == "regenerate"
# ---------------------------------------------------------------------------


def test_warmstart_suffix_not_appended_when_regenerate(tmp_path: Path) -> None:
    """When draft.decision == 'regenerate', draft prompt must NOT have warm-start suffix."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/regenerate-role"
    slug = derive_slug(url)

    captured_prompts: dict[str, str] = {}
    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        captured_prompts[phase] = kwargs.get("prompt", "")
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    plan = _regenerate_plan()

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith._cli_apply._compute_pipeline_reuse_plan", return_value=plan),
    ):
        rc = run_apply(url, cwd=repo, skip_confirm=True, force=True, renderer=rdr)

    assert rc == 0, f"run_apply returned {rc}"
    assert "draft" in captured_prompts, "draft phase was not called"
    draft_prompt = captured_prompts["draft"]
    assert "Warm-start mode" not in draft_prompt, (
        "Draft prompt must NOT include warm-start suffix when plan.draft.decision=='regenerate'"
    )


# ---------------------------------------------------------------------------
# 3. Gather replay copies jd-parsed.json when jd_parse.decision == "reuse"
# ---------------------------------------------------------------------------


def test_gather_replay_copies_jd_parsed_when_reuse(tmp_path: Path) -> None:
    """Before gather runs, jd-parsed.json from matched slug is copied to current state."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/replay-jd-parse"
    slug = derive_slug(url)
    prior_slug = "prior-reuse-app"

    apps_dir = repo / "private" / "applications"
    prior_jd = {
        "company": "PriorCo",
        "position": "Engineer",
        "jd_text_clean": "Python dev role",
        "must_haves": [],
        "nice_to_haves": [],
    }
    _seed_prior_state(apps_dir, prior_slug, jd_parsed=prior_jd)

    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    plan = _reuse_plan(prior_slug)

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith._cli_apply._compute_pipeline_reuse_plan", return_value=plan),
    ):
        rc = run_apply(url, cwd=repo, skip_confirm=True, force=True, renderer=rdr)

    assert rc == 0, f"run_apply returned {rc}"

    current_jd_path = apps_dir / slug / ".apply-state" / "jd-parsed.json"
    assert current_jd_path.exists(), (
        "jd-parsed.json was not replayed into the current state dir"
    )
    replayed = json.loads(current_jd_path.read_text(encoding="utf-8"))
    assert replayed.get("company") == "PriorCo", (
        f"Replayed jd-parsed.json does not match prior — got: {replayed!r}"
    )


# ---------------------------------------------------------------------------
# 4. Gather replay copies fit-score.json when reuse decision
# ---------------------------------------------------------------------------


def test_gather_replay_copies_fit_score_when_reuse(tmp_path: Path) -> None:
    """Before gather runs, fit-score.json from matched slug is copied when reuse."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/replay-fit-score"
    slug = derive_slug(url)
    prior_slug = "prior-fit-app"

    apps_dir = repo / "private" / "applications"
    prior_fit = {"score": 87, "summary": "Strong match", "gaps": []}
    _seed_prior_state(apps_dir, prior_slug, fit_score=prior_fit)

    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    plan = _reuse_plan(prior_slug)

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith._cli_apply._compute_pipeline_reuse_plan", return_value=plan),
    ):
        rc = run_apply(url, cwd=repo, skip_confirm=True, force=True, renderer=rdr)

    assert rc == 0, f"run_apply returned {rc}"

    current_fit_path = apps_dir / slug / ".apply-state" / "fit-score.json"
    assert current_fit_path.exists(), (
        "fit-score.json was not replayed into the current state dir"
    )
    replayed = json.loads(current_fit_path.read_text(encoding="utf-8"))
    assert replayed.get("score") == 87, (
        f"Replayed fit-score.json does not match prior — got: {replayed!r}"
    )


# ---------------------------------------------------------------------------
# 4b. Per-artifact gating: jd_parse=reuse but fit_score=regenerate must NOT
#     replay fit-score.json (roborev job 994 — HIGH).
# ---------------------------------------------------------------------------


def test_gather_replay_gates_fit_score_independently(tmp_path: Path) -> None:
    """jd_parse=reuse + fit_score=regenerate copies jd-parsed.json but NOT fit-score.json.

    The two phase decisions are independent. Pre-populating fit-score.json on the
    jd_parse decision alone would make the gather specialist skip a regeneration
    the plan explicitly requested, carrying stale fit data forward.
    """
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/replay-mixed-decisions"
    slug = derive_slug(url)
    prior_slug = "prior-mixed-app"

    apps_dir = repo / "private" / "applications"
    # Prior app has BOTH artifacts available on disk.
    _seed_prior_state(
        apps_dir,
        prior_slug,
        jd_parsed={
            "company": "PriorCo",
            "position": "Engineer",
            "jd_text_clean": "Python dev role",
            "must_haves": [],
            "nice_to_haves": [],
        },
        fit_score={"score": 87, "summary": "Strong match", "gaps": []},
    )

    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    # jd_parse reusable, fit_score must regenerate.
    plan = ReusePlan(
        jd_parse=PhaseDecision(decision="reuse", source=prior_slug, score=0.98),
        fit_score=PhaseDecision(decision="regenerate", source=None, score=0.0),
        company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
        bullet_map={},
        matched_slug=prior_slug,
        draft=PhaseDecision(decision="regenerate", source=None, score=0.0),
    )

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith._cli_apply._compute_pipeline_reuse_plan", return_value=plan),
    ):
        rc = run_apply(url, cwd=repo, skip_confirm=True, force=True, renderer=rdr)

    assert rc == 0, f"run_apply returned {rc}"

    state_dir = apps_dir / slug / ".apply-state"
    assert (state_dir / "jd-parsed.json").exists(), (
        "jd-parsed.json should be replayed (jd_parse.decision == reuse)"
    )
    assert not (state_dir / "fit-score.json").exists(), (
        "fit-score.json must NOT be replayed when fit_score.decision == regenerate "
        "— stale fit data would suppress the requested regeneration"
    )


# ---------------------------------------------------------------------------
# 5. Gather replay skipped when --no-reuse
# ---------------------------------------------------------------------------


def test_gather_replay_skipped_when_no_reuse_flag(tmp_path: Path) -> None:
    """When no_reuse=True, no artifacts are pre-copied even if matched_slug exists."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/no-reuse-replay"
    slug = derive_slug(url)
    prior_slug = "prior-no-reuse-app"

    apps_dir = repo / "private" / "applications"
    prior_jd = {"company": "ShouldNeverAppear", "must_haves": [], "nice_to_haves": []}
    _seed_prior_state(apps_dir, prior_slug, jd_parsed=prior_jd)

    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    # no_reuse=True means _compute_pipeline_reuse_plan returns no_reuse_plan()
    # We do NOT patch _compute_pipeline_reuse_plan here — let the real path run
    # with no_reuse=True which should produce a regenerate plan.
    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        rc = run_apply(
            url, cwd=repo, skip_confirm=True, force=True, renderer=rdr, no_reuse=True
        )

    assert rc == 0, f"run_apply returned {rc}"

    current_jd_path = apps_dir / slug / ".apply-state" / "jd-parsed.json"
    if current_jd_path.exists():
        content = json.loads(current_jd_path.read_text(encoding="utf-8"))
        assert content.get("company") != "ShouldNeverAppear", (
            "jd-parsed.json from prior slug was replayed even with --no-reuse"
        )


# ---------------------------------------------------------------------------
# 6. Gather replay skipped when decision == "regenerate"
# ---------------------------------------------------------------------------


def test_gather_replay_skipped_when_regenerate_decision(tmp_path: Path) -> None:
    """When jd_parse.decision == 'regenerate', prior artifacts are NOT replayed."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/regen-no-replay"
    slug = derive_slug(url)
    prior_slug = "prior-regen-app"

    apps_dir = repo / "private" / "applications"
    prior_jd = {"company": "NeverCopied", "must_haves": [], "nice_to_haves": []}
    _seed_prior_state(apps_dir, prior_slug, jd_parsed=prior_jd)

    call_index = [0]
    phase_seq = ["gather", "draft", "render"]

    def fake_run_phase(*args, **kwargs):
        yield Event(type="phase_complete", name=phase_seq[call_index[0]])
        call_index[0] += 1

    plan = _regenerate_plan()

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", fake_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
        patch("jobsmith._cli_apply._compute_pipeline_reuse_plan", return_value=plan),
    ):
        rc = run_apply(url, cwd=repo, skip_confirm=True, force=True, renderer=rdr)

    assert rc == 0, f"run_apply returned {rc}"

    current_jd_path = apps_dir / slug / ".apply-state" / "jd-parsed.json"
    if current_jd_path.exists():
        content = json.loads(current_jd_path.read_text(encoding="utf-8"))
        assert content.get("company") != "NeverCopied", (
            "jd-parsed.json from prior slug was replayed even with regenerate decision"
        )
