"""Tests for KIND_MODELS completeness and Pydantic model validation.

Asserts every artifact kind that needs a DB home is registered in
``KIND_MODELS``, and that each registered model can validate a minimal
sample dict.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from jobsmith.db_models import KIND_MODELS, TextArtifact

# ---------------------------------------------------------------------------
# Existing kinds — must still be registered
# ---------------------------------------------------------------------------

EXISTING_KINDS = [
    "jd-parsed",
    "fit-score",
    "bullet-selection",
    "hm-snippet",
    "prose-draft",
    "ai-tell-report",
    "ats-check",
    "company-research",
    "outreach-snippets",
]


@pytest.mark.parametrize("kind", EXISTING_KINDS)
def test_existing_kind_registered(kind: str) -> None:
    """All pre-existing kinds remain in KIND_MODELS."""
    assert kind in KIND_MODELS, f"kind={kind!r} missing from KIND_MODELS"


# ---------------------------------------------------------------------------
# New kinds — must be registered after implementation
# ---------------------------------------------------------------------------

NEW_KINDS = [
    "cover-letter-draft",
    "quarto-config",
    "variables",
    "manifest",
    "anchor-check",
    "fact-check",
]


@pytest.mark.parametrize("kind", NEW_KINDS)
def test_new_kind_registered(kind: str) -> None:
    """Newly-required kinds must be registered in KIND_MODELS."""
    assert kind in KIND_MODELS, f"kind={kind!r} missing from KIND_MODELS"


# ---------------------------------------------------------------------------
# All registered models can validate a minimal sample dict
# ---------------------------------------------------------------------------

MINIMAL_SAMPLES: dict[str, dict] = {
    "jd-parsed": {"company": "Acme", "position": "Engineer"},
    "fit-score": {"score": 0.8, "rationale": "good match"},
    "bullet-selection": {},
    "hm-snippet": {"detected": False},
    "prose-draft": {"text": "Dear Hiring Manager"},
    "ai-tell-report": {},
    "ats-check": {"score": 90.0},
    "company-research": {"text": "Acme Corp was founded in…"},
    "outreach-snippets": {"text": "LinkedIn message draft"},
    "cover-letter-draft": {"text": "Dear Hiring Manager,\n\nI am excited…"},
    "quarto-config": {"content": "project:\n  type: default\n"},
    "variables": {"slug": "acme-engineer", "company": "Acme"},
    "manifest": {
        "run_id": "abc-123",
        "slug": "acme-engineer",
        "started_at": "2024-01-01T10:00:00",
    },
    "anchor-check": {"exit_code": 0},
    "fact-check": {"passed": True},
}


@pytest.mark.parametrize("kind", list(MINIMAL_SAMPLES.keys()))
def test_model_validates_minimal_sample(kind: str) -> None:
    """Each registered model must validate a minimal sample dict without error."""
    if kind not in KIND_MODELS:
        pytest.skip(f"kind={kind!r} not yet registered — test will fail until implemented")
    model_cls = KIND_MODELS[kind]
    sample = MINIMAL_SAMPLES[kind]
    instance = model_cls.model_validate(sample)
    assert isinstance(instance, BaseModel)


# ---------------------------------------------------------------------------
# All registered kinds have a non-None model class
# ---------------------------------------------------------------------------

def test_all_registered_kinds_have_model_class() -> None:
    """Every entry in KIND_MODELS must map to a concrete Pydantic BaseModel subclass."""
    for kind, model_cls in KIND_MODELS.items():
        assert model_cls is not None, f"kind={kind!r} has None model"
        assert issubclass(model_cls, BaseModel), (
            f"kind={kind!r} maps to {model_cls!r} which is not a BaseModel subclass"
        )


# ---------------------------------------------------------------------------
# TextArtifact is the generic fallback — confirm it still validates
# ---------------------------------------------------------------------------

def test_text_artifact_validates_empty() -> None:
    """TextArtifact must accept an empty dict (used as fallback in deserialize_output)."""
    ta = TextArtifact.model_validate({})
    assert ta.text is None


def test_text_artifact_validates_with_text() -> None:
    ta = TextArtifact.model_validate({"text": "hello"})
    assert ta.text == "hello"
