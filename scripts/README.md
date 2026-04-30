# Scripts

Python utilities used by the agents.

## Files

- **`anchor_bullet_guard.py`** — mechanical guardrail that imports `_MONEY_RE` and `_PERCENT_RE` from `fact_check_draft.py`, identifies anchor bullets in master `work.yml` (≥$10M, ≥50%, ≥100K-asset), and cross-references against a `bullet-selection.json` produced by `apply-bullet-selector`. Exit codes:
  - `0` — all anchors preserved or all drops have logged reasons
  - `1` — anchor dropped without a logged reason → caller must invoke `apply-relevance-inquirer`
  - `2` — internal error
- **`fact_check_draft.py`** — scans a draft markdown file for hard claims (numbers, dates, proper nouns) and verifies each appears verbatim in the master YAML files. Used as a blocking gate before the cover-letter draft is written. Exit non-zero on any unverified claim.

## Pending

- **`jd_similarity.py`** — Jaccard-on-top_keywords + role_type hard gate, computing similarity between an incoming JD and prior applications. Foundation for the reuse-detector specialist (0.2).
- **`vocab_mismatch_scanner.py`** — surfaces old-JD-flavored bullet text in copied `bullet-selection.json` `rephrased` fields when running the light-edit path (0.2).
- **`corpus_backfill.py`** — populates `.apply-state/jd-parsed.json` for prior applications by re-running `apply-jd-parser` on each application's `index.qmd` URL or extracted JD text (0.2).

## Conventions

- Run via `uv run python scripts/<name>.py` (uv is the canonical Python runner)
- Exit codes are load-bearing — the orchestrator branches on them
- Scripts read state from `.apply-state/` and write back to it; they don't touch master YAMLs
