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
from typing import Any

import yaml

from jobsmith._state_readers import load_jd_parsed

from .schemas.applications import Application, ApplicationDetail, ArtifactNode, ArtifactTree

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


# ---------------------------------------------------------------------------
# Detail helpers
# ---------------------------------------------------------------------------

_PROSE_SIZE_LIMIT = 256 * 1024   # 256 KB — if larger, truncate
_PROSE_READ_LIMIT = 64 * 1024    # 64 KB — bytes to read when truncating


def _node_for(file_path: Path, slug_dir: Path) -> ArtifactNode:
    """Return an ArtifactNode for a file, with path relative to slug_dir."""
    stat = file_path.stat()
    rel = str(file_path.relative_to(slug_dir))
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return ArtifactNode(name=file_path.name, path=rel, size=stat.st_size, mtime=mtime)


def _build_artifact_tree(slug_dir: Path, slug: str) -> ArtifactTree:
    """Walk slug_dir and collect ArtifactNodes for apply-state and rendered dirs."""
    state_dir = slug_dir / ".apply-state"
    apply_state_nodes: list[ArtifactNode] = []
    if state_dir.is_dir():
        for p in sorted(state_dir.iterdir()):
            if p.is_file():
                try:
                    apply_state_nodes.append(_node_for(p, slug_dir))
                except OSError:
                    pass

    rendered_dir = slug_dir / "rendered" / slug
    rendered_nodes: list[ArtifactNode] = []
    if rendered_dir.is_dir():
        for p in sorted(rendered_dir.iterdir()):
            if p.is_file():
                try:
                    rendered_nodes.append(_node_for(p, slug_dir))
                except OSError:
                    pass

    return ArtifactTree(apply_state=apply_state_nodes, rendered=rendered_nodes)


def _read_prose(path: Path) -> tuple[str | None, bool]:
    """Read prose markdown with size guard. Returns (content, truncated)."""
    if not path.exists():
        return None, False
    size = path.stat().st_size
    if size > _PROSE_SIZE_LIMIT:
        raw = path.read_bytes()[:_PROSE_READ_LIMIT]
        return raw.decode("utf-8", errors="replace"), True
    return path.read_text(encoding="utf-8", errors="replace"), False


def _load_json_file(path: Path) -> dict[str, Any] | None:
    """Load a JSON file into a dict; return None on missing or error."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        _log.warning("Could not parse JSON at %s", path)
        return None


def _load_yaml_file(path: Path) -> dict[str, Any] | None:
    """Load a YAML file into a dict; return None on missing or error."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except (OSError, yaml.YAMLError):
        _log.warning("Could not parse YAML at %s", path)
        return None


def _load_config_subset(slug_dir: Path) -> dict[str, Any] | None:
    """Load .apply-config.yaml and return only the output + render keys.

    Exposes just the output and render sections to avoid leaking sensitive
    keys (e.g. API keys, personal paths) from the full config.
    """
    config_path = slug_dir / ".apply-config.yaml"
    if not config_path.exists():
        # Walk up to find config — check parent dirs up to 3 levels
        for parent in list(slug_dir.parents)[:3]:
            candidate = parent / ".apply-config.yaml"
            if candidate.exists():
                config_path = candidate
                break
        else:
            return None

    data = _load_yaml_file(config_path)
    if data is None:
        return None

    # Return only safe public sections
    safe_keys = {"output", "render"}
    return {k: v for k, v in data.items() if k in safe_keys} or None


# ---------------------------------------------------------------------------
# Public detail API
# ---------------------------------------------------------------------------


def derive_application_detail(slug_dir: Path) -> ApplicationDetail:
    """Derive a rich ApplicationDetail record from a slug directory.

    Builds on derive_application_state for base fields and adds:
    - artifacts: ArtifactTree (apply-state files + rendered files)
    - spec: parsed jd-parsed.json
    - prose_draft: raw markdown (size-guarded to 64 KB)
    - cover_letter_draft: raw markdown (size-guarded to 64 KB)
    - fact_check: parsed fact_check.json
    - anchor_check: parsed anchor_check.json
    - bullet_selection: parsed bullet_selection.json
    - variables: parsed _variables.yml
    - config: safe subset of .apply-config.yaml (output + render keys)
    - truncated: True if any large field was truncated

    Parameters
    ----------
    slug_dir:
        Absolute path to <applications_dir>/<slug>/. Must be a directory.

    Returns
    -------
    ApplicationDetail
        Fully populated detail model.
    """
    slug = slug_dir.name
    state_dir = slug_dir / ".apply-state"

    # Base fields from existing logic
    base = derive_application_state(slug_dir)

    # Artifact tree
    artifacts = _build_artifact_tree(slug_dir, slug)

    # Spec (jd-parsed.json)
    spec = _load_json_file(state_dir / "jd-parsed.json") if state_dir.is_dir() else None

    # Prose drafts with size guard
    prose_path = state_dir / "prose-draft.md"
    cover_path = slug_dir / "cover-letter-draft.md"
    prose_draft, prose_truncated = _read_prose(prose_path)
    cover_letter_draft, cover_truncated = _read_prose(cover_path)
    truncated = prose_truncated or cover_truncated

    # JSON state files
    fact_check = _load_json_file(state_dir / "fact_check.json") if state_dir.is_dir() else None
    anchor_check = (
        _load_json_file(state_dir / "anchor_check.json") if state_dir.is_dir() else None
    )
    bullet_selection = (
        _load_json_file(state_dir / "bullet_selection.json") if state_dir.is_dir() else None
    )

    # YAML files
    variables = _load_yaml_file(slug_dir / "_variables.yml")
    config = _load_config_subset(slug_dir)

    return ApplicationDetail(
        # Base fields (spread from Application)
        slug=base.slug,
        role=base.role,
        company=base.company,
        status=base.status,
        updated_at=base.updated_at,
        phase=base.phase,
        anchors=base.anchors,
        factcheck=base.factcheck,
        renders=base.renders,
        url=base.url,
        # Detail-specific fields
        artifacts=artifacts,
        spec=spec,
        prose_draft=prose_draft,
        cover_letter_draft=cover_letter_draft,
        fact_check=fact_check,
        anchor_check=anchor_check,
        bullet_selection=bullet_selection,
        variables=variables,
        config=config,
        truncated=truncated,
    )


__all__ = ["derive_application_detail", "derive_application_state"]
