#!/usr/bin/env python3
"""Inverted-harness POC — code owns the control flow, the model does ONE narrow task.

Context: the spike (docs/spikes/byo-model-apply.md) proved a 4B local model CANNOT
drive Claude Code's 70-tool agentic loop (it looped on step 1). But it does single-shot
structured tasks fine. This POC validates the pivot the user chose: a thin Python driver
runs ONE pipeline node (jd-parse) against the local gemma via the EXISTING
``jobsmith.llm.openai_compat.OpenAICompatClient`` with json_schema + robust-parse, and
produces a valid ``.apply-state/jd-parsed.json`` — no agentic autonomy, no Claude Code.

It is deliberately written so the ``Node`` + ``Pipeline`` shapes preview the real
code-orchestrated harness the re-plan will generalize over specialist-contracts.yaml's
``pipeline.stages``.

Run:  uv run python scripts/_apply_local_poc.py            # local gemma on :8081
      POC_BASE_URL=... POC_MODEL=... uv run python scripts/_apply_local_poc.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from jobsmith.llm.openai_compat import OpenAICompatClient  # the EXISTING httpx client

# ---------------------------------------------------------------------------
# 1. The node's output contract (subset of apply-jd-parser's jd-parsed.json)
# ---------------------------------------------------------------------------

ROLE_TYPES = "data-analyst | data-engineer | ai-engineer | finance | renewable-energy | general"
LOCATION_TYPES = "remote | hybrid | onsite | unknown"


class JdParsed(BaseModel):
    company: str
    position: str
    location: str | None = None
    location_type: str = "unknown"
    salary_range: str | None = None
    req_id: str | None = None
    role_type: str
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    top_keywords: list[str] = Field(default_factory=list)


# OpenAI-style json_schema response_format. vllm-mlx enforces this server-side
# (xgrammar/llguidance); we ALSO embed it in the prompt and robust-parse the
# result, because a 4B honors the schema only as well as the server does.
JD_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "jd_parsed",
        "schema": JdParsed.model_json_schema(),
    },
}


# ---------------------------------------------------------------------------
# 2. Robust parse (mirrors jobsmith.sourcing.llm_rescore: strip fence -> loads
#    -> substring fallback; flag, never raise).
# ---------------------------------------------------------------------------


def _coerce_json_object(content: str) -> dict | None:
    text = content.strip()
    if text.startswith("```"):
        body = text[3:]
        nl = body.find("\n")
        if nl != -1 and body[:nl].strip().lower() in ("json", ""):
            body = body[nl + 1 :]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        text = body.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                return None
    return None


# ---------------------------------------------------------------------------
# 3. The driver primitives the real harness will generalize.
# ---------------------------------------------------------------------------


class Node:
    """One bounded LLM task. Python decides WHEN it runs; the model only does it."""

    def __init__(self, name: str, model_cls: type[BaseModel], schema: dict) -> None:
        self.name = name
        self.model_cls = model_cls
        self.schema = schema

    def run(self, client: OpenAICompatClient, prompt: str, *, retries: int = 3) -> BaseModel:
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract structured data. Respond with ONE JSON object matching "
                    f"this schema and NOTHING else:\n{json.dumps(self.schema['json_schema']['schema'])}"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        last_err: str = ""
        for attempt in range(1, retries + 1):
            content = client.complete(messages, response_format=self.schema, temperature=0.0)
            obj = _coerce_json_object(content)
            if obj is not None:
                try:
                    return self.model_cls.model_validate(obj)
                except ValidationError as e:
                    last_err = str(e)
            else:
                last_err = "no JSON object found in response"
            # bounded reask — code owns the loop, not the model
            messages.append({"role": "assistant", "content": content[:500]})
            messages.append(
                {"role": "user", "content": f"That was invalid ({last_err}). Re-emit ONLY the JSON object."}
            )
            print(f"  [{self.name}] attempt {attempt} invalid -> reask")
        raise RuntimeError(f"{self.name}: no valid JSON after {retries} attempts: {last_err}")


def checkpoint(state_dir: Path, name: str, payload: dict) -> Path:
    """Write a node result to .apply-state/<name>.json (crash-resume seam)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / f"{name}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 4. Run the single node and assert the spike's jd-parse done_when.
# ---------------------------------------------------------------------------


def main() -> int:
    base_url = os.environ.get("POC_BASE_URL", "http://127.0.0.1:8081/v1")
    model = os.environ.get("POC_MODEL", "mlx-community/gemma-4-E4B-it-qat-4bit")
    jd_path = Path(
        os.environ.get(
            "POC_JD",
            "/private/tmp/claude-501/-Users-shakes-DevProjects-jobsmith--claude-worktrees-"
            "pluggable-llm-backends-for-trk-f5052600/310cff37-61bf-4ff7-9c84-164d58a69307/"
            "scratchpad/spike-jd.txt",
        )
    )
    state_dir = Path("docs/spikes/.poc-apply-state")

    if not jd_path.exists():
        print(f"FAIL: JD file not found: {jd_path}")
        return 2

    jd_text = jd_path.read_text(encoding="utf-8")
    client = OpenAICompatClient(base_url=base_url, model=model, api_key="dummy", timeout_s=300.0)
    node = Node("jd-parse", JdParsed, JD_SCHEMA)

    prompt = (
        "Parse the following job description into the schema fields. role_type must be one of: "
        f"{ROLE_TYPES}. location_type must be one of: {LOCATION_TYPES}. "
        "must_haves and nice_to_haves are short requirement strings; top_keywords is 5-8 terms.\n\n"
        f"JOB DESCRIPTION:\n{jd_text}"
    )

    print(f"=== POC jd-parse node -> {model} @ {base_url} ===")
    t0 = time.monotonic()
    try:
        result: JdParsed = node.run(client, prompt)  # type: ignore[assignment]
    except Exception as e:  # noqa: BLE001 — POC: surface any failure plainly
        print(f"FAIL: {e}")
        return 1
    dt = time.monotonic() - t0

    out = checkpoint(state_dir, "jd-parsed", result.model_dump())
    print(f"  wrote {out} in {dt:.1f}s")
    print(json.dumps(result.model_dump(), indent=2))

    # done_when (mirrors the spike's jd-parsed.json checklist)
    ok = bool(result.company) and bool(result.position) and bool(result.role_type) and len(result.must_haves) >= 2
    print()
    print(f"  company={result.company!r} position={result.position!r} role_type={result.role_type!r} "
          f"must_haves={len(result.must_haves)}")
    if ok:
        print(f"=== POC PASS: code-driven node produced a valid jd-parsed.json in {dt:.1f}s ===")
        return 0
    print("=== POC FAIL: jd-parsed.json missing required fields ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
