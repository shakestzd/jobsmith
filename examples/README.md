# Examples

Sample master YAMLs and sample application output.

**Status:** Pending. See [ROADMAP.md](../ROADMAP.md).

## Subdirectories

- **`master-yaml/`** — Sanitized sample `work.yml`, `skill.yml`, `education.yml`, `author.yml`, `publication.yml` files. Use these as a starting point for your own master profile. The samples are based on a fictional "Pat Doe" data engineer profile; replace with your real content.
- **`applications/`** — One end-to-end sample: a job URL, the resulting `jd-parsed.json`, the rendered `resume.pdf`, and the cover letter draft. Useful for understanding what the pipeline produces before you run it on a real application.

## Why fictional samples

The master YAML format is opinionated about anchor bullets (≥$10M, ≥50%, ≥100K-asset). Real master content is the user's biographical data and shouldn't be in the framework repo. The fictional examples let new users see the shape without committing personal data into the framework codebase.
