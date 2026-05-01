---
name: jobsmith-quarto
description: Authoring jobsmith application Quarto artifacts: resume.qmd (single-page Typst PDF via awesomecv-typst extension), cover-letter-draft.md (markdown with click-to-copy code blocks), and index.qmd review pages (Quarto website with partials, callouts, and per-company SCSS themes). Use when editing or generating any of these three surfaces inside a jobsmith application directory.
---

# jobsmith-quarto

This skill covers exactly the three Quarto surfaces that jobsmith uses: the
`resume.qmd` rendered to a single-page Typst PDF via the `awesomecv-typst`
extension, the `cover-letter-draft.md` rendered as markdown with a click-to-copy
body block, and the per-application `index.qmd` review page (a self-contained
Quarto website using partials, callouts, column layouts, and a per-company SCSS
theme chain). It intentionally excludes citations, cross-ref labels, tabsets,
code folding, diagrams, multi-format outputs, and every Quarto feature not
observed in jobsmith templates.

---

## Surface 1: Resume (resume.qmd → Typst → PDF)

### Overview

The resume uses the `awesomecv-typst` Quarto extension. The extension ships
under `shared/extensions/_extensions/awesomecv/` in the user's application repo.
Each per-application `documents/` directory symlinks `_extensions` back to that
shared location so the extension resolves at render time.

The canonical render command (run by `apply-resume-renderer`):

```bash
cd private/applications/{slug}/documents && \
  uv run quarto render resume.qmd --to awesomecv-typst
```

Output: `resume.pdf`. Gate: must be exactly **one page**.

### Frontmatter pattern

The resume templates in jobsmith use **no YAML frontmatter block**. All
Typst configuration is written inline via `{=typst}` raw blocks using
`awesomecv` Typst functions — not Quarto YAML keys. The `_resume-typst.qmd`
and `_resume-typst-simple.qmd` templates begin directly with Python code cells.

The awesomecv extension wires the format via its `_extension.yml`:

```yaml
# templates/extensions/_extensions/awesomecv/awesomecv/_extension.yml
title: Quarto-awesomecv-typst
quarto-required: ">=1.7.31"
contributes:
  shortcodes:
    - shortcodes.lua
  formats:
    typst:
      template-partials:
        - typst-template.typ
        - typst-show.typ
```

### Data loading pattern

Resume content lives in `data/resume-data.yml`, loaded via a Python code cell
with `#| include: false`:

```python
#| include: false
import yaml

with open('data/resume-data.yml', 'r') as f:
    resume_data = yaml.safe_load(f)

personal = resume_data['personal']
experience = resume_data['experience']
education = resume_data['education']
skills = resume_data['skills']
```

### resume-data.yml schema

```yaml
personal:
  firstname: "FIRSTNAME"
  lastname: "LASTNAME"
  email: "you@example.com"
  phone: "(+1) 000-000-0000"
  github: "your-github"
  linkedin: "your-linkedin"
  address: "City, ST, USA"
  positions: ["Senior Data Analyst", "Data Engineer"]
summary:
  default: "Professional summary: 2-3 sentences on impact."
experience:
  - id: "role-1"
    title: "Senior Data Analyst"
    company: "Company Name"
    location: "Remote"
    date: "Jan 24 - Present"
    bullets:
      - {id: "bullet-1", text: "Impact with metrics", tags: ["python"]}
education:
  - {degree: "M.S. in Field", school: "University", location: "City, ST", date: "Sept 20 - Sept 22"}
  - {degree: "B.S. in Field", school: "University", location: "City, ST", date: "Aug 15 - May 20",
     notes: ["Honors"]}
skills:
  programming: [Python, SQL]
  data_tools: [Tableau, Power BI]
```

### Layout primitives (awesomecv-typst)

The resume uses Typst functions from the awesomecv package. Two template
approaches coexist in jobsmith:

**Approach A — raw Typst block** (`_resume-typst.qmd`): Uses `{=typst}` raw
blocks with inline Python expressions for data substitution:

```markdown
```{=typst}
#import "@preview/awesomecv:0.1.0": *

#show: resume.with(
  author: (
    firstname: "`{python} personal['firstname']`",
    lastname: "`{python} personal['lastname']`",
    email: "`{python} personal['email']`",
    phone: "`{python} personal['phone']`",
    github: "`{python} personal['github']`",
    linkedin: "`{python} personal['linkedin']`",
    address: "`{python} personal['address']`",
    positions: `{python} tuple(personal['positions'])`,
  ),
  profile-picture: none,
  date: datetime.today().display(),
  language: "en",
  colored-headers: true,
  show-footer: false,
  paper-size: "us-letter",
)
```
```

Experience entries use `#resume-entry()` and `#resume-item[]`:

```typst
#resume-entry(
  title: "Senior Data Analyst",
  location: "Remote",
  date: "Jan 24 - Present",
  description: "Company Name",
)

#resume-item[
  - *Led* migration of ETL pipeline, reducing runtime by 40%
  - Built SQL framework serving 15 dashboards
]
```

**Approach B — output: asis** (`_resume-typst-simple.qmd`): Python cells with
`#| output: asis` print raw Typst markup. Same awesomecv functions, but
generated from Python loops rather than inline expressions:

```python
#| output: asis
for exp in experience:
    print(f"*{exp['title']}* | {exp['company']} | {exp['location']} | {exp['date']}")
    for bullet in exp['bullets']:
        text = bullet['text'].replace('$', '\\$')   # escape dollars for Typst
        print(f"- {text}")
    print()
```

Approach B is simpler to debug; Approach A is closer to Typst-native.

### Worked example (Approach B — simple)

The data-loading cell (Approach B) + the experience loop above are the complete
resume pattern. Full template: `templates/resume/_resume-typst-simple.qmd`.
The page-geometry defaults for awesomecv (`us-letter`, `1.5cm` margins) are set
inside `#set page(...)` in the `{=typst}` or `output: asis` block — not in YAML frontmatter.

### Common pitfalls

- **`$` in bullet text breaks Typst math mode.** Always `text.replace('$', '\\$')` before printing to Typst output. Forgetting this is the most common render failure.
- **Page count != 1 halts the pipeline.** `apply-resume-renderer` gates on exactly one page. If the resume spills to page 2, trim bullets — do not adjust margins below `1cm` (awesomecv minimum).
- **`_extensions` symlink must exist** in the `documents/` directory before render. The renderer agent checks and creates it, but if you're rendering manually, run: `ln -sf ../../../../shared/extensions/_extensions _extensions` from within `documents/`.
- **Inline Python expressions vs code cells.** In Approach A, `` `{python} expr` `` is inline; in Approach B, `#| output: asis` cells produce block output. Do not mix the two within a single `{=typst}` block — it won't parse.

---

## Surface 2: Cover letter (cover-letter-draft.md)

### Overview

The cover letter is plain markdown produced by `apply-cover-letter-writer` and
written to `private/applications/{slug}/cover-letter-draft.md`. It does NOT go
through Quarto rendering itself — it is either:

1. Copy-pasted by the user into a portal's single-text-field (the
   `_copy-paste.qmd` partial wraps it in a fenced code block for click-to-copy).
2. Referenced for a future PDF render step.

The click-to-copy display is handled by the **review page** (`index.qmd`), not
the cover letter file itself. The cover letter file is pure markdown prose.

### Structure of cover-letter-draft.md

```markdown
{firstname} {lastname}
**{position_title}**

{current_date}

**{hiring_manager}**
{company_name}
{company_location}

**RE: {position_title} Position**

Dear {first_name},

{opening paragraph — specific hook, role + what user uniquely brings}

{body 1 — strongest relevant experience, one concrete metric}

{body 2 — secondary angle, another metric if natural}

{close — forward-looking, what user brings to the specific problem}

Sincerely,
{firstname} {lastname}
{email} | {phone}
```

### How cover-letter-draft.md appears in index.qmd

The `_copy-paste.qmd` partial wraps the cover letter body for click-to-copy:

```markdown
# Copy-Paste Output {#sec-copy-paste}

::: {.callout-tip appearance="simple"}
The code block has Quarto's copy icon (top-right). Re-run `jobsmith assemble {slug}`
after editing `cover-letter-draft.md` to refresh.
:::

`````markdown
{{< include /_blocks/cover-letter-body.md >}}
`````
```

Quarto renders the nested fenced block as a non-executed code block. The
`code-copy: true` key in the review page frontmatter enables the copy icon.

### Length rules (enforced by apply-cover-letter-writer)

| Role type | Target words |
|---|---|
| Senior / strategic / AI-eng | 150 |
| IC portal | 120 |
| Maximum | 500 |

### Common pitfalls

- **Emoji in contact header** (`📧`, `📱`, `📍`) renders fine in markdown preview but may paste oddly into some portals. Prefer plain text separators (`|`) for portal-paste variants.
- **Never "I'm excited to apply"** or "I'm passionate about" as openers — these are blocked by the writer agent's hard rules and will be flagged in the fact-check gate.
- **The draft file is fact-check gated.** `jobsmith fact-check cover-letter-draft.md --verbose` must exit 0 before the file is considered final. Every metric, year count, dollar amount, and proper noun must trace to the user's master YAML.
- **`cover-letter-draft.md` is body content only** — the salutation and contact header for the PDF version come from the cover letter template; the draft file contains only the paragraphs.

---

## Surface 3: Per-app review page (index.qmd)

### Overview

Each application directory contains a self-contained Quarto website project.
The `index.qmd` is the review surface — it assembles all application artifacts
into a single navigable HTML page for pre-submission review.

Key architectural point: **each per-app directory is its own Quarto project**.
This is why `{{< include >}}` paths start with `/` (project-root-absolute).
The site-level aggregator (`templates/site/`) does not re-render these — it
reads their frontmatter via Quarto listings.

### Frontmatter pattern

```yaml
---
title: "Application Review"
author: "{{< var user.name >}}"
date: today
format:
  html:
    toc: true
    toc-depth: 3
    toc-location: left
    toc-title: "Contents"
    callout-appearance: simple
    embed-resources: true
    page-layout: full
    code-copy: true
---
```

Key fields:
- `embed-resources: true` — self-contained HTML (no external asset dir). Required because these pages live in private directories and may be opened without a server.
- `code-copy: true` — enables the copy icon on fenced code blocks (used by the copy-paste cover letter block).
- `page-layout: full` — removes the Quarto sidebar margins for full-width content.
- `callout-appearance: simple` — renders callouts without colored left-border boxes; keeps review pages low-noise.

### Per-application index.qmd frontmatter (apply-index-writer output)

The `apply-index-writer` agent writes a different set of frontmatter fields —
these are data fields consumed by the site-level listings page:

```yaml
---
title: "{position}"
company: "{company}"
position: "{position}"
location: "{location}"
salary-range: "{salary_range}"
job-url: "{apply_url}"
req-id: "{req_id}"
date-found: "{date}"
status: "materials-ready"
next-action: "Review resume and submit application"
---
```

These custom fields (`company`, `position`, `status`, `fit_score`, etc.) are
read by the site listings config via Quarto's frontmatter-extraction mechanism.

### Partials pattern

Partials live in `templates/partials/` and are included via project-root-absolute
paths (each per-app directory is its own Quarto project, so `/` resolves to
that directory's root):

```markdown
{{< include /_partials/_resume-preview.qmd >}}
{{< include /_partials/_copy-paste.qmd >}}
{{< include /_partials/_letter-draft.qmd >}}
{{< include /_partials/_jd-summary.qmd >}}
{{< include /_partials/_must-have-table.qmd >}}
{{< include /_partials/_why-work-here.qmd >}}
{{< include /_partials/_humanizer-audit.qmd >}}
{{< include /_partials/_outreach.qmd >}}
```

Partials themselves include pre-assembled block files from `/_blocks/`:

```markdown
{{< include /_blocks/must-have-table.md >}}
{{< include /_blocks/cover-letter-body.md >}}
```

The `/_blocks/` files are generated by `jobsmith assemble {slug}` from
`.apply-state/*.json` before Quarto renders.

### Variables pattern (`_variables.yml` + `{{< var >}}`)

The `jobsmith assemble {slug}` step writes `_variables.yml` into the per-app
directory. Partials reference it via `{{< var >}}` shortcodes:

```markdown
::: {.callout-note appearance="minimal"}
**{{< var company >}}** — {{< var position >}}\
{{< var location >}} ({{< var location_type >}}) · {{< var salary_range >}} · req `{{< var req_id >}}`\
[Apply URL]({{< var apply_url >}})
:::
```

User identity variables (`user.name`, `user.email`) are set in the user repo's
`_variables.yml` and cascade to every page.

### Divs and callouts for review blocks

jobsmith uses three callout types in review pages:

**`.callout-note appearance="minimal"`** — informational metadata blocks
(JD summary, source artifact table):

```markdown
::: {.callout-note appearance="minimal"}
**Anthropic** — Research Engineer, Interpretability\
San Francisco, CA (Hybrid) · $200k–$280k · req `RE-INTERP-042`
:::
```

**`.callout-tip appearance="simple"` or `appearance="minimal"`** — fit-score
display, action prompts, copy-paste instructions:

```markdown
::: {.callout-tip appearance="simple"}
The code block has Quarto's copy icon (top-right). Re-run `jobsmith assemble {slug}`
after editing `cover-letter-draft.md` to refresh.
:::
```

**`.callout-warning appearance="minimal"`** — stale-data or review-surface
warnings:

```markdown
::: {.callout-warning appearance="minimal"}
This is a **review surface** generated from `.apply-state/`. Edit the underlying
state files and re-run `jobsmith assemble {slug}` to refresh.
:::
```

### Column layouts for Key Requirements

The `apply-index-writer` uses Quarto's multi-column div for Must Have / Nice to
Have split:

```markdown
::: {.columns}
::: {.column width="50%"}

### Must Have

- [x] 6+ years data engineering — **STRONG**
- [x] Python + SQL — **HAVE**
- [ ] Spark at scale — **PARTIAL**

:::
::: {.column width="50%"}

### Nice to Have

- [ ] Rust or Go systems experience
- [ ] Prior startup (Series B+)

:::
:::
```

### Per-company SCSS theme chain

Each per-application review page uses a two-file SCSS cascade: a base theme
(`default.scss` or `jobsmith.scss`) plus a company-specific override. The theme
chain is set in the per-app `_quarto.yml`:

```yaml
format:
  html:
    theme:
      - ../../shared/styles/default.scss
      - ../../shared/styles/companies/anthropic.scss
```

Or as absolute paths resolved by the Quarto project root:

```yaml
format:
  html:
    theme:
      - cosmo
      - styles/jobsmith.scss
```

**SCSS contract** — every company theme must define these four variables (the
base `default.scss` provides the neutral slate defaults):

```scss
$jobsmith-primary:      #CC785C;   // primary brand color (headings, accents)
$jobsmith-primary-bg:   #F5E8E1;   // light tint (callout backgrounds)
$jobsmith-accent-color: #181818;   // secondary accent (borders, highlights)
$jobsmith-accent-bg:    #F5F0EB;   // light tint (blockquote backgrounds)
```

The base theme then applies them:

```scss
h1, h2 {
  border-left: 4px solid $jobsmith-primary;
  padding-left: 0.5rem;
}
blockquote {
  border-left: 3px solid $jobsmith-accent-color;
  background-color: $jobsmith-accent-bg;
}
.callout-tip {
  border-left-color: $jobsmith-primary !important;
}
```

Company SCSS files are in `templates/themes/companies/`: `anthropic.scss`,
`google.scss`, `amazon.scss`, `apple.scss`, `microsoft.scss`, `netflix.scss`,
`openai.scss`, `stripe.scss`, `pwc.scss`, `schneider-electric.scss`.

To add a new company: copy `default.scss`, rename, update the four `$jobsmith-*`
variables to match the brand palette.

### Site-level listings aggregator

The aggregator site (`templates/site/`) reads frontmatter from per-app
`index.qmd` files via Quarto listings — it does **not** re-render them:

```yaml
# templates/site/_quarto.yml
project:
  type: website
  output-dir: _site
  render:
    - "index.qmd"   # only renders the listings page

format:
  html:
    theme:
      - cosmo
      - styles/jobsmith.scss
    embed-resources: false
```

```yaml
# templates/site/index.qmd frontmatter
listing:
  id: applications
  contents:
    - private/applications/*/index.qmd
  type: table
  fields: [company, position, status, fit_score, date_found]
  sort:
    - "fit_score desc"
    - "date_found desc"
  filter-ui: [company, position, status]
  table-striped: true
  table-hover: true
  page-size: 50
```

### Worked example (index.qmd review page)

```markdown
---
title: "Application Review"
author: "{{< var user.name >}}"
date: today
format:
  html:
    toc: true
    toc-depth: 3
    toc-location: left
    toc-title: "Contents"
    callout-appearance: simple
    embed-resources: true
    page-layout: full
    code-copy: true
---

# Anthropic — Research Engineer, Interpretability

::: {.callout-warning appearance="minimal"}
This is a **review surface** generated from `.apply-state/`. Edit the underlying
state files and re-run `jobsmith assemble anthropic-research-engineer-interp` to refresh.
:::

# Submission artifacts

{{< include /_partials/_resume-preview.qmd >}}

{{< include /_partials/_copy-paste.qmd >}}

{{< include /_partials/_letter-draft.qmd >}}

---

# Workflow review

{{< include /_partials/_jd-summary.qmd >}}

{{< include /_partials/_must-have-table.qmd >}}

{{< include /_partials/_why-work-here.qmd >}}

{{< include /_partials/_humanizer-audit.qmd >}}

{{< include /_partials/_outreach.qmd >}}
```

### Common pitfalls

- **`{{< include >}}` paths must be project-root-absolute** (starting with `/`). Each per-app dir is its own Quarto project — paths like `../../partials/_foo.qmd` won't resolve. Always use `/_partials/_foo.qmd`.
- **`embed-resources: true` is required** for per-app pages. Without it, Quarto writes assets to a `{slug}_files/` sibling directory, which breaks when pages are opened directly from the filesystem.
- **`_variables.yml` must exist before render.** Run `jobsmith assemble {slug}` first. If `{{< var company >}}` renders as literal text, the variables file is missing or stale.
- **Do not render per-app pages from the site project.** The site `_quarto.yml` sets `render: ["index.qmd"]` — only the listings page. Per-app pages render independently: `quarto render private/applications/{slug}`. Mixing the two breaks project-root-absolute include paths.
- **Company SCSS must define all four `$jobsmith-*` variables** or the base theme's rules will fail to compile with `undefined variable` errors.
