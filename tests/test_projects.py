"""Tests for projects loader (feat-5f184890)."""

from __future__ import annotations

from pathlib import Path

import pytest
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
