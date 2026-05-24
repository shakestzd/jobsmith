"""Regression tests for roborev branch-review job 980 (feat-1d1a32fb).

Covers the five cross-slice findings:
  1. synthetic "onboard" slug bypasses the events directory-existence 404
  3. master reads thread the injected repo_root (read/write consistency)
  4. `onboard --force` actually propagates CLOBBER_FORCE to the merge step
  5. multi-source ingestion accumulates candidate-*.json instead of clobbering

Finding 2 (web SSE static-token) is a frontend change covered by the TS
typecheck + the delegation to the shared ``buildEventsUrl`` helper.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Finding 1 — synthetic "onboard" slug bypasses the 404 directory guard
# ---------------------------------------------------------------------------
class TestSyntheticOnboardSlug:
    def test_onboard_slug_bypasses_missing_dir_404(self, tmp_path: Path) -> None:
        from jobsmith.api.events import _validate_slug_or_404

        # No "onboard" dir exists under apps_dir — must NOT raise.
        result = _validate_slug_or_404(tmp_path, "onboard")
        assert result == tmp_path / "onboard"

    def test_real_missing_slug_still_404s(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from jobsmith.api.events import _validate_slug_or_404

        with pytest.raises(HTTPException) as exc:
            _validate_slug_or_404(tmp_path, "does-not-exist")
        assert exc.value.status_code == 404

    def test_onboard_slug_still_rejects_traversal(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from jobsmith.api.events import _validate_slug_or_404

        with pytest.raises(HTTPException) as exc:
            _validate_slug_or_404(tmp_path, "../etc")
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Finding 3 — master reads thread the injected repo_root
# ---------------------------------------------------------------------------
class TestMasterReadThreadsRepoRoot:
    def test_db_load_section_forwards_repo_root(self, monkeypatch) -> None:
        from jobsmith.api import master

        captured: dict = {}

        def _fake_db_path(repo_root: Path | None = None) -> Path | None:
            captured["repo_root"] = repo_root
            return None  # short-circuits the read; we only assert the arg

        monkeypatch.setattr(master, "_get_db_path_for_master", _fake_db_path)
        master._db_load_section("work", repo_root=Path("/injected/root"))
        assert captured["repo_root"] == Path("/injected/root")


# ---------------------------------------------------------------------------
# Finding 4 — `onboard --force` propagates CLOBBER_FORCE to the merge step
# ---------------------------------------------------------------------------
class TestForcePropagatesClobber:
    def test_dispatch_passes_clobber_to_merge(self, tmp_path: Path, monkeypatch) -> None:
        from jobsmith.onboard import pipeline

        captured: dict = {}

        monkeypatch.setattr(pipeline, "run_ingestion", lambda *a, **k: 0)
        monkeypatch.setattr(pipeline, "run_gap_interview_cli", lambda *a, **k: {})

        class _Result:
            ok = True
            lint_errors: list = []

        def _fake_merge(state_dir, repo_root, answers, *, clobber):
            captured["clobber"] = clobber
            return _Result()

        monkeypatch.setattr(pipeline, "merge_candidates_to_masters", _fake_merge)

        pipeline.dispatch_onboard_pipeline(
            repo_root=tmp_path,
            paste="x",
            clobber=pipeline.CLOBBER_FORCE,
        )
        assert captured["clobber"] == pipeline.CLOBBER_FORCE

    def test_dispatch_defaults_to_merge(self, tmp_path: Path, monkeypatch) -> None:
        from jobsmith.onboard import pipeline

        captured: dict = {}
        monkeypatch.setattr(pipeline, "run_ingestion", lambda *a, **k: 0)
        monkeypatch.setattr(pipeline, "run_gap_interview_cli", lambda *a, **k: {})

        class _Result:
            ok = True
            lint_errors: list = []

        monkeypatch.setattr(
            pipeline,
            "merge_candidates_to_masters",
            lambda *a, clobber, **k: captured.update(clobber=clobber) or _Result(),
        )
        pipeline.dispatch_onboard_pipeline(repo_root=tmp_path, paste="x")
        assert captured["clobber"] == pipeline.CLOBBER_MERGE


# ---------------------------------------------------------------------------
# Finding 5 — multi-source ingestion accumulates rather than clobbers
# ---------------------------------------------------------------------------
class TestCandidateAccumulation:
    def test_later_empty_source_does_not_discard_earlier_data(self, tmp_path: Path) -> None:
        from jobsmith.onboard.parsers.ingest import _write_candidate_files

        # Source A (resume): real work + author content.
        _write_candidate_files(
            tmp_path,
            {
                "work": {"entries": [{"company": "Acme"}]},
                "author": {"name": "Jane Doe"},
            },
            {"work.entries.0.company": "resume snippet"},
            "resume",
        )
        # Source B (linkedin): empty work, adds a skill — must not wipe A's work.
        _write_candidate_files(
            tmp_path,
            {"work": {}, "skill": {"skills": [{"name": "Python"}]}},
            {},
            "linkedin",
        )

        work = json.loads((tmp_path / "candidate-work.json").read_text())
        assert work["data"]["entries"] == [{"company": "Acme"}]
        assert set(work["sources"]) == {"resume", "linkedin"}

        author = json.loads((tmp_path / "candidate-author.json").read_text())
        assert author["data"]["name"] == "Jane Doe"

        skill = json.loads((tmp_path / "candidate-skill.json").read_text())
        assert skill["data"]["skills"] == [{"name": "Python"}]

    def test_list_sections_union_across_sources(self, tmp_path: Path) -> None:
        from jobsmith.onboard.parsers.ingest import _write_candidate_files

        _write_candidate_files(
            tmp_path, {"work": {"entries": [{"company": "Acme"}]}}, {}, "resume"
        )
        _write_candidate_files(
            tmp_path, {"work": {"entries": [{"company": "Globex"}]}}, {}, "linkedin"
        )
        work = json.loads((tmp_path / "candidate-work.json").read_text())
        companies = [e["company"] for e in work["data"]["entries"]]
        assert companies == ["Acme", "Globex"]

    def test_per_source_provenance_files_do_not_collide(self, tmp_path: Path) -> None:
        from jobsmith.onboard.parsers.ingest import _write_candidate_files

        _write_candidate_files(tmp_path, {"work": {"entries": [1]}}, {"a": "1"}, "resume")
        _write_candidate_files(tmp_path, {"work": {"entries": [2]}}, {"b": "2"}, "linkedin")
        assert (tmp_path / "provenance-resume.json").exists()
        assert (tmp_path / "provenance-linkedin.json").exists()
