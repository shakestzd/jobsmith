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


# ---------- init_site ----------

from jobsmith.site import (  # noqa: E402  (intentional grouping)
    DEFAULT_SITE_TEMPLATE_SRC,
    discover_applications,
    init_site,
)


def test_init_site_copies_bundled_templates(tmp_path: Path) -> None:
    """init_site copies _quarto.yml + index.qmd + styles/jobsmith.scss + .gitignore."""
    written = init_site(tmp_path)

    assert (tmp_path / "_quarto.yml").is_file()
    assert (tmp_path / "index.qmd").is_file()
    assert (tmp_path / "styles" / "jobsmith.scss").is_file()
    assert (tmp_path / ".gitignore").is_file()

    # Returns the list of files actually written
    assert len(written) >= 4
    written_names = {p.name for p in written}
    assert {"_quarto.yml", "index.qmd", "jobsmith.scss", ".gitignore"} <= written_names


def test_init_site_creates_root_when_missing(tmp_path: Path) -> None:
    """init_site mkdirs *root* and any nested style directories."""
    target = tmp_path / "fresh-repo"
    assert not target.exists()
    init_site(target)
    assert target.is_dir()
    assert (target / "styles").is_dir()


def test_init_site_does_not_overwrite_by_default(tmp_path: Path) -> None:
    """Existing files are preserved (jobsmith never clobbers user edits)."""
    (tmp_path / "_quarto.yml").write_text("user-edited\n")
    (tmp_path / "index.qmd").write_text("user listings\n")

    written = init_site(tmp_path)

    assert (tmp_path / "_quarto.yml").read_text() == "user-edited\n"
    assert (tmp_path / "index.qmd").read_text() == "user listings\n"
    # Only the not-already-present files were written
    assert all(f.name not in {"_quarto.yml", "index.qmd"} for f in written)


def test_init_site_overwrite_replaces_files(tmp_path: Path) -> None:
    """overwrite=True replaces existing files with bundled copies."""
    (tmp_path / "_quarto.yml").write_text("user-edited\n")

    init_site(tmp_path, overwrite=True)

    # The bundled _quarto.yml is non-trivial — assert against any signature
    # string to confirm the file was replaced.
    refreshed = (tmp_path / "_quarto.yml").read_text()
    assert "user-edited" not in refreshed
    assert "type: website" in refreshed


def test_init_site_unknown_template_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="site template"):
        init_site(tmp_path, template_src=tmp_path / "does-not-exist")


def test_default_site_template_src_exists() -> None:
    """The bundled templates/site/ directory ships with the package."""
    assert DEFAULT_SITE_TEMPLATE_SRC.is_dir()
    assert (DEFAULT_SITE_TEMPLATE_SRC / "_quarto.yml").is_file()
    assert (DEFAULT_SITE_TEMPLATE_SRC / "index.qmd").is_file()


# ---------- discover_applications ----------


def _make_app(root: Path, slug: str, with_index: bool = True, with_state: bool = True) -> Path:
    app = root / "private" / "applications" / slug
    app.mkdir(parents=True)
    if with_state:
        (app / ".apply-state").mkdir()
    if with_index:
        (app / "index.qmd").write_text(f"# {slug}\n")
    return app


def test_discover_applications_finds_assembled_apps(tmp_path: Path) -> None:
    _make_app(tmp_path, "acme-engineer")
    _make_app(tmp_path, "stripe-platform")

    found = discover_applications(tmp_path)
    slugs = [p.name for p in found]
    assert slugs == ["acme-engineer", "stripe-platform"]  # stable alphabetical


def test_discover_applications_skips_apps_without_state(tmp_path: Path) -> None:
    _make_app(tmp_path, "acme-engineer")
    _make_app(tmp_path, "no-state-yet", with_state=False)

    slugs = [p.name for p in discover_applications(tmp_path)]
    assert slugs == ["acme-engineer"]


def test_discover_applications_skips_apps_without_index(tmp_path: Path) -> None:
    _make_app(tmp_path, "acme-engineer")
    _make_app(tmp_path, "draft-only", with_index=False)

    slugs = [p.name for p in discover_applications(tmp_path)]
    assert slugs == ["acme-engineer"]


def test_discover_applications_skips_underscore_and_dot_dirs(tmp_path: Path) -> None:
    _make_app(tmp_path, "acme-engineer")
    _make_app(tmp_path, "_pending")
    _make_app(tmp_path, ".cache")

    slugs = [p.name for p in discover_applications(tmp_path)]
    assert slugs == ["acme-engineer"]


def test_discover_applications_returns_empty_when_root_missing(tmp_path: Path) -> None:
    found = discover_applications(tmp_path / "not-a-repo")
    assert found == []


def test_discover_applications_returns_empty_when_no_applications_dir(tmp_path: Path) -> None:
    """Repo exists but private/applications/ has not been created yet."""
    found = discover_applications(tmp_path)
    assert found == []
