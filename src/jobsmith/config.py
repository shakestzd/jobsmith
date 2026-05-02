"""Pydantic models for `.apply-config.yaml`.

Loads a user's jobsmith config from `<repo>/.apply-config.yaml`, validates
it, and exposes typed accessors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_FILENAME = ".apply-config.yaml"


class MasterPaths(BaseModel):
    """Paths to the user's source-of-truth content YAMLs."""

    work_yml: Path = Path("assets/content/work.yml")
    skill_yml: Path = Path("assets/content/skill.yml")
    education_yml: Path = Path("assets/content/education.yml")
    author_yml: Path = Path("assets/content/author.yml")
    publication_yml: Path | None = None
    award_yml: Path | None = None


class OutputPaths(BaseModel):
    """Where /apply writes per-application artifacts."""

    applications_dir: Path = Path("private/applications")
    job_search_db: Path = Path("private/job_search.db")


class UserIdentity(BaseModel):
    """Author identity for cover letters and resume contact blocks."""

    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    github: str = ""
    linkedin: str = ""


class VoiceSettings(BaseModel):
    """Controls for prose-writer + cover-letter-writer phrasing.

    Voice precedence chain (high to low):
      1. result_verbs / action_verbs — user overrides; empty list = use seeds
      2. benchmark-derived — extracted from benchmarks.resume_qmd by voice.py
      3. GENERIC seed defaults — grounded in published authority (Harvard FAS,
         MIT CAPD, Yale OCS, The Muse, Resume Worded, novoresume)

    banned_adjectives: tier-1 puffery banned by default (Q6 option-c).
      'innovative' and 'passionate' live here, NOT in banned_buzzwords.

    Feedback (feedback.py) is a SEPARATE PARALLEL pathway — soft lessons read
    by specialists, NOT structured verb lists. Do not merge into VoiceSettings.
    """

    voice_guide_path: Path | None = None
    employment_gap_snippet: str | None = None

    # Verb lists — empty means "use benchmark-derived or GENERIC seeds"
    result_verbs: list[str] = Field(default_factory=list)
    action_verbs: list[str] = Field(default_factory=list)

    # Q6 option-c: tier-1 puffery defaults (hard-ban)
    # NOTE: 'innovative' and 'passionate' live HERE, not in banned_buzzwords
    banned_adjectives: list[str] = Field(
        default_factory=lambda: [
            "innovative",
            "passionate",
            "dynamic",
            "results-driven",
            "self-starter",
        ]
    )

    # AI-tell banned action verbs (extended from original 6)
    banned_action_verbs: list[str] = Field(
        default_factory=lambda: [
            # Original 6 — overly corporate / AI-generated tells
            "Architected",
            "Leveraged",
            "Orchestrated",
            "Spearheaded",
            "Delivered end-to-end",
            "Shipped end-to-end",
            # AI-tell additions — passive or vague constructions
            "Utilized",
            "Responsible for",
            "Worked on",
            "Helped with",
            "Participated in",
            "Handled",
        ]
    )
    banned_buzzwords: list[str] = Field(
        default_factory=lambda: [
            "enterprise",
            "proprietary",
            "comprehensive",
            # NOTE: 'innovative' and 'passionate' moved to banned_adjectives
        ]
    )
    banned_marketer_phrases: list[str] = Field(
        default_factory=lambda: [
            "perfect fit",
            "passionate about",
            "proven track record",
        ]
    )

    @field_validator("banned_adjectives")
    @classmethod
    def _no_overlap_with_buzzwords(cls, v: list[str], info) -> list[str]:
        """Enforce: no token appears in both banned_adjectives and banned_buzzwords."""
        buzzwords = (info.data.get("banned_buzzwords") or [])
        overlap = set(v) & set(buzzwords)
        if overlap:
            raise ValueError(
                f"Tokens in both banned_adjectives and banned_buzzwords: {sorted(overlap)}. "
                "Move them to one list only."
            )
        return v


class AnchorThresholds(BaseModel):
    """What counts as a load-bearing bullet."""

    money_min_usd: int = 10_000_000
    percent_min: float = 50.0
    asset_count_min: int = 100_000


class CoverLetterSettings(BaseModel):
    """Letter-specific tuning."""

    framework: Literal["careerfair-io", "minimal", "none"] = "careerfair-io"
    word_targets: dict[str, int] = Field(
        default_factory=lambda: {
            "senior_strategic": 150,
            "ai_engineer": 150,
            "data_engineer": 150,
            "data_analyst": 130,
            "ic_portal": 120,
            "finance": 150,
            "renewable_energy": 150,
            "general": 130,
        }
    )
    default_salutation: str = "Hello,"


class ResumeSettings(BaseModel):
    """Render-specific tuning."""

    template: Path = Path("templates/resume/resume-template.typ")
    max_pages: int = 1
    layout_iteration_limit: int = 2


class FitScorerSettings(BaseModel):
    """Fit thresholds and tier assignment."""

    fast_threshold: float = 0.70
    profile_yaml: Path = Path("private/capacity/profile.yaml")


class PortfolioSettings(BaseModel):
    """Required signals for ai-engineer / data-engineer / data-analyst roles."""

    blocking_for_role_types: list[str] = Field(
        default_factory=lambda: ["ai-engineer", "data-engineer", "data-analyst"]
    )


class BenchmarkConfig(BaseModel):
    """Paths to the user's personal style-reference files (benchmarks).

    All paths are relative to the repo root (resolved via ``resolve()``).
    When a field is ``None`` the helper ``resolve_benchmark_or_fallback``
    automatically falls back to the generic Pat Doe files shipped inside the
    plugin.  Set ``required=True`` to make missing user benchmarks a hard
    failure instead of a silent fallback.
    """

    resume_pdf: Path | None = None
    resume_qmd: Path | None = None
    cover_letter_md: Path | None = None
    cover_letter_pdf: Path | None = None
    workflow_html: Path | None = None
    required: bool = False


class JobsmithConfig(BaseModel):
    """Full jobsmith configuration loaded from `.apply-config.yaml`."""

    master: MasterPaths = Field(default_factory=MasterPaths)
    output: OutputPaths = Field(default_factory=OutputPaths)
    user: UserIdentity = Field(default_factory=UserIdentity)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    anchor_thresholds: AnchorThresholds = Field(default_factory=AnchorThresholds)
    cover_letter: CoverLetterSettings = Field(default_factory=CoverLetterSettings)
    resume: ResumeSettings = Field(default_factory=ResumeSettings)
    fit_scorer: FitScorerSettings = Field(default_factory=FitScorerSettings)
    portfolio: PortfolioSettings = Field(default_factory=PortfolioSettings)
    benchmarks: BenchmarkConfig = Field(default_factory=BenchmarkConfig)

    @field_validator("anchor_thresholds")
    @classmethod
    def _percent_in_range(cls, v: AnchorThresholds) -> AnchorThresholds:
        if not 0 <= v.percent_min <= 100:
            raise ValueError(f"percent_min must be 0-100, got {v.percent_min}")
        return v


def load_config(path: Path | None = None, search_from: Path | None = None) -> JobsmithConfig:
    """Load `.apply-config.yaml` from disk and validate it.

    If `path` is given, load from that exact file.
    Otherwise walk up from `search_from` (or cwd) looking for
    `.apply-config.yaml`. Returns defaults if no config found.
    """
    if path is None:
        path = find_config(search_from or Path.cwd())
    if path is None or not path.exists():
        return JobsmithConfig()
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return JobsmithConfig.model_validate(data)


def find_config(start: Path) -> Path | None:
    """Walk up from `start` looking for `.apply-config.yaml`.

    Returns the first match or None.
    """
    current = start.resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.exists():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


__all__ = [
    "AnchorThresholds",
    "BenchmarkConfig",
    "CONFIG_FILENAME",
    "CoverLetterSettings",
    "FitScorerSettings",
    "JobsmithConfig",
    "MasterPaths",
    "OutputPaths",
    "PortfolioSettings",
    "ResumeSettings",
    "UserIdentity",
    "VoiceSettings",
    "find_config",
    "load_config",
]
