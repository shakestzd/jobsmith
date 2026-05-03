"""Tests for jobsmith.marimo.finalize — atomic YAML write-back + quarto render.

TDD-first: all tests should fail before finalize.py is implemented,
then pass after implementation.

Test inventory
--------------
1. test_finalize_writes_only_modified_files
2. test_finalize_preserves_unedited_fields
3. test_finalize_atomic_on_crash
4. test_finalize_creates_backup_tarball
5. test_finalize_marks_amendments_finalized
6. test_finalize_idempotent
7. test_finalize_skips_pending_and_rejected
8. test_finalize_rejects_fit_score_amendments
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
  details:
    - Built distributed cache reducing latency 40%
    - Led migration to Kubernetes; cut costs $2M/year
- company: Beta Inc
  title: Engineer
  details:
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
# Test 1: only modified files are rewritten
# ---------------------------------------------------------------------------


def test_finalize_writes_only_modified_files(app_tree):
    """3 accepted amendments touching work.yml + cover-letter; education mtime unchanged."""
    t = app_tree
    slug = t["slug"]

    # Record original mtimes
    edu_mtime_before = t["education_yml"].stat().st_mtime
    skill_mtime_before = t["skill_yml"].stat().st_mtime

    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="New bullet 1"),
        _make_amendment(section="work", index=0, field="bullet[1]", value="New bullet 2"),
        _make_amendment(
            section="cover-letter", index=None, field=None, op="replace",
            value="Updated cover letter body"
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

    # work.yml and cover-letter modified
    assert any(p.name == "work.yml" for p in result.modified_files)
    assert any(p.name == "cover-letter-final.md" for p in result.modified_files)

    # education.yml and skill.yml NOT modified
    assert t["education_yml"].stat().st_mtime == edu_mtime_before
    assert t["skill_yml"].stat().st_mtime == skill_mtime_before


# ---------------------------------------------------------------------------
# Test 2: unedited fields preserved after ruamel round-trip
# ---------------------------------------------------------------------------


def test_finalize_preserves_unedited_fields(app_tree):
    """ruamel round-trip: only edited field changes; top comment + other keys intact."""
    t = app_tree
    slug = t["slug"]

    # Replace only work[0].bullet[0]
    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="REPLACED"),
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

    written = t["work_yml"].read_text(encoding="utf-8")

    # Top-of-file comment preserved
    assert "# Top-of-file comment" in written
    # Second company untouched
    assert "Beta Inc" in written
    assert "Designed API" in written
    # First bullet replaced
    assert "REPLACED" in written
    # Second bullet of first entry untouched
    assert "Led migration to Kubernetes" in written


# ---------------------------------------------------------------------------
# Test 3: atomic write — no partial writes on crash
# ---------------------------------------------------------------------------


def test_finalize_atomic_on_crash(app_tree):
    """Patch os.replace to raise on 2nd call; first file's content intact."""
    t = app_tree
    slug = t["slug"]

    # Two YAML amendments: work and education — should trigger two os.replace calls
    amendments_work = _make_amendment(
        section="work", index=0, field="bullet[0]", value="Should succeed"
    )
    amendments_edu = _make_amendment(
        section="education", index=0, field="degree", op="replace",
        value="M.S. Computer Science"
    )
    amendments = [amendments_work, amendments_edu]
    _seed_db(slug, t["review_dir"], amendments)

    call_count = 0
    real_replace = os.replace

    def _patched_replace(src, dst):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise OSError("Simulated crash on second replace")
        return real_replace(src, dst)

    with (
        mock.patch("jobsmith.marimo.finalize.os.replace", side_effect=_patched_replace),
        pytest.raises(OSError, match="Simulated crash"),
        _mock_quarto_success(),
    ):
        finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    # The first file (work.yml) was replaced; no tmp file left over
    work_tmp = t["work_yml"].with_suffix(".yml.tmp")
    assert not work_tmp.exists(), "Tmp file must not linger after successful replace"

    # Education was NOT replaced (os.replace raised on second call)
    # Education content should still be the original
    edu_content = t["education_yml"].read_text(encoding="utf-8")
    assert "B.S. Computer Science" in edu_content


# ---------------------------------------------------------------------------
# Test 4: backup tarball created before any write
# ---------------------------------------------------------------------------


def test_finalize_creates_backup_tarball(app_tree):
    """Backup tarball exists at expected path; contains work.yml."""
    t = app_tree
    slug = t["slug"]

    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="New"),
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

    assert result.backup_path.exists()
    assert result.backup_path.suffix == ".gz"
    assert slug in result.backup_path.name

    with tarfile.open(result.backup_path, "r:gz") as tar:
        names = tar.getnames()
    assert "work.yml" in names


# ---------------------------------------------------------------------------
# Test 5: amendments marked finalized after success
# ---------------------------------------------------------------------------


def test_finalize_marks_amendments_finalized(app_tree):
    """After success, 3 accepted amendments have status='finalized' in DB."""
    t = app_tree
    slug = t["slug"]

    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="A1"),
        _make_amendment(section="work", index=0, field="bullet[1]", value="A2"),
        _make_amendment(
            section="cover-letter", index=None, field=None, op="replace", value="A3"
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

    assert len(result.finalized_amendment_ids) == 3

    conn = open_review_db(slug, t["review_dir"])
    finalized = conn.execute(
        "SELECT COUNT(*) FROM amendments WHERE slug=? AND status='finalized'",
        (slug,),
    ).fetchone()[0]
    conn.close()
    assert finalized == 3


# ---------------------------------------------------------------------------
# Test 6: idempotent — second call is a no-op
# ---------------------------------------------------------------------------


def test_finalize_idempotent(app_tree):
    """Call twice; second call returns empty finalized_ids and quarto NOT re-invoked."""
    t = app_tree
    slug = t["slug"]

    amendments = [
        _make_amendment(section="work", index=0, field="bullet[0]", value="Once"),
    ]
    _seed_db(slug, t["review_dir"], amendments)

    quarto_call_count = 0

    def _counting_run(cmd, **kwargs):
        nonlocal quarto_call_count
        quarto_call_count += 1
        return mock.Mock(returncode=0, stdout="", stderr="")

    with mock.patch("jobsmith.marimo.finalize.subprocess.run", side_effect=_counting_run):
        result1 = finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )
        result2 = finalize_run(
            slug=slug,
            accepted_amendments=amendments,
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    # First call applied and finalized
    assert len(result1.finalized_amendment_ids) == 1
    # Second call: nothing to apply (already finalized in DB)
    assert result2.finalized_amendment_ids == []
    assert result2.modified_files == []
    # Quarto only invoked once
    assert quarto_call_count == 1


# ---------------------------------------------------------------------------
# Test 7: skips pending and rejected amendments
# ---------------------------------------------------------------------------


def test_finalize_skips_pending_and_rejected(app_tree):
    """Only accepted amendments are applied; pending/rejected stay untouched."""
    t = app_tree
    slug = t["slug"]

    accepted = _make_amendment(
        section="work", index=0, field="bullet[0]", value="Accepted", status="accepted"
    )
    pending = _make_amendment(
        section="work", index=0, field="bullet[1]", value="Pending", status="pending"
    )
    rejected = _make_amendment(
        section="work", index=1, field="bullet[0]", value="Rejected", status="rejected"
    )

    _seed_db(slug, t["review_dir"], [accepted, pending, rejected])

    # Only pass the accepted amendment to finalize_run
    with _mock_quarto_success():
        result = finalize_run(
            slug=slug,
            accepted_amendments=[accepted],
            masters=t["masters"],
            applications_dir=t["applications_dir"],
            review_db_dir=t["review_dir"],
            backup_dir=t["backup_dir"],
        )

    # Only 1 amendment finalized
    assert len(result.finalized_amendment_ids) == 1
    assert accepted.id in result.finalized_amendment_ids

    # Pending and rejected amendments unchanged in DB
    conn = open_review_db(slug, t["review_dir"])
    rows = {
        row["amendment_id"]: row["status"]
        for row in conn.execute(
            "SELECT amendment_id, status FROM amendments WHERE slug=?", (slug,)
        ).fetchall()
    }
    conn.close()
    assert rows[pending.id] == "pending"
    assert rows[rejected.id] == "rejected"


