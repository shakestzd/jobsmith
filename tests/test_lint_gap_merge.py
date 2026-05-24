"""Tests for feat-01cad829: lint library, gap-interview, lint-gated merge.

TDD Protocol: tests written first, implementation follows.

Coverage:
  (a) extracted lint validates all 4 sections incl. failure cases
  (b) gap-interview identifies missing required fields, returns question structure
  (c) lint-gate blocks write on invalid merge, caps at max_attempts, no broken master
  (d) --merge vs --force behavior when masters exist
  (e) summary categorization
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# (a) Extracted lint library — all four sections
# ---------------------------------------------------------------------------


class TestLintLibrary:
    """jobsmith.lint.validate_masters_from_paths validates all four sections."""

    def _make_paths(self, tmp_path: Path):
        from jobsmith.lint import MasterPathSet

        return MasterPathSet(
            work_yml=tmp_path / "work.yml",
            skill_yml=tmp_path / "skill.yml",
            education_yml=tmp_path / "education.yml",
            author_yml=tmp_path / "author.yml",
        )

    def test_passes_when_all_files_absent(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert result.ok
        assert result.errors == []
        assert result.exit_code == 0

    def test_passes_with_valid_work_yaml(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "work.yml").write_text(
            "- title: Engineer\n  company: Acme\n  details:\n    - Built things\n"
        )
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert result.ok, result.errors

    def test_fails_work_yaml_not_a_list(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "work.yml").write_text("title: Engineer\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert not result.ok
        assert any("root must be a list" in e for e in result.errors)

    def test_fails_work_yaml_details_not_list(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "work.yml").write_text(
            "- title: Engineer\n  details: bad_string\n"
        )
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert not result.ok
        assert any("details must be a list" in e for e in result.errors)

    def test_passes_with_valid_skill_dict(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "skill.yml").write_text("skills:\n  - name: Python\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert result.ok, result.errors

    def test_fails_skill_yaml_skills_not_list(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "skill.yml").write_text("skills: bad_string\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert not result.ok
        assert any("skills" in e for e in result.errors)

    def test_passes_with_valid_education_dict(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "education.yml").write_text(
            "entries:\n  - institution: MIT\n    degree: BS\n"
        )
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert result.ok, result.errors

    def test_fails_education_yaml_entries_not_list(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "education.yml").write_text("entries: bad_string\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert not result.ok

    def test_passes_with_valid_author_dict(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "author.yml").write_text("name: Jane\nemail: jane@example.com\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert result.ok, result.errors

    def test_fails_author_yaml_not_mapping(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "author.yml").write_text("- name: Jane\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert not result.ok
        assert any("root must be a mapping" in e for e in result.errors)

    def test_fails_yaml_parse_error(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        (tmp_path / "work.yml").write_text("{{not valid yaml: [\n")
        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert not result.ok
        assert any("YAML parse error" in e for e in result.errors)

    def test_bool_result(self, tmp_path: Path):
        from jobsmith.lint import validate_masters_from_paths

        paths = self._make_paths(tmp_path)
        result = validate_masters_from_paths(paths)
        assert bool(result) is True

    def test_validate_masters_uses_repo_root(self, tmp_path: Path):
        """validate_masters() resolves paths from config or defaults."""
        from jobsmith.lint import validate_masters

        # No config file — falls back to default paths (all absent = OK)
        result = validate_masters(tmp_path)
        assert result.ok


# ---------------------------------------------------------------------------
# (b) Gap-interview question structure
# ---------------------------------------------------------------------------


class TestGapInterview:
    """build_gap_questions returns structured GapQuestion objects."""

    def test_all_sections_missing_returns_questions(self, tmp_path: Path):
        from jobsmith.onboard.gap import GapQuestion, build_gap_questions

        questions = build_gap_questions(tmp_path)
        assert len(questions) > 0
        sections = {q.section for q in questions}
        assert "work" in sections
        assert "skill" in sections
        assert "author" in sections

    def test_question_has_required_fields(self, tmp_path: Path):
        from jobsmith.onboard.gap import build_gap_questions

        questions = build_gap_questions(tmp_path)
        q = questions[0]
        assert hasattr(q, "section")
        assert hasattr(q, "field")
        assert hasattr(q, "prompt")
        assert hasattr(q, "required")
        assert hasattr(q, "hint")

    def test_to_dict_serializable(self, tmp_path: Path):
        from jobsmith.onboard.gap import build_gap_questions

        questions = build_gap_questions(tmp_path)
        d = questions[0].to_dict()
        assert "section" in d
        assert "field" in d
        assert "prompt" in d
        assert "required" in d
        assert "hint" in d

    def test_no_questions_when_all_data_present(self, tmp_path: Path):
        from jobsmith.onboard.gap import build_gap_questions

        # Write complete candidate files
        (tmp_path / "candidate-work.json").write_text(
            json.dumps({"data": {"entries": [{"title": "Eng", "company": "Acme"}]}})
        )
        (tmp_path / "candidate-skill.json").write_text(
            json.dumps({"data": {"skills": [{"name": "Python"}]}})
        )
        (tmp_path / "candidate-education.json").write_text(
            json.dumps({"data": {"entries": [{"institution": "MIT"}]}})
        )
        (tmp_path / "candidate-author.json").write_text(
            json.dumps({"data": {"name": "Jane", "email": "j@e.com"}})
        )

        questions = build_gap_questions(tmp_path)
        # Required questions (work, skill, author.name, author.email) should be absent
        required_qs = [q for q in questions if q.required]
        assert required_qs == []

    def test_partial_author_generates_questions(self, tmp_path: Path):
        """If author has name but no email, only email question is generated."""
        from jobsmith.onboard.gap import build_gap_questions

        (tmp_path / "candidate-author.json").write_text(
            json.dumps({"data": {"name": "Jane"}})
        )
        questions = build_gap_questions(tmp_path)
        author_qs = [q for q in questions if q.section == "author"]
        fields = {q.field for q in author_qs}
        assert "email" in fields
        assert "name" not in fields

    def test_run_gap_interview_cli_mocked_input(self, tmp_path: Path):
        from jobsmith.onboard.gap import run_gap_interview_cli

        responses = iter(["Acme Corp Engineer", "Python Go", "", "Jane Smith", "jane@ex.com", "", "", "", ""])
        answers = run_gap_interview_cli(tmp_path, input_fn=lambda _: next(responses, ""))
        assert isinstance(answers, dict)
        # All keys follow section.field format
        for key in answers:
            assert "." in key

    def test_run_gap_interview_cli_returns_dict(self, tmp_path: Path):
        from jobsmith.onboard.gap import run_gap_interview_cli

        answers = run_gap_interview_cli(tmp_path, input_fn=lambda _: "test answer")
        assert isinstance(answers, dict)


# ---------------------------------------------------------------------------
# (c) Lint-gate: blocks write on invalid merge, caps at max_attempts
# ---------------------------------------------------------------------------


class TestLintGate:
    """merge_candidates_to_masters blocks persist on lint failure."""

    def _make_state(self, tmp_path: Path, section: str, data: dict) -> None:
        (tmp_path / f"candidate-{section}.json").write_text(json.dumps({"data": data}))

    def test_ok_on_valid_candidates(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (repo / "assets" / "content").mkdir(parents=True)

        self._make_state(state, "work", {"entries": [{"title": "Eng", "details": []}]})
        self._make_state(state, "skill", {"skills": [{"name": "Python"}]})
        self._make_state(state, "education", {"entries": [{"institution": "MIT"}]})
        self._make_state(state, "author", {"name": "Jane", "email": "j@e.com"})

        result = merge_candidates_to_masters(state, repo, {}, clobber="force")
        assert result.ok
        assert result.lint_errors == []

    def test_lint_failure_prevents_write(self, tmp_path: Path):
        from jobsmith.lint import LintResult
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (repo / "assets" / "content").mkdir(parents=True)

        # Inject always-failing lint (receives MasterPathSet)
        def bad_lint(_paths):
            return LintResult(ok=False, errors=["work.yml:1: synthetic error"])

        result = merge_candidates_to_masters(
            state, repo, {}, clobber="force", lint_fn=bad_lint
        )
        assert not result.ok
        assert "synthetic error" in result.lint_errors[0]

        # Masters must NOT have been persisted
        work = repo / "assets" / "content" / "work.yml"
        assert not work.exists()

    def test_lint_gate_caps_at_max_attempts(self, tmp_path: Path):
        from jobsmith.lint import LintResult
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (repo / "assets" / "content").mkdir(parents=True)

        call_count = 0

        def counting_lint(_paths):
            nonlocal call_count
            call_count += 1
            return LintResult(ok=False, errors=["error"])

        result = merge_candidates_to_masters(
            state, repo, {}, clobber="force", lint_fn=counting_lint, max_attempts=3
        )
        assert not result.ok
        # Should stop after 1 attempt per the current design (no auto-fix)
        assert call_count >= 1
        assert call_count <= 3  # never exceeds cap

    def test_no_broken_master_persisted_on_failure(self, tmp_path: Path):
        """Even after max_attempts, broken masters are never written."""
        from jobsmith.lint import LintResult
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        content_dir = repo / "assets" / "content"
        content_dir.mkdir(parents=True)

        original = "- title: Original\n  details: []\n"
        (content_dir / "work.yml").write_text(original)

        def bad_lint(_paths):
            return LintResult(ok=False, errors=["synthetic error"])

        merge_candidates_to_masters(
            state, repo, {}, clobber="force", lint_fn=bad_lint, max_attempts=3
        )

        # Original must be intact (not overwritten with broken content)
        assert (content_dir / "work.yml").read_text() == original


# ---------------------------------------------------------------------------
# (d) --merge vs --force behavior
# ---------------------------------------------------------------------------


class TestClobberSemantics:
    """merge mode deduplicates; force mode overwrites."""

    def _make_state(self, state_dir: Path, entries: list) -> None:
        (state_dir / "candidate-work.json").write_text(
            json.dumps({"data": {"entries": entries}})
        )

    def test_merge_keeps_existing_entries(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        content_dir = repo / "assets" / "content"
        content_dir.mkdir(parents=True)

        # Existing master has one entry
        (content_dir / "work.yml").write_text(
            "- title: Old Engineer\n  company: OldCo\n  details: []\n"
        )

        # Candidate has a different entry
        self._make_state(state, [{"title": "New Engineer", "company": "NewCo", "details": []}])

        result = merge_candidates_to_masters(state, repo, {}, clobber="merge")
        assert result.ok

        merged = yaml.safe_load((content_dir / "work.yml").read_text())
        titles = [e["title"] for e in merged]
        assert "Old Engineer" in titles
        assert "New Engineer" in titles

    def test_force_overwrites_existing(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        content_dir = repo / "assets" / "content"
        content_dir.mkdir(parents=True)

        (content_dir / "work.yml").write_text(
            "- title: Old Engineer\n  company: OldCo\n  details: []\n"
        )
        self._make_state(state, [{"title": "New Engineer", "company": "NewCo", "details": []}])

        result = merge_candidates_to_masters(state, repo, {}, clobber="force")
        assert result.ok

        merged = yaml.safe_load((content_dir / "work.yml").read_text())
        titles = [e["title"] for e in (merged if isinstance(merged, list) else [])]
        assert "Old Engineer" not in titles
        assert "New Engineer" in titles

    def test_merge_deduplicates_by_title_and_company(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        content_dir = repo / "assets" / "content"
        content_dir.mkdir(parents=True)

        (content_dir / "work.yml").write_text(
            "- title: Engineer\n  company: Acme\n  details: []\n"
        )
        # Same title+company — should not be duplicated
        self._make_state(state, [{"title": "Engineer", "company": "Acme", "details": []}])

        result = merge_candidates_to_masters(state, repo, {}, clobber="merge")
        assert result.ok

        merged = yaml.safe_load((content_dir / "work.yml").read_text())
        assert isinstance(merged, list)
        assert len(merged) == 1  # deduped


# ---------------------------------------------------------------------------
# (e) Summary categorization
# ---------------------------------------------------------------------------


class TestOnboardSummary:
    """MergeResult.summary categorizes imported vs user-supplied vs still-optional."""

    def _make_state(self, state_dir: Path) -> None:
        (state_dir / "candidate-work.json").write_text(
            json.dumps({"data": {"entries": [{"title": "Eng", "company": "Acme"}]}})
        )
        (state_dir / "candidate-skill.json").write_text(
            json.dumps({"data": {"skills": [{"name": "Python"}]}})
        )
        (state_dir / "candidate-author.json").write_text(
            json.dumps({"data": {"name": "Jane", "email": "j@e.com"}})
        )

    def test_imported_fields_present(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (repo / "assets" / "content").mkdir(parents=True)
        self._make_state(state)

        result = merge_candidates_to_masters(state, repo, {}, clobber="force")
        assert "work.entries" in result.summary.imported
        assert "skill.skills" in result.summary.imported
        assert "author.name" in result.summary.imported

    def test_user_supplied_when_answered_in_gap(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (repo / "assets" / "content").mkdir(parents=True)

        # No candidate files — user fills in
        answers = {
            "work.entries": "Software Engineer at StartupCo",
            "skill.skills": "Python, Go",
            "author.name": "Alice",
            "author.email": "alice@example.com",
        }
        result = merge_candidates_to_masters(state, repo, answers, clobber="force")
        assert "work.entries" in result.summary.user_supplied
        assert "skill.skills" in result.summary.user_supplied

    def test_still_optional_when_not_filled(self, tmp_path: Path):
        from jobsmith.onboard.merge import merge_candidates_to_masters

        repo = tmp_path / "repo"
        repo.mkdir()
        state = tmp_path / "state"
        state.mkdir()
        (repo / "assets" / "content").mkdir(parents=True)
        self._make_state(state)

        result = merge_candidates_to_masters(state, repo, {}, clobber="force")
        # Optional fields not in candidate or answers
        assert "author.phone" in result.summary.still_optional or \
               "author.linkedin" in result.summary.still_optional


# ---------------------------------------------------------------------------
# (f) Pipeline wiring: gap + merge called after ingest
# ---------------------------------------------------------------------------


class TestPipelineWiring:
    """dispatch_onboard_pipeline and run_onboard_pipeline call gap + merge."""

    def _make_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".apply-config.yaml").write_text(
            "master:\n"
            "  work_yml: assets/content/work.yml\n"
            "  skill_yml: assets/content/skill.yml\n"
            "  education_yml: assets/content/education.yml\n"
            "  author_yml: assets/content/author.yml\n"
            "output:\n"
            "  applications_dir: private/applications\n"
        )
        (repo / "assets" / "content").mkdir(parents=True)
        return repo

    def test_dispatch_calls_gap_and_merge(self, tmp_path: Path):
        from jobsmith.onboard.pipeline import dispatch_onboard_pipeline
        from jobsmith.onboard.merge import MergeResult, OnboardSummary

        repo = self._make_repo(tmp_path)

        with (
            patch("jobsmith.onboard.pipeline.run_ingestion", return_value=0),
            patch("jobsmith.onboard.pipeline.run_gap_interview_cli", return_value={}) as mock_gap,
            patch(
                "jobsmith.onboard.pipeline.merge_candidates_to_masters",
                return_value=MergeResult(ok=True, summary=OnboardSummary()),
            ) as mock_merge,
        ):
            rc = dispatch_onboard_pipeline(repo_root=repo, input_fn=lambda _: "")

        assert rc == 0
        mock_gap.assert_called_once()
        mock_merge.assert_called_once()

    def test_run_pipeline_emits_gap_questions_event(self, tmp_path: Path):
        from jobsmith.onboard.pipeline import run_onboard_pipeline
        from jobsmith.onboard.merge import MergeResult, OnboardSummary

        repo = self._make_repo(tmp_path)
        events = MagicMock()

        with (
            patch("jobsmith.onboard.pipeline.run_ingestion", return_value=0),
            patch("jobsmith.onboard.pipeline.build_gap_questions", return_value=[]),
            patch(
                "jobsmith.onboard.pipeline.merge_candidates_to_masters",
                return_value=MergeResult(ok=True, summary=OnboardSummary()),
            ),
        ):
            run_onboard_pipeline(repo_root=repo, events=events)

        # Check via PipelineEvent.kind attribute
        emitted_kinds = set()
        for c in events.emit.call_args_list:
            arg = c.args[0] if c.args else None
            if arg is not None:
                emitted_kinds.add(getattr(arg, "kind", None))
        assert "gap_questions" in emitted_kinds

    def test_pipeline_returns_1_on_lint_failure(self, tmp_path: Path):
        from jobsmith.onboard.pipeline import dispatch_onboard_pipeline
        from jobsmith.onboard.merge import MergeResult, OnboardSummary

        repo = self._make_repo(tmp_path)

        with (
            patch("jobsmith.onboard.pipeline.run_ingestion", return_value=0),
            patch("jobsmith.onboard.pipeline.run_gap_interview_cli", return_value={}),
            patch(
                "jobsmith.onboard.pipeline.merge_candidates_to_masters",
                return_value=MergeResult(
                    ok=False,
                    lint_errors=["work.yml: broken"],
                    summary=OnboardSummary(),
                ),
            ),
        ):
            rc = dispatch_onboard_pipeline(repo_root=repo, input_fn=lambda _: "")

        assert rc == 1
