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

## 0.3.0 — Quarto content architecture: gather once, render many

Establish a unified content-composition layer where every artifact (resume, cover letter, careerfair.io 8-step workflow review document, per-application portfolio page, LinkedIn outreach) is a **rendering** of `.apply-state/` rather than an independent gathering process. Uses Quarto's native DRY features (`{{< include >}}`, `{{< var >}}`, `_metadata.yml`, listings, profiles, themes) instead of bolt-on infrastructure.

See [`docs/architecture.md`](docs/architecture.md) for the full design.

### Specialists (small additive changes)

- **NEW** — `apply-company-research`: produces `.apply-state/company-research.md` (mission, product, values, 2 selected reasons for §4, product-use evidence). Cached at `private/companies/{slug}.md` so two applications to the same company within N days don't re-research.
- **EXTENDED** — `apply-hm-enricher`: also writes `.apply-state/outreach-snippets.md` (LinkedIn connection note + InMail draft when an HM is named).
- **EXTENDED** — `apply-cover-letter-writer`: produces `cover-letter-draft.md` AND assembles the per-app `index.qmd` via `{{< include >}}` shortcodes. The index QMD doesn't re-gather any state — it composes partials. (Renamed from `workflow.qmd` to match Quarto's website-page convention; the site listings page reads each app's `index.qmd`.)
- **EXTENDED** — `apply-prose-qa`: humanizer pass extended to cover letter prose, not just resume.

### Quarto templates (the "render many" surface)

- **`templates/partials/`** (NEW) — reusable Quarto includes:
  - `_jd-summary.qmd` reads `jd-parsed.json`
  - `_must-have-table.qmd` reads `fit-score.json`
  - `_bullet-diff.qmd` reads `bullet-selection.json` + `bullet-diff.md`
  - `_company-research.qmd` reads `company-research.md`
  - `_letter-draft.qmd` reads `cover-letter-draft.md`
  - `_outreach.qmd` reads `outreach-snippets.md`
  - `_humanizer-audit.qmd` reads `ai-tell-report.json`
  - `_resume-preview.qmd` embeds the rendered resume PDF
- **`templates/workflow/`** (NEW) — careerfair.io 8-step rendering that includes the partials in sequence
- **`templates/portfolio/`** (NEW) — application portfolio site:
  - `_quarto.yml` (website project + listings)
  - `_metadata.yml` (cascading frontmatter)
  - `_variables.yml` (user identity)
  - `index.qmd` (listings page — sortable applications index)
  - `_application-page.qmd` (per-application page partial composing partials/)
  - `styles/jobsmith.scss` (theme)

### Pre-render assembly (composition strategy)

Partials read state via `{{< var >}}` shortcodes against a per-application `_variables.yml` written by `jobsmith assemble {slug}`. The `_quarto.yml` runs assembly as a `pre-render` hook so `quarto preview` automatically refreshes when state changes. Plain Python + plain Quarto — no Lua filters.

### Why this scope

Cover letter workflow + portfolio site share the same partials, theme, and variables — they're two layouts of the same content. Treating them as one milestone (instead of separate 0.3 + 0.4) avoids building each rendering as a one-off.

---

## 0.4.0 — Portfolio site CLI + publishing

The infrastructure shipped in 0.3 makes the portfolio site renderable. 0.4 wraps it in a clean CLI surface and adds the publishing/serving story.

### CLI commands

```bash
jobsmith assemble <slug>   # Read .apply-state/*.json → write _variables.yml. Auto-run by site preview/render.
jobsmith site init         # Scaffold templates/portfolio/ in user's repo + _quarto.yml
jobsmith site serve        # quarto preview private/applications/
jobsmith site render       # quarto render private/applications/
jobsmith site list         # CLI table view (rich) — quick sortable list without browser
jobsmith review <slug>     # Open the per-application page in browser
```

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
