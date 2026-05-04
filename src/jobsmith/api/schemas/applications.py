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


__all__ = ["Application"]
