# Contributing

**Status:** jobsmith is mid-extraction (see ROADMAP.md). External contributions are not yet accepted because the codebase shape is still in flux.

## When this opens up

After 0.3.0 ships and the plugin has a stable shape. Watch the repo for the milestone or check ROADMAP.md.

## Until then

If you have ideas, open an issue with `[discussion]` in the title. Particularly welcome:

- Use cases that don't fit the current opinionated pipeline
- Edge cases in master YAML structure (multi-stint roles, gap years, sabbaticals, parallel positions)
- Voice considerations for non-US English markets
- Other ATS systems' parsing quirks
- Cover letter framework alternatives to careerfair.io

## Principles (so you know what kind of changes will land)

- **Master is read-only** — no PR will land that mutates the user's source-of-truth YAML
- **No fabrication** — no PR will land that makes the pipeline invent claims, metrics, or proper nouns
- **Halt over guess** — if a JD requirement has no master coverage, the pipeline must halt, not improvise
- **Anchors are sacred** — load-bearing metrics (≥$10M, ≥50%, ≥100K-asset) are preserved unless explicitly logged otherwise
- **Voice is the user's** — scaffolding helps; overriding their voice does not

If your idea conflicts with one of these, that's fine — open the discussion issue and we'll talk through it.
