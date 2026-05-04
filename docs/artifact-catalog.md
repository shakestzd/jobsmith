# Artifact Catalog — jobsmith pipeline

Single source of truth for every artifact kind that needs a DB home.
Generated as part of feat-ca7408db (track trk-9bb48a61).

---

## Target read/write architecture

```
┌──────────────┐  PUT /artifacts   ┌──────────────┐
│ specialist   │──────────────────►│  FastAPI     │
│ (claude -p)  │                   │  /api/...    │
│              │◄──────────────────│              │
│              │  GET /artifacts   │              │
└──────────────┘                   └──────┬───────┘
                                          │
                                          ▼
                                   ┌──────────────┐
                                   │  SQLite      │
                                   │  jobsmith.db │
                                   └──────────────┘
```

---

## Migration sequencing

```
Phase 1 (slices 4-6):  specialist writes BOTH FS and DB
Phase 2 (slices 7-8):  reads switch to DB; FS still written for safety
Phase 3 (slices 9-10): specialist writes only DB; snapshot regenerates FS
                        for quarto render and git diff
```

---

## Artifact kind catalog

| kind | Pydantic model | Producing specialist | FS path | Pipeline phase | Currently ingested by db_ingest |
|------|---------------|----------------------|---------|----------------|---------------------------------|
| `jd-parsed` | `JDParsed` | `apply-jd-parser` | `.apply-state/jd-parsed.json` | gather | yes |
| `fit-score` | `FitScore` | `apply-fit-scorer` | `.apply-state/fit-score.json` | gather | yes |
| `bullet-selection` | `BulletSelection` | `apply-bullet-selector` | `.apply-state/bullet-selection.json` | gather | yes |
| `hm-snippet` | `HMSnippet` | `apply-hm-enricher` | `.apply-state/hm-snippet.md` | gather | yes |
| `company-research` | `TextArtifact` | `apply-company-research` | `.apply-state/company-research.md` | gather | yes |
| `prose-draft` | `TextArtifact` | `apply-prose-writer` | `.apply-state/prose-draft.md` | draft | yes |
| `ai-tell-report` | `AITellReport` | `apply-prose-qa` | `.apply-state/ai-tell-report.json` | draft | yes |
| `ats-check` | `ATSCheck` | `apply-portfolio-ats-checker` | `.apply-state/ats-check.json` | render | yes |
| `anchor-check` | `AnchorCheck` | orchestrator (guard step) | `.apply-state/anchor-check.json` *(future)* | render | no |
| `fact-check` | `FactCheck` | orchestrator (guard step) | `.apply-state/fact-check.json` *(future)* | render | no |
| `cover-letter-draft` | `CoverLetterDraft` | `apply-cover-letter-writer` | `<app>/cover-letter-draft.md` | render | no |
| `quarto-config` | `QuartoConfig` | `assemble` (orchestrator) | `<app>/_quarto.yml` | render (post-phase) | no |
| `variables` | `Variables` | `assemble` (orchestrator) | `<app>/_variables.yml` | render (post-phase) | no |
| `outreach-snippets` | `TextArtifact` | `apply-outreach-writer` *(future)* | `.apply-state/outreach-snippets.md` | render | yes |
| `manifest` | `Manifest` | orchestrator (`apply.py`) | `<app>/.apply-state/manifest.json` | all (written continuously) | no |

---

## Notes

### Kinds not yet wired into `ARTIFACT_READERS` / `SPECIALIST_TO_ARTIFACT`

The following kinds have Pydantic models registered in `KIND_MODELS` but
are **not yet picked up by `db_ingest.ingest_phase_outputs`** because they
lack entries in `_state_readers.ARTIFACT_READERS` and/or
`_state_readers.SPECIALIST_TO_ARTIFACT`. They are catalogued here so that
later slices (4–6) have a clear implementation target.

- `anchor-check` — guard step result; no FS file written yet by the pipeline.
- `fact-check` — guard step result; no FS file written yet by the pipeline.
- `cover-letter-draft` — written to `<app>/` (not `.apply-state/`); ingest
  requires a reader that looks one level up from `state_dir`.
- `quarto-config` — written to `<app>/_quarto.yml`; same path issue as above.
- `variables` — written to `<app>/_variables.yml`; same path issue.
- `manifest` — the manifest itself is the ingest trigger, not a target row;
  recording it as a kind enables future audit queries against DB-stored
  manifest snapshots.
- `outreach-snippets` — reader exists in `ARTIFACT_READERS` but the
  producing specialist (`apply-outreach-writer`) is not yet in
  `SPECIALIST_TO_ARTIFACT`.

### `TextArtifact` as shared model

`TextArtifact` (`{"text": str | None}`) is reused for all plain-markdown
kinds (`prose-draft`, `company-research`, `outreach-snippets`,
`cover-letter-draft`).  The cover-letter-draft variant uses the dedicated
`CoverLetterDraft` model (same shape today, but owns its own type identity
for future extension).

### `Variables` extra fields

`Variables` uses `model_config = {"extra": "allow"}` because
`_variables.yml` contains deeply nested keys that evolve as specialists are
added.  The top-level scalar fields (`slug`, `company`, `position`) are
explicitly typed for fast DB lookups.
