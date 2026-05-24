"""Regression tests for roborev branch-review job 981 (feat-3b510661).

Covers the four round-2 findings:
  1. merge converts candidate shapes into the master YAML schema
  2. pipeline emits a terminal phase_complete/"onboard" event (API path)
  3. each run gets an isolated .onboard-state/{run_id} directory

Findings 2 (frontend payload.type / close-on-terminal) and 4 (postOnboard
static-token) are frontend changes verified by the TS typecheck.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def _seed(state_dir: Path, section: str, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"candidate-{section}.json").write_text(json.dumps({"data": data}))


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "assets" / "content").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Finding 1 — candidate → master schema conversion
# ---------------------------------------------------------------------------
class TestMergeProducesMasterSchema:
    def _run(self, repo: Path, state: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        return merge_candidates_to_masters(state, repo, {}, clobber="force")

    def test_work_entry_maps_company_to_location(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        state = tmp_path / "state"
        _seed(
            state,
            "work",
            {"entries": [{
                "company": "Acme", "title": "Engineer",
                "start_date": "2020", "end_date": "2023",
                "location": "Remote", "bullets": ["shipped X"],
            }]},
        )
        result = self._run(repo, state)
        assert result.ok
        work = yaml.safe_load((repo / "assets" / "content" / "work.yml").read_text())
        assert isinstance(work, list)
        entry = work[0]
        assert entry["title"] == "Engineer"
        assert entry["location"] == "Acme"          # company → master.location
        assert entry["date"] == "2020 – 2023"
        assert entry["description"] == "Remote"      # work place → master.description
        assert entry["details"] == ["shipped X"]     # bullets → master.details
        # candidate-only keys must NOT leak into the master file
        assert "company" not in entry and "bullets" not in entry

    def test_skill_is_master_list_grouped_by_category(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        state = tmp_path / "state"
        _seed(state, "skill", {"skills": [
            {"name": "Python", "category": "technical"},
            {"name": "Go", "category": "technical"},
            {"name": "Leadership", "category": "soft"},
        ]})
        self._run(repo, state)
        skill = yaml.safe_load((repo / "assets" / "content" / "skill.yml").read_text())
        assert isinstance(skill, list)              # master shape, NOT {skills: ...}
        tech = next(s for s in skill if s["title"] == "technical")
        assert tech["details"] == ["Python", "Go"]
        assert tech["description"] == "Python, Go"

    def test_education_is_master_list(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        state = tmp_path / "state"
        _seed(state, "education", {"entries": [
            {"institution": "MIT", "degree": "BSc", "field": "CS",
             "start_date": "2014", "end_date": "2018"},
        ]})
        self._run(repo, state)
        edu = yaml.safe_load((repo / "assets" / "content" / "education.yml").read_text())
        assert isinstance(edu, list)                # master shape, NOT {entries: ...}
        assert edu[0]["title"] == "MIT"             # institution → master.title
        assert edu[0]["description"] == "BSc, CS"   # degree+field → master.description
        assert edu[0]["date"] == "2014 – 2018"

    def test_author_is_wrapped_list_with_split_name(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        state = tmp_path / "state"
        _seed(state, "author", {
            "name": "Jane Q Doe", "email": "j@e.com",
            "location": "NYC", "github": "gh.com/jane",
        })
        self._run(repo, state)
        author = yaml.safe_load((repo / "assets" / "content" / "author.yml").read_text())
        assert isinstance(author, dict) and isinstance(author["author"], list)
        a = author["author"][0]
        assert a["firstname"] == "Jane Q"
        assert a["lastname"] == "Doe"
        assert a["email"] == "j@e.com"
        assert a["address"] == "NYC"                # location → address
        assert a["homepage"] == "gh.com/jane"       # github → homepage
        assert "name" not in a                      # flat candidate key gone


# ---------------------------------------------------------------------------
# Finding 3 — per-run state directory isolation
# ---------------------------------------------------------------------------
class TestPerRunStateDir:
    def test_init_onboard_state_nests_run_id(self, tmp_path: Path) -> None:
        from jobsmith.onboard.pipeline import _init_onboard_state

        state_dir = _init_onboard_state(tmp_path, "run-abc", paste="hi")
        assert state_dir == tmp_path / ".onboard-state" / "run-abc"
        assert (state_dir / "run.json").exists()

    def test_distinct_runs_do_not_share_candidates(self, tmp_path: Path) -> None:
        from jobsmith.onboard.pipeline import _init_onboard_state

        a = _init_onboard_state(tmp_path, "run-a", paste="x")
        b = _init_onboard_state(tmp_path, "run-b", paste="y")
        assert a != b
        # Writing a candidate into run-a must not appear in run-b.
        (a / "candidate-work.json").write_text("{}")
        assert not (b / "candidate-work.json").exists()


# ---------------------------------------------------------------------------
# Finding 2 (backend) — terminal phase_complete/"onboard" event
# ---------------------------------------------------------------------------
class TestTerminalOnboardEvent:
    def test_run_pipeline_emits_terminal_onboard_event(self, tmp_path: Path, monkeypatch) -> None:
        from jobsmith.core.events import PipelineEvent
        from jobsmith.onboard import pipeline

        emitted: list[PipelineEvent] = []

        class _Sink:
            def emit(self, event):  # noqa: ANN001
                emitted.append(event)

        monkeypatch.setattr(pipeline, "run_ingestion", lambda *a, **k: 0)
        monkeypatch.setattr(pipeline, "build_gap_questions", lambda *a, **k: [])

        class _Result:
            ok = True
            lint_errors: list = []

        monkeypatch.setattr(
            pipeline, "merge_candidates_to_masters", lambda *a, **k: _Result()
        )

        pipeline.run_onboard_pipeline(repo_root=tmp_path, paste="x", events=_Sink())

        terminal = [e for e in emitted if e.kind == "phase_complete" and e.phase == "onboard"]
        assert terminal, "expected a terminal phase_complete/onboard event"
        # …and it must be the LAST event so subscribers close only at the end.
        assert emitted[-1].kind == "phase_complete"
        assert emitted[-1].phase == "onboard"
