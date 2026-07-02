"""DRAFT specialists as code Nodes (feat-9517bed8, slice 6).

Two bounded specialists over the existing ``applications/{slug}/.apply-state``
contract, built on the slice-1 driver and the slice-2 backends:

* ``prose-write`` — ONE backend call (routable per-node to cloud Claude via
  ``llm.apply.node_backends`` for quality) that authors ``prose-draft.md`` as
  Markdown carried in a one-field JSON envelope (:class:`ProseDraft`). Markdown
  is the contract — we do NOT force a rigid resume JSON schema onto the writer.
  A would-fabricate situation returns ``status=halt`` (the driver's halt
  mechanism) so NO fabricated metric/claim is ever written.

* ``prose-qa`` — the gate is MOSTLY DETERMINISTIC: the five blocking checks
  (:func:`run_prose_qa_checks`) run in pure CODE, NOT a model call, so a 4B local
  model never policies its own quality. Returns ``pass`` | ``revise`` | ``halt``
  and writes ``ai-tell-report.json``.

The bounded revise loop reuses the driver's :class:`LoopStage`: a single
:class:`ProseDraftNode` performs ONE write+qa cycle per iteration, feeding the
prior blocking findings back into the writer, and the loop runs at most
:data:`MAX_DRAFT_ITERS` times before surfacing the remaining findings as a halt.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.driver import (
    LoopStage,
    Node,
    NodeResult,
    Pipeline,
    StructuredBackend,
)
from jobsmith.apply_local.inputs import (
    MasterData,
    build_prose_write_prompt,
    load_master_data,
    load_voice_guide,
)
from jobsmith.apply_local.schemas import response_format
from jobsmith.config import JobsmithConfig

NODE_PROSE_WRITE = "prose-write"
NODE_PROSE_QA = "prose-qa"

ART_PROSE_DRAFT = "prose-draft.md"
ART_AI_TELL_REPORT = "ai-tell-report.json"

# Bounded prose-write<->prose-qa loop cap (specialist-contracts: max 3 iterations).
MAX_DRAFT_ITERS = 3

# Five-check thresholds + word lists (authoritative: apply-prose-qa.md step 3).
BULLET_WORD_LIMIT = 25
METRIC_CLUSTER_LIMIT = 3
EM_DASH = "—"
STOCK_PHRASES = ("leveraged", "cutting-edge", "cross-functionally", "spearheaded", "synergies")

CHECK_KEYS = (
    "bullet_word_count",
    "metric_cluster_count",
    "parenthetical_tech_list",
    "em_dash",
    "stock_phrases",
)

# A numeric quantity: optional $, digits/commas, optional decimal, optional unit.
_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|bn|[BMKx])?", re.IGNORECASE)
# Baseline connectors collapse two numbers into ONE metric cluster.
_BASELINE_RE = re.compile(r"\b(?:to|from|vs|versus)\b|→|->", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_TECH_TOKEN_RE = re.compile(r"^[A-Za-z][\w.+#/-]*$")


# ---------------------------------------------------------------------------
# prose-write envelope — Markdown carried through the structured transport
# ---------------------------------------------------------------------------


class ProseDraft(BaseModel):
    """prose-draft.md content (Markdown) + a no-fabrication signal.

    ``markdown`` is the verbatim ``prose-draft.md`` body (Professional Summary +
    tailored bullets). ``would_fabricate``, when set, is the offending claim the
    writer refuses to invent — it triggers a halt BEFORE any artifact is written.
    """

    model_config = ConfigDict(extra="ignore")

    markdown: str = ""
    would_fabricate: str | None = None


# ---------------------------------------------------------------------------
# Filesystem helpers (atomic; mirror checkpoint.py's tmp+replace policy)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# prose-qa — FIVE deterministic blocking checks (pure code, no model)
# ---------------------------------------------------------------------------


def _bullet_lines(markdown: str) -> list[str]:
    """Return the bullet bodies (text after a leading ``- ``) from the draft."""
    return [ln.strip()[2:].strip() for ln in markdown.splitlines() if ln.strip().startswith("- ")]


def _first_words(text: str, n: int = 8) -> str:
    return " ".join(text.split()[:n])


def _metric_clusters(bullet: str) -> int:
    """Count INDEPENDENT numeric clusters; a baseline pair counts as one."""
    matches = list(_NUMBER_RE.finditer(bullet))
    if not matches:
        return 0
    clusters = 1
    for prev, cur in zip(matches, matches[1:], strict=False):
        if not _BASELINE_RE.search(bullet[prev.end() : cur.start()]):
            clusters += 1
    return clusters


def _parenthetical_tech_lists(bullet: str) -> list[str]:
    """Return parentheticals holding 2+ comma-separated technology names."""
    hits: list[str] = []
    for match in _PAREN_RE.finditer(bullet):
        items = [part.strip() for part in match.group(1).split(",")]
        if len(items) >= 2 and all(item and _TECH_TOKEN_RE.match(item) for item in items):
            hits.append(match.group(0))
    return hits


def _check_bullet(bullet: str) -> dict[str, list[str]]:
    """Run all five checks on one bullet; return ``{check_key: [detail, ...]}``."""
    found: dict[str, list[str]] = {key: [] for key in CHECK_KEYS}
    words = len(bullet.split())
    if words > BULLET_WORD_LIMIT:
        found["bullet_word_count"].append(f"bullet_too_long: {words} words — '{_first_words(bullet)}...'")
    clusters = _metric_clusters(bullet)
    if clusters >= METRIC_CLUSTER_LIMIT:
        found["metric_cluster_count"].append(
            f"too_many_metrics: {clusters} independent numbers in '{_first_words(bullet)}...'"
        )
    found["parenthetical_tech_list"].extend(f"parenthetical_tech_list: '{p}'" for p in _parenthetical_tech_lists(bullet))
    if EM_DASH in bullet:
        found["em_dash"].append(f"em_dash_in_bullet: '{bullet}'")
    low = bullet.lower()
    found["stock_phrases"].extend(f"stock_phrase: '{p}'" for p in STOCK_PHRASES if p in low)
    return found


def run_prose_qa_checks(markdown: str, *, iteration: int, max_iter: int = MAX_DRAFT_ITERS) -> dict[str, Any]:
    """Run the five blocking checks over ``markdown`` and decide pass/revise/halt.

    This is the prose-qa gate: pure deterministic code (NO model call). Every
    violation contributes one ``blocking_findings`` entry. ``decision`` is
    ``pass`` when there are none, ``halt`` once ``iteration`` reaches ``max_iter``
    with findings remaining, else ``revise``.

    An empty/whitespace draft is itself a blocking finding (``check="empty_draft"``)
    so a 0-byte response from prose-write NEVER returns ``pass`` — it is ``revise``
    until the cap, then ``halt``.
    """
    if not markdown or not markdown.strip():
        empty_finding = {
            "category": "empty_draft",
            "span": "draft is empty or whitespace-only",
            "suggestion": "prose-write must return a non-empty markdown body",
        }
        decision = "halt" if iteration >= max_iter else "revise"
        return {
            "iteration": iteration,
            "decision": decision,
            "blocking_findings": [empty_finding],
            "advisory_findings": [],
            "bullet_style_checks": {key: {"violations": 0, "details": []} for key in CHECK_KEYS},
            "words_unchanged": [],
            "calibration_metrics": {"false_positive_estimate": 0.0},
        }

    aggregate: dict[str, list[str]] = {key: [] for key in CHECK_KEYS}
    for bullet in _bullet_lines(markdown):
        for key, details in _check_bullet(bullet).items():
            aggregate[key].extend(details)

    blocking = [
        {"category": key, "span": detail, "suggestion": "remove or restructure (no new facts)"}
        for key in CHECK_KEYS
        for detail in aggregate[key]
    ]
    if not blocking:
        decision = "pass"
    elif iteration >= max_iter:
        decision = "halt"
    else:
        decision = "revise"
    return {
        "iteration": iteration,
        "decision": decision,
        "blocking_findings": blocking,
        "advisory_findings": [],
        "bullet_style_checks": {
            key: {"violations": len(aggregate[key]), "details": aggregate[key]} for key in CHECK_KEYS
        },
        "words_unchanged": [],
        "calibration_metrics": {"false_positive_estimate": 0.0},
    }


# ---------------------------------------------------------------------------
# prose-write node — structured backend call with a would-fabricate halt
# ---------------------------------------------------------------------------


_PLAIN_SYSTEM_PROMPT = (
    "You are a resume prose writer. Write the resume content as plain Markdown — "
    "a Professional Summary section followed by Tailored Bullets — with NO JSON, "
    "no code fences, and no commentary.\n"
    "If you cannot include a specific metric or achievement because it is not "
    "present in the provided context and including it would require invention, "
    "start your ENTIRE response with exactly:\n"
    "WOULD_FABRICATE: <one-line description of the claim you cannot verify>\n"
    "Otherwise, write only the Markdown body."
)


def make_prose_write_node(master: MasterData, *, voice_guide: str, plain_text_mode: bool = False) -> Node:
    """Build the prose-write :class:`Node` (Markdown-in-JSON envelope).

    When ``plain_text_mode=True`` the system prompt asks for plain Markdown (no
    JSON schema) and the backend is expected to wrap the response itself — see
    :class:`~jobsmith.apply_local.backends.OpenAICompatBackend` with
    ``plain_text_mode=True``.  Use this with small local models where constrained
    JSON decoding fights long-form generation.
    """
    schema = response_format(ProseDraft, "prose_draft")

    def build_messages(ctx: Mapping[str, Any]) -> list[dict]:
        prompt = build_prose_write_prompt(
            master,
            ctx.get("jd_parsed") or {},
            ctx.get("fit_score"),
            ctx.get("bullet_selection"),
            voice_guide=voice_guide,
            prior_findings=ctx.get("prior_findings"),
        )
        if plain_text_mode:
            system_content = _PLAIN_SYSTEM_PROMPT
        else:
            schema_obj = (schema.get("json_schema") or {}).get("schema", schema)
            system_content = (
                "You are a resume prose writer. Respond with ONE JSON object matching "
                "this schema and nothing else:\n" + json.dumps(schema_obj)
            )
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ]

    def halt(obj: BaseModel) -> str | None:
        assert isinstance(obj, ProseDraft)
        return f"WOULD_FABRICATE: {obj.would_fabricate}" if obj.would_fabricate else None

    return Node(NODE_PROSE_WRITE, ProseDraft, schema, build_messages=build_messages, halt_predicate=halt)


# ---------------------------------------------------------------------------
# prose-qa loop node — one write+qa cycle per LoopStage iteration
# ---------------------------------------------------------------------------


class ProseDraftNode:
    """Loop node (duck-types :class:`Node`) running ONE prose-write+prose-qa cycle.

    Each :meth:`run` runs the writer, then the deterministic QA checks, writes
    ``prose-draft.md`` + ``ai-tell-report.json``, and returns the QA decision in
    ``data``. A writer halt (would-fabricate) short-circuits BEFORE any write, so
    nothing is persisted. The driver's :class:`LoopStage` re-runs this node until
    the QA decision is ``pass`` or the iteration cap surfaces the findings.
    """

    def __init__(self, write_node: Node, *, slug: str, root: Any, max_iter: int = MAX_DRAFT_ITERS) -> None:
        self.name = NODE_PROSE_QA
        self.write_node = write_node
        self.slug = slug
        self.root = root
        self.max_iter = max(1, max_iter)
        self._iteration = 0
        self._prior_findings: list[dict] = []

    def run(self, backend: StructuredBackend, *, context: Mapping[str, Any] | None = None) -> NodeResult:
        self._iteration += 1
        ctx = dict(context or {})
        ctx["prior_findings"] = self._prior_findings
        write = self.write_node.run(backend, context=ctx)
        if write.status == "halt" or not write.parse_ok or write.data is None:
            return write  # would-fabricate / unparseable -> nothing written
        return self._qa(write.data.get("markdown", ""))

    def _qa(self, markdown: str) -> NodeResult:
        report = run_prose_qa_checks(markdown, iteration=self._iteration, max_iter=self.max_iter)
        self._prior_findings = report["blocking_findings"]
        self._write_artifacts(markdown, report)
        decision = report["decision"]
        reason = None
        if decision == "halt":
            spans = "; ".join(f["span"] for f in report["blocking_findings"])
            reason = f"PROSE_QA_UNRESOLVED after {self.max_iter} iterations: {spans}"
        return NodeResult(
            name=self.name,
            status="halt" if decision == "halt" else "ok",
            parse_ok=True,
            reason=reason,
            data={"markdown": markdown, "decision": decision, "report": report},
        )

    def _write_artifacts(self, markdown: str, report: dict) -> None:
        state = apply_state_dir(self.slug, root=self.root)
        _atomic_write(state / ART_PROSE_DRAFT, markdown)
        _atomic_write(state / ART_AI_TELL_REPORT, json.dumps(report, indent=2, ensure_ascii=False))


def _qa_passed(result: NodeResult) -> bool:
    return bool(result.data) and result.data.get("decision") == "pass"


# ---------------------------------------------------------------------------
# Pipeline factory
# ---------------------------------------------------------------------------


def build_draft_pipeline(config: JobsmithConfig, slug: str, *, root: Any) -> Pipeline:
    """Build the bounded prose-write<->prose-qa loop pipeline for ``slug``.

    Master data is loaded ONCE (read-only) and the voice guide assembled from
    inputs.py; both are injected into the writer's prompt. The loop runs at most
    :data:`MAX_DRAFT_ITERS` times via the driver's :class:`LoopStage`. The
    ``slug`` is threaded through the context so the loop node writes its artifacts
    under ``applications/{slug}/.apply-state``.

    ``plain_text_mode`` is read from the per-node backend config for
    :data:`NODE_PROSE_WRITE` (falling back to the default node backend) so the
    prose-write Node's system prompt matches the backend's completion mode.
    """
    repo_root = Path(root)
    master = load_master_data(config, repo_root=repo_root)
    voice_guide = load_voice_guide(config, repo_root=repo_root)
    # Derive plain_text_mode from the per-node (then default) backend config so
    # the node's system prompt matches the backend's completion strategy.
    # Use the same effective-flags resolution as resolve_backend so that Gemma
    # auto-defaults (plain_text_mode=True for prose-write) propagate to the
    # system prompt path even when the user hasn't set plain_text_mode explicitly.
    _node_cfg = config.llm.apply.node_backends.get(NODE_PROSE_WRITE) or config.llm.apply.node_backend
    if _node_cfg is not None and _node_cfg.provider == "openai_compatible":
        from jobsmith.apply_local.backends import _effective_openai_compat_flags
        _, _plain, _ = _effective_openai_compat_flags(_node_cfg, NODE_PROSE_WRITE)
    elif _node_cfg is not None:
        _plain = bool(_node_cfg.plain_text_mode)
    else:
        _plain = False
    write_node = make_prose_write_node(master, voice_guide=voice_guide, plain_text_mode=_plain)
    draft_node = ProseDraftNode(write_node, slug=slug, root=repo_root, max_iter=MAX_DRAFT_ITERS)
    loop = LoopStage(draft_node, _qa_passed, max_iter=MAX_DRAFT_ITERS)
    return Pipeline([loop], slug=slug, root=repo_root)


__all__ = [
    "NODE_PROSE_WRITE",
    "NODE_PROSE_QA",
    "ART_PROSE_DRAFT",
    "ART_AI_TELL_REPORT",
    "MAX_DRAFT_ITERS",
    "ProseDraft",
    "run_prose_qa_checks",
    "make_prose_write_node",
    "ProseDraftNode",
    "build_draft_pipeline",
]
