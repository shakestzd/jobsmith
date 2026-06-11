# jobsmith

> Tailored resume and cover-letter pipeline for Claude Code. Master-first, no fabrication, anchor-preserving. Renders 1-page Quarto+Typst PDFs from a structured YAML profile.

**Status:** 0.1.0-alpha — early extraction from a working personal pipeline. Not yet ready for general use. See [ROADMAP.md](ROADMAP.md).

**Website:** https://jobsmith.dev (placeholder — domain registered, site pending)

---

## What this is

A multi-agent application pipeline that takes a job description, compares it against your master YAML (work history, skills, education, projects), and ships a tailored 1-page resume PDF + cover letter draft. Designed for Claude Code as a plugin.

The framework was extracted from a personal job-search pipeline that produced 25+ tailored applications across senior data engineering, AI engineering, and data analyst roles. It is opinionated about three things:

1. **Master is read-only.** Your `assets/content/*.yml` is the only source of truth. The pipeline never invents bullets, metrics, or claims.
2. **Anchors are preserved.** Bullets containing ≥$10M, ≥50%, or ≥100K-asset metrics are kept across applications unless you log a reason to drop them.
3. **Voice is portable.** Cover letters use a careerfair.io-derived 5-component template + a humanizer pass that scrubs AI tells.

## Why this exists

Tailoring a resume for every application is real work — typically 30-90 minutes of cognitive load per job. Most of that work is repeated across similar roles. jobsmith automates the mechanical parts (anchor preservation, ATS validation, layout review, fact-checking) and surfaces the parts that genuinely need human judgment (which bullets matter for this JD, what voice to use in the cover letter).

It is **not** an AI resume generator. It does not invent content. It selects, orders, and rephrases what you've already lived through.

## Who it's for

- People applying to 5+ roles and tired of redoing the same edits
- People whose master content is rich (real metrics, named projects, specific systems) and want it surfaced consistently
- People using Claude Code who would rather encode their voice once than re-explain it every session

## Architecture (when extraction is complete)

```
jobsmith/
├── plugin.json              # Claude Code plugin manifest
├── agents/                  # 13 specialist agents — orchestrator dispatches these
│   ├── apply-jd-parser.md
│   ├── apply-fit-scorer.md
│   ├── apply-bullet-selector.md
│   ├── apply-prose-writer.md
│   ├── apply-resume-renderer.md
│   ├── apply-cover-letter-writer.md
│   ├── apply-portfolio-ats-checker.md
│   ├── apply-visual-layout-reviewer.md
│   ├── apply-index-writer.md
│   ├── apply-db-logger.md
│   ├── apply-hm-enricher.md
│   ├── apply-relevance-inquirer.md
│   ├── apply-prose-qa.md
│   └── apply/specialist-contracts.yaml  # FROZEN binding interface
├── commands/                # Slash commands
│   ├── apply.md             # /apply <url-or-jd-text>
│   └── apply-batch.md       # /apply-batch <linkedin-search-url>
├── scripts/                 # Python utilities
│   ├── anchor_bullet_guard.py
│   └── fact_check_draft.py
├── templates/
│   ├── resume/              # Quarto + Typst awesomecv-typst
│   └── cover-letter/        # Careerfair.io 5-component workflow QMD
├── examples/                # Sample master YAML, sample application
└── docs/                    # Getting started, configuration, contributing
```

## Quickstart — `uv tool install` → `jobsmith up`

Install jobsmith as a standalone CLI tool (wheel includes the bundled UI):

```bash
# Install from PyPI (or a local wheel)
uv tool install jobsmith

# Or install from a local build (e.g. a development wheel):
uv tool install dist/jobsmith-*.whl
```

Initialize a repo and start the server:

```bash
mkdir my-job-search && cd my-job-search
jobsmith init          # scaffold assets/content/*.yml + .apply-config.yaml
jobsmith up            # starts http://127.0.0.1:8000 and opens your browser
```

The browser opens automatically once the server is ready.  The UI auto-authenticates
on localhost — no token management required.

**Flags:**

| Flag | Effect |
|------|--------|
| `--no-open` | Don't open the browser automatically |
| `--port PORT` | Bind to a different port (default: 8000) |
| `--bind-public` | Bind to 0.0.0.0 instead of 127.0.0.1 (disables auto-auth) |
| `--dev` | API-only mode — use with `npm run dev` in `web/` for hot-reload |

**Editable-install dev note (for contributors):**

```bash
git clone <repo> && cd jobsmith
uv pip install -e ".[dev]"   # editable install, no bundled UI
cd web && npm install && npm run build  # build UI into src/jobsmith/web_dist/
jobsmith up                  # now serves the built UI

# Or for front-end hot-reload:
jobsmith up --dev            # API only on :8000
cd web && npm run dev        # Vite dev server on :5173
```

CI requirement: **node is required at wheel-build time** (`uv build --wheel` runs
`vite build` via the hatch_build.py hook).  The installed wheel and its runtime
venv are npm-free.

## Quickstart (planned, not yet functional)

```bash
# Install as a Claude Code plugin
claude plugin install github.com/shakestzd/jobsmith

# Initialize a master YAML profile in your application repo
mkdir my-job-search && cd my-job-search
jobsmith init  # writes assets/content/{work,skill,education,author}.yml stubs

# Run the pipeline against a job URL
claude /apply https://example.com/jobs/12345

# On first run, jobsmith auto-freezes specialist-contracts.yaml (sets frozen_at).
# If the gather phase fails with a "contracts not frozen" error, run:
#   jobsmith doctor
```

### Disabling the cover letter

The cover letter is generated by default.  Two ways to skip it:

**Per-run** — pass a CLI flag:

```bash
jobsmith apply --no-cover-letter https://example.com/jobs/12345
# Forces the cover letter even when config has framework: none:
jobsmith apply --cover-letter  https://example.com/jobs/12345
```

**Persistent default** — set `framework: none` in `.apply-config.yaml`:

```yaml
cover_letter:
  framework: none   # disables apply-company-research + apply-cover-letter-writer
```

When disabled, the two cover-letter-only specialists (`apply-company-research`,
`apply-cover-letter-writer`) are not dispatched; synthetic `status=ok` entries
are injected into the manifest before each phase runs so the pipeline treats
those specialists as already complete.  All resume-only steps run unchanged.

## CLI — feedback loop

After you hand-edit the agent's draft, capture those edits as structured lessons:

```bash
jobsmith feedback record <slug>          # diff live drafts vs .agent.md snapshots:
                                         #   .apply-state/prose-draft.md vs prose-draft.agent.md
                                         #   <app>/cover-letter-draft.md vs .apply-state/cover-letter-draft.agent.md
jobsmith feedback list                   # Rich table of all records
jobsmith feedback list --kind prose-bullet --since 30d
jobsmith feedback prune --older-than 90d # rotate old records
jobsmith feedback export --out feedback-export.yaml  # YAML summary — review before sharing
```

Records live in `private/feedback/` (gitignored by default) as
`<iso-timestamp>__<slug>.json`. The `export` subcommand drops slug + per-app
context but copies user-authored `lesson` strings verbatim; review the YAML
before syncing it. See [docs/render-benchmark.md](docs/render-benchmark.md)
for the full record schema and read-back integration roadmap.

## Sourcing runbook

jobsmith can automatically discover job postings by crawling ATS boards and ingesting job alert emails.  All sourcing is configured in `sourcing.yaml` at the root of your job-search repo.

### Enable / disable the schedule

```bash
# Install the daily launchd schedule (runs once per day, 08:00 local time)
jobsmith source install-schedule

# Disable without removing — pause the schedule while keeping config
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.jobsmith.source.plist

# Re-enable
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.jobsmith.source.plist

# Remove permanently
jobsmith source uninstall-schedule   # or manually: rm ~/Library/LaunchAgents/dev.jobsmith.source.plist
```

The schedule uses `JOBSMITH_REPO_ROOT` to locate your `.apply-config.yaml` + `sourcing.yaml`.  When running via launchd the environment variable must be set in the plist (done automatically by `install-schedule`).  If you move your repo, re-run `install-schedule` to update the plist.

### Add an ATS source

Edit (or create) `sourcing.yaml` in your repo root:

```yaml
sources:
  - type: greenhouse      # greenhouse | lever | ashby | hn_whos_hiring | climatebase
    slug: stripe          # the board slug (from the company's Greenhouse URL)
    company: Stripe       # canonical company name shown in the UI
    name: Stripe          # display name (used in per-source funnel table)
    enabled: true         # omit or set false to pause without removing
```

Run a one-off crawl to verify:

```bash
jobsmith source run              # crawl all enabled sources
jobsmith source run --source greenhouse/stripe   # single source
jobsmith source run --no-llm     # skip LLM rescore (fast_score only, no API cost)
jobsmith source run --dry-run    # fetch + parse, but do not write to DB
```

### Add an email alert sender

jobsmith can ingest job alert emails from Gmail or Apple Mail.

**Gmail** (reads via Gmail API — requires `gcloud auth application-default login`):

```yaml
alert_senders:
  - type: gmail_alert
    sender: jobs-noreply@linkedin.com   # the From: address of the alert emails
    sender_slug: linkedin-alert         # used as the source key (email/linkedin-alert)
    enabled: true
```

**Apple Mail** (reads from the local Mail.app store — macOS only):

```yaml
alert_senders:
  - type: mailapp_alert
    sender_slug: indeed-alert
    account: myemail@example.com   # Mail.app account name
    mailbox: Job Alerts            # mailbox name inside that account
    enabled: true
```

Set `enabled: false` on any sender to pause ingestion without removing the config.

### Read the funnel and run-health

After a crawl, open the jobsmith UI (`jobsmith up`) and navigate to:

- **Postings inbox** — ranked list (LLM score desc, fast_score desc) with source, title, company.
- **Funnel** — stage counts (sourced → queued → promoted → interview → offer), conversion rates, per-source yield.
- **Run health** — state (`ok` / `degraded` / `stale` / `failed`), last run timestamp, degraded sources.

Or query the API directly:

```bash
curl -s http://127.0.0.1:8000/api/postings | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/funnel?window=30 | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/sourcing/run-health | python3 -m json.tool
```

### `--no-llm` and budget caps

The LLM rescore pass ranks the top-N new postings by fast_score using the Anthropic API.  To disable it:

```bash
jobsmith source run --no-llm
```

Or cap cost per run in `sourcing.yaml`:

```yaml
rescore_n_cap: 30         # top-N by fast_score to send to the LLM (default: 30)
rescore_budget_usd: 1.00  # soft USD ceiling — stops after this spend (default: $1.00)
```

### JOBSMITH_REPO_ROOT semantics for launchd

launchd strips most environment variables.  `install-schedule` injects `JOBSMITH_REPO_ROOT` into the plist pointing to your job-search repo root (the directory containing `.apply-config.yaml`).  The sourcing run reads `sourcing.yaml` from that root.

If you use multiple repos, run `install-schedule` once per repo — it will create or overwrite the plist.  Check the current value:

```bash
launchctl getenv JOBSMITH_REPO_ROOT   # (only works inside a launchd session)
cat ~/Library/LaunchAgents/dev.jobsmith.source.plist | grep -A1 JOBSMITH_REPO_ROOT
```

---

## Key principles

- **Evidence > assumptions.** Every metric on the rendered resume must trace back to your master YAML. Halt the pipeline rather than fabricate.
- **Anchors are sacred.** Bullets with load-bearing metrics (≥$10M, ≥50%, ≥100K-asset) are preserved across applications unless an explicit reason is logged.
- **Voice is yours.** The framework provides scaffolding (5-component cover letter, humanizer pass for AI tells, prose-writer with role-conditional length) but never overrides your written voice.
- **Master is read-only.** The pipeline reads your master YAML; it never writes to it. Corrections go into master via separate tooling.
- **Halt over fabricate.** When a JD requirement has no master coverage, the pipeline stops and asks. It does not invent.

## Roadmap

See [ROADMAP.md](ROADMAP.md). High-level:

- **0.1**: Extraction from `shakestzd` — agents, contracts, scripts, templates
- **0.2**: JD-similarity reuse-detector (light-edit + reuse paths) — sliced in `plan-bf34f540`
- **0.3**: First external user
- **1.0**: Stable plugin API, public docs, examples

## Contributing

Not yet open for external contributions — the codebase is mid-extraction. Watch the repo or check ROADMAP.md for when 0.3 lands.

If you have ideas, open an issue with `[discussion]` in the title.

## License

MIT — see [LICENSE](LICENSE).

## Author

Thandolwethu "Shakes" Dlamini · [shakestzd.github.io](https://shakestzd.github.io) · [github.com/shakestzd](https://github.com/shakestzd)
