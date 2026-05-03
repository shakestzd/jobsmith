"""Tests for jobsmith.marimo.finalize — section appliers, PDF path, append ops.

Companion to test_marimo_finalize.py (which covers the atomicity, backup,
idempotency, and amendment-status-routing core). This file covers the
section-by-section appliers, the PDF path contract, and the append
syntax for skills + cover-letter.

Tests
-----
1. test_finalize_rejects_fit_score_amendments
2. test_finalize_pdf_path_is_absolute
3. test_finalize_quarto_subprocess_called
4. test_finalize_cover_letter_append
5. test_finalize_skills_append
6. test_finalize_no_amendments_is_noop
9. test_finalize_pdf_path_is_absolute
10. test_finalize_quarto_subprocess_called
"""
from __future__ import annotations

import os
import tarfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

from jobsmith.config import MasterPaths
from jobsmith.db import insert_amendment, open_review_db
from jobsmith.marimo.directive_parser import Amendment
from jobsmith.marimo.finalize import (
    finalize_run,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WORK_YAML = """\
# Top-of-file comment preserved by ruamel round-trip
- company: Acme Corp
  title: Senior Engineer
  bullets:
    - Built distributed cache reducing latency 40%
    - Led migration to Kubernetes; cut costs $2M/year
- company: Beta Inc
  title: Engineer
  bullets:
    - Designed API serving 10k req/s
"""

EDUCATION_YAML = """\
- institution: State University
  degree: B.S. Computer Science
  year: 2015
"""

SKILL_YAML = """\
technical:
  - Python
  - Go
  - Rust
languages:
  - English
  - Spanish
"""

COVER_LETTER_MD = """\
Dear Hiring Manager,

I am excited to apply for this position.

Sincerely,
Jane
"""


@pytest.fixture()
def app_tree(tmp_path: Path):
    """Create a minimal application + master YAML tree."""
    slug = "acme-swe-2024"

    # Master YAML directory (assets/content)
    assets = tmp_path / "assets" / "content"
    assets.mkdir(parents=True)
    work_yml = assets / "work.yml"
    education_yml = assets / "education.yml"
    skill_yml = assets / "skill.yml"
    work_yml.write_text(WORK_YAML, encoding="utf-8")
    education_yml.write_text(EDUCATION_YAML, encoding="utf-8")
    skill_yml.write_text(SKILL_YAML, encoding="utf-8")

    # Application directory
    app_dir = tmp_path / "private" / "applications" / slug
    app_dir.mkdir(parents=True)
    cover_letter = app_dir / "cover-letter-final.md"
    cover_letter.write_text(COVER_LETTER_MD, encoding="utf-8")

    # Documents directory (quarto output destination)
    docs_dir = app_dir / "documents"
    docs_dir.mkdir()

    # Review DB directory
    review_dir = tmp_path / "private" / ".review"
    review_dir.mkdir(parents=True)

    masters = MasterPaths(
        work_yml=work_yml,
        skill_yml=skill_yml,
        education_yml=education_yml,
        author_yml=assets / "author.yml",
    )
    applications_dir = tmp_path / "private" / "applications"
    backup_dir = tmp_path / "private" / ".review-backups"

    return {
        "slug": slug,
        "tmp_path": tmp_path,
        "masters": masters,
        "applications_dir": applications_dir,
        "review_dir": review_dir,
        "backup_dir": backup_dir,
        "work_yml": work_yml,
        "education_yml": education_yml,
        "skill_yml": skill_yml,
        "cover_letter": cover_letter,
        "app_dir": app_dir,
        "docs_dir": docs_dir,
    }


def _make_amendment(
    section: str = "work",
    index: int | None = 0,
    field: str | None = "bullet[0]",
    op: str = "replace",
    value: str = "Updated bullet",
    status: str = "accepted",
) -> Amendment:
    return Amendment(
        id=str(uuid.uuid4()),
        section=section,
        index=index,
        field=field,
        op=op,
        value=value,
        status=status,
    )


def _seed_db(slug: str, review_dir: Path, amendments: list[Amendment]) -> None:
    """Insert amendments into the review DB."""
    conn = open_review_db(slug, review_dir)
    for a in amendments:
        insert_amendment(
            conn,
            amendment_id=a.id,
            slug=slug,
            run_id=None,
            section=a.section,
            op=a.op,
            value=a.value,
            status=a.status,
            created_at="2024-01-01T00:00:00+00:00",
        )
    conn.close()


def _mock_quarto_success():
    """Patch subprocess.run to simulate a successful quarto render."""
    return mock.patch(
        "jobsmith.marimo.finalize.subprocess.run",
        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
    )


def _mock_quarto_and_pdf(app_tree: dict):
    """Patch subprocess.run and create the PDF so pdf_path exists."""
    docs_dir = app_tree["docs_dir"]

    def _fake_run(cmd, **kwargs):
        # Create the PDF as a side-effect
        (docs_dir / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
        return mock.Mock(returncode=0, stdout="", stderr="")

    return mock.patch("jobsmith.marimo.finalize.subprocess.run", side_effect=_fake_run)


# ---------------------------------------------------------------------------
# Test 8: fit-score amendments rejected with unsupported_sections
# ---------------------------------------------------------------------------


def test_finalize_rejects_fit_score_amendments(app_tree):
    """fit-score amendment → in unsupported_sections, not applied."""
    t = app_tree
    slug = t["slug"]

    fit_score_amend = _make_amendment(
        section="fit-score",
        index=None,
        field=None,
        op="replace",
        value="0.95",
        status="accepted",
    )
    work_amend = _make_amendment(
        section="work", index=0, field="bullet[0]", value="Real edit"
    )
    _seed_db(slug, t["review_dir"], [fit_score_amend, work_amend])

    with _mock_quarto_success():
        result = finalize_run(
            slug=slug,
            accepted_amendments=[fit_score_amend, work_amend],
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    assert "fit-score" in result.unsupported_sections
    # fit-score amendment NOT in finalized list
    assert fit_score_amend.id not in result.finalized_amendment_ids
    # work amendment IS finalized
    assert work_amend.id in result.finalized_amendment_ids


# ---------------------------------------------------------------------------
# Test 9: PDF path is absolute
# ---------------------------------------------------------------------------


def test_finalize_pdf_path_is_absolute(app_tree):
    """result.pdf_path is an absolute Path (not relative to notebook CWD)."""
    t = app_tree
    slug = t["slug"]

    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="Bullet"),
    ]
    _seed_db(slug, t["review_dir"], amendments)

    with _mock_quarto_and_pdf(t):
        result = finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    assert result.pdf_path is not None
    assert result.pdf_path.is_absolute(), f"Expected absolute path, got {result.pdf_path}"


# ---------------------------------------------------------------------------
# Test 10: quarto subprocess called with correct args + cwd
# ---------------------------------------------------------------------------


def test_finalize_quarto_subprocess_called(app_tree):
    """subprocess.run called with ['quarto', 'render', 'documents/resume.qmd']
    and cwd=<app_dir>."""
    t = app_tree
    slug = t["slug"]

    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="Test"),
    ]
    _seed_db(slug, t["review_dir"], amendments)

    with mock.patch("jobsmith.marimo.finalize.subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    mock_run.assert_called_once()
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert cmd == ["quarto", "render", "documents/resume.qmd"]

    cwd = call_args.kwargs.get("cwd") or call_args[1].get("cwd")
    expected_cwd = str(t["app_dir"])
    assert cwd == expected_cwd, f"Expected cwd={expected_cwd}, got cwd={cwd}"


# ---------------------------------------------------------------------------
# Bonus: cover-letter append op
# ---------------------------------------------------------------------------


def test_finalize_cover_letter_append(app_tree):
    """cover-letter append op adds text to end of existing content."""
    t = app_tree
    slug = t["slug"]
    original = t["cover_letter"].read_text(encoding="utf-8")

    amendments = [
        _make_amendment(
            section="cover-letter", index=None, field=None,
            op="append", value="P.S. Thank you for your consideration."
        ),
    ]
    _seed_db(slug, t["review_dir"], amendments)

    with _mock_quarto_success():
        finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    written = t["cover_letter"].read_text(encoding="utf-8")
    assert original.strip() in written
    assert "P.S. Thank you" in written


# ---------------------------------------------------------------------------
# Bonus: skills append op
# ---------------------------------------------------------------------------


def test_finalize_skills_append(app_tree):
    """AMEND skills.technical[+]: add 'TypeScript' → appended to technical list."""
    t = app_tree
    slug = t["slug"]

    amendments = [
        _make_amendment(
            section="skills",
            index=None,
            field="technical",
            op="append",
            value="TypeScript",
        ),
    ]
    _seed_db(slug, t["review_dir"], amendments)

    with _mock_quarto_success():
        result = finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    written = t["skill_yml"].read_text(encoding="utf-8")
    assert "TypeScript" in written
    assert amendments[0].id in result.finalized_amendment_ids


# ---------------------------------------------------------------------------
# Bonus: no amendments = no quarto, no file writes
# ---------------------------------------------------------------------------


def test_finalize_no_amendments_is_noop(app_tree):
    """Empty accepted_amendments list → no files written, quarto NOT called."""
    t = app_tree
    slug = t["slug"]

    with mock.patch("jobsmith.marimo.finalize.subprocess.run") as mock_run:
        result = finalize_run(
            slug=slug,
            accepted_amendments=[],
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    assert result.modified_files == []
    assert result.finalized_amendment_ids == []
    assert result.quarto_returncode == -1
    mock_run.assert_not_called()
