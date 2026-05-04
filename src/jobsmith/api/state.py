"""Pure state derivation for application slug directories.

``derive_application_state(slug_dir)`` reads filesystem artifacts under a
single <applications_dir>/<slug>/ directory and returns an Application record.
No I/O outside the provided path — easy to unit-test in isolation.

Artifact layout (see apply.py for write-side conventions)
----------------------------------------------------------
<slug>/
  .apply-state/
    jd-parsed.json          phase 1 gate; role + company source
    prose-draft.md          phase 2 gate
    anchor_check.json       anchors summary
    fact_check.json         factcheck summary
  cover-letter-draft.md     phase 3 gate (root-level)
  _quarto.yml               phase 3 gate (root-level)
  rendered/<slug>/*.pdf     rendered artefacts; presence → status "rendered"

Phase derivation
----------------
  0 (queued)        jd-parsed.json absent
  1 (gather done)   jd-parsed.json present, prose-draft.md absent
  2 (draft running) prose-draft.md present, cover-letter-draft.md absent
  3 (render-ready)  both drafts + _quarto.yml present

Status derivation
-----------------
  phase 0 → queued
  phase 1 → gather
  phase 2 → draft
  phase 3, no rendered PDF → review
  phase 3, rendered PDF exists → rendered
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from jobsmith._state_readers import load_jd_parsed

from .schemas.applications import Application

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _max_mtime(slug_dir: Path) -> str:
    """Return ISO 8601 UTC string of max mtime across all files under slug_dir.

    Falls back to the slug_dir mtime itself when the directory is empty.
    """
    mtimes: list[float] = []
    for p in slug_dir.rglob("*"):
        if p.is_file():
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                pass
    base = max(mtimes) if mtimes else slug_dir.stat().st_mtime
    return datetime.fromtimestamp(base, tz=timezone.utc).isoformat()


def _derive_phase(slug_dir: Path) -> int:
    """Return phase index 0-3 based on artifact presence."""
    state_dir = slug_dir / ".apply-state"
    jd_parsed = state_dir / "jd-parsed.json"
    prose_draft = state_dir / "prose-draft.md"
    cover_letter = slug_dir / "cover-letter-draft.md"
    quarto_yml = slug_dir / "_quarto.yml"

    if not jd_parsed.exists():
        return 0
    if not prose_draft.exists():
        return 1
    if not (cover_letter.exists() and quarto_yml.exists()):
        return 2
    return 3


def _derive_status(phase: int, slug_dir: Path, slug: str) -> str:
    """Return status string from phase + rendered artefact presence."""
    if phase == 0:
        return "queued"
    if phase == 1:
        return "gather"
    if phase == 2:
        return "draft"
    # phase 3
    rendered_dir = slug_dir / "rendered" / slug
    if rendered_dir.is_dir() and any(rendered_dir.glob("*.pdf")):
        return "rendered"
    return "review"


def _derive_renders(slug_dir: Path, slug: str) -> list[str]:
    """Return sorted list of .pdf filenames under rendered/<slug>/."""
    rendered_dir = slug_dir / "rendered" / slug
    if not rendered_dir.is_dir():
        return []
    return sorted(p.name for p in rendered_dir.iterdir() if p.suffix == ".pdf")


def _derive_anchors(state_dir: Path) -> str:
    """Read anchor_check.json and return 'pass/total' string, or '—'."""
    path = state_dir / "anchor_check.json"
    if not path.exists():
        return "—"
    try:
        data = json.loads(path.read_text())
        passed = data.get("pass", data.get("passed", 0))
        total = data.get("total", 0)
        return f"{passed}/{total}"
    except (json.JSONDecodeError, OSError, KeyError):
        _log.warning("Could not parse anchor_check.json at %s", path)
        return "—"


def _derive_factcheck(state_dir: Path) -> str:
    """Read fact_check.json and return summary string, or '—'."""
    path = state_dir / "fact_check.json"
    if not path.exists():
        return "—"
    try:
        data = json.loads(path.read_text())
        flagged = data.get("flagged", data.get("flags", 0))
        if flagged == 0:
            return "pass"
        return f"{flagged} flagged"
    except (json.JSONDecodeError, OSError, KeyError):
        _log.warning("Could not parse fact_check.json at %s", path)
        return "—"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_application_state(slug_dir: Path) -> Application:
    """Derive an Application record from a slug directory.

    Parameters
    ----------
    slug_dir:
        Absolute path to <applications_dir>/<slug>/. Must be a directory.
        No I/O is performed outside this path.

    Returns
    -------
    Application
        Fully populated model. role/company are None when jd-parsed.json is
        absent or unreadable. anchors/factcheck default to "—" when their
        source files are missing.
    """
    slug = slug_dir.name
    state_dir = slug_dir / ".apply-state"

    # Phase and status
    phase = _derive_phase(slug_dir)
    status = _derive_status(phase, slug_dir, slug)

    # Role / company from jd-parsed.json
    role: str | None = None
    company: str | None = None
    if state_dir.is_dir():
        try:
            jd = load_jd_parsed(state_dir)
            role = jd.get("position") or None
            company = jd.get("company") or None
        except Exception:
            _log.warning("Could not load jd-parsed.json for slug %s", slug)

    # Supplementary fields
    anchors = _derive_anchors(state_dir) if state_dir.is_dir() else "—"
    factcheck = _derive_factcheck(state_dir) if state_dir.is_dir() else "—"
    renders = _derive_renders(slug_dir, slug)
    updated_at = _max_mtime(slug_dir)
    url = f"/applications/{slug}/"

    return Application(
        slug=slug,
        role=role,
        company=company,
        status=status,
        updated_at=updated_at,
        phase=phase,
        anchors=anchors,
        factcheck=factcheck,
        renders=renders,
        url=url,
    )


__all__ = ["derive_application_state"]
