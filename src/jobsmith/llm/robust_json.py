"""Shared robust JSON-object extraction for unreliable model output (feat-c920ceeb).

OpenAI-compatible servers (esp. Ollama's /v1) and other local engines do NOT
reliably honour ``response_format`` json_schema — the returned body may be
prose, a fenced code block, or schema-violating JSON. These helpers turn that
raw text into a JSON object on a best-effort basis, returning ``None`` rather
than raising when nothing usable can be recovered.

Single source of truth: extracted from ``sourcing.llm_rescore`` so the sourcing
scorers AND the apply-local per-node backends parse model output identically.
Domain-specific validation (e.g. the fit-metrics ``score`` requirement) stays
with its caller; this module only answers "is there a JSON object in here?".
"""

from __future__ import annotations

import json


def strip_code_fence(text: str) -> str:
    """Strip a leading/trailing markdown code fence (```json ... ```)."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    # Drop the opening fence line (``` or ```json) and any trailing fence.
    body = stripped[3:]
    newline = body.find("\n")
    if newline != -1:
        first_line = body[:newline].strip().lower()
        if first_line in ("", "json"):
            body = body[newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def coerce_json_object(content: str | None) -> dict | None:
    """Best-effort: turn raw model output into a JSON object, else None.

    Handles three common failure modes from local constrained-decoding engines:
    1. Clean valid JSON (fast path).
    2. Valid JSON followed by trailing content (e.g. a field regenerated outside
       the object, or an extra closing brace) — recovered via raw_decode which
       stops after the first complete JSON value.
    3. JSON buried inside prose or a markdown code fence — recovered by scanning
       for the first ``{`` … last ``}`` span.
    """
    if not content or not isinstance(content, str):
        return None
    text = strip_code_fence(content)
    # Fast path: the whole string is a single valid JSON object.
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    # Middle path: valid JSON followed by trailing content (e.g. Gemma-4
    # constrained-decoding placing a field *outside* the closing brace).
    # raw_decode parses exactly the first complete JSON value and ignores the rest.
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj, _ = json.JSONDecoder().raw_decode(stripped)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Last resort: salvage the first {...} span (handles prose wrapping the JSON).
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


__all__ = ["strip_code_fence", "coerce_json_object"]
