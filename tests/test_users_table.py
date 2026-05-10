"""Tests for the users/user_sessions migration and upsert_or_load_user dep.

Covers feat-ddd98f7d:
- Migration 007 creates users + user_sessions tables.
- upsert_or_load_user inserts on a fresh DB.
- upsert_or_load_user is idempotent on duplicate insert.
- upsert_or_load_user updates name when it drifts.
- upsert_or_load_user handles None config without raising.
- upsert_or_load_user handles missing email/name gracefully.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jobsmith import db as jobsmith_db
from jobsmith.api.deps import upsert_or_load_user


def _make_config(email: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(email=email, name=name))


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "jobsmith.db"
    c = jobsmith_db.open_pipeline_db(db_path)
    yield c
    c.close()


def test_users_table_exists(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'user_sessions')"
    ).fetchall()
    assert len(rows) == 2


def test_upsert_inserts_on_fresh_db(conn):
    cfg = _make_config("alice@example.com", "Alice")
    row = upsert_or_load_user(conn, cfg)
    assert row is not None
    assert row["email"] == "alice@example.com"
    assert row["name"] == "Alice"
    assert row["user_id"]  # auto-generated
    assert row["created_at"]


def test_upsert_idempotent_on_duplicate(conn):
    cfg = _make_config("bob@example.com", "Bob")
    first = upsert_or_load_user(conn, cfg)
    second = upsert_or_load_user(conn, cfg)
    assert first["user_id"] == second["user_id"]
    count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE email = ?", ("bob@example.com",)
    ).fetchone()[0]
    assert count == 1


def test_upsert_updates_name_when_drifts(conn):
    upsert_or_load_user(conn, _make_config("carol@example.com", "Carol"))
    updated = upsert_or_load_user(conn, _make_config("carol@example.com", "Carol Smith"))
    assert updated["name"] == "Carol Smith"


def test_upsert_returns_none_when_config_is_none(conn):
    assert upsert_or_load_user(conn, None) is None


def test_upsert_returns_none_when_email_missing(conn):
    cfg = _make_config("", "Dan")
    assert upsert_or_load_user(conn, cfg) is None


def test_upsert_returns_none_when_name_missing(conn):
    cfg = _make_config("eve@example.com", "")
    assert upsert_or_load_user(conn, cfg) is None
