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
import tempfile
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


def _format_date_range(start: str, end: str) -> str:
    """Combine candidate start/end dates into a master ``date`` string."""
    start = (start or "").strip()
    end = (end or "").strip()
    if start and end:
        return f"{start} – {end}"
    return start or end or ""


def _candidate_work_to_master(entry: dict) -> dict:
    """Map a candidate work entry to the master WorkEntry schema.

    Candidate: ``{company, title, start_date, end_date, location, bullets}``.
    Master:    ``{title, location(=company), date, description, details}`` —
    note the master ``location`` field holds the *company* name by convention
    and ``description`` holds the work place/mode (e.g. "Remote").
    """
    bullets = entry.get("bullets") or entry.get("details") or []
    if not isinstance(bullets, list):
        bullets = [str(bullets)]
    return {
        "title": str(entry.get("title", "")),
        "location": str(entry.get("company", entry.get("location", ""))),
        "date": _format_date_range(entry.get("start_date", ""), entry.get("end_date", "")),
        "description": str(entry.get("location", "")) if entry.get("company") else "",
        "details": [b for b in bullets if b],
    }


def _build_work_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build work.yml: a master-schema list of position dicts."""
    if data:
        entries = data.get("entries", [])
        if entries:
            return [_candidate_work_to_master(e) for e in entries if isinstance(e, dict)]
    # Answers can provide raw YAML if the user typed it
    raw = answers.get("work.entries", "")
    if raw:
        try:
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, list):
                return [
                    _candidate_work_to_master(e) if isinstance(e, dict) else {"title": str(e), "details": []}
                    for e in parsed
                ]
        except yaml.YAMLError:
            pass
        return [{"title": raw, "location": "", "date": "", "description": "", "details": []}]
    return []


def _build_skill_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build skill.yml: a master-schema list of category dicts.

    Candidate skills ``[{name, category}]`` are grouped by category into the
    master ``{title(=category), description(comma string), details(list)}``
    shape (a list, not a ``{skills: ...}`` mapping).
    """
    grouped: dict[str, list[str]] = {}

    if data:
        for item in data.get("skills", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            category = str(item.get("category", "technical")).strip() or "technical"
            grouped.setdefault(category, []).append(name)

    if not grouped:
        raw = answers.get("skill.skills", "")
        if raw:
            names = [s.strip() for s in raw.replace(",", "\n").splitlines() if s.strip()]
            if names:
                grouped["technical"] = names

    return [
        {"title": category, "description": ", ".join(names), "details": names}
        for category, names in grouped.items()
    ]


def _candidate_education_to_master(entry: dict) -> dict:
    """Map a candidate education entry to the master EducationEntry schema.

    Candidate: ``{institution, degree, field, start_date, end_date}``.
    Master:    ``{title(=institution), location, date, description(=degree), details}``.
    """
    degree = str(entry.get("degree", "")).strip()
    field = str(entry.get("field", "")).strip()
    description = ", ".join(p for p in (degree, field) if p)
    return {
        "title": str(entry.get("institution", "")),
        "location": "",
        "date": _format_date_range(entry.get("start_date", ""), entry.get("end_date", "")),
        "description": description,
        "details": [],
    }


def _build_education_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build education.yml: a master-schema list of institution dicts."""
    if data:
        entries = data.get("entries", [])
        if entries:
            return [_candidate_education_to_master(e) for e in entries if isinstance(e, dict)]
    raw = answers.get("education.entries", "")
    if raw:
        return [{"title": raw, "location": "", "date": "", "description": "", "details": []}]
    return []


def _split_name(full: str) -> tuple[str, str]:
    """Split a full name into (firstname, lastname); last token is the surname."""
    parts = [p for p in str(full).split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _build_author_yaml(data: dict | None, answers: dict[str, str]) -> Any:
    """Build author.yml in the master shape: ``{author: [ {firstname, ...} ]}``.

    Candidate author is a flat ``{name, email, phone, location, github,
    linkedin, summary}`` dict; the master file wraps a single author dict in an
    ``author:`` list and uses ``firstname/lastname/address/homepage`` fields.
    """
    def _pick(key: str) -> str:
        val = (data or {}).get(key, "") if data else ""
        if not val:
            val = answers.get(f"author.{key}", "")
        return str(val).strip()

    firstname, lastname = _split_name(_pick("name"))
    homepage = _pick("github") or _pick("linkedin")

    author: dict[str, str] = {
        "firstname": firstname,
        "lastname": lastname,
        "email": _pick("email"),
        "phone": _pick("phone"),
        "address": _pick("location"),
        "homepage": homepage,
    }
    # Drop empty fields so the YAML stays clean.
    author = {k: v for k, v in author.items() if v}
    return {"author": [author]} if author else {"author": []}


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
    """Merge existing + incoming work lists (master shape), dedupe by title+location.

    Master work entries use ``location`` for the company name; dedup on
    ``title|location`` so the same role at the same company isn't duplicated.
    """
    if not isinstance(existing, list):
        return incoming if isinstance(incoming, list) else []
    if not isinstance(incoming, list):
        return existing

    def _key(item: dict) -> str:
        return f"{item.get('title','')}|{item.get('location','')}"

    seen = {_key(item) for item in existing if isinstance(item, dict)}
    result = list(existing)
    for item in incoming:
        if isinstance(item, dict) and _key(item) not in seen:
            seen.add(_key(item))
            result.append(item)
    return result


def _merge_skill(existing: Any, incoming: Any) -> Any:
    """Merge two master-shape skill lists by category title, unioning details."""
    ex = existing if isinstance(existing, list) else []
    inc = incoming if isinstance(incoming, list) else []

    by_title: dict[str, dict] = {}
    order: list[str] = []
    for item in [*ex, *inc]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        details = [d for d in (item.get("details") or []) if d]
        if title not in by_title:
            by_title[title] = {"title": title, "details": list(details)}
            order.append(title)
        else:
            for d in details:
                if d not in by_title[title]["details"]:
                    by_title[title]["details"].append(d)
    # Recompute the comma-joined description from the unioned details.
    return [
        {
            "title": by_title[t]["title"],
            "description": ", ".join(by_title[t]["details"]),
            "details": by_title[t]["details"],
        }
        for t in order
    ]


def _merge_education(existing: Any, incoming: Any) -> Any:
    """Merge two master-shape education lists, dedupe by title (institution)."""
    if not isinstance(existing, list):
        return incoming if isinstance(incoming, list) else []
    if not isinstance(incoming, list):
        return existing

    seen = {item.get("title", "") for item in existing if isinstance(item, dict)}
    result = list(existing)
    for item in incoming:
        if isinstance(item, dict) and item.get("title", "") not in seen:
            seen.add(item.get("title", ""))
            result.append(item)
    return result


def _merge_author(existing: Any, incoming: Any) -> Any:
    """Merge ``{author: [dict]}`` masters — incoming fills only missing fields.

    Operates on the inner author dict (first list item) so the existing
    author's populated fields win and incoming only fills the gaps.
    """
    def _first(blob: Any) -> dict:
        if isinstance(blob, dict):
            authors = blob.get("author")
            if isinstance(authors, list) and authors and isinstance(authors[0], dict):
                return dict(authors[0])
            if isinstance(authors, dict):
                return dict(authors)
        return {}

    ex = _first(existing)
    inc = _first(incoming)
    if not ex:
        return {"author": [inc]} if inc else {"author": []}
    for key, val in inc.items():
        if not ex.get(key):
            ex[key] = val
    return {"author": [ex]}


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

    # Lint-gate loop: stage into a temp DIRECTORY using the REAL master
    # filenames so jobsmith.lint (which dispatches section validators by
    # ``path.name``) actually runs work/skill/education/author validation.
    # A ``*.yml.tmp`` name would bypass section validation entirely.
    summary = _build_summary(work_data, skill_data, education_data, author_data, answers)
    from jobsmith.lint import MasterPathSet, validate_masters_from_paths

    last_errors: list[str] = []

    # Stage in a temp dir on the SAME filesystem as the masters so the final
    # commit is an atomic rename (Path.replace) rather than a cross-device move.
    staging_parent = work_path.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jobsmith-merge-", dir=staging_parent) as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        staged = {
            work_path: tmp_dir / "work.yml",
            skill_path: tmp_dir / "skill.yml",
            education_path: tmp_dir / "education.yml",
            author_path: tmp_dir / "author.yml",
        }

        for attempt in range(1, max_attempts + 1):
            logger.info("merge: lint attempt %d/%d", attempt, max_attempts)

            _write_yaml(staged[work_path], work_content)
            _write_yaml(staged[skill_path], skill_content)
            _write_yaml(staged[education_path], education_content)
            _write_yaml(staged[author_path], author_content)

            tmp_paths = MasterPathSet(
                work_yml=staged[work_path],
                skill_yml=staged[skill_path],
                education_yml=staged[education_path],
                author_yml=staged[author_path],
            )

            if _custom_lint_fn is not None:
                lint_result = _custom_lint_fn(tmp_paths)
            else:
                lint_result = validate_masters_from_paths(tmp_paths)

            if lint_result.ok:
                # Commit: move staged files to their real master paths.
                for real_path, staged_path in staged.items():
                    real_path.parent.mkdir(parents=True, exist_ok=True)
                    staged_path.replace(real_path)
                logger.info("merge: lint passed on attempt %d", attempt)
                return MergeResult(ok=True, lint_errors=[], summary=summary)

            last_errors = lint_result.errors
            logger.warning(
                "merge: lint failed on attempt %d — %d errors", attempt, len(last_errors)
            )
            # No automated fix between attempts; stop on first failure.
            break

    logger.error("merge: stopping after %d attempt(s) — lint errors remain", max_attempts)
    return MergeResult(ok=False, lint_errors=last_errors, summary=summary)
