# AGENTS.md — jobsmith development guide for AI coding agents

This file documents the codebase layout and critical conventions for AI agents
(Claude Code, Codex, Gemini, etc.) working on this repository.

## Quick orientation

- `src/jobsmith/` — Python package (FastAPI API + CLI + pipeline logic)
- `web/` — TypeScript/React frontend (Vite build)
- `tests/` — pytest unit + integration tests
- `tests/e2e/` — end-to-end acceptance tests (opt-in, see below)
- `hatch_build.py` — custom build hook: runs `vite build` → `src/jobsmith/web_dist/`

## Launch story — `uv tool install` → `jobsmith up`

The canonical install and launch sequence:

```bash
# 1. Install (wheel includes bundled UI — no npm needed at runtime)
uv tool install jobsmith

# 2. Init a repo
mkdir my-job-search && cd my-job-search
jobsmith init

# 3. Launch
jobsmith up              # opens http://127.0.0.1:8000 in your browser
jobsmith up --no-open    # headless / CI mode
```

**Localhost auto-auth:** on a `127.0.0.1` or `localhost` bind, the server
injects `window.__JOBSMITH__ = {token, apiBase}` into the served `index.html`
so the browser SPA authenticates immediately without any token configuration.
On `--bind-public` (0.0.0.0), auto-auth is disabled and the user must supply
an explicit token.

## Editable-install dev workflow

```bash
git clone <repo> && cd jobsmith
uv pip install -e ".[dev]"     # editable Python install
cd web && npm install           # install frontend deps
npm run build                   # build UI → src/jobsmith/web_dist/
cd ..
jobsmith up                     # serves bundled UI at :8000
```

**Two-process hot-reload mode (for frontend work):**

```bash
jobsmith up --dev          # API only on :8000 (skips static mount)
cd web && npm run dev      # Vite dev server on :5173 (proxies /api → :8000)
```

## Build note — node is required at wheel-build time

`uv build --wheel` runs `hatch_build.py` which calls `vite build` to compile
the frontend into `src/jobsmith/web_dist/`.  Node (v18+) must be on `PATH`
when building the wheel.

The installed wheel and its runtime venv are **npm-free** — `npm` is not
required at runtime.  The `tests/e2e/` fixtures assert this explicitly.

## Key source files

| File | Purpose |
|------|---------|
| `src/jobsmith/api/main.py` | FastAPI app factory (`create_app`) + lifespan |
| `src/jobsmith/api/staticui.py` | `find_web_dist()` locator + SPA catch-all + auto-auth shim |
| `src/jobsmith/api/server.py` | `up_serve()` + browser-opener daemon thread |
| `src/jobsmith/cli.py` | `jobsmith up` Typer command |
| `src/jobsmith/api/auth.py` | Static bearer token + JWT auth deps |
| `src/jobsmith/onboard/pipeline.py` | Onboarding pipeline (CLI + API paths) |
| `hatch_build.py` | Wheel build hook: vite build → web_dist |

## E2E test suite

Tests in `tests/e2e/` are **opt-in** (guarded by `JOBSMITH_E2E=1`):

```bash
# Default pytest run — e2e tests skip automatically, suite stays fast
uv run pytest -q

# Run e2e suite explicitly (requires node + JOBSMITH_E2E=1)
JOBSMITH_E2E=1 uv run pytest tests/e2e -q -m e2e
```

The e2e suite:
1. Builds a wheel (`uv build --wheel`) — requires node for `vite build`.
2. Installs into a **clean (npm-free) venv** — asserts no npm binary present.
3. Starts `jobsmith up --no-open` as a subprocess (bounded 30s readiness wait).
4. Drives apply + onboard flows through the same-origin HTTP API.
5. Asserts master YAMLs written and DB rows present (concrete onboard check).
6. Tears down the server subprocess in `finally` — no test hangs.

## Sourcing runbook (for agents touching the sourcing pipeline)

### Key sourcing files

| File | Purpose |
|------|---------|
| `src/jobsmith/sourcing/runner.py` | `run_crawl()` orchestrator — ATS + email ingestion, expiry, LLM rescore seam |
| `src/jobsmith/sourcing/store.py` | `upsert_posting`, `promote_posting`, `finish_sourcing_run` — DB helpers |
| `src/jobsmith/sourcing/config.py` | `load_sourcing_config()` — reads `sourcing.yaml` |
| `src/jobsmith/sourcing/llm_rescore.py` | LLM rescore pass — injectable `query_fn` for tests |
| `src/jobsmith/sourcing/adapters/` | Per-ATS adapters: greenhouse, lever, ashby, hn_whos_hiring, climatebase |
| `src/jobsmith/sourcing/email/` | Email ingestion: gmail.py, mailapp.py, parsers.py |
| `src/jobsmith/api/postings_routes.py` | `GET /api/postings`, `POST /api/postings/{id}/status`, `POST /api/postings/{id}/promote` |
| `src/jobsmith/api/funnel_routes.py` | `GET /api/funnel` — stage counts, conversion rates, per-source yield |
| `src/jobsmith/api/run_health.py` | `GET /api/sourcing/run-health` — last run state + age |

### Schedule management (launchd, macOS)

```bash
# Install the daily schedule (writes ~/Library/LaunchAgents/dev.jobsmith.source.plist)
jobsmith source install-schedule

# Disable (pause) without removing
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.jobsmith.source.plist

# Re-enable
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.jobsmith.source.plist
```

`install-schedule` embeds the current working directory as `JOBSMITH_REPO_ROOT` in the plist.  If the repo moves, re-run `install-schedule`.

### Adding a source

Add an entry under `sources:` in `sourcing.yaml` (at repo root, next to `.apply-config.yaml`):

```yaml
sources:
  - type: greenhouse   # greenhouse | lever | ashby | hn_whos_hiring | climatebase
    slug: stripe
    company: Stripe
    enabled: true
```

Verify with `jobsmith source run --source greenhouse/stripe --dry-run`.

### Adding an email sender

```yaml
alert_senders:
  - type: gmail_alert           # or mailapp_alert
    sender: jobs@linkedin.com
    sender_slug: linkedin-alert
    enabled: true
```

### One-off crawl commands

```bash
jobsmith source run                       # all enabled sources + email senders
jobsmith source run --source lever/netflix  # single source
jobsmith source run --no-llm              # skip LLM rescore (no Anthropic API call)
jobsmith source run --dry-run             # parse only, no DB writes
```

### Reading funnel / run-health

```bash
# Via API (server must be running: jobsmith up)
curl -s http://127.0.0.1:8000/api/funnel?window=30
curl -s http://127.0.0.1:8000/api/sourcing/run-health
```

`run-health` states: `ok` (last run done within 25 h), `stale` (> 25 h), `degraded` (some sources errored), `failed`, `no_runs`, `unknown`.

### `--no-llm` and budget caps

In `sourcing.yaml`:

```yaml
rescore_n_cap: 30         # top-N by fast_score sent to LLM (default 30)
rescore_budget_usd: 1.00  # soft USD ceiling (default $1.00)
```

Pass `no_llm=True` to `run_crawl()` in tests to skip the LLM seam entirely (uses only `fast_score`).

### DB migration

The postings store lives in migration 010 (`postings` + `sourcing_runs` tables).  Open the pipeline DB with `jobsmith.db.open_pipeline_db(path)` which auto-applies all migrations.

### Test patterns for the sourcing pipeline

- Use the `_adapter_factory_for(roles_by_key)` pattern from `tests/test_sourcing_runner.py` to inject fake adapters.
- Pass `_run_email_alerts_fn=<mock>` to `run_crawl()` to skip real Gmail / Mail.app.
- Pass `no_llm=True` to skip LLM rescore — no Anthropic SDK calls.
- Patch `jobsmith.sourcing.runner._INTER_SOURCE_SLEEP` to `0.0` in tests to skip the rate-limit sleep.
- Monkeypatch `jobsmith.api.postings_routes._get_db_path` and `jobsmith.api.funnel_routes._get_db_path` to point TestClient at a temp DB.
- Monkeypatch `jobsmith.api.run_health._resolve_db_path` (takes a `Request` arg) for run-health tests.

See `tests/e2e/test_sourcing_funnel_e2e.py` for the full fixtures-only E2E example.

## Pre-existing test failures (do not regress)

The default pytest run has 9 known pre-existing failures in:
- `tests/test_api_applications_post.py` (6 — force/jd_text assertions)
- `tests/test_apply_iter.py` (3 — phase-event assertions)

Do not add new failures to the default run.

## Ruff lint

```bash
uv run ruff check .
```

Known house style: `cli.py` has 26 pre-existing `B008` warnings (FastAPI
dependency injection in default args) — these are intentional and not yours
to fix.
