# Getting Started

**Status:** This document is a placeholder for 0.1. The actual install and run flow requires extraction work captured in [ROADMAP.md](../ROADMAP.md). Below is the shape of what the experience will be once 0.1 is complete.

---

## Install (planned)

```bash
# As a Claude Code plugin:
claude plugin install github.com/shakestzd/jobsmith

# Or clone and link locally:
git clone https://github.com/shakestzd/jobsmith ~/.claude/plugins/jobsmith
```

## Set up your master YAML (planned)

```bash
# In your application-tracking repo:
mkdir my-job-search && cd my-job-search
jobsmith init
# Writes assets/content/{work,skill,education,author,publication}.yml stubs
# Writes .apply-config.yaml with default paths
# Writes .gitignore additions for .apply-state/, applications/
```

Edit the generated YAML files with your real work history. Master is read-only from the pipeline — the only way to change what appears on a tailored resume is to edit master.

### Anchor bullets

When writing your work history, mark bullets that contain load-bearing metrics — money amounts ≥$10M, percentage gains ≥50%, or portfolio scale ≥100K assets. The pipeline will preserve these across applications unless you log a reason to drop them.

The regex catches dollar amounts, percentages, and asset counts automatically. You don't need to mark them by hand — write the bullet naturally and the anchor guard will detect it.

## Run /apply against a real job (planned)

```bash
# In your application-tracking repo:
claude
> /apply https://example.com/jobs/12345
```

The pipeline will:

1. Parse the JD into structured fields
2. Score fit against your master YAML (must-have table, GAP/PARTIAL/STRONG)
3. Pause for your confirmation before generating files
4. Select bullets from your master, preserving anchors
5. Write a Professional Summary tailored to the JD
6. Render a 1-page Quarto+Typst PDF
7. Validate ATS parseability and portfolio URL coverage
8. Review the layout for orphan words / widow lines / overflow
9. Draft a 5-component careerfair.io-derived cover letter
10. Run a humanizer pass to scrub AI tells
11. Write an `index.qmd` summarizing the application
12. Log to a local SQLite DB for tracking

Output lives at `private/applications/<company-slug>-<role-slug>/`.

## Configuration (planned)

`.apply-config.yaml` controls paths and behavior. Defaults are sensible; override only what you need.

```yaml
master:
  work_yml: assets/content/work.yml
  skill_yml: assets/content/skill.yml
  education_yml: assets/content/education.yml
  author_yml: assets/content/author.yml
  publication_yml: assets/content/publication.yml

output:
  applications_dir: private/applications/

voice:
  voice_guide_path: ~/.claude/memory/feedback_writing_voice.md  # optional
  banned_words:
    - Architected
    - Leveraged
    - Orchestrated
    - Spearheaded

anchor_thresholds:
  money_min_usd: 10000000      # $10M
  percent_min: 50.0            # 50%
  asset_count_min: 100000      # 100K

cover_letter:
  framework: careerfair-io     # or: minimal | none
  word_targets:
    senior_strategic: 150
    ai_engineer: 150
    ic_portal: 120
```

## What this won't do

- Won't apply to a job for you. The pipeline produces materials; you submit.
- Won't invent claims you haven't lived. If a JD requires something not in your master, the pipeline halts and asks.
- Won't override your written voice. Cover letter scaffolding is an option, not a mandate.
- Won't track recruiter conversations or LinkedIn outreach (use a separate tool — jobsmith stays focused on application materials).
