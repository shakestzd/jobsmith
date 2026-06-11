"""Tests for projects loader (feat-5f184890)."""

from __future__ import annotations

from pathlib import Path

import yaml

from jobsmith.assemble import load_projects
from jobsmith.config import ResumeSettings

# ---------- fixtures ----------


def _write_projects_yml(path: Path, projects: list[dict]) -> None:
    """Write a minimal projects.yml to *path*."""
    path.write_text(yaml.safe_dump({"projects": projects}, allow_unicode=True))


_FIXTURE_PROJECTS = [
    {
        "title": "nova_fde",
        "description": "Open-source data-pipeline framework.",
        "url": "https://github.com/patdoe/nova_fde",
        "highlights": ["Cut pipeline boot time from 12s to 2s."],
        "kind": "open-source",
        "is_project": True,
        "excluded_from_resume": False,
        "fillability": "high",
        "tags": ["data", "python", "oss"],
    },
    {
        "title": "Personal Portfolio Site",
        "description": "Static site listing CV, projects, blog.",
        "url": "https://patdoe.dev",
        "kind": "portfolio-site",
        "is_project": False,
        "excluded_from_resume": True,
        "fillability": "low",
        "tags": ["meta"],
    },
    {
        "title": "Climate-impact dashboard",
        "description": "D3 dashboard accompanying the 2024 paper.",
        "url": "https://patdoe.dev/papers/climate-2024",
        "kind": "paper-deliverable",
        "is_project": True,
        "excluded_from_resume": False,
        "fillability": "medium",
        "tags": ["d3", "climate", "viz"],
    },
]


# ---------- loader happy path ----------


def test_projects_loader_returns_expected_list_from_fixture(tmp_path: Path) -> None:
    projects_yml = tmp_path / "projects.yml"
    _write_projects_yml(projects_yml, _FIXTURE_PROJECTS)
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage=None)
    # nova_fde and climate-impact pass all filters; portfolio-site is excluded
    titles = [p["title"] for p in result]
    assert "nova_fde" in titles
    assert "Climate-impact dashboard" in titles
    assert "Personal Portfolio Site" not in titles


# ---------- individual filter tests ----------


def test_projects_loader_filters_excluded_from_resume_true(tmp_path: Path) -> None:
    projects = [
        {
            "title": "Excluded Project",
            "url": "https://example.com/x",
            "kind": "open-source",
            "is_project": True,
            "excluded_from_resume": True,
        },
        {
            "title": "Included Project",
            "url": "https://example.com/y",
            "kind": "open-source",
            "is_project": True,
            "excluded_from_resume": False,
        },
    ]
    projects_yml = tmp_path / "projects.yml"
    _write_projects_yml(projects_yml, projects)
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage=None)
    titles = [p["title"] for p in result]
    assert "Included Project" in titles
    assert "Excluded Project" not in titles


def test_projects_loader_filters_kind_in_excluded_kinds(tmp_path: Path) -> None:
    projects = [
        {
            "title": "My Dotfiles",
            "url": "https://github.com/user/dotfiles",
            "kind": "dotfiles",
            "is_project": True,
            "excluded_from_resume": False,
        },
        {
            "title": "Resume Source",
            "url": "https://github.com/user/resume",
            "kind": "resume-source",
            "is_project": True,
            "excluded_from_resume": False,
        },
        {
            "title": "Real Project",
            "url": "https://github.com/user/real",
            "kind": "open-source",
            "is_project": True,
            "excluded_from_resume": False,
        },
    ]
    projects_yml = tmp_path / "projects.yml"
    _write_projects_yml(projects_yml, projects)
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage=None)
    titles = [p["title"] for p in result]
    assert "Real Project" in titles
    assert "My Dotfiles" not in titles
    assert "Resume Source" not in titles


def test_projects_loader_filters_is_project_false(tmp_path: Path) -> None:
    projects = [
        {
            "title": "Not a Project",
            "url": "https://example.com/n",
            "kind": "open-source",
            "is_project": False,
            "excluded_from_resume": False,
        },
        {
            "title": "Is a Project",
            "url": "https://example.com/y",
            "kind": "open-source",
            "is_project": True,
            "excluded_from_resume": False,
        },
    ]
    projects_yml = tmp_path / "projects.yml"
    _write_projects_yml(projects_yml, projects)
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage=None)
    titles = [p["title"] for p in result]
    assert "Is a Project" in titles
    assert "Not a Project" not in titles


def test_projects_loader_filters_url_matching_author_homepage(tmp_path: Path) -> None:
    projects = [
        {
            "title": "Personal Site",
            "url": "https://patdoe.dev",
            "kind": "open-source",
            "is_project": True,
            "excluded_from_resume": False,
        },
        {
            "title": "Other Project",
            "url": "https://github.com/patdoe/other",
            "kind": "open-source",
            "is_project": True,
            "excluded_from_resume": False,
        },
    ]
    projects_yml = tmp_path / "projects.yml"
    _write_projects_yml(projects_yml, projects)
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage="https://patdoe.dev")
    titles = [p["title"] for p in result]
    assert "Other Project" in titles
    assert "Personal Site" not in titles


# ---------- backward compat ----------


def test_projects_loader_returns_empty_when_file_absent(tmp_path: Path) -> None:
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage=None)
    assert result == []


# ---------- config defaults ----------


def test_resume_settings_excluded_project_kinds_default() -> None:
    settings = ResumeSettings()
    assert settings.excluded_project_kinds == ["portfolio-site", "resume-source", "dotfiles"]


# ---------- Slice C.1 — bullet_type_ordering ----------


def test_resume_settings_bullet_type_ordering_default_work_first() -> None:
    """Default ordering is [work, project] — traditional resume bias."""
    settings = ResumeSettings()
    assert settings.bullet_type_ordering == ["work", "project"]


def test_resume_settings_bullet_type_ordering_overrideable_to_project_first() -> None:
    """Portfolio-heavy careers override to [project, work] (Q7 option-a escape hatch)."""
    settings = ResumeSettings(bullet_type_ordering=["project", "work"])
    assert settings.bullet_type_ordering == ["project", "work"]
    # work-first tiebreaker is now reversed
    assert settings.bullet_type_ordering[0] == "project"


# ---------- Slice C.1 — agent contract: restoration_queue in selector schema ----------


def test_specialist_contracts_declares_restoration_queue() -> None:
    """The bullet-selector outputs schema must declare restoration_queue (Slice C.1 BREAKING change)."""
    from pathlib import Path as _Path

    import yaml as _yaml

    contracts = _yaml.safe_load(
        (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply/specialist-contracts.yaml").read_text()
    )
    selector = next(s for s in contracts["specialists"] if s["name"] == "apply-bullet-selector")
    selection_writes = next(
        w for w in selector["outputs"]["writes"] if ".apply-state/bullet-selection.json" in w
    )
    schema = selection_writes[".apply-state/bullet-selection.json"]
    assert "restoration_queue" in schema, (
        "bullet-selection.json schema must declare restoration_queue field "
        "(Slice C.1 BREAKING contract change — must be coordinated with "
        "apply-bullet-selector.md and apply-visual-layout-reviewer.md)"
    )


def test_apply_bullet_selector_md_documents_ordering_rule() -> None:
    """apply-bullet-selector.md must contain rule 9 (work-first ordering) and rule 10 (restoration_queue)."""
    from pathlib import Path as _Path

    md = (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply-bullet-selector.md").read_text()
    # Rule 9 — Selected Projects ordering
    assert "Selected Projects ordering" in md
    assert "bullet_type_ordering" in md
    # Rule 10 — restoration_queue
    assert "restoration_queue" in md.lower() or "Restoration queue" in md


def test_apply_visual_layout_reviewer_md_documents_restoration() -> None:
    """apply-visual-layout-reviewer.md must contain restoration logic + RESTORATION_STALE halt."""
    from pathlib import Path as _Path

    md = (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply-visual-layout-reviewer.md").read_text()
    assert "restoration_queue" in md, "Reviewer must consume restoration_queue"
    assert "RESTORATION_STALE" in md, "Staleness halt reason must be documented"
    assert "RESTORATION_LIMIT" in md, "Hard cap halt reason must be documented"


# ---------- Bullet style rules: contract enforcement (feat-e5fa07a0) ----------


def test_apply_prose_writer_md_has_bullet_style_rules() -> None:
    """apply-prose-writer.md must contain the Bullet style rules section (Rules 1-6 + 8)."""
    from pathlib import Path as _Path

    md = (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply-prose-writer.md").read_text()
    assert "Bullet style rules" in md, "Must have a 'Bullet style rules' section"
    # Rule 1 — XYZ formula
    assert "XYZ formula" in md or "action verb" in md.lower()
    # Rule 2 — 25-word cap
    assert "25" in md and "word" in md.lower()
    # Rule 3 — one metric cluster
    assert "One metric" in md or "one metric" in md.lower() or "metric cluster" in md.lower()
    # Rule 4 — no parenthetical tech lists
    assert "parenthetical" in md.lower()
    # Rule 5 — acronym discipline
    assert "Acronym" in md or "acronym" in md
    # Rule 6 — em dash banned
    assert "em dash" in md.lower() or "em-dash" in md.lower()
    # Rule 6 — stock phrases
    assert "spearheaded" in md and "leveraged" in md
    # Rule 8 — hard invariant kept
    assert "REMOVE" in md or "RESTRUCTURE" in md or "remove" in md.lower()


def test_apply_bullet_selector_md_has_recency_weighted_bullet_count() -> None:
    """apply-bullet-selector.md must document the 3-5 bullet recency-weighting rule (Rule 7)."""
    from pathlib import Path as _Path

    md = (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply-bullet-selector.md").read_text()
    assert "3-5" in md, "Must state 3-5 bullets per role"
    assert "recen" in md.lower(), "Must mention recency weighting"


def test_apply_prose_qa_md_has_named_bullet_style_checks() -> None:
    """apply-prose-qa.md must define the five named blocking bullet style checks."""
    from pathlib import Path as _Path

    md = (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply-prose-qa.md").read_text()
    assert "check:bullet_word_count" in md
    assert "check:metric_cluster_count" in md
    assert "check:parenthetical_tech_list" in md
    assert "check:em_dash" in md
    assert "check:stock_phrases" in md
    assert "bullet_style_checks" in md, "Output schema must include bullet_style_checks key"


def test_specialist_contracts_yaml_references_bullet_style() -> None:
    """specialist-contracts.yaml must reference bullet style in prose-writer and prose-qa entries."""
    from pathlib import Path as _Path

    import yaml as _yaml

    contracts = _yaml.safe_load(
        (_Path(__file__).parent.parent / "src/jobsmith/plugin/agents/apply/specialist-contracts.yaml").read_text()
    )
    specialists_by_name = {s["name"]: s for s in contracts["specialists"]}

    # prose-writer must declare bullet_style_contract
    pw = specialists_by_name["apply-prose-writer"]
    assert "bullet_style_contract" in pw, "apply-prose-writer contract must include bullet_style_contract key"

    # prose-qa must declare bullet_style_checks
    pq = specialists_by_name["apply-prose-qa"]
    assert "bullet_style_checks" in pq, "apply-prose-qa contract must include bullet_style_checks key"

    # bullet-selector must declare bullet_count_rule
    bs = specialists_by_name["apply-bullet-selector"]
    assert "bullet_count_rule" in bs, "apply-bullet-selector contract must include bullet_count_rule key"

    # frozen_at must be non-null
    assert contracts.get("frozen_at") is not None, "specialist-contracts.yaml must have a non-null frozen_at"


# ---------- Roborev fix: URL normalization (job 917 finding 3) ----------


def test_load_projects_homepage_match_is_scheme_and_www_insensitive(tmp_path: Path) -> None:
    """patdoe.dev vs https://patdoe.dev vs https://www.patdoe.dev/ must all match."""
    from jobsmith.assemble import _normalize_url, load_projects

    # Direct unit test of the normalizer
    assert _normalize_url("patdoe.dev") == "patdoe.dev"
    assert _normalize_url("https://patdoe.dev") == "patdoe.dev"
    assert _normalize_url("https://patdoe.dev/") == "patdoe.dev"
    assert _normalize_url("https://www.patdoe.dev") == "patdoe.dev"
    assert _normalize_url("HTTP://Patdoe.Dev/") == "patdoe.dev"
    assert _normalize_url(None) == ""
    assert _normalize_url("") == ""

    # End-to-end: all three URL spellings get filtered when homepage = patdoe.dev
    projects = [
        {"title": "scheme-less match", "url": "patdoe.dev", "kind": "open-source",
         "is_project": True, "excluded_from_resume": False},
        {"title": "https match", "url": "https://patdoe.dev", "kind": "open-source",
         "is_project": True, "excluded_from_resume": False},
        {"title": "www match", "url": "https://www.patdoe.dev/", "kind": "open-source",
         "is_project": True, "excluded_from_resume": False},
        {"title": "Other Project", "url": "https://github.com/p/other",
         "kind": "open-source", "is_project": True, "excluded_from_resume": False},
    ]
    projects_yml = tmp_path / "projects.yml"
    _write_projects_yml(projects_yml, projects)
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage="patdoe.dev")
    titles = [p["title"] for p in result]
    assert titles == ["Other Project"], (
        f"All three patdoe.dev URL spellings should be filtered. Got: {titles}"
    )


# ---------- Roborev fix: load_projects accepts exact file path (job 917 finding 2) ----------


def test_load_projects_accepts_exact_file_path(tmp_path: Path) -> None:
    """load_projects works when given a non-projects.yml filename (e.g. portfolio-projects.yml)."""
    from jobsmith.assemble import load_projects

    # Custom-named file, NOT projects.yml
    custom = tmp_path / "portfolio-projects.yml"
    _write_projects_yml(custom, [
        {"title": "Custom Path Project", "url": "https://example.com/x",
         "kind": "open-source", "is_project": True, "excluded_from_resume": False},
    ])
    config = ResumeSettings()
    result = load_projects(custom, config, author_homepage=None)
    assert len(result) == 1
    assert result[0]["title"] == "Custom Path Project"


def test_load_projects_directory_form_still_works(tmp_path: Path) -> None:
    """Back-compat: passing a directory still finds <dir>/projects.yml."""
    from jobsmith.assemble import load_projects

    _write_projects_yml(tmp_path / "projects.yml", [
        {"title": "Dir Form Project", "url": "https://example.com/y",
         "kind": "open-source", "is_project": True, "excluded_from_resume": False},
    ])
    config = ResumeSettings()
    result = load_projects(tmp_path, config, author_homepage=None)
    assert len(result) == 1
    assert result[0]["title"] == "Dir Form Project"
