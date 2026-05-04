"""Tests for GET /api/master (and per-section sub-routes).

TDD: these tests are written before the implementation exists.
Run: uv run pytest tests/test_api_master.py

Contract
--------
- 200 + parsed sections when config + YAMLs found
- 200 + empty list / null for any section whose YAML is missing
- 404 when locate_config() cannot find .apply-config.yaml
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobsmith.api.main import create_app

# ---------------------------------------------------------------------------
# Minimal fixture YAML content
# ---------------------------------------------------------------------------

WORK_YAML = """\
- title: "Senior Data Engineer"
  location: "Helios Energy Corp"
  date: "Aug 2024 - Present"
  description: "Remote"
  details:
    - "Built a Python geospatial analytics platform"
"""

SKILL_YAML = """\
- title: "Programming"
  description: "Python (Advanced), SQL (Advanced)"
  details:
    - "Python (Advanced)"
    - "SQL (Advanced)"
"""

EDUCATION_YAML = """\
- title: "Northeastern University"
  location: "Boston, MA"
  date: "Sept 2018 - May 2020"
  description: "M.S. in Data Analytics Engineering"
  details:
    - "Graduate Research Assistant"
"""

AUTHOR_YAML = """\
author:
  - name:
      first: "Pat"
      middle: ""
      last: "Doe"
    address: "Brooklyn, NY, USA"
    email: "pat@example.com"
    phone: "(+1) 555-0100"
    homepage: "patdoe.dev"
    contacts:
      - icon: fa solid envelope
        text: pat@example.com
        url: mailto:pat@example.com
"""

CONFIG_YAML = """\
master:
  work_yml: assets/content/work.yml
  skill_yml: assets/content/skill.yml
  education_yml: assets/content/education.yml
  author_yml: assets/content/author.yml
"""


def _write_full_fixture(tmp_path: Path) -> None:
    """Write .apply-config.yaml and all four master YAMLs under tmp_path."""
    (tmp_path / ".apply-config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    content_dir = tmp_path / "assets" / "content"
    content_dir.mkdir(parents=True)
    (content_dir / "work.yml").write_text(WORK_YAML, encoding="utf-8")
    (content_dir / "skill.yml").write_text(SKILL_YAML, encoding="utf-8")
    (content_dir / "education.yml").write_text(EDUCATION_YAML, encoding="utf-8")
    (content_dir / "author.yml").write_text(AUTHOR_YAML, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_master_returns_all_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/master returns 200 and a body with all four sections."""
    _write_full_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)

    app = create_app()
    client = TestClient(app)
    response = client.get("/api/master")

    assert response.status_code == 200
    body = response.json()
    assert "work" in body
    assert "skill" in body
    assert "education" in body
    assert "author" in body
    # Spot-check work list
    assert isinstance(body["work"], list)
    assert len(body["work"]) == 1
    assert body["work"][0]["title"] == "Senior Data Engineer"
    # Spot-check author
    assert body["author"] is not None


def test_section_endpoint_returns_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/master/work returns just the work list."""
    _write_full_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)

    app = create_app()
    client = TestClient(app)
    response = client.get("/api/master/work")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["title"] == "Senior Data Engineer"
    assert isinstance(body[0]["details"], list)


def test_missing_section_yaml_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/master and /api/master/skill return 200+empty when skill.yml missing."""
    _write_full_fixture(tmp_path)
    # Remove skill.yml
    (tmp_path / "assets" / "content" / "skill.yml").unlink()
    monkeypatch.chdir(tmp_path)

    app = create_app()
    client = TestClient(app)

    # Full payload — skill should be empty list
    response = client.get("/api/master")
    assert response.status_code == 200
    body = response.json()
    assert body["skill"] == []

    # Section endpoint — also empty list
    response = client.get("/api/master/skill")
    assert response.status_code == 200
    assert response.json() == []


def test_no_config_returns_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/master returns 404 when no .apply-config.yaml is found."""
    # tmp_path has no .apply-config.yaml and is not inside a tree with one
    monkeypatch.chdir(tmp_path)

    app = create_app()
    client = TestClient(app)
    response = client.get("/api/master")

    assert response.status_code == 404
