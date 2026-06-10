"""Tests for jobsmith.sourcing.runner — crawl orchestration, isolation, expiry.

TDD: written before implementation (feat-5531c54b).

Covers:
  - Determinism: given the same sources, run_crawl produces consistent DB writes.
  - Per-source isolation: a crashing adapter does NOT stop other adapters.
  - Circuit breaker: after CIRCUIT_BREAKER_THRESHOLD failures, source added to degraded.
  - Auto-expiry: postings not re-sighted are marked expired.
  - dry_run: no DB writes when dry_run=True.
  - source_filter: only the filtered source is crawled.
  - sourcing_run record is created + finished.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.sourcing.adapters.base import Role, SourceFetchError
from jobsmith.sourcing.runner import (
    CIRCUIT_BREAKER_THRESHOLD,
    canonical_url,
    expire_stale_postings,
    role_dedup_key,
    run_crawl,
)

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


def _make_role(
    slug: str = "testco",
    title: str = "Data Engineer",
    company: str = "TestCo",
    url: str = "https://testco.com/jobs/1",
    jd_text: str = "Build data pipelines.",
) -> Role:
    return Role(
        id=f"greenhouse:{slug}:001",
        source="greenhouse",
        source_slug=slug,
        company=company,
        title=title,
        location="Remote",
        url=url,
        jd_text=jd_text,
        posted_date="2026-06-01",
    )


def _adapter_factory_for(roles_by_key: dict):
    """Return an adapter_factory that yields specified roles per source key."""
    from collections.abc import Iterable

    from jobsmith.sourcing.adapters.base import ATSSourceAdapter

    class _FakeAdapter(ATSSourceAdapter):
        name = "fake"

        def __init__(self, roles: list[Role]) -> None:
            self._roles = roles

        def fetch(self, slug: str) -> Iterable[Role]:
            return iter(self._roles)

    def factory(spec: dict):
        key = f"{spec.get('type')}/{spec.get('slug')}"
        roles = roles_by_key.get(key)
        if roles is None:
            return None
        return _FakeAdapter(roles)

    return factory


def _crashing_adapter_factory(crash_key: str, fallback_roles: list[Role]):
    """Adapter factory where crash_key always raises SourceFetchError."""
    from collections.abc import Iterable

    from jobsmith.sourcing.adapters.base import ATSSourceAdapter

    class _CrashAdapter(ATSSourceAdapter):
        name = "crash"

        def fetch(self, slug: str) -> Iterable[Role]:
            raise SourceFetchError("simulated crash")

    class _OkAdapter(ATSSourceAdapter):
        name = "ok"

        def __init__(self, roles: list[Role]) -> None:
            self._roles = roles

        def fetch(self, slug: str) -> Iterable[Role]:
            return iter(self._roles)

    def factory(spec: dict):
        key = f"{spec.get('type')}/{spec.get('slug')}"
        if key == crash_key:
            return _CrashAdapter()
        return _OkAdapter(fallback_roles)

    return factory


# ---------------------------------------------------------------------------
# canonical_url
# ---------------------------------------------------------------------------


def test_canonical_url_strips_query() -> None:
    assert canonical_url("https://x.com/job?ref=foo") == "https://x.com/job"


def test_canonical_url_strips_trailing_slash() -> None:
    assert canonical_url("https://x.com/job/") == "https://x.com/job"


def test_canonical_url_lowercases_host() -> None:
    assert canonical_url("HTTPS://X.COM/job") == "https://x.com/job"


def test_canonical_url_empty_returns_empty() -> None:
    assert canonical_url("") == ""


# ---------------------------------------------------------------------------
# role_dedup_key
# ---------------------------------------------------------------------------


def test_role_dedup_key_stable() -> None:
    r1 = _make_role()
    r2 = _make_role()
    assert role_dedup_key(r1) == role_dedup_key(r2)


def test_role_dedup_key_differs_for_different_url_no_external_id() -> None:
    """Without an external_id, different URLs produce different dedup keys."""
    r1 = Role(
        id="",
        source="greenhouse",
        source_slug="testco",
        company="TestCo",
        title="Data Engineer",
        location="Remote",
        url="https://testco.com/jobs/1",
        jd_text="",
    )
    r2 = Role(
        id="",
        source="greenhouse",
        source_slug="testco",
        company="TestCo",
        title="Data Engineer",
        location="Remote",
        url="https://testco.com/jobs/2",
        jd_text="",
    )
    assert role_dedup_key(r1) != role_dedup_key(r2)


def test_role_dedup_key_differs_for_different_external_id() -> None:
    """Two Indeed jobs with same company/title but different jk= external IDs → different keys."""
    r1 = Role(
        id="abc123",
        source="email",
        source_slug="indeed-alert",
        company="Acme",
        title="Data Engineer",
        location="Remote",
        url="https://www.indeed.com/viewjob?jk=abc123",
        jd_text="",
    )
    r2 = Role(
        id="xyz789",
        source="email",
        source_slug="indeed-alert",
        company="Acme",
        title="Data Engineer",
        location="Remote",
        url="https://www.indeed.com/viewjob?jk=xyz789",
        jd_text="",
    )
    assert role_dedup_key(r1) != role_dedup_key(r2)


def test_role_dedup_key_same_external_id_collides_despite_different_url() -> None:
    """Two postings with the same external_id produce the same dedup key."""
    r1 = Role(
        id="same-id",
        source="greenhouse",
        source_slug="testco",
        company="TestCo",
        title="Data Engineer",
        location="Remote",
        url="https://testco.com/jobs/1",
        jd_text="",
    )
    r2 = Role(
        id="same-id",
        source="greenhouse",
        source_slug="testco",
        company="TestCo",
        title="Data Engineer",
        location="Remote",
        url="https://testco.com/jobs/2",
        jd_text="",
    )
    assert role_dedup_key(r1) == role_dedup_key(r2)


# ---------------------------------------------------------------------------
# run_crawl — basic upsert
# ---------------------------------------------------------------------------


def test_run_crawl_upserts_postings(db_path: Path) -> None:
    role = _make_role()
    sources = [{"type": "greenhouse", "slug": "testco"}]
    factory = _adapter_factory_for({"greenhouse/testco": [role]})

    summary = run_crawl(db_path, sources, adapter_factory=factory)

    assert summary["roles_fetched"] == 1
    assert summary["roles_upserted"] == 1
    assert "greenhouse/testco" in summary["sources_checked"]

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM postings").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_run_crawl_no_sources_returns_clean(db_path: Path) -> None:
    summary = run_crawl(db_path, sources=[])
    assert summary["roles_fetched"] == 0
    assert not summary["aborted"]


def test_run_crawl_dry_run_writes_nothing(db_path: Path) -> None:
    role = _make_role()
    sources = [{"type": "greenhouse", "slug": "testco"}]
    factory = _adapter_factory_for({"greenhouse/testco": [role]})

    summary = run_crawl(db_path, sources, adapter_factory=factory, dry_run=True)

    assert summary["roles_fetched"] == 1
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM postings").fetchone()
        assert row[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_crawl — dedup: same role fetched twice
# ---------------------------------------------------------------------------


def test_run_crawl_dedup_prevents_duplicate_postings(db_path: Path) -> None:
    role = _make_role()
    sources = [{"type": "greenhouse", "slug": "testco"}]
    factory = _adapter_factory_for({"greenhouse/testco": [role]})

    # First run
    run_crawl(db_path, sources, adapter_factory=factory)
    # Second run — same role
    run_crawl(db_path, sources, adapter_factory=factory)

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM postings").fetchone()
        assert row[0] == 1  # still only one row
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_crawl — per-source isolation
# ---------------------------------------------------------------------------


def test_run_crawl_crash_in_one_source_does_not_stop_others(db_path: Path) -> None:
    ok_role = _make_role(slug="okco", company="OkCo", url="https://okco.com/job/1")
    sources = [
        {"type": "greenhouse", "slug": "crashco"},
        {"type": "greenhouse", "slug": "okco"},
    ]
    factory = _crashing_adapter_factory("greenhouse/crashco", [ok_role])

    summary = run_crawl(db_path, sources, adapter_factory=factory)

    # okco should have been fetched
    assert "greenhouse/okco" in summary["sources_checked"]
    # crashco should be in error_counts
    assert summary["error_counts"].get("greenhouse/crashco", 0) > 0

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM postings").fetchone()
        assert row[0] == 1  # okco's role
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_run_crawl_circuit_breaker_marks_source_degraded(db_path: Path) -> None:
    """A source that fails CIRCUIT_BREAKER_THRESHOLD times is marked degraded."""
    sources = [
        {"type": "greenhouse", "slug": "crashco"},
    ] * CIRCUIT_BREAKER_THRESHOLD

    def crashing_factory(spec):
        from collections.abc import Iterable

        from jobsmith.sourcing.adapters.base import ATSSourceAdapter

        class _Crash(ATSSourceAdapter):
            name = "crash"

            def fetch(self, slug: str) -> Iterable[Role]:
                raise SourceFetchError("crash")

        return _Crash()

    summary = run_crawl(db_path, sources, adapter_factory=crashing_factory)
    assert "greenhouse/crashco" in summary["degraded_sources"]


# ---------------------------------------------------------------------------
# Auto-expiry
# ---------------------------------------------------------------------------


def test_expire_stale_postings_expires_old_sourced(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        # Insert a posting with a very old last_seen_at
        conn.execute(
            """
            INSERT INTO postings
                (source, url, title, company, location, status, dedup_key,
                 first_seen_at, last_seen_at)
            VALUES
                ('greenhouse/old', 'https://old.com/job', 'Old Job', 'Old Co',
                 'Remote', 'sourced', 'old-dedup',
                 '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
            """
        )
        conn.commit()

        expired = expire_stale_postings(conn, expiry_days=21)
        assert expired == 1

        row = conn.execute(
            "SELECT status FROM postings WHERE dedup_key = 'old-dedup'"
        ).fetchone()
        assert row["status"] == "expired"
    finally:
        conn.close()


def test_expire_stale_postings_does_not_expire_recently_seen(db_path: Path) -> None:
    from datetime import datetime, timezone

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO postings
                (source, url, title, company, location, status, dedup_key,
                 first_seen_at, last_seen_at)
            VALUES
                ('greenhouse/new', 'https://new.com/job', 'New Job', 'New Co',
                 'Remote', 'sourced', 'new-dedup', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

        expired = expire_stale_postings(conn, expiry_days=21)
        assert expired == 0

        row = conn.execute(
            "SELECT status FROM postings WHERE dedup_key = 'new-dedup'"
        ).fetchone()
        assert row["status"] == "sourced"
    finally:
        conn.close()


def test_expire_stale_postings_skips_dismissed_promoted(db_path: Path) -> None:
    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        for status in ("dismissed", "promoted"):
            conn.execute(
                """
                INSERT INTO postings
                    (source, url, title, company, location, status, dedup_key,
                     first_seen_at, last_seen_at)
                VALUES
                    ('greenhouse/x', 'https://x.com/job', 'Job', 'X', 'Remote',
                     ?, ?, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
                """,
                (status, f"{status}-dedup"),
            )
        conn.commit()

        expired = expire_stale_postings(conn, expiry_days=21)
        assert expired == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sourcing_run record lifecycle
# ---------------------------------------------------------------------------


def test_run_crawl_creates_and_finishes_sourcing_run(db_path: Path) -> None:
    role = _make_role()
    sources = [{"type": "greenhouse", "slug": "testco"}]
    factory = _adapter_factory_for({"greenhouse/testco": [role]})

    summary = run_crawl(db_path, sources, adapter_factory=factory)

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM sourcing_runs WHERE run_id = ?", (summary["run_id"],)
        ).fetchone()
        assert row is not None
        assert row["status"] in ("done", "degraded")
        assert row["finished_at"] is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# source_filter
# ---------------------------------------------------------------------------


def test_run_crawl_source_filter_limits_to_one_source(db_path: Path) -> None:
    role_a = _make_role(slug="a", company="A", url="https://a.com/job/1")
    role_b = _make_role(slug="b", company="B", url="https://b.com/job/1")
    sources = [
        {"type": "greenhouse", "slug": "a"},
        {"type": "greenhouse", "slug": "b"},
    ]
    factory = _adapter_factory_for({
        "greenhouse/a": [role_a],
        "greenhouse/b": [role_b],
    })

    summary = run_crawl(
        db_path, sources, adapter_factory=factory, source_filter="greenhouse/a"
    )

    assert "greenhouse/a" in summary["sources_checked"]
    assert "greenhouse/b" not in summary["sources_checked"]
    assert summary["roles_fetched"] == 1


# ---------------------------------------------------------------------------
# Finding 1: Circuit breaker trips on a SINGLE adapter failure
# ---------------------------------------------------------------------------


def test_single_adapter_failure_marks_source_degraded(db_path: Path) -> None:
    """A source that fails once in a run is immediately marked degraded (finding 1)."""
    from jobsmith.sourcing.adapters.base import ATSSourceAdapter, SourceFetchError

    class _CrashAdapter(ATSSourceAdapter):
        name = "crash"

        def fetch(self, slug: str):
            raise SourceFetchError("boom")

    def crashing_factory(spec):
        return _CrashAdapter()

    sources = [{"type": "greenhouse", "slug": "crashco"}]
    summary = run_crawl(db_path, sources, adapter_factory=crashing_factory)

    assert "greenhouse/crashco" in summary["degraded_sources"]


def test_single_adapter_failure_triggers_run_record_degraded(db_path: Path) -> None:
    """When a source fails, the sourcing_run record status is 'degraded' (finding 1)."""
    from jobsmith.sourcing.adapters.base import ATSSourceAdapter, SourceFetchError

    class _CrashAdapter(ATSSourceAdapter):
        name = "crash"

        def fetch(self, slug: str):
            raise SourceFetchError("boom")

    def crashing_factory(spec):
        return _CrashAdapter()

    sources = [{"type": "greenhouse", "slug": "crashco"}]
    summary = run_crawl(db_path, sources, adapter_factory=crashing_factory)

    conn = jobsmith_db.open_pipeline_db(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM sourcing_runs WHERE run_id = ?", (summary["run_id"],)
        ).fetchone()
        assert row is not None
        assert row["status"] == "degraded"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Finding 3: Email postings get LLM-rescored
# ---------------------------------------------------------------------------


def test_email_new_postings_included_in_rescore(db_path: Path) -> None:
    """Email-sourced new postings are passed to the LLM rescore pass (finding 3)."""

    def _fake_query_fn(prompt: str) -> str:
        return '{"score_a": 70, "score_b": 65, "dominant_specialty": "data", "rationale": "ok"}'

    from jobsmith import db as jobsmith_db_mod

    # Pre-seed the DB so upsert_posting returns a real ID
    conn = jobsmith_db_mod.open_pipeline_db(db_path)
    conn.close()

    def _mock_email_alerts(conn, senders, *, dry_run=False, max_per_sender=20):
        """Return a fake newly-inserted posting so new_posting_ids get a real DB id."""
        from jobsmith.sourcing.runner import run_email_alerts

        def _mailapp_ingest(senders, *, max_per_sender=20):
            return [
                {
                    "source": "mailapp/linkedin-alert",
                    "title": "Data Engineer",
                    "company": "EmailCo",
                    "location": "Remote",
                    "url": "https://www.linkedin.com/jobs/view/9900001/",
                    "external_id": "9900001",
                }
            ], []

        return run_email_alerts(
            conn,
            senders,
            dry_run=dry_run,
            max_per_sender=max_per_sender,
            _mailapp_ingest_fn=_mailapp_ingest,
        )

    from unittest.mock import patch

    rescore_call_ids: list = []

    def _fake_rescore(conn, posting_ids, **kwargs):
        rescore_call_ids.extend(posting_ids)
        return []

    with patch("jobsmith.sourcing.llm_rescore.rescore_postings", side_effect=_fake_rescore):
        run_crawl(
            db_path=db_path,
            sources=[],
            alert_senders=[
                {
                    "type": "mailapp_alert",
                    "sender_slug": "linkedin-alert",
                    "account": "me@example.com",
                    "mailbox": "Job Alerts",
                }
            ],
            no_llm=False,
            _run_email_alerts_fn=_mock_email_alerts,
            _query_fn=_fake_query_fn,
        )

    # The email-sourced posting ID should have been passed to rescore
    assert len(rescore_call_ids) >= 1, "email postings were not included in LLM rescore"


# ---------------------------------------------------------------------------
# Finding 5: AppleScript injection escaping
# ---------------------------------------------------------------------------


def test_notify_failure_escapes_double_quotes() -> None:
    """_notify_failure escapes double quotes and backslashes in messages (finding 5)."""
    import platform
    import subprocess as real_subprocess
    from unittest.mock import MagicMock, patch

    from jobsmith.sourcing.runner import _notify_failure

    if platform.system() != "Darwin":
        # On non-macOS the function returns early; just verify it doesn't raise
        _notify_failure('Error: adapter "indeed" failed with \\path')
        return

    captured_scripts: list[str] = []

    def _fake_run(cmd, **kwargs):
        if isinstance(cmd, list):
            captured_scripts.extend(cmd)
        return MagicMock(returncode=0)

    # _notify_failure does `import subprocess as _sp` locally, so we patch
    # the subprocess module's run attribute directly.
    with patch.object(real_subprocess, "run", side_effect=_fake_run):
        _notify_failure('Error: adapter "indeed" failed with \\path\\issue')

    # Find the osascript script argument (the -e value)
    script_arg = ""
    for i, arg in enumerate(captured_scripts):
        if arg == "-e" and i + 1 < len(captured_scripts):
            script_arg = captured_scripts[i + 1]
            break

    if script_arg:
        # The message segment between 'display notification "' and '" with title'
        # should use escaped quotes, not raw "
        msg_start = 'display notification "'
        assert msg_start in script_arg
        after_open = script_arg.split(msg_start, 1)[1]
        # The message should be escaped: \" for quotes, \\ for backslashes
        assert '\\"' in after_open, f"double quotes not escaped in script: {script_arg}"
        assert '\\\\' in after_open, f"backslashes not escaped in script: {script_arg}"
