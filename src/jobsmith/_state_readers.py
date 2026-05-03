"""File-system readers for .apply-state/ artifacts.

These functions read the on-disk specialist outputs from a .apply-state/
directory and return plain Python dicts or strings.  The logic was
previously inlined in assemble.py; it is extracted here so that:

  - db.py (ingest_phase_outputs) can read artifacts without depending on
    the full assemble machinery.
  - scripts/backfill_db.py can iterate slugs and ingest history.
  - assemble.py re-exports from here (thin shim) — slice 9 will remove
    assemble.py entirely.

Nothing in this module writes to disk or touches the DB.

Public functions
----------------
load_jd_parsed(state_dir)       -> dict[str, Any]
load_fit_score(state_dir)       -> dict[str, Any]
load_bullet_selection(state_dir)-> dict[str, Any]
load_hm_snippet(state_dir)      -> dict[str, Any]
load_text_artifact(state_dir, filename) -> str | None
load_ai_tell_report(state_dir)  -> dict[str, Any] | None
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def load_jd_parsed(state_dir: Path) -> dict[str, Any]:
    """Load .apply-state/jd-parsed.json; returns {} when absent."""
    path = state_dir / "jd-parsed.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_fit_score(state_dir: Path) -> dict[str, Any]:
    """Load .apply-state/fit-score.json; returns {} when absent."""
    path = state_dir / "fit-score.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_bullet_selection(state_dir: Path) -> dict[str, Any]:
    """Load .apply-state/bullet-selection.json; returns {} when absent."""
    path = state_dir / "bullet-selection.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_hm_snippet(state_dir: Path) -> dict[str, Any]:
    """Parse .apply-state/hm-snippet.md into a dict.

    The hm-snippet.md format::

        # HM dossier (or sentinel)

        detected: yes | no
        name: <string|null>
        source: linkedin_post | jd_signature | shakes_arg | none
        one_specific_signal: <string|null>
        suggested_hook: <string|null>
    """
    path = state_dir / "hm-snippet.md"
    if not path.exists():
        return {}
    out: dict[str, Any] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if value in ("null", "none", ""):
                out[key.strip()] = None
            elif value in ("yes", "no"):
                out[key.strip()] = value == "yes"
            else:
                out[key.strip()] = value
    return out


def load_text_artifact(state_dir: Path, filename: str) -> str | None:
    """Load a text/markdown artifact from state_dir, or None if missing."""
    path = state_dir / filename
    if not path.exists():
        return None
    return path.read_text()


def load_ai_tell_report(state_dir: Path) -> dict[str, Any] | None:
    """Load .apply-state/ai-tell-report.json; returns None if missing or malformed.

    Degrades gracefully — callers must not raise on None.
    """
    path = state_dir / "ai-tell-report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("ai-tell-report.json could not be loaded (%s): %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Mapping: specialist output filename -> (kind, loader)
# Used by db.ingest_phase_outputs to know which reader to call per artifact.
# ---------------------------------------------------------------------------

#: Maps artifact filename to (kind, loader_callable).
#: loader returns a dict/str/None; db.py serialises to JSON.
ARTIFACT_READERS: dict[str, tuple[str, Any]] = {
    "jd-parsed.json": ("jd-parsed", load_jd_parsed),
    "fit-score.json": ("fit-score", load_fit_score),
    "bullet-selection.json": ("bullet-selection", load_bullet_selection),
    "hm-snippet.md": ("hm-snippet", load_hm_snippet),
    "ai-tell-report.json": ("ai-tell-report", load_ai_tell_report),
    # text artifacts — loader returns str or None; we wrap in {"text": ...}
    "prose-draft.md": ("prose-draft", lambda d: load_text_artifact(d, "prose-draft.md")),
    "company-research.md": (
        "company-research",
        lambda d: load_text_artifact(d, "company-research.md"),
    ),
    "outreach-snippets.md": (
        "outreach-snippets",
        lambda d: load_text_artifact(d, "outreach-snippets.md"),
    ),
    "ats-check.json": (
        "ats-check",
        lambda d: json.loads((d / "ats-check.json").read_text())
        if (d / "ats-check.json").exists()
        else None,
    ),
}

#: Maps a specialist name (as written into manifest.json.invocations[].specialist)
#: to the artifact filename it produces under .apply-state/. Used by
#: db_ingest.ingest_phase_outputs to dispatch the right reader for each
#: invocation. Keep this in lockstep with apply._PHASE_REQUIRED_SPECIALISTS.
SPECIALIST_TO_ARTIFACT: dict[str, str] = {
    # gather
    "apply-jd-parser": "jd-parsed.json",
    "apply-fit-scorer": "fit-score.json",
    "apply-hm-enricher": "hm-snippet.md",
    "apply-bullet-selector": "bullet-selection.json",
    "apply-company-research": "company-research.md",
    # draft
    "apply-prose-writer": "prose-draft.md",
    "apply-prose-qa": "ai-tell-report.json",
    # render — optional specialists that DO write to .apply-state/
    "apply-portfolio-ats-checker": "ats-check.json",
    # apply-resume-renderer / apply-cover-letter-writer / apply-index-writer
    # write user-facing files under documents/ rather than .apply-state/, so
    # they have no entry here. The DB row records the invocation status only.
}

#: Maps each phase to the specialists whose .apply-state/ artifacts the
#: ingest hook should pull. Sourced from apply._PHASE_REQUIRED_SPECIALISTS
#: but inlined here to avoid a cyclic import.
PHASE_SPECIALISTS: dict[str, tuple[str, ...]] = {
    "gather": (
        "apply-jd-parser",
        "apply-fit-scorer",
        "apply-hm-enricher",
        "apply-bullet-selector",
        "apply-company-research",
    ),
    "draft": (
        "apply-prose-writer",
        "apply-prose-qa",
    ),
    "render": (
        "apply-portfolio-ats-checker",
    ),
}

__all__ = [
    "ARTIFACT_READERS",
    "PHASE_SPECIALISTS",
    "SPECIALIST_TO_ARTIFACT",
    "load_ai_tell_report",
    "load_bullet_selection",
    "load_fit_score",
    "load_hm_snippet",
    "load_jd_parsed",
    "load_text_artifact",
]
