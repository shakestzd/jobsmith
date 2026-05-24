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
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Body, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ValidationError

from jobsmith.config import find_config, load_config
from jobsmith.db import open_pipeline_db
from jobsmith.master_io import (
    MasterSection,
    add_bullet_in_blob,
    mark_anchor_in_blob,
    remove_bullet_in_blob,
)
from jobsmith.paths import repo_root_for, resolve

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

_log = logging.getLogger(__name__)

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


def _require_config_path(repo_root: Path | None = None) -> Path:
    """Return the .apply-config.yaml path or raise 404.

    When *repo_root* is provided (e.g. from ``app.state.repo_root``), the
    search starts there.  Otherwise the shared resolver chain is used so
    the function remains callable from non-request contexts (tests, CLI).
    """
    search_start = repo_root if repo_root is not None else repo_root_for()
    config_path = find_config(search_start)
    if config_path is None:
        raise HTTPException(status_code=404, detail="No .apply-config.yaml found")
    return config_path


def _get_db_path_for_master(repo_root: Path | None = None) -> Path | None:
    """Resolve the pipeline DB path for master content reads.

    When *repo_root* is provided (e.g. from ``app.state.repo_root``), the
    search starts there.  Otherwise the shared resolver chain is used.
    Returns None when no config is found (DB path is config-derived).
    Module-level so tests can monkeypatch it.
    """
    search_start = repo_root if repo_root is not None else repo_root_for()
    config_path = find_config(search_start)
    if config_path is None:
        return None
    try:
        config = load_config(path=config_path)
        root = config_path.parent
        return (root / config.output.jobsmith_db).resolve()
    except Exception:
        return None


def _db_load_section(section: str) -> str | None:
    """Query ``master_content`` for *section*.  Returns raw YAML text or None.

    Returns None when the DB path cannot be resolved, when the DB has no row
    for the section, or when any DB error occurs.  Callers fall back to the
    filesystem when None is returned.
    """
    db_path = _get_db_path_for_master()
    if db_path is None or not db_path.exists():
        return None
    try:
        conn = open_pipeline_db(db_path)
        try:
            row = conn.execute(
                "SELECT content_blob FROM master_content WHERE section = ?",
                (section,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        _log.debug("master: DB read failed for section %r", section, exc_info=True)
        return None
    if row is None:
        return None
    return row["content_blob"]


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


def _parse_yaml_list(blob: str) -> list[dict[str, Any]]:
    """Parse a YAML blob that contains a list.  Return [] on error."""
    try:
        data = yaml.safe_load(blob)
    except yaml.YAMLError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _parse_skill_blob(blob: str) -> list[SkillEntry]:
    """Parse a skill YAML blob (list or dict-of-lists form)."""
    raw = _parse_yaml_list(blob)
    if raw:
        return [SkillEntry.model_validate(item) for item in raw]
    # dict-of-lists form
    try:
        data = yaml.safe_load(blob)
    except yaml.YAMLError:
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


def _parse_author_blob(blob: str) -> Author | None:
    """Parse an author YAML blob into an Author model or None."""
    try:
        data = yaml.safe_load(blob)
    except yaml.YAMLError:
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


def _raise_missing_section(section: str) -> None:
    """Raise 404 with structured body when a master section is absent from the DB."""
    raise HTTPException(
        status_code=404,
        detail={
            "error": "missing_in_db",
            "section": section,
            "suggestion": f"jobsmith db load-master  # to backfill section '{section}'",
        },
    )


def _set_db_etag(response: Response, blob: str) -> None:
    """Set ETag header from blob content.

    Uses the full sha256 hex digest so the value matches ``etag_for_section``
    (which the PUT handler uses to validate If-Match) when the DB row was
    ingested from the same file bytes.
    """
    response.headers["ETag"] = (
        '"' + hashlib.sha256(blob.encode("utf-8")).hexdigest() + '"'
    )


@router.get("/master", response_model=MasterPayload)
def get_master(response: Response) -> MasterPayload:
    """Return all master content sections in one payload (DB-only)."""
    sections: dict[str, Any] = {}
    for section in _SECTIONS:
        blob = _db_load_section(section)
        if blob is None:
            _raise_missing_section(section)
        sections[section] = blob
    return MasterPayload(
        work=[WorkEntry.model_validate(item) for item in _parse_yaml_list(sections["work"])],
        skill=_parse_skill_blob(sections["skill"]),
        education=[
            EducationEntry.model_validate(item)
            for item in _parse_yaml_list(sections["education"])
        ],
        author=_parse_author_blob(sections["author"]),
    )


@router.get("/master/work", response_model=list[WorkEntry])
def get_master_work(response: Response) -> list[WorkEntry]:
    """Return the work history list (DB-only)."""
    blob = _db_load_section("work")
    if blob is None:
        _raise_missing_section("work")
    _set_db_etag(response, blob)
    return [WorkEntry.model_validate(item) for item in _parse_yaml_list(blob)]


@router.get("/master/skill", response_model=list[SkillEntry])
def get_master_skill(response: Response) -> list[SkillEntry]:
    """Return the skill categories list (DB-only)."""
    blob = _db_load_section("skill")
    if blob is None:
        _raise_missing_section("skill")
    _set_db_etag(response, blob)
    return _parse_skill_blob(blob)


@router.get("/master/education", response_model=list[EducationEntry])
def get_master_education(response: Response) -> list[EducationEntry]:
    """Return the education list (DB-only)."""
    blob = _db_load_section("education")
    if blob is None:
        _raise_missing_section("education")
    _set_db_etag(response, blob)
    return [EducationEntry.model_validate(item) for item in _parse_yaml_list(blob)]


@router.get("/master/author", response_model=Author | None)
def get_master_author(response: Response) -> Author | None:
    """Return the author block (DB-only)."""
    blob = _db_load_section("author")
    if blob is None:
        _raise_missing_section("author")
    _set_db_etag(response, blob)
    return _parse_author_blob(blob)


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


def _benchmark_load_text(config_path: Path) -> str:
    """Return current benchmark text, preferring DB over disk.

    DB-as-source-of-truth: read ``master_content`` row for section
    ``'benchmark'``. Fall back to ``assets/content/benchmark.md`` only when
    the DB has no row (fresh project before first ingest). Returns ``""``
    when neither source has content.
    """
    db_text = _db_load_section("benchmark")
    if db_text is not None:
        return db_text
    path = _benchmark_path(config_path)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _benchmark_save_db(text: str, *, repo_root: Path | None = None) -> None:
    """Upsert the benchmark text into ``master_content``. Raises 503 on no DB."""
    db_path = _get_db_path_for_master(repo_root=repo_root)
    if db_path is None or not db_path.exists():
        raise HTTPException(503, "pipeline DB unavailable — cannot persist benchmark")
    etag_short = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    conn = open_pipeline_db(db_path)
    try:
        from datetime import datetime, timezone

        conn.execute(
            "INSERT OR REPLACE INTO master_content "
            "(section, content_blob, etag, loaded_at) VALUES (?, ?, ?, ?)",
            ("benchmark", text, etag_short, datetime.now(tz=timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@router.get("/master/benchmark", response_model=BenchmarkResponse)
def get_benchmark(request: Request, response: Response) -> BenchmarkResponse:
    """Return the current benchmark text + version, DB-preferred.

    Returns ``{text: '', version: ''}`` when neither the DB row nor the
    file exist, so the frontend can show an empty editor.
    """
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    config_path = _require_config_path(repo_root=repo_root)
    text = _benchmark_load_text(config_path)
    if not text:
        response.headers["ETag"] = '""'
        return BenchmarkResponse(text="", version="")
    version = _content_version(text)
    response.headers["ETag"] = f'"{version}"'
    return BenchmarkResponse(text=text, version=version)


@router.put("/master/benchmark", response_model=BenchmarkResponse)
def put_benchmark(
    request: Request,
    body: BenchmarkPayload,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),  # noqa: B008
) -> BenchmarkResponse:
    """Persist *body.text* as the new benchmark in the DB only (S5 contract).

    DB-only: the ``master_content`` row for section ``'benchmark'`` is
    upserted. The ``benchmark.md`` file on disk is NOT touched — run
    ``jobsmith master export`` to materialise a YAML/MD snapshot.

    Concurrent-write guard: clients SHOULD send ``If-Match: "<version>"``
    where ``<version>`` is the SHA-256 hex of the current benchmark text
    from the most recent GET. A mismatch returns 412 Precondition Failed.
    The header is optional for backwards compatibility — omitting it
    accepts last-writer-wins.
    """
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    config_path = _require_config_path(repo_root=repo_root)
    if if_match is not None:
        current_text = _benchmark_load_text(config_path)
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
    _benchmark_save_db(body.text, repo_root=repo_root)
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
    *,
    repo_root: Path | None = None,
) -> WriteResponse:
    """Validate *body* and persist it to the master_content DB table (S5).

    The YAML file on disk is no longer touched by PUT — users regenerate it
    via ``jobsmith master export`` when they want to commit a snapshot to
    git.  Comment preservation is handled by reading the previous DB blob
    and round-tripping through ruamel.yaml.

    When *if_match* is provided, the current DB blob ETag is compared first;
    a mismatch raises 412 Precondition Failed.
    """
    if section not in _SECTIONS:
        raise HTTPException(400, f"invalid section: {section!r}")

    master_section = _SECTION_MAP[section]
    payload = _normalise_author_payload(body) if section == "author" else body

    existing_blob = _db_load_section(section)

    # ETag / If-Match check (DB-derived)
    current_etag = (
        hashlib.sha256(existing_blob.encode("utf-8")).hexdigest()
        if existing_blob is not None
        else ""
    )
    if if_match is not None:
        client_etag = if_match.strip('"')
        if client_etag != current_etag:
            raise HTTPException(
                412,
                detail="Precondition Failed: ETag mismatch — DB blob changed since last read",
            )

    try:
        from jobsmith.master_io import save_master_to_blob

        new_blob = save_master_to_blob(master_section, payload, existing_blob)
    except ValidationError as exc:
        raise HTTPException(400, f"schema validation failed: {exc.errors()[:3]}") from exc

    db_path = _get_db_path_for_master(repo_root=repo_root)
    if db_path is None or not db_path.exists():
        raise HTTPException(503, "pipeline DB unavailable — cannot persist section")

    new_etag_short = hashlib.sha256(new_blob.encode("utf-8")).hexdigest()[:16]
    conn = open_pipeline_db(db_path)
    try:
        from datetime import datetime, timezone

        conn.execute(
            "INSERT OR REPLACE INTO master_content "
            "(section, content_blob, etag, loaded_at) VALUES (?, ?, ?, ?)",
            (section, new_blob, new_etag_short, datetime.now(tz=timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    if response is not None:
        response.headers["ETag"] = (
            '"' + hashlib.sha256(new_blob.encode("utf-8")).hexdigest() + '"'
        )

    # Reported path is the DB row identity; bytes_written is the new blob length.
    return WriteResponse(
        section=section,
        path=f"db:master_content:{section}",
        bytes_written=len(new_blob.encode("utf-8")),
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


def _load_work_blob_for_bullet_op() -> str:
    """Return the work section blob from master_content, or 404 with backfill hint."""
    blob = _db_load_section("work")
    if blob is None:
        _raise_missing_section("work")
    return blob


def _persist_work_blob(new_blob: str, *, repo_root: Path | None = None) -> None:
    """Write *new_blob* to the master_content table for the work section.

    S5 contract: writes go to DB only, never to disk.  Closes ultrareview
    bug_005 — the bullet endpoints used to call _atomic_write to work.yml.
    """
    from datetime import datetime, timezone

    db_path = _get_db_path_for_master(repo_root=repo_root)
    if db_path is None or not db_path.exists():
        raise HTTPException(503, "pipeline DB unavailable — cannot persist bullet edit")

    etag_short = hashlib.sha256(new_blob.encode("utf-8")).hexdigest()[:16]
    conn = open_pipeline_db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO master_content "
            "(section, content_blob, etag, loaded_at) VALUES (?, ?, ?, ?)",
            ("work", new_blob, etag_short, datetime.now(tz=timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@router.post(
    "/master/work/roles/{role_index}/bullets/{bullet_index}/anchor",
    response_model=BulletWriteResponse,
)
def post_anchor_bullet(
    request: Request,
    role_index: int,
    bullet_index: int,
    body: AnchorPayload,
) -> BulletWriteResponse:
    """Mark bullet at ``work[role_index].details[bullet_index]`` as an anchor.

    DB-only: edits the master_content row, never the YAML file (S5).
    """
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    blob = _load_work_blob_for_bullet_op()
    try:
        new_blob = mark_anchor_in_blob(
            blob,
            role_index=role_index,
            bullet_index=bullet_index,
            drop_reason=body.drop_reason,
        )
    except IndexError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    _persist_work_blob(new_blob, repo_root=repo_root)
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
    request: Request,
    role_index: int,
    body: AddBulletPayload,
) -> BulletWriteResponse:
    """Append or insert a new bullet (DB-only, S5)."""
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    blob = _load_work_blob_for_bullet_op()
    try:
        new_blob, effective_index = add_bullet_in_blob(
            blob,
            role_index=role_index,
            text=body.text,
            position=body.position,
        )
    except IndexError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    _persist_work_blob(new_blob, repo_root=repo_root)
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
    request: Request,
    role_index: int,
    bullet_index: int,
    body: RemoveBulletPayload,
) -> BulletWriteResponse:
    """Remove or soft-drop bullet (DB-only, S5)."""
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    blob = _load_work_blob_for_bullet_op()
    try:
        new_blob = remove_bullet_in_blob(
            blob,
            role_index=role_index,
            bullet_index=bullet_index,
            reason=body.reason,
        )
    except IndexError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    _persist_work_blob(new_blob, repo_root=repo_root)
    return BulletWriteResponse(
        role_index=role_index,
        bullet_index=bullet_index,
        action="remove",
    )


@router.put("/master/{section}", response_model=WriteResponse)
def put_master_section(
    request: Request,
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
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    return _put_section(section, body, if_match=if_match, response=response, repo_root=repo_root)


@router.post("/master/{section}/upload", response_model=WriteResponse)
async def upload_master_section(
    request: Request, section: Section, file: UploadFile
) -> WriteResponse:
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
    repo_root: Path | None = getattr(request.app.state, "repo_root", None)
    return _put_section(section, parsed, repo_root=repo_root)


__all__ = ["router"]
