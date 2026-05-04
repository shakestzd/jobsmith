"""Read-only /api/master router for the jobsmith HTTP API.

Endpoints
---------
GET /master          → MasterPayload  (all four sections)
GET /master/work     → list[WorkEntry]
GET /master/skill    → list[SkillEntry]
GET /master/education → list[EducationEntry]
GET /master/author   → Author | None

Behavior contract
-----------------
- 200 + parsed content when .apply-config.yaml + YAMLs are found.
- 200 + empty list (or null for author) when a YAML file is missing.
- 404 when find_config() cannot locate .apply-config.yaml up the cwd tree.

Read-only. No PUT / POST / PATCH / DELETE endpoints in this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException

from jobsmith.config import find_config, load_config
from jobsmith.paths import resolve

from .schemas.master import Author, EducationEntry, MasterPayload, SkillEntry, WorkEntry

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


__all__ = ["router"]
