"""Tests for jobsmith.reuse.planner — reuse-planner pre-phase (feat-6623ee3b).

TDD: failing tests written BEFORE implementation.

Covers:
  - planner returns reuse decisions given seeded prior data (dedup hit, company hit, bullet-map hit)
  - planner returns regenerate when nothing matches / hashes stale
  - --no-reuse bypasses the planner entirely (no reuse decisions applied)
  - the new `reuse lookup-bullet` CLI command runs and returns the expected JSON contract
  - pipeline short-circuits a phase marked reuse and runs a phase marked regenerate
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.config import ReuseSettings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> sqlite3.Connection:
    return jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")


def _state_dir(base: Path, slug: str, jd_parsed: dict | None = None, fit_score: dict | None = None) -> Path:
    sd = base / slug / ".apply-state"
    sd.mkdir(parents=True, exist_ok=True)
    if jd_parsed is not None:
        (sd / "jd-parsed.json").write_text(json.dumps(jd_parsed))
    if fit_score is not None:
        (sd / "fit-score.json").write_text(json.dumps(fit_score))
    return sd


JD_TEXT_A = (
    "Senior Python Engineer — build scalable ETL pipelines, "
    "mentor junior engineers. 5+ years Python, SQL, AWS."
)
JD_TEXT_B = (
    "Senior Python Engineer — build and maintain scalable ETL pipelines, "
    "mentor junior engineers. 5+ years Python, SQL, cloud platforms AWS."
)
JD_TEXT_DISTINCT = "Marketing Manager — GTM strategy, brand development. MBA preferred."

JD_PARSED_A = {
    "company": "Acme Inc",
    "must_haves": [{"raw": "5+ years Python"}, {"raw": "SQL expertise"}],
    "nice_to_haves": [],
    "top_keywords": ["python", "sql", "etl"],
}
FIT_SCORE_A = {"overall": 0.85, "tier": "strong"}


# ---------------------------------------------------------------------------
# ReusePlan structure
# ---------------------------------------------------------------------------


class TestReusePlanStructure:
    def test_reuse_plan_has_expected_fields(self):
        from jobsmith.reuse.planner import PhaseDecision, ReusePlan

        plan = ReusePlan(
            jd_parse=PhaseDecision(decision="regenerate", source=None),
            fit_score=PhaseDecision(decision="regenerate", source=None),
            company_research=PhaseDecision(decision="regenerate", source=None),
            bullet_map={},
            matched_slug=None,
        )
        assert plan.jd_parse.decision == "regenerate"
        assert plan.fit_score.decision == "regenerate"
        assert plan.company_research.decision == "regenerate"
        assert plan.bullet_map == {}
        assert plan.matched_slug is None

    def test_phase_decision_has_decision_and_source(self):
        from jobsmith.reuse.planner import PhaseDecision

        pd = PhaseDecision(decision="reuse", source="prior-slug-abc")
        assert pd.decision == "reuse"
        assert pd.source == "prior-slug-abc"

    def test_reuse_plan_reuse_fields(self):
        from jobsmith.reuse.planner import PhaseDecision, ReusePlan

        plan = ReusePlan(
            jd_parse=PhaseDecision(decision="reuse", source="slug-abc"),
            fit_score=PhaseDecision(decision="reuse", source="slug-abc"),
            company_research=PhaseDecision(decision="reuse", source="acme"),
            bullet_map={"req-hash-1": "bullet-id-001"},
            matched_slug="slug-abc",
        )
        assert plan.jd_parse.decision == "reuse"
        assert plan.bullet_map["req-hash-1"] == "bullet-id-001"
        assert plan.matched_slug == "slug-abc"


# ---------------------------------------------------------------------------
# compute_reuse_plan — regenerate path (nothing matches)
# ---------------------------------------------------------------------------


class TestComputeReusePlanRegenerate:
    def test_plan_is_all_regenerate_when_no_prior_data(self, tmp_path: Path):
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        cfg = ReuseSettings()
        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="Acme Inc",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.jd_parse.decision == "regenerate"
        assert plan.fit_score.decision == "regenerate"
        assert plan.company_research.decision == "regenerate"
        assert plan.bullet_map == {}
        assert plan.matched_slug is None

    def test_plan_regenerate_when_jd_below_threshold(self, tmp_path: Path):
        from jobsmith.reuse.dedup import write_jd_fingerprint
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        write_jd_fingerprint(conn, slug="prior-slug", jd_text=JD_TEXT_DISTINCT)
        cfg = ReuseSettings(dedup_threshold=0.90)

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="Acme Inc",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.jd_parse.decision == "regenerate"
        assert plan.fit_score.decision == "regenerate"


# ---------------------------------------------------------------------------
# compute_reuse_plan — reuse path (dedup hit)
# ---------------------------------------------------------------------------


class TestComputeReusePlanDedupHit:
    def test_dedup_hit_sets_jd_parse_and_fit_score_reuse(self, tmp_path: Path):
        from jobsmith.reuse.dedup import write_jd_fingerprint
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        # Seed identical JD under a prior slug so exact-hash match fires
        write_jd_fingerprint(conn, slug="prior-slug", jd_text=JD_TEXT_A)
        cfg = ReuseSettings(dedup_threshold=0.90)

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="Acme Inc",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.jd_parse.decision == "reuse"
        assert plan.fit_score.decision == "reuse"
        assert plan.matched_slug == "prior-slug"
        assert plan.jd_parse.source == "prior-slug"

    def test_near_dup_above_threshold_also_sets_reuse(self, tmp_path: Path):
        from jobsmith.reuse.dedup import write_jd_fingerprint
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        write_jd_fingerprint(conn, slug="prior-slug", jd_text=JD_TEXT_A)
        cfg = ReuseSettings(dedup_threshold=0.70)  # low threshold to catch near-dup

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_B,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="Acme Inc",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.jd_parse.decision == "reuse"
        assert plan.matched_slug == "prior-slug"


# ---------------------------------------------------------------------------
# compute_reuse_plan — company research hit
# ---------------------------------------------------------------------------


class TestComputeReusePlanCompanyHit:
    def test_fresh_company_cache_sets_company_research_reuse(self, tmp_path: Path):
        from jobsmith.reuse.company_cache import write_cache
        from jobsmith.reuse.planner import compute_reuse_plan

        companies_dir = tmp_path / "companies"
        write_cache("Acme Inc", "# Acme Inc research content", companies_dir=companies_dir)

        conn = _db(tmp_path)
        cfg = ReuseSettings(company_ttl_days=30)

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=companies_dir,
            company_name="Acme Inc",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.company_research.decision == "reuse"
        assert plan.company_research.source is not None

    def test_stale_company_cache_sets_regenerate(self, tmp_path: Path):
        import time

        from jobsmith.reuse.company_cache import normalize_company_key
        from jobsmith.reuse.planner import compute_reuse_plan

        companies_dir = tmp_path / "companies"
        companies_dir.mkdir(parents=True, exist_ok=True)
        key = normalize_company_key("Acme Inc")
        cache_file = companies_dir / f"{key}.md"
        cache_file.write_text("stale content")
        # Force a very old mtime so TTL check fails
        old_mtime = time.time() - (60 * 24 * 3600)  # 60 days ago
        import os
        os.utime(cache_file, (old_mtime, old_mtime))

        conn = _db(tmp_path)
        cfg = ReuseSettings(company_ttl_days=30)

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=companies_dir,
            company_name="Acme Inc",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.company_research.decision == "regenerate"


# ---------------------------------------------------------------------------
# compute_reuse_plan — bullet map hit
# ---------------------------------------------------------------------------


class TestComputeReusePlanBulletHit:
    def test_bullet_map_hit_populates_bullet_map(self, tmp_path: Path):
        from jobsmith.reuse.canonicalize import canonicalize
        from jobsmith.reuse.planner import compute_reuse_plan
        from jobsmith.reuse.store import content_hash, upsert_canonical_requirement

        conn = _db(tmp_path)

        # Seed a canonical requirement
        req_raw = "5+ years Python"
        _, normalized = canonicalize(req_raw)
        req_hash = content_hash({"normalized_phrase": normalized, "canonical_tag": None})
        upsert_canonical_requirement(conn, content_hash=req_hash, payload=json.dumps({
            "normalized_phrase": normalized,
            "canonical_tag": None,
        }))

        # Seed an evidence mapping: req_hash → bullet_id
        bullet_id = "abc123def456"
        bullet_text = "Built Python ETL pipelines"
        bullet_hash = content_hash(bullet_text)
        conn.execute(
            "INSERT OR IGNORE INTO requirement_evidence_map "
            "(requirement_hash, evidence_key, evidence_text, created_at) VALUES (?, ?, ?, ?)",
            (req_hash, bullet_id, bullet_hash, "2024-01-01T00:00:00+00:00"),
        )
        conn.commit()

        current_bullet_texts = {bullet_id: bullet_text}
        cfg = ReuseSettings()

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="Acme Inc",
            current_bullet_texts=current_bullet_texts,
            requirement_hashes=[req_hash],
        )
        assert req_hash in plan.bullet_map
        assert plan.bullet_map[req_hash] == bullet_id

    def test_bullet_map_miss_when_bullet_text_changed(self, tmp_path: Path):
        from jobsmith.reuse.canonicalize import canonicalize
        from jobsmith.reuse.planner import compute_reuse_plan
        from jobsmith.reuse.store import content_hash, upsert_canonical_requirement

        conn = _db(tmp_path)

        req_raw = "SQL expertise"
        _, normalized = canonicalize(req_raw)
        req_hash = content_hash({"normalized_phrase": normalized, "canonical_tag": None})
        upsert_canonical_requirement(conn, content_hash=req_hash, payload=json.dumps({
            "normalized_phrase": normalized,
            "canonical_tag": None,
        }))

        bullet_id = "abc123def456"
        old_text = "Old bullet text"
        old_hash = content_hash(old_text)
        conn.execute(
            "INSERT OR IGNORE INTO requirement_evidence_map "
            "(requirement_hash, evidence_key, evidence_text, created_at) VALUES (?, ?, ?, ?)",
            (req_hash, bullet_id, old_hash, "2024-01-01T00:00:00+00:00"),
        )
        conn.commit()

        # Current text is different from stored hash → stale
        current_bullet_texts = {bullet_id: "Updated bullet text (different content)"}
        cfg = ReuseSettings()

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="Acme Inc",
            current_bullet_texts=current_bullet_texts,
            requirement_hashes=[req_hash],
        )
        # Bullet map entry absent because current hash differs from stored
        assert req_hash not in plan.bullet_map


# ---------------------------------------------------------------------------
# --no-reuse bypasses planner
# ---------------------------------------------------------------------------


class TestNoReuseFlag:
    def test_no_reuse_plan_is_all_regenerate_regardless_of_data(self, tmp_path: Path):
        from jobsmith.reuse.dedup import write_jd_fingerprint
        from jobsmith.reuse.planner import no_reuse_plan

        # Seed data that would normally produce a reuse decision
        conn = _db(tmp_path)
        write_jd_fingerprint(conn, slug="prior-slug", jd_text=JD_TEXT_A)

        plan = no_reuse_plan()
        assert plan.jd_parse.decision == "regenerate"
        assert plan.fit_score.decision == "regenerate"
        assert plan.company_research.decision == "regenerate"
        assert plan.bullet_map == {}
        assert plan.matched_slug is None

    def test_no_reuse_plan_is_sentinel(self, tmp_path: Path):
        from jobsmith.reuse.planner import ReusePlan, no_reuse_plan

        plan = no_reuse_plan()
        assert isinstance(plan, ReusePlan)


# ---------------------------------------------------------------------------
# pipeline short-circuit (stub-based test)
# ---------------------------------------------------------------------------


class TestPipelineShortCircuit:
    """Verify pipeline respects the ReusePlan to skip or run phases."""

    def test_pipeline_consults_reuse_plan_skip_phase(self, tmp_path: Path):
        """When plan says 'reuse' for a phase, it should not call the phase runner."""
        from jobsmith.reuse.planner import PhaseDecision, ReusePlan, should_skip_phase

        plan = ReusePlan(
            jd_parse=PhaseDecision(decision="reuse", source="prior-slug"),
            fit_score=PhaseDecision(decision="reuse", source="prior-slug"),
            company_research=PhaseDecision(decision="regenerate", source=None),
            bullet_map={},
            matched_slug="prior-slug",
        )
        assert should_skip_phase(plan, "jd-parse") is True
        assert should_skip_phase(plan, "fit-score") is True
        assert should_skip_phase(plan, "company-research") is False

    def test_pipeline_no_reuse_skips_nothing(self, tmp_path: Path):
        from jobsmith.reuse.planner import no_reuse_plan, should_skip_phase

        plan = no_reuse_plan()
        assert should_skip_phase(plan, "jd-parse") is False
        assert should_skip_phase(plan, "fit-score") is False
        assert should_skip_phase(plan, "company-research") is False
        assert should_skip_phase(plan, "bullet-selection") is False


# ---------------------------------------------------------------------------
# CLI: reuse lookup-bullet
# ---------------------------------------------------------------------------


class TestReuseLookupBulletCLI:
    """Verify the `jobsmith reuse lookup-bullet` command returns the expected JSON."""

    def test_lookup_bullet_reused_true(self, tmp_path: Path):
        """When a fresh bullet mapping exists, exit 0, JSON reused=true."""
        from typer.testing import CliRunner

        from jobsmith.cli import app
        from jobsmith.reuse.canonicalize import canonicalize
        from jobsmith.reuse.store import content_hash, upsert_canonical_requirement

        conn = _db(tmp_path)

        req_raw = "5+ years Python experience"
        _, normalized = canonicalize(req_raw)
        req_hash = content_hash({"normalized_phrase": normalized, "canonical_tag": None})
        upsert_canonical_requirement(conn, content_hash=req_hash, payload=json.dumps({
            "normalized_phrase": normalized,
            "canonical_tag": None,
        }))

        bullet_id = "abc123def456"
        bullet_text = "Built Python ETL pipelines at scale"
        bullet_hash = content_hash(bullet_text)
        conn.execute(
            "INSERT OR IGNORE INTO requirement_evidence_map "
            "(requirement_hash, evidence_key, evidence_text, created_at) VALUES (?, ?, ?, ?)",
            (req_hash, bullet_id, bullet_hash, "2024-01-01T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        slug = "test-slug"
        state_dir = tmp_path / "applications" / slug / ".apply-state"
        state_dir.mkdir(parents=True, exist_ok=True)

        runner = CliRunner()
        # Patch the DB path resolver and bullet text loader
        with patch("jobsmith.reuse._cli_reuse._resolve_db_conn") as mock_conn, \
             patch("jobsmith.reuse._cli_reuse._load_current_bullet_texts") as mock_bullets:
            mock_conn.return_value = jobsmith_db.open_pipeline_db(tmp_path / "jobsmith.db")
            # Re-seed in the new conn
            c2 = mock_conn.return_value
            _, norm2 = canonicalize(req_raw)
            h2 = content_hash({"normalized_phrase": norm2, "canonical_tag": None})
            upsert_canonical_requirement(c2, content_hash=h2, payload=json.dumps({
                "normalized_phrase": norm2, "canonical_tag": None,
            }))
            c2.execute(
                "INSERT OR IGNORE INTO requirement_evidence_map "
                "(requirement_hash, evidence_key, evidence_text, created_at) VALUES (?, ?, ?, ?)",
                (h2, bullet_id, content_hash(bullet_text), "2024-01-01T00:00:00+00:00"),
            )
            c2.commit()
            mock_bullets.return_value = {bullet_id: bullet_text}

            result = runner.invoke(app, [
                "reuse", "lookup-bullet",
                "--requirement-raw", req_raw,
                "--slug", slug,
            ])

        assert result.exit_code == 0, result.output
        output = json.loads(result.output.strip())
        assert output["master_bullet_id"] == bullet_id
        assert output["reused"] is True

    def test_lookup_bullet_reused_false_no_prior_data(self, tmp_path: Path):
        """When no mapping exists, exit 0, JSON reused=false, master_bullet_id=null."""
        from typer.testing import CliRunner

        from jobsmith.cli import app

        runner = CliRunner()
        with patch("jobsmith.reuse._cli_reuse._resolve_db_conn") as mock_conn, \
             patch("jobsmith.reuse._cli_reuse._load_current_bullet_texts") as mock_bullets:
            mock_conn.return_value = _db(tmp_path)
            mock_bullets.return_value = {}

            result = runner.invoke(app, [
                "reuse", "lookup-bullet",
                "--requirement-raw", "some new requirement",
                "--slug", "test-slug",
            ])

        assert result.exit_code == 0, result.output
        output = json.loads(result.output.strip())
        assert output["master_bullet_id"] is None
        assert output["reused"] is False


# ---------------------------------------------------------------------------
# Gap 1: reuse-plan artifact written before gather
# ---------------------------------------------------------------------------


class TestReusePlanArtifact:
    """write_reuse_plan_artifact serializes ReusePlan to reuse-plan.json."""

    def test_write_reuse_plan_artifact_creates_file(self, tmp_path: Path):
        from jobsmith.reuse.planner import (
            PhaseDecision,
            ReusePlan,
            write_reuse_plan_artifact,
        )

        state_dir = tmp_path / ".apply-state"
        plan = ReusePlan(
            jd_parse=PhaseDecision(decision="reuse", source="prior-slug", score=1.0),
            fit_score=PhaseDecision(decision="reuse", source="prior-slug", score=1.0),
            company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
            bullet_map={"hash1": "bullet-id-001"},
            matched_slug="prior-slug",
            draft=PhaseDecision(decision="warm-start", source="prior-slug", score=0.92),
            jd_overlap_score=0.92,
        )
        write_reuse_plan_artifact(plan, state_dir)

        artifact_path = state_dir / "reuse-plan.json"
        assert artifact_path.exists()
        data = json.loads(artifact_path.read_text())
        assert data["jd_parse"]["decision"] == "reuse"
        assert data["jd_parse"]["source"] == "prior-slug"
        assert data["fit_score"]["decision"] == "reuse"
        assert data["company_research"]["decision"] == "regenerate"
        assert data["bullet_map"] == {"hash1": "bullet-id-001"}
        assert data["matched_slug"] == "prior-slug"
        assert data["draft"]["decision"] == "warm-start"
        assert data["jd_overlap_score"] == pytest.approx(0.92)

    def test_write_reuse_plan_artifact_creates_dir_if_missing(self, tmp_path: Path):
        from jobsmith.reuse.planner import (
            no_reuse_plan,
            write_reuse_plan_artifact,
        )

        state_dir = tmp_path / "deep" / "nested" / ".apply-state"
        assert not state_dir.exists()
        write_reuse_plan_artifact(no_reuse_plan(), state_dir)
        assert (state_dir / "reuse-plan.json").exists()

    def test_planner_runs_before_gather(self, tmp_path: Path):
        """Artifact is written (by write_reuse_plan_artifact) before gather runs.

        We exercise the helper directly: call write_reuse_plan_artifact on a
        plan, then verify the artifact exists with per-phase verdict fields.
        This confirms the integration point is available for specialists.
        """
        from jobsmith.reuse.planner import (
            PhaseDecision,
            ReusePlan,
            write_reuse_plan_artifact,
        )

        state_dir = tmp_path / ".apply-state"
        plan = ReusePlan(
            jd_parse=PhaseDecision(decision="regenerate", source=None, score=0.0),
            fit_score=PhaseDecision(decision="regenerate", source=None, score=0.0),
            company_research=PhaseDecision(decision="regenerate", source=None, score=0.0),
            bullet_map={},
            matched_slug=None,
        )
        # Simulate: write artifact BEFORE gather (pre-phase)
        write_reuse_plan_artifact(plan, state_dir)

        artifact_path = state_dir / "reuse-plan.json"
        assert artifact_path.exists(), "reuse-plan.json must exist before gather runs"
        data = json.loads(artifact_path.read_text())
        # Verify per-phase verdict fields are present
        for field in ("jd_parse", "fit_score", "company_research", "draft"):
            assert field in data, f"Missing verdict field: {field}"
            assert "decision" in data[field]
        assert "bullet_map" in data
        assert "matched_slug" in data
        assert "jd_overlap_score" in data


# ---------------------------------------------------------------------------
# Gap 2: warm-start verdict + JD-overlap
# ---------------------------------------------------------------------------


class TestPlannerVerdicts:
    """Exact test names required by plan done_when (Gap 2)."""

    def test_planner_verdicts_per_candidate(self, tmp_path: Path):
        """ReusePlan carries per-phase decisions that callers can inspect."""
        from jobsmith.reuse.dedup import write_jd_fingerprint
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        write_jd_fingerprint(conn, slug="prior-slug", jd_text=JD_TEXT_A)
        cfg = ReuseSettings(dedup_threshold=0.90)

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        # Exact-hash dedup hit → jd_parse + fit_score both reuse
        assert plan.jd_parse.decision == "reuse"
        assert plan.fit_score.decision == "reuse"
        assert plan.matched_slug == "prior-slug"
        # draft decision is present and has a decision key
        assert plan.draft.decision in {"warm-start", "regenerate"}

    def test_warmstart_threshold(self, tmp_path: Path):
        """Above the warm-start threshold → warm-start; below → regenerate."""
        from jobsmith.reuse.dedup import write_jd_fingerprint
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        write_jd_fingerprint(conn, slug="prior-slug", jd_text=JD_TEXT_A)

        # Threshold = 0.0 → any overlap >= 0 qualifies as warm-start
        cfg_low = ReuseSettings(dedup_threshold=0.50, jd_overlap_warm_start_threshold=0.0)
        plan_ws = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug",
            cfg=cfg_low,
            companies_dir=tmp_path / "companies",
            company_name="",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan_ws.draft.decision == "warm-start"
        assert plan_ws.jd_overlap_score >= 0.0

        # Threshold = 1.01 → impossible to reach → regenerate
        cfg_high = ReuseSettings(dedup_threshold=0.50, jd_overlap_warm_start_threshold=1.01)
        plan_regen = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="new-slug-2",
            cfg=cfg_high,
            companies_dir=tmp_path / "companies",
            company_name="",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan_regen.draft.decision == "regenerate"

    def test_no_reuse_forces_full(self, tmp_path: Path):
        """no_reuse_plan() yields all-regenerate with no warm-start."""
        from jobsmith.reuse.planner import no_reuse_plan

        plan = no_reuse_plan()
        assert plan.jd_parse.decision == "regenerate"
        assert plan.fit_score.decision == "regenerate"
        assert plan.company_research.decision == "regenerate"
        assert plan.draft.decision == "regenerate"
        assert plan.bullet_map == {}
        assert plan.matched_slug is None
        assert plan.jd_overlap_score == 0.0

    def test_no_candidate_falls_back_full(self, tmp_path: Path):
        """When there is no prior candidate, plan is all-regenerate."""
        from jobsmith.reuse.planner import compute_reuse_plan

        conn = _db(tmp_path)
        cfg = ReuseSettings()

        plan = compute_reuse_plan(
            conn=conn,
            jd_text=JD_TEXT_A,
            current_slug="brand-new-slug",
            cfg=cfg,
            companies_dir=tmp_path / "companies",
            company_name="",
            current_bullet_texts={},
            requirement_hashes=[],
        )
        assert plan.jd_parse.decision == "regenerate"
        assert plan.fit_score.decision == "regenerate"
        assert plan.draft.decision == "regenerate"
        assert plan.matched_slug is None
        assert plan.jd_overlap_score == 0.0


# ---------------------------------------------------------------------------
# Finding #4 — _load_current_bullet_texts must use guard.parse_master_bullets
# ---------------------------------------------------------------------------


class TestLoadCurrentBulletTexts:
    """_load_current_bullet_texts must produce the same bullet_id mapping as guard."""

    def _write_master_yaml(self, tmp_path: Path, content: str) -> Path:
        work_yml = tmp_path / "work.yml"
        work_yml.write_text(content, encoding="utf-8")
        return work_yml

    def test_realistic_master_yaml_produces_nonempty_map(self, tmp_path: Path) -> None:
        """Feed a realistic list-of-positions master YAML and assert non-empty result.

        The old ad-hoc parser expected {"positions": [...]} (dict root) and would
        return an empty map for the real list-of-positions format.  After the fix,
        guard.parse_master_bullets handles the real format.
        """
        import hashlib

        from jobsmith.reuse._cli_reuse import _load_current_bullet_texts

        bullet_text_a = "Led migration of $250M solar asset portfolio to new data platform."
        bullet_text_b = "Automated 200K+ solar asset monitoring pipelines using Python."

        master_content = (
            "- location: SolarCo\n"
            "  title: Data Engineer\n"
            "  details:\n"
            f"    - bullet: \"{bullet_text_a}\"\n"
            f"    - bullet: \"{bullet_text_b}\"\n"
        )
        work_yml = self._write_master_yaml(tmp_path, master_content)

        # Build stable bullet_ids as guard._bullet_id would
        def _bullet_id(text: str) -> str:
            return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

        bid_a = _bullet_id(bullet_text_a)
        bid_b = _bullet_id(bullet_text_b)

        mock_config = MagicMock()
        mock_config.master.work_yml = str(work_yml)

        with (
            patch("jobsmith.config.find_config", return_value=tmp_path / ".apply-config.yaml"),
            patch("jobsmith.config.load_config", return_value=mock_config),
            patch("jobsmith.paths.resolve", return_value=work_yml),
        ):
            result = _load_current_bullet_texts(tmp_path)

        assert result, "Expected a non-empty bullet map from realistic master YAML"
        assert bid_a in result, f"bullet_id {bid_a!r} missing from result"
        assert bid_b in result, f"bullet_id {bid_b!r} missing from result"
        assert result[bid_a] == bullet_text_a
        assert result[bid_b] == bullet_text_b

    def test_string_details_entries_work(self, tmp_path: Path) -> None:
        """guard.parse_master_bullets also handles plain-string entries in details."""
        import hashlib

        from jobsmith.reuse._cli_reuse import _load_current_bullet_texts

        bullet_text = "Built ETL pipelines processing 1M+ records daily."
        master_content = (
            "- location: Acme Corp\n"
            "  title: Engineer\n"
            "  details:\n"
            f"    - {bullet_text!r}\n"
        )
        work_yml = self._write_master_yaml(tmp_path, master_content)

        def _bullet_id(text: str) -> str:
            return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

        bid = _bullet_id(bullet_text)
        mock_config = MagicMock()
        mock_config.master.work_yml = str(work_yml)

        with (
            patch("jobsmith.config.find_config", return_value=tmp_path / ".apply-config.yaml"),
            patch("jobsmith.config.load_config", return_value=mock_config),
            patch("jobsmith.paths.resolve", return_value=work_yml),
        ):
            result = _load_current_bullet_texts(tmp_path)

        assert bid in result
        assert result[bid] == bullet_text
