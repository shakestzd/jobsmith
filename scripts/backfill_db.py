"""Backfill jobsmith.db from existing .apply-state/ directories.

Iterates all slug directories under applications_dir, reads each slug's
.apply-state/ artifacts, and inserts apply_runs + specialist_outputs rows
into the pipeline DB.

Idempotent: uses deterministic UUIDv5 run_ids (UUIDv5(NAMESPACE_DNS,
"backfill:<slug>")) and INSERT OR IGNORE — running twice produces no new rows.

Usage
-----
    uv run python scripts/backfill_db.py
    uv run python scripts/backfill_db.py --applications-dir private/applications
    uv run python scripts/backfill_db.py --db private/jobsmith.db --dry-run

The script reads .apply-config.yaml from the repo root for default paths.
All CLI flags override config values.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project src is importable when run directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from jobsmith.config import load_config  # noqa: E402
from jobsmith.db import open_pipeline_db  # noqa: E402
from jobsmith.db_ingest import backfill_all, iter_backfillable_slugs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill jobsmith.db from existing .apply-state/ directories."
    )
    parser.add_argument(
        "--applications-dir",
        type=Path,
        default=None,
        help="Path to the applications directory (default: from .apply-config.yaml)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to jobsmith.db (default: from .apply-config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be done without writing to the DB.",
    )
    args = parser.parse_args(argv)

    # Load config from repo root
    config = load_config(search_from=_REPO_ROOT)

    applications_dir: Path = args.applications_dir or (
        _REPO_ROOT / config.output.applications_dir
    )
    db_path: Path = args.db or (_REPO_ROOT / config.output.jobsmith_db)

    if not applications_dir.is_dir():
        print(
            f"[backfill] ERROR: applications_dir not found: {applications_dir}",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        slugs = iter_backfillable_slugs(applications_dir)
        print(f"[backfill] DRY RUN — would process {len(slugs)} slug(s):")
        for slug in slugs:
            print(f"  - {slug}")
        return 0

    print(f"[backfill] Opening DB: {db_path}")
    conn = open_pipeline_db(db_path)

    print(f"[backfill] Scanning: {applications_dir}")
    results = backfill_all(conn, applications_dir)
    conn.close()

    total_slugs = len(results)
    total_rows = sum(results.values())
    print(f"[backfill] Processed {total_slugs} slug(s); inserted {total_rows} specialist_output row(s).")

    for slug, rows_inserted in sorted(results.items()):
        if rows_inserted > 0:
            print(f"  {slug}: {rows_inserted} row(s) inserted")
        else:
            print(f"  {slug}: already present — skipped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
