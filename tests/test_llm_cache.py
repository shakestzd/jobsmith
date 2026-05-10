"""Tests for the SQLite LLM response cache (feat-ff4ccde2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.llm.sqlite_cache import (
    cache_key,
    cache_stats,
    get_cached_phase,
    invalidate_all,
    jd_hash,
    master_composite_etag,
    put_cached_phase,
)


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "jobsmith.db"
    c = jobsmith_db.open_pipeline_db(db_path)
    yield c
    c.close()


def _seed_master(conn, sections: list[tuple[str, str]]) -> None:
    for section, blob in sections:
        conn.execute(
            "INSERT INTO master_content (section, content_blob, etag) VALUES (?, ?, ?)",
            (section, blob, "etag-" + section),
        )
    conn.commit()


def test_llm_cache_table_exists(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_cache'"
    ).fetchall()
    assert len(rows) == 1


def test_jd_hash_is_stable_and_strips_whitespace():
    assert jd_hash("hello world") == jd_hash("  hello world  ")
    assert jd_hash("hello") != jd_hash("world")


def test_master_composite_etag_changes_with_content(conn):
    empty = master_composite_etag(conn)
    _seed_master(conn, [("work", "alpha")])
    seeded = master_composite_etag(conn)
    assert empty != seeded
    conn.execute("UPDATE master_content SET content_blob = ? WHERE section = ?", ("beta", "work"))
    conn.commit()
    assert seeded != master_composite_etag(conn)


def test_cache_key_is_deterministic():
    a = cache_key("apply-jd-parser", "abc", "etag")
    b = cache_key("apply-jd-parser", "abc", "etag")
    c = cache_key("apply-jd-parser", "abc", "different")
    assert a == b
    assert a != c


def test_get_cached_phase_returns_none_on_miss(conn):
    assert get_cached_phase(conn, ["apply-jd-parser"], "jdh", "metag") is None


def test_put_then_get_returns_outputs(conn):
    outputs = {"apply-jd-parser": {"company": "Acme"}}
    put_cached_phase(conn, outputs, "jdh", "metag", "claude")
    hit = get_cached_phase(conn, ["apply-jd-parser"], "jdh", "metag")
    assert hit is not None
    assert hit["apply-jd-parser"] == {"company": "Acme"}


def test_get_increments_hit_count(conn):
    put_cached_phase(conn, {"sp": {"x": 1}}, "jdh", "metag", "claude")
    before = conn.execute("SELECT hit_count FROM llm_cache").fetchone()[0]
    get_cached_phase(conn, ["sp"], "jdh", "metag")
    after = conn.execute("SELECT hit_count FROM llm_cache").fetchone()[0]
    assert after == before + 1


def test_partial_hit_returns_none(conn):
    put_cached_phase(conn, {"a": {"v": 1}}, "jdh", "metag", "claude")
    assert get_cached_phase(conn, ["a", "b"], "jdh", "metag") is None


def test_upsert_preserves_hit_count(conn):
    put_cached_phase(conn, {"a": {"v": 1}}, "jdh", "metag", "claude")
    get_cached_phase(conn, ["a"], "jdh", "metag")  # hit_count = 1
    put_cached_phase(conn, {"a": {"v": 2}}, "jdh", "metag", "claude-new")
    row = conn.execute(
        "SELECT output_json, model, hit_count FROM llm_cache WHERE specialist = ?",
        ("a",),
    ).fetchone()
    assert json.loads(row[0]) == {"v": 2}
    assert row[1] == "claude-new"
    assert row[2] == 1  # preserved


def test_cache_stats_aggregates(conn):
    assert cache_stats(conn) == {"total_entries": 0, "total_hits": 0}
    put_cached_phase(conn, {"a": {}, "b": {}}, "jdh", "metag", "m")
    get_cached_phase(conn, ["a", "b"], "jdh", "metag")
    stats = cache_stats(conn)
    assert stats["total_entries"] == 2
    assert stats["total_hits"] == 2


def test_invalidate_all_drops_rows(conn):
    put_cached_phase(conn, {"a": {}, "b": {}}, "jdh", "metag", "m")
    n = invalidate_all(conn)
    assert n == 2
    assert cache_stats(conn) == {"total_entries": 0, "total_hits": 0}
