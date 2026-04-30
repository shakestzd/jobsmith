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
