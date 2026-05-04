# Storage Architecture Decision: PocketBase vs Existing SQLite

## Context

The jobsmith backend is gaining a FastAPI layer to serve a React frontend replacing `jobsmith.html`. That frontend needs read endpoints for pipeline results and a realtime channel for live pipeline events (slice 8 / SSE). Two invariants constrain the decision: (1) master YAMLs are **read-only** — the pipeline never writes to them; and (2) the project is single-user/local today, but a SaaS path is not ruled out. Picking the wrong storage layer now either incurs migration cost later or adds operational overhead before iteration speed is established.

---

## Option A: PocketBase as Canonical Storage

Replace the existing SQLite layer entirely. All pipeline data — `apply_runs`, `specialist_outputs`, amendments, chat sessions — migrates into PocketBase collections. The pipeline write path (`db_ingest.py`) is rewritten to call PocketBase's REST API instead of the direct SQLite driver.

**Pros:** Auth, admin UI, realtime subscriptions, and a REST API come for free. No custom read endpoints to write. Realtime for SSE (slice 8) is built in.

**Cons:** The migration cost is non-trivial: 5 tables, a custom migration runner, and Pydantic-typed queries all need replacing. The pipeline currently writes via direct SQLite after each phase; switching to HTTP calls adds latency and a network failure mode inside the critical apply path. PocketBase's schema is managed via its admin UI or JS migrations — neither integrates cleanly with the existing Python migration runner. PocketBase is explicitly "not recommended for production-critical apps" before v1.0.

**Impact on master-yaml invariant:** None.

---

## Option B: PocketBase as Auth/Realtime Layer; SQLite Stays Canonical

The pipeline DB (`jobsmith.db`) and review DBs remain unchanged. PocketBase runs as a sidecar and holds only user-facing, web-session-scoped data (e.g., auth tokens, web edit sessions, any future annotation collections). FastAPI mediates: it reads from SQLite for pipeline data and either proxies PocketBase or owns the SSE stream directly.

**Pros:** Zero migration cost for existing pipeline data. The apply path stays purely local SQLite. Web-facing auth and realtime are delegated to PocketBase. The boundary is clean: PocketBase never sees pipeline internals.

**Cons:** Two processes to run and keep alive. FastAPI must bridge two data sources. Auth for a single-user local tool is overhead without clear near-term payoff. Realtime subscriptions in PocketBase won't cover pipeline events stored in SQLite — FastAPI still needs a custom SSE mechanism for those.

**Impact on master-yaml invariant:** None.

---

## Option C: Skip PocketBase Entirely

Keep the existing SQLite layer. FastAPI exposes the read endpoints and a custom SSE stream for pipeline events. A minimal admin route can serve as a lightweight dashboard. No second process, no new runtime dependency.

**Pros:** Simplest deployment (one process). No migration. The SQLite schema, migration runner, and Pydantic models remain exactly as they are. SSE for pipeline events is straightforward via FastAPI's `StreamingResponse` polling the existing `apply_runs` table. The full stack is Python, testable with `pytest`, with no external service to mock.

**Cons:** No built-in auth. Single-user local deployment makes auth low priority now, but adding it later requires custom code. No admin UI; developer must query SQLite directly for diagnostics. If SaaS is targeted, more custom work is needed.

**Impact on master-yaml invariant:** None.

---

## Option D: PocketBase for New User-Facing Entities Only

A hybrid: pipeline data stays in SQLite; PocketBase hosts only new collections that have no existing SQLite equivalent (e.g., feedback annotations, web-editor session state, future per-user settings). The FastAPI layer reads from both.

**Pros:** No migration of existing data. Adds PocketBase's admin UI for net-new collections only.

**Cons:** Two processes. Two query patterns in FastAPI. Adds complexity without eliminating the need for custom SSE for pipeline events. Over time, the boundary between "SQLite entities" and "PocketBase entities" is likely to blur, creating a maintenance burden. Realtime still needs bridging for pipeline events.

**Impact on master-yaml invariant:** None.

---

## Decision Matrix

| Criterion | A: PB Canonical | B: PB Sidecar | C: Skip PB | D: PB Hybrid |
|---|---|---|---|---|
| Deployment complexity | 1 process (PB replaces Python) | 2 processes | 1 process | 2 processes |
| Auth for single-user local | Built in (overkill) | Built in (overkill) | Not included | Built in (overkill) |
| Auth for future SaaS | Excellent | Good (PB handles it) | Manual build | Partial |
| Realtime / SSE for pipeline events | Requires migration of pipeline tables | Still needs custom SSE for SQLite events | Custom SSE, trivial to add | Still needs custom SSE |
| Data migration cost from current SQLite | High (full schema + write path rewrite) | None | None | None |
| Impact on master-yaml invariant | None | None | None | None |
| Operational/observability burden | Medium (PB process + logs) | High (two processes, two log streams) | Low (single Python process) | High (two processes, two log streams) |
| Maturity/stability risk | Moderate (pre-v1.0 caution) | Moderate | Low | Moderate |

---

## Recommendation: Option C

**Recommendation: C** — Skip PocketBase for now. FastAPI + existing SQLite + custom SSE.

The realtime requirement (slice 8) is satisfied with FastAPI's `StreamingResponse` polling `apply_runs`; the pipeline already commits atomically after each phase, making this safe. Auth is single-user and local; HTTP Basic or a static token takes under 20 lines when needed. The existing migration runner, Pydantic models, and `db.py` helpers represent accumulated correctness (WAL mode, per-kind dedup, backfill) that would be costly to reproduce in PocketBase's schema model.

PocketBase's value proposition is auth and admin for multi-user SaaS. At current scale — one developer, one laptop, pipeline writes from Claude Code subprocesses — it is pure overhead. Option B is the right revisit if SaaS is targeted within 6 months; that boundary (SQLite owns pipeline, PocketBase owns auth/users) is clean enough to migrate to from Option C with minimal disruption.

**Slice 7 implication:** Slice 7 (PocketBase setup, feat-97c991d0) is **superseded** — close as "not needed" or park as a future spike gated on a concrete SaaS commitment.

---

## Open Questions

1. **SaaS timeline.** If multi-user SaaS is targeted within ~6 months, Option B becomes preferable: the PocketBase sidecar handles auth/user management while SQLite continues to own the pipeline. If the timeline is >12 months or uncertain, Option C defers complexity appropriately.
2. **SSE concurrency.** If pipeline events must stream to multiple simultaneous browser sessions, a polling SSE degrades; revisit PocketBase realtime or a lightweight event bus at that point.
3. **Admin UI.** If non-developer users need to browse data via a GUI soon, PocketBase's dashboard in Option B becomes a concrete timesaver rather than overhead.
