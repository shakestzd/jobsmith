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
import os
import sqlite3
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

# Env guard: when set to "1" (by an apply running with --no-reuse), lookup-bullet
# returns no-reuse unconditionally so prompt-side reuse cannot alter selection.
_NO_REUSE_ENV = "JOBSMITH_NO_REUSE"

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
    Reuse hit:     {"master_bullet_id": "<id>", "reused": true,  "matched_hash": "<reqhash>"}
    No mapping:    {"master_bullet_id": null,   "reused": false, "matched_hash": "<reqhash>"}

    ``matched_hash`` is the canonical requirement hash (the
    ``requirement_content_hash`` contract).  It is ALWAYS present — even for a
    fresh selection with no prior mapping — so the selector can write it back
    as ``matched_requirement_hash`` and seed ``requirement_evidence_map`` for
    future reuse.  It is ``null`` only when the requirement cannot be hashed.

    Exit code is always 0 (the JSON payload signals the result).
    """
    resolved_cwd = cwd or Path.cwd()

    # The canonical requirement hash is independent of any DB/mapping state —
    # compute it up front so it is returned even on the no-reuse / no-DB paths.
    req_hash = _requirement_hash_safe(requirement_raw)

    # --no-reuse guard (finding: prompt-side reuse must also be disabled).  When
    # the apply runs with --no-reuse the wrapper sets JOBSMITH_NO_REUSE=1; honor
    # it here so an existing evidence-map row can never alter selection even if
    # the (static) selector prompt still calls lookup-bullet.
    if os.environ.get(_NO_REUSE_ENV) == "1":
        _print_no_reuse(req_hash)
        return

    conn = _resolve_db_conn(slug, resolved_cwd)
    if conn is None:
        _print_no_reuse(req_hash)
        return

    try:
        current_bullet_texts = _load_current_bullet_texts(resolved_cwd)
        bullet_id, matched_canonical = _do_lookup(
            conn, requirement_raw, current_bullet_texts
        )
    except Exception as exc:  # noqa: BLE001 — degrade to no-reuse, never error
        logger.warning("lookup-bullet: lookup failed — returning no-reuse: %s", exc)
        bullet_id, matched_canonical = None, None
    finally:
        import contextlib
        with contextlib.suppress(Exception):
            conn.close()

    # Prefer the canonical hash match() resolved (synonym/fuzzy-aware) so the
    # value we emit matches the row keyed in canonical_requirements /
    # requirement_evidence_map.  Fall back to the locally-computed hash only
    # when no canonical requirement matched at all (a brand-new requirement).
    effective_hash = matched_canonical or req_hash

    if bullet_id is not None:
        typer.echo(json.dumps({
            "master_bullet_id": bullet_id,
            "reused": True,
            "matched_hash": effective_hash,
        }))
    else:
        _print_no_reuse(effective_hash)


def _requirement_hash_safe(requirement_raw: str) -> str | None:
    """Canonical requirement hash for *requirement_raw*, or None on error.

    Uses the single ``requirement_content_hash`` contract so the value matches
    ``canonical_requirements.content_hash`` and
    ``requirement_evidence_map.requirement_hash``.
    """
    try:
        from jobsmith.reuse.canonicalize import (
            canonicalize,
            requirement_content_hash,
        )

        tag, normalized = canonicalize(requirement_raw)
        return requirement_content_hash(
            {"canonical_tag": tag, "normalized_phrase": normalized}
        )
    except Exception as exc:  # noqa: BLE001 — never error the lookup on hashing
        logger.warning("lookup-bullet: could not hash requirement: %s", exc)
        return None


def _print_no_reuse(matched_hash: str | None = None) -> None:
    typer.echo(json.dumps({
        "master_bullet_id": None,
        "reused": False,
        "matched_hash": matched_hash,
    }))


def _do_lookup(
    conn: sqlite3.Connection,
    requirement_raw: str,
    current_bullet_texts: dict[str, str],
) -> tuple[str | None, str | None]:
    """Core lookup logic.

    Returns ``(bullet_id, matched_canonical_hash)``:

    - ``matched_canonical_hash`` is the hash ``match()`` resolved to when a
      canonical requirement matched (synonym/fuzzy-aware) — the hash actually
      keyed in ``requirement_evidence_map`` — or ``None`` when no canonical
      requirement matched (caller falls back to the locally-computed hash).
    - ``bullet_id`` is the mapped master bullet id, or ``None`` when there is
      no canonical match OR no fresh mapped bullet for the matched hash.
    """
    from jobsmith.reuse.evidence_map import lookup_mapped_bullet
    from jobsmith.reuse.match import match

    # First try a direct match to get the canonical requirement hash.
    match_result = match(requirement_raw, conn)

    if match_result.decision != "reuse" or match_result.matched_hash is None:
        # No canonical requirement found → no mapping possible.
        return None, None

    bullet_id = lookup_mapped_bullet(
        conn,
        requirement_hash=match_result.matched_hash,
        current_bullet_texts=current_bullet_texts,
    )
    return bullet_id, match_result.matched_hash


# ---------------------------------------------------------------------------
# reuse backfill subcommand (feat-60d8bef1)
# ---------------------------------------------------------------------------


@reuse_app.command("backfill")
def reuse_backfill(
    slug: str | None = typer.Option(
        None,
        "--slug",
        help="Backfill a single application slug (directory name under applications_dir).",
    ),
    all_slugs: bool = typer.Option(
        False,
        "--all",
        help="Backfill every eligible slug under applications_dir.",
    ),
) -> None:
    """Backfill the reuse store from existing applications.

    Populates application_fingerprints, run_metrics, canonical_requirements,
    and requirement_evidence_map from the .apply-state/ artifacts that were
    produced by previous ``jobsmith apply`` runs.  Safe to re-run — all writes
    use INSERT OR IGNORE on their natural unique keys so the operation is
    idempotent.

    \\b
      jobsmith reuse backfill               # backfill every eligible slug
      jobsmith reuse backfill --all         # same as above (explicit)
      jobsmith reuse backfill --slug X      # backfill a single slug
    """
    if slug and all_slugs:
        typer.echo("ERROR: pass either --slug or --all, not both", err=True)
        raise typer.Exit(code=1)

    try:
        from jobsmith.config import find_config, load_config
        from jobsmith.db import open_pipeline_db
        from jobsmith.paths import repo_root_for, resolve
        from jobsmith.reuse.backfill import backfill_all_reuse, backfill_slug_reuse
    except ImportError as exc:
        typer.echo(f"ERROR: could not import required module: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    cwd = Path.cwd()
    config_path = find_config(cwd)
    if config_path is None:
        typer.echo("ERROR: No .apply-config.yaml found — run `jobsmith init` first.", err=True)
        raise typer.Exit(code=2)

    config = load_config(config_path)
    repo_root = repo_root_for()
    applications_dir = resolve(config.output.applications_dir, repo_root)
    db_path = (repo_root / config.output.jobsmith_db).resolve()

    if not db_path.exists():
        typer.echo(f"ERROR: Pipeline DB not found at {db_path}.", err=True)
        raise typer.Exit(code=2)

    conn = open_pipeline_db(db_path)
    try:
        if slug:
            inserted = backfill_slug_reuse(conn, slug, applications_dir)
            typer.echo(f"backfilled {slug}: {inserted} row(s) inserted")
        else:
            # Both bare invocation and --all iterate all eligible slugs
            results = backfill_all_reuse(conn, applications_dir)
            if not results:
                typer.echo("No eligible slugs found under applications_dir.")
                return
            total = sum(results.values())
            typer.echo(
                f"backfilled {len(results)} slug(s), {total} row(s) inserted"
            )
            for s, n in results.items():
                typer.echo(f"  {s}: {n}")
    finally:
        conn.close()


__all__ = [
    "reuse_app",
    "_resolve_db_conn",
    "_load_current_bullet_texts",
]
