"""/api/master router for the jobsmith HTTP API.

Endpoints
---------
GET  /master                       → MasterPayload  (all four sections)
GET  /master/{section}             → section content (work | skill | education | author)
PUT  /master/{section}             → replace section content (validated against schema)
POST /master/{section}/upload      → upload a raw YAML file replacing the section

Behavior contract
-----------------
- Reads return 200 + content; 404 when .apply-config.yaml is missing up the cwd tree.
- Writes use atomic tmp-file + rename. Comments are NOT preserved on round-trip in
  this MVP (yaml.safe_dump). The 0.8 DB-as-source-of-truth track will replace this
  surface entirely; the comment-preservation work belongs there with ruamel.yaml.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Body, HTTPException, UploadFile
from pydantic import BaseModel, ValidationError

from jobsmith.config import find_config, load_config
from jobsmith.paths import resolve

from .schemas.master import Author, EducationEntry, MasterPayload, SkillEntry, WorkEntry

Section = Literal["work", "skill", "education", "author"]
_SECTIONS: tuple[Section, ...] = ("work", "skill", "education", "author")

router = APIRouter(tags=["master"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_config_path() -> Path:
    """Return the .apply-config.yaml path or raise 404.

    Raises
    ------
    HTTPException(404)
        When find_config() returns None (no config found up the cwd tree).
    """
    config_path = find_config(Path.cwd())
    if config_path is None:
        raise HTTPException(status_code=404, detail="No .apply-config.yaml found")
    return config_path


def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
    """Load a YAML file that contains a list. Return [] on missing or error."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _load_work(config_path: Path) -> list[WorkEntry]:
    config = load_config(path=config_path)
    repo_root = config_path.parent
    path = resolve(config.master.work_yml, repo_root)
    return [WorkEntry.model_validate(item) for item in _load_yaml_list(path)]


def _load_skill(config_path: Path) -> list[SkillEntry]:
    config = load_config(path=config_path)
    repo_root = config_path.parent
    path = resolve(config.master.skill_yml, repo_root)
    raw = _load_yaml_list(path)
    if raw:
        return [SkillEntry.model_validate(item) for item in raw]
    # Fallback: dict-of-lists form (e.g. {technical: [...], languages: [...]})
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        if isinstance(data, dict):
            entries = []
            for key, val in data.items():
                if isinstance(val, list):
                    entries.append(
                        SkillEntry(
                            title=key,
                            description=", ".join(str(v) for v in val),
                            details=[str(v) for v in val],
                        )
                    )
            return entries
    return []


def _load_education(config_path: Path) -> list[EducationEntry]:
    config = load_config(path=config_path)
    repo_root = config_path.parent
    path = resolve(config.master.education_yml, repo_root)
    return [EducationEntry.model_validate(item) for item in _load_yaml_list(path)]


def _load_author(config_path: Path) -> Author | None:
    config = load_config(path=config_path)
    repo_root = config_path.parent
    path = resolve(config.master.author_yml, repo_root)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    author_val = data.get("author")
    if isinstance(author_val, list) and author_val:
        author_dict = author_val[0]
    elif isinstance(author_val, dict):
        author_dict = author_val
    else:
        return None
    if not isinstance(author_dict, dict):
        return None
    return Author.model_validate(author_dict)


# ---------------------------------------------------------------------------
# Routes (read-only)
# ---------------------------------------------------------------------------


@router.get("/master", response_model=MasterPayload)
def get_master() -> MasterPayload:
    """Return all master content sections in one payload."""
    config_path = _require_config_path()
    return MasterPayload(
        work=_load_work(config_path),
        skill=_load_skill(config_path),
        education=_load_education(config_path),
        author=_load_author(config_path),
    )


@router.get("/master/work", response_model=list[WorkEntry])
def get_master_work() -> list[WorkEntry]:
    """Return the work history list from work.yml."""
    config_path = _require_config_path()
    return _load_work(config_path)


@router.get("/master/skill", response_model=list[SkillEntry])
def get_master_skill() -> list[SkillEntry]:
    """Return the skill categories list from skill.yml."""
    config_path = _require_config_path()
    return _load_skill(config_path)


@router.get("/master/education", response_model=list[EducationEntry])
def get_master_education() -> list[EducationEntry]:
    """Return the education list from education.yml."""
    config_path = _require_config_path()
    return _load_education(config_path)


@router.get("/master/author", response_model=Author | None)
def get_master_author() -> Author | None:
    """Return the author block from author.yml, or null if the file is missing."""
    config_path = _require_config_path()
    return _load_author(config_path)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def _resolve_section_path(config_path: Path, section: Section) -> Path:
    config = load_config(path=config_path)
    repo_root = config_path.parent
    if section == "work":
        return resolve(config.master.work_yml, repo_root)
    if section == "skill":
        return resolve(config.master.skill_yml, repo_root)
    if section == "education":
        return resolve(config.master.education_yml, repo_root)
    return resolve(config.master.author_yml, repo_root)


def _atomic_write_yaml(path: Path, payload: Any) -> None:
    """Write *payload* as YAML to *path* atomically (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_section_payload(section: Section, raw: Any) -> Any:
    """Validate *raw* (already-parsed YAML/JSON) against the section's schema.

    Returns the JSON-serializable payload to write back to disk on success.
    Raises HTTPException(400) on schema or shape errors.
    """
    try:
        if section == "work":
            if not isinstance(raw, list):
                raise HTTPException(400, "work payload must be a list")
            return [WorkEntry.model_validate(item).model_dump(exclude_none=False) for item in raw]
        if section == "skill":
            if not isinstance(raw, list):
                raise HTTPException(400, "skill payload must be a list")
            return [SkillEntry.model_validate(item).model_dump(exclude_none=False) for item in raw]
        if section == "education":
            if not isinstance(raw, list):
                raise HTTPException(400, "education payload must be a list")
            return [EducationEntry.model_validate(item).model_dump(exclude_none=False) for item in raw]
        # author — accept either {author: [...]} (canonical), {author: {...}} (single),
        # or a bare author dict (frontend convenience).
        if isinstance(raw, dict) and "author" in raw:
            inner = raw["author"]
            if isinstance(inner, list):
                if not inner:
                    raise HTTPException(400, "author list cannot be empty")
                Author.model_validate(inner[0])
                # Keep ALL top-level keys the caller sent (taglines, etc.)
                return dict(raw)
            if isinstance(inner, dict):
                Author.model_validate(inner)
                # Wrap single-dict author but preserve every other top-level
                # key sent by the caller.
                merged = dict(raw)
                merged["author"] = [inner]
                return merged
            raise HTTPException(400, "author must wrap a list or dict")
        if isinstance(raw, dict):
            Author.model_validate(raw)
            return {"author": [raw]}
        raise HTTPException(400, "author payload must be a dict")
    except ValidationError as exc:
        raise HTTPException(400, f"schema validation failed: {exc.errors()[:3]}") from exc


# ---------------------------------------------------------------------------
# Write routes (MVP — comment loss accepted; ruamel.yaml round-trip is 0.8)
# ---------------------------------------------------------------------------


class WriteResponse(BaseModel):
    section: Section
    path: str
    bytes_written: int


def _merge_with_existing_author(target: Path, payload: Any) -> Any:
    """Preserve unknown top-level keys (e.g. ``taglines``) when saving author.

    The author YAML file may contain sibling keys beyond ``author:`` (taglines
    is the documented one in schemas/master.py). The MVP write path replaces
    the whole file, which silently drops them. Read the existing file, merge
    its top-level keys with the caller's payload (caller wins on conflict),
    and return the merged dict.
    """
    if not isinstance(payload, dict):
        return payload  # author canonical shape is always a dict
    if not target.exists():
        return payload
    try:
        existing = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return payload
    if not isinstance(existing, dict):
        return payload
    merged = dict(existing)
    merged.update(payload)
    return merged


@router.put("/master/{section}", response_model=WriteResponse)
def put_master_section(
    section: Section, body: Any = Body(...)  # noqa: B008
) -> WriteResponse:
    """Replace the YAML for *section* with the validated *body* contents.

    The body shape mirrors the corresponding GET response. Comments and key
    order in the existing YAML file are NOT preserved (yaml.safe_dump). For
    comment-preserving edits, see the 0.8 track plan.

    For ``author``, top-level sibling keys (taglines, etc.) on the existing
    file are merged into the written payload so they're not silently dropped.
    """
    if section not in _SECTIONS:
        raise HTTPException(400, f"invalid section: {section!r}")
    payload = _validate_section_payload(section, body)
    config_path = _require_config_path()
    target = _resolve_section_path(config_path, section)
    if section == "author":
        payload = _merge_with_existing_author(target, payload)
    _atomic_write_yaml(target, payload)
    return WriteResponse(
        section=section,
        path=str(target),
        bytes_written=target.stat().st_size,
    )


@router.post("/master/{section}/upload", response_model=WriteResponse)
async def upload_master_section(section: Section, file: UploadFile) -> WriteResponse:
    """Upload a raw YAML file replacing *section*.

    The upload is parsed, validated against the section schema, and atomically
    written to the configured path. Comments in the uploaded file are NOT
    preserved across the parse/dump round-trip (MVP limitation).
    """
    if section not in _SECTIONS:
        raise HTTPException(400, f"invalid section: {section!r}")
    raw_bytes = await file.read()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, f"file must be UTF-8: {exc}") from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"YAML parse failed: {exc}") from exc
    payload = _validate_section_payload(section, parsed)
    config_path = _require_config_path()
    target = _resolve_section_path(config_path, section)
    if section == "author":
        payload = _merge_with_existing_author(target, payload)
    _atomic_write_yaml(target, payload)
    return WriteResponse(
        section=section,
        path=str(target),
        bytes_written=target.stat().st_size,
    )


__all__ = ["router"]
