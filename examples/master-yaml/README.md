# Sample master YAML — fictional "Pat Doe" profile

A complete master profile for a senior data engineer with renewable-energy and tax-equity finance experience. Use this as a starting template — copy the structure to your own application repo and replace with your real content.

## Files

| File | Purpose |
|---|---|
| `work.yml` | Work history with anchor bullets (≥$10M, ≥50%, ≥100K) marked naturally in prose |
| `skill.yml` | Skill categories (5 groups, ~30 skills) — order is your default; bullet-selector reorders per JD |
| `education.yml` | Education with thesis + honors |
| `author.yml` | Resume header block (name, contact, links) + optional per-role-type taglines |
| `publication.yml` | Optional — papers, talks, public projects |

## Anchor bullet examples in this profile

Pat Doe's work history demonstrates how anchors look in practice:

- **`$250M`** ITC unlock from geospatial platform → preserved across all data-engineer applications
- **`500K-asset`** portfolio → preserved
- **`$50M`** incremental tax credits → preserved
- **`$4.25B`** FMV optimizer → preserved
- **`75%`** AP processing time reduction → preserved
- **`$95M`** revenue recovery → preserved
- **`$1B+`** renewable energy fund → preserved
- **`$1M`** interest revenue + **`$51M`** non-compliant systems → preserved

When jobsmith tailors a resume, anchor-bullet-guard ensures these stay in unless `bullet-selection.json` logs an explicit reason to drop one (e.g., "this anchor's domain is off-thesis for this role").

## Replacing with your own

1. Copy this directory to your application repo at `assets/content/`
2. Replace every Pat Doe-specific detail with yours
3. Keep the YAML structure identical — the specialists rely on the field names
4. If you have anchor metrics (load-bearing dollar amounts, percentages, scale numbers), write them into bullets naturally — the regex catches them automatically. No manual marking needed.

## Don't have anchor-tier metrics yet?

That's fine. The pipeline still works without anchors — it just won't have the "anchor preservation" safety net for those bullets. Most applicants accumulate anchor-tier achievements over 3-5 years of work; if you're earlier in your career, the bullet-selector will rank-order based on JD alignment alone.
