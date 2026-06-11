"""Tests for title filters + min_fast_score (feat-e32cde37).

TDD — written before implementation.

Covers:
  1. Config parsing: title_filters section + min_fast_score parsed correctly.
  2. Config defaults: missing section → empty lists / 0.0.
  3. Filter semantics:
     - exclude_patterns: substring, case-insensitive.
     - include_patterns allowlist mode.
     - min_fast_score gate.
  4. Runner: filtered postings NOT stored; roles_filtered in summary.
  5. Email postings filtered by title (title-only filter; min_fast_score
     applies if config > 0.0).
  6. Prune CLI: dismisses only status='sourced' postings; dry-run writes nothing.
  7. Prune respects title filters + min_fast_score from config.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.sourcing.adapters.base import Role

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Create and return a pipeline DB path with schema applied."""
    path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(path)
    conn.close()
    return path


@pytest.fixture()
def minimal_repo(tmp_path: Path):
    """Minimal repo with .apply-config.yaml and jobsmith.db."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        "master:\n"
        "  work_yml: assets/content/work.yml\n"
        "  skill_yml: assets/content/skill.yml\n"
        "  education_yml: assets/content/education.yml\n"
        "  author_yml: assets/content/author.yml\n"
        "  publication_yml: null\n"
        "output:\n"
        "  applications_dir: private/applications\n"
        "  job_search_db: private/job_search.db\n"
        "  jobsmith_db: private/jobsmith.db\n"
    )
    db_dir = tmp_path / "private"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    conn.close()
    return tmp_path


# ---------------------------------------------------------------------------
# 1 & 2. Config parsing
# ---------------------------------------------------------------------------


def test_config_title_filters_parsed(tmp_path: Path) -> None:
    """title_filters section is parsed into typed fields."""
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text(
        dedent("""\
        title_filters:
          exclude_patterns: ["account executive", "customer success"]
          include_patterns: ["data engineer", "ml engineer"]
        min_fast_score: 0.25
        sources: []
        """)
    )
    cfg = load_sourcing_config(cfg_file)
    assert cfg.title_exclude_patterns == ["account executive", "customer success"]
    assert cfg.title_include_patterns == ["data engineer", "ml engineer"]
    assert cfg.min_fast_score == pytest.approx(0.25)


def test_config_title_filters_defaults(tmp_path: Path) -> None:
    """Missing title_filters section and min_fast_score defaults to empty/0.0."""
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text("sources: []\n")
    cfg = load_sourcing_config(cfg_file)
    assert cfg.title_exclude_patterns == []
    assert cfg.title_include_patterns == []
    assert cfg.min_fast_score == pytest.approx(0.0)


def test_config_no_file_defaults() -> None:
    """No sourcing.yaml → package defaults → empty filter lists, 0.0 score."""
    from jobsmith.sourcing.config import SourcingConfig

    cfg = SourcingConfig()
    assert cfg.title_exclude_patterns == []
    assert cfg.title_include_patterns == []
    assert cfg.min_fast_score == pytest.approx(0.0)


def test_config_title_filters_missing_min_fast_score(tmp_path: Path) -> None:
    """title_filters present but no min_fast_score top-level → defaults to 0.0."""
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text(
        dedent("""\
        title_filters:
          exclude_patterns: ["recruiter"]
        sources: []
        """)
    )
    cfg = load_sourcing_config(cfg_file)
    assert cfg.title_exclude_patterns == ["recruiter"]
    assert cfg.title_include_patterns == []
    assert cfg.min_fast_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. Filter semantics (pure logic, no DB needed)
# ---------------------------------------------------------------------------


def test_apply_title_filters_exclude_case_insensitive() -> None:
    """Exclude pattern matches are case-insensitive substring checks."""
    from jobsmith.sourcing.runner import apply_title_filters

    roles = [
        Role(id="1", source="gh", source_slug="co", company="Co", title="Data Engineer", location="", url="", jd_text=""),
        Role(id="2", source="gh", source_slug="co", company="Co", title="Account Executive - West", location="", url="", jd_text=""),
        Role(id="3", source="gh", source_slug="co", company="Co", title="SOLUTIONS ARCHITECT", location="", url="", jd_text=""),
    ]
    kept, filtered = apply_title_filters(
        roles,
        exclude_patterns=["account executive", "solutions architect"],
        include_patterns=[],
        min_fast_score=0.0,
        scored_roles=None,
    )
    assert len(kept) == 1
    assert kept[0].title == "Data Engineer"
    assert filtered == 2


def test_apply_title_filters_include_allowlist() -> None:
    """Include patterns are an allowlist — title MUST match at least one."""
    from jobsmith.sourcing.runner import apply_title_filters

    roles = [
        Role(id="1", source="gh", source_slug="co", company="Co", title="Data Engineer", location="", url="", jd_text=""),
        Role(id="2", source="gh", source_slug="co", company="Co", title="ML Engineer", location="", url="", jd_text=""),
        Role(id="3", source="gh", source_slug="co", company="Co", title="Sales Manager", location="", url="", jd_text=""),
    ]
    kept, filtered = apply_title_filters(
        roles,
        exclude_patterns=[],
        include_patterns=["data engineer", "ml engineer"],
        min_fast_score=0.0,
        scored_roles=None,
    )
    assert len(kept) == 2
    assert {r.title for r in kept} == {"Data Engineer", "ML Engineer"}
    assert filtered == 1


def test_apply_title_filters_exclude_wins_over_include() -> None:
    """Exclude takes priority: if a title matches both, it's excluded."""
    from jobsmith.sourcing.runner import apply_title_filters

    roles = [
        # "solutions engineer" matches both exclude and would match include "engineer"
        Role(id="1", source="gh", source_slug="co", company="Co", title="Solutions Engineer", location="", url="", jd_text=""),
    ]
    kept, filtered = apply_title_filters(
        roles,
        exclude_patterns=["solutions engineer"],
        include_patterns=["engineer"],
        min_fast_score=0.0,
        scored_roles=None,
    )
    assert kept == []
    assert filtered == 1


def test_apply_title_filters_min_fast_score() -> None:
    """Roles with fast_score below min_fast_score are filtered out."""
    from jobsmith.sourcing.runner import apply_title_filters

    roles = [
        Role(id="1", source="gh", source_slug="co", company="Co", title="Data Engineer", location="", url="", jd_text=""),
        Role(id="2", source="gh", source_slug="co", company="Co", title="ML Engineer", location="", url="", jd_text=""),
        Role(id="3", source="gh", source_slug="co", company="Co", title="Software Engineer", location="", url="", jd_text=""),
    ]
    # scored_roles maps dedup_key -> fast_score; roles without a score entry pass through
    scored_roles = {
        roles[0].id: 0.4,  # above 0.3 → kept
        roles[1].id: 0.1,  # below 0.3 → filtered
        # roles[2] has no score → pass through (score unknown, be conservative)
    }
    kept, filtered = apply_title_filters(
        roles,
        exclude_patterns=[],
        include_patterns=[],
        min_fast_score=0.3,
        scored_roles=scored_roles,
    )
    assert len(kept) == 2
    assert roles[0] in kept
    assert roles[2] in kept
    assert filtered == 1


def test_apply_title_filters_empty_config() -> None:
    """Empty config: all roles pass through unchanged."""
    from jobsmith.sourcing.runner import apply_title_filters

    roles = [
        Role(id="1", source="gh", source_slug="co", company="Co", title="Recruiter", location="", url="", jd_text=""),
        Role(id="2", source="gh", source_slug="co", company="Co", title="Data Engineer", location="", url="", jd_text=""),
    ]
    kept, filtered = apply_title_filters(
        roles,
        exclude_patterns=[],
        include_patterns=[],
        min_fast_score=0.0,
        scored_roles=None,
    )
    assert len(kept) == 2
    assert filtered == 0


# ---------------------------------------------------------------------------
# 4. Runner: filtered postings not stored; roles_filtered in summary
# ---------------------------------------------------------------------------


def _make_role(
    title: str = "Data Engineer",
    company: str = "TestCo",
    url: str = "https://testco.com/jobs/1",
    jd_text: str = "Build data pipelines.",
    role_id: str = "001",
) -> Role:
    return Role(
        id=f"greenhouse:testco:{role_id}",
        source="greenhouse",
        source_slug="testco",
        company=company,
        title=title,
        location="Remote",
        url=url,
        jd_text=jd_text,
        posted_date="2026-06-01",
    )


def _adapter_factory_for(roles: list[Role]):
    """Return an adapter_factory that always yields the given roles."""
    from collections.abc import Iterable

    from jobsmith.sourcing.adapters.base import ATSSourceAdapter

    class _FakeAdapter(ATSSourceAdapter):
        name = "fake"

        def __init__(self, roles: list[Role]) -> None:
            self._roles = roles

        def fetch(self, slug: str) -> Iterable[Role]:
            return iter(self._roles)

    def factory(spec: dict):
        return _FakeAdapter(roles)

    return factory


def test_runner_filtered_roles_not_stored(db_path: Path) -> None:
    """Roles matching exclude_patterns are never written to DB."""
    from jobsmith.sourcing.runner import run_crawl

    sources = [{"type": "greenhouse", "slug": "testco"}]
    roles = [
        _make_role(title="Data Engineer", url="https://testco.com/jobs/1", role_id="001"),
        _make_role(title="Account Executive", url="https://testco.com/jobs/2", role_id="002"),
        _make_role(title="Recruiter", url="https://testco.com/jobs/3", role_id="003"),
    ]

    summary = run_crawl(
        db_path=db_path,
        sources=sources,
        adapter_factory=_adapter_factory_for(roles),
        title_exclude_patterns=["account executive", "recruiter"],
        title_include_patterns=[],
        min_fast_score=0.0,
        no_llm=True,
        _rescore_n_cap=0,
    )

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT title FROM postings ORDER BY title").fetchall()
        titles = [r["title"] for r in rows]
    finally:
        conn.close()

    assert titles == ["Data Engineer"]
    assert summary["roles_filtered"] == 2
    assert summary["roles_fetched"] == 3
    assert summary["roles_upserted"] == 1


def test_runner_roles_filtered_in_summary(db_path: Path) -> None:
    """Summary contains roles_filtered key even when zero filtered."""
    from jobsmith.sourcing.runner import run_crawl

    sources = [{"type": "greenhouse", "slug": "testco"}]
    roles = [_make_role(title="Data Engineer", role_id="001")]

    summary = run_crawl(
        db_path=db_path,
        sources=sources,
        adapter_factory=_adapter_factory_for(roles),
        title_exclude_patterns=[],
        title_include_patterns=[],
        min_fast_score=0.0,
        no_llm=True,
        _rescore_n_cap=0,
    )
    assert "roles_filtered" in summary
    assert summary["roles_filtered"] == 0


# ---------------------------------------------------------------------------
# 5. Email postings filtered by title
# ---------------------------------------------------------------------------


def test_runner_email_postings_filtered_by_title(db_path: Path) -> None:
    """Email alert postings matching exclude_patterns are not stored."""
    from jobsmith.sourcing.runner import run_crawl

    # Two email postings: one keeper, one excluded
    def _mock_email_fn(conn, senders, *, dry_run, max_per_sender):
        # Returns: (upserted, new_ids, degraded)
        # We need to simulate the upsert happening inside run_email_alerts
        # Instead we test that run_crawl wraps the fn with filtering
        from jobsmith.sourcing.adapters.base import Role as _Role
        from jobsmith.sourcing.runner import role_dedup_key
        from jobsmith.sourcing.store import upsert_posting

        if dry_run:
            return 0, [], []

        roles_to_upsert = [
            {"title": "Data Engineer", "company": "Co", "url": "https://co.com/1", "source": "email/li", "external_id": "e1"},
            {"title": "Account Executive", "company": "Co", "url": "https://co.com/2", "source": "email/li", "external_id": "e2"},
        ]
        upserted = 0
        new_ids = []
        for entry in roles_to_upsert:
            r = _Role(id=entry["external_id"], source="email", source_slug="li",
                      company=entry["company"], title=entry["title"],
                      location="", url=entry["url"], jd_text="")
            dk = role_dedup_key(r)
            pid = upsert_posting(conn, source=entry["source"], dedup_key=dk,
                                 external_id=entry["external_id"], url=entry["url"],
                                 title=entry["title"], company=entry["company"],
                                 location="", fast_score=0.0)
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at FROM postings WHERE id = ?", (pid,)
            ).fetchone()
            if row and row["first_seen_at"] == row["last_seen_at"]:
                new_ids.append(pid)
            upserted += 1
        return upserted, new_ids, []

    summary = run_crawl(
        db_path=db_path,
        sources=[],
        alert_senders=[{"type": "mailapp_alert", "sender_slug": "li"}],
        title_exclude_patterns=["account executive"],
        title_include_patterns=[],
        min_fast_score=0.0,
        no_llm=True,
        _rescore_n_cap=0,
        _run_email_alerts_fn=_mock_email_fn,
    )

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT title, status FROM postings").fetchall()
        status_by_title = {r["title"]: r["status"] for r in rows}
    finally:
        conn.close()

    # "Account Executive" should have been dismissed (not stored as 'sourced')
    assert status_by_title.get("Account Executive") == "dismissed"
    assert status_by_title.get("Data Engineer") == "sourced"
    assert summary["roles_filtered"] >= 1


# ---------------------------------------------------------------------------
# 6 & 7. Prune CLI: dry-run writes nothing; real run dismisses only 'sourced'
# ---------------------------------------------------------------------------


def _seed_postings(conn, postings: list[dict]) -> list[int]:
    """Seed test postings; returns list of inserted ids."""
    from jobsmith.sourcing.store import set_posting_status, upsert_posting

    ids = []
    for p in postings:
        pid = upsert_posting(
            conn,
            source=p.get("source", "gh/co"),
            dedup_key=p["dedup_key"],
            title=p.get("title", ""),
            company=p.get("company", "TestCo"),
            url=p.get("url", "https://co.com"),
            fast_score=p.get("fast_score", 0.5),
            location="",
        )
        status = p.get("status", "sourced")
        if status != "sourced":
            set_posting_status(conn, posting_id=pid, status=status)
        ids.append(pid)
    conn.commit()
    return ids


def test_prune_dry_run_writes_nothing(minimal_repo: Path) -> None:
    """prune --dry-run shows candidates but does NOT change DB."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    _seed_postings(conn, [
        {"dedup_key": "abc1", "title": "Account Executive", "status": "sourced"},
        {"dedup_key": "abc2", "title": "Data Engineer", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        title_filters:
          exclude_patterns: ["account executive"]
        min_fast_score: 0.0
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(
        app, ["source", "prune", "--dry-run"], catch_exceptions=False, env=env
    )
    assert result.exit_code == 0, result.output

    # DB must be untouched
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute(
            "SELECT title, status FROM postings ORDER BY title"
        ).fetchall()
        statuses = {r["title"]: r["status"] for r in rows}
    finally:
        conn.close()

    assert statuses["Account Executive"] == "sourced"  # NOT dismissed
    assert statuses["Data Engineer"] == "sourced"


def test_prune_dismisses_only_sourced(minimal_repo: Path) -> None:
    """prune dismisses status='sourced' matches, never queued/promoted/dismissed/expired."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)

    _seed_postings(conn, [
        {"dedup_key": "p1", "title": "Account Executive", "status": "sourced"},
        {"dedup_key": "p2", "title": "Account Executive Senior", "status": "queued"},
        {"dedup_key": "p3", "title": "Account Executive Lead", "status": "dismissed"},
        {"dedup_key": "p4", "title": "Account Executive VP", "status": "promoted"},
        {"dedup_key": "p5", "title": "Data Engineer", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        title_filters:
          exclude_patterns: ["account executive"]
        min_fast_score: 0.0
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(
        app, ["source", "prune"], catch_exceptions=False, env=env
    )
    assert result.exit_code == 0, result.output

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT dedup_key, status FROM postings").fetchall()
        statuses = {r["dedup_key"]: r["status"] for r in rows}
    finally:
        conn.close()

    assert statuses["p1"] == "dismissed"   # sourced + match → dismissed
    assert statuses["p2"] == "queued"      # queued → untouched
    assert statuses["p3"] == "dismissed"   # already dismissed → stays dismissed
    assert statuses["p4"] == "promoted"    # promoted → untouched
    assert statuses["p5"] == "sourced"     # Data Engineer → untouched


def test_prune_min_fast_score_dismisses_low_scored(minimal_repo: Path) -> None:
    """prune dismisses sourced postings below min_fast_score."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    _seed_postings(conn, [
        {"dedup_key": "lo1", "title": "Data Engineer", "fast_score": 0.05, "status": "sourced"},
        {"dedup_key": "hi1", "title": "ML Engineer", "fast_score": 0.8, "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        title_filters:
          exclude_patterns: []
        min_fast_score: 0.3
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(
        app, ["source", "prune"], catch_exceptions=False, env=env
    )
    assert result.exit_code == 0, result.output

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT dedup_key, status FROM postings").fetchall()
        statuses = {r["dedup_key"]: r["status"] for r in rows}
    finally:
        conn.close()

    assert statuses["lo1"] == "dismissed"
    assert statuses["hi1"] == "sourced"


def test_prune_reports_counts(minimal_repo: Path) -> None:
    """prune prints dismissed count in its output."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path)
    _seed_postings(conn, [
        {"dedup_key": "ex1", "title": "Recruiter", "status": "sourced"},
        {"dedup_key": "ex2", "title": "Data Engineer", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        title_filters:
          exclude_patterns: ["recruiter"]
        min_fast_score: 0.0
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(
        app, ["source", "prune"], catch_exceptions=False, env=env
    )
    assert result.exit_code == 0, result.output
    # Output should mention 1 dismissed
    assert "1" in result.output
