# scripts/

Reserved for one-off Python utilities that don't belong in the `jobsmith` package.

The core fact-checking, anchor-guard, and config-scaffolding logic lives in `src/jobsmith/` and is exposed via the `jobsmith` CLI:

| Old script | Replaced by |
|---|---|
| `scripts/anchor_bullet_guard.py` | `jobsmith anchor-check` (function: `jobsmith.guard.check_anchors`) |
| `scripts/fact_check_draft.py` | `jobsmith fact-check` (function: `jobsmith.factcheck.check_draft`) |
| `scripts/jobsmith_init.py` | `jobsmith init` (function: `jobsmith.cli.init`) |

## What lives here now

Future planned scripts (per ROADMAP 0.2):

- `scripts/corpus_backfill.py` — slice 0 of plan-bf34f540 (port the reuse-detector plan from shakestzd to jobsmith). Backfills `.apply-state/jd-parsed.json` for prior applications.
- `scripts/migrations/` — SQLite migrations for the job-search DB. e.g., `001_add_last_synced_at.sql`.

## Why keep `scripts/` at all?

For tools that:
- Are user-customizable starting points (not framework-canonical)
- Run rarely enough that they don't need a stable CLI surface
- Are personal corpus migrations / one-time analytics

Anything reusable across users belongs in `src/jobsmith/`, exposed via the CLI.
