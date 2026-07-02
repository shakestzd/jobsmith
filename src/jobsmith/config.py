"""Pydantic models for `.apply-config.yaml`.

Loads a user's jobsmith config from `<repo>/.apply-config.yaml`, validates
it, and exposes typed accessors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_FILENAME = ".apply-config.yaml"

# ---------------------------------------------------------------------------
# LLM provider — presets and settings
# ---------------------------------------------------------------------------

# Supported provider enum values (5 after feat-c7fab5c0 added `anthropic`).
LLMProvider = Literal["claude_cli", "antigravity_cli", "codex_cli", "openai_compatible", "anthropic"]

# Preset base_urls so the UI / config can one-click fill base_url.
# These represent the two local inference servers that use openai_compatible.
LLM_PRESETS: dict[str, str] = {
    "mlx": "http://127.0.0.1:8080/v1",
    "ollama": "http://localhost:11434/v1",
}


class MasterPaths(BaseModel):
    """Paths to the user's source-of-truth content YAMLs."""

    work_yml: Path = Path("assets/content/work.yml")
    skill_yml: Path = Path("assets/content/skill.yml")
    education_yml: Path = Path("assets/content/education.yml")
    author_yml: Path = Path("assets/content/author.yml")
    publication_yml: Path | None = None
    award_yml: Path | None = None
    # Slice C: optional projects schema. None = no projects.yml configured.
    # When set, jobsmith.assemble.load_projects() reads + filters this file
    # and the path is injected into the Paths block as master.projects_yml.
    projects_yml: Path | None = None


class OutputPaths(BaseModel):
    """Where /apply writes per-application artifacts."""

    applications_dir: Path = Path("private/applications")
    job_search_db: Path = Path("private/job_search.db")
    # Pipeline state DB — separate from job_search_db (different schema/purpose).
    jobsmith_db: Path = Path("private/jobsmith.db")
    # Per-slug review DBs live here; outside applications_dir to prevent leaking
    # personal review notes when the application directory is shared/exported.
    review_db_dir: Path = Path("private/.review")


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

    @model_validator(mode="after")
    def _no_overlap_between_banned_lists(self):
        """Enforce: no token appears in both banned_adjectives and banned_buzzwords.

        Uses model_validator(mode="after") so both lists are fully resolved
        regardless of declaration order. The previous field_validator on
        banned_adjectives ran before banned_buzzwords was set in the
        validation context — overlap on defaults silently slipped through.
        """
        overlap = set(self.banned_adjectives or []) & set(self.banned_buzzwords or [])
        if overlap:
            raise ValueError(
                f"Tokens in both banned_adjectives and banned_buzzwords: {sorted(overlap)}. "
                "Move them to one list only."
            )
        return self


class AnchorThresholds(BaseModel):
    """What counts as a load-bearing bullet."""

    money_min_usd: int = 10_000_000
    percent_min: float = 50.0
    asset_count_min: int = 100_000


class CoverLetterSettings(BaseModel):
    """Letter-specific tuning."""

    framework: Literal["careerfair-io", "minimal", "none"] = "careerfair-io"
    auto: bool = False
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

    def cover_letter_enabled(self) -> bool:
        """Return True iff cover-letter generation is possible at all.

        ``framework == "none"`` is the only disabled state.  All other
        framework values (``"careerfair-io"``, ``"minimal"``) are enabled.
        Used by the standalone trigger (``jobsmith cover-letter``) as a
        capability gate; whether apply runs generate one *automatically*
        is governed by :meth:`auto_generate`.
        """
        return self.framework != "none"

    def auto_generate(self) -> bool:
        """Return True iff apply runs should generate a cover letter unprompted.

        Opt-in (feat-ebb7a7ee): requires BOTH ``auto: true`` AND a usable
        framework.  The default is False — cover letters are produced only
        via the explicit ``--cover-letter`` flag or the standalone
        ``jobsmith cover-letter <slug>`` trigger.
        """
        return self.auto and self.cover_letter_enabled()


class ResumeSettings(BaseModel):
    """Render-specific tuning."""

    template: Path = Path("templates/resume/resume-template.typ")
    max_pages: int = 1
    layout_iteration_limit: int = 2
    # Slice C: project entries with these kinds are filtered out at load time.
    # Defaults cover the most common "not real work" portfolio entries.
    excluded_project_kinds: list[str] = Field(
        default_factory=lambda: ["portfolio-site", "resume-source", "dotfiles"]
    )
    # Slice C.1 (Q7 option-a): one-slot tiebreaker order between work bullets
    # and project entries. Default is work-first (matches traditional resumes).
    # Portfolio-heavy careers (designers, contractors with no traditional
    # employment) override to ['project', 'work'].
    bullet_type_ordering: list[str] = Field(
        default_factory=lambda: ["work", "project"]
    )


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


class NodeBackendConfig(BaseModel):
    """Per-node LLM backend override for the code-local apply orchestrator.

    Used in ``ApplySettings.node_backend`` (default for all nodes) and
    ``ApplySettings.node_backends`` (per-node override map).

    Validation rules:
      - ``api_key`` is REQUIRED when ``provider == anthropic``.
      - ``base_url`` is REQUIRED when ``provider == openai_compatible``.
    """

    provider: LLMProvider = "claude_cli"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    # Optional overrides for the OpenAI-compatible backend.  ``None`` means
    # "use the backend's built-in default" so existing configs are unaffected.
    max_tokens: int | None = None
    timeout_s: float | None = None
    # When True, the OpenAI-compatible backend sends
    # ``{"chat_template_kwargs": {"enable_thinking": false}}`` in the request
    # body so mlx_lm's constrained JSON decoding applies without a thinking
    # preamble in the response that would confuse the JSON parser.
    disable_thinking: bool = False

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> NodeBackendConfig:
        if self.provider == "anthropic" and not self.api_key:
            raise ValueError(
                "api_key is required when provider is 'anthropic'. "
                "Set api_key to your Anthropic API key."
            )
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError(
                "base_url is required when provider is 'openai_compatible'. "
                "Use LLM_PRESETS['mlx'] or LLM_PRESETS['ollama'] as a starting point."
            )
        return self


# Apply orchestrator modes.
ApplyOrchestrator = Literal["claude_cloud", "code_local"]

# Behaviour when a node fails during code-local orchestration.
ApplyOnFailure = Literal["error", "fallback_cloud"]


class ApplySettings(BaseModel):
    """Orchestrator selection + per-node backend routing for the apply pipeline.

    Default behaviour (``orchestrator == claude_cloud``) is byte-identical to
    the existing ``claude -p`` path — no regression when this block is absent.

    When ``orchestrator == code_local`` the Python DAG runner executes the
    apply pipeline locally, routing each node to its configured backend.

    Node backend resolution (highest priority first):
      1. ``node_backends[node_name]`` — explicit per-node override.
      2. ``node_backend`` — default backend for all nodes.
      3. Falls back to the parent ``LLMSettings`` provider when neither is set.
    """

    orchestrator: ApplyOrchestrator = "claude_cloud"
    node_backend: NodeBackendConfig | None = None
    node_backends: dict[str, NodeBackendConfig] = Field(default_factory=dict)
    on_failure: ApplyOnFailure = "error"


class LLMSettings(BaseModel):
    """LLM backend configuration for chat and scoring pipelines.

    The default provider is ``claude_cli`` for strict backward compatibility —
    existing configs without an ``llm`` block continue to work unchanged.

    Validation rules:
      - ``base_url`` is REQUIRED when ``provider == openai_compatible``.
      - ``api_key`` is REQUIRED when ``provider == anthropic``.

    The ``apply`` sub-block controls orchestrator selection and per-node backend
    routing for the apply pipeline (added in feat-c7fab5c0).
    """

    provider: LLMProvider = "claude_cli"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    budget_usd: float = 1.0
    timeout_s: int = 300
    apply: ApplySettings = Field(default_factory=ApplySettings)

    @model_validator(mode="after")
    def _validate_provider_requirements(self) -> LLMSettings:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError(
                "base_url is required when provider is 'openai_compatible'. "
                "Use LLM_PRESETS['mlx'] or LLM_PRESETS['ollama'] as a starting point."
            )
        if self.provider == "anthropic" and not self.api_key:
            raise ValueError(
                "api_key is required when provider is 'anthropic'. "
                "Set api_key to your Anthropic API key."
            )
        return self


class ReuseSettings(BaseModel):
    """All reuse-layer knobs in one place.

    Every threshold that controls caching, deduplication, or warm-start
    behaviour lives here so a reviewer sees every dial at a glance.

    Attributes
    ----------
    fuzzy_cutoff:
        Minimum similarity ratio (0.0-1.0) for fuzzy string matching.
        Used by the canonicalization/match slice (slice 2) when deciding
        whether two requirement strings are the same.  Default 0.85.

    jd_overlap_warm_start_threshold:
        Minimum JD token-overlap ratio (0.0-1.0) required to reuse a
        prior planner output as a warm-start for a new application.
        Used by the warm-start slice (slice 7).  Default 0.80.

    dedup_threshold:
        Minimum similarity ratio (0.0-1.0) for near-duplicate JD detection.
        Used by the JD dedup slice (slice 5).  Default 0.90.

    company_ttl_days:
        Maximum age (in whole days) before company-research results are
        considered stale and must be refreshed.  Company research reuses
        the existing file cache (slice 3), and this TTL governs when that
        cache is considered fresh.  Default 30 days.

    regen_retry_bound:
        Maximum number of regeneration attempts before the re-gate
        backstop (slice 8) gives up and marks the section as unresolvable.
        Must be >= 1.  Default 3.
    """

    fuzzy_cutoff: float = 0.85
    jd_overlap_warm_start_threshold: float = 0.80
    dedup_threshold: float = 0.90
    company_ttl_days: int = 30
    regen_retry_bound: int = 3


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
    reuse: ReuseSettings = Field(default_factory=ReuseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)

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
    "ApplyOnFailure",
    "ApplyOrchestrator",
    "ApplySettings",
    "BenchmarkConfig",
    "CONFIG_FILENAME",
    "CoverLetterSettings",
    "FitScorerSettings",
    "JobsmithConfig",
    "LLM_PRESETS",
    "LLMProvider",
    "LLMSettings",
    "MasterPaths",
    "NodeBackendConfig",
    "OutputPaths",
    "PortfolioSettings",
    "ResumeSettings",
    "ReuseSettings",
    "UserIdentity",
    "VoiceSettings",
    "find_config",
    "load_config",
]
