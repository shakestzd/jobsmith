#!/usr/bin/env bash
# _apply_byo_spike.sh — THROWAWAY decision-gate spike for plan-21911e16 / slice-1
# (feat-927b6921). Proves whether a local gemma-4 served by vllm-mlx can drive the
# unmodified Claude Code agentic apply pipeline. Outputs a GO/NO-GO that gates
# slices 2-6. NOT production code — delete after the decision is recorded.
#
# Why this works WITHOUT any source changes: headless.py run_phase() spawns
# `claude -p` via subprocess.Popen WITHOUT an env= param (see headless.py:415),
# so the child `claude` INHERITS this shell's environment. Exporting the
# ANTHROPIC_* vars here is exactly what slice-4 will later inject per-subprocess.
# That makes `jobsmith apply` (the real entrypoint) talk to the local engine today.
#
# Usage:
#   scripts/_apply_byo_spike.sh                 # full run (start engine -> apply -> assess)
#   KEEP_ENGINE=1 scripts/_apply_byo_spike.sh   # leave vllm-mlx running after the spike
#   scripts/_apply_byo_spike.sh --engine-only   # just start the engine + health-check, no apply
#
# Tunable knobs (env overrides). The ones marked RUN-CONFIRM are best verified
# against the live engine on first run; defaults are the documented values.
set -uo pipefail

# ---- knobs ----------------------------------------------------------------
MODEL="${MODEL:-mlx-community/gemma-4-E4B-it-qat-4bit}"   # already in HF cache
HAIKU_MODEL="${HAIKU_MODEL:-$MODEL}"                       # single-model server: same gemma for every tier
PORT="${PORT:-8081}"
# RUN-CONFIRM: vllm-mlx exposes Anthropic /v1/messages; Claude Code appends
# "/v1/messages" to ANTHROPIC_BASE_URL. If vllm-mlx namespaces the Anthropic API
# under /anthropic, set BASE_URL accordingly (http://127.0.0.1:$PORT/anthropic).
ANTHROPIC_BASE_URL_VAL="${ANTHROPIC_BASE_URL_VAL:-http://127.0.0.1:${PORT}}"
# vllm-mlx 0.3.0 ships a dedicated `gemma4` tool-call + reasoning parser
# (confirmed via `vllm-mlx serve --help`). tool_use FIRING is the whole point of
# this spike — WITHOUT --enable-auto-tool-choice + --tool-call-parser gemma4 the
# model's tool calls never parse and the gate would falsely NO-GO.
#
# DO NOT add --continuous-batching for gemma-4: on vllm-mlx 0.3.0 the gemma4 MLLM
# batching attention patch raises
#   TypeError: _patched_call() got an unexpected keyword argument 'shared_kv'
# during preprocessing, so EVERY request returns 0 tokens. The non-batched path
# generates and tool-calls correctly. (Finding for slice-3's engine manager.)
SERVE_EXTRA_FLAGS="${SERVE_EXTRA_FLAGS:---enable-auto-tool-choice --tool-call-parser gemma4 --reasoning-parser gemma4}"
# A real JS-rendered board exercises the claude-in-chrome render fallback under gemma.
JD_URL="${JD_URL:-https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"   # cold load is ~60-90s; allow headroom

# ---- paths ----------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/docs/spikes"
TRANSCRIPT="${OUT_DIR}/byo-apply-transcript.jsonl"
ENGINE_LOG="${OUT_DIR}/vllm-mlx-engine.log"
mkdir -p "$OUT_DIR"

ENGINE_PID=""
cleanup() {
  if [[ -n "$ENGINE_PID" && -z "${KEEP_ENGINE:-}" ]]; then
    echo "[spike] stopping vllm-mlx (pid $ENGINE_PID)"
    kill "$ENGINE_PID" 2>/dev/null
    wait "$ENGINE_PID" 2>/dev/null
  elif [[ -n "$ENGINE_PID" ]]; then
    echo "[spike] KEEP_ENGINE set — leaving vllm-mlx running (pid $ENGINE_PID, port $PORT)"
  fi
}
trap cleanup EXIT INT TERM

# ---- preflight ------------------------------------------------------------
echo "=== [spike] preflight ==="
command -v claude   >/dev/null || { echo "FAIL: claude CLI not on PATH"; exit 2; }
command -v jobsmith >/dev/null || { echo "FAIL: jobsmith not on PATH (uv tool install?)"; exit 2; }
if ! command -v vllm-mlx >/dev/null; then
  cat <<EOF
FAIL: vllm-mlx is not installed. Install it into the active venv, then re-run:
    uv pip install vllm-mlx        # or: pip install vllm-mlx
(See github.com/waybarrios/vllm-mlx. Avoid LiteLLM 1.82.7/1.82.8 — malware.)
EOF
  exit 2
fi
echo "claude:   $(claude --version 2>&1 | head -1)"
echo "model:    $MODEL"
echo "port:     $PORT   base_url: $ANTHROPIC_BASE_URL_VAL"

# ---- start engine ---------------------------------------------------------
echo "=== [spike] starting vllm-mlx serve ==="
# shellcheck disable=SC2086
vllm-mlx serve "$MODEL" --port "$PORT" $SERVE_EXTRA_FLAGS >"$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!
echo "[spike] engine pid=$ENGINE_PID (log: $ENGINE_LOG)"

echo "=== [spike] waiting for health (cold load up to ${HEALTH_TIMEOUT}s) ==="
ready=""
for ((i=0; i<HEALTH_TIMEOUT; i+=3)); do
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    echo "FAIL: vllm-mlx process died during load — tail of $ENGINE_LOG:"; tail -20 "$ENGINE_LOG"; exit 3
  fi
  # /v1/models is the OpenAI listing; ready means the model finished loading.
  if curl -fsS "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then ready=1; break; fi
  sleep 3
done
[[ -n "$ready" ]] || { echo "FAIL: engine not healthy within ${HEALTH_TIMEOUT}s"; tail -20 "$ENGINE_LOG"; exit 3; }
echo "[spike] engine healthy after ~${i}s"

[[ "${1:-}" == "--engine-only" ]] && { echo "[spike] --engine-only: done."; exit 0; }

# ---- BYO env (inherited by the claude -p child; mirrors slice-4 injection) -
export ANTHROPIC_BASE_URL="$ANTHROPIC_BASE_URL_VAL"
export ANTHROPIC_AUTH_TOKEN="dummy-spike-token"     # localhost engine accepts any non-empty token
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$HAIKU_MODEL"  # NOT the deprecated ANTHROPIC_SMALL_FAST_MODEL
export CLAUDE_CODE_ATTRIBUTION_HEADER=0              # disable per-request hash (breaks prefix cache locally)

# ---- run the real pipeline ------------------------------------------------
echo "=== [spike] running jobsmith apply against gemma (transcript -> $TRANSCRIPT) ==="
# -y skips phase-gate confirmations so the spike is non-interactive.
jobsmith apply "$JD_URL" -y -vv 2>&1 | tee "$TRANSCRIPT"
APPLY_RC=${PIPESTATUS[0]}
echo "[spike] jobsmith apply exit=$APPLY_RC"

# ---- assess done_when -----------------------------------------------------
echo ""
echo "=== [spike] ASSESSMENT (plan-21911e16 slice-1 done_when) ==="
pass_all=1
check() { # name, condition-rc
  if [[ "$2" -eq 0 ]]; then echo "  [PASS] $1"; else echo "  [FAIL] $1"; pass_all=0; fi
}

grep -q '"type"[[:space:]]*:[[:space:]]*"tool_use"' "$TRANSCRIPT"; check "tool_use blocks fired (real, not hallucinated)" $?
grep -Eq 'WebFetch|"Read"|"Write"|claude-in-chrome' "$TRANSCRIPT"; check "core tools engaged (WebFetch/Read/Write or chrome render)" $?
grep -q '<<PHASE_COMPLETE:' "$TRANSCRIPT"; check "phase marker <<PHASE_COMPLETE:...>> emitted (run_phase advances)" $?
grep -Eqi 'jd-parser|bullet-selector|"agent"' "$TRANSCRIPT"; check ">=2 specialist tiers dispatched (haiku jd-parser + sonnet bullet-selector)" $?
! grep -Eqi 'invalid x-api-key|authentication_error|401' "$TRANSCRIPT"; check "claude -p initialized with dummy token (no auth error)" $?

# Locate the run's state dir and verify the field checklist.
STATE_DIR="$(find "${REPO_ROOT}/applications" -type d -name .apply-state -newermt "-1 hour" 2>/dev/null | head -1)"
if [[ -n "$STATE_DIR" ]]; then
  echo "  state dir: $STATE_DIR"
  JD_JSON="$STATE_DIR/jd-parsed.json"; PROSE="$STATE_DIR/prose-draft.md"
  python3 - "$JD_JSON" <<'PY'; check "jd-parsed.json: company+position+role_type set, must_haves>=2" $?
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
ok = bool(d.get("company")) and bool(d.get("position")) and bool(d.get("role_type")) and len(d.get("must_haves") or []) >= 2
sys.exit(0 if ok else 1)
PY
  test -s "$PROSE" && grep -qi 'Professional Summary' "$PROSE" && \
    awk '/[Pp]rofessional [Ss]ummary/{f=1;next} f&&NF{print;exit}' "$PROSE" | grep -q .
  check "prose-draft.md has a non-empty Professional Summary" $?
else
  echo "  [FAIL] no .apply-state dir produced in the last hour"; pass_all=0
fi

echo ""
if [[ "$pass_all" -eq 1 ]]; then
  echo "=== [spike] VERDICT: all done_when checks PASSED -> candidate GO ==="
  echo "    Record GO + transcript evidence in docs/spikes/byo-model-apply.md."
else
  echo "=== [spike] VERDICT: one or more checks FAILED -> candidate NO-GO ==="
  echo "    Inspect $TRANSCRIPT + $ENGINE_LOG. A NO-GO pivots the plan to the C fallback"
  echo "    (OpenCode/OpenHands). Record the decision + evidence in docs/spikes/byo-model-apply.md."
fi
exit $(( pass_all == 1 ? 0 : 1 ))
