# Roadmap

Honest tracker of what's extracted, what's pending, and what's planned.

---

## 0.1.0 — Extraction + Python package + CLI ✅ Shipped

The /apply pipeline was extracted from the personal `shakestzd` repo, depersonalized, and shipped as both a Claude Code plugin AND a standalone Python package with a Typer CLI. One core; two surfaces.

### Done

**Extraction:**
- [x] LICENSE (MIT), plugin.json, .gitignore, README, CONTRIBUTING
- [x] All 14 specialist agents in `agents/` (orchestrator + 13 specialists), depersonalized
- [x] Specialist contracts at `agents/apply/specialist-contracts.yaml` (frozen_at reset to null pending re-freeze; version: 1)
- [x] Slash commands: `/apply`, `/apply-batch`, `/jobsmith-init`
- [x] Quarto + Typst templates
- [x] Example master YAML for fictional "Pat Doe" data engineer profile

**Python package + CLI:**
- [x] `pyproject.toml` declaring `pydantic`, `pyyaml`, `typer`, `rich` as deps; `textual` and `pytest` as optional extras
- [x] `src/jobsmith/` Python package — `__init__.py`, `config.py` (Pydantic), `paths.py`, `anchors.py` (regex + threshold constants), `guard.py` (anchor-bullet-guard core), `factcheck.py` (fact-check core), `cli.py` (Typer CLI)
- [x] CLI commands: `init`, `validate`, `status`, `doctor`, `fact-check`, `anchor-check`, `--version`
- [x] CLI commands call package functions directly — no subprocess hops
- [x] All agent prompts and `specialist-contracts.yaml` reference `jobsmith` CLI commands instead of script paths
- [x] Legacy `scripts/anchor_bullet_guard.py`, `scripts/fact_check_draft.py`, `scripts/jobsmith_init.py` removed (logic moved into the package)
- [x] 61 pytest tests passing — anchor regex, config validation, guard logic, factcheck logic
- [x] `uv pip install -e ".[test]"` produces a working `jobsmith` binary on PATH

**Verified clean:**
- No `Sunnova`, `SunStrong`, `Moreen`, `/Users/shakes/`, or feedback-memory-file references in framework files
- All shakestzd-specific paths replaced with config-driven references via `${VOICE_GUIDE_PATH}`, `${USER_EMAIL}`, `${USER_GITHUB}`, `${EMPLOYMENT_GAP_SNIPPET}`

### Caveats

- Framework has **not yet been tested end-to-end against a fresh init**. First-user (Shakes) will validate by running `jobsmith init` in a fresh directory and `/apply <url>` against a real role.
- The Quarto extension symlink path inside agents may need adjustment depending on whether jobsmith is installed as a Claude Code plugin (`${CLAUDE_PLUGIN_ROOT}/templates/extensions/...`) vs. cloned standalone.

---

## 0.1.x — First-user validation patches

Things that will surface only when the framework is actually used:

- [ ] Path-resolution bugs in agents when run from a non-shakestzd repo
- [ ] Config-loading wiring (where exactly does `.apply-config.yaml` get read into the orchestrator's context?)
- [ ] `${CLAUDE_PLUGIN_ROOT}` vs. local-clone path resolution in agent prompts
- [ ] Template rendering against a fresh user's master YAML (likely some font / margin tweaks)
- [ ] First non-Shakes user reporting issues
- [ ] SQLite migration shipping — `scripts/migrations/001_add_last_synced_at.sql` referenced by `apply-db-logger.md` but not yet present in the repo

---

## 0.2.0 — JD-similarity reuse-detector

Captured in `plan-bf34f540` (currently in `shakestzd/.htmlgraph/plans/`, will be ported to jobsmith). Adds a reuse-detector specialist that compares incoming JDs against prior applications and surfaces three branches: full / light-edit / reuse.

Empirical motivation: a manual schneider←google light-edit took ~10 min vs. ~3 hours for a full pipeline run. ~80% of incoming JDs are similar enough to a prior application to qualify for light-edit.

### Slices

- Slice 0: Corpus backfill — populate `.apply-state/jd-parsed.json` for prior applications
- Slice 1: `jobsmith.similarity` — Jaccard on `top_keywords` + role_type hard gate
- Slice 2: `apply-reuse-detector` specialist + contract update (version → 2)
- Slice 3: Orchestrator branch (Step 1.5 reuse-decision pause)
- Slice 4: Reuse path (symlink + cover-letter-only pipeline)
- Slice 5: Light-edit path (copy + vocabulary-mismatch scanner + targeted prose-writer mode)
- Slice 6: Master-freshness staleness check
- Slice 7: Calibration suite + threshold tuning

---

## 0.3.0 — Cover-letter workflow process

Generalize the careerfair.io 8-step `cover-letter-workflow.qmd` (built tonight as Google + Schneider Electric one-offs) into a reusable process.

### Plan

- `templates/cover-letter/cover-letter-workflow-template.qmd` — Quarto template with 8 sections scaffolded:
  1. JD analysis (responsibilities + qualifications tables)
  2. Requirements-to-qualifications match (top 2 selection)
  3. Why-do-you-want-to-work-here research (6 questions, 2 selected reasons)
  4. 5-component letter draft (§1 who-I-am / §2 transition / §3 skill match / §4 why-here / §5 conclusion)
  5. Pre-humanizer assembled letter
  6. Humanizer pass (6.1 draft → 6.2 audit "what's still AI?" → 6.3 final)
  7. Copy-paste output (body-only for portal paste; full letter for PDF/email)
  8. LinkedIn outreach (connection-request + InMail + outreach plan)
- `agents/apply-cover-letter-workflow.md` — new specialist that scaffolds the workflow QMD per application (vs. existing apply-cover-letter-writer which does a one-shot draft)
- `commands/apply-cover-letter-workflow.md` — slash command `/apply-cover-letter-workflow {slug}`
- `docs/cover-letter-workflow.md` — documentation: when to use the deep workflow vs. the one-shot drafter, how the humanizer pass works, what the careerfair.io 5-component template gives you

---

## 0.4.0 — Quarto application portfolio site

Each application becomes a page in a Quarto site rooted at the user's repo. The site is a browsable, reviewable surface for the entire application pipeline state.

### Per-application page sections

- Frontmatter: company, position, location, salary, JD URL, req ID, date found, status, fit score, similarity score (when 0.2 ships)
- JD summary + key requirements two-column table (must-have / nice-to-have)
- Fit score breakdown — must-have table with STRONG/HAVE/PARTIAL/GAP/BLOCKER
- Bullet diff — anchor preservation summary
- Cover letter workflow (the careerfair.io 8-step process inline as sub-page)
- Embedded resume PDF preview
- Embedded cover letter PDF preview
- Application materials list (what was generated)
- Timeline (decisions, edits, submission date)

### CLI commands

- `jobsmith site init` — scaffold the `_quarto.yml` and per-application page template in the user's repo
- `jobsmith site serve` — run `quarto preview` for live editing
- `jobsmith site render` — `quarto render` for static export (gitignored `_site/`)

### Privacy

Site renders to `_site/` which is gitignored. Each application contains JD URLs, salary figures, fit scores, hiring-manager intel. Never push to a public host without an explicit opt-in flag.

### Cross-linking

When 0.2 reuse-detector ships, similar applications are cross-linked on the page (e.g., "schneider-electric was a light-edit of google-data-scientist — see source").

---

## 0.5.0 — Hybrid plugin / standalone CLI maturity

Today the package and the plugin agents share one source of truth (the agent prompts dispatch via Claude Code's Task tool, but they invoke `jobsmith` CLI for the deterministic logic). 0.5 closes the loop:

- Standalone Python orchestrator that dispatches the same agent prompts via the Anthropic SDK directly (no Claude Code required)
- `jobsmith apply <url>` becomes the entry point for users not on Claude Code
- Provider abstraction so OpenAI / other LLM providers can plug in
- Textual TUI surfaces (`jobsmith dashboard`, `jobsmith calibrate`, `jobsmith review <slug>`)

---

## 1.0.0 — Stable

- Plugin API frozen
- Versioned specialist contracts (semver)
- 3+ public users
- Issue tracker has more closed than open
- Documented upgrade path from 0.x → 1.0
