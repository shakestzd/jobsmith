"""Regression tests for roborev branch-review job 982 (feat-425666f3).

Covers four of the five round-3 findings:
  1. onboard scaffolds empty stubs (examples=False) so the clobber guard does
     not abort on a brand-new repo
  2. the merge lint-gate stages files under their REAL master filenames so the
     section validators actually run
  3. LinkedIn URL ingestion is SSRF-safe (https + exact linkedin.com host)
  4. raw paste text is not persisted to run.json; sensitive dirs are gitignored

Finding 5 (409 on a concurrent onboard run) lives in test_api_onboard_routes.py
alongside the route test harness.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Finding 1 — fresh-repo bootstrap must not trip the clobber guard
# ---------------------------------------------------------------------------
class TestScaffoldNoExamples:
    def test_examples_false_writes_empty_stubs(self, tmp_path: Path) -> None:
        from jobsmith._init import scaffold_repo
        from jobsmith.onboard.pipeline import _masters_have_content

        scaffold_repo(tmp_path, examples=False)
        work = (tmp_path / "assets" / "content" / "work.yml").read_text()
        assert work.lstrip().startswith("#")          # comment-only stub
        # The clobber guard must see a freshly bootstrapped repo as "empty".
        assert _masters_have_content(tmp_path) is False


# ---------------------------------------------------------------------------
# Finding 2 — lint-gate stages under real master filenames
# ---------------------------------------------------------------------------
class TestLintGateUsesRealFilenames:
    def test_lint_fn_receives_real_master_names(self, tmp_path: Path) -> None:
        from jobsmith.onboard.merge import merge_candidates_to_masters

        (tmp_path / "assets" / "content").mkdir(parents=True)
        state = tmp_path / "state"
        state.mkdir()
        (state / "candidate-work.json").write_text(
            json.dumps({"data": {"entries": [{"title": "Eng", "company": "Acme"}]}})
        )

        seen: dict[str, str] = {}

        def _capturing_lint(paths):
            seen["work"] = paths.work_yml.name
            seen["skill"] = paths.skill_yml.name

            class _R:
                ok = True
                errors: list = []

            return _R()

        result = merge_candidates_to_masters(
            state, tmp_path, {}, clobber="force", lint_fn=_capturing_lint
        )
        assert result.ok
        # Must be the real filenames so jobsmith.lint dispatches section
        # validators by path.name (not "work.yml.tmp", which bypasses them).
        assert seen["work"] == "work.yml"
        assert seen["skill"] == "skill.yml"


# ---------------------------------------------------------------------------
# Finding 3 — SSRF guard on LinkedIn URL ingestion
# ---------------------------------------------------------------------------
class TestLinkedInUrlSsrfGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/in/jane",
            "https://linkedin.com/in/jane",
        ],
    )
    def test_accepts_real_linkedin(self, url: str) -> None:
        from jobsmith.onboard.parsers.ingest import _is_safe_linkedin_url

        assert _is_safe_linkedin_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://linkedin.com.evil.com/in/jane",   # look-alike host
            "http://www.linkedin.com/in/jane",          # not https
            "https://evil.com/linkedin.com",            # path, not host
            "http://169.254.169.254/latest/meta-data/",  # link-local SSRF
            "ftp://linkedin.com/x",
            "not a url",
        ],
    )
    def test_rejects_unsafe(self, url: str) -> None:
        from jobsmith.onboard.parsers.ingest import _is_safe_linkedin_url

        assert _is_safe_linkedin_url(url) is False

    def test_ingest_rejects_lookalike_without_fetching(self, tmp_path: Path, monkeypatch) -> None:
        from jobsmith.onboard.parsers import ingest

        called = {"fetched": False}

        def _boom(*a, **k):
            called["fetched"] = True
            return "should-not-be-used"

        monkeypatch.setattr(ingest, "_fetch_url_text", _boom)
        ingest.ingest_linkedin_url(
            "https://linkedin.com.evil.com/in/jane", tmp_path, llm_call=lambda *a, **k: {}
        )
        assert called["fetched"] is False
        status = json.loads((tmp_path / "url-fetch-status.json").read_text())
        assert status["reason"] == "not_linkedin_url"


# ---------------------------------------------------------------------------
# Finding 4 — privacy: no raw paste in run.json; sensitive dirs gitignored
# ---------------------------------------------------------------------------
class TestPrivacy:
    def test_run_json_does_not_store_raw_paste(self, tmp_path: Path) -> None:
        from jobsmith.onboard.pipeline import _init_onboard_state

        state = _init_onboard_state(tmp_path, "run-1", paste="My SECRET resume text")
        meta = json.loads((state / "run.json").read_text())
        inputs = meta["inputs"]
        assert "paste" not in inputs
        assert inputs["paste_provided"] is True
        assert "My SECRET resume text" not in json.dumps(meta)

    def test_scaffold_gitignores_onboard_dirs(self, tmp_path: Path) -> None:
        from jobsmith._init import scaffold_repo

        scaffold_repo(tmp_path, examples=False)
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".onboard-state/" in gitignore
        assert ".onboard-uploads/" in gitignore
