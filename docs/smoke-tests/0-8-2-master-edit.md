# 0.8.2 — master content edit flows smoke test

Real-browser verification of the 0.8.2 save flows. Not run in CI. Acts as
the user-facing "this actually works" receipt for the contract introduced
in 0.8.1: **PUT writes to the DB only; YAML on disk is regenerated only
via `jobsmith master export`.**

Track: `trk-484b0cda` · Plan: `plan-7b4a91eb`

## Flow under test

1. Seed `master_content` table from a known fixture
   (`jobsmith db load-master`).
2. Snapshot SHA-256 of
   `assets/content/{work,skill,education,author,benchmark}.{yml,md}`.
3. Open the Master content panel in the UI.
4. **Skill** — add a row, click Save. Assert: PUT 200, GET reflects new
   row, YAML file SHA-256 unchanged.
5. **Education** — add an entry, Save. Same assertions.
6. **Author** — change the name, Save. Same assertions.
7. **Benchmark** — append a paragraph, Save. Same assertions.
8. **WorkEditor bullet** — add a bullet via `POST /api/master/work/...`.
   Assert POST 200, GET reflects new bullet, YAML unchanged.
9. Run `jobsmith master export --all`. Assert YAML files now reflect the
   DB state and SHA-256 changes.
10. Diff YAML vs `git HEAD` — should show ONLY the edits made; comments
    preserved (per `ruamel` round-trip).

## Run record

_Fill in when the smoke test is executed. Each run captures:_

- **Date:** `<date>`
- **Operator:** `<who>`
- **Branch / commit:** `<branch> @ <sha>`
- **Backend / frontend versions:** `<jobsmith --version>` / `<web/package.json version>`

### Pre-state SHA-256

```
<paste output of: shasum -a 256 assets/content/*.yml assets/content/*.md>
```

### Step-by-step assertions

| # | Action                                | HTTP | Status | Notes |
| - | ------------------------------------- | ---- | ------ | ----- |
| 4 | Skill: add row → Save                 | PUT  |        |       |
| 5 | Education: add entry → Save           | PUT  |        |       |
| 6 | Author: change name → Save            | PUT  |        |       |
| 7 | Benchmark: append paragraph → Save    | PUT  |        |       |
| 8 | Work: add bullet                      | POST |        |       |

### Mid-state SHA-256 (after PUT/POST, before export)

```
<paste — should match Pre-state SHA-256>
```

### Export

```
$ jobsmith master export --all
<paste output>
```

### Post-export SHA-256

```
<paste — should differ from Pre-state>
```

### `git diff` summary

```
<paste git diff --stat assets/content/>
```

Verify: only the fields edited above appear; comments preserved.

## Screenshots

_Attach (or link to) screenshots captured during the run:_

- `skill-saved.png`
- `education-saved.png`
- `author-saved.png`
- `benchmark-saved.png`
- `bullet-added.png`
- `412-conflict-banner.png` (if exercised)

## Failures

_File any defects as follow-up bugs in HtmlGraph (`htmlgraph bug create`).
Reference this report in the bug description._

## Sign-off

- [ ] All 5 PUT/POST flows returned 2xx
- [ ] Pre-state and mid-state SHA-256 match (DB-only writes)
- [ ] Post-export YAML SHA-256 differs (export regenerates files)
- [ ] `git diff` shows only intentional edits + preserved comments
