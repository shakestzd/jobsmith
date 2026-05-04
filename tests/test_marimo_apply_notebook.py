"""Unit tests for the marimo apply notebook's helper functions.

The notebook itself is exercised end-to-end by
``test_marimo_loader.py::test_notebook_imports_cleanly`` (which compiles
every cell). This module covers the pure-Python helpers factored out for
direct testability.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobsmith.marimo.apply import _cli_flag_to_bool, _read_distinct_slugs

# ---------------------------------------------------------------------------
# _cli_flag_to_bool — roborev #928 MEDIUM 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,default,expected",
    [
        # Bare flag (mo.cli_args returns "" for `--force`) → truthy.
        # The pre-fix code did ``bool("")`` → False, silently swallowing
        # the user's intent. This is the regression case.
        ("", False, True),
        # Explicit truthy values
        ("true", False, True),
        ("TRUE", False, True),
        ("yes", False, True),
        ("1", False, True),
        ("on", False, True),
        # Explicit falsy values — must override the bare-flag default
        ("false", True, False),
        ("FALSE", True, False),
        ("no", True, False),
        ("0", True, False),
        ("off", True, False),
        # Native bool / int passthrough
        (True, False, True),
        (False, True, False),
        (1, False, True),
        (0, True, False),
        # Missing → default
        (None, True, True),
        (None, False, False),
    ],
)
def test_cli_flag_to_bool(value, default, expected) -> None:
    """Bare ``--force`` (parses to "") must be truthy; explicit "false" must be falsy."""
    assert _cli_flag_to_bool(value, default=default) is expected


# ---------------------------------------------------------------------------
# _read_distinct_slugs — roborev #928 MEDIUM 2
# ---------------------------------------------------------------------------


def test_read_distinct_slugs_returns_empty_when_db_file_missing(tmp_path: Path) -> None:
    """Fresh project: no jobsmith.db yet. Must return [], not crash.

    Pre-fix the cell did ``sqlite3.connect(str(db_path))`` unconditionally,
    which DOES NOT raise on a missing file (sqlite creates it). But then
    ``SELECT FROM apply_runs`` raises ``OperationalError: no such table``.
    Either way the cell graph crashed before ``_script_mode_runner`` could
    fire (roborev #928 MEDIUM 2).
    """
    assert _read_distinct_slugs(tmp_path / "does-not-exist.db") == []


def test_read_distinct_slugs_returns_empty_when_apply_runs_table_missing(
    tmp_path: Path,
) -> None:
    """File exists but apply_runs table doesn't (e.g. an older schema)."""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()  # empty DB — no tables
    assert _read_distinct_slugs(db_path) == []


def test_read_distinct_slugs_returns_distinct_sorted_slugs(tmp_path: Path) -> None:
    """Sanity: when apply_runs has rows, slugs come back distinct + sorted."""
    db_path = tmp_path / "with-runs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE apply_runs (
            run_id TEXT PRIMARY KEY,
            slug TEXT,
            phase TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT
        )
    """)
    rows = [
        ("r1", "duke-engineer", "gather", "2024", "2024", "done"),
        ("r2", "duke-engineer", "gather", "2024", "2024", "done"),  # duplicate slug
        ("r3", "acme-engineer", "gather", "2024", "2024", "done"),
    ]
    conn.executemany("INSERT INTO apply_runs VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    assert _read_distinct_slugs(db_path) == ["acme-engineer", "duke-engineer"]
