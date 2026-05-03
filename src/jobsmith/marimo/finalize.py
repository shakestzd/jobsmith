"""Finalize accepted amendments: atomic YAML write-back + quarto render.

Entry point
-----------
``finalize_run`` — pure function, no marimo imports.
The caller (notebook) passes ``accepted_amendments`` — a list of
:class:`~jobsmith.marimo.directive_parser.Amendment` objects with
``status='accepted'``.  The function:

1. Filters already-finalized amendments (idempotent re-click is a no-op).
2. Rejects read-only sections (``fit-score``).
3. Creates a backup tarball **before** any write (aborts if this fails).
4. Applies amendments to canonical files via ruamel round-trip (YAML) or
   plain-text write (cover-letter).
5. Atomic write: tmp file (same dir) → fsync → os.replace.
6. Runs ``quarto render documents/resume.qmd`` inside the slug app dir.
7. Marks applied amendments ``'finalized'`` in the review DB.

Section → file mapping
----------------------
- work       → masters.work_yml        (ruamel round-trip)
- education  → masters.education_yml   (ruamel round-trip)
- skills     → masters.skill_yml       (ruamel round-trip)
- cover-letter → <applications_dir>/<slug>/cover-letter-final.md (plain text)
- fit-score  → READ-ONLY → unsupported_sections
"""
from __future__ import annotations

import os
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

from jobsmith.db import open_review_db

if TYPE_CHECKING:
    from jobsmith.config import MasterPaths
    from jobsmith.marimo.directive_parser import Amendment

VALID_WRITEBACK_SECTIONS: frozenset[str] = frozenset(
    {"work", "education", "skills", "cover-letter"}
)
READONLY_SECTIONS: frozenset[str] = frozenset({"fit-score"})

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FinalizeResult:
    """Return value from :func:`finalize_run`.

    Attributes
    ----------
    backup_path:
        Path to the backup tarball created before any write.
    modified_files:
        Canonical files actually rewritten.
    pdf_path:
        Absolute path to the rendered PDF (None if quarto was not invoked).
    quarto_returncode:
        Return code from ``quarto render``; 0 = success; -1 when skipped.
    finalized_amendment_ids:
        amendment_ids whose status was set to ``'finalized'``.
    unsupported_sections:
        Section names rejected (e.g. ``fit-score``).
    """

    backup_path: Path
    modified_files: list[Path] = field(default_factory=list)
    pdf_path: Path | None = None
    quarto_returncode: int = -1
    finalized_amendment_ids: list[str] = field(default_factory=list)
    unsupported_sections: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def _now_ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _create_backup_tarball(
    master_paths: list[Path],
    cover_letter_path: Path,
    dest_dir: Path,
    slug: str,
) -> Path:
    """Create a .tar.gz of all canonical YAMLs + cover letter.

    Raises on I/O failure — caller must abort finalize.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = dest_dir / f"{slug}-{_now_ts()}.tar.gz"
    with tarfile.open(tarball_path, "w:gz") as tar:
        for f in master_paths:
            if f.exists():
                tar.add(f, arcname=f.name)
        if cover_letter_path.exists():
            tar.add(cover_letter_path, arcname=cover_letter_path.name)
    return tarball_path


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (tmp→fsync→os.replace).

    The tmp file is placed in the SAME directory as *path* to guarantee
    the same filesystem and a true atomic rename.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = open(tmp, "w", encoding="utf-8")  # noqa: SIM115
    try:
        fd.write(content)
        fd.flush()
        os.fsync(fd.fileno())
    finally:
        fd.close()
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# ruamel.yaml round-trip helpers
# ---------------------------------------------------------------------------


def _yaml_rt() -> YAML:
    y = YAML(typ="rt")
    y.default_flow_style = False
    y.preserve_quotes = True
    return y


def _load_yaml_rt(path: Path) -> object:
    y = _yaml_rt()
    with path.open("r", encoding="utf-8") as fh:
        return y.load(fh)


def _dump_yaml_rt(data: object) -> str:
    y = _yaml_rt()
    buf = StringIO()
    y.dump(data, buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Field parsing helper
# ---------------------------------------------------------------------------


def _parse_field(field_str: str) -> tuple[str, int | None]:
    """Parse ``'bullet[2]'`` → ``('bullet', 2)``; ``'title'`` → ``('title', None)``."""
    import re

    m = re.match(r"^([a-z][a-z_\-]*)(?:\[(\d+)\])?$", field_str)
    if not m:
        return field_str, None
    name = m.group(1)
    idx_str = m.group(2)
    return name, (int(idx_str) if idx_str is not None else None)


# ---------------------------------------------------------------------------
# Per-section YAML appliers
# ---------------------------------------------------------------------------


# The AMEND grammar uses `bullet[N]` as the canonical pointer into a
# work / education entry's bullet list, but the actual master YAML schema
# uses `details` for both sections (see examples/master-yaml/work.yml and
# the slice-A object-form bullet schema in feat-46f09a56). When the user
# writes `AMEND work[0].bullet[2]` the applier must translate the
# `bullet` reference to `details`. Roborev #921 MEDIUM.
_SECTION_LIST_KEY: dict[str, str] = {
    "work": "details",
    "education": "details",
}

_BULLET_REFS: frozenset[str] = frozenset({"bullet", "bullets"})


def _apply_yaml_amendment(data: object, amendment: Amendment) -> bool:
    """Dispatch to the correct applier based on ``amendment.section``."""
    section = amendment.section
    if section in _SECTION_LIST_KEY:
        return _apply_list_section(data, amendment)
    if section == "skills":
        return _apply_skills(data, amendment)
    return False


def _set_list_entry(sub: list, field_idx: int, value: str) -> bool:
    """Replace ``sub[field_idx]`` with ``value``.

    Master `details` entries can be plain strings or dict-form
    ``{bullet, anchor, anchor_reason}`` (slice-A object-form schema).
    For dict-form entries we update the ``bullet`` key in place so
    anchor metadata survives the edit; for plain strings we replace.
    """
    if field_idx >= len(sub):
        return False
    item = sub[field_idx]
    if isinstance(item, dict):
        item["bullet"] = value
    else:
        sub[field_idx] = value
    return True


def _apply_list_section(data: object, amendment: Amendment) -> bool:
    """Apply to a top-level YAML list (work, education).

    Expects:
    - data: list of entry dicts
    - amendment.index: selects the entry
    - amendment.field: field name, optionally with index (``bullet[2]``)

    Translates the grammar's ``bullet`` reference to the section's actual
    YAML list key (``details`` for work / education) via _SECTION_LIST_KEY.
    """
    if not isinstance(data, list):
        return False
    idx = amendment.index
    if idx is None or idx >= len(data):
        return False
    entry = data[idx]
    if amendment.field is None:
        return False

    field_name, field_idx = _parse_field(amendment.field)
    section_list_key = _SECTION_LIST_KEY.get(amendment.section)

    if field_idx is not None or amendment.op == "append":
        # Resolve sub-list: bullet → details for work/education, else
        # try the field name verbatim then its plural form.
        if field_name in _BULLET_REFS and section_list_key is not None:
            sub = entry.get(section_list_key)
        else:
            sub = entry.get(field_name) or entry.get(field_name + "s")
        if not isinstance(sub, list):
            return False
        if amendment.op == "append":
            sub.append(amendment.value)
            return True
        if field_idx is not None:
            return _set_list_entry(sub, field_idx, amendment.value)
        return False

    # Scalar field replace
    if amendment.op == "replace":
        entry[field_name] = amendment.value
        return True
    return False


def _apply_skills(data: object, amendment: Amendment) -> bool:
    """Apply to the skills YAML (dict of category → list).

    amendment.field is the category key (e.g. ``technical``);
    amendment.index selects the item within the category.
    """
    if not isinstance(data, dict):
        return False
    if amendment.field is None:
        return False

    field_name, field_idx = _parse_field(amendment.field)
    category = data.get(field_name)
    if category is None:
        return False

    if amendment.op == "append":
        if isinstance(category, list):
            category.append(amendment.value)
            return True
        return False

    # Replace: use amendment.index or field_idx
    idx = amendment.index if field_idx is None else field_idx
    if idx is None or not isinstance(category, list) or idx >= len(category):
        return False
    category[idx] = amendment.value
    return True


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_finalized_ids(slug: str, review_db_dir: Path) -> set[str]:
    """Return set of amendment_ids already marked 'finalized'."""
    conn = open_review_db(slug, review_db_dir)
    try:
        rows = conn.execute(
            "SELECT amendment_id FROM amendments WHERE slug=? AND status='finalized'",
            (slug,),
        ).fetchall()
        return {row["amendment_id"] for row in rows}
    finally:
        conn.close()


def _mark_finalized(
    slug: str, amendment_ids: list[str], review_db_dir: Path
) -> None:
    """Set status='finalized' for all given amendment_ids."""
    if not amendment_ids:
        return
    conn = open_review_db(slug, review_db_dir)
    try:
        placeholders = ",".join("?" * len(amendment_ids))
        conn.execute(
            f"UPDATE amendments SET status='finalized' "  # noqa: S608
            f"WHERE amendment_id IN ({placeholders}) AND slug=?",
            (*amendment_ids, slug),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Quarto helper
# ---------------------------------------------------------------------------


def _run_quarto(app_dir: Path) -> int:
    """Run ``quarto render documents/resume.qmd`` in *app_dir*.

    Returns the process exit code.
    """
    result = subprocess.run(
        ["quarto", "render", "documents/resume.qmd"],
        cwd=str(app_dir),
        capture_output=True,
        text=True,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def finalize_run(
    *,
    slug: str,
    accepted_amendments: list[Amendment],
    masters: MasterPaths,
    applications_dir: Path,
    review_db_dir: Path,
    backup_dir: Path | None = None,
    repo_root: Path | None = None,
) -> FinalizeResult:
    """Apply all accepted amendments and render the PDF.

    Parameters
    ----------
    slug:
        Application slug.
    accepted_amendments:
        List of :class:`~jobsmith.marimo.directive_parser.Amendment` objects
        with ``status='accepted'``.  Only these are considered; pending and
        rejected amendments are ignored.
    masters:
        :class:`~jobsmith.config.MasterPaths` (paths to canonical YAMLs).
    applications_dir:
        Parent directory for per-slug application folders.
    review_db_dir:
        Parent directory for per-slug review DBs.
    backup_dir:
        Directory for backup tarballs. Defaults to
        ``<applications_dir>/../.review-backups``.
    repo_root:
        Used to resolve relative master paths. Defaults to cwd.
    """
    applications_dir = Path(applications_dir)
    review_db_dir = Path(review_db_dir)
    app_dir = applications_dir / slug

    _root = Path(repo_root) if repo_root else Path.cwd()
    if backup_dir is None:
        backup_dir = applications_dir.parent / ".review-backups"

    def _resolve(p: Path) -> Path:
        return p if p.is_absolute() else (_root / p).resolve()

    work_yml = _resolve(masters.work_yml)
    education_yml = _resolve(masters.education_yml)
    skill_yml = _resolve(masters.skill_yml)
    cover_letter_path = app_dir / "cover-letter-final.md"

    yaml_path_map = {
        "work": work_yml,
        "education": education_yml,
        "skills": skill_yml,
    }

    # Separate amendments by section; reject read-only
    unsupported: list[str] = []
    yaml_amendments: dict[str, list[Amendment]] = {
        "work": [],
        "education": [],
        "skills": [],
    }
    cover_amendments: list[Amendment] = []

    # Filter: only accepted, not already finalized
    already_finalized = _load_finalized_ids(slug, review_db_dir)
    new_amendments = [
        a for a in accepted_amendments
        if a.id not in already_finalized and a.status == "accepted"
    ]

    for a in new_amendments:
        if a.section in READONLY_SECTIONS:
            if a.section not in unsupported:
                unsupported.append(a.section)
        elif a.section == "cover-letter":
            cover_amendments.append(a)
        elif a.section in yaml_amendments:
            yaml_amendments[a.section].append(a)

    # --- Create backup BEFORE any write ---
    master_paths = [work_yml, education_yml, skill_yml]
    backup_path = _create_backup_tarball(
        master_paths, cover_letter_path, Path(backup_dir), slug
    )

    result = FinalizeResult(
        backup_path=backup_path,
        unsupported_sections=unsupported,
    )

    # Check if there is anything to do
    has_yaml_work = any(v for v in yaml_amendments.values())
    if not has_yaml_work and not cover_amendments:
        return result

    # --- Apply YAML amendments ---
    yaml_data: dict[str, object] = {}
    applied_ids: list[str] = []

    for section, amendments in yaml_amendments.items():
        if not amendments:
            continue
        p = yaml_path_map[section]
        if not p.exists():
            continue
        if section not in yaml_data:
            yaml_data[section] = _load_yaml_rt(p)
        for a in amendments:
            ok = _apply_yaml_amendment(yaml_data[section], a)
            if ok:
                applied_ids.append(a.id)

    # --- Write modified YAML files atomically ---
    for section, data in yaml_data.items():
        p = yaml_path_map[section]
        content = _dump_yaml_rt(data)
        _atomic_write(p, content)
        result.modified_files.append(p)

    # --- Apply cover-letter amendments ---
    cover_modified = False
    if cover_amendments and cover_letter_path.exists():
        text = cover_letter_path.read_text(encoding="utf-8")
        for a in cover_amendments:
            if a.op == "replace":
                text = a.value
                applied_ids.append(a.id)
                cover_modified = True
            elif a.op == "append":
                text = text.rstrip("\n") + "\n\n" + a.value
                applied_ids.append(a.id)
                cover_modified = True

    if cover_modified:
        _atomic_write(cover_letter_path, text)
        result.modified_files.append(cover_letter_path)

    if not result.modified_files:
        # Amendments were present but no files changed (e.g. bad indices)
        return result

    # --- Run quarto render ---
    pdf_path = app_dir / "documents" / "resume.pdf"
    rc = _run_quarto(app_dir)
    result.quarto_returncode = rc
    result.pdf_path = pdf_path.resolve() if pdf_path.exists() else pdf_path.resolve()

    # --- Mark amendments finalized ---
    _mark_finalized(slug, applied_ids, review_db_dir)
    result.finalized_amendment_ids = applied_ids

    return result


__all__ = [
    "READONLY_SECTIONS",
    "VALID_WRITEBACK_SECTIONS",
    "FinalizeResult",
    "finalize_run",
]
