"""Tests for db_migrate_slugs.normalize_existing_slugs."""

from __future__ import annotations

import sqlite3

import pytest

from jobsmith.db import open_pipeline_db
from jobsmith.db_migrate_slugs import find_malformed_slugs, normalize_existing_slugs


def _seed_run(conn: sqlite3.Connection, run_id: str, slug: str) -> None:
    conn.execute(
        "INSERT INTO apply_runs (run_id, slug, phase, started_at, finished_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, slug, "unknown", None, None, "backfilled"),
    )
    conn.commit()


def test_finds_malformed_slugs(tmp_path):
    conn = open_pipeline_db(tmp_path / "p.db")
    _seed_run(conn, "r1", "12345")  # numeric-only
    _seed_run(conn, "r2", "engineer")  # single-word
    _seed_run(conn, "r3", "linear-linear-product-engineer")  # duplicated leading
    _seed_run(conn, "r4", "anthropic-applied-ai-2026-04")  # clean

    malformed = find_malformed_slugs(conn)

    assert "12345" in malformed
    assert "engineer" in malformed
    assert "linear-linear-product-engineer" in malformed
    assert "anthropic-applied-ai-2026-04" not in malformed


def test_normalizes_in_place(tmp_path):
    conn = open_pipeline_db(tmp_path / "p.db")
    _seed_run(conn, "r1", "linear-linear-product-engineer")
    _seed_run(conn, "r2", "anthropic-applied-ai-2026-04")

    rewritten = normalize_existing_slugs(conn)

    assert rewritten == {
        "linear-linear-product-engineer": "linear-product-engineer",
    }
    rows = conn.execute("SELECT run_id, slug FROM apply_runs ORDER BY run_id").fetchall()
    assert rows[0]["slug"] == "linear-product-engineer"
    assert rows[1]["slug"] == "anthropic-applied-ai-2026-04"


def test_is_idempotent(tmp_path):
    conn = open_pipeline_db(tmp_path / "p.db")
    _seed_run(conn, "r1", "linear-linear-product-engineer")

    first = normalize_existing_slugs(conn)
    second = normalize_existing_slugs(conn)

    assert first == {"linear-linear-product-engineer": "linear-product-engineer"}
    assert second == {}


def test_empty_db(tmp_path):
    conn = open_pipeline_db(tmp_path / "p.db")
    rewritten = normalize_existing_slugs(conn)
    assert rewritten == {}


def test_collision_skips_rewrite(tmp_path):
    """A clean slug that already exists with the target name should not be overwritten."""
    conn = open_pipeline_db(tmp_path / "p.db")
    # Already-clean canonical slug.
    _seed_run(conn, "r1", "linear-product-engineer")
    # Malformed sibling that would normalize to the same target.
    _seed_run(conn, "r2", "linear-linear-product-engineer")

    rewritten = normalize_existing_slugs(conn)

    # The malformed row stays as-is to avoid collapsing two distinct runs.
    assert "linear-linear-product-engineer" not in rewritten
    rows = {
        row["run_id"]: row["slug"]
        for row in conn.execute("SELECT run_id, slug FROM apply_runs").fetchall()
    }
    assert rows["r1"] == "linear-product-engineer"
    assert rows["r2"] == "linear-linear-product-engineer"


def test_sibling_collision_does_not_collapse_runs(tmp_path):
    """Two malformed slugs that normalize to the same target must not collide.

    Both rows are runs of separate applications. The migration must rewrite at
    most one of them and leave the other untouched, never collapse them onto
    one slug.
    """
    conn = open_pipeline_db(tmp_path / "p.db")
    # Both normalize to "linear-product-engineer".
    _seed_run(conn, "r1", "linear-linear-product-engineer")
    _seed_run(conn, "r2", "linear-linear-linear-product-engineer")

    rewritten = normalize_existing_slugs(conn)

    slugs = {
        row["run_id"]: row["slug"]
        for row in conn.execute("SELECT run_id, slug FROM apply_runs").fetchall()
    }
    # Exactly one rewrite happened, and the two run_ids still have distinct slugs.
    assert len(rewritten) == 1
    assert slugs["r1"] != slugs["r2"]


def test_specialist_outputs_remain_consistent(tmp_path):
    """run_id-keyed FK rows are unaffected by slug rewrites."""
    conn = open_pipeline_db(tmp_path / "p.db")
    _seed_run(conn, "r1", "linear-linear-product-engineer")
    conn.execute(
        "INSERT INTO specialist_outputs "
        "(run_id, specialist, kind, output_json, finished_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("r1", "apply-jd-parser", "jd_parsed", "{}", None),
    )
    conn.commit()

    normalize_existing_slugs(conn)

    # The specialist_outputs row still references r1; the slug change is
    # transparent because the FK is on run_id.
    rows = conn.execute("SELECT run_id FROM specialist_outputs").fetchall()
    assert rows[0]["run_id"] == "r1"
    new_slug = conn.execute(
        "SELECT slug FROM apply_runs WHERE run_id = 'r1'"
    ).fetchone()[0]
    assert new_slug == "linear-product-engineer"
