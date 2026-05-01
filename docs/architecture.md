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

For partials to display content from `.apply-state/jd-parsed.json` etc., the JSON needs to be available to Quarto's `{{< var >}}` shortcode. We use a **pre-render assembly step** (rather than a Lua filter) for simplicity.

The CLI has two assembly modes:

```bash
jobsmith assemble <slug>   # Per-application: reads private/applications/<slug>/.apply-state/*.json
                           # and writes private/applications/<slug>/_variables.yml
jobsmith assemble --all    # Site-wide: assembles every application under the configured
                           # output.applications_dir. Used as the Quarto pre-render hook
                           # so a single render pass refreshes all pages.
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

The Quarto site (`templates/portfolio/_quarto.yml`) wires `jobsmith assemble --all` as a `pre-render` hook so `quarto preview` and `quarto render` automatically refresh every application's `_variables.yml` before composing the partials:

```yaml
project:
  type: website
  pre-render: jobsmith assemble --all
```

For ad-hoc per-application work outside the site context (e.g., regenerating just one application's workflow QMD after a state edit), invoke the slug-aware form directly:

```bash
jobsmith assemble pwc-ai-engineer-data-scientist-ai-manager
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

## Privacy model

### Why privacy must be the default

Each application page contains sensitive intelligence that must never accidentally
reach a public host:

| Category | Examples |
|---|---|
| Compensation | `salary_range`, `salary` |
| Scoring | `fit_score`, `must_have_table` (evidence column + full table) |
| Bullet analysis | `bullet_decisions`, `bullet_diff`, `gap_resolutions` |
| Hiring-manager intel | `hm_name`, `hm_email`, `hm_signals` |
| Outreach internals | `outreach_snippets`, `humanizer_audit` |

These values live in the assembled `_variables.yml` for every application.
A naive `quarto render` would embed them into every portfolio page.

### Two rendering modes

| Mode | Output dir | What's in it |
|---|---|---|
| **private** (default) | `_site/` | Everything — full `_variables.yml` unchanged |
| **public** (opt-in) | `_site-public/` | Sensitive keys stripped; only public-safe fields remain |

Public-safe fields that are always kept: `company`, `position`, `slug`,
`status`, `date_found`, `date_applied`.

### Gitignore contract

Both output directories must be gitignored in the **user's repo** (the repo
that holds `private/applications/`).  Add these lines to `~/<your-repo>/.gitignore`:

```gitignore
# jobsmith site output — never commit rendered sites
_site/
_site-public/
```

The `_site/` directory is the default render destination and must be gitignored
unconditionally.  The `_site-public/` directory is also gitignored by default
even though it is sanitized — publishing is an intentional act that goes through
an explicit deployment step (e.g. `gh-pages` push or a static host upload), not
a `git push`.

### Public mode is explicit, not accidental

The `--public` flag on the site CLI (feat-9377b64d) is the only way to
produce a `_site-public/` render.  There is no automatic sanitization on
normal renders.  The sequence is:

1. `jobsmith assemble --all` refreshes `_variables.yml` for every application.
2. In public mode, `sanitize_variables(vars_dict, mode='public')` strips sensitive
   keys before the Quarto render step.
3. `quarto render` produces the sanitized site to `_site-public/`.
4. The full `_variables.yml` is restored so private state is not lost.

The sanitization function and the `SENSITIVE_KEYS` constant live in
`src/jobsmith/site.py` and are tested in `tests/test_site.py`.

### Never push `_site/` to a public host

Even if your hosting pipeline gives you the option, never point a public host at
the default `_site/` output.  Always re-generate with `--public` immediately
before deployment so you have an explicit audit trail of what was published.

---

## What this does NOT change

- **Existing `.apply-state/` contracts remain compatible.** No fields are renamed, removed, or restructured. New optional artifacts are added (`company-research.md`, `outreach-snippets.md`, per-application `_variables.yml`) but every specialist's existing output schema is preserved. Older applications missing the new artifacts render with empty sections rather than failing.
- **Specialist contracts stay version 1** — `apply-company-research` is an additive specialist, not a breaking change.
- **The Python package stays simple.** Three new modules (`assemble.py`, `company.py`, `site.py`); no architectural rewrite.
- **The plugin layer is unchanged.** Agents still dispatch via Claude Code's Task tool; the only diff is they write more state and assemble it into Quarto-friendly form.
