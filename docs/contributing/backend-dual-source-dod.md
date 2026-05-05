# Backend Dual-Source DoD (Definition of Done)

This document defines the **minimum bar** for a backend feature whose data is canonically held in the SQLite pipeline DB but historically (or for git/quarto reasons) also lives on the filesystem. It exists because 0.8 shipped dual-write but reads stayed DB-only and FS still leaked into the contract in 4+ places (#61, #62, #63, feat-bb81c3ce). The DoD makes that class of regression impossible to merge.

Sibling: [frontend-feature-dod.md](frontend-feature-dod.md).

## Scope

Applies to any backend feature that adds a piece of state which can live in two places — the pipeline DB (`master_content`, `apply_runs`, `specialist_outputs`, etc.) **and** a filesystem artifact (`assets/content/*.yml`, `applications/<slug>/.apply-state/`, etc.).

Does **not** apply to:

- Pure DB-only data (e.g. `feedback_events`).
- Pure FS-only data (e.g. `_quarto.yml` rendered output, generated PDFs).
- Read-only computation derived from already-canonical state.

## The contract

After this PR, the following must be true:

1. **The DB is the runtime authority.** `GET /api/*` never falls back to the filesystem. If data is missing from the DB, the endpoint returns a structured `404 {error: "missing_in_db", suggestion: "<recovery command>"}` so the gap is surfaced explicitly, not silently masked.
2. **The FS is an ingest path AND an export path.** Files on disk are uploadable / editable / git-tracked, but they are *inputs* to the DB (via `jobsmith db load-master`, `jobsmith db backfill --all`, etc.) and *outputs* from the DB (via `jobsmith master export`, the snapshot endpoint, `quarto render`, etc.). Never both at the same write.
3. **Every DB-canonical surface ships its FS-ingest path in the SAME PR, never deferred.** If you add a new master section or artifact kind, the corresponding `jobsmith db load-X` or backfill registry entry lands in the same merge.

## The four artifacts

A backend dual-source feature is not done until **all four** are present in the PR:

### 1. DB schema migration

A SQL migration under `src/jobsmith/migrations/NNN_<name>.sql` that adds the table or columns. Numbering is monotonic; never edit a shipped migration. Register it in `src/jobsmith/db.py:_PIPELINE_MIGRATIONS` (or `_REVIEW_MIGRATIONS` for the review DB).

### 2. FS → DB ingest path

A function or CLI command that reads the on-disk artifact and writes it to the DB. Must be **idempotent** — running it twice on the same input is a no-op (or a deterministic update). Must surface `--reload` semantics so users can force a re-ingest after editing the file.

Examples:
- `jobsmith db load-master [--reload]` (S1, feat-bf06bdea) for `master_content`.
- `jobsmith db backfill [--slug X | --all]` for `specialist_outputs`.
- The `ARTIFACT_READERS` registry in `src/jobsmith/_state_readers.py` for per-kind ingest.

### 3. DB → FS export path (when applicable)

A function or CLI command that regenerates the on-disk artifact from DB content. Required when the artifact is git-tracked or consumed by an external tool (quarto, browser preview).

Examples:
- `jobsmith master export [--section X | --all]` (S5, feat-484c52b5) for `master_content`.
- The snapshot endpoint (shipped in 0.8) for `.apply-state/` quarto inputs.

### 4. Tests

- **DB-only read test:** `GET /api/<resource>` returns `404 {error: "missing_in_db", suggestion: ...}` when the DB row is absent. The fixture must NOT seed the on-disk file (or must seed it but verify the response stays 404 — proving FS fallback is gone).
- **FS-only state warning test:** When `.apply-state/<slug>/` exists on disk but no `apply_runs` row, server startup logs a single WARNING with the recovery command. Test reads `caplog`.
- **Round-trip test:** Edit via API → export to FS → re-ingest from FS → DB state is identical (modulo timestamp / etag). Catches lossy transforms.

## First-run UX (server-level)

`jobsmith api serve` startup must:

1. Detect FS-only state (e.g. `applications_dir` has slug subdirs but `apply_runs` is empty for them) and log a single clear WARNING with the exact recovery commands. Don't flood — one line per startup, not per slug.
2. Honor `JOBSMITH_AUTO_BACKFILL=1` to run the backfill automatically when FS-only state is detected. This is a config flag, not the default, because auto-mutation on startup should be opt-in.

## Anti-patterns

- ❌ Silent FS fallback in a read endpoint. The whole point of this DoD is to make missing data explicit.
- ❌ A new master section or artifact kind without an ingest path in the same PR ("we'll backfill later" never happens — the data drifts and the next reader fails confusingly).
- ❌ Writing to both DB and FS on the same API call (the legacy "dual-write" pattern). Instead: write to DB, expose an explicit export step.
- ❌ Conflating ETag computation between FS bytes and DB blob without verifying they agree (S5 of trk-144d42b1 hit this — the fix was to standardize on `sha256(blob.encode("utf-8"))` everywhere).

## History

- **0.8 (trk-9bb48a61):** introduced dual-write — FS authoritative, DB a shadow. Reads stayed DB-only behind `JOBSMITH_FS_FALLBACK=1`. The flag turned out to mask backfill gaps.
- **0.8.1 (trk-144d42b1):** flipped the source-of-truth — DB is authoritative, FS is ingest+export. Removed `_fs_fallback_load`, added `master_content` table and `jobsmith master export`, codified this DoD.
