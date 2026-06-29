"""Master-data assembly for the GATHER specialists (feat-d46dde68, slice 5).

This is the hardest surface in the slice: it loads the master data EACH gather
node DECLARES (per specialist-contracts.yaml) and injects it into that node's
prompt context. Reads are READ-ONLY — master YAML is the single source of truth
and is never mutated here (master-first rule).

Invariant (no-silent-empty-prompt): a missing REQUIRED master file raises
:class:`MissingMasterDataError` naming the section and the resolved path. A node
NEVER reaches the model with an empty prompt where master evidence should be.

Declared requirements (from the frozen contract):
  * jd-parse     — none (works from JD url/text only)
  * fit-score    — profile + work + skill + education + author (+ publication?)
  * bullet-select — work + skill

Bullet identity reuses :func:`jobsmith.guard.parse_master_bullets`, so a bullet's
``master_bullet_id`` (sha1[:12] of its text) is IDENTICAL to the anchor guard's —
selection/decision artifacts stay cross-referenceable with the existing CLI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobsmith.config import JobsmithConfig
from jobsmith.guard import Bullet, parse_master_bullets
from jobsmith.paths import resolve

# ---------------------------------------------------------------------------
# Errors + declared requirements
# ---------------------------------------------------------------------------


class MissingMasterDataError(RuntimeError):
    """A REQUIRED master file for a gather node is absent.

    Carries the section name and the resolved path so the operator knows
    exactly which file to create — never a silent empty prompt.
    """


@dataclass(frozen=True)
class MasterRequirement:
    """Which master sections a node declares, and which are mandatory."""

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    @property
    def sections(self) -> tuple[str, ...]:
        return self.required + self.optional


# Section -> the ``JobsmithConfig`` attribute path holding its file path.
_SECTION_PATHS: dict[str, tuple[str, str]] = {
    "profile": ("fit_scorer", "profile_yaml"),
    "work": ("master", "work_yml"),
    "skill": ("master", "skill_yml"),
    "education": ("master", "education_yml"),
    "author": ("master", "author_yml"),
    "publication": ("master", "publication_yml"),
}

# Keyed by gather node name (see nodes_gather.NODE_*).
NODE_MASTER_REQUIREMENTS: dict[str, MasterRequirement] = {
    "jd-parse": MasterRequirement(),
    "fit-score": MasterRequirement(
        required=("profile", "work", "skill", "education", "author"),
        optional=("publication",),
    ),
    "bullet-select": MasterRequirement(required=("work", "skill")),
}

# The full set any pipeline run needs (fit-score is the superset).
_FULL_REQUIREMENT = NODE_MASTER_REQUIREMENTS["fit-score"]


# ---------------------------------------------------------------------------
# Loaded bundle
# ---------------------------------------------------------------------------


@dataclass
class MasterData:
    """A read-only bundle of the master sections a node declared.

    Sections a node did not declare stay at their empty defaults. ``work_path``
    + ``work_bullets`` are populated whenever ``work`` is loaded so anchor logic
    (which is path/text based) can run without re-reading the file.
    """

    profile: dict[str, Any] = field(default_factory=dict)
    work: list[Any] = field(default_factory=list)
    skill: list[Any] = field(default_factory=list)
    education: list[Any] = field(default_factory=list)
    author: Any = None
    publication: list[Any] | None = None
    work_path: Path | None = None
    work_bullets: list[Bullet] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _resolve_section_path(section: str, config: JobsmithConfig, repo_root: Path) -> Path | None:
    """Resolve a section's configured file path against the repo root."""
    group, attr = _SECTION_PATHS[section]
    raw = getattr(getattr(config, group), attr)
    if raw is None:
        return None
    return resolve(Path(raw), repo_root)


def _read_yaml(path: Path, section: str) -> Any:
    """Read+parse a master YAML file (read-only). Raises on a missing file."""
    if not path.exists():
        raise MissingMasterDataError(
            f"Required master file for '{section}' not found: {path}. "
            f"Create it (or fix config.{'.'.join(_SECTION_PATHS[section])}) before running apply."
        )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _assign_section(master: MasterData, section: str, data: Any, path: Path) -> None:
    """Place a parsed section onto the bundle, normalising shape per section."""
    if section == "profile":
        master.profile = data or {}
    elif section == "work":
        master.work = data or []
        master.work_path = path
        master.work_bullets = parse_master_bullets(path)
    elif section == "skill":
        master.skill = data or []
    elif section == "education":
        master.education = data or []
    elif section == "author":
        master.author = data
    elif section == "publication":
        master.publication = data or []


def _load(config: JobsmithConfig, requirement: MasterRequirement, repo_root: Path) -> MasterData:
    """Load the sections in ``requirement``; raise on a missing REQUIRED file."""
    master = MasterData()
    for section in requirement.sections:
        path = _resolve_section_path(section, config, repo_root)
        is_required = section in requirement.required
        if path is None:
            if is_required:
                raise MissingMasterDataError(
                    f"Required master section '{section}' has no configured path "
                    f"(config.{'.'.join(_SECTION_PATHS[section])} is unset)."
                )
            continue
        if not path.exists():
            if is_required:
                _read_yaml(path, section)  # raises MissingMasterDataError
            continue
        _assign_section(master, section, _read_yaml(path, section), path)
    return master


def load_node_master(node_name: str, config: JobsmithConfig, *, repo_root: Path) -> MasterData:
    """Load exactly the master data ``node_name`` declares (and no more)."""
    requirement = NODE_MASTER_REQUIREMENTS.get(node_name)
    if requirement is None:
        raise KeyError(f"no master requirement declared for node {node_name!r}")
    return _load(config, requirement, repo_root)


def load_master_data(config: JobsmithConfig, *, repo_root: Path) -> MasterData:
    """Load the full master set a gather pipeline run needs (fit-score superset)."""
    return _load(config, _FULL_REQUIREMENT, repo_root)


# ---------------------------------------------------------------------------
# Prompt assembly — inject master + JD/upstream context (never an empty prompt)
# ---------------------------------------------------------------------------


def _render_bullets(bullets: list[Bullet]) -> str:
    """Render master work bullets with their stable ids + anchor markers."""
    lines: list[str] = []
    for b in bullets:
        mark = " [ANCHOR]" if b.is_anchor else ""
        lines.append(f"- ({b.bullet_id}) {b.company} / {b.position_title}{mark}: {b.text}")
    return "\n".join(lines) if lines else "[no work bullets]"


def _render_section(label: str, data: Any) -> str:
    if not data:
        return f"## {label}\n[none]"
    return f"## {label}\n{yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()}"


def build_jd_parse_prompt(*, jd_text: str | None, jd_url: str | None, explicit_company: str | None) -> str:
    """Prompt for jd-parse — JD url/text only, no master data."""
    parts = ["Extract structured fields from this job posting."]
    if explicit_company:
        parts.append(f"Company override: {explicit_company}")
    if jd_url:
        parts.append(f"Job URL: {jd_url}")
    parts.append("Job description text:\n" + (jd_text or "[no JD text provided]"))
    parts.append("Return at least two concrete must_haves taken from the JD text.")
    return "\n\n".join(parts)


def build_fit_score_prompt(
    master: MasterData, jd_parsed: dict[str, Any], *, fast_path_scores: dict | None = None
) -> str:
    """Prompt for fit-score — JD fields + master evidence + optional fast scores."""
    parts = [
        "Score this candidate's fit for the role on a 0-100 scale (score_raw).",
        "Judge must_have coverage ONLY from the master evidence below — never infer "
        "from general knowledge. Mark a requirement GAP or BLOCKER when no master "
        "evidence covers it.",
        "## Job\n" + json.dumps(jd_parsed, ensure_ascii=False, indent=2),
        "## Master work evidence\n" + _render_bullets(master.work_bullets),
        _render_section("Master skills", master.skill),
        _render_section("Profile", master.profile),
    ]
    if fast_path_scores:
        parts.append("## Fast-path prior\n" + json.dumps(fast_path_scores, ensure_ascii=False))
    return "\n\n".join(parts)


def build_bullet_select_prompt(
    master: MasterData, jd_parsed: dict[str, Any], fit_score: dict[str, Any] | None
) -> str:
    """Prompt for bullet-select — JD + fit + master work/skill bullet inventory."""
    parts = [
        "Select and reorder the candidate's master work bullets to match the JD.",
        "Reference bullets ONLY by their (id). NEVER fabricate: select, reorder, and "
        "JD-keyword-phrase existing bullets. Preserve [ANCHOR] bullets unless you log "
        "a reason_if_dropped. List any JD must-have you cannot map to a master bullet "
        "in uncovered_must_haves.",
        "## Job\n" + json.dumps(jd_parsed, ensure_ascii=False, indent=2),
    ]
    if fit_score:
        table = fit_score.get("must_have_table", [])
        parts.append("## Fit must-have table\n" + json.dumps(table, ensure_ascii=False))
    parts.append("## Master work bullets\n" + _render_bullets(master.work_bullets))
    parts.append(_render_section("Master skills", master.skill))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# DRAFT context (slice 6) — voice-guide loader + prose-write prompt assembly
# ---------------------------------------------------------------------------


def load_voice_guide(config: JobsmithConfig, *, repo_root: Path) -> str:
    """Return the voice-guide text for the prose writer (``""`` when absent).

    The voice guide is ADVISORY style context (``config.voice.voice_guide_path``),
    not master evidence, so a missing/unset file is non-fatal (unlike master data):
    the no-fabrication guarantee comes from the writer's halt, not this guide.
    """
    raw = config.voice.voice_guide_path
    if raw is None:
        return ""
    path = resolve(Path(raw), repo_root)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _render_findings(prior_findings: list[dict] | None) -> str:
    """Render prior prose-qa blocking findings as fix constraints (revise pass)."""
    if not prior_findings:
        return ""
    items = "\n".join(f"- {f.get('category', '')}: {f.get('span', '')}" for f in prior_findings)
    return (
        "## Fix these blocking style violations from the previous draft\n"
        "Rewrite ONLY to clear them; never add a fact to do so:\n" + items
    )


def build_prose_write_prompt(
    master: MasterData,
    jd_parsed: dict[str, Any],
    fit_score: dict[str, Any] | None,
    bullet_selection: dict[str, Any] | None,
    *,
    voice_guide: str = "",
    prior_findings: list[dict] | None = None,
) -> str:
    """Prompt for prose-write — master facts + voice guide + JD/fit/selection.

    The writer may only REMOVE/RESTRUCTURE master facts into a Professional
    Summary + tailored bullets; it MUST signal ``would_fabricate`` (never write)
    when a JD requirement has no master coverage.
    """
    must_haves = (jd_parsed or {}).get("must_haves", [])
    parts = [
        "Write a Professional Summary (2-3 sentences) and tailored resume bullets "
        "as Markdown, using ONLY facts present in the master evidence below.",
        "Output the prose-draft.md body in `markdown`. If a JD must-have has no "
        "master coverage, set `would_fabricate` to the offending claim and write nothing.",
        "## Voice guide\n" + (voice_guide or "[none]"),
        "## JD must-haves\n" + json.dumps(must_haves, ensure_ascii=False),
        "## Master work evidence\n" + _render_bullets(master.work_bullets),
        _render_section("Master skills", master.skill),
    ]
    if fit_score:
        parts.append("## Fit must-have table\n" + json.dumps(fit_score.get("must_have_table", []), ensure_ascii=False))
    if bullet_selection:
        parts.append("## Bullet selection\n" + json.dumps(bullet_selection.get("positions", []), ensure_ascii=False))
    findings = _render_findings(prior_findings)
    if findings:
        parts.append(findings)
    return "\n\n".join(parts)


__all__ = [
    "MissingMasterDataError",
    "MasterRequirement",
    "MasterData",
    "NODE_MASTER_REQUIREMENTS",
    "load_node_master",
    "load_master_data",
    "load_voice_guide",
    "build_jd_parse_prompt",
    "build_fit_score_prompt",
    "build_bullet_select_prompt",
    "build_prose_write_prompt",
]
