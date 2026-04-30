# Architecture

## Core principle: gather once, render many

Every application produces structured state in `private/applications/{slug}/.apply-state/`. That state is the **single source of truth**. Every artifact — the resume PDF, the cover letter PDF, the careerfair.io workflow review document, the per-application portfolio page, the LinkedIn outreach snippets — is a **rendering** of that state.

```
        ┌──────────────────────────────────┐
        │  Specialists gather information  │
        │  ────────────────────────────────│
        │  jd-parsed.json                  │
        │  fit-score.json                  │
        │  bullet-selection.json           │
        │  bullet-decisions.json           │
        │  hm-snippet.md                   │
        │  company-research.md   (NEW 0.3) │
        │  prose-draft.md                  │
        │  cover-letter-draft.md           │
        │  outreach-snippets.md  (NEW 0.3) │
        │  ai-tell-report.json             │
        │  layout-report.md                │
        └────────────────┬─────────────────┘
                         │
            .apply-state │ (single source of truth)
                         │
         ┌───────────────┴───────────────┐
         │                               │
         ▼                               ▼
  ┌─────────────┐                 ┌──────────────┐
  │ Renderings  │                 │  Renderings  │
  │  (Quarto)   │                 │   (Typst)    │
  ├─────────────┤                 ├──────────────┤
  │ workflow.qmd│                 │  resume.pdf  │
  │ index.qmd   │                 │  cover.pdf   │
  │ site/       │                 └──────────────┘
  └─────────────┘
```

### Why this matters

Without this principle, every artifact re-gathers the same facts:
- The cover letter writer re-parses the JD to know what to mention
- The portfolio page re-runs fit analysis to display fit score
- The workflow review document re-extracts requirements to make tables
- The LinkedIn outreach re-summarizes the cover letter

That redundancy is where bugs live, where prompts drift, where the user sees inconsistent claims across documents. Gathering once and rendering many means every rendering shows the same facts, computed by the same specialist, validated by the same gates.

### What gets gathered (specialist topology)

```
Stage 0:   apply-jd-parser
Stage 1:   apply-fit-scorer ⫷⫸ apply-hm-enricher ⫷⫸ apply-bullet-selector ⫷⫸ apply-company-research [NEW 0.3]
Stage 1.5: anchor-bullet-guard
Stage 1.6: apply-relevance-inquirer (conditional)
Stage 2:   apply-prose-writer ↔ apply-prose-qa (loop)
Stage 3:   apply-resume-renderer → apply-portfolio-ats-checker → apply-visual-layout-reviewer
Stage 4:   apply-cover-letter-writer — produces draft.md AND assembles workflow.qmd
           apply-hm-enricher (extended in 0.3) — also produces outreach-snippets.md
Stage 5:   apply-index-writer — produces per-application portfolio page
           apply-db-logger
```

The diff vs. 0.1: **one new specialist (`apply-company-research`)** and **two specialists' outputs extended**. No new agent for the workflow QMD; it's pure composition.

---

## Quarto features that make rendering DRY

Quarto ships native features that map directly to the "render many" requirement:

### `{{< include _partial.qmd >}}` — content reuse

Partials are stored in `templates/partials/` and included by both the workflow QMD and the per-application portfolio page. Same content, different containers.

```qmd
## Step 2 — Requirements Match
{{< include /partials/_must-have-table.qmd >}}
```

### `{{< var path.to.value >}}` + `_variables.yml` — single source for identity

User's name, email, contact links — set once in `templates/portfolio/_variables.yml`, used everywhere via `{{< var user.name >}}`.

### `{{< meta field >}}` — per-page frontmatter

Per-application values like `company`, `fit_score`, `salary_range` defined in each application's `index.qmd` frontmatter, referenced via `{{< meta company >}}`.

### `_metadata.yml` — cascading frontmatter

`private/applications/_metadata.yml` defines layout/theme/format options that automatically apply to every application page. Set once.

### Profiles — same source, different render

`_quarto-review.yml` shows the workflow scaffolding; `_quarto-final.yml` hides it and only renders the clean letter. Same source files; flag at render time.

### Cross-references — navigable structure

Within the workflow QMD, sections and tables are labeled (`@sec-jd-analysis`, `@tbl-must-haves`) so the reader can jump between them. When 0.2 reuse-detector ships, similar applications cross-link via `@sec-...` references across pages.

### Listings — auto-generated index from frontmatter

The portfolio site's index page is *just* a listings config — no manual list-keeping. Applications are sortable, filterable, searchable, paginated for free:

```yaml
---
title: "Applications"
listing:
  contents: ../../private/applications
  type: table
  sort: ["fit_score desc", "date_found desc"]
  fields: [date_found, company, position, fit_score, status]
  filter-ui: true
  page-size: 25
---
```

### Theme as SCSS — one brand file

`templates/portfolio/styles/jobsmith.scss` uses Quarto's `/*-- scss:defaults --*/` and `/*-- scss:rules --*/` pattern. Used by every HTML rendering — workflow review, portfolio site, any future surface.

### Custom Typst formats — extension reuse

Resume + cover letter already use the `awesomecv` Typst extension (shipped under `templates/extensions/_extensions/`). Same pattern is available for any future custom format.

---

## How partials read state: pre-render assembly

For partials to display content from `.apply-state/jd-parsed.json` etc., the JSON needs to be available to Quarto's `{{< var >}}` shortcode. We use a **pre-render assembly step** (rather than a Lua filter) for simplicity:

```bash
jobsmith assemble {slug}
# Reads private/applications/{slug}/.apply-state/*.json
# Writes private/applications/{slug}/_variables.yml with the relevant fields
```

After assembly, partials read the data via:

```qmd
The role demands {{< var jd.must_haves.0 >}} as the top must-have.
```

Where `_variables.yml` was generated as:

```yaml
jd:
  must_haves:
    - "6+ years of experience in data engineering, analytics engineering, or platform engineering roles"
    - ...
```

The Quarto preview/render pipeline runs `jobsmith assemble` automatically as a `pre-render` hook in `_quarto.yml`:

```yaml
project:
  pre-render: jobsmith assemble
```

### Why pre-render assembly over Lua filter

- **Simpler** — plain Python + plain Quarto shortcodes. No second language to maintain.
- **Inspectable** — the assembled `_variables.yml` is a real file you can `cat` to debug.
- **Cacheable** — Quarto caches based on file mtime; if state hasn't changed, no re-render.
- **Cost** — assembly is JSON read + YAML write, sub-second.

The trade-off is that any change to `.apply-state/` requires `jobsmith assemble` to refresh. Quarto's `pre-render` hook handles this automatically when serving via `quarto preview`.

---

## Directory structure (0.3+)

```
jobsmith/
├── src/jobsmith/
│   ├── ...                              # Existing package modules
│   ├── assemble.py                      # NEW — pre-render state assembly
│   ├── company.py                       # NEW — company research orchestration
│   └── site.py                          # NEW — portfolio site init/serve/render
├── templates/
│   ├── partials/                        # NEW — reusable Quarto includes
│   │   ├── _jd-summary.qmd              # JD overview from jd-parsed.json
│   │   ├── _must-have-table.qmd         # Fit table from fit-score.json
│   │   ├── _bullet-diff.qmd             # Anchor preservation from bullet-diff.md
│   │   ├── _company-research.qmd        # Why-this-company from company-research.md
│   │   ├── _letter-draft.qmd            # Cover letter from cover-letter-draft.md
│   │   ├── _outreach.qmd                # LinkedIn from outreach-snippets.md
│   │   ├── _humanizer-audit.qmd         # AI-tell findings from ai-tell-report.json
│   │   └── _resume-preview.qmd          # Embeds the rendered resume PDF
│   ├── workflow/                        # NEW — careerfair.io 8-step rendering
│   │   ├── _workflow.qmd                # Composes the 8 partials in sequence
│   │   └── _quarto.yml                  # Workflow render profile
│   ├── portfolio/                       # NEW — application portfolio site
│   │   ├── _quarto.yml                  # Website project + listings config
│   │   ├── _metadata.yml                # Cascading frontmatter
│   │   ├── _variables.yml               # User identity vars
│   │   ├── index.qmd                    # Listings page
│   │   ├── _application-page.qmd        # Per-app page partial
│   │   └── styles/
│   │       └── jobsmith.scss            # Theme SCSS
│   ├── resume/                          # (existing — Quarto + Typst awesomecv)
│   ├── cover-letter/                    # (existing)
│   └── extensions/_extensions/          # (existing — awesomecv-typst)
└── ...
```

---

## CLI surface (0.4)

```bash
jobsmith assemble <slug>      # Read .apply-state/*.json → write _variables.yml. Auto-run by site preview/render.
jobsmith site init            # Scaffold templates/portfolio/ in user's repo + _quarto.yml
jobsmith site serve           # quarto preview private/applications/
jobsmith site render          # quarto render private/applications/
jobsmith site list            # CLI table view (rich) — quick sortable list without browser
jobsmith review <slug>        # Open the per-application page in browser
```

---

## What this does NOT change

- **`.apply-state/` stays exactly the same.** No schema changes.
- **Specialist contracts stay version 1** — `apply-company-research` is an additive specialist, not a breaking change.
- **The Python package stays simple.** Three new modules (`assemble.py`, `company.py`, `site.py`); no architectural rewrite.
- **The plugin layer is unchanged.** Agents still dispatch via Claude Code's Task tool; the only diff is they write more state and assemble it into Quarto-friendly form.
