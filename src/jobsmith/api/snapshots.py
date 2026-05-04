"""POST /api/applications/{slug}/runs/{run_id}/snapshot

Materialises pipeline artifacts from the DB to canonical FS paths so that
``quarto render`` and ``git diff`` continue to work after Phase 3 drops the
FS-write side-effects from specialists.

Master YAMLs (assets/content/*.yml) are NEVER touched by this endpoint.

Writer functions
----------------
Each artifact kind has a *writer* that converts the DB output dict back to
the canonical on-disk format (mirroring the ARTIFACT_READERS readers):

    JSON kinds  → pretty-printed JSON to <filename>
    Text kinds  → ``output["text"]`` written to <filename>
    hm-snippet  → reconstructed key:value Markdown to hm-snippet.md

Atomic write
------------
Each file is written to a temporary path next to the target, then renamed
via ``os.replace`` (POSIX rename — atomic on the same filesystem).  A
failed write leaves no partial file at the target path.

Target selector
---------------
``target='apply-state'``  → only write artifacts that live in .apply-state/
``target='slug-root'``    → only write artifacts that live at the slug root
``target='both'``         → write everything (default)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db

from .schemas.snapshots import SnapshotFile, SnapshotRequest, SnapshotResult

router = APIRouter(tags=["snapshots"])

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind → canonical filename (inverse of ARTIFACT_READERS)
# ---------------------------------------------------------------------------

#: Artifacts that live under <slug>/.apply-state/
_APPLY_STATE_FILENAMES: dict[str, str] = {
    "jd-parsed": "jd-parsed.json",
    "fit-score": "fit-score.json",
    "bullet-selection": "bullet-selection.json",
    "hm-snippet": "hm-snippet.md",
    "ai-tell-report": "ai-tell-report.json",
    "prose-draft": "prose-draft.md",
    "company-research": "company-research.md",
    "outreach-snippets": "outreach-snippets.md",
    "ats-check": "ats-check.json",
    "anchor-check": "anchor-check.json",
    "fact-check": "fact-check.json",
}

#: Artifacts that live at the <slug>/ root (not inside .apply-state/)
_SLUG_ROOT_FILENAMES: dict[str, str] = {
    "cover-letter-draft": "cover-letter-draft.md",
    "quarto-config": "_quarto.yml",
    "variables": "_variables.yml",
    "manifest": "manifest.json",
}


def _target_for_kind(kind: str) -> Literal["apply-state", "slug-root"] | None:
    """Return which FS tree the kind belongs to, or None if not writable."""
    if kind in _APPLY_STATE_FILENAMES:
        return "apply-state"
    if kind in _SLUG_ROOT_FILENAMES:
        return "slug-root"
    return None


# ---------------------------------------------------------------------------
# Serialisers: DB output dict → file bytes
# ---------------------------------------------------------------------------


def _serialise_json(output: dict[str, Any]) -> bytes:
    """Serialise a dict artifact to pretty JSON bytes."""
    return (json.dumps(output, indent=2, ensure_ascii=False) + "\n").encode()


def _serialise_hm_snippet(output: dict[str, Any]) -> bytes:
    """Reconstruct the hm-snippet.md format from an output dict."""
    lines = ["# HM dossier", ""]
    field_order = ["detected", "name", "source", "one_specific_signal", "suggested_hook"]
    seen = set()
    for key in field_order:
        if key in output:
            seen.add(key)
            value = output[key]
            if value is None:
                lines.append(f"{key}: null")
            elif isinstance(value, bool):
                lines.append(f"{key}: {'yes' if value else 'no'}")
            else:
                lines.append(f"{key}: {value}")
    # Emit any extra fields not in canonical order
    for key, value in output.items():
        if key not in seen:
            if value is None:
                lines.append(f"{key}: null")
            elif isinstance(value, bool):
                lines.append(f"{key}: {'yes' if value else 'no'}")
            else:
                lines.append(f"{key}: {value}")
    lines.append("")
    return "\n".join(lines).encode()


def _serialise_text(output: dict[str, Any]) -> bytes:
    """Serialise a TextArtifact (has ``text`` field) to bytes."""
    text = output.get("text") or ""
    return text.encode() if text else b""


def _serialise_variables_yml(output: dict[str, Any]) -> bytes:
    """Serialise the variables dict to YAML bytes."""
    try:
        import yaml  # type: ignore[import]

        return yaml.safe_dump(output, allow_unicode=True, sort_keys=False).encode()
    except Exception:
        return (json.dumps(output, indent=2, ensure_ascii=False) + "\n").encode()


def _serialise_quarto_config(output: dict[str, Any]) -> bytes:
    """Re-emit _quarto.yml from a ``{"content": <yaml-text>}`` envelope.

    Falls back to ``text`` for backwards-compatibility with payloads written
    before the quarto-config envelope was tightened (feat-60be8c3a fix).
    """
    content = output.get("content") or output.get("text") or ""
    return content.encode() if isinstance(content, str) else b""


def _serialise_artifact(kind: str, output: dict[str, Any]) -> bytes:
    """Dispatch to the right serialiser for *kind*."""
    if kind == "hm-snippet":
        return _serialise_hm_snippet(output)
    # Text/Markdown kinds
    if kind in ("prose-draft", "company-research", "outreach-snippets", "cover-letter-draft"):
        return _serialise_text(output)
    if kind == "quarto-config":
        return _serialise_quarto_config(output)
    # YAML kinds
    if kind == "variables":
        return _serialise_variables_yml(output)
    # All remaining kinds → JSON
    return _serialise_json(output)


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(dest: Path, data: bytes) -> int:
    """Write *data* atomically to *dest* using a temp-file + os.replace.

    Returns the number of bytes written.
    Raises OSError on failure; no partial file is left at *dest*.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=".snap-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, str(dest))
    except Exception:
        # fdopen takes ownership of *fd* and closes it on context exit (or on
        # exception inside the with-block), so no manual close here. We only
        # need to clean up the temp file if rename failed after close.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return len(data)


# ---------------------------------------------------------------------------
# Internal helpers (module-level → testable via monkeypatch)
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    """Resolve the pipeline DB path from the nearest .apply-config.yaml."""
    config_path = find_config(Path.cwd())
    if config_path is None:
        raise HTTPException(status_code=404, detail="No .apply-config.yaml found")
    config = load_config(path=config_path)
    repo_root = config_path.parent
    return (repo_root / config.output.jobsmith_db).resolve()


def _get_apps_dir() -> Path:
    """Resolve the applications directory from the nearest .apply-config.yaml."""
    config_path = find_config(Path.cwd())
    if config_path is None:
        raise HTTPException(status_code=404, detail="No .apply-config.yaml found")
    config = load_config(path=config_path)
    repo_root = config_path.parent
    return (repo_root / config.output.applications_dir).resolve()


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "/applications/{slug}/runs/{run_id}/snapshot",
    response_model=SnapshotResult,
    status_code=200,
)
def create_snapshot(
    slug: str,
    run_id: str,
    body: SnapshotRequest | None = None,
) -> SnapshotResult:
    """Materialise DB artifacts for *run_id* to canonical FS paths.

    Parameters
    ----------
    slug:
        Application slug (e.g. ``"acme-swe"``).
    run_id:
        Pipeline run identifier whose artifacts are written.
    body:
        Optional filter body.  Omit to snapshot all artifacts in the run.

    Returns
    -------
    SnapshotResult
        List of files written with absolute paths and byte counts.
    """
    req = body or SnapshotRequest()
    db_path = _get_db_path()
    apps_dir = _get_apps_dir()

    try:
        conn = open_pipeline_db(db_path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

    try:
        # Verify run exists AND belongs to this slug — without the slug
        # check a snapshot of /foo/runs/<bar's run> would dump bar's
        # artifacts into foo's directory.
        run_row = conn.execute(
            "SELECT run_id FROM apply_runs WHERE run_id = ? AND slug = ?",
            (run_id, slug),
        ).fetchone()
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

        # Slug path-traversal guard (mirrors events.py:_validate_slug_or_404).
        if not slug or "/" in slug or ".." in slug or slug.startswith("."):
            raise HTTPException(
                status_code=400, detail=f"Invalid slug: {slug!r}"
            )

        # Fetch artifact rows
        rows = conn.execute(
            "SELECT kind, output_json FROM specialist_outputs WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    slug_dir = apps_dir / slug
    apply_state_dir = slug_dir / ".apply-state"

    written: list[SnapshotFile] = []

    for row in rows:
        kind: str = row["kind"]
        output_json: str = row["output_json"]

        # Apply kinds filter
        if req.kinds is not None and kind not in req.kinds:
            continue

        # Determine target tree
        kind_target = _target_for_kind(kind)
        if kind_target is None:
            _log.debug("Skipping kind %r — no canonical FS path defined", kind)
            continue

        # Apply target selector filter
        if req.target == "apply-state" and kind_target != "apply-state":
            continue
        if req.target == "slug-root" and kind_target != "slug-root":
            continue

        # Resolve canonical path
        if kind_target == "apply-state":
            filename = _APPLY_STATE_FILENAMES[kind]
            dest = apply_state_dir / filename
        else:
            filename = _SLUG_ROOT_FILENAMES[kind]
            dest = slug_dir / filename

        # Deserialise + serialise
        try:
            output = json.loads(output_json)
            if not isinstance(output, dict):
                output = {"value": output}
        except (json.JSONDecodeError, TypeError):
            output = {}

        try:
            data = _serialise_artifact(kind, output)
        except Exception as exc:
            _log.warning("Failed to serialise kind %r: %s", kind, exc)
            continue

        # Atomic write
        try:
            nbytes = _atomic_write(dest, data)
        except OSError as exc:
            _log.warning("Failed to write %s: %s", dest, exc)
            raise HTTPException(
                status_code=500, detail=f"Failed to write {dest}: {exc}"
            ) from exc

        written.append(
            SnapshotFile(
                path=str(dest),
                kind=kind,
                bytes_written=nbytes,
            )
        )

    return SnapshotResult.from_files(slug=slug, run_id=run_id, files=written)


__all__ = ["router"]
