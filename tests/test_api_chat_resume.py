"""Tests for /api/chat system prompt with resume sections.

Coverage:
- System prompt loads education.yml, work.yml, skill.yml, author.yml from per-application documents/.
- Resume sections are included in the prompt with proper headers.
- Sections are capped at 2500 chars with truncation marker.
- Proposal instructions include resume editing guidance.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jobsmith.api.chat import _PROPOSAL_INSTRUCTIONS


@pytest.fixture
def app_dir(tmp_path: Path):
    """Create a test application directory with resume sections."""
    app_dir = tmp_path / "test-app"
    app_dir.mkdir(parents=True, exist_ok=True)

    documents_dir = app_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)

    # Create sample resume sections
    (documents_dir / "education.yml").write_text(
        """
- school: "University of Chicago"
  degree: "B.S."
  field: "Computer Science"
  graduated: "2020"
"""
    )

    (documents_dir / "work.yml").write_text(
        """
- company: "Acme Corp"
  position: "Senior Engineer"
  started: "2021-01"
  ended: "2023-12"
  notes: |
    - Led team of 5
    - Shipped features
"""
    )

    (documents_dir / "skill.yml").write_text(
        """
- area: "Programming"
  items: ["Python", "Go", "TypeScript"]
- area: "Cloud"
  items: ["AWS", "GCP"]
"""
    )

    (documents_dir / "author.yml").write_text(
        """
name: "Test User"
email: "test@example.com"
phone: "(555) 123-4567"
"""
    )

    return app_dir


def test_build_system_prompt_includes_resume_sections(app_dir: Path):
    """System prompt includes all four resume sections."""
    from jobsmith.api import applications as app_module
    from jobsmith.api.chat import _build_system_prompt as real_build

    with patch.object(app_module, '_get_app_dir', return_value=app_dir):
        prompt = real_build("test-slug")

    assert prompt is not None
    assert "Resume Sections" in prompt
    assert "## Education (education.yml)" in prompt or "### Education" in prompt
    assert "## Work (work.yml)" in prompt or "### Work" in prompt
    assert "## Skills (skill.yml)" in prompt or "### Skills" in prompt
    assert "## Author (author.yml)" in prompt or "### Author" in prompt

    # Check content is present
    assert "University of Chicago" in prompt
    assert "Acme Corp" in prompt
    assert "Python" in prompt
    assert "Test User" in prompt


def test_build_system_prompt_respects_2500_char_cap(tmp_path: Path):
    """Resume sections are truncated at 2500 chars with marker."""
    app_dir = tmp_path / "test-app"
    app_dir.mkdir(parents=True, exist_ok=True)
    documents_dir = app_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)

    # Create a work.yml that's much longer than 2500 chars
    long_work = "- company: Test\n  position: Eng\n  notes: " + "x" * 3000 + "\n"
    (documents_dir / "work.yml").write_text(long_work)

    # Empty other sections
    (documents_dir / "education.yml").write_text("- school: Test\n  degree: BS\n")
    (documents_dir / "skill.yml").write_text("- area: Programming\n  items: []\n")
    (documents_dir / "author.yml").write_text("name: Test\n")

    from jobsmith.api import applications as app_module
    from jobsmith.api.chat import _build_system_prompt as build_sys_prompt

    with patch.object(app_module, '_get_app_dir', return_value=app_dir):
        prompt = build_sys_prompt("test-slug")

    assert prompt is not None
    assert "[truncated]" in prompt  # Indicates truncation happened
    # Total prompt shouldn't be massive
    assert len(prompt) < 10000


def test_proposal_instructions_mention_resume(tmp_path: Path):
    """Proposal instructions include resume editing guidance."""
    assert "Editing the cover letter or resume sections" in _PROPOSAL_INSTRUCTIONS
    assert "education.yml" in _PROPOSAL_INSTRUCTIONS
    assert "work.yml" in _PROPOSAL_INSTRUCTIONS
    assert "skill.yml" in _PROPOSAL_INSTRUCTIONS
    assert "author.yml" in _PROPOSAL_INSTRUCTIONS
    assert "target_section" in _PROPOSAL_INSTRUCTIONS
    assert "target_file" in _PROPOSAL_INSTRUCTIONS
    assert "ONE PAGE" in _PROPOSAL_INSTRUCTIONS


def test_system_prompt_missing_sections_graceful(tmp_path: Path):
    """System prompt works even if some resume sections are missing."""
    app_dir = tmp_path / "test-app"
    app_dir.mkdir(parents=True, exist_ok=True)
    documents_dir = app_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)

    # Only create one section
    (documents_dir / "education.yml").write_text("- school: Test\n")
    # Others missing

    from jobsmith.api import applications as app_module
    from jobsmith.api.chat import _build_system_prompt as build_sys_prompt_func

    with patch.object(app_module, '_get_app_dir', return_value=app_dir):
        prompt = build_sys_prompt_func("test-slug")

    assert prompt is not None
    assert "University of Chicago" in prompt or "Test" in prompt
    # Should still mention the resume sections header even if sparse
    assert "Resume" in prompt or "resume" in prompt.lower()
