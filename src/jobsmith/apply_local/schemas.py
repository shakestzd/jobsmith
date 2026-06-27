"""Pydantic models for the GATHER specialists' ``.apply-state`` artifacts
(feat-d46dde68, slice 5).

These shapes are LIFTED from the FROZEN
``plugin/agents/apply/specialist-contracts.yaml`` — they are NOT invented here.
Each model both (a) validates a node's structured-JSON output and (b) is the
on-disk shape of the bare artifact the node writes:

* :class:`JdParsed`        -> ``.apply-state/jd-parsed.json``     (apply-jd-parser)
* :class:`FitScore`        -> ``.apply-state/fit-score.json``     (apply-fit-scorer)
* :class:`BulletSelection` -> ``.apply-state/bullet-selection.json`` (apply-bullet-selector)
* :class:`BulletDecisions` -> ``.apply-state/bullet-decisions.json``

``score`` on :class:`FitScore` is DERIVED: the local gemma call emits the
core scorer's 0-100 ``score_raw`` and the model normalises it to a 0.0-1.0
``score`` during validation, so normalisation cannot be skipped or drift.

:func:`response_format` turns any model into the OpenAI-style ``response_format``
dict the driver/backends pass verbatim; the ``name`` is what test stubs (and the
real router) key on to dispatch a payload per node.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

# ---------------------------------------------------------------------------
# Artifact filenames (single source of truth for on-disk names)
# ---------------------------------------------------------------------------

ART_JD_PARSED = "jd-parsed.json"
ART_FIT_SCORE = "fit-score.json"
ART_BULLET_SELECTION = "bullet-selection.json"
ART_BULLET_DIFF = "bullet-diff.md"
ART_BULLET_DECISIONS = "bullet-decisions.json"

# Enumerations from the frozen contract.
RoleType = Literal[
    "data-analyst", "data-engineer", "ai-engineer", "finance", "renewable-energy", "general"
]
LocationType = Literal["remote", "hybrid", "onsite", "unknown"]
Confidence = Literal["high", "medium", "low"]
# STRONG|HAVE|PARTIAL|GAP|BLOCKER — GAP/BLOCKER are uncovered must-haves.
MustHaveLevel = Literal["STRONG", "HAVE", "PARTIAL", "GAP", "BLOCKER"]
# A must-have at GAP or BLOCKER level has no master coverage (no-fabrication gate).
UNCOVERED_LEVELS: frozenset[str] = frozenset({"GAP", "BLOCKER"})


def response_format(model_cls: type[BaseModel], name: str) -> dict:
    """Return the OpenAI-style ``response_format`` dict for ``model_cls``."""
    return {"type": "json_schema", "json_schema": {"name": name, "schema": model_cls.model_json_schema()}}


# ---------------------------------------------------------------------------
# jd-parsed.json
# ---------------------------------------------------------------------------


class JdParsed(BaseModel):
    """Structured JD fields the rest of the pipeline reasons over."""

    model_config = ConfigDict(extra="ignore")

    company: str
    position: str
    location: str | None = None
    location_type: LocationType = "unknown"
    salary_range: str | None = None
    req_id: str | None = None
    apply_url: str = ""
    named_hm: str | None = None
    role_type: RoleType
    # >= 2 enforced by the schema so a thin parse reasks rather than under-filling.
    must_haves: list[str] = Field(min_length=2)
    nice_to_haves: list[str] = Field(default_factory=list)
    top_keywords: list[str] = Field(default_factory=list)
    jd_text_clean: str = ""
    jd_url: str | None = None


# ---------------------------------------------------------------------------
# fit-score.json
# ---------------------------------------------------------------------------


class MustHaveCoverage(BaseModel):
    """One JD must-have and how strongly master evidence covers it."""

    model_config = ConfigDict(extra="ignore")

    requirement: str
    level: MustHaveLevel
    evidence: str = ""


class FitScore(BaseModel):
    """fit-score.json — the SINGLE local call that replaces the cloud core scorer.

    The model emits ``score_raw`` (0-100, the core scorer's native range);
    ``score`` is always derived as ``score_raw / 100`` clamped to [0, 1].
    """

    model_config = ConfigDict(extra="ignore")

    specialty: str = "none"
    score_raw: int = Field(ge=0, le=100)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    matched_evidence: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    must_have_table: list[MustHaveCoverage] = Field(default_factory=list)
    pitch: str = ""

    @model_validator(mode="after")
    def _normalize_score(self) -> FitScore:
        self.score = max(0.0, min(1.0, self.score_raw / 100.0))
        return self

    def uncovered_requirements(self) -> list[str]:
        """Must-haves the table marks GAP/BLOCKER — no master coverage."""
        return [m.requirement for m in self.must_have_table if m.level in UNCOVERED_LEVELS]


# ---------------------------------------------------------------------------
# bullet-selection.json (+ companions)
# ---------------------------------------------------------------------------


class BulletChoice(BaseModel):
    """One master bullet's fate within a position."""

    model_config = ConfigDict(extra="ignore")

    master_bullet_id: str
    included: bool
    rephrased: str | None = None
    reason_if_dropped: str | None = None


class PositionSelection(BaseModel):
    """Per-position selection + ordering of master bullets."""

    model_config = ConfigDict(extra="ignore")

    company: str = ""
    title: str = ""
    bullets: list[BulletChoice] = Field(default_factory=list)


class AnchorDrop(BaseModel):
    """A dropped anchor bullet with its logged reason + replacing JD keyword."""

    model_config = ConfigDict(extra="ignore")

    bullet_id: str
    reason: str = ""
    # Field name matches the frozen contract verbatim.
    JD_keyword_replacing_it: str = ""


class RestorationQueue(BaseModel):
    """Slice C.1 — pre-computed dropped-but-restorable queue for the reviewer."""

    model_config = ConfigDict(extra="ignore")

    bullets: list[str] = Field(default_factory=list)
    context_hash: str = ""


class BulletSelection(BaseModel):
    """bullet-selection.json — selection/reordering of master bullets per JD."""

    model_config = ConfigDict(extra="ignore")

    positions: list[PositionSelection] = Field(default_factory=list)
    anchor_bullets_master: list[str] = Field(default_factory=list)
    anchor_bullets_kept: list[str] = Field(default_factory=list)
    anchor_bullets_dropped: list[AnchorDrop] = Field(default_factory=list)
    # Must-haves the selector could not map to any master bullet (no-fab gate).
    uncovered_must_haves: list[str] = Field(default_factory=list)
    restoration_queue: RestorationQueue | None = None


class BulletDecisions(RootModel[dict[str, str]]):
    """bullet-decisions.json — ``{bullet_id: reason}`` for every dropped anchor."""

    root: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "ART_JD_PARSED",
    "ART_FIT_SCORE",
    "ART_BULLET_SELECTION",
    "ART_BULLET_DIFF",
    "ART_BULLET_DECISIONS",
    "RoleType",
    "LocationType",
    "Confidence",
    "MustHaveLevel",
    "UNCOVERED_LEVELS",
    "response_format",
    "JdParsed",
    "MustHaveCoverage",
    "FitScore",
    "BulletChoice",
    "PositionSelection",
    "AnchorDrop",
    "RestorationQueue",
    "BulletSelection",
    "BulletDecisions",
]
