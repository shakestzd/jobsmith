"""/api/master router for the jobsmith HTTP API.

Endpoints
---------
GET  /master                       → MasterPayload  (all four sections)
GET  /master/{section}             → section content (work | skill | education | author)
PUT  /master/{section}             → replace section content (validated against schema)
POST /master/{section}/upload      → upload a raw YAML file replacing the section
POST /master/validate              → validate master content body; returns {ok, errors}
GET  /master/benchmark             → {text, version} for benchmark.md
PUT  /master/benchmark             → write new benchmark.md text, returns {text, version}

Behavior contract
-----------------
- Reads return 200 + content; 404 when .apply-config.yaml is missing up the cwd tree.
- Writes use ruamel.yaml round-trip: comments + key order are preserved on every
  PUT/upload cycle (feat-eb277ecd).  Atomic tmp-file + rename guarantees no partial
  writes on failure.
- benchmark.md is a plain-text markdown file; no schema validation, no YAML round-trip.
  version is a SHA-256 hex digest of the file content (empty string when file absent).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Body, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, ValidationError

from jobsmith.config import find_config, load_config
from jobsmith.master_io import (
    MasterSection,
    add_bullet,
    etag_for_section,
    mark_anchor,
    remove_bullet,
    save_benchmark,
    save_master,
)
from jobsmith.paths import resolve

from .schemas.master import (
    Author,
    EducationEntry,
    MasterPayload,
    SkillEntry,
    ValidateError,
    ValidateRequest,
    ValidateResponse,
    WorkEntry,
)

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
def get_master_work(response: Response) -> list[WorkEntry]:
    """Return the work history list from work.yml.

    Includes an ``ETag`` response header (SHA-256 hex of work.yml content)
    for concurrent-write safety.
    """
    config_path = _require_config_path()
    target = _resolve_section_path(config_path, "work")
    response.headers["ETag"] = f'"{etag_for_section(target)}"'
    return _load_work(config_path)


@router.get("/master/skill", response_model=list[SkillEntry])
def get_master_skill(response: Response) -> list[SkillEntry]:
    """Return the skill categories list from skill.yml.

    Includes an ``ETag`` response header for concurrent-write safety.
    """
    config_path = _require_config_path()
    target = _resolve_section_path(config_path, "skill")
    response.headers["ETag"] = f'"{etag_for_section(target)}"'
    return _load_skill(config_path)


@router.get("/master/education", response_model=list[EducationEntry])
def get_master_education(response: Response) -> list[EducationEntry]:
    """Return the education list from education.yml.

    Includes an ``ETag`` response header for concurrent-write safety.
    """
    config_path = _require_config_path()
    target = _resolve_section_path(config_path, "education")
    response.headers["ETag"] = f'"{etag_for_section(target)}"'
    return _load_education(config_path)


@router.get("/master/author", response_model=Author | None)
def get_master_author(response: Response) -> Author | None:
    """Return the author block from author.yml, or null if the file is missing.

    Includes an ``ETag`` response header for concurrent-write safety.
    """
    config_path = _require_config_path()
    target = _resolve_section_path(config_path, "author")
    response.headers["ETag"] = f'"{etag_for_section(target)}"'
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
# Validate route — registered before the templated /master/{section} routes
# so that `/master/validate` is not shadowed by the section-parameterised ones.
# ---------------------------------------------------------------------------


def _validate_work(entries: list[WorkEntry]) -> list[ValidateError]:
    errors: list[ValidateError] = []
    for i, entry in enumerate(entries):
        if not entry.title or not entry.title.strip():
            errors.append(
                ValidateError(
                    field=f"work[{i}].title",
                    message="title must not be empty",
                )
            )
    return errors


def _validate_skill(entries: list[SkillEntry]) -> list[ValidateError]:
    errors: list[ValidateError] = []
    for i, entry in enumerate(entries):
        if not entry.title or not entry.title.strip():
            errors.append(
                ValidateError(
                    field=f"skill[{i}].title",
                    message="title must not be empty",
                )
            )
    return errors


def _validate_education(entries: list[EducationEntry]) -> list[ValidateError]:
    errors: list[ValidateError] = []
    for i, entry in enumerate(entries):
        if not entry.title or not entry.title.strip():
            errors.append(
                ValidateError(
                    field=f"education[{i}].title",
                    message="title must not be empty",
                )
            )
    return errors


def _validate_author(author: Author | None) -> list[ValidateError]:
    """Validate author block; returns errors if required fields are absent."""
    if author is None:
        return []
    errors: list[ValidateError] = []
    # name must be a non-empty string or a dict with at least first/last
    name = author.name
    if name is None and not author.firstname and not author.lastname:
        errors.append(
            ValidateError(
                field="author.name",
                message="author must have a name",
            )
        )
    elif isinstance(name, str) and not name.strip():
        errors.append(
            ValidateError(
                field="author.name",
                message="author name must not be empty",
            )
        )
    return errors


@router.post("/master/validate", response_model=ValidateResponse)
def post_master_validate(body: ValidateRequest) -> ValidateResponse:
    """Validate master content body against section schemas.

    Returns ``{ok: true, errors: []}`` when all sections are valid.
    Returns ``{ok: false, errors: [{field, message}, ...]}`` when any
    section fails validation.

    This endpoint does **not** read or write any files — it validates
    only the submitted payload.
    """
    errors: list[ValidateError] = []
    errors.extend(_validate_work(body.work))
    errors.extend(_validate_skill(body.skill))
    errors.extend(_validate_education(body.education))
    errors.extend(_validate_author(body.author))
    return ValidateResponse(ok=len(errors) == 0, errors=errors)


# ---------------------------------------------------------------------------
# Benchmark routes (plain markdown — no YAML round-trip)
#
# Registered BEFORE the templated /master/{section} routes so that
# `/master/benchmark` matches these handlers, not the section-validated ones.
# ---------------------------------------------------------------------------

_BENCHMARK_DEFAULT = Path("assets/content/benchmark.md")


class BenchmarkPayload(BaseModel):
    """Request body for PUT /master/benchmark."""

    text: str


class BenchmarkResponse(BaseModel):
    """Response body for GET/PUT /master/benchmark."""

    text: str
    version: str  # SHA-256 hex digest of text content; "" when file absent


def _benchmark_path(config_path: Path) -> Path:
    """Return the benchmark.md path relative to the repo root."""
    repo_root = config_path.parent
    return repo_root / _BENCHMARK_DEFAULT


def _content_version(text: str) -> str:
    """Return the file content-hash version token for *text*.

    Uses the same full SHA-256 hex digest as :func:`etag_for_section` so
    GET ``ETag`` and ``BenchmarkResponse.version`` match byte-for-byte.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@router.get("/master/benchmark", response_model=BenchmarkResponse)
def get_benchmark(response: Response) -> BenchmarkResponse:
    """Return the raw text + version of benchmark.md.

    Returns ``{text: '', version: ''}`` when the file is absent (not a 404)
    so the frontend can show an empty editor rather than an error page.
    """
    config_path = _require_config_path()
    path = _benchmark_path(config_path)
    if not path.exists():
        response.headers["ETag"] = '""'
        return BenchmarkResponse(text="", version="")
    text = path.read_text(encoding="utf-8")
    version = _content_version(text)
    response.headers["ETag"] = f'"{version}"'
    return BenchmarkResponse(text=text, version=version)


@router.put("/master/benchmark", response_model=BenchmarkResponse)
def put_benchmark(
    body: BenchmarkPayload,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),  # noqa: B008
) -> BenchmarkResponse:
    """Atomically replace benchmark.md with *body.text*.

    Concurrent-write guard: clients SHOULD send ``If-Match: "<version>"``
    where ``<version>`` is the SHA-256 hex of the file content from the
    most recent GET. A mismatch returns 412 Precondition Failed. The
    header is optional for backwards compatibility — omitting it accepts
    last-writer-wins, matching the existing master-section semantics.
    """
    config_path = _require_config_path()
    path = _benchmark_path(config_path)
    if if_match is not None:
        current_text = path.read_text(encoding="utf-8") if path.exists() else ""
        current_version = _content_version(current_text) if current_text else ""
        # Strip surrounding double-quotes that conform to the HTTP ETag spec.
        sent = if_match.strip().strip('"')
        if sent != current_version:
            raise HTTPException(
                status_code=412,
                detail=(
                    f"If-Match version mismatch: client sent {sent!r}, "
                    f"current is {current_version!r}"
                ),
            )
    save_benchmark(body.text, path)
    new_version = _content_version(body.text)
    response.headers["ETag"] = f'"{new_version}"'
    return BenchmarkResponse(text=body.text, version=new_version)


# ---------------------------------------------------------------------------
# Write routes (comment-preserving via ruamel.yaml — feat-eb277ecd)
# ---------------------------------------------------------------------------


class WriteResponse(BaseModel):
    section: Section
    path: str
    bytes_written: int


def _put_section(
    section: Section,
    body: Any,
    if_match: str | None = None,
    response: Response | None = None,
) -> WriteResponse:
    """Shared logic for PUT handler and upload handler.

    Validates *body*, resolves the target path, delegates write to
    ``save_master`` (ruamel.yaml round-trip), and returns WriteResponse.

    When *if_match* is provided, the current file ETag is compared first;
    a mismatch raises HTTP 412 Precondition Failed without touching the file.
    """
    if section not in _SECTIONS:
        raise HTTPException(400, f"invalid section: {section!r}")

    config_path = _require_config_path()
    target = _resolve_section_path(config_path, section)

    # ETag / If-Match check
    current_etag = etag_for_section(target)
    if if_match is not None:
        # Strip optional surrounding quotes from the If-Match value
        client_etag = if_match.strip('"')
        if client_etag != current_etag:
            raise HTTPException(
                412,
                detail="Precondition Failed: ETag mismatch — file was modified since last read",
            )

    master_section = _SECTION_MAP[section]

    payload = _normalise_author_payload(body) if section == "author" else body

    try:
        save_master(master_section, payload, target)
    except ValidationError as exc:
        raise HTTPException(400, f"schema validation failed: {exc.errors()[:3]}") from exc

    # Attach new ETag to response headers if caller passed a Response object
    if response is not None:
        response.headers["ETag"] = f'"{etag_for_section(target)}"'

    return WriteResponse(
        section=section,
        path=str(target),
        bytes_written=target.stat().st_size,
    )


# ---------------------------------------------------------------------------
# Bullet-level endpoints (registered BEFORE the templated /master/{section}
# routes so they are not shadowed by the section-parameterised handlers)
# ---------------------------------------------------------------------------


class AnchorPayload(BaseModel):
    """Request body for POST .../anchor."""

    drop_reason: str | None = None


class AddBulletPayload(BaseModel):
    """Request body for POST .../bullets."""

    text: str
    position: int | None = None


class RemoveBulletPayload(BaseModel):
    """Request body for DELETE .../bullets/{bullet_index}."""

    reason: str


class BulletWriteResponse(BaseModel):
    """Minimal response for bullet mutation endpoints."""

    role_index: int
    bullet_index: int
    action: str


def _require_work_path(config_path: Path) -> Path:
    """Return the work.yml path, raising 404 if the config is missing."""
    return _resolve_section_path(config_path, "work")


@router.post(
    "/master/work/roles/{role_index}/bullets/{bullet_index}/anchor",
    response_model=BulletWriteResponse,
)
def post_anchor_bullet(
    role_index: int,
    bullet_index: int,
    body: AnchorPayload,
) -> BulletWriteResponse:
    """Mark bullet at ``work[role_index].details[bullet_index]`` as an anchor.

    When ``drop_reason`` is omitted, sets ``anchor=True``.
    When ``drop_reason`` is provided, sets ``anchor=False`` and
    ``drop_when=<drop_reason>``.
    """
    config_path = _require_config_path()
    work_path = _require_work_path(config_path)
    try:
        mark_anchor(
            work_path,
            role_index=role_index,
            bullet_index=bullet_index,
            drop_reason=body.drop_reason,
        )
    except IndexError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    action = "drop" if body.drop_reason is not None else "anchor"
    return BulletWriteResponse(
        role_index=role_index,
        bullet_index=bullet_index,
        action=action,
    )


@router.post(
    "/master/work/roles/{role_index}/bullets",
    response_model=BulletWriteResponse,
)
def post_add_bullet(
    role_index: int,
    body: AddBulletPayload,
) -> BulletWriteResponse:
    """Append or insert a new bullet into ``work[role_index].details``."""
    config_path = _require_config_path()
    work_path = _require_work_path(config_path)
    try:
        add_bullet(
            work_path,
            role_index=role_index,
            text=body.text,
            position=body.position,
        )
    except IndexError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    # Determine effective bullet_index for the response (yaml is already
    # imported at module level — no need for a local re-import).
    data = yaml.safe_load(work_path.read_text(encoding="utf-8"))
    details = data[role_index].get("details", [])
    effective_index = len(details) - 1 if body.position is None else body.position
    return BulletWriteResponse(
        role_index=role_index,
        bullet_index=effective_index,
        action="add",
    )


@router.delete(
    "/master/work/roles/{role_index}/bullets/{bullet_index}",
    response_model=BulletWriteResponse,
)
def delete_bullet(
    role_index: int,
    bullet_index: int,
    body: RemoveBulletPayload,
) -> BulletWriteResponse:
    """Remove or soft-drop bullet at ``work[role_index].details[bullet_index]``."""
    config_path = _require_config_path()
    work_path = _require_work_path(config_path)
    try:
        remove_bullet(
            work_path,
            role_index=role_index,
            bullet_index=bullet_index,
            reason=body.reason,
        )
    except IndexError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return BulletWriteResponse(
        role_index=role_index,
        bullet_index=bullet_index,
        action="remove",
    )


@router.put("/master/{section}", response_model=WriteResponse)
def put_master_section(
    section: Section,
    body: Any = Body(...),  # noqa: B008
    response: Response = Response(),  # noqa: B008
    if_match: str | None = Header(None, alias="If-Match"),  # noqa: B008
) -> WriteResponse:
    """Replace the YAML for *section* with the validated *body* contents.

    Comments and key order in the existing YAML file are preserved via
    ruamel.yaml round-trip merge (feat-eb277ecd).

    When the ``If-Match`` request header is present, the current file ETag is
    checked first; a mismatch returns HTTP 412 Precondition Failed without
    modifying the file.
    """
    return _put_section(section, body, if_match=if_match, response=response)


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
