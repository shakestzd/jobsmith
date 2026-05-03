"""DB → typed model loader for the marimo apply notebook.

Pure logic module — no marimo dependency, fully testable with pytest.

Usage
-----
    from jobsmith.marimo.loader import load_sections, ApplicationNotFound, Sections

    sections = load_sections(slug, db_path)  # raises ApplicationNotFound if missing
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsmith.db import (
    get_apply_run_by_slug,
    get_specialist_outputs,
    open_pipeline_db,
)
from jobsmith.db_models import (
    ATSCheck,
    BulletSelection,
    FitScore,
    HMSnippet,
    TextArtifact,
    deserialize_output,
)


class ApplicationNotFoundError(LookupError):
    """Raised when the requested slug is absent from apply_runs."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"No apply run found for slug: {slug!r}")
        self.slug = slug


# Backward-compatible alias — the task spec refers to ApplicationNotFound.
ApplicationNotFound = ApplicationNotFoundError


@dataclass
class Sections:
    """Per-section content loaded from the pipeline DB.

    Each field is ``None`` when the corresponding specialist output has not
    been ingested yet — callers should render a placeholder in that case.
    """

    work_bullets: BulletSelection | None = field(default=None)
    fit_score: FitScore | None = field(default=None)
    hm_snippet: HMSnippet | None = field(default=None)
    cover_letter: str | None = field(default=None)
    ats_check: ATSCheck | None = field(default=None)
    prose_draft: TextArtifact | None = field(default=None)


# Maps specialist_outputs.kind → Sections field name
_KIND_TO_FIELD: dict[str, str] = {
    "bullet-selection": "work_bullets",
    "fit-score": "fit_score",
    "hm-snippet": "hm_snippet",
    "ats-check": "ats_check",
    "prose-draft": "prose_draft",
}


def _read_cover_letter(applications_dir: Path, slug: str) -> str | None:
    """Read the slug's cover-letter content for review display.

    Search order (first hit wins):

    1. ``<app>/cover-letter-draft.md`` — the apply pipeline writes the
       reviewable draft here at app root (apply-cover-letter-writer).
    2. ``<app>/cover-letter-final.md`` — written by Finalize once the
       user accepts amendments and clicks Finalize (slice 7).
    3. ``<app>/documents/cover-letter-final.md`` — legacy assemble.py
       output kept for back-compat with older slugs.

    Returns None when none of the candidates exist (the section card
    then shows the "not yet generated" placeholder).
    """
    app_dir = applications_dir / slug
    candidates = (
        app_dir / "cover-letter-draft.md",
        app_dir / "cover-letter-final.md",
        app_dir / "documents" / "cover-letter-final.md",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return None


def load_sections(
    slug: str,
    db_path: Path,
    *,
    applications_dir: Path | None = None,
) -> Sections:
    """Load all available specialist outputs for *slug* from *db_path*.

    Parameters
    ----------
    slug:
        Application slug (e.g. ``"acme-swe-2024"``).
    db_path:
        Absolute path to ``private/jobsmith.db``.
    applications_dir:
        Override for the per-slug application directory used to read the
        cover letter. Defaults to ``<db_path>.parent / "applications"``,
        which matches the production layout (``private/jobsmith.db`` lives
        beside ``private/applications/``).

    Returns
    -------
    Sections
        Populated dataclass; any missing specialist output is ``None``.

    Raises
    ------
    ApplicationNotFound
        When *slug* has no row in ``apply_runs``.
    """
    conn = open_pipeline_db(db_path)
    try:
        run_row = get_apply_run_by_slug(conn, slug)
        if run_row is None:
            raise ApplicationNotFound(slug)

        run_id: str = run_row["run_id"]
        rows = get_specialist_outputs(conn, run_id)
    finally:
        conn.close()

    sections = Sections()

    for row in rows:
        kind: str = row["kind"]
        field_name = _KIND_TO_FIELD.get(kind)
        if field_name is None:
            continue  # jd-parsed, company-research, etc. — not displayed here
        typed_model: Any = deserialize_output(row)
        setattr(sections, field_name, typed_model)

    # Cover letter lives on disk; _read_cover_letter checks both
    # cover-letter-draft.md (apply pipeline output) and -final.md
    # (post-Finalize, slice 7).
    apps_dir = (
        applications_dir
        if applications_dir is not None
        else db_path.parent / "applications"
    )
    sections.cover_letter = _read_cover_letter(apps_dir, slug)

    return sections


__all__ = [
    "ApplicationNotFound",
    "ApplicationNotFoundError",
    "Sections",
    "load_sections",
]
