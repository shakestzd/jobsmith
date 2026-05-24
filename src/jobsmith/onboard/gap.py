"""jobsmith.onboard.gap — gap-interview for the onboarding pipeline (feat-01cad829).

Diff candidate-*.json content against required master schema fields and
produce a structured set of questions for missing / low-confidence data.

The question structure is designed to be renderable by:
  - CLI: interactive terminal prompts
  - API/slice-6: SSE-streamed questions rendered in the browser

Public entry points
-------------------
build_gap_questions(state_dir) -> list[GapQuestion]
    Inspect all candidate-*.json files under state_dir and return a list of
    structured GapQuestion objects.  Does NOT perform I/O to the user.

run_gap_interview_cli(state_dir, *, input_fn) -> dict[str, str]
    CLI path: iterate questions, prompt via input_fn, return answers dict.

GapQuestion
-----------
    section:   str   — "work" | "skill" | "education" | "author"
    field:     str   — dot-path key that is missing/low-confidence
    prompt:    str   — human-readable question
    required:  bool  — whether the field is hard-required
    hint:      str   — example / format hint for the user
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------


@dataclass
class GapQuestion:
    """A single gap-interview question targeting one missing/low-confidence field."""

    section: str
    field: str
    prompt: str
    required: bool = True
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "section": self.section,
            "field": self.field,
            "prompt": self.prompt,
            "required": self.required,
            "hint": self.hint,
        }


# ---------------------------------------------------------------------------
# Required-field manifests per section
# ---------------------------------------------------------------------------

# Each entry: (field_path, prompt, required, hint)
_WORK_REQUIRED: list[tuple[str, str, bool, str]] = [
    (
        "entries",
        "Please describe your work history (company, title, dates, bullets).",
        True,
        "e.g. Acme Corp, Software Engineer, 2020-2023",
    ),
]

_SKILL_REQUIRED: list[tuple[str, str, bool, str]] = [
    (
        "skills",
        "What are your key skills? (programming languages, tools, frameworks)",
        True,
        "e.g. Python, Go, Kubernetes",
    ),
]

_EDUCATION_REQUIRED: list[tuple[str, str, bool, str]] = [
    (
        "entries",
        "Where did you study? (institution, degree, field, graduation year)",
        False,
        "e.g. MIT, BS Computer Science, 2020",
    ),
]

_AUTHOR_REQUIRED: list[tuple[str, str, bool, str]] = [
    ("name", "What is your full name?", True, "e.g. Jane Smith"),
    ("email", "What is your contact email address?", True, "e.g. jane@example.com"),
    ("phone", "What is your phone number?", False, "e.g. +1 555-555-5555"),
    ("location", "What is your location / city?", False, "e.g. San Francisco, CA"),
    ("github", "What is your GitHub username or URL?", False, "e.g. github.com/janesmith"),
    ("linkedin", "What is your LinkedIn URL?", False, "e.g. linkedin.com/in/janesmith"),
]


# ---------------------------------------------------------------------------
# Helpers to load candidate files
# ---------------------------------------------------------------------------


def _load_candidate(state_dir: Path, section: str) -> dict | None:
    """Load candidate-<section>.json from state_dir; return None if absent."""
    path = state_dir / f"candidate-{section}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("gap: could not load %s — %s", path, exc)
        return None


def _has_non_empty(data: dict, *keys: str) -> bool:
    """Return True iff the nested key path leads to a non-empty value."""
    node: object = data
    for key in keys:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    if node is None:
        return False
    if isinstance(node, (list, dict, str)):
        return bool(node)
    return True


# ---------------------------------------------------------------------------
# Section gap detectors
# ---------------------------------------------------------------------------


def _work_gaps(state_dir: Path) -> list[GapQuestion]:
    raw = _load_candidate(state_dir, "work")
    questions: list[GapQuestion] = []
    if raw is None:
        # No candidate file at all — ask everything
        for f, prompt, req, hint in _WORK_REQUIRED:
            questions.append(GapQuestion("work", f, prompt, req, hint))
        return questions

    data = raw.get("data", raw)
    entries = data.get("entries", [])
    if not entries:
        for f, prompt, req, hint in _WORK_REQUIRED:
            questions.append(GapQuestion("work", f, prompt, req, hint))
    return questions


def _skill_gaps(state_dir: Path) -> list[GapQuestion]:
    raw = _load_candidate(state_dir, "skill")
    questions: list[GapQuestion] = []
    if raw is None:
        for f, prompt, req, hint in _SKILL_REQUIRED:
            questions.append(GapQuestion("skill", f, prompt, req, hint))
        return questions

    data = raw.get("data", raw)
    skills = data.get("skills", [])
    if not skills:
        for f, prompt, req, hint in _SKILL_REQUIRED:
            questions.append(GapQuestion("skill", f, prompt, req, hint))
    return questions


def _education_gaps(state_dir: Path) -> list[GapQuestion]:
    raw = _load_candidate(state_dir, "education")
    questions: list[GapQuestion] = []
    if raw is None:
        for f, prompt, req, hint in _EDUCATION_REQUIRED:
            questions.append(GapQuestion("education", f, prompt, req, hint))
        return questions

    data = raw.get("data", raw)
    entries = data.get("entries", [])
    if not entries:
        for f, prompt, req, hint in _EDUCATION_REQUIRED:
            questions.append(GapQuestion("education", f, prompt, req, hint))
    return questions


def _author_gaps(state_dir: Path) -> list[GapQuestion]:
    raw = _load_candidate(state_dir, "author")
    questions: list[GapQuestion] = []
    if raw is None:
        for f, prompt, req, hint in _AUTHOR_REQUIRED:
            questions.append(GapQuestion("author", f, prompt, req, hint))
        return questions

    data = raw.get("data", raw)
    for f, prompt, req, hint in _AUTHOR_REQUIRED:
        # f is a simple key here (no nesting)
        val = data.get(f, "")
        if not val:
            questions.append(GapQuestion("author", f, prompt, req, hint))
    return questions


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build_gap_questions(state_dir: Path) -> list[GapQuestion]:
    """Inspect all candidate-*.json files and return structured gap questions.

    Returns a list of GapQuestion objects.  The list is empty if all required
    fields are present.  Questions are ordered: work → skill → education →
    author, with required=True questions first within each section.
    """
    questions: list[GapQuestion] = []
    questions.extend(_work_gaps(state_dir))
    questions.extend(_skill_gaps(state_dir))
    questions.extend(_education_gaps(state_dir))
    questions.extend(_author_gaps(state_dir))
    return questions


def run_gap_interview_cli(
    state_dir: Path,
    *,
    input_fn=None,
) -> dict[str, str]:
    """CLI path: prompt user for missing fields and return an answers dict.

    Parameters
    ----------
    state_dir:
        Path to the .onboard-state/ directory containing candidate-*.json.
    input_fn:
        Callable(prompt: str) -> str used to solicit user input.  Defaults to
        the built-in ``input()``.  Pass a mock in tests.

    Returns
    -------
    dict[str, str]
        Mapping of "<section>.<field>" → user-supplied answer string.
        Fields already present in candidate-*.json are not included.
    """
    if input_fn is None:
        input_fn = input  # pragma: no cover

    questions = build_gap_questions(state_dir)
    answers: dict[str, str] = {}

    for q in questions:
        hint_str = f" [{q.hint}]" if q.hint else ""
        required_str = " (required)" if q.required else " (optional, press Enter to skip)"
        display = f"\n{q.prompt}{hint_str}{required_str}\n> "
        answer = input_fn(display).strip()
        answers[f"{q.section}.{q.field}"] = answer

    return answers
