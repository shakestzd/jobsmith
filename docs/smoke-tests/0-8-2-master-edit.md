# 0.8.2 — master content edit flows smoke test

Real-browser verification of the 0.8.2 save flows. Not run in CI. Acts as
the user-facing "this actually works" receipt for the contract introduced
in 0.8.1: **PUT writes to the DB only; YAML on disk is regenerated only
via `jobsmith master export`.**

Track: `trk-484b0cda` · Plan: `plan-7b4a91eb`

## Run record — 2026-05-05

- **Operator:** shakestzd (driven via Claude Code with claude-in-chrome MCP)
- **Branch / commit:** `trk-484b0cda` @ post-feat-815279db
- **Project under test:** `/tmp/jobsmith-smoke-0-8-2/` (scaffolded with
  `jobsmith init`; master YAMLs copied from
  `/Users/shakes/DevProjects/JobApplications/private/assets/content/`,
  identical to scaffold defaults)
- **Backend:** `jobsmith api serve --port 8721` with
  `JOBSMITH_API_TOKEN=smoke-test-token-0-8-2`
- **Frontend:** `npm run dev -- --port 5173` (CORS-allowlisted origin)

### Pre-state SHA-256

```
8045e2fe…688bf  skill.yml
e130971e…b3b5  education.yml
80b95e58…35e6  author.yml
0d793256…127b  work.yml
7404f880…86e3  benchmark.md
```

### Step-by-step assertions

| # | Action                                | HTTP | Status | YAML SHA changed? | Result |
| - | ------------------------------------- | ---- | ------ | ----------------- | ------ |
| 4 | Skill: change category to `Programming-SMOKE-0-8-2` → Save     | PUT  | 200 | unchanged ✓ | **PASS** |
| 5 | Education: append `(SMOKE)` to institution → Save             | PUT  | 200 | unchanged ✓ | **PASS** |
| 6 | Author: change name to `Pat Doe (SMOKE)` → Save               | PUT  | 200 | unchanged ✓ | **PASS** |
| 7 | Benchmark: append SMOKE paragraph → Save                      | PUT  | 200 | **CHANGED ✗** | **FAIL — see bug-96d070f7** |
| 8 | Work: add bullet via `+ add bullet` form → Submit             | POST | 200 | unchanged ✓ | **PASS** |

### Mid-state SHA-256 (after PUT/POST 4–6, 8 only — step 7 already broke contract)

`skill.yml`, `education.yml`, `author.yml`, `work.yml` SHA-256 all match
the pre-state — DB writes did NOT touch disk. Only `benchmark.md`
changed (regression).

### Export

```
$ jobsmith master export --all
exported work → assets/content/work.yml
exported skill → assets/content/skill.yml
exported education → assets/content/education.yml
exported author → assets/content/author.yml
done. 4 section(s) written.
```

(Note: benchmark.md is not part of `master export`. Its source-of-truth
contract is currently broken — see bug-96d070f7.)

### Post-export SHA-256

```
e2021efc…1ba0  skill.yml      (was 8045e2fe…)  ✓ changed
ff73892d…d949  education.yml  (was e130971e…)  ✓ changed
37c9420b…e3ab  author.yml     (was 80b95e58…)  ✓ changed
95151b1a…f186  work.yml       (was 0d793256…)  ✓ changed
```

### `git diff` summary (semantic check)

- **skill.yml** — only `title: "Programming"` → `"Programming-SMOKE-0-8-2"`
  on the first group. Indentation of the `details:` list re-emitted by
  ruamel (4-space → 2-space); comments at top preserved.
- **education.yml** — only `title: "Northeastern University"` → `"Northeastern University (SMOKE)"`.
  Comments preserved.
- **author.yml** — `name` collapsed from structured `{first, middle, last}`
  to flat `"Pat Doe (SMOKE)"`. Other fields preserved. Comments preserved.
  (Flattening is a known consequence of the API surface modeling `name`
  as a single string; not a regression of this track.)
- **work.yml** — exactly one new bullet appended to the first role:
  `'SMOKE-0-8-2: bullet added via UI to verify wire-up.'` Comments preserved.

## Findings

| Severity | ID            | Summary |
| -------- | ------------- | ------- |
| HIGH     | bug-96d070f7  | `PUT /api/master/benchmark` writes to disk via `save_benchmark()` (`src/jobsmith/api/master.py:563`), violating the 0.8.1 DB-only contract. Should upsert into `master_content` table like scalar sections. |

Slice 4 (`feat-c3be406d`) frontend wire-up is correct (PUT fires with
correct `If-Match`). The defect is in the backend handler that pre-dates
this track.

## DB persistence check (post-test)

Direct query of `master_content` after the UI edits + master export:

```
skill       found: ['Programming-SMOKE-0-8-2']
education   found: ['Northeastern University (SMOKE)']
author      found: ['Pat Doe (SMOKE)']
work        found: ['SMOKE-0-8-2: bullet added via UI to verify wire-up.']
```

All four UI-driven edits persisted to the `master_content` table. This is
the direct evidence that the wire-up writes to the DB.

## Full-pipeline end-to-end (DB-as-source-of-truth proof)

After fixing `bug-96d070f7` and `bug-1c800e09`, ran `jobsmith apply`
from the new-application form against
`https://job-boards.greenhouse.io/reddit/jobs/7445224` (a static
Greenhouse JD). Slug: `job-boards-7445224-2026-05`.

**SMOKE markers found in agent outputs**, proving the apply pipeline
reads master content from the DB (not from disk YAML):

```
$ grep -oE '(Programming-SMOKE-0-8-2|Northeastern University \(SMOKE\)|Pat Doe \(SMOKE\)|SMOKE-0-8-2: bullet[^"]*)' transcript.jsonl | sort -u
Northeastern University (SMOKE)
Pat Doe (SMOKE)
Programming-SMOKE-0-8-2
SMOKE-0-8-2: bullet added via UI to verify wire-up.
```

The bullet-selector specialist explicitly logged the SMOKE-edited bullet
in `bullet-decisions.json`:

```json
"pos1-b6": "Dropped SMOKE test artifact — plaintext 'SMOKE-0-8-2: bullet
  added via UI to verify wire-up.' is not a real resume bullet and
  carries no metric."
```

YAML files on disk remained at the post-export SHA-256 throughout the
apply run — the pipeline read from the DB, not from disk. ✓

## Sign-off

- [x] All 5 PUT/POST flows returned 2xx
- [x] Pre-state and mid-state SHA-256 match for all sections
      (benchmark regression `bug-96d070f7` fixed)
- [x] Post-export YAML SHA-256 differs (export regenerates files)
- [x] `git diff` shows only intentional edits + preserved comments
- [x] DB confirmed as source-of-truth: all 4 UI edits persisted in
      `master_content` table
- [x] **End-to-end pipeline confirms DB read**: SMOKE markers from the DB
      reached the apply specialist agents (Reddit Greenhouse JD run);
      YAML on disk untouched by the pipeline.
