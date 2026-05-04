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
- Writes use ruamel.yaml round-trip: comments + key order are preserved on every
  PUT/upload cycle (feat-eb277ecd).  Atomic tmp-file + rename guarantees no partial
  writes on failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Body, HTTPException, UploadFile
from pydantic import BaseModel, ValidationError

from jobsmith.config import find_config, load_config
from jobsmith.master_io import MasterSection, save_master
from jobsmith.paths import resolve

from .schemas.master import Author, EducationEntry, MasterPayload, SkillEntry, WorkEntry

Section = Literal["work", "skill", "education", "author"]
_SECTIONS: tuple[Section, ...] = ("work", "skill", "education", "author")

_SECTION_MAP: dict[Section, MasterSection] = {
    "work": MasterSection.WORK,
    "skill": MasterSection.SKILL,
    "education": MasterSection.EDUCATION,
    "author": MasterSection.AUTHOR,
}

router = APIRouter(tags=["master"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_config_path() -> Path:
    """Return the .apply-config.yaml path or raise 404."""
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


def _normalise_author_payload(raw: Any) -> Any:
    """Normalise *raw* into the canonical ``{author: [...]}`` shape for saving.

    Accepts:
    - ``{author: [...]}``  — canonical, returned as-is
    - ``{author: {...}}``  — single-dict; wrapped in list
    - bare author dict     — wrapped as ``{author: [raw]}``
    """
    if isinstance(raw, dict) and "author" in raw:
        inner = raw["author"]
        if isinstance(inner, list):
            return dict(raw)
        if isinstance(inner, dict):
            merged = dict(raw)
            merged["author"] = [inner]
            return merged
    if isinstance(raw, dict):
        return {"author": [raw]}
    return raw


# ---------------------------------------------------------------------------
# Write routes (comment-preserving via ruamel.yaml — feat-eb277ecd)
# ---------------------------------------------------------------------------


class WriteResponse(BaseModel):
    section: Section
    path: str
    bytes_written: int


def _put_section(section: Section, body: Any) -> WriteResponse:
    """Shared logic for PUT handler and upload handler.

    Validates *body*, resolves the target path, delegates write to
    ``save_master`` (ruamel.yaml round-trip), and returns WriteResponse.
    """
    if section not in _SECTIONS:
        raise HTTPException(400, f"invalid section: {section!r}")

    config_path = _require_config_path()
    target = _resolve_section_path(config_path, section)

    master_section = _SECTION_MAP[section]

    payload = _normalise_author_payload(body) if section == "author" else body

    try:
        save_master(master_section, payload, target)
    except ValidationError as exc:
        raise HTTPException(400, f"schema validation failed: {exc.errors()[:3]}") from exc

    return WriteResponse(
        section=section,
        path=str(target),
        bytes_written=target.stat().st_size,
    )


@router.put("/master/{section}", response_model=WriteResponse)
def put_master_section(
    section: Section, body: Any = Body(...)  # noqa: B008
) -> WriteResponse:
    """Replace the YAML for *section* with the validated *body* contents.

    Comments and key order in the existing YAML file are preserved via
    ruamel.yaml round-trip merge (feat-eb277ecd).
    """
    return _put_section(section, body)


@router.post("/master/{section}/upload", response_model=WriteResponse)
async def upload_master_section(section: Section, file: UploadFile) -> WriteResponse:
    """Upload a raw YAML file replacing *section*.

    The upload is parsed, validated against the section schema, and atomically
    written to the configured path with comment preservation via ruamel.yaml.
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
    return _put_section(section, parsed)


__all__ = ["router"]
