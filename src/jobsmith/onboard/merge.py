"""jobsmith.onboard.merge — candidate→master merge with lint-gate (feat-01cad829).

Reads candidate-*.json files from the .onboard-state/ directory, merges them
(+ any gap-interview answers) into the four master YAMLs at assets/content/,
and validates them via jobsmith.lint.  On lint failure it loops up to
MAX_LINT_ATTEMPTS times, then stops and reports remaining errors — never
persisting broken masters, never looping forever.

Public entry points
-------------------
merge_candidates_to_masters(
    state_dir,
    repo_root,
    answers,            # from gap_interview
    *,
    clobber,            # "force" | "merge"
    lint_fn,            # injectable for tests
    max_attempts,
) -> MergeResult

MergeResult
-----------
    ok: bool
    lint_errors: list[str]     — remaining errors after final attempt
    summary: OnboardSummary    — categorized fields

OnboardSummary
--------------
    imported: list[str]        — fields sourced from candidate-*.json
    user_supplied: list[str]   — fields filled by gap-interview
    still_optional: list[str]  — optional fields left empty
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

MAX_LINT_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class OnboardSummary:
    """Categorized fields after the merge."""

    imported: list[str] = field(default_factory=list)
    user_supplied: list[str] = field(default_factory=list)
    still_optional: list[str] = field(default_factory=list)


@dataclass
class MergeResult:
    """Result of merge_candidates_to_masters()."""

    ok: bool
    lint_errors: list[str] = field(default_factory=list)
    summary: OnboardSummary = field(default_factory=OnboardSummary)


# ---------------------------------------------------------------------------
# Helpers: load candidate-*.json
# ---------------------------------------------------------------------------


def _load_candidate(state_dir: Path, section: str) -> dict | None:
    path = state_dir / f"candidate-{section}.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("data", raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("merge: could not load %s — %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Helpers: resolve master paths
# ---------------------------------------------------------------------------


def _resolve_master_paths(repo_root: Path):
    """Return (work, skill, education, author) Path objects for master YAMLs."""
    try:
        from jobsmith.config import find_config, load_config
        from jobsmith.paths import resolve

        config_path = find_config(repo_root)
        if config_path is not None:
            config = load_config(config_path)
            return (
                resolve(config.master.work_yml, repo_root),
                resolve(config.master.skill_yml, repo_root),
                resolve(config.master.education_yml, repo_root),
                resolve(config.master.author_yml, repo_root),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("merge: could not load config — %s", exc)

    base = repo_root / "assets" / "content"
    return (
        base / "work.yml",
        base / "skill.yml",
        base / "education.yml",
        base / "author.yml",
    )


# ---------------------------------------------------------------------------
# Per-section serializers
# ---------------------------------------------------------------------------


def _build_work_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build the work.yml content from candidate data + answers."""
    if data:
        entries = data.get("entries", [])
        if entries:
            return entries
    # Answers can provide raw YAML if user typed it
    raw = answers.get("work.entries", "")
    if raw:
        try:
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, list):
                return parsed
        except yaml.YAMLError:
            pass
        # Treat as a single free-text bullet
        return [{"title": raw, "details": []}]
    return []


def _build_skill_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build the skill.yml content from candidate data + answers."""
    if data:
        skills = data.get("skills", [])
        if skills:
            return {"skills": skills}
    raw = answers.get("skill.skills", "")
    if raw:
        skill_list = [s.strip() for s in raw.replace(",", "\n").splitlines() if s.strip()]
        return {"skills": [{"name": s, "category": "technical"} for s in skill_list]}
    return {"skills": []}


def _build_education_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build the education.yml content from candidate data + answers."""
    if data:
        entries = data.get("entries", [])
        if entries:
            return {"entries": entries}
    raw = answers.get("education.entries", "")
    if raw:
        return {"entries": [{"institution": raw, "degree": "", "field": "", "end_date": ""}]}
    return {"entries": []}


def _build_author_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build the author.yml content from candidate data + answers."""
    author: dict[str, str] = {}
    if data:
        for key in ("name", "email", "phone", "location", "github", "linkedin"):
            val = data.get(key, "")
            if val:
                author[key] = str(val)

    # Fill missing from gap answers
    for key in ("name", "email", "phone", "location", "github", "linkedin"):
        if not author.get(key):
            val = answers.get(f"author.{key}", "")
            if val:
                author[key] = val

    return author


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _build_summary(
    work_data: dict | None,
    skill_data: dict | None,
    education_data: dict | None,
    author_data: dict | None,
    answers: dict[str, str],
) -> OnboardSummary:
    summary = OnboardSummary()

    # Work
    if work_data and work_data.get("entries"):
        summary.imported.append("work.entries")
    elif answers.get("work.entries"):
        summary.user_supplied.append("work.entries")

    # Skills
    if skill_data and skill_data.get("skills"):
        summary.imported.append("skill.skills")
    elif answers.get("skill.skills"):
        summary.user_supplied.append("skill.skills")

    # Education
    if education_data and education_data.get("entries"):
        summary.imported.append("education.entries")
    elif answers.get("education.entries"):
        summary.user_supplied.append("education.entries")
    else:
        summary.still_optional.append("education.entries")

    # Author fields
    for key in ("name", "email"):
        val = (author_data or {}).get(key, "")
        if val:
            summary.imported.append(f"author.{key}")
        elif answers.get(f"author.{key}"):
            summary.user_supplied.append(f"author.{key}")

    for key in ("phone", "location", "github", "linkedin"):
        val = (author_data or {}).get(key, "")
        if val:
            summary.imported.append(f"author.{key}")
        elif answers.get(f"author.{key}"):
            summary.user_supplied.append(f"author.{key}")
        else:
            summary.still_optional.append(f"author.{key}")

    return summary


# ---------------------------------------------------------------------------
# Merge + lint-gate
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: Any) -> None:
    """Write data as YAML to path, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")


def _read_existing(path: Path) -> Any:
    """Read existing YAML at path; return None if absent or empty."""
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None


def _merge_work(existing: Any, incoming: Any) -> Any:
    """Merge existing work.yml (list) with incoming (list), deduping by title+company."""
    if not isinstance(existing, list):
        return incoming if isinstance(incoming, list) else []
    if not isinstance(incoming, list):
        return existing

    seen: set[str] = set()
    result = []
    for item in existing:
        key = f"{item.get('title','')}|{item.get('company', item.get('employer',''))}"
        seen.add(key)
        result.append(item)
    for item in incoming:
        key = f"{item.get('title','')}|{item.get('company', item.get('employer',''))}"
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _merge_skill(existing: Any, incoming: Any) -> Any:
    """Merge skill dicts — union of skills lists by name."""
    ex_skills = (existing or {}).get("skills", []) if isinstance(existing, dict) else []
    in_skills = (incoming or {}).get("skills", []) if isinstance(incoming, dict) else []
    seen: set[str] = {s.get("name", "") for s in ex_skills}
    merged = list(ex_skills)
    for s in in_skills:
        if s.get("name", "") not in seen:
            seen.add(s.get("name", ""))
            merged.append(s)
    return {"skills": merged}


def _merge_education(existing: Any, incoming: Any) -> Any:
    """Merge education dicts — union of entries by institution."""
    ex_entries = (existing or {}).get("entries", []) if isinstance(existing, dict) else []
    in_entries = (incoming or {}).get("entries", []) if isinstance(incoming, dict) else []
    seen: set[str] = {e.get("institution", "") for e in ex_entries}
    merged = list(ex_entries)
    for e in in_entries:
        if e.get("institution", "") not in seen:
            seen.add(e.get("institution", ""))
            merged.append(e)
    return {"entries": merged}


def _merge_author(existing: Any, incoming: Any) -> Any:
    """Merge author dicts — incoming fills only missing keys."""
    if not isinstance(existing, dict):
        return incoming or {}
    if not isinstance(incoming, dict):
        return existing
    result = dict(existing)
    for key, val in incoming.items():
        if not result.get(key):
            result[key] = val
    return result


def merge_candidates_to_masters(
    state_dir: Path,
    repo_root: Path,
    answers: dict[str, str],
    *,
    clobber: str = "merge",
    lint_fn: Callable | None = None,
    max_attempts: int = MAX_LINT_ATTEMPTS,
) -> MergeResult:
    """Merge candidate-*.json + gap answers into master YAMLs, then lint-gate.

    Parameters
    ----------
    state_dir:
        Path to .onboard-state/ containing candidate-*.json.
    repo_root:
        Repo root (used to resolve master YAML paths via config).
    answers:
        Gap-interview answers keyed "<section>.<field>".
    clobber:
        "force" — overwrite masters with candidate data.
        "merge" — merge candidate into existing master (non-destructive).
    lint_fn:
        Callable(MasterPathSet) -> LintResult for injection in tests.
        Defaults to jobsmith.lint.validate_masters_from_paths.
    max_attempts:
        Maximum lint-fix loop iterations.  After this many failures the
        function returns MergeResult(ok=False, ...) without persisting.

    Returns
    -------
    MergeResult
        ok=True iff lint passed on at least one attempt.
    """
    _custom_lint_fn = lint_fn  # may be None → use path-based default below

    work_path, skill_path, education_path, author_path = _resolve_master_paths(repo_root)

    # Load candidates
    work_data = _load_candidate(state_dir, "work")
    skill_data = _load_candidate(state_dir, "skill")
    education_data = _load_candidate(state_dir, "education")
    author_data = _load_candidate(state_dir, "author")

    # Build target content
    work_content = _build_work_yaml(work_data, answers)
    skill_content = _build_skill_yaml(skill_data, answers)
    education_content = _build_education_yaml(education_data, answers)
    author_content = _build_author_yaml(author_data, answers)

    # Apply clobber policy
    if clobber == "merge":
        existing_work = _read_existing(work_path)
        existing_skill = _read_existing(skill_path)
        existing_education = _read_existing(education_path)
        existing_author = _read_existing(author_path)

        work_content = _merge_work(existing_work, work_content)
        skill_content = _merge_skill(existing_skill, skill_content)
        education_content = _merge_education(existing_education, education_content)
        author_content = _merge_author(existing_author, author_content)

    # Lint-gate loop: write to tmp paths, lint, move on success
    summary = _build_summary(work_data, skill_data, education_data, author_data, answers)

    # Use temp directory alongside the masters to avoid partial writes
    tmp_work = work_path.with_suffix(".yml.tmp")
    tmp_skill = skill_path.with_suffix(".yml.tmp")
    tmp_education = education_path.with_suffix(".yml.tmp")
    tmp_author = author_path.with_suffix(".yml.tmp")

    last_errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        logger.info("merge: lint attempt %d/%d", attempt, max_attempts)

        # Write to temp files first
        _write_yaml(tmp_work, work_content)
        _write_yaml(tmp_skill, skill_content)
        _write_yaml(tmp_education, education_content)
        _write_yaml(tmp_author, author_content)

        # Lint the tmp files using a custom path set.
        from jobsmith.lint import MasterPathSet, validate_masters_from_paths

        tmp_paths = MasterPathSet(
            work_yml=tmp_work,
            skill_yml=tmp_skill,
            education_yml=tmp_education,
            author_yml=tmp_author,
        )

        if _custom_lint_fn is not None:
            # Injected function (tests): called with tmp MasterPathSet so it
            # receives the same input as the default path-based validator.
            lint_result = _custom_lint_fn(tmp_paths)
        else:
            lint_result = validate_masters_from_paths(tmp_paths)

        if lint_result.ok:
            # Atomically commit the writes
            tmp_work.replace(work_path)
            tmp_skill.replace(skill_path)
            tmp_education.replace(education_path)
            tmp_author.replace(author_path)
            logger.info("merge: lint passed on attempt %d", attempt)
            return MergeResult(ok=True, lint_errors=[], summary=summary)

        last_errors = lint_result.errors
        logger.warning(
            "merge: lint failed on attempt %d — %d errors",
            attempt,
            len(last_errors),
        )
        # On subsequent attempts there is no automated fix; we just stop.
        break

    # Clean up temp files without persisting
    for tmp in (tmp_work, tmp_skill, tmp_education, tmp_author):
        tmp.unlink(missing_ok=True)

    logger.error("merge: stopping after %d attempt(s) — lint errors remain", max_attempts)
    return MergeResult(ok=False, lint_errors=last_errors, summary=summary)
