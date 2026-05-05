"""Pydantic models for the /api/master endpoint family.

These models mirror the real YAML shapes found in assets/content/:

work.yml
--------
A list of position dicts:
  - title: str          (position title, e.g. "Senior Data Engineer")
  - location: str       (company name — the YAML convention is unusual)
  - date: str
  - description: str    (e.g. "Remote")
  - details: list       (each item is str OR dict with bullet/anchor/tags/...)

skill.yml
---------
A list of category dicts:
  - title: str
  - description: str    (comma-separated skills string)
  - details: list[str]  (individual skill items)

education.yml
-------------
A list of institution dicts:
  - title: str          (institution name)
  - location: str
  - date: str
  - description: str    (degree)
  - details: list[str]  (thesis, honors, coursework, etc.)

author.yml
----------
Top-level dict:
  author: list of author dicts (first item is used)
    - name: {first, middle, last} OR str
    - address, email, phone, homepage, photo, position, contacts, ...
  taglines: dict[str, str]  (optional, role-type keyed)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkDetailDict(BaseModel):
    """Object-form work bullet (Slice A schema)."""

    bullet: str
    anchor: bool | None = None
    anchor_reason: str | None = None
    tags: list[str] = Field(default_factory=list)
    drop_when: str | None = None

    model_config = {"extra": "allow"}


class WorkEntry(BaseModel):
    """One position entry from work.yml.

    ``location`` holds the company name (jobsmith YAML convention).
    ``details`` items are either plain strings or object-form bullet dicts.
    """

    title: str
    location: str = ""
    date: str = ""
    description: str = ""
    details: list[str | dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class SkillEntry(BaseModel):
    """One skill category entry from skill.yml (list form).

    The examples use a list-of-categories shape where each category has
    ``title``, ``description``, and ``details``.
    """

    title: str
    description: str = ""
    details: list[str] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class EducationEntry(BaseModel):
    """One education entry from education.yml.

    ``title`` is the institution name; ``description`` is the degree.
    """

    title: str
    location: str = ""
    date: str = ""
    description: str = ""
    details: list[str] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class AuthorName(BaseModel):
    """Nested name block (Pat Doe shape)."""

    first: str = ""
    middle: str = ""
    last: str = ""

    model_config = {"extra": "allow"}


class AuthorContact(BaseModel):
    """One entry in the author contacts list."""

    icon: str = ""
    text: str = ""
    url: str = ""

    model_config = {"extra": "allow"}


class Author(BaseModel):
    """Single author block from author.yml ``author[0]``.

    Supports both flat (firstname/lastname) and nested (name.first/last) forms.
    The ``name`` field is ``Any`` to handle either str or dict.
    """

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
    contacts: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class MasterPayload(BaseModel):
    """Full /api/master response — all four content sections."""

    work: list[WorkEntry] = Field(default_factory=list)
    skill: list[SkillEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    author: Author | None = None


class ValidateError(BaseModel):
    """One validation error entry returned by POST /api/master/validate."""

    field: str
    message: str


class ValidateRequest(BaseModel):
    """Request body for POST /api/master/validate.

    All sections are optional so callers may validate a subset.
    """

    work: list[WorkEntry] = Field(default_factory=list)
    skill: list[SkillEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    author: Author | None = None


class ValidateResponse(BaseModel):
    """Response body for POST /api/master/validate."""

    ok: bool
    errors: list[ValidateError] = Field(default_factory=list)


__all__ = [
    "Author",
    "AuthorContact",
    "AuthorName",
    "EducationEntry",
    "MasterPayload",
    "SkillEntry",
    "ValidateError",
    "ValidateRequest",
    "ValidateResponse",
    "WorkDetailDict",
    "WorkEntry",
]
