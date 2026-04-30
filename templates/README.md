# Templates

Quarto + Typst rendering templates.

**Status:** Pending extraction from `shakestzd/shared/templates/` and `shakestzd/shared/extensions/`. See [ROADMAP.md](../ROADMAP.md).

## Subdirectories

- **`resume/`** — `awesomecv-typst` Quarto extension + a sample `resume.qmd` that includes `work.yml`, `skill.yml`, `education.yml` and renders to a 1-page PDF.
- **`cover-letter/`** — Careerfair.io 5-component cover letter workflow QMD that walks the user through job analysis, requirements-to-qualifications match, why-do-you-want-to-work-here research, the 5-component letter draft, the humanizer pass, and copy-paste output.

## Rendering

Both templates target Quarto's `awesomecv-typst` format and produce single-page PDFs at us-letter, 1-inch margins, 11pt New Computer Modern. Symlink `_extensions → ../../../shared/extensions/_extensions` is required in any application's `documents/` directory for Quarto to find the extension.
