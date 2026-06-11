"""Tests for location filters + ashby location capture (feat-e0aa9c3a).

TDD — written before implementation.

Covers:
  1. Ashby adapter: location from `location` field (primary), secondaryLocations joined.
  2. Ashby adapter: falls back to locationName when location empty.
  3. Re-sight location backfill in store: NULL/empty location updated on re-sight.
  4. Re-sight backfill: does NOT overwrite non-empty location.
  5. Re-sight backfill: does NOT change status.
  6. location_passes predicate semantics: match / no-match / empty+keep / empty+dismiss / disabled.
  7. Config: location_filters section parsed; defaults.
  8. Runner: ATS roles filtered by location; filtered-but-existing dedup_key still touched.
  9. Runner: email postings filtered by location.
  10. Prune: dismisses 'sourced' rows with non-matching location (unknown=dismiss).
  11. Prune: keeps unknown-location rows when unknown=keep.
"""

from __future__ import annotations

import json
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
    path = tmp_path / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(path)
    conn.close()
    return path


@pytest.fixture()
def minimal_repo(tmp_path: Path) -> Path:
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
    db_path_local = db_dir / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path_local)
    conn.close()
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Ashby adapter: location field (primary) + secondaryLocations join
# ---------------------------------------------------------------------------

ASHBY_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "ats" / "ashby_linear_response.json"
)


def _ashby_payload_with_location(
    primary_location: str = "North America",
    secondary_locations: list | None = None,
    location_name: str = "",
) -> dict:
    """Build a minimal Ashby payload with location fields."""
    return {
        "jobs": [
            {
                "id": "test-001",
                "title": "Data Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/co/test-001",
                "location": primary_location,
                "locationName": location_name,
                "secondaryLocations": secondary_locations or [],
                "publishedAt": "2026-06-01T00:00:00Z",
                "descriptionHtml": "<p>Build pipelines.</p>",
            }
        ]
    }


def test_ashby_parse_uses_location_field() -> None:
    """parse_ashby_payload uses the `location` field (primary) when non-empty."""
    from jobsmith.sourcing.adapters.ashby import parse_ashby_payload

    payload = _ashby_payload_with_location(primary_location="North America")
    roles = list(parse_ashby_payload(payload, source_slug="co"))
    assert len(roles) == 1
    assert roles[0].location == "North America"


def test_ashby_parse_falls_back_to_location_name() -> None:
    """parse_ashby_payload falls back to `locationName` when `location` is empty."""
    from jobsmith.sourcing.adapters.ashby import parse_ashby_payload

    payload = _ashby_payload_with_location(primary_location="", location_name="Remote, US")
    roles = list(parse_ashby_payload(payload, source_slug="co"))
    assert roles[0].location == "Remote, US"


def test_ashby_parse_secondary_locations_appended() -> None:
    """Secondary locations are appended with '; ' separator."""
    from jobsmith.sourcing.adapters.ashby import parse_ashby_payload

    secondary = [
        {"location": "Europe", "address": {}},
        {"location": "Australia", "address": {}},
    ]
    payload = _ashby_payload_with_location(
        primary_location="North America",
        secondary_locations=secondary,
    )
    roles = list(parse_ashby_payload(payload, source_slug="co"))
    assert roles[0].location == "North America; Europe; Australia"


def test_ashby_parse_secondary_locations_empty_list() -> None:
    """Empty secondaryLocations: only primary location returned (no trailing '; ')."""
    from jobsmith.sourcing.adapters.ashby import parse_ashby_payload

    payload = _ashby_payload_with_location(
        primary_location="Remote", secondary_locations=[]
    )
    roles = list(parse_ashby_payload(payload, source_slug="co"))
    assert roles[0].location == "Remote"


def test_ashby_parse_fixture_location_round_trip() -> None:
    """Updated fixture still parses correctly (regression guard)."""
    from jobsmith.sourcing.adapters.ashby import parse_ashby_payload

    payload = json.loads(ASHBY_FIXTURE_PATH.read_text())
    roles = list(parse_ashby_payload(payload, source_slug="linear"))
    # All fixture jobs should have non-empty location
    for role in roles:
        assert role.location, f"Expected non-empty location for {role.title}"


# ---------------------------------------------------------------------------
# 2. Re-sight location backfill
# ---------------------------------------------------------------------------


def _seed_posting_with_location(conn, dedup_key: str, location: str, status: str = "sourced") -> int:
    """Insert a posting and optionally set a non-sourced status."""
    from jobsmith.sourcing.store import set_posting_status, upsert_posting

    pid = upsert_posting(
        conn,
        source="ashby/linear",
        dedup_key=dedup_key,
        external_id=f"ashby:linear:{dedup_key}",
        url=f"https://jobs.ashbyhq.com/linear/{dedup_key}",
        title="Data Engineer",
        company="Linear",
        location=location,
        fast_score=0.5,
    )
    if status != "sourced":
        set_posting_status(conn, posting_id=pid, status=status)
    conn.commit()
    return pid


def test_upsert_posting_backfills_empty_location(db_path: Path) -> None:
    """On re-sight: if stored location is NULL/empty and incoming is non-empty, update it."""
    from jobsmith.sourcing.store import upsert_posting

    conn = jobsmith_db.open_pipeline_db(db_path)
    dk = "backfill-dk-001"
    # First sight: no location
    pid = _seed_posting_with_location(conn, dk, location="")
    row_before = conn.execute("SELECT location FROM postings WHERE id = ?", (pid,)).fetchone()
    assert (row_before["location"] or "") == ""

    # Re-sight with location
    upsert_posting(
        conn,
        source="ashby/linear",
        dedup_key=dk,
        external_id="ashby:linear:backfill-dk-001",
        url="https://jobs.ashbyhq.com/linear/backfill-dk-001",
        title="Data Engineer",
        company="Linear",
        location="Remote",
        fast_score=0.5,
    )
    conn.commit()
    row_after = conn.execute("SELECT location FROM postings WHERE id = ?", (pid,)).fetchone()
    assert row_after["location"] == "Remote"
    conn.close()


def test_upsert_posting_does_not_overwrite_non_empty_location(db_path: Path) -> None:
    """On re-sight: if stored location is already non-empty, do NOT overwrite it."""
    from jobsmith.sourcing.store import upsert_posting

    conn = jobsmith_db.open_pipeline_db(db_path)
    dk = "backfill-dk-002"
    pid = _seed_posting_with_location(conn, dk, location="San Francisco, CA")

    # Re-sight with different location
    upsert_posting(
        conn,
        source="ashby/linear",
        dedup_key=dk,
        external_id="ashby:linear:backfill-dk-002",
        url="https://jobs.ashbyhq.com/linear/backfill-dk-002",
        title="Data Engineer",
        company="Linear",
        location="Remote",
        fast_score=0.5,
    )
    conn.commit()
    row = conn.execute("SELECT location FROM postings WHERE id = ?", (pid,)).fetchone()
    assert row["location"] == "San Francisco, CA"  # NOT overwritten
    conn.close()


def test_upsert_posting_backfill_does_not_change_status(db_path: Path) -> None:
    """Location backfill must never change status of existing row."""
    from jobsmith.sourcing.store import upsert_posting

    conn = jobsmith_db.open_pipeline_db(db_path)
    dk = "backfill-dk-003"
    pid = _seed_posting_with_location(conn, dk, location="", status="queued")
    row_before = conn.execute("SELECT status FROM postings WHERE id = ?", (pid,)).fetchone()
    assert row_before["status"] == "queued"

    # Re-sight with location — status must stay queued
    upsert_posting(
        conn,
        source="ashby/linear",
        dedup_key=dk,
        external_id="ashby:linear:backfill-dk-003",
        url="https://jobs.ashbyhq.com/linear/backfill-dk-003",
        title="Data Engineer",
        company="Linear",
        location="Remote",
        fast_score=0.5,
    )
    conn.commit()
    row_after = conn.execute("SELECT status, location FROM postings WHERE id = ?", (pid,)).fetchone()
    assert row_after["status"] == "queued"  # unchanged
    assert row_after["location"] == "Remote"  # backfilled
    conn.close()


def test_upsert_posting_backfill_null_incoming_does_nothing(db_path: Path) -> None:
    """Re-sight with None/empty incoming location does not clear existing location."""
    from jobsmith.sourcing.store import upsert_posting

    conn = jobsmith_db.open_pipeline_db(db_path)
    dk = "backfill-dk-004"
    pid = _seed_posting_with_location(conn, dk, location="Durham, NC")

    # Re-sight with empty location — should not clear the stored value
    upsert_posting(
        conn,
        source="ashby/linear",
        dedup_key=dk,
        external_id="ashby:linear:backfill-dk-004",
        url="https://jobs.ashbyhq.com/linear/backfill-dk-004",
        title="Data Engineer",
        company="Linear",
        location=None,
        fast_score=0.5,
    )
    conn.commit()
    row = conn.execute("SELECT location FROM postings WHERE id = ?", (pid,)).fetchone()
    assert row["location"] == "Durham, NC"  # not cleared
    conn.close()


# ---------------------------------------------------------------------------
# 3. location_passes predicate
# ---------------------------------------------------------------------------


def test_location_passes_matching_pattern() -> None:
    """location_passes returns True when location matches allowed_patterns."""
    from jobsmith.sourcing.runner import location_passes

    assert location_passes("Remote, US", allowed_patterns=["remote"], unknown="keep") is True


def test_location_passes_no_match() -> None:
    """location_passes returns False when location does not match any pattern."""
    from jobsmith.sourcing.runner import location_passes

    assert location_passes("San Francisco, CA", allowed_patterns=["remote", "durham"], unknown="keep") is False


def test_location_passes_case_insensitive() -> None:
    """Matching is case-insensitive."""
    from jobsmith.sourcing.runner import location_passes

    assert location_passes("REMOTE (NORTH AMERICA)", allowed_patterns=["remote"], unknown="keep") is True


def test_location_passes_empty_allowed_patterns_disabled() -> None:
    """Empty allowed_patterns = filter disabled → always pass."""
    from jobsmith.sourcing.runner import location_passes

    assert location_passes("San Francisco, CA", allowed_patterns=[], unknown="dismiss") is True
    assert location_passes("", allowed_patterns=[], unknown="dismiss") is True


def test_location_passes_unknown_keep() -> None:
    """Empty/None location + unknown=keep → pass."""
    from jobsmith.sourcing.runner import location_passes

    assert location_passes("", allowed_patterns=["remote"], unknown="keep") is True
    assert location_passes(None, allowed_patterns=["remote"], unknown="keep") is True


def test_location_passes_unknown_dismiss() -> None:
    """Empty/None location + unknown=dismiss → fail."""
    from jobsmith.sourcing.runner import location_passes

    assert location_passes("", allowed_patterns=["remote"], unknown="dismiss") is False
    assert location_passes(None, allowed_patterns=["remote"], unknown="dismiss") is False


def test_location_passes_durham_nc() -> None:
    """Durham + NC patterns match Durham-area postings."""
    from jobsmith.sourcing.runner import location_passes

    patterns = ["remote", "durham", "raleigh", ", nc"]
    assert location_passes("Durham, NC", allowed_patterns=patterns, unknown="keep") is True
    assert location_passes("Raleigh, NC (Hybrid)", allowed_patterns=patterns, unknown="keep") is True
    assert location_passes("Chapel Hill, NC", allowed_patterns=[", nc"], unknown="keep") is True


# ---------------------------------------------------------------------------
# 4. Config: location_filters parsed + defaults
# ---------------------------------------------------------------------------


def test_config_location_filters_parsed(tmp_path: Path) -> None:
    """location_filters section is parsed into typed dataclass fields."""
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text(
        dedent("""\
        location_filters:
          allowed_patterns: ["remote", "durham", ", nc"]
          unknown: dismiss
        sources: []
        """)
    )
    cfg = load_sourcing_config(cfg_file)
    assert cfg.location_allowed_patterns == ["remote", "durham", ", nc"]
    assert cfg.location_unknown == "dismiss"


def test_config_location_filters_defaults(tmp_path: Path) -> None:
    """Missing location_filters section defaults to empty list and 'keep'."""
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text("sources: []\n")
    cfg = load_sourcing_config(cfg_file)
    assert cfg.location_allowed_patterns == []
    assert cfg.location_unknown == "keep"


def test_config_no_file_location_defaults() -> None:
    """SourcingConfig() defaults: empty allowed_patterns, unknown='keep'."""
    from jobsmith.sourcing.config import SourcingConfig

    cfg = SourcingConfig()
    assert cfg.location_allowed_patterns == []
    assert cfg.location_unknown == "keep"


def test_config_location_filters_partial(tmp_path: Path) -> None:
    """location_filters with only allowed_patterns (no unknown) defaults unknown='keep'."""
    from jobsmith.sourcing.config import load_sourcing_config

    cfg_file = tmp_path / "sourcing.yaml"
    cfg_file.write_text(
        dedent("""\
        location_filters:
          allowed_patterns: ["remote"]
        sources: []
        """)
    )
    cfg = load_sourcing_config(cfg_file)
    assert cfg.location_allowed_patterns == ["remote"]
    assert cfg.location_unknown == "keep"


# ---------------------------------------------------------------------------
# 5. Runner: ATS roles filtered by location
# ---------------------------------------------------------------------------


def _make_role_loc(
    title: str = "Data Engineer",
    location: str = "Remote",
    role_id: str = "001",
) -> Role:
    return Role(
        id=f"greenhouse:testco:{role_id}",
        source="greenhouse",
        source_slug="testco",
        company="TestCo",
        title=title,
        location=location,
        url=f"https://testco.com/jobs/{role_id}",
        jd_text="Build data pipelines.",
        posted_date="2026-06-01",
    )


def _adapter_factory_for(roles: list[Role]):
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


def test_runner_location_filter_excludes_non_matching(db_path: Path) -> None:
    """Roles with non-matching location are not stored in DB."""
    from jobsmith.sourcing.runner import run_crawl

    sources = [{"type": "greenhouse", "slug": "testco"}]
    roles = [
        _make_role_loc(title="Data Engineer", location="Remote", role_id="001"),
        _make_role_loc(title="ML Engineer", location="San Francisco, CA", role_id="002"),
        _make_role_loc(title="Platform Engineer", location="New York, NY", role_id="003"),
    ]

    summary = run_crawl(
        db_path=db_path,
        sources=sources,
        adapter_factory=_adapter_factory_for(roles),
        location_allowed_patterns=["remote"],
        location_unknown="keep",
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


def test_runner_location_filter_unknown_keep_passes_empty(db_path: Path) -> None:
    """Roles with empty location pass when unknown=keep."""
    from jobsmith.sourcing.runner import run_crawl

    sources = [{"type": "greenhouse", "slug": "testco"}]
    roles = [
        _make_role_loc(title="Data Engineer", location="", role_id="001"),
    ]

    summary = run_crawl(
        db_path=db_path,
        sources=sources,
        adapter_factory=_adapter_factory_for(roles),
        location_allowed_patterns=["remote"],
        location_unknown="keep",
        no_llm=True,
        _rescore_n_cap=0,
    )

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT title FROM postings").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert summary["roles_filtered"] == 0


def test_runner_location_filter_unknown_dismiss_filters_empty(db_path: Path) -> None:
    """Roles with empty location are filtered when unknown=dismiss."""
    from jobsmith.sourcing.runner import run_crawl

    sources = [{"type": "greenhouse", "slug": "testco"}]
    roles = [
        _make_role_loc(title="Data Engineer", location="", role_id="001"),
        _make_role_loc(title="ML Engineer", location="Remote", role_id="002"),
    ]

    summary = run_crawl(
        db_path=db_path,
        sources=sources,
        adapter_factory=_adapter_factory_for(roles),
        location_allowed_patterns=["remote"],
        location_unknown="dismiss",
        no_llm=True,
        _rescore_n_cap=0,
    )

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        rows = conn.execute("SELECT title FROM postings").fetchall()
        titles = [r["title"] for r in rows]
    finally:
        conn.close()

    assert titles == ["ML Engineer"]
    assert summary["roles_filtered"] == 1


def test_runner_location_filter_disabled_when_no_patterns(db_path: Path) -> None:
    """Empty allowed_patterns disables location filter — all roles pass through."""
    from jobsmith.sourcing.runner import run_crawl

    sources = [{"type": "greenhouse", "slug": "testco"}]
    roles = [
        _make_role_loc(title="Data Engineer", location="San Francisco, CA", role_id="001"),
        _make_role_loc(title="ML Engineer", location="New York, NY", role_id="002"),
    ]

    summary = run_crawl(
        db_path=db_path,
        sources=sources,
        adapter_factory=_adapter_factory_for(roles),
        location_allowed_patterns=[],
        location_unknown="dismiss",
        no_llm=True,
        _rescore_n_cap=0,
    )
    assert summary["roles_filtered"] == 0
    assert summary["roles_upserted"] == 2


def test_runner_location_filtered_existing_dedup_key_still_touched(db_path: Path) -> None:
    """Filtered role that already exists in DB still gets last_seen_at bumped."""
    from jobsmith.sourcing.runner import expire_stale_postings, role_dedup_key, run_crawl
    from jobsmith.sourcing.store import set_posting_status, upsert_posting

    exact_role = _make_role_loc(title="Data Engineer", location="San Francisco, CA", role_id="sf1")
    dk = role_dedup_key(exact_role)

    conn = jobsmith_db.open_pipeline_db(db_path)
    pid = upsert_posting(
        conn,
        source="greenhouse/testco",
        dedup_key=dk,
        external_id=exact_role.id,
        url=exact_role.url,
        title=exact_role.title,
        company=exact_role.company,
        location=exact_role.location,
        fast_score=0.5,
    )
    set_posting_status(conn, posting_id=pid, status="queued")
    # Force last_seen_at to look 30 days old
    conn.execute(
        "UPDATE postings SET last_seen_at = datetime('now', '-30 days') WHERE id = ?",
        (pid,),
    )
    conn.commit()
    conn.close()

    def _factory(spec):
        from jobsmith.sourcing.adapters.base import ATSSourceAdapter

        class _FA(ATSSourceAdapter):
            name = "fake"

            def fetch(self, slug):
                return [exact_role]

        return _FA()

    run_crawl(
        db_path=db_path,
        sources=[{"type": "greenhouse", "slug": "testco"}],
        adapter_factory=_factory,
        location_allowed_patterns=["remote"],
        location_unknown="keep",
        no_llm=True,
        _rescore_n_cap=0,
    )

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        expire_stale_postings(conn, expiry_days=1)
        row = conn.execute("SELECT status FROM postings WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()

    assert row["status"] == "queued", "Queued posting must not be expired when last_seen_at bumped"


# ---------------------------------------------------------------------------
# 6. Runner: email postings filtered by location
# ---------------------------------------------------------------------------


def test_runner_email_postings_filtered_by_location(db_path: Path) -> None:
    """Email alert postings with non-matching location are dismissed."""
    from jobsmith.sourcing.runner import run_crawl

    def _mock_email_fn(conn, senders, *, dry_run, max_per_sender):
        from jobsmith.sourcing.adapters.base import Role as _Role
        from jobsmith.sourcing.runner import role_dedup_key
        from jobsmith.sourcing.store import upsert_posting

        if dry_run:
            return 0, [], []

        entries = [
            ("e1", "Data Engineer", "Remote", "https://co.com/1"),
            ("e2", "ML Engineer", "San Francisco, CA", "https://co.com/2"),
        ]
        upserted, new_ids = 0, []
        for eid, title, loc, url in entries:
            r = _Role(id=eid, source="email", source_slug="li",
                      company="Co", title=title, location=loc, url=url, jd_text="")
            dk = role_dedup_key(r)
            pid = upsert_posting(conn, source="email/li", dedup_key=dk,
                                 external_id=eid, url=url, title=title,
                                 company="Co", location=loc, fast_score=0.0)
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at FROM postings WHERE id = ?", (pid,)
            ).fetchone()
            if row and row["first_seen_at"] == row["last_seen_at"]:
                new_ids.append(pid)
            upserted += 1
        return upserted, new_ids, []

    run_crawl(
        db_path=db_path,
        sources=[],
        alert_senders=[{"type": "mailapp_alert", "sender_slug": "li"}],
        location_allowed_patterns=["remote"],
        location_unknown="keep",
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

    assert status_by_title.get("Data Engineer") == "sourced"
    assert status_by_title.get("ML Engineer") == "dismissed"


# ---------------------------------------------------------------------------
# 7. Prune: location filters applied alongside title filters
# ---------------------------------------------------------------------------


def _seed_with_location(conn, postings: list[dict]) -> list[int]:
    from jobsmith.sourcing.store import set_posting_status, upsert_posting

    ids = []
    for p in postings:
        pid = upsert_posting(
            conn,
            source=p.get("source", "ashby/co"),
            dedup_key=p["dedup_key"],
            title=p.get("title", "Data Engineer"),
            company=p.get("company", "Co"),
            url=p.get("url", "https://co.com"),
            location=p.get("location", ""),
            fast_score=p.get("fast_score", 0.5),
        )
        status = p.get("status", "sourced")
        if status != "sourced":
            set_posting_status(conn, posting_id=pid, status=status)
        ids.append(pid)
    conn.commit()
    return ids


def test_prune_dismisses_non_matching_location(minimal_repo: Path) -> None:
    """source prune dismisses sourced postings with non-matching location."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path_local = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path_local)
    _seed_with_location(conn, [
        {"dedup_key": "r1", "title": "Data Engineer", "location": "Remote", "status": "sourced"},
        {"dedup_key": "r2", "title": "ML Engineer", "location": "San Francisco, CA", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        location_filters:
          allowed_patterns: ["remote"]
          unknown: keep
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(app, ["source", "prune"], catch_exceptions=False, env=env)
    assert result.exit_code == 0, result.output

    conn = jobsmith_db.open_pipeline_db(db_path_local)
    try:
        rows = conn.execute("SELECT dedup_key, status FROM postings").fetchall()
        statuses = {r["dedup_key"]: r["status"] for r in rows}
    finally:
        conn.close()

    assert statuses["r1"] == "sourced"    # Remote — kept
    assert statuses["r2"] == "dismissed"  # SF — dismissed


def test_prune_keeps_unknown_location_when_unknown_keep(minimal_repo: Path) -> None:
    """source prune keeps unknown-location rows when unknown=keep."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path_local = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path_local)
    _seed_with_location(conn, [
        {"dedup_key": "u1", "title": "Data Engineer", "location": "", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        location_filters:
          allowed_patterns: ["remote"]
          unknown: keep
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(app, ["source", "prune"], catch_exceptions=False, env=env)
    assert result.exit_code == 0, result.output

    conn = jobsmith_db.open_pipeline_db(db_path_local)
    try:
        row = conn.execute("SELECT status FROM postings WHERE dedup_key = 'u1'").fetchone()
    finally:
        conn.close()

    assert row["status"] == "sourced"  # kept due to unknown=keep


def test_prune_dismisses_unknown_location_when_unknown_dismiss(minimal_repo: Path) -> None:
    """source prune dismisses unknown-location rows when unknown=dismiss."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path_local = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path_local)
    _seed_with_location(conn, [
        {"dedup_key": "u2", "title": "Data Engineer", "location": "", "status": "sourced"},
        {"dedup_key": "u3", "title": "ML Engineer", "location": "Remote", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        location_filters:
          allowed_patterns: ["remote"]
          unknown: dismiss
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(app, ["source", "prune"], catch_exceptions=False, env=env)
    assert result.exit_code == 0, result.output

    conn = jobsmith_db.open_pipeline_db(db_path_local)
    try:
        rows = conn.execute("SELECT dedup_key, status FROM postings").fetchall()
        statuses = {r["dedup_key"]: r["status"] for r in rows}
    finally:
        conn.close()

    assert statuses["u2"] == "dismissed"  # empty location + unknown=dismiss
    assert statuses["u3"] == "sourced"    # Remote — kept


def test_prune_location_filter_disabled_when_no_patterns(minimal_repo: Path) -> None:
    """source prune: empty allowed_patterns disables location filter."""
    import os

    from typer.testing import CliRunner

    from jobsmith.cli import app

    db_path_local = minimal_repo / "private" / "jobsmith.db"
    conn = jobsmith_db.open_pipeline_db(db_path_local)
    _seed_with_location(conn, [
        {"dedup_key": "v1", "title": "Data Engineer", "location": "San Francisco, CA", "status": "sourced"},
    ])
    conn.close()

    sourcing_yaml = minimal_repo / "sourcing.yaml"
    sourcing_yaml.write_text(
        dedent("""\
        location_filters:
          allowed_patterns: []
          unknown: dismiss
        sources: []
        """)
    )

    cli_runner = CliRunner()
    env = {**os.environ, "JOBSMITH_REPO_ROOT": str(minimal_repo)}
    result = cli_runner.invoke(app, ["source", "prune"], catch_exceptions=False, env=env)
    assert result.exit_code == 0, result.output

    conn = jobsmith_db.open_pipeline_db(db_path_local)
    try:
        row = conn.execute("SELECT status FROM postings WHERE dedup_key = 'v1'").fetchone()
    finally:
        conn.close()

    assert row["status"] == "sourced"  # filter disabled, not dismissed
