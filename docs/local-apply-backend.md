# Local Apply Backend — vllm-mlx + Gemma

This document covers the **`code_local` apply orchestrator** introduced in trk-f5052600.
When `llm.apply.orchestrator = code_local` the Python DAG runner executes the apply
pipeline locally, routing each node to its configured LLM backend instead of delegating
the full pipeline to `claude -p`.

The default remains `claude_cloud` — no change to existing behaviour when the
`llm.apply` block is absent or uses the default.

---

## Prerequisites

- Apple Silicon Mac (the MLX path targets Apple Silicon via `mlx-community` model weights).
- Python 3.11+ with `uv` available in PATH.
- Sufficient RAM: Gemma 4 E4B 4-bit quantised fits in ~6 GB of unified memory.

---

## Install

```bash
uv pip install vllm-mlx
```

`vllm-mlx` bundles `mlx_lm` and the vLLM OpenAI-compatible server adapter for Apple
Silicon. No CUDA or NVIDIA driver required.

---

## Serve the model

```bash
python -m mlx_lm.server \
  --model mlx-community/gemma-4-E4B-it-qat-4bit \
  --host 127.0.0.1 \
  --port 8081 \
  --enable-auto-tool-choice \
  --tool-call-parser gemma4 \
  --reasoning-parser gemma4
```

**Critical flags:**

| Flag | Required | Note |
|------|----------|------|
| `--enable-auto-tool-choice` | YES | enables structured tool-call parsing |
| `--tool-call-parser gemma4` | YES | uses Gemma 4-specific parser |
| `--reasoning-parser gemma4` | YES | strips thinking tokens before content is emitted |
| `--continuous-batching` | **DO NOT USE** | causes hangs with the current `mlx_lm.server` build |

> **Port note:** wipnote reserves port 8080 for its own server. Use **8081** for
> `mlx_lm.server`.

---

## Cold-load time

The MLX runtime lazy-loads model weights on the **first inference request**.
`/v1/models` responds immediately, but the first `/v1/chat/completions` call blocks
for approximately **20–30 seconds** while the model is paged into Apple Silicon
unified memory. Subsequent calls are fast. Plan for this latency in the first
apply run after starting the server.

---

## Config tab setup (Web UI)

1. Open the jobsmith Web UI → **Config** tab.
2. Under **apply orchestrator**, change the selector from
   `Claude (cloud, claude -p)` to `Local code driver (gemma via vllm-mlx)`.
3. Fill in:
   - **node backend base url** — `http://127.0.0.1:8081/v1`
   - **node backend model** — `mlx-community/gemma-4-E4B-it-qat-4bit`
   - **on failure** — `error (stop)` or `fallback to cloud`
4. Click **Save**.

This writes the `llm.apply` block to `.apply-config.yaml`:

```yaml
llm:
  apply:
    orchestrator: code_local
    on_failure: fallback_cloud   # or 'error'
    node_backend:
      provider: openai_compatible
      base_url: http://127.0.0.1:8081/v1
      model: mlx-community/gemma-4-E4B-it-qat-4bit
```

---

## v1 scope: gather → draft nodes only

The code-local DAG covers the **gather** and **draft** phases of the apply pipeline:

- `jd-parser` — parses job description into structured fields
- `fit-scorer` — scores fit against the candidate profile
- `bullet-selector` — selects and ranks resume bullets
- `prose-writer` — drafts tailored resume prose
- `cover-letter-writer` (when `cover_letter.auto = true`) — drafts cover letter

Specialist nodes outside this scope (render, site-build, review) continue to run
locally via the existing CLI path unchanged.

---

## Per-node hybrid routing

You can route individual pipeline nodes to different backends using
`llm.apply.node_backends` — a map of node name → backend config.

**Example: route prose-write to cloud Claude while keeping all other nodes local:**

```yaml
llm:
  apply:
    orchestrator: code_local
    on_failure: fallback_cloud
    node_backend:
      provider: openai_compatible
      base_url: http://127.0.0.1:8081/v1
      model: mlx-community/gemma-4-E4B-it-qat-4bit
    node_backends:
      prose-writer:
        provider: anthropic
        model: claude-opus-4-5
        api_key: sk-ant-...
```

Node backend resolution order (highest priority first):

1. `node_backends[node_name]` — explicit per-node override.
2. `node_backend` — default backend for all nodes.
3. Falls back to the parent `llm` provider when neither is set.

---

## on_failure behaviour

| Value | Behaviour |
|-------|-----------|
| `error` | Any node failure raises immediately and halts the pipeline. |
| `fallback_cloud` | A failing node retries via `claude -p` (cloud) before propagating the error. |

`fallback_cloud` is useful during local iteration when model quality is uncertain —
the pipeline completes even if a local inference call fails, at the cost of cloud spend.

---

## Troubleshooting

**First request hangs for >60 seconds**
The model weights are loading. Wait; subsequent requests are fast. If it takes >90s,
check system memory (Activity Monitor → Memory Pressure).

**`content=None` in LLM response**
Gemma 4 E4B is a thinking model. Without enough token budget the reasoning trace
exhausts the context window before content is emitted. Set `extra={'max_tokens': 3000}`
in the node backend config (or raise the server's default `--max-tokens`).

**`parse_ok=False` for fit-scorer**
Gemma 3 (not E4B) echoes back the JSON schema structure instead of flat values.
Use `mlx-community/gemma-4-E4B-it-qat-4bit` — the E4B model emits clean flat
fit-metrics JSON.

**Port conflict on 8080**
wipnote's local server binds 8080. Use `--port 8081` for `mlx_lm.server`.
