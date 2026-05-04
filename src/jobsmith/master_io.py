"""Comment-safe YAML round-trip helpers for master content sections.

Public API
----------
load_master(section, path)       → ruamel.yaml CommentedMap/CommentedSeq
save_master(section, payload, path) → None (atomic write, preserves comments)
save_benchmark(text, path)       → None (atomic write of benchmark.md)

Design notes
------------
- ruamel.yaml is used throughout so that YAML comments and key order survive
  every read-modify-write cycle.
- save_master performs schema-aware merge: it validates the incoming payload
  against the Pydantic model for the given section, then walks the existing
  CommentedMap/CommentedSeq and updates only the scalar values that changed,
  leaving untouched keys and their adjacent comments in place.
- All writes are atomic (tmp file + os.replace) so a crash mid-write never
  leaves a half-written file.
"""

from __future__ import annotations

import io
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

# ---------------------------------------------------------------------------
# Section enum
# ---------------------------------------------------------------------------


class MasterSection(str, Enum):
    """The four content sections in master YAML."""

    WORK = "work"
    SKILL = "skill"
    EDUCATION = "education"
    AUTHOR = "author"


# ---------------------------------------------------------------------------
# Pydantic models (inline — mirrors schemas/master.py in the api package but
# without the HTTP layer so master_io has no FastAPI dependency).
# ---------------------------------------------------------------------------


class _WorkDetailDict(BaseModel):
    bullet: str
    anchor: bool | None = None
    anchor_reason: str | None = None
    tags: list[str] = []
    drop_when: str | None = None

    model_config = {"extra": "allow"}


class _WorkEntry(BaseModel):
    title: str
    location: str = ""
    date: str = ""
    description: str = ""
    details: list[str | dict[str, Any]] = []

    model_config = {"extra": "allow"}


class _SkillEntry(BaseModel):
    title: str
    description: str = ""
    details: list[str] = []

    model_config = {"extra": "allow"}


class _EducationEntry(BaseModel):
    title: str
    location: str = ""
    date: str = ""
    description: str = ""
    details: list[str] = []

    model_config = {"extra": "allow"}


class _Author(BaseModel):
    name: Any = None
    firstname: str | None = None
    lastname: str | None = None
    address: str = ""
    email: str = ""
    phone: str = ""
    homepage: str = ""
    photo: str = ""
    position: str = ""
    profession: str = ""
    quote: str = ""
    contacts: list[dict[str, Any]] = []

    model_config = {"extra": "allow"}


# Map section → Pydantic model for list-item validation
_LIST_MODELS: dict[MasterSection, type[BaseModel]] = {
    MasterSection.WORK: _WorkEntry,
    MasterSection.SKILL: _SkillEntry,
    MasterSection.EDUCATION: _EducationEntry,
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_yaml() -> YAML:
    """Return a ruamel YAML instance configured for round-trip preservation."""
    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    y.width = 4096  # prevent unwanted line-wrapping
    return y


def _validate_list_payload(section: MasterSection, payload: list[Any]) -> None:
    """Validate each item in *payload* against the section's Pydantic model.

    Raises ValidationError (pydantic) on the first invalid item.
    """
    model = _LIST_MODELS[section]
    for item in payload:
        model.model_validate(item)


def _validate_author_payload(payload: Any) -> None:
    """Validate an author payload (dict or list-wrapped dict).

    Raises ValidationError on schema mismatch.
    """
    if isinstance(payload, dict):
        inner = payload.get("author", payload)
        if isinstance(inner, list):
            if inner:
                _Author.model_validate(inner[0])
        elif isinstance(inner, dict):
            _Author.model_validate(inner)
    elif isinstance(payload, list):
        if payload:
            _Author.model_validate(payload[0])


def _merge_commented_map(existing: CommentedMap, new_data: dict[str, Any]) -> CommentedMap:
    """Update scalar values in *existing* CommentedMap from *new_data*.

    - Keys present in *existing* but absent from *new_data* are kept as-is
      (comments preserved).
    - Keys present in *new_data* but absent from *existing* are appended.
    - Scalar and list leaves are replaced; nested dicts recurse.
    """
    for key, new_val in new_data.items():
        if key in existing:
            old_val = existing[key]
            if isinstance(old_val, CommentedMap) and isinstance(new_val, dict):
                _merge_commented_map(old_val, new_val)
            elif isinstance(old_val, CommentedSeq) and isinstance(new_val, list):
                _merge_commented_seq(old_val, new_val)
            else:
                existing[key] = new_val
        else:
            existing[key] = new_val
    return existing


def _merge_commented_seq(existing: CommentedSeq, new_items: list[Any]) -> CommentedSeq:
    """Update items in *existing* CommentedSeq from *new_items*.

    If lengths match, items are updated in place (comments on each item
    preserved).  If lengths differ, the sequence is replaced wholesale
    (comments are lost for added/removed items, but that is unavoidable).
    """
    if len(existing) == len(new_items):
        for i, new_item in enumerate(new_items):
            old_item = existing[i]
            if isinstance(old_item, CommentedMap) and isinstance(new_item, dict):
                _merge_commented_map(old_item, new_item)
            elif isinstance(old_item, CommentedSeq) and isinstance(new_item, list):
                _merge_commented_seq(old_item, new_item)
            else:
                existing[i] = new_item
    else:
        # Lengths differ — rebuild the sequence, attempt per-item merge where
        # an existing CommentedMap at the same index is available.
        del existing[:]
        for new_item in new_items:
            existing.append(new_item)
    return existing


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via tmp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_master(section: MasterSection, path: Path) -> Any:  # noqa: ANN401
    """Parse *path* with ruamel.yaml in round-trip mode, preserving comments.

    Parameters
    ----------
    section:
        Which master section the file holds (used for type-hinting intent;
        not enforced during load).
    path:
        Absolute path to the YAML file.

    Returns
    -------
    ruamel.yaml CommentedMap or CommentedSeq (depending on file content).

    Raises
    ------
    FileNotFoundError
        When *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Master YAML not found: {path}")
    y = _make_yaml()
    return y.load(path.read_text(encoding="utf-8"))


def save_master(section: MasterSection, payload: Any, path: Path) -> None:
    """Validate *payload* and atomically write it to *path*, preserving comments.

    Schema-aware merge strategy
    ---------------------------
    1. Validate *payload* against the section's Pydantic model.  Raises
       ``pydantic.ValidationError`` on failure — file is NOT written.
    2. If *path* exists, load the current CommentedMap/CommentedSeq.
    3. Walk the existing structure and update only changed leaves, so that
       comments adjacent to unchanged keys survive.
    4. Serialise to a tmp file, then atomically rename to *path*.

    Parameters
    ----------
    section:
        Which master section this payload represents.
    payload:
        JSON-serialisable data (list-of-dicts for work/skill/education,
        dict for author).
    path:
        Target YAML file path (created if absent; parent dirs created).

    Raises
    ------
    pydantic.ValidationError
        When *payload* fails schema validation (file not touched).
    OSError
        On atomic-write failure (tmp file cleaned up).
    """
    # Step 1: validate before touching the file
    if section in _LIST_MODELS:
        if not isinstance(payload, list):
            # Trigger a ValidationError by attempting to validate as the model
            model = _LIST_MODELS[section]
            model.model_validate(payload)  # raises ValidationError
        _validate_list_payload(section, payload)
    else:
        _validate_author_payload(payload)

    # Step 2: load existing structure (if present) for comment-preserving merge
    y = _make_yaml()
    if path.exists():
        existing = y.load(path.read_text(encoding="utf-8"))
        if existing is None:
            existing = None
    else:
        existing = None

    # Step 3: merge into the existing CommentedMap/CommentedSeq
    if existing is not None:
        if isinstance(existing, CommentedSeq) and isinstance(payload, list):
            merged = _merge_commented_seq(existing, payload)
        elif isinstance(existing, CommentedMap) and isinstance(payload, dict):
            merged = _merge_commented_map(existing, payload)
        else:
            merged = payload  # type mismatch — replace wholesale
    else:
        merged = payload

    # Step 4: serialise to string
    buf = io.StringIO()
    y.dump(merged, buf)
    content = buf.getvalue()

    # Step 5: atomic write
    _atomic_write(path, content)


def save_benchmark(text: str, path: Path) -> None:
    """Atomically write *text* to *path* (benchmark.md or similar).

    Parent directories are created if they do not exist.

    Parameters
    ----------
    text:
        Content to write verbatim.
    path:
        Destination file path.

    Raises
    ------
    OSError
        On write failure (tmp file cleaned up).
    """
    _atomic_write(path, text)


__all__ = [
    "MasterSection",
    "load_master",
    "save_benchmark",
    "save_master",
]
