"""Tests for jobsmith.assemble — pre-render state assembly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobsmith.assemble import (
    _bullet_list,
    _hm_dossier_md,
    _keyword_inline,
    _must_have_table,
    _outreach_snippets_block,
    assemble_all,
    assemble_application,
)

# ---------- markdown renderers ----------


def test_bullet_list_renders_items() -> None:
    assert _bullet_list(["a", "b"]) == "- a\n- b"


def test_bullet_list_empty_returns_placeholder() -> None:
    assert _bullet_list([]) == "_(none)_"


def test_keyword_inline_wraps_in_code() -> None:
    assert _keyword_inline(["Python", "SQL"]) == "`Python`, `SQL`"


def test_must_have_table_renders_with_emoji_levels() -> None:
    rows = [
        {"requirement": "5+ years Python", "level": "STRONG", "evidence": "real"},
        {"requirement": "Redshift", "level": "GAP", "evidence": "missing"},
    ]
    out = _must_have_table(rows)
    assert "✅ STRONG" in out
    assert "❌ GAP" in out
    assert "5+ years Python" in out


def test_must_have_table_escapes_pipes_in_evidence() -> None:
    rows = [{"requirement": "x|y", "level": "HAVE", "evidence": "a|b"}]
    out = _must_have_table(rows)
    assert "x\\|y" in out
    assert "a\\|b" in out


def test_hm_dossier_undetected_renders_placeholder() -> None:
    out = _hm_dossier_md({"detected": False})
    assert "No hiring manager detected" in out


def test_hm_dossier_detected_renders_signal_and_hook() -> None:
    out = _hm_dossier_md(
        {
            "detected": True,
            "name": "Pat Director",
            "source": "linkedin_post",
            "one_specific_signal": "Authored 2024 paper on X",
            "suggested_hook": "Reference the 2024 paper directly",
        }
    )
    assert "Pat Director" in out
    assert "linkedin_post" in out
    assert "2024 paper" in out


# ---------- assemble_application ----------


def _setup_app(tmp_path: Path, slug: str = "test-co-engineer") -> Path:
    """Create a minimal application directory with .apply-state files."""
    app_dir = tmp_path / "applications" / slug
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    (state_dir / "jd-parsed.json").write_text(
        json.dumps(
            {
                "company": "Test Co",
                "position": "Engineer",
                "location": "Remote",
                "location_type": "remote",
                "salary_range": "$100K-$150K",
                "req_id": "TC-001",
                "apply_url": "https://example.com/apply",
                "named_hm": None,
                "role_type": "data-engineer",
                "must_haves": ["Python", "SQL"],
                "nice_to_haves": ["Terraform"],
                "top_keywords": ["Python", "SQL", "ETL"],
                "jd_text_clean": "We need engineers.",
            }
        )
    )
    (state_dir / "fit-score.json").write_text(
        json.dumps(
            {
                "score": 0.75,
                "score_raw": 75,
                "rationale": "Strong fit",
                "specialty": "none",
                "confidence": "high",
                "must_have_table": [
                    {"requirement": "Python", "level": "STRONG", "evidence": "yes"},
                ],
                "matched_evidence": ["profile.python"],
                "concerns": [],
                "pitch": "Great match",
            }
        )
    )
    (state_dir / "hm-snippet.md").write_text(
        "# HM dossier\n\ndetected: no\nname: null\nsource: none\n"
    )
    (app_dir / "cover-letter-draft.md").write_text("Dear Hiring Team,\n\nReal letter.")

    return tmp_path / "applications"


def test_assemble_application_writes_variables_yml(tmp_path: Path) -> None:
    apps_dir = _setup_app(tmp_path)
    out_path = assemble_application("test-co-engineer", apps_dir)
    assert out_path.exists()
    assert out_path.name == "_variables.yml"
    data = yaml.safe_load(out_path.read_text())
    assert data["slug"] == "test-co-engineer"
    assert data["company"] == "Test Co"
    assert data["fit"]["score_raw"] == 75


def test_assemble_application_includes_md_renderings(tmp_path: Path) -> None:
    apps_dir = _setup_app(tmp_path)
    assemble_application("test-co-engineer", apps_dir)
    data = yaml.safe_load((apps_dir / "test-co-engineer" / "_variables.yml").read_text())
    assert "must_haves_md" in data["jd"]
    assert data["jd"]["must_haves_md"] == "- Python\n- SQL"
    assert "must_have_table_md" in data["fit"]
    assert "✅ STRONG" in data["fit"]["must_have_table_md"]
    assert "hm_md" in data
    assert "No hiring manager detected" in data["hm_md"]


def test_assemble_application_loads_cover_letter(tmp_path: Path) -> None:
    apps_dir = _setup_app(tmp_path)
    assemble_application("test-co-engineer", apps_dir)
    data = yaml.safe_load((apps_dir / "test-co-engineer" / "_variables.yml").read_text())
    assert "Real letter." in data["cover_letter_draft"]


def test_assemble_application_missing_app_raises(tmp_path: Path) -> None:
    apps_dir = tmp_path / "applications"
    apps_dir.mkdir()
    with pytest.raises(ValueError, match="application directory not found"):
        assemble_application("nonexistent", apps_dir)


def test_assemble_application_missing_state_dir_raises(tmp_path: Path) -> None:
    apps_dir = tmp_path / "applications"
    (apps_dir / "no-state").mkdir(parents=True)
    with pytest.raises(ValueError, match=".apply-state/ not found"):
        assemble_application("no-state", apps_dir)


# ---------- assemble_all ----------


def test_assemble_all_processes_every_app(tmp_path: Path) -> None:
    apps_dir = _setup_app(tmp_path, "first-co-engineer")
    _setup_app(tmp_path, "second-co-engineer")  # adds another to same apps_dir
    written = assemble_all(apps_dir)
    assert len(written) == 2
    slugs = {p.parent.name for p in written}
    assert slugs == {"first-co-engineer", "second-co-engineer"}


def test_assemble_all_skips_dirs_without_apply_state(tmp_path: Path) -> None:
    apps_dir = _setup_app(tmp_path)
    # Add a directory that lacks .apply-state/
    (apps_dir / "incomplete").mkdir()
    written = assemble_all(apps_dir)
    assert len(written) == 1
    assert written[0].parent.name == "test-co-engineer"


def test_assemble_all_skips_underscore_and_dot_dirs(tmp_path: Path) -> None:
    apps_dir = _setup_app(tmp_path)
    (apps_dir / "_pending").mkdir()
    (apps_dir / ".cache").mkdir()
    written = assemble_all(apps_dir)
    assert len(written) == 1


def test_assemble_all_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="applications directory not found"):
        assemble_all(tmp_path / "nonexistent")


# ---------- outreach-snippets block ----------

_SAMPLE_OUTREACH_SNIPPETS = """\
## Connection Request Note (≤300 chars)

Hi {name}, I saw your post on climate data infrastructure and would love to connect.\
 I'm applying for the Data Engineer role at {company} — your work on sensor\
 pipelines resonates directly with my background.

## InMail Message (~180 words)

Hi {name},

I noticed your recent LinkedIn post about real-time climate data pipelines, and it\
 resonated strongly with the work I've been doing on energy asset monitoring at\
 Mercuria. I'm reaching out because I've just applied for the Senior Data Engineer\
 role at {company}, and your post made it clear why this team is doing genuinely\
 differentiated work.

At Mercuria I built the ETL pipeline that ingests telemetry from 200K+ renewable\
 assets in real time, cutting reporting latency from 4 hours to under 15 minutes.\
 The climate sensor infrastructure you described faces exactly the same trade-offs\
 between throughput and accuracy that shaped our design choices.

I'd appreciate the chance to hear your perspective on how the team approaches\
 stream processing at scale, and to share a bit more about my background if it\
 seems like a fit.

Thanks for your time,
{user}
"""

_SENTINEL_OUTREACH = "no HM detected — portal-only application"


def _setup_app_with_outreach(
    tmp_path: Path,
    slug: str = "test-co-engineer",
    outreach_content: str | None = _SAMPLE_OUTREACH_SNIPPETS,
) -> Path:
    """Create a minimal application directory; optionally include outreach-snippets.md."""
    apps_dir = _setup_app(tmp_path, slug)
    state_dir = apps_dir / slug / ".apply-state"
    if outreach_content is not None:
        (state_dir / "outreach-snippets.md").write_text(outreach_content)
    return apps_dir


def _connection_note_lines(block_text: str) -> list[str]:
    """Extract lines that belong to the ## Connection Request Note section.

    Returns lines between the section header and the next ## header (or end of file).
    Strips blank lines and the header itself.
    """
    lines = block_text.splitlines()
    in_section = False
    result: list[str] = []
    for line in lines:
        if line.startswith("## Connection Request Note"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.strip():
            result.append(line)
    return result


def test_outreach_snippets_block_present_returns_content() -> None:
    """_outreach_snippets_block returns the file content when provided."""
    content = _outreach_snippets_block(_SAMPLE_OUTREACH_SNIPPETS)
    assert "Connection Request Note" in content
    assert "InMail Message" in content


def test_outreach_snippets_block_none_returns_sentinel_callout() -> None:
    """_outreach_snippets_block returns a fallback callout when content is None."""
    block = _outreach_snippets_block(None)
    assert "callout-warning" in block or "awaiting specialist" in block.lower()
    assert "outreach" in block.lower() or "hm" in block.lower() or "specialist" in block.lower()


def test_outreach_block_when_snippets_present(tmp_path: Path) -> None:
    """When outreach-snippets.md exists in .apply-state, it's written to _blocks/outreach-snippets.md."""
    apps_dir = _setup_app_with_outreach(tmp_path, outreach_content=_SAMPLE_OUTREACH_SNIPPETS)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "outreach-snippets.md"
    assert block_file.exists(), "_blocks/outreach-snippets.md was not written"
    content = block_file.read_text()
    assert "Connection Request Note" in content
    assert "InMail Message" in content


def test_outreach_block_fallback_when_missing(tmp_path: Path) -> None:
    """When outreach-snippets.md is absent, the block is a 'awaiting specialist' callout."""
    # _setup_app does NOT write outreach-snippets.md
    apps_dir = _setup_app(tmp_path)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "outreach-snippets.md"
    assert block_file.exists(), "_blocks/outreach-snippets.md fallback was not written"
    content = block_file.read_text()
    assert "callout-warning" in content


def test_outreach_connection_note_within_300_chars(tmp_path: Path) -> None:
    """A connection note ≤300 chars passes through unchanged."""
    note_280 = "A" * 280
    content = f"## Connection Request Note (≤300 chars)\n\n{note_280}\n\n## InMail Message (~180 words)\n\nHello.\n"
    apps_dir = _setup_app_with_outreach(tmp_path, outreach_content=content)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "outreach-snippets.md"
    block_text = block_file.read_text()
    note_lines = _connection_note_lines(block_text)
    assert note_lines, "No content lines found in Connection Request Note section"
    for line in note_lines:
        assert len(line) <= 300, f"Line in connection note exceeds 300 chars: {len(line)}"


def test_outreach_connection_note_350_chars_preserved_verbatim(tmp_path: Path) -> None:
    """A 350-char connection note is passed through verbatim; constraint belongs to the specialist."""
    note_350 = "B" * 350
    content = f"## Connection Request Note (≤300 chars)\n\n{note_350}\n\n## InMail Message (~180 words)\n\nHello.\n"
    apps_dir = _setup_app_with_outreach(tmp_path, outreach_content=content)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "outreach-snippets.md"
    block_text = block_file.read_text()
    # The loader passes through verbatim — the specialist owns the ≤300 constraint.
    assert note_350 in block_text


# ---------- theme resolution ----------


def _make_package_root(tmp_path: Path) -> Path:
    """Create a minimal package root with default.scss and one curated theme."""
    pkg_root = tmp_path / "pkg"
    themes_dir = pkg_root / "templates" / "themes"
    themes_dir.mkdir(parents=True)
    (themes_dir / "default.scss").write_text(
        "// default\n$jobsmith-primary: #333333;\n"
    )
    companies_dir = themes_dir / "companies"
    companies_dir.mkdir()
    (companies_dir / "schneider-electric.scss").write_text(
        "// Schneider Electric\n$jobsmith-primary: #3DCD58;\n"
    )
    return pkg_root


def test_slugify_company_name() -> None:
    assert _slugify_company("Schneider Electric") == "schneider-electric"
    assert _slugify_company("PwC") == "pwc"
    assert _slugify_company("Google") == "google"
    assert _slugify_company("Microsoft Corp.") == "microsoft-corp"
    assert _slugify_company("Netflix") == "netflix"
    assert _slugify_company("Microsoft") == "microsoft"


def test_resolve_theme_picks_app_override_when_present(tmp_path: Path) -> None:
    """When <app>/theme.scss already exists, it is used as-is (user override)."""
    pkg_root = _make_package_root(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    override = app_dir / "theme.scss"
    override.write_text("// user override\n$jobsmith-primary: #FF0000;\n")

    resolved = _resolve_theme(
        slug="schneider-electric",
        company="Schneider Electric",
        app_dir=app_dir,
        package_root=pkg_root,
    )
    assert resolved == override


def test_resolve_theme_picks_curated_when_no_override(tmp_path: Path) -> None:
    """When no app override, the curated company SCSS is selected."""
    pkg_root = _make_package_root(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir()

    resolved = _resolve_theme(
        slug="schneider-electric",
        company="Schneider Electric",
        app_dir=app_dir,
        package_root=pkg_root,
    )
    assert resolved == pkg_root / "templates" / "themes" / "companies" / "schneider-electric.scss"


def test_resolve_theme_falls_back_to_default(tmp_path: Path) -> None:
    """No override and no curated file for the slug → falls back to default.scss."""
    pkg_root = _make_package_root(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir()

    resolved = _resolve_theme(
        slug="unknown-corp",
        company="Unknown Corp",
        app_dir=app_dir,
        package_root=pkg_root,
    )
    assert resolved == pkg_root / "templates" / "themes" / "default.scss"


def test_assemble_writes_theme_into_app_dir(tmp_path: Path) -> None:
    """assemble_application creates <app>/theme.scss linked/copied from the resolved theme."""
    pkg_root = _make_package_root(tmp_path)
    apps_dir = _setup_app(tmp_path, "schneider-corp-engineer")
    app_dir = apps_dir / "schneider-corp-engineer"

    # Write the company name into jd-parsed.json
    state_dir = app_dir / ".apply-state"
    jd_data = json.loads((state_dir / "jd-parsed.json").read_text())
    jd_data["company"] = "Schneider Electric"
    (state_dir / "jd-parsed.json").write_text(json.dumps(jd_data))

    assemble_application(
        "schneider-corp-engineer",
        apps_dir,
        package_root=pkg_root,
    )

    theme_path = app_dir / "theme.scss"
    assert theme_path.exists(), "theme.scss was not created in app dir"
    content = theme_path.read_text()
    assert "$jobsmith-primary" in content


def test_quarto_yml_references_theme_scss(tmp_path: Path) -> None:
    """The generated _quarto.yml must include format.html.theme: [cosmo, theme.scss]."""
    pkg_root = _make_package_root(tmp_path)
    apps_dir = _setup_app(tmp_path, "theme-test-engineer")

    assemble_application(
        "theme-test-engineer",
        apps_dir,
        package_root=pkg_root,
    )

    quarto_yml = apps_dir / "theme-test-engineer" / "_quarto.yml"
    assert quarto_yml.exists()
    data = yaml.safe_load(quarto_yml.read_text())
    theme_val = data.get("format", {}).get("html", {}).get("theme")
    assert theme_val == ["cosmo", "theme.scss"], (
        f"Expected ['cosmo', 'theme.scss'], got {theme_val!r}"
    )


# ---------- humanizer-audit block ----------

_SAMPLE_AI_TELL_REPORT = {
    "version": 1,
    "started_at": "2026-04-30T09:00:00Z",
    "iterations": [
        {
            "id": "6.1",
            "label": "first-pass scrub",
            "tells_caught": [
                {"phrase": "leveraged", "category": "ai_action_verb", "replaced_with": "used"}
            ],
            "diff_preview": "@@ -1,1 +1,1 @@\n-leveraged\n+used",
        },
        {
            "id": "6.2",
            "label": "audit",
            "remaining_tells": [
                {"phrase": "innovative", "rationale": "still buzzword", "severity": "med"}
            ],
            "verdict": "needs-revision",
        },
        {
            "id": "6.3",
            "label": "final humanized",
            "applied_fixes": [{"phrase": "innovative", "replaced_with": "novel"}],
            "final_diff": "@@ -1,1 +1,1 @@\n-innovative\n+novel",
        },
    ],
}

_SAMPLE_AI_TELL_REPORT_OUT_OF_ORDER = {
    "version": 1,
    "started_at": "2026-04-30T10:00:00Z",
    "iterations": [
        {
            "id": "6.3",
            "label": "final humanized",
            "applied_fixes": [],
            "final_diff": "",
        },
        {
            "id": "6.1",
            "label": "first-pass scrub",
            "tells_caught": [],
            "diff_preview": "",
        },
        {
            "id": "6.2",
            "label": "audit",
            "remaining_tells": [],
            "verdict": "clean",
        },
    ],
}


def _setup_app_with_ai_tell_report(
    tmp_path: Path,
    slug: str = "test-co-engineer",
    report: dict | None = _SAMPLE_AI_TELL_REPORT,
    corrupt_json: bool = False,
) -> Path:
    """Create a minimal application directory; optionally include ai-tell-report.json."""
    apps_dir = _setup_app(tmp_path, slug)
    state_dir = apps_dir / slug / ".apply-state"
    if corrupt_json:
        (state_dir / "ai-tell-report.json").write_text("{not valid json")
    elif report is not None:
        (state_dir / "ai-tell-report.json").write_text(json.dumps(report))
    return apps_dir


def test_load_ai_tell_report_returns_dict_when_present(tmp_path: Path) -> None:
    """_load_ai_tell_report returns the parsed dict when the file exists."""
    state_dir = tmp_path / ".apply-state"
    state_dir.mkdir(parents=True)
    (state_dir / "ai-tell-report.json").write_text(json.dumps(_SAMPLE_AI_TELL_REPORT))
    result = _load_ai_tell_report(state_dir)
    assert result is not None
    assert result["version"] == 1
    assert len(result["iterations"]) == 3


def test_load_ai_tell_report_returns_none_when_missing(tmp_path: Path) -> None:
    """_load_ai_tell_report returns None when the file does not exist."""
    state_dir = tmp_path / ".apply-state"
    state_dir.mkdir(parents=True)
    assert _load_ai_tell_report(state_dir) is None


def test_load_ai_tell_report_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    """_load_ai_tell_report returns None (not raises) on malformed JSON."""
    state_dir = tmp_path / ".apply-state"
    state_dir.mkdir(parents=True)
    (state_dir / "ai-tell-report.json").write_text("{not valid json")
    result = _load_ai_tell_report(state_dir)
    assert result is None


def test_render_humanizer_audit_block_contains_62_and_63_sections() -> None:
    """_render_humanizer_audit_block includes ### 6.2 Audit and ### 6.3 Final sections."""
    block = _render_humanizer_audit_block(_SAMPLE_AI_TELL_REPORT)
    assert "### 6.2" in block
    assert "### 6.3" in block


def test_render_humanizer_audit_block_includes_remaining_tell() -> None:
    """The 6.2 audit section lists the remaining tell phrase."""
    block = _render_humanizer_audit_block(_SAMPLE_AI_TELL_REPORT)
    assert "innovative" in block


def test_render_humanizer_audit_block_includes_final_diff() -> None:
    """The 6.3 section includes the final_diff content."""
    block = _render_humanizer_audit_block(_SAMPLE_AI_TELL_REPORT)
    assert "novel" in block


def test_render_humanizer_audit_block_fallback_when_none() -> None:
    """_render_humanizer_audit_block returns the awaiting-specialist callout when None."""
    block = _render_humanizer_audit_block(None)
    assert "callout" in block or "awaiting" in block.lower() or "specialist" in block.lower()


def test_humanizer_audit_block_when_report_present(tmp_path: Path) -> None:
    """When ai-tell-report.json is present, _blocks/humanizer-audit.md contains 6.2 + 6.3 sections."""
    apps_dir = _setup_app_with_ai_tell_report(tmp_path, report=_SAMPLE_AI_TELL_REPORT)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "humanizer-audit.md"
    assert block_file.exists(), "_blocks/humanizer-audit.md was not written"
    content = block_file.read_text()
    assert "### 6.2" in content, "6.2 Audit section missing"
    assert "### 6.3" in content, "6.3 Final section missing"


def test_humanizer_audit_block_fallback_when_missing(tmp_path: Path) -> None:
    """When ai-tell-report.json is absent, _blocks/humanizer-audit.md is the awaiting callout."""
    apps_dir = _setup_app_with_ai_tell_report(tmp_path, report=None)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "humanizer-audit.md"
    assert block_file.exists(), "_blocks/humanizer-audit.md fallback was not written"
    content = block_file.read_text()
    assert "callout" in content or "awaiting" in content.lower() or "specialist" in content.lower()


def test_humanizer_audit_handles_malformed_json(tmp_path: Path) -> None:
    """Corrupt ai-tell-report.json degrades to fallback callout without raising."""
    apps_dir = _setup_app_with_ai_tell_report(tmp_path, corrupt_json=True)
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "humanizer-audit.md"
    assert block_file.exists(), "_blocks/humanizer-audit.md not written on malformed input"
    content = block_file.read_text()
    assert "callout" in content or "awaiting" in content.lower() or "specialist" in content.lower()


def test_ai_tell_report_iteration_ordering(tmp_path: Path) -> None:
    """Iterations out of order in the JSON are rendered in 6.1 -> 6.2 -> 6.3 sequence."""
    apps_dir = _setup_app_with_ai_tell_report(
        tmp_path, report=_SAMPLE_AI_TELL_REPORT_OUT_OF_ORDER
    )
    assemble_application("test-co-engineer", apps_dir)

    block_file = apps_dir / "test-co-engineer" / "_blocks" / "humanizer-audit.md"
    content = block_file.read_text()
    pos_62 = content.find("### 6.2")
    pos_63 = content.find("### 6.3")
    assert pos_62 != -1, "6.2 section not found"
    assert pos_63 != -1, "6.3 section not found"
    assert pos_62 < pos_63, "6.2 must appear before 6.3 in the rendered block"
