"""jobsmith.reuse._cli_reuse — `jobsmith reuse` CLI command group.

Subcommands
-----------
lookup-bullet
    For a given raw requirement text and application slug, check whether
    a prior (fresh) bullet mapping exists in the evidence map.

    Exits 0 in all non-error cases and prints JSON to stdout:

    Reuse hit::

        {"master_bullet_id": "<id>", "reused": true}

    No mapping (or stale hash)::

        {"master_bullet_id": null, "reused": false}

    The prompt ``apply-bullet-selector.md`` calls this command at the start
    of every bullet-selection run to skip requirements already resolved.

JSON contract for ``lookup-bullet``
------------------------------------
- Exit code: always 0 (output signals the result; callers check ``reused``).
- ``master_bullet_id``: 12-char SHA-1 hex bullet ID, or ``null``.
- ``reused``: boolean.

Internal helpers (patchable in tests)
---------------------------------------
``_resolve_db_conn(slug, cwd)``
    Open the pipeline DB for the repo containing *slug*.
``_load_current_bullet_texts(cwd)``
    Load ``{bullet_id: text}`` from master work YAML via the DB or FS.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

reuse_app = typer.Typer(
    name="reuse",
    help="Reuse-layer CLI — query evidence maps, bullet mappings, and cache state.",
    no_args_is_help=True,
    add_completion=False,
)


# ---------------------------------------------------------------------------
# Internal helpers — patchable in tests
# ---------------------------------------------------------------------------


def _resolve_db_conn(slug: str, cwd: Path) -> sqlite3.Connection | None:
    """Open the pipeline DB for the repo that contains *slug*.

    Returns None when the config or DB cannot be resolved (degrade gracefully).
    """
    try:
        from jobsmith.config import find_config, load_config
        from jobsmith.db import open_pipeline_db
        from jobsmith.paths import resolve

        config_path = find_config(cwd)
        if config_path is None:
            return None
        config = load_config(config_path)
        db_path = resolve(config.output.jobsmith_db, config_path.parent)
        return open_pipeline_db(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("_resolve_db_conn failed: %s", exc)
        return None


def _load_current_bullet_texts(cwd: Path) -> dict[str, str]:
    """Return ``{master_bullet_id: text}`` for all bullets in master work YAML.

    Delegates to ``guard.parse_master_bullets`` so the bullet_id computation
    (SHA-1 hex first 12 chars of bullet text, via ``guard._bullet_id``) and
    YAML parsing (list-of-positions with ``details`` entries) exactly match
    what the anchor guard uses.  Falls back to an empty dict on any error
    (degrade to regenerate — never error the lookup path).
    """
    try:
        from jobsmith.config import find_config, load_config
        from jobsmith.guard import parse_master_bullets
        from jobsmith.paths import resolve

        config_path = find_config(cwd)
        if config_path is None:
            return {}
        config = load_config(config_path)
        repo_root = config_path.parent
        work_path = resolve(config.master.work_yml, repo_root)
        if not work_path.exists():
            return {}

        bullets = parse_master_bullets(work_path)
        return {b.bullet_id: b.text for b in bullets}
    except Exception as exc:  # noqa: BLE001
        logger.warning("_load_current_bullet_texts failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# lookup-bullet subcommand
# ---------------------------------------------------------------------------


@reuse_app.command("lookup-bullet")
def lookup_bullet(
    requirement_raw: str = typer.Option(
        ...,
        "--requirement-raw",
        help="Raw requirement text to look up (e.g. '5+ years Python')",
    ),
    slug: str = typer.Option(
        ...,
        "--slug",
        help="Application slug (used to resolve the repo DB)",
    ),
    cwd: Path = typer.Option(
        None,
        "--cwd",
        help="Working directory (default: current directory)",
        exists=False,
    ),
) -> None:
    """Look up whether a prior bullet mapping exists for REQUIREMENT_RAW.

    Prints JSON to stdout:

    \b
    Reuse hit:     {"master_bullet_id": "<id>", "reused": true}
    No mapping:    {"master_bullet_id": null, "reused": false}

    Exit code is always 0 (the JSON payload signals the result).
    """
    resolved_cwd = cwd or Path.cwd()

    conn = _resolve_db_conn(slug, resolved_cwd)
    if conn is None:
        _print_no_reuse()
        return

    try:
        current_bullet_texts = _load_current_bullet_texts(resolved_cwd)
        bullet_id = _do_lookup(conn, requirement_raw, current_bullet_texts)
    except Exception as exc:  # noqa: BLE001 — degrade to no-reuse, never error
        logger.warning("lookup-bullet: lookup failed — returning no-reuse: %s", exc)
        bullet_id = None
    finally:
        import contextlib
        with contextlib.suppress(Exception):
            conn.close()

    if bullet_id is not None:
        typer.echo(json.dumps({"master_bullet_id": bullet_id, "reused": True}))
    else:
        _print_no_reuse()


def _print_no_reuse() -> None:
    typer.echo(json.dumps({"master_bullet_id": None, "reused": False}))


def _do_lookup(
    conn: sqlite3.Connection,
    requirement_raw: str,
    current_bullet_texts: dict[str, str],
) -> str | None:
    """Core lookup logic: find match via canonical requirements, return bullet_id."""
    from jobsmith.reuse.evidence_map import lookup_mapped_bullet
    from jobsmith.reuse.match import match

    # First try a direct match to get the requirement hash
    match_result = match(requirement_raw, conn)

    if match_result.decision != "reuse" or match_result.matched_hash is None:
        # No canonical requirement found → no mapping possible
        return None

    return lookup_mapped_bullet(
        conn,
        requirement_hash=match_result.matched_hash,
        current_bullet_texts=current_bullet_texts,
    )


__all__ = [
    "reuse_app",
    "_resolve_db_conn",
    "_load_current_bullet_texts",
]
