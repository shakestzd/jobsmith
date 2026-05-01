# Render benchmarks and feedback loop

This document covers two runtime-quality mechanisms added in wave 2 (v0.5):

- **Quality benchmarks** — per-user style-reference files that specialists read
  to calibrate voice, rhythm, and page-fit before drafting.
- **Feedback loop** — a diff-capture system that records user edits to agent
  drafts as structured lessons, so future runs can learn from them.

For the broader pipeline architecture (specialist topology, phase boundaries,
config surface), see [architecture.md](architecture.md).

---

## Quality benchmarks

### What benchmarks are

A benchmark is a past application file you are proud of. It functions as a
**STYLE reference** — voice, rhythm, paragraph cadence, bullet density, page-fit
instinct. It is never a source of fact.

Specifically, benchmarks teach:

- How you write: sentence length, opening energy, word-choice register.
- How your pages are laid out: margin density, section ordering, bullet
  compactness, how tightly a full-page resume feels.
- How your cover letter is structured: paragraph count, hook style, 5-component
  balance, salutation tone.

Benchmarks do **not** supply:

- Dollar amounts, percentages, year counts, or asset counts.
- Company names, institution names, proper nouns, or project names.
- Any claim that could land in the rendered document.

The master YAML (`assets/content/*.yml`) is the sole source of fact for every
application. A specialist that copies a metric from a benchmark is treated as if
it fabricated that metric — both result in a pipeline halt.

### Where benchmarks live

Benchmark files live under `private/benchmarks/` in your application repo.
The recommended pattern is a symlink pointing to the best version of a past
application:

```
private/
  benchmarks/
    resume.qmd            -> ../applications/acme-senior-de-2024/documents/resume.qmd
    resume.pdf            -> ../applications/acme-senior-de-2024/documents/resume.pdf
    cover-letter.md       -> ../applications/acme-senior-de-2024/documents/cover-letter-final.md
    cover-letter.pdf      -> ../applications/acme-senior-de-2024/documents/cover-letter-final.pdf
    workflow.html         -> ../applications/acme-senior-de-2024/_site/index.html
    README.md             # written by `jobsmith init`
```

Symlinks work well because you can update the target once and all five benchmark
files update together. An alternative is to copy the files directly if you prefer
an immutable snapshot.

`jobsmith init` creates `private/benchmarks/README.md` with a reference table
and wiring instructions. Run `jobsmith doctor` to verify that all configured
paths resolve.

The `private/benchmarks/` directory is listed in `.gitignore` by default (added
by `jobsmith init`), so your personal files stay local.

### Config surface

Benchmarks are wired in `.apply-config.yaml` under the `benchmarks:` key.
The underlying Pydantic model is `BenchmarkConfig` in `src/jobsmith/config.py`.

Fields:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `resume_pdf` | path or null | `null` | Rendered PDF for visual-layout density calibration |
| `resume_qmd` | path or null | `null` | Quarto source for prose-writer voice calibration |
| `cover_letter_md` | path or null | `null` | Markdown source for cover-letter-writer voice calibration |
| `cover_letter_pdf` | path or null | `null` | Rendered PDF (optional — reserved for future wave) |
| `workflow_html` | path or null | `null` | Rendered workflow review page (reserved for future wave) |
| `required` | bool | `false` | When `true`, missing user benchmarks halt the pipeline instead of falling back |

Example configuration:

```yaml
benchmarks:
  resume_qmd:      private/benchmarks/resume.qmd
  resume_pdf:      private/benchmarks/resume.pdf
  cover_letter_md: private/benchmarks/cover-letter.md
  # cover_letter_pdf and workflow_html are optional — omit until you have them
  required: false
```

All paths are relative to the repo root. When a path is set but the file is
missing, the pipeline reports an error during preflight (`jobsmith doctor`).

### Fallback behavior

When a benchmark path is `null` (not set), the relevant specialist falls back to
the generic "Pat Doe" files shipped inside the plugin at
`src/jobsmith/plugin/benchmarks/`. These are intentionally neutral — they
demonstrate correct structure without carrying any real candidate's voice.

Set `required: true` once you have all five files in place. This turns a silent
fallback into a hard halt, ensuring no application runs against generic style
references once you have personal ones wired up.

### Which specialists consume benchmarks

Three specialists read benchmark files during a `/apply` run:

**`apply-prose-writer`** reads `benchmark_resume_qmd` (from `benchmarks.resume_qmd`).
It uses the `.qmd` source as a voice and rhythm exemplar: sentence length,
Professional Summary length, bullet density, and page-fit instinct. Hard rule:
no metric, company name, or claim from the benchmark may appear in the draft.

**`apply-cover-letter-writer`** reads `benchmark_cover_letter_md` (from
`benchmarks.cover_letter_md`). It uses the markdown file to calibrate the
5-component hook-body-body-close structure, paragraph length, opening energy,
and salutation formality. Same hard rule applies: benchmark is voice only.

**`apply-visual-layout-reviewer`** reads `benchmark_resume_pdf` (from
`benchmarks.resume_pdf`). It uses the rendered PDF to set layout targets —
target page count and text-to-whitespace density ratio. When a benchmark PDF
is present, the specialist measures density against it rather than using internal
one-page heuristics. Content of the PDF is not read — only page geometry.

See the specialist prompt files for the full benchmark sections:

- `src/jobsmith/plugin/agents/apply-prose-writer.md` — `## Benchmark style reference`
- `src/jobsmith/plugin/agents/apply-cover-letter-writer.md` — `## Benchmark style reference`
- `src/jobsmith/plugin/agents/apply-visual-layout-reviewer.md` — `## Benchmark layout reference`

### Read-only style contract

The benchmark contract is enforced by the specialist prompts, not by the
Python layer. The contract has two parts:

1. Benchmarks are read-only. No specialist writes to `private/benchmarks/`.
2. Benchmarks are style references only. Any metric, claim, company name, or
   proper noun that can be traced to a benchmark and not to master YAML is
   treated as fabrication and triggers a halt.

The master YAML (`assets/content/*.yml`) remains the sole source of fact.
This contract cannot be relaxed via config.

### Privacy note

`private/benchmarks/` is gitignored by default (`jobsmith init` adds the rule).
Your personal application files never leave your local machine unless you
explicitly remove that gitignore entry.

---

## Feedback loop

### What the feedback loop is

After a `/apply` run, you may hand-edit the agent's output before submitting.
The feedback loop captures those edits as structured JSON records so future
runs can account for your preferences.

Specifically:

- If you edit `private/applications/{slug}/documents/prose-draft.md` after the
  pipeline writes it, the diff is recorded as `prose-bullet` records.
- If you edit `private/applications/{slug}/documents/cover-letter-final.md`
  after the pipeline writes it, the diff is recorded as
  `cover-letter-paragraph` records.

The records are stored in `private/feedback/` as timestamped JSON files.

### Running the feedback subcommand

The `jobsmith feedback` subcommand has four operations:

#### `jobsmith feedback record <slug>`

Diffs the user's final version of each document against the agent's original
draft and writes JSON records for each significant change.

```bash
jobsmith feedback record acme-senior-de-2024
```

The command looks for:

- `prose-draft.md` (user version) vs `prose-draft-agent.md` (agent snapshot)
- `cover-letter-final.md` (user version) vs `cover-letter-agent.md` (agent snapshot)

A change is "significant" if it alters more than 5 characters and is not
whitespace-only. Insignificant diffs (trimming trailing spaces, minor
reformatting) are silently skipped.

Records are written to `private/feedback/<slug>-<iso-timestamp>.json`.

#### `jobsmith feedback list`

Displays a Rich table of all feedback records in `private/feedback/`.

```bash
jobsmith feedback list
jobsmith feedback list --kind prose-bullet
jobsmith feedback list --since 30d
jobsmith feedback list --since 2025-01-01
```

Options:

- `--kind` — filter to `prose-bullet` or `cover-letter-paragraph`
- `--since` — accepts an ISO date string or `Nd` shorthand (e.g. `30d` = last
  30 days)

#### `jobsmith feedback prune`

Deletes records older than a threshold, based on file mtime.

```bash
jobsmith feedback prune --older-than 90d
```

The `--older-than` option accepts `Nd` shorthand (e.g. `90d`). Default: `90d`.

#### `jobsmith feedback export`

Produces a sanitized YAML summary of recurring patterns, safe for
cross-machine sync or sharing.

```bash
jobsmith feedback export
jobsmith feedback export --out private/feedback-export.yaml
```

The export groups records by `kind` and emits lesson texts. Slug names,
company names, and all per-application context are stripped. The output
contains only pattern frequencies and lesson strings — no information that
identifies a specific application or employer.

With `--out PATH`, the YAML is written to the given file. Without it, the
YAML is printed to stdout.

### Record schema

Each feedback record written to `private/feedback/` is a JSON file with this
shape:

```json
{
  "slug": "acme-senior-de-2024",
  "timestamp": "2025-04-15T09:23:01.412+00:00",
  "kind": "prose-bullet",
  "before": "Built 7 ETL pipelines (Python, DLT, DuckDB) delivering portfolio KPIs...",
  "after": "Built 7 ETL pipelines (Python, DLT, DuckDB) for 500K assets with 99.9% uptime.",
  "lesson": "",
  "context": null
}
```

Fields:

| Field | Description |
|---|---|
| `slug` | Application directory name under `private/applications/` |
| `timestamp` | UTC ISO-8601 string — when `feedback record` was run |
| `kind` | `prose-bullet` or `cover-letter-paragraph` |
| `before` | The agent's original text |
| `after` | Your edited version |
| `lesson` | A string describing what rule the edit implies — see note below |
| `context` | Reserved for future use; currently `null` |

#### The `lesson` field in v0.5

In v0.5, `lesson` is always an empty string. The placeholder is intentional —
the function `lesson_placeholder(before, after)` in `src/jobsmith/feedback.py`
has a stable signature but returns `""` until wave 3.

Wave 3+ will auto-suggest a lesson string by diffing `before` and `after`
and inferring a rule (e.g. "avoid passive voice in bullet openers" or "trim
trailing metric context to fit line budget"). Until then, you can fill the
`lesson` field manually by editing the JSON file after running `feedback record`.

### Read-back integration (wave 3 preview)

In wave 3, `apply-prose-writer` and `apply-cover-letter-writer` will read
recent feedback records as soft lessons before drafting. Records with non-empty
`lesson` strings will surface as voice constraints alongside the voice guide.

This integration is owned by wave 3 specialist edits (F4). The record schema
and subcommand surface described above are stable inputs to that work.

Cross-references:

- `src/jobsmith/plugin/agents/apply-prose-writer.md` — will gain a
  `## Feedback lessons` section in wave 3
- `src/jobsmith/plugin/agents/apply-cover-letter-writer.md` — same

### Privacy

`private/feedback/` is listed in `.gitignore` by default (added by
`jobsmith init` alongside `private/benchmarks/`). Records contain slug names
and `context` fields that may indirectly identify applications, so the
directory is treated as private data.

To sync lessons across machines, use `jobsmith feedback export`. The export
output strips slug names, company names, and context. It is safe to commit to
a private dotfiles repo or share with a trusted collaborator.

The sanitization happens in `feedback.export()` in `src/jobsmith/feedback.py`:
records are grouped by kind, only `lesson` strings are carried forward, and all
per-application metadata is dropped before the YAML is serialized.
