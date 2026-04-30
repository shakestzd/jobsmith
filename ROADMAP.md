# Roadmap

Honest tracker of what's extracted, what's pending, and what's planned.

---

## 0.1.0-alpha — Extraction from `shakestzd` (in progress)

The /apply pipeline currently lives inside the personal `shakestzd` repo. Extraction means moving the framework to this repo, making it personality-neutral, and shipping a clean install path.

### Done

- [x] Repo scaffolding (directory structure, LICENSE, plugin.json, .gitignore, README, this ROADMAP)
- [x] Initial commit

### Pending

- [ ] **Specialists** — copy and depersonalize each of the 13 specialist agents from `shakestzd/.claude/agents/apply-*.md`
  - [ ] apply-jd-parser
  - [ ] apply-fit-scorer
  - [ ] apply-bullet-selector
  - [ ] apply-prose-writer
  - [ ] apply-resume-renderer
  - [ ] apply-cover-letter-writer
  - [ ] apply-portfolio-ats-checker
  - [ ] apply-visual-layout-reviewer
  - [ ] apply-index-writer
  - [ ] apply-db-logger
  - [ ] apply-hm-enricher
  - [ ] apply-relevance-inquirer
  - [ ] apply-prose-qa
- [ ] **Specialist contracts** — `agents/apply/specialist-contracts.yaml` ported from `shakestzd`
- [ ] **Orchestrator** — `agents/apply-agent.md` ported, with all hardcoded shakestzd paths replaced by config-driven paths
- [ ] **Scripts** — `scripts/anchor_bullet_guard.py`, `scripts/fact_check_draft.py` ported (already personality-neutral)
- [ ] **Slash commands** — `commands/apply.md`, `commands/apply-batch.md` ported
- [ ] **Templates** — Quarto + Typst awesomecv-typst extension; cover-letter workflow QMD
- [ ] **Config schema** — `config-schema.yaml` defining what users configure (master YAML paths, voice memory file, anchor thresholds, output dirs, fact-check sources)
- [ ] **Example master YAML** — sanitized sample work.yml, skill.yml, education.yml, author.yml
- [ ] **Getting-started doc** — `docs/getting-started.md` covering install, master YAML setup, first /apply run
- [ ] **`jobsmith init`** — CLI helper that scaffolds a fresh master YAML in a user's application repo

### Migration story (Shakes-specific, kept in `shakestzd`)

- `shakestzd/assets/content/*.yml` stays as-is — Shakes' master data
- `shakestzd/private/applications/` stays as-is — personal application archive
- `shakestzd/.claude/agents/apply-*.md` will eventually become a thin reference to the jobsmith plugin
- A `shakestzd/.apply-config.yaml` will declare the paths for jobsmith to read from

---

## 0.2.0 — JD-similarity reuse-detector

Captured in `plan-bf34f540` (currently in `shakestzd/.htmlgraph/plans/`, will be migrated to jobsmith). Adds a reuse-detector specialist that compares incoming JDs against prior applications and surfaces three branches: full / light-edit / reuse.

Empirical motivation: a manual schneider←google light-edit took ~10 min vs. ~3 hours for a full pipeline run. ~80% of incoming JDs are similar enough to a prior application to qualify for light-edit.

### Slices (post-extraction)

- Slice 0: Corpus backfill — populate `.apply-state/jd-parsed.json` for prior applications
- Slice 1: `jd_similarity.py` — Jaccard on `top_keywords` + role_type hard gate
- Slice 2: `apply-reuse-detector` specialist + contract update
- Slice 3: Orchestrator branch (Step 1.5 reuse-decision pause)
- Slice 4: Reuse path (symlink + cover-letter-only pipeline)
- Slice 5: Light-edit path (copy + vocabulary-mismatch scanner + targeted prose-writer mode)
- Slice 6: Master-freshness staleness check
- Slice 7: Calibration suite + threshold tuning

---

## 0.3.0 — First external user

Beyond Shakes. This means:

- Public docs at `jobsmith.dev`
- Example applications repo separate from this framework
- Plugin marketplace listing (Anthropic plugin marketplace if/when public)
- A "from zero to first /apply" video walkthrough

---

## 1.0.0 — Stable

- Plugin API frozen
- Versioned specialist contracts
- 3+ public users
- Issue tracker has more closed than open
