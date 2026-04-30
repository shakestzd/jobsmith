# Roadmap

Honest tracker of what's extracted, what's pending, and what's planned.

---

## 0.1.0-alpha — Extraction from `shakestzd` ✅ Complete

The /apply pipeline was extracted from the personal `shakestzd` repo, depersonalized, and shipped as a Claude Code plugin scaffold. The framework can now be installed in any application repo via `jobsmith init`.

### Done

- [x] Repo scaffolding (LICENSE, plugin.json, .gitignore, README, CONTRIBUTING)
- [x] **All 13 specialist agents** depersonalized and copied to `agents/`:
  - apply-jd-parser
  - apply-fit-scorer
  - apply-hm-enricher
  - apply-bullet-selector
  - apply-prose-writer
  - apply-prose-qa
  - apply-resume-tell-fixer
  - apply-resume-renderer
  - apply-portfolio-ats-checker
  - apply-visual-layout-reviewer
  - apply-cover-letter-writer
  - apply-index-writer
  - apply-db-logger
  - apply-relevance-inquirer
- [x] Specialist contracts at `agents/apply/specialist-contracts.yaml` (frozen_at reset to null pending user re-freeze; version: 1)
- [x] Orchestrator at `agents/apply-agent.md` with config-driven paths
- [x] Slash commands: `/apply`, `/apply-batch`, `/jobsmith-init`
- [x] Scripts: `anchor_bullet_guard.py`, `fact_check_draft.py`, `jobsmith_init.py`
- [x] Quarto + Typst templates: `templates/resume/`, `templates/cover-letter/`, `templates/extensions/_extensions/`
- [x] Config schema at `config-schema.yaml` documenting every field
- [x] Example master YAML for fictional "Pat Doe" data engineer profile
- [x] `docs/getting-started.md` describing install + run UX

### Verified

- Personal-reference scan clean — no `Sunnova`, `SunStrong`, `Moreen`, or `/Users/shakes/` references in framework files
- All shakestzd-specific paths replaced with config-driven references via `${VOICE_GUIDE_PATH}`, `${USER_EMAIL}`, `${USER_GITHUB}`, `${EMPLOYMENT_GAP_SNIPPET}`
- 55 framework files / ~5,400 lines

### Caveats

- The framework has **not yet been tested end-to-end against a fresh init**. First-user (Shakes) will validate by running `jobsmith init` in a fresh directory and `/apply <url>` against a real role. Bugs found during that pass go into the 0.1.x patch milestones.
- The Quarto extension symlink path inside agents may need adjustment depending on whether jobsmith is installed as a Claude Code plugin (`${CLAUDE_PLUGIN_ROOT}/templates/extensions/...`) vs. cloned standalone.

---

## 0.1.x — First-user validation patches

Things that will surface only when the framework is actually used:

- [ ] Path-resolution bugs in the agents when run from a non-shakestzd repo
- [ ] Config-loading wiring (where exactly does `.apply-config.yaml` get read into the orchestrator's context?)
- [ ] `${CLAUDE_PLUGIN_ROOT}` vs. local-clone path resolution in agent prompts and scripts
- [ ] Template rendering against a fresh user's master YAML (likely some font / margin tweaks for non-English / non-US-letter contexts)
- [ ] First non-Shakes user reporting issues

---

## 0.2.0 — JD-similarity reuse-detector

Captured in `plan-bf34f540` (currently in `shakestzd/.htmlgraph/plans/`, will be ported to jobsmith). Adds a reuse-detector specialist that compares incoming JDs against prior applications and surfaces three branches: full / light-edit / reuse.

Empirical motivation: a manual schneider←google light-edit took ~10 min vs. ~3 hours for a full pipeline run. ~80% of incoming JDs are similar enough to a prior application to qualify for light-edit.

### Slices (post-extraction)

- Slice 0: Corpus backfill — populate `.apply-state/jd-parsed.json` for prior applications
- Slice 1: `jd_similarity.py` — Jaccard on `top_keywords` + role_type hard gate
- Slice 2: `apply-reuse-detector` specialist + contract update (version → 2)
- Slice 3: Orchestrator branch (Step 1.5 reuse-decision pause)
- Slice 4: Reuse path (symlink + cover-letter-only pipeline)
- Slice 5: Light-edit path (copy + vocabulary-mismatch scanner + targeted prose-writer mode)
- Slice 6: Master-freshness staleness check
- Slice 7: Calibration suite + threshold tuning

---

## 0.3.0 — First external user

Beyond Shakes. This means:

- Public docs at `jobsmith.dev`
- Plugin marketplace listing (Anthropic plugin marketplace if/when public)
- A "from zero to first /apply" walkthrough (video or annotated transcript)
- One additional user (anyone — friend, colleague, fellow job-searcher) successfully running the full pipeline end-to-end on a real role

---

## 1.0.0 — Stable

- Plugin API frozen
- Versioned specialist contracts (semver)
- 3+ public users
- Issue tracker has more closed than open
- Documented upgrade path from 0.x → 1.0
