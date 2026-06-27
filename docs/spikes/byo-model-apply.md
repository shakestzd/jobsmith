# Spike: gemma-4 drives the Claude Code agentic apply via vllm-mlx

**Plan:** plan-21911e16 / slice-1 · **Feature:** feat-927b6921 · **Status:** ✅ COMPLETE — **VERDICT: NO-GO** (with a pivot)

This is the **decision gate** for the whole plan. The literal hypothesis — *a 4B local
model drives Claude Code's unmodified agentic apply loop* — is **NO-GO**. But the spike
surfaced exactly which part fails and which part works, pointing at a viable pivot
(below). Slices 2–6 as written are superseded and need re-planning.

## Hypothesis under test

`mlx-community/gemma-4-E4B-it-qat-4bit`, served by **vllm-mlx** over its native Anthropic
`/v1/messages`, drives Claude Code's unmodified multi-step agentic apply — real
`tool_use`, ≥2 specialist tiers routing to the one gemma, phase markers emitted, a usable
draft produced.

## What actually happened

The **transport + tool mechanism works**; the **4B model cannot drive the multi-step
agentic plan**, and throughput is far too low for a many-turn loop.

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | Real `tool_use` blocks fire | ✅ **PASS** | Direct Anthropic-endpoint probes returned well-formed `get_weather` + `Read` tool_use with `stop_reason:"tool_use"`; the live apply showed a genuine multi-turn `tool_use → tool_result → next turn` loop (conversation grew to 17 msgs, 70 tools in context). |
| 2 | ≥2 specialist tiers reach gemma | ❌ **FAIL (not reached)** | The orchestrator never got past its **first** step — it never dispatched jd-parser (haiku) or bullet-selector (sonnet). |
| 3 | `<<PHASE_COMPLETE: …>>` emitted | ❌ **FAIL** | Never advanced past gather setup; no marker emitted. |
| 4 | `claude -p` inits with dummy token | ✅ **PASS** | `x-api-key: dummy` accepted; the loop ran 30+ turns with no auth error. |
| 5 | `jd-parsed.json` fields | ❌ **FAIL** | **Zero `.apply-state` artifacts** — the state dir was never even created. |
| 6 | `prose-draft.md` Professional Summary | ❌ **FAIL** | Never reached the draft phase. |

**Root-cause failure mode:** the orchestrator's first instruction is "run `date` once and
write it to `manifest.json`." gemma-4-E4B called `Bash(date …)` **5+ times in a row** over
~26 min and never wrote the file or moved on. It has tool *fidelity* (each individual call
is correct) but not the agentic *planning* to progress a multi-step workflow — the classic
small-model failure. At **~5 tok/s (~200 s/turn)**, a pipeline needing dozens–hundreds of
turns is impractical even if it didn't loop. A larger gemma (12B) would plan marginally
better but run *slower* per token on the same ~12 GB Metal budget — it does not clear the
throughput wall.

## Environment notes (for the re-plan)

- **vllm-mlx 0.3.0**; `mlx-community/gemma-4-E4B-it-qat-4bit`; cold load **~18–30 s** (not the ~60–90 s estimated).
- **Anthropic endpoint works**: `ANTHROPIC_BASE_URL=http://127.0.0.1:8081` (Claude Code appends `/v1/messages`). OpenAI `/v1/*` also served from the same process.
- **Tool calling requires** `--enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4`. Without them tool_use never parses.
- **⚠ ENGINE BUG:** `--continuous-batching` crashes the gemma-4 MLLM path —
  `TypeError: patch_gemma4_attention_for_batching._patched_call() got an unexpected keyword argument 'shared_kv'` → **0 tokens** on every request. The non-batched path works. (Critical input for any engine-manager slice.)
- No source changes were needed: today's `headless.py:415` `Popen` has no `env=`, so the
  `claude -p` child inherits the exported `ANTHROPIC_*` vars (validates the slice-4 seam premise).

## VERDICT → NO-GO, and the pivot

**NO-GO** for *approach D* (local model drives Claude Code's agentic harness). The plan's
documented fallback was "adopt OpenCode/OpenHands"; the **user redirected to a sharper
pivot**:

> **Invert the harness.** Python owns ALL orchestration — the fixed DAG, the fan-outs, the
> bounded retry loops, state-file I/O, phase sequencing (jobsmith already specifies this DAG
> in `src/jobsmith/plugin/agents/apply/specialist-contracts.yaml → pipeline.stages`). The
> local model is invoked **per node** for ONE narrow, bounded task with structured/JSON
> output. No "agent decides the next step" autonomy. Cloud Claude stays a drop-in per-node
> backend. Evaluate code-driven orchestrators (LangGraph, etc.).

**Salvage from this spike:** vllm-mlx serving gemma over OpenAI+Anthropic APIs (the per-node
worker), the cloud/local backend-swap goal, and the engine-lifecycle learnings (no
continuous-batching, the gemma4 parser flags, the cold-load timing).

**Obsolete:** slices 4 & 5 (injecting BYO env so the *model* drives Claude Code). Slices 2–6
overall need re-planning around code orchestration.

## POC follow-up (pivot validated) — 2026-06-27

`scripts/_apply_local_poc.py` — a thin Python driver (`Node` + checkpoint, reusing the
existing `jobsmith.llm.openai_compat.OpenAICompatClient` + a json_schema + robust-parse) —
ran ONE jd-parse node against the same local gemma-4-E4B and produced a **valid, accurate
`jd-parsed.json` in 49.6 s on the first attempt (no reask)**: correct company/position,
`role_type=data-engineer`, exact salary + req_id, 5 verbatim must-haves. Same model, same
engine — the only change is *code owns the control flow and the model does one bounded task*.
This green-lights the re-plan: a no-framework code-orchestrated harness over
`specialist-contracts.yaml → pipeline.stages`, local gemma or cloud Claude per node.
