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


def _site_root(tmp_path: Path) -> Path:
    """Scaffold a minimal Quarto site project at *tmp_path* so render_site
    passes its existence check without running quarto."""
    (tmp_path / "_quarto.yml").write_text("project:\n  type: website\n")
    return tmp_path


def test_render_site_missing_quarto_yml_raises(tmp_path: Path) -> None:
    """render_site requires _quarto.yml at root — direct user to site init."""
    from jobsmith.site import render_site

    with pytest.raises(FileNotFoundError, match="_quarto.yml"):
        render_site(tmp_path, mode="private")


def test_render_site_resolves_private_output_dir(tmp_path: Path, monkeypatch) -> None:
    """Private mode resolves to <root>/_site/. Stub quarto to avoid invoking it."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)

    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    captured: dict = {}

    class _StubResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        return _StubResult()

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    result = site_mod.render_site(root, mode="private")
    assert result == root / "_site"
    assert "--output-dir" in captured["cmd"]
    assert str(root / "_site") in captured["cmd"]


def test_render_site_default_profile_private_in_cmd(tmp_path: Path, monkeypatch) -> None:
    """render_site passes --profile private to quarto by default (bug-08a3ad82).

    The private Quarto profile activates _quarto-private.yml which includes
    private/applications/**/*.qmd — without it per-application HTML pages
    are never produced.
    """
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    captured: dict = {}

    class _StubResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        return _StubResult()

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    site_mod.render_site(root, mode="private")

    assert "--profile" in captured["cmd"], "expected --profile in quarto cmd"
    profile_idx = captured["cmd"].index("--profile")
    assert captured["cmd"][profile_idx + 1] == "private"


def test_render_site_custom_profile_in_cmd(tmp_path: Path, monkeypatch) -> None:
    """render_site forwards a custom profile value to the quarto subprocess."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    captured: dict = {}

    class _StubResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        return _StubResult()

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    site_mod.render_site(root, mode="private", profile="staging")

    assert "--profile" in captured["cmd"]
    profile_idx = captured["cmd"].index("--profile")
    assert captured["cmd"][profile_idx + 1] == "staging"


def test_render_site_empty_profile_omits_flag(tmp_path: Path, monkeypatch) -> None:
    """When profile='' is passed, --profile must NOT appear in the quarto cmd."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    captured: dict = {}

    class _StubResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        return _StubResult()

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    site_mod.render_site(root, mode="private", profile="")

    assert "--profile" not in captured["cmd"]


def test_render_site_resolves_public_output_dir(tmp_path: Path, monkeypatch) -> None:
    """Public mode resolves to <root>/_site-public/."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    class _StubResult:
        returncode = 0
        stdout = ""
        stderr = ""

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: _StubResult())

    result = site_mod.render_site(root, mode="public")
    assert result == root / "_site-public"


def test_render_site_respects_explicit_output_dir(tmp_path: Path, monkeypatch) -> None:
    """When output_dir is provided explicitly, it overrides the default resolution."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    class _StubResult:
        returncode = 0
        stdout = ""
        stderr = ""

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: _StubResult())

    custom = tmp_path / "my-output"
    result = site_mod.render_site(root, mode="private", output_dir=custom)
    assert result == custom


def test_render_site_unknown_mode_raises() -> None:
    from jobsmith.site import render_site

    with pytest.raises(ValueError, match="mode"):
        render_site(Path("/tmp"), mode="typo")


def test_render_site_quarto_missing_raises_runtimeerror(tmp_path: Path, monkeypatch) -> None:
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="quarto"):
        site_mod.render_site(root, mode="private")


def test_render_site_quarto_failure_raises_runtimeerror(tmp_path: Path, monkeypatch) -> None:
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")

    class _StubResult:
        returncode = 7
        stdout = "stdout text"
        stderr = "boom"

    import subprocess as subprocess_mod

    monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: _StubResult())

    with pytest.raises(RuntimeError, match="exited 7"):
        site_mod.render_site(root, mode="private")


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


# ---------- nested sanitization (fix-roborev-906) ----------

from jobsmith.site import (  # noqa: E402
    SENSITIVE_BLOCK_FILES,
    SENSITIVE_VARIABLE_PATHS,
    sanitize_blocks_dir,
)

NESTED_SAMPLE: dict = {
    "company": "Acme Corp",
    "position": "Engineer",
    "slug": "acme-engineer",
    "salary_range": "$120k–$150k",
    "fit": {
        "score": 0.78,
        "rationale": "Strong on Python, weak on Spark",
        "must_have_table": [{"requirement": "Python", "level": "STRONG"}],
        "matched_evidence": ["profile.python"],
        "concerns": ["no Spark"],
        "pitch": "good fit",
    },
    "hm": {
        "detected": True,
        "name": "Pat Director",
        "source": "linkedin_post",
        "one_specific_signal": "Authored 2024 paper on X",
        "suggested_hook": "Reference paper directly",
    },
    "hm_md": "## HM dossier\n- Pat Director\n",
    "outreach": "## Connection note\nHi Pat...",
    "humanizer_audit": {"ai_tell_score": 0.12},
    "cover_letter_draft": "Dear Hiring Team,\nReal letter.",
    "bullets": {
        "positions": [{"company": "Prior Co"}],
        "anchor_bullets_master": ["bullet a"],
        "anchor_bullets_kept": ["bullet a"],
        "anchor_bullets_dropped": ["bullet b — internal context"],
    },
}


def test_sanitize_strips_nested_fit_in_public_mode() -> None:
    out = sanitize_variables(NESTED_SAMPLE, mode="public")
    # fit.* must be empty (every nested sensitive subkey gone)
    assert out["fit"] == {}
    # Originals untouched
    assert NESTED_SAMPLE["fit"]["score"] == 0.78


def test_sanitize_strips_nested_hm_in_public_mode() -> None:
    out = sanitize_variables(NESTED_SAMPLE, mode="public")
    assert out["hm"] == {}
    assert "hm_md" not in out


def test_sanitize_strips_outreach_humanizer_cover_letter_draft() -> None:
    out = sanitize_variables(NESTED_SAMPLE, mode="public")
    assert "outreach" not in out
    assert "humanizer_audit" not in out
    assert "cover_letter_draft" not in out


def test_sanitize_strips_dropped_bullets_but_keeps_kept_bullets() -> None:
    """Anchor bullets that were dropped reveal internal selection logic;
    kept bullets are public (they appear in the final résumé)."""
    out = sanitize_variables(NESTED_SAMPLE, mode="public")
    bullets = out["bullets"]
    assert "anchor_bullets_dropped" not in bullets
    assert bullets["anchor_bullets_kept"] == ["bullet a"]
    assert bullets["positions"] == [{"company": "Prior Co"}]


def test_sanitize_keeps_safe_keys_in_public_mode() -> None:
    out = sanitize_variables(NESTED_SAMPLE, mode="public")
    assert out["company"] == "Acme Corp"
    assert out["position"] == "Engineer"
    assert out["slug"] == "acme-engineer"


def test_sanitize_strips_legacy_top_level_salary() -> None:
    out = sanitize_variables(NESTED_SAMPLE, mode="public")
    assert "salary_range" not in out


def test_sanitize_private_mode_returns_deep_copy() -> None:
    out = sanitize_variables(NESTED_SAMPLE, mode="private")
    out["fit"]["score"] = 0.0  # mutate the copy
    assert NESTED_SAMPLE["fit"]["score"] == 0.78  # original intact


def test_sanitize_handles_missing_nested_subkeys() -> None:
    minimal = {"company": "X", "fit": {"score": 0.5}}  # no hm, no outreach
    out = sanitize_variables(minimal, mode="public")
    assert out["company"] == "X"
    assert out["fit"] == {}


def test_sensitive_variable_paths_documented() -> None:
    # Sanity: every required category is represented somewhere in the
    # paths tuple. This guards against silent regressions where a future
    # refactor drops fit.* or hm.*.
    flat_heads = {p[0] for p in SENSITIVE_VARIABLE_PATHS}
    assert "fit" in flat_heads
    assert "hm" in flat_heads
    assert "outreach" in flat_heads
    assert "humanizer_audit" in flat_heads
    assert "salary_range" in flat_heads
    assert "cover_letter_draft" in flat_heads


# ---------- sanitize_blocks_dir ----------


def test_sanitize_blocks_dir_replaces_sensitive_files(tmp_path: Path) -> None:
    blocks = tmp_path / "_blocks"
    blocks.mkdir()
    # Sensitive files
    (blocks / "must-have-table.md").write_text("| evidence column |\n")
    (blocks / "hm-dossier.md").write_text("## HM dossier\nPat Director\n")
    (blocks / "outreach-snippets.md").write_text("LinkedIn note\n")
    # Public-safe files
    (blocks / "must-haves.md").write_text("- Python\n")
    (blocks / "nice-to-haves.md").write_text("- Terraform\n")

    rewritten = sanitize_blocks_dir(blocks, mode="public")

    rewritten_names = {p.name for p in rewritten}
    assert "must-have-table.md" in rewritten_names
    assert "hm-dossier.md" in rewritten_names
    assert "outreach-snippets.md" in rewritten_names
    # Safe files untouched
    assert (blocks / "must-haves.md").read_text() == "- Python\n"
    assert (blocks / "nice-to-haves.md").read_text() == "- Terraform\n"
    # Rewritten content is the redaction notice
    assert "omitted in the public variant" in (blocks / "hm-dossier.md").read_text()


def test_sanitize_blocks_dir_private_mode_is_noop(tmp_path: Path) -> None:
    blocks = tmp_path / "_blocks"
    blocks.mkdir()
    (blocks / "hm-dossier.md").write_text("private content\n")

    rewritten = sanitize_blocks_dir(blocks, mode="private")
    assert rewritten == []
    assert (blocks / "hm-dossier.md").read_text() == "private content\n"


def test_sanitize_blocks_dir_handles_missing_directory(tmp_path: Path) -> None:
    """No-op when the blocks directory doesn't exist (e.g. unassembled app)."""
    rewritten = sanitize_blocks_dir(tmp_path / "nope", mode="public")
    assert rewritten == []


def test_sensitive_block_files_covers_known_artifacts() -> None:
    # Sanity: the block-file allowlist must include every artifact that a
    # specialist writes containing private analysis.
    required = {
        "must-have-table.md",
        "hm-dossier.md",
        "outreach-snippets.md",
        "humanizer-audit.md",
        "cover-letter.md",
    }
    assert required <= SENSITIVE_BLOCK_FILES


# ---------- public-mode snapshot/sanitize/restore (fix-roborev-906) ----------

import yaml as _yaml  # noqa: E402


def _make_assembled_app(root: Path, slug: str) -> Path:
    """Lay out an app with _variables.yml + sensitive _blocks/*.md files,
    skipping the full assemble pipeline."""
    app = root / "private" / "applications" / slug
    blocks = app / "_blocks"
    blocks.mkdir(parents=True)
    (app / "_variables.yml").write_text(
        _yaml.safe_dump(
            {
                "company": "Acme Corp",
                "position": "Engineer",
                "slug": slug,
                "salary_range": "$120k–$150k",
                "fit": {"score": 0.78, "rationale": "strong"},
                "hm": {"detected": True, "name": "Pat Director"},
            },
            sort_keys=False,
        )
    )
    (blocks / "must-haves.md").write_text("- Python\n")
    (blocks / "hm-dossier.md").write_text("## HM dossier\nPat Director\n")
    (blocks / "outreach-snippets.md").write_text("LinkedIn note\n")
    return app


def test_render_site_public_sanitizes_then_restores_variables(
    tmp_path: Path, monkeypatch
) -> None:
    """Public render writes sanitized _variables.yml during the quarto call,
    then restores the originals on completion."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    app = _make_assembled_app(root, "acme-engineer")
    original_vars = (app / "_variables.yml").read_text()
    original_hm_dossier = (app / "_blocks" / "hm-dossier.md").read_text()

    captured_during_run: dict = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        # While quarto would be running, the on-disk files should be sanitized.
        captured_during_run["vars"] = (app / "_variables.yml").read_text()
        captured_during_run["hm_dossier"] = (
            app / "_blocks" / "hm-dossier.md"
        ).read_text()

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    import subprocess as subprocess_mod

    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")
    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    site_mod.render_site(root, mode="public")

    # During the quarto run, sensitive content was stripped/redacted
    sanitized_vars = _yaml.safe_load(captured_during_run["vars"])
    assert sanitized_vars["fit"] == {}  # nested fit emptied
    assert "hm" not in sanitized_vars or sanitized_vars["hm"] == {}
    assert "salary_range" not in sanitized_vars
    assert "omitted in the public variant" in captured_during_run["hm_dossier"]

    # After completion, originals restored
    assert (app / "_variables.yml").read_text() == original_vars
    assert (app / "_blocks" / "hm-dossier.md").read_text() == original_hm_dossier


def test_render_site_public_restores_variables_even_on_quarto_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """If quarto exits non-zero, the snapshot restore still runs — never
    leave private state stripped on disk."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    app = _make_assembled_app(root, "acme-engineer")
    original_vars = (app / "_variables.yml").read_text()
    original_hm_dossier = (app / "_blocks" / "hm-dossier.md").read_text()

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    import subprocess as subprocess_mod

    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")
    monkeypatch.setattr(subprocess_mod, "run", lambda *a, **kw: _R())

    with pytest.raises(RuntimeError):
        site_mod.render_site(root, mode="public")

    assert (app / "_variables.yml").read_text() == original_vars
    assert (app / "_blocks" / "hm-dossier.md").read_text() == original_hm_dossier


def test_render_site_private_does_not_mutate_or_restore(
    tmp_path: Path, monkeypatch
) -> None:
    """Private mode must not touch _variables.yml or _blocks/*.md at all."""
    from jobsmith import site as site_mod

    root = _site_root(tmp_path)
    app = _make_assembled_app(root, "acme-engineer")
    original_vars = (app / "_variables.yml").read_text()
    original_hm_dossier = (app / "_blocks" / "hm-dossier.md").read_text()

    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["vars"] = (app / "_variables.yml").read_text()

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    import subprocess as subprocess_mod

    monkeypatch.setattr(site_mod.shutil, "which", lambda name: "/fake/quarto")
    monkeypatch.setattr(subprocess_mod, "run", fake_run)

    site_mod.render_site(root, mode="private")

    # During the quarto call, _variables.yml is still the original (private mode
    # never sanitizes). Note: assemble_all may have run if private/applications/
    # exists with .apply-state — _make_assembled_app does NOT create .apply-state,
    # so assemble_all skips this dir, leaving _variables.yml untouched.
    assert captured["vars"] == original_vars
    assert (app / "_blocks" / "hm-dossier.md").read_text() == original_hm_dossier


# ---------- T1.1 / T1.5 — sensitive paths added per PR #1 review ----------


def test_sanitize_strips_company_research_in_public_mode() -> None:
    """company_research is mirrored into _variables.yml from .apply-state/.
    Block file is redacted by sanitize_blocks_dir; the raw text in the
    variables dict must also be stripped to keep the privacy guarantee."""
    sample = {
        "company": "Acme",
        "company_research": (
            "# Mission\nDemocratize energy.\n## Selected Reasons\n"
            "Topical: my Invenergy work overlaps directly."
        ),
    }
    out = sanitize_variables(sample, mode="public")
    assert "company_research" not in out
    assert out["company"] == "Acme"


def test_sanitize_strips_jd_text_clean_in_public_mode() -> None:
    """jd.text_clean often contains salary ranges, internal req IDs, and
    named-HM mentions left in by the parser."""
    sample = {
        "company": "Acme",
        "jd": {
            "must_haves": ["Python"],
            "text_clean": "Salary: $200k. Reporting to Pat Director.",
        },
    }
    out = sanitize_variables(sample, mode="public")
    assert "text_clean" not in out["jd"]
    # Sibling jd keys survive — only text_clean is private
    assert out["jd"]["must_haves"] == ["Python"]


# ---------- T1.2 — empty-file restore (PR #1 review) ----------


def test_restore_snapshot_handles_empty_variables_yml(tmp_path: Path) -> None:
    """An app whose _variables.yml exists but is empty must come back as
    empty after a public render — not as the sanitized YAML the snapshot
    pass might have written."""
    from jobsmith.site import _restore_snapshot, _snapshot_and_sanitize

    apps_root = tmp_path / "private" / "applications"
    app = apps_root / "empty-vars-app"
    (app / ".apply-state").mkdir(parents=True)
    (app / "_variables.yml").write_text("")  # present but empty
    (app / "_blocks").mkdir()
    (app / "_blocks" / "hm-dossier.md").write_text("private dossier\n")

    snapshot = _snapshot_and_sanitize(apps_root)

    # The sanitize pass should NOT have rewritten the empty file with
    # sanitized content (loaded YAML is None, so it would be {}).
    assert (app / "_variables.yml").read_text() == ""
    # Block file got redacted
    assert "omitted in the public variant" in (
        app / "_blocks" / "hm-dossier.md"
    ).read_text()

    _restore_snapshot(snapshot)

    # After restore: file is still present and still empty (the snapshot
    # remembered "" via the present-but-empty path, distinguished from
    # absent via None).
    assert (app / "_variables.yml").is_file()
    assert (app / "_variables.yml").read_text() == ""
    # Block file restored
    assert (app / "_blocks" / "hm-dossier.md").read_text() == "private dossier\n"


def test_restore_snapshot_does_not_create_absent_variables_yml(tmp_path: Path) -> None:
    """If _variables.yml never existed, restore must NOT create it."""
    from jobsmith.site import _restore_snapshot, _snapshot_and_sanitize

    apps_root = tmp_path / "private" / "applications"
    app = apps_root / "no-vars-app"
    (app / ".apply-state").mkdir(parents=True)
    # Note: no _variables.yml, no _blocks/

    snapshot = _snapshot_and_sanitize(apps_root)
    _restore_snapshot(snapshot)

    assert not (app / "_variables.yml").exists()
