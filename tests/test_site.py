"""Tests for jobsmith.site — privacy model and sanitize_variables."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsmith.site import SENSITIVE_KEYS, sanitize_variables

# ---------- test data ----------

SENSITIVE_SAMPLE: dict = {
    # sensitive keys
    "salary_range": "$120k–$150k",
    "salary": "$135k",
    "fit_score": 87,
    "must_have_table": [{"requirement": "Python 5yr", "met": True}],
    "bullet_decisions": [{"bullet_id": "abc123", "action": "keep"}],
    "bullet_diff": "- old bullet\n+ new bullet",
    "gap_resolutions": {"gap1": "covered by project X"},
    "hm_name": "Jane Doe",
    "hm_email": "jane@company.com",
    "hm_signals": "Uses OSS heavily",
    "outreach_snippets": {"linkedin": "Hi Jane..."},
    "humanizer_audit": {"ai_tell_score": 0.12},
    # public-safe keys
    "company": "Acme Corp",
    "position": "Senior Engineer",
    "slug": "acme-senior-engineer",
    "status": "applied",
    "date_found": "2026-01-15",
    "date_applied": "2026-01-17",
}

PUBLIC_SAFE_KEYS = {"company", "position", "slug", "status", "date_found", "date_applied"}


# ---------- sanitize_variables ----------


def test_sanitize_strips_sensitive_keys_in_public_mode() -> None:
    result = sanitize_variables(SENSITIVE_SAMPLE, mode="public")
    # All public-safe keys must be present
    for key in PUBLIC_SAFE_KEYS:
        assert key in result, f"Public-safe key '{key}' was unexpectedly stripped"
    # All sensitive keys must be gone
    for key in SENSITIVE_KEYS:
        if key in SENSITIVE_SAMPLE:
            assert key not in result, f"Sensitive key '{key}' was not stripped in public mode"


def test_sanitize_keeps_everything_in_private_mode() -> None:
    result = sanitize_variables(SENSITIVE_SAMPLE, mode="private")
    # Private mode is identity — all original keys must be present
    for key in SENSITIVE_SAMPLE:
        assert key in result, f"Key '{key}' was stripped in private mode (should be identity)"
    assert result == SENSITIVE_SAMPLE


def test_sanitize_handles_missing_keys() -> None:
    """Partial dict — only some sensitive keys present — must not raise KeyError."""
    sparse = {
        "company": "Startup Inc",
        "fit_score": 75,  # one sensitive key
        # salary_range, hm_name, etc. absent
    }
    result = sanitize_variables(sparse, mode="public")
    assert "company" in result
    assert "fit_score" not in result


def test_sanitize_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="mode"):
        sanitize_variables(SENSITIVE_SAMPLE, mode="whatever")


def test_sanitize_does_not_mutate_input() -> None:
    original = dict(SENSITIVE_SAMPLE)
    sanitize_variables(SENSITIVE_SAMPLE, mode="public")
    assert original == SENSITIVE_SAMPLE


# ---------- SENSITIVE_KEYS constant ----------


def test_sensitive_keys_constant_documented() -> None:
    """SENSITIVE_KEYS must be a non-empty frozenset containing documented members."""
    assert isinstance(SENSITIVE_KEYS, frozenset)
    assert len(SENSITIVE_KEYS) > 0
    required = {
        "salary_range",
        "salary",
        "fit_score",
        "must_have_table",
        "hm_name",
        "hm_email",
        "hm_signals",
        "outreach_snippets",
        "humanizer_audit",
    }
    for key in required:
        assert key in SENSITIVE_KEYS, f"Expected '{key}' in SENSITIVE_KEYS"


# ---------- render_site output dir resolution ----------


def test_render_site_resolves_private_output_dir(tmp_path: Path) -> None:
    """Private mode should resolve to <root>/_site/."""
    from jobsmith.site import render_site

    try:
        result = render_site(tmp_path, mode="private")
    except NotImplementedError:
        # quarto not available — but the function should have resolved the path
        # before raising; catch and inspect via the exception message
        pytest.skip("quarto not on PATH — output-dir resolution not verifiable without quarto")
    assert result == tmp_path / "_site"


def test_render_site_resolves_public_output_dir(tmp_path: Path) -> None:
    """Public mode should resolve to <root>/_site-public/."""
    from jobsmith.site import render_site

    try:
        result = render_site(tmp_path, mode="public")
    except NotImplementedError:
        pytest.skip("quarto not on PATH — output-dir resolution not verifiable without quarto")
    assert result == tmp_path / "_site-public"


def test_render_site_respects_explicit_output_dir(tmp_path: Path) -> None:
    """When output_dir is provided explicitly, it overrides the default resolution."""
    from jobsmith.site import render_site

    custom = tmp_path / "my-output"
    try:
        result = render_site(tmp_path, mode="private", output_dir=custom)
    except NotImplementedError:
        pytest.skip("quarto not on PATH")
    assert result == custom


def test_render_site_unknown_mode_raises() -> None:
    from jobsmith.site import render_site

    with pytest.raises(ValueError, match="mode"):
        render_site(Path("/tmp"), mode="typo")
