"""jobsmith.lint — reusable YAML validation library (feat-01cad829).

Entry point
-----------
validate_masters(repo_root) -> LintResult
    Validate ALL four master YAMLs (work / skill / education / author) and
    return a structured result so callers can act on errors without subprocess.

validate_masters_from_paths(paths) -> LintResult
    Lower-level variant accepting an explicit PathSet rather than auto-resolving
    from repo_root; useful in tests.

LintResult
----------
    dataclass with:
      ok: bool                  — True iff no errors
      errors: list[str]         — human-readable error messages
      exit_code: int            — 0 or 1

The jobsmith lint CLI command is a thin wrapper calling validate_masters().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class LintResult:
    """Structured result from a lint run."""

    ok: bool
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# PathSet: optional explicit paths (for tests / merge-loop)
# ---------------------------------------------------------------------------


@dataclass
class MasterPathSet:
    """Explicit paths to the four master YAMLs."""

    work_yml: Path
    skill_yml: Path
    education_yml: Path
    author_yml: Path


# ---------------------------------------------------------------------------
# Section validators
# ---------------------------------------------------------------------------


def _validate_work(data: Any, path: Path, errors: list[str]) -> None:
    """work.yml must be a list of position dicts."""
    if data is None:
        return  # empty file is OK
    if not isinstance(data, list):
        errors.append(
            f"{path}:1: root must be a list of positions, got {type(data).__name__}"
        )
        return
    for i, pos in enumerate(data):
        if not isinstance(pos, dict):
            errors.append(
                f"{path}: position[{i}] must be a mapping, got {type(pos).__name__}"
            )
            continue
        details = pos.get("details")
        if details is not None and not isinstance(details, list):
            errors.append(
                f"{path}: position[{i}].details must be a list, "
                f"got {type(details).__name__}"
            )


def _validate_skill(data: Any, path: Path, errors: list[str]) -> None:
    """skill.yml: if present, must be a dict with a 'skills' list."""
    if data is None:
        return
    if not isinstance(data, (dict, list)):
        errors.append(
            f"{path}:1: root must be a mapping or list, got {type(data).__name__}"
        )
        return
    if isinstance(data, dict):
        skills = data.get("skills")
        if skills is not None and not isinstance(skills, list):
            errors.append(
                f"{path}: 'skills' key must be a list, got {type(skills).__name__}"
            )
    # list form: each element should be a dict
    if isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(
                    f"{path}: skill[{i}] must be a mapping, got {type(item).__name__}"
                )


def _validate_education(data: Any, path: Path, errors: list[str]) -> None:
    """education.yml: if present, must be a list or dict with 'entries' list."""
    if data is None:
        return
    if not isinstance(data, (dict, list)):
        errors.append(
            f"{path}:1: root must be a mapping or list, got {type(data).__name__}"
        )
        return
    if isinstance(data, dict):
        entries = data.get("entries")
        if entries is not None and not isinstance(entries, list):
            errors.append(
                f"{path}: 'entries' key must be a list, got {type(entries).__name__}"
            )
    if isinstance(data, list):
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(
                    f"{path}: education[{i}] must be a mapping, got {type(item).__name__}"
                )


def _validate_author(data: Any, path: Path, errors: list[str]) -> None:
    """author.yml: if present, must be a dict."""
    if data is None:
        return
    if not isinstance(data, dict):
        errors.append(
            f"{path}:1: root must be a mapping, got {type(data).__name__}"
        )
        return
    # No required fields enforced here — just structural validity.


# ---------------------------------------------------------------------------
# Core validate function
# ---------------------------------------------------------------------------

_VALIDATORS = {
    "work.yml": _validate_work,
    "skill.yml": _validate_skill,
    "education.yml": _validate_education,
    "author.yml": _validate_author,
}


def _validate_path(path: Path, errors: list[str]) -> None:
    """Parse a single YAML file and run its section validator."""
    if not path.exists():
        # Missing master is valid (not yet written)
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{path}: YAML parse error — {exc}")
        return

    validator = _VALIDATORS.get(path.name)
    if validator is not None:
        validator(data, path, errors)
    else:
        # Generic: must be a parseable YAML document (any type)
        pass


def validate_masters_from_paths(paths: MasterPathSet) -> LintResult:
    """Validate the four master YAMLs described by *paths*.

    Returns a LintResult with ok=True iff no structural errors are found.
    Missing files are silently accepted (not yet written is OK).
    """
    errors: list[str] = []

    _validate_path(paths.work_yml, errors)
    _validate_path(paths.skill_yml, errors)
    _validate_path(paths.education_yml, errors)
    _validate_path(paths.author_yml, errors)

    return LintResult(ok=not errors, errors=errors)


def validate_masters(repo_root: Path) -> LintResult:
    """Validate all four master YAMLs under *repo_root*.

    Resolves master paths from the repo's .apply-config.yaml (or uses
    defaults) and delegates to validate_masters_from_paths().
    """
    try:
        from jobsmith.config import find_config, load_config
        from jobsmith.paths import resolve

        config_path = find_config(repo_root)
        if config_path is not None:
            config = load_config(config_path)
            paths = MasterPathSet(
                work_yml=resolve(config.master.work_yml, repo_root),
                skill_yml=resolve(config.master.skill_yml, repo_root),
                education_yml=resolve(config.master.education_yml, repo_root),
                author_yml=resolve(config.master.author_yml, repo_root),
            )
        else:
            # Fallback to conventional defaults relative to repo_root
            base = repo_root / "assets" / "content"
            paths = MasterPathSet(
                work_yml=base / "work.yml",
                skill_yml=base / "skill.yml",
                education_yml=base / "education.yml",
                author_yml=base / "author.yml",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("lint: could not load config — %s", exc)
        base = repo_root / "assets" / "content"
        paths = MasterPathSet(
            work_yml=base / "work.yml",
            skill_yml=base / "skill.yml",
            education_yml=base / "education.yml",
            author_yml=base / "author.yml",
        )

    return validate_masters_from_paths(paths)
