# 0.8.3 — apply pipeline observability via SSE

Real-browser verification that structured agent events from `transcript.jsonl`
reach the web UI as typed rows (bug-0e13706c), and that pipeline halts
surface as a red "failed" phase + specialists (bug-0489bff3 + bug-8ade6f70)
instead of stuck spinners.

Track: `trk-eb70f385`

## Run record — 2026-05-06

- **Operator:** shakestzd (driven via Claude Code with claude-in-chrome MCP)
- **Branch / commit:** `trk-eb70f385` @ post-bug-0e13706c
- **Project under test:** `/tmp/jobsmith-smoke-0-8-2/` (master content
  carried over from 0.8.2 smoke test, including the SMOKE markers)
- **JD URL:** `https://job-boards.greenhouse.io/reddit/jobs/7445224`
  (Reddit Senior Analytics Engineer — chosen because Greenhouse is static
  HTML and fetches cleanly)
- **Backend:** `jobsmith api serve --port 8721` from worktree source
  (`PYTHONPATH=…/0-8-3-apply-observability-trk-eb70f385/src`) so the
  supervisor's transcript tailer + new prompts are loaded
- **Frontend:** vite from worktree (port 5173) so the structured-event
  UI handler is loaded

## Pipeline outcome

The apply pipeline correctly halted in phase 1 with `UNCOVERED_MUST_HAVE`
(Spark/Scala absent from Pat Doe's master skills; Airflow not covered).
This is the desired refusal-to-fabricate behaviour and the same outcome
as the 0.8.2 smoke run — what changed is **how** the halt surfaced.

## Verification matrix

| # | Check | Before fixes | After fixes |
|---|-------|--------------|-------------|
| 1 | Transcript event types after halt | 0 phase_failed; 1 free-text "HALT surfaced" markdown blob | **1 `type: phase_failed`** with structured `{phase, name, error}` ✓ |
| 2 | Header badge | `running` (stuck) | **`failed`** ✓ |
| 3 | Phase 1 phase-status text | `running` (stuck) | **`failed`** ✓ |
| 4 | Phase 1 specialist labels | `running`, `running`, `running` (stuck) | **`failed`, `failed`, `failed`** ✓ |
| 5 | Phase 2/3 status | `queued` | `queued` (correct — pipeline never started those) ✓ |
| 6 | Event-stream row count in UI | 8 (mostly `→ Agent()` spinner ticks) | **177** (73 tool_call + 73 tool_result + 5 agent_text + 26 legacy log lines) ✓ |
| 7 | Tool-call rows visible as bold tool name | none | **73** ✓ |
| 8 | Tool-result rows visible as `✓` + summary | none | **73** ✓ |
| 9 | Agent-text rows visible as italic quote | none | **5** ✓ |

## Backend evidence — phase_failed event

The orchestrator emitted the structured marker that bug-0489bff3's prompt
fix required:

```json
{
  "ts": "2026-05-06T06:32:24-04:00",
  "phase": "gather",
  "type": "phase_failed",
  "name": "gather",
  "error": "apply-bullet-selector-halted: UNCOVERED_MUST_HAVE — Spark/Scala absent from master; Airflow not covered (master uses Dagster); needs apply-relevance-inquirer cycle."
}
```

`headless.py` parsed `<<PHASE_FAILED: gather: …>>` from the agent's
output and wrote this structured event to `transcript.jsonl`. The
supervisor's transcript tailer forwarded it as an `event=transcript` SSE
message; the UI's transcript handler set `sseStatus='failed'`, which
the failed-phase rendering (bug-8ade6f70) then translated into red x +
"failed" for both the phase card and its specialists.

## Frontend evidence — UI snapshot

```js
{
  badgeStatus: "failed",
  phaseStatuses: ["failed", "queued", "queued"],
  specialistStatuses: [
    "apply-jd-parser",   "failed",
    "apply-anchor-scorer","failed",
    "apply-spec-builder","failed",
  ],
  eventCount: "177",
  toolRowCount: 73,
  resultRowCount: 73,
  agentRowCount: 5,
}
```

Sample event-stream rows (the kind of thing the user couldn't see at all
before this track):

```
06:28:45  tool   Write ({"file_path": "..."})
06:28:47  ✓      File created successfully at: ...
06:28:47  tool   Bash ({"command": "..."})
06:28:54  ✓      lrwxr-xr-x shakes wheel 44 B ...
06:28:56  agent  Now dispatching apply-jd-parser.
06:29:03  tool   Agent ({"description": "Parse JD via apply-jd-parser", ...})
06:29:04  tool   Read ({"file_path": "..."})
06:29:05  ✓      1\t{\n2\t  "specialist": "apply-jd-parser", ...
```

The legacy `warn → Agent()` / `warn ✓ (1s)` terminal log lines still
appear (the supervisor forwards both stdout and the transcript) but
they're no longer the only signal — they coexist with the structured
typed rows.

## Sign-off

- [x] bug-0489bff3 — orchestrator emits `<<PHASE_FAILED>>` marker on halt
- [x] bug-8ade6f70 — UI renders failed phase + halts specialist spinners
- [x] bug-0e13706c — transcript events stream to UI as `event=transcript`
- [x] All three fixes verified end-to-end via live Reddit Greenhouse JD
- [x] 974 backend tests + 137 frontend tests pass; production build clean
