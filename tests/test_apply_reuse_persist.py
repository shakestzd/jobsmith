"""Tests for finding #3 (bug-6204cdad): the live apply pipeline must persist
the reuse tables after the gather phase.

After a gather run via ``run_apply`` (the CLI renderer path that drives
``_run_apply_phases``), these three tables must have rows:

- ``application_fingerprints``     (JD fingerprint)
- ``canonical_requirements``       (per-requirement canonical hashes)
- ``requirement_evidence_map``     (bullet-selection evidence map)

Without this the reuse layer has nothing to read on later runs.
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


def _seed_gather_artifacts(state_dir: Path, url: str) -> None:
    """Write the artifacts a real gather phase would produce."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bullet-decisions.json").write_text("{}\n")
    (state_dir / "jd-parsed.json").write_text(
        json.dumps({
            "company": "Acme",
            "position": "Backend Engineer",
            "location": "Remote",
            "location_type": "remote",
            "salary_range": None,
            "req_id": None,
            "apply_url": url,
            "role_type": "ic",
            "jd_text_clean": "We need a backend engineer with Python and AWS.",
            "must_haves": [
                {
                    "raw": "5+ years Python",
                    "canonical_tag": "tag:python",
                    "normalized_phrase": "python experience",
                },
            ],
            "nice_to_haves": [
                {
                    "raw": "AWS experience",
                    "canonical_tag": "tag:aws",
                    "normalized_phrase": "aws experience",
                },
            ],
            "top_keywords": ["python", "aws"],
        })
    )
    (state_dir / "bullet-selection.json").write_text(
        json.dumps({
            "positions": [
                {
                    "company": "PriorCo",
                    "title": "Engineer",
                    "bullets": [
                        {
                            "master_bullet_id": "abc123def456",
                            "text": "Built Python services on AWS.",
                            "included": True,
                            "matched_requirement_hash": "deadbeef",
                        },
                    ],
                },
            ],
        })
    )


def test_gather_persists_reuse_tables(tmp_path: Path):
    """After a gather run via run_apply, the three reuse tables have rows."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/backend-engineer"
    slug = derive_slug(url)

    state_dir = repo / "private" / "applications" / slug / ".apply-state"
    _seed_gather_artifacts(state_dir, url)

    fake_run_phase = _fake_run_phase_factory(
        {phase: _make_phase_events(phase) for phase, _ in _PHASES}
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
    ):
        rc = run_apply(
            url,
            cwd=repo,
            skip_confirm=True,
            force=True,
            renderer=rdr,
        )

    assert rc == 0, f"run_apply returned {rc}"

    from jobsmith.db import open_pipeline_db

    conn = open_pipeline_db(repo / "private" / "jobsmith.db")
    try:
        fp = conn.execute(
            "SELECT COUNT(*) FROM application_fingerprints"
        ).fetchone()[0]
        reqs = conn.execute(
            "SELECT COUNT(*) FROM canonical_requirements"
        ).fetchone()[0]
        evmap = conn.execute(
            "SELECT COUNT(*) FROM requirement_evidence_map"
        ).fetchone()[0]
    finally:
        conn.close()

    assert fp >= 1, f"application_fingerprints empty (got {fp})"
    assert reqs >= 2, f"canonical_requirements should have 2 rows (got {reqs})"
    assert evmap >= 1, f"requirement_evidence_map empty (got {evmap})"


def test_no_reuse_skips_persistence(tmp_path: Path):
    """--no-reuse must NOT write any reuse tables (legacy path)."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    url = "https://example.com/jobs/no-reuse-role"
    slug = derive_slug(url)

    state_dir = repo / "private" / "applications" / slug / ".apply-state"
    _seed_gather_artifacts(state_dir, url)

    fake_run_phase = _fake_run_phase_factory(
        {phase: _make_phase_events(phase) for phase, _ in _PHASES}
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
    ):
        rc = run_apply(
            url,
            cwd=repo,
            skip_confirm=True,
            force=True,
            renderer=rdr,
            no_reuse=True,
        )

    assert rc == 0, f"run_apply returned {rc}"

    from jobsmith.db import open_pipeline_db

    conn = open_pipeline_db(repo / "private" / "jobsmith.db")
    try:
        fp = conn.execute(
            "SELECT COUNT(*) FROM application_fingerprints"
        ).fetchone()[0]
        evmap = conn.execute(
            "SELECT COUNT(*) FROM requirement_evidence_map"
        ).fetchone()[0]
    finally:
        conn.close()

    assert fp == 0, f"--no-reuse wrote application_fingerprints (got {fp})"
    assert evmap == 0, f"--no-reuse wrote requirement_evidence_map (got {evmap})"


# ---------------------------------------------------------------------------
# Finding #2 (bug-6204cdad): the planner is fed real jd_text and emits a
# reuse-plan artifact for the prompt-side reuse path.  Wrapper-level
# whole-gather skip was intentionally reverted (it dropped bullet-selection /
# tailoring outputs and the post-gather slug reconciliation); reuse is applied
# per-specialist inside the phases + re-gated by the backstop instead.
# ---------------------------------------------------------------------------


def test_reuse_plan_artifact_written_before_phases(tmp_path: Path):
    """run_apply emits reuse-plan.json and still runs the gather specialist."""
    repo = _minimal_repo(tmp_path)
    plugin_dir = _mock_plugin_dir(tmp_path)
    jd = "We need a backend engineer with Python and AWS. Five years experience."
    url = "https://example.com/jobs/backend-reuse-plan"
    slug = derive_slug(url)

    state_dir = repo / "private" / "applications" / slug / ".apply-state"
    _seed_gather_artifacts(state_dir, url)

    called_phases: list[str] = []

    def recording_run_phase(*args, **kwargs):
        phase = kwargs.get("phase") or args[0]
        called_phases.append(phase)
        yield Event(type="phase_complete", name=phase)

    rdr = ApplyRenderer(
        yes=True,
        verbosity=0,
        console=Console(file=io.StringIO(), force_terminal=False),
    )

    with (
        patch("jobsmith.apply.headless.run_phase", recording_run_phase),
        patch("jobsmith.apply.get_plugin_dir", return_value=plugin_dir),
        patch("jobsmith.apply._build_paths", return_value={}),
        patch("jobsmith.apply._reconcile_canonical_slug", return_value=(slug, False)),
        patch("jobsmith.apply._run_step45_orchestration", return_value=0),
        patch("jobsmith.apply.ensure_bootstrap"),
    ):
        rc = run_apply(
            url,
            cwd=repo,
            skip_confirm=True,
            force=True,
            renderer=rdr,
            jd_text=jd,
        )

    assert rc == 0, f"run_apply returned {rc}"
    # The reuse-plan artifact is emitted for the prompt-side reuse path.
    assert (state_dir / "reuse-plan.json").exists()
    # Gather is NOT skipped at the wrapper level — the specialist still runs.
    assert "gather" in called_phases, f"gather was skipped: {called_phases}"
