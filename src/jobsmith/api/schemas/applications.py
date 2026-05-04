"""Pydantic models for the /api/applications endpoint family.

Alignment with web/src/types.ts SampleApp
------------------------------------------
TypeScript field   Python field    Notes
-----------------  --------------  -------------------------------------------
slug               slug            identical
role               role            None when jd-parsed.json absent/unreadable
company            company         None when jd-parsed.json absent/unreadable
status             status          AppStatus union (queued/gather/draft/review/rendered)
updated            updated_at      ISO 8601 UTC string; TS field is `updated`
phase              phase           int 0-3 matching AppPhase
anchors            anchors         "pass/total" string or "—"
factcheck          factcheck       "pass", "N flagged", or "—"
renders            renders         list of .pdf filenames
url                url             relative path e.g. /applications/<slug>/

JSON serialization: snake_case Python names are serialized with model aliases
so `updated_at` becomes `updated` in the JSON response, matching SampleApp.
All other fields are already identical between Python and TypeScript.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Application(BaseModel):
    """Single application record returned by GET /api/applications."""

    slug: str
    role: str | None = None
    company: str | None = None
    # AppStatus union from types.ts: queued | gather | draft | review | rendered | running | done | failed
    status: str
    # Serialized as "updated" to match SampleApp.updated in types.ts
    updated_at: str = Field(serialization_alias="updated")
    # AppPhase: 0 | 1 | 2 | 3
    phase: int
    # "pass/total", "N/N", or "—" when not yet computed
    anchors: str = "—"
    # "pass", "N flagged", or "—"
    factcheck: str = "—"
    # Filenames of rendered artefacts under rendered/<slug>/
    renders: list[str] = Field(default_factory=list)
    # Relative URL into the site
    url: str

    model_config = {"populate_by_name": True}


class ArtifactNode(BaseModel):
    """Metadata for a single file artifact within a slug directory."""

    name: str    # filename (basename)
    path: str    # path relative to slug_dir
    size: int    # bytes
    mtime: str   # ISO 8601 UTC timestamp


class ArtifactTree(BaseModel):
    """Two-bucket tree of artifacts for a slug directory."""

    apply_state: list[ArtifactNode] = Field(default_factory=list)
    rendered: list[ArtifactNode] = Field(default_factory=list)


class ApplicationDetail(Application):
    """Rich application record returned by GET /api/applications/{slug}.

    Extends Application with artifact tree + parsed document payloads for
    ArtifactsTab, FactCheckTab, AnchorCheckTab, and ConfigTab in the frontend.
    """

    artifacts: ArtifactTree
    spec: dict | None = None               # parsed .apply-state/jd-parsed.json
    prose_draft: str | None = None         # raw markdown; size-guarded (64 KB max)
    cover_letter_draft: str | None = None  # raw markdown; size-guarded (64 KB max)
    fact_check: dict | None = None         # parsed .apply-state/fact_check.json
    anchor_check: dict | None = None       # parsed .apply-state/anchor_check.json
    bullet_selection: dict | None = None   # parsed .apply-state/bullet_selection.json
    variables: dict | None = None          # parsed _variables.yml
    config: dict | None = None             # subset of .apply-config.yaml: output + render keys
    truncated: bool = False                # True if any large field was truncated


class CreateApplicationRequest(BaseModel):
    """Request body for POST /api/applications.

    Exactly one of jd_url, jd_text, or jd_file_b64 must be set.
    """

    jd_url: str | None = None
    jd_text: str | None = None
    jd_file_b64: str | None = None  # base64-encoded text content
    verbosity: str = "-v"  # '-v' | '-vv' | '-vvv'
    skip_confirmations: bool = True
    force: bool = False  # passes --force to apply


class CreateApplicationResponse(BaseModel):
    """Response body for POST /api/applications (201 Created)."""

    slug: str
    run_id: str
    events_url: str


# ---------------------------------------------------------------------------
# Re-run endpoint models (feat-9b3cfcfd)
#
# Alignment note: RerunResponse intentionally mirrors CreateApplicationResponse
# (both return slug + run_id + events_url). They are kept as distinct classes
# so each endpoint can evolve its shape independently without coupling.
# ---------------------------------------------------------------------------

from typing import Literal  # noqa: E402 (post-class import, avoids reorder churn)


class RerunRequest(BaseModel):
    """Body for POST /api/applications/{slug}/run."""

    verbosity: Literal["-v", "-vv", "-vvv"] = "-v"
    force: bool = False


class RerunResponse(BaseModel):
    """202 Accepted body for POST /api/applications/{slug}/run."""

    slug: str
    run_id: str
    events_url: str


class RerunConflictResponse(BaseModel):
    """409 Conflict detail body — a run is already in flight for this slug."""

    slug: str
    run_id: str
    status: str = "running"
    events_url: str


__all__ = [
    "Application",
    "ApplicationDetail",
    "ArtifactNode",
    "ArtifactTree",
    "CreateApplicationRequest",
    "CreateApplicationResponse",
    "RerunRequest",
    "RerunResponse",
    "RerunConflictResponse",
]
