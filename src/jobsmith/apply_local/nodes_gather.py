"""GATHER specialists as code Nodes (feat-d46dde68, slice 5).

Three bounded LLM tasks over the existing ``applications/{slug}/.apply-state``
contract, built on the slice-1 driver (:class:`Node` / :class:`Pipeline`) and the
slice-2 backends:

* ``jd-parse``     — JD text/url -> ``jd-parsed.json``
* ``fit-score``    — ONE local call (replaces the cloud core scorer) ->
  ``fit-score.json`` (0-100 normalised to 0-1 by the schema)
* ``bullet-select`` -> ``bullet-selection.json`` + companions: ``bullet-diff.md``,
  ``bullet-decisions.json``, and tailored ``documents/work.yml`` + ``skill.yml``

No-fabrication v1 = HALT-AND-SURFACE (ports NONE of the cloud enforcers): a JD
must-have with no master coverage, or an anchor bullet dropped without a logged
reason, makes the node return ``status=halt`` (the driver's halt mechanism) so
the pipeline short-circuits and surfaces the blocker. It never invents to fill a
gap. Anchor logic REUSES :mod:`jobsmith.guard` / :mod:`jobsmith.anchors` so the
local path and the ``jobsmith anchor-check`` CLI agree on what is load-bearing.

:class:`GatherStage` wraps a driver :class:`Node` to (a) resume from the bare
contract artifact, (b) run the bounded reask/halt loop, and (c) write the bare
artifact(s) only on a final success — so a halt is never cached.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.driver import Node, NodeResult, Pipeline, StructuredBackend
from jobsmith.apply_local.inputs import (
    MasterData,
    build_bullet_select_prompt,
    build_fit_score_prompt,
    build_jd_parse_prompt,
    load_master_data,
)
from jobsmith.apply_local.schemas import (
    ART_BULLET_DECISIONS,
    ART_BULLET_DIFF,
    ART_BULLET_SELECTION,
    ART_FIT_SCORE,
    ART_JD_PARSED,
    BulletSelection,
    FitScore,
    JdParsed,
    response_format,
)
from jobsmith.config import JobsmithConfig
from jobsmith.guard import Bullet, GuardResult, render_diff_md

NODE_JD_PARSE = "jd-parse"
NODE_FIT_SCORE = "fit-score"
NODE_BULLET_SELECT = "bullet-select"

EmitFn = Callable[..., None]


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


def _write_json(path: Path, obj: Any) -> None:
    _atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False))


def _state_path(slug: str, name: str, root: Any) -> Path:
    return apply_state_dir(slug, root=root) / name


def _docs_path(slug: str, name: str, root: Any) -> Path:
    return apply_state_dir(slug, root=root).parent / "documents" / name


def _load_artifact(slug: str, filename: str, model_cls: type, root: Any) -> dict | None:
    """Return a validated bare artifact (for resume), or None if absent/invalid."""
    path = _state_path(slug, filename, root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return model_cls.model_validate(data).model_dump(mode="json")
    except ValidationError:
        return None


def _messages(schema: dict, prompt: str) -> list[dict]:
    """System (schema) + user (assembled prompt) messages for a node."""
    schema_obj = (schema.get("json_schema") or {}).get("schema", schema)
    return [
        {
            "role": "system",
            "content": (
                "You produce ONE JSON object matching this schema and nothing else:\n"
                + json.dumps(schema_obj)
            ),
        },
        {"role": "user", "content": prompt},
    ]


# ---------------------------------------------------------------------------
# Stage — resume from bare artifact, run node, persist on final success only
# ---------------------------------------------------------------------------


class GatherStage:
    """A pipeline :class:`~jobsmith.apply_local.driver.Stage` for one gather node.

    Resume is keyed on the bare contract artifact (e.g. ``jd-parsed.json``): if
    it exists and schema-validates, the LLM call is skipped. A halt / parse
    failure writes NOTHING, so it is re-evaluated on the next run (driver policy).
    """

    def __init__(self, node: Node, *, primary_artifact: str, model_cls: type, emit: EmitFn) -> None:
        self.node = node
        self.primary_artifact = primary_artifact
        self.model_cls = model_cls
        self.emit = emit

    def run(self, backend: StructuredBackend, *, slug: str, root: Any, ctx: Mapping[str, Any]) -> list[NodeResult]:
        cached = _load_artifact(slug, self.primary_artifact, self.model_cls, root)
        if cached is not None:
            return [NodeResult(name=self.node.name, status="ok", parse_ok=True, data=cached)]
        result = self.node.run(backend, context=ctx)
        if result.is_final and result.data is not None:
            self.emit(result.data, slug=slug, root=root, ctx=ctx)
        return [result]


# ---------------------------------------------------------------------------
# Anchor evaluation (in-memory mirror of jobsmith.guard.check_anchors)
# ---------------------------------------------------------------------------


def evaluate_anchor_selection(master_bullets: list[Bullet], selection: BulletSelection) -> GuardResult:
    """Cross-reference master anchors against a selection, in memory.

    Same logic as :func:`jobsmith.guard.check_anchors` but operating on the
    validated model (not files), so it can run inside a halt predicate before any
    artifact is written. ``exit_code == 1`` means an anchor was dropped without a
    logged reason.
    """
    anchors = [b for b in master_bullets if b.is_anchor]
    sel_index = {ch.master_bullet_id: ch for pos in selection.positions for ch in pos.bullets}
    decisions = {d.bullet_id: d.reason.strip() for d in selection.anchor_bullets_dropped if d.reason.strip()}

    kept: list[Bullet] = []
    dropped_with_reason: list[tuple[Bullet, str]] = []
    dropped_without_reason: list[Bullet] = []
    has_selection = bool(sel_index)

    for b in anchors:
        choice = sel_index.get(b.bullet_id)
        if choice is not None and choice.included:
            kept.append(b)
            continue
        if choice is None and not has_selection:
            kept.append(b)
            continue
        reason = ((choice.reason_if_dropped if choice else None) or decisions.get(b.bullet_id, "") or "").strip()
        if reason and reason != "pending-inquiry":
            dropped_with_reason.append((b, reason))
        else:
            dropped_without_reason.append(b)

    return GuardResult(
        exit_code=1 if dropped_without_reason else 0,
        anchor_bullets=anchors,
        kept=kept,
        dropped_without_reason=dropped_without_reason,
        dropped_with_reason=dropped_with_reason,
    )


def _build_decisions(master_bullets: list[Bullet], selection: BulletSelection) -> dict[str, str]:
    """``{bullet_id: reason}`` for dropped anchors, with anchor_reason propagation."""
    by_id = {b.bullet_id: b for b in master_bullets}
    result = evaluate_anchor_selection(master_bullets, selection)
    out: dict[str, str] = {}
    for bullet, reason in result.dropped_with_reason:
        master = by_id.get(bullet.bullet_id)
        if master is not None and master.anchor_reason:
            out[bullet.bullet_id] = f"anchor_reason: {master.anchor_reason}; {reason}"
        else:
            out[bullet.bullet_id] = reason
    return out


def _build_tailored_work(selection: BulletSelection, master: MasterData) -> list[dict]:
    """Build tailored work.yml — selection + ordering + phrasing only (no facts)."""
    by_id = {b.bullet_id: b for b in master.work_bullets}
    raw_by_key: dict[tuple[str, str], dict] = {}
    for role in master.work:
        if isinstance(role, dict):
            raw_by_key[(str(role.get("title", "")), str(role.get("location", "")))] = role
    out: list[dict] = []
    for pos in selection.positions:
        role = raw_by_key.get((pos.title, pos.company), {})
        details: list[str] = []
        for choice in pos.bullets:
            if not choice.included:
                continue
            bullet = by_id.get(choice.master_bullet_id)
            text = choice.rephrased or (bullet.text if bullet else "")
            if text:
                details.append(text)
        out.append(
            {
                "title": pos.title or role.get("title", ""),
                "location": pos.company or role.get("location", ""),
                "date": role.get("date", ""),
                "description": role.get("description", ""),
                "details": details,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Emit functions — write the bare contract artifacts
# ---------------------------------------------------------------------------


def _emit_jd(data: dict, *, slug: str, root: Any, ctx: Mapping[str, Any] | None = None) -> None:
    _write_json(_state_path(slug, ART_JD_PARSED, root), data)


def _emit_fit(data: dict, *, slug: str, root: Any, ctx: Mapping[str, Any] | None = None) -> None:
    _write_json(_state_path(slug, ART_FIT_SCORE, root), data)


def _emit_bullets(master: MasterData, data: dict, *, slug: str, root: Any) -> None:
    selection = BulletSelection.model_validate(data)
    _write_json(_state_path(slug, ART_BULLET_SELECTION, root), data)
    _write_json(_state_path(slug, ART_BULLET_DECISIONS, root), _build_decisions(master.work_bullets, selection))
    result = evaluate_anchor_selection(master.work_bullets, selection)
    _atomic_write(_state_path(slug, ART_BULLET_DIFF, root), render_diff_md(result, master.work_path or Path("work.yml")))
    _atomic_write(
        _docs_path(slug, "work.yml", root),
        yaml.safe_dump(_build_tailored_work(selection, master), sort_keys=False, allow_unicode=True),
    )
    _atomic_write(
        _docs_path(slug, "skill.yml", root),
        yaml.safe_dump(master.skill, sort_keys=False, allow_unicode=True),
    )


# ---------------------------------------------------------------------------
# Stage factories
# ---------------------------------------------------------------------------


def make_jd_parse_stage() -> GatherStage:
    schema = response_format(JdParsed, "jd_parsed")

    def build_messages(ctx: Mapping[str, Any]) -> list[dict]:
        return _messages(
            schema,
            build_jd_parse_prompt(
                jd_text=ctx.get("jd_text"),
                jd_url=ctx.get("jd_url"),
                explicit_company=ctx.get("explicit_company"),
            ),
        )

    node = Node(NODE_JD_PARSE, JdParsed, schema, build_messages=build_messages)
    return GatherStage(node, primary_artifact=ART_JD_PARSED, model_cls=JdParsed, emit=_emit_jd)


def make_fit_score_stage(master: MasterData) -> GatherStage:
    schema = response_format(FitScore, "fit_score")

    def build_messages(ctx: Mapping[str, Any]) -> list[dict]:
        return _messages(
            schema,
            build_fit_score_prompt(master, ctx.get(NODE_JD_PARSE) or {}, fast_path_scores=ctx.get("fast_path_scores")),
        )

    def halt(obj: FitScore) -> str | None:
        uncovered = obj.uncovered_requirements()
        return f"UNCOVERED_MUST_HAVE: {'; '.join(uncovered)}" if uncovered else None

    node = Node(NODE_FIT_SCORE, FitScore, schema, build_messages=build_messages, halt_predicate=halt)
    return GatherStage(node, primary_artifact=ART_FIT_SCORE, model_cls=FitScore, emit=_emit_fit)


def make_bullet_select_stage(master: MasterData) -> GatherStage:
    schema = response_format(BulletSelection, "bullet_selection")
    bullets = master.work_bullets

    def build_messages(ctx: Mapping[str, Any]) -> list[dict]:
        return _messages(
            schema,
            build_bullet_select_prompt(master, ctx.get(NODE_JD_PARSE) or {}, ctx.get(NODE_FIT_SCORE)),
        )

    def halt(obj: BulletSelection) -> str | None:
        result = evaluate_anchor_selection(bullets, obj)
        if result.exit_code == 1:
            ids = ", ".join(b.bullet_id for b in result.dropped_without_reason)
            return f"ANCHOR_DROP_REQUIRES_INQUIRY: {ids}"
        if obj.uncovered_must_haves:
            return f"UNCOVERED_MUST_HAVE: {'; '.join(obj.uncovered_must_haves)}"
        return None

    def emit(data: dict, *, slug: str, root: Any, ctx: Mapping[str, Any] | None = None) -> None:
        _emit_bullets(master, data, slug=slug, root=root)

    node = Node(NODE_BULLET_SELECT, BulletSelection, schema, build_messages=build_messages, halt_predicate=halt)
    return GatherStage(node, primary_artifact=ART_BULLET_SELECTION, model_cls=BulletSelection, emit=emit)


def build_gather_pipeline(config: JobsmithConfig, slug: str, *, root: Any) -> Pipeline:
    """Build the sequential gather pipeline: jd-parse -> fit-score -> bullet-select.

    Master data is loaded ONCE (read-only) and injected into the fit-score and
    bullet-select prompts. A missing REQUIRED master file raises
    :class:`~jobsmith.apply_local.inputs.MissingMasterDataError` before any model
    call. Sequential (not fan-out) because one local engine serializes generation
    and bullet-select consumes fit-score's coverage table.
    """
    repo_root = Path(root)
    master = load_master_data(config, repo_root=repo_root)
    stages = [
        make_jd_parse_stage(),
        make_fit_score_stage(master),
        make_bullet_select_stage(master),
    ]
    return Pipeline(stages, slug=slug, root=repo_root)


__all__ = [
    "NODE_JD_PARSE",
    "NODE_FIT_SCORE",
    "NODE_BULLET_SELECT",
    "GatherStage",
    "evaluate_anchor_selection",
    "make_jd_parse_stage",
    "make_fit_score_stage",
    "make_bullet_select_stage",
    "build_gather_pipeline",
]
