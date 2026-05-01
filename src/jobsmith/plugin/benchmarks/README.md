# jobsmith — Generic Benchmark Files (Pat Doe)

This directory contains generic "Pat Doe" style-reference files used as
fallbacks when no personal benchmarks are configured.  They represent a
fictional data engineer with neutral, explorer-voice writing and solid
quantified bullets — the style target jobsmith aims for.  These files are
**never copied into a real application**; they exist solely so quality checks
can run on a fresh install before you wire up your own references.

## Overriding with your own benchmarks

Point `benchmarks:` in your `.apply-config.yaml` at files under
`private/benchmarks/` (symlinks to your best previous application work well):

```yaml
benchmarks:
  resume_qmd:       private/benchmarks/resume.qmd
  resume_pdf:       private/benchmarks/resume.pdf
  cover_letter_md:  private/benchmarks/cover-letter.md
  required: false   # set true once you have all five files in place
```

Run `jobsmith doctor` to confirm all paths resolve, then run
`jobsmith init` in a fresh repo to scaffold the `private/benchmarks/`
directory with a README explaining what to put there.
