"""Tests for the LOCAL (offline) resume render + DB run-record of the code_local
apply path (feat-d1ef000b, roborev 1061 finding 1).

These exercise render.py and run_record.py in isolation: gather output is SEEDED
on disk (documents/work.yml + skill.yml + .apply-state/prose-draft.md) rather than
produced by a live model. Pat-Doe-style fixtures under a tmp_path repo root — the
real user's data is never read.

done_when proven here:
  1. render_local builds documents/resume.qmd from prose-draft.md + master
     author/education, copies education.yml/author.yml into documents/, and with
     quarto ABSENT returns status="skipped" with NO fake resume.pdf.
  2. The apply run is recorded: insert apply_runs(running) then finalize
     (done|failed). With no config DB it is a clean no-op (guarded).
  3. A render precondition failure (missing gather docs) returns an error
     RenderResult, never raising.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from jobsmith.apply_local.checkpoint import apply_state_dir
from jobsmith.apply_local.nodes_draft import ART_PROSE_DRAFT
from jobsmith.apply_local.render import (
    RenderResult,
    _extract_professional_summary,
    render_local,
)
from jobsmith.apply_local.run_record import finalize_run, open_run_record
from jobsmith.config import JobsmithConfig

SLUG = "helios-data-engineer"

_AUTHOR = {"author": [{"name": {"first": "Pat", "last": "Doe"}, "email": "pat@example.com"}]}
_EDUCATION = [
    {
        "title": "Northeastern University",
        "location": "Boston, MA",
        "date": "2018 - 2020",
        "description": "M.S. Data Analytics Engineering",
        "details": ["Thesis: geospatial ML for renewable siting"],
    }
]
_WORK = [
    {
        "title": "Senior Data Engineer",
        "location": "Helios Energy",
        "date": "2024 - Present",
        "description": "Remote",
        "details": ["Unlocked $250M in tax credits across 200K solar assets"],
    }
]
_SKILL = [{"title": "Programming", "description": "Python, SQL", "details": ["Python", "SQL"]}]
_PROSE = (
    "# Professional Summary\n\n"
    "Data engineer building renewable analytics platforms with Python and SQL, "
    "recovering tax credits at scale.\n\n"
    "# Tailored Bullets\n\n"
    "## Senior Data Engineer @ Helios Energy\n"
    "- Cut quarterly report time from 5 days to 4 hours\n"
)


def _write_master(root: Path) -> JobsmithConfig:
    """Master author.yml + education.yml under the default-layout repo root."""
    content = root / "assets" / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "education.yml").write_text(yaml.safe_dump(_EDUCATION), encoding="utf-8")
    (content / "author.yml").write_text(yaml.safe_dump(_AUTHOR), encoding="utf-8")
    (root / "private").mkdir(parents=True, exist_ok=True)
    return JobsmithConfig()


def _seed_documents(root: Path) -> Path:
    """Seed gather output: documents/work.yml + skill.yml + .apply-state draft."""
    state = apply_state_dir(SLUG, root=root)
    state.mkdir(parents=True, exist_ok=True)
    (state / ART_PROSE_DRAFT).write_text(_PROSE, encoding="utf-8")
    documents = state.parent / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    (documents / "work.yml").write_text(yaml.safe_dump(_WORK), encoding="utf-8")
    (documents / "skill.yml").write_text(yaml.safe_dump(_SKILL), encoding="utf-8")
    return documents


# ===========================================================================
# Professional-summary extraction (heading-level robust)
# ===========================================================================


def test_extract_professional_summary_handles_heading_levels() -> None:
    assert (
        _extract_professional_summary("## Professional Summary\n\nHi there.\n\n## Skills\n- x")
        == "Hi there."
    )
    assert _extract_professional_summary("# Professional Summary\n\nLevel one.\n") == "Level one."


def test_extract_professional_summary_falls_back_to_leading_prose() -> None:
    # No 'Professional Summary' heading -> the leading prose block.
    assert _extract_professional_summary("Just some prose.\n\n## Skills\n- x") == "Just some prose."


# ===========================================================================
# done_when 1 — resume.qmd structure + master copy; quarto-absent => skipped
# ===========================================================================


def test_render_builds_resume_qmd_structure_quarto_absent(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_master(tmp_path)
    documents = _seed_documents(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)  # quarto absent

    result = render_local(SLUG, cfg, repo_root=tmp_path)

    assert result.status == "skipped"
    qmd = (documents / "resume.qmd").read_text(encoding="utf-8")
    assert 'title: "Pat Doe"' in qmd
    assert "metadata-files:" in qmd and "- author.yml" in qmd
    assert "awesomecv-typst: default" in qmd
    assert "## Professional Summary" in qmd
    assert "renewable analytics platforms" in qmd  # prose carried over
    assert "{{< yaml work.yml >}}" in qmd
    assert "{{< yaml education.yml >}}" in qmd
    assert "{{< yaml skill.yml >}}" in qmd
    # master education + author copied into documents/
    assert (documents / "education.yml").is_file()
    assert (documents / "author.yml").is_file()
    assert result.artifacts.get("resume_qmd") == str(documents / "resume.qmd")


def test_render_flags_qa_findings_on_resume_bullets(tmp_path: Path, monkeypatch) -> None:
    """roborev 1066: the resume's work.yml bullets are QA-gated (prose-qa only saw
    prose-draft.md), so un-QA'd bullet text is flagged, not silently shipped."""
    cfg = _write_master(tmp_path)
    documents = _seed_documents(tmp_path)
    bad_work = [{"title": "Data Analyst", "location": "Atlas", "date": "2020",
                 "details": ["Leveraged existing pipelines to speed up analyst reporting"]}]
    (documents / "work.yml").write_text(yaml.safe_dump(bad_work), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: None)  # quarto absent

    result = render_local(SLUG, cfg, repo_root=tmp_path)
    assert result.qa_pass is False
    assert any(f.get("category") == "stock_phrases" for f in result.qa_findings)


def test_render_qa_passes_on_clean_resume_bullets(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_master(tmp_path)
    _seed_documents(tmp_path)  # the seeded _WORK bullets are clean
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = render_local(SLUG, cfg, repo_root=tmp_path)
    assert result.qa_pass is True and result.qa_findings == []


def test_render_skipped_makes_no_fake_pdf(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_master(tmp_path)
    documents = _seed_documents(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)

    result = render_local(SLUG, cfg, repo_root=tmp_path)

    assert result.status == "skipped"
    assert result.pdf_path is None
    assert not (documents / "resume.pdf").exists()
    assert "resume_pdf" not in result.artifacts


def test_render_preserves_existing_resume_qmd(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_master(tmp_path)
    documents = _seed_documents(tmp_path)
    sentinel = '---\ntitle: "Custom"\n---\n## Professional Summary\n\nManual edit.\n'
    (documents / "resume.qmd").write_text(sentinel, encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: None)

    render_local(SLUG, cfg, repo_root=tmp_path)

    assert (documents / "resume.qmd").read_text(encoding="utf-8") == sentinel


def test_render_errors_when_gather_docs_missing(tmp_path: Path) -> None:
    cfg = _write_master(tmp_path)
    state = apply_state_dir(SLUG, root=tmp_path)
    state.mkdir(parents=True, exist_ok=True)
    (state / ART_PROSE_DRAFT).write_text(_PROSE, encoding="utf-8")
    # documents/ has neither work.yml nor skill.yml — gather never ran.

    result = render_local(SLUG, cfg, repo_root=tmp_path)

    assert result.status == "error"
    assert "work.yml" in (result.reason or "")
    assert isinstance(result, RenderResult)


# ===========================================================================
# done_when 2 — DB run-record: insert(running) -> finalize; no-config no-op
# ===========================================================================


def test_run_record_no_config_is_clean_noop(tmp_path: Path) -> None:
    conn, run_id = open_run_record(tmp_path, slug=SLUG)  # tmp_path has no .apply-config.yaml
    assert conn is None
    assert run_id  # a uuid was still minted
    finalize_run(conn, run_id, SLUG, "done")  # must not raise


def test_run_record_inserts_running_then_finalizes(tmp_path: Path) -> None:
    (tmp_path / ".apply-config.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "private").mkdir(parents=True, exist_ok=True)

    conn, run_id = open_run_record(tmp_path, slug=SLUG)
    assert conn is not None
    finalize_run(conn, run_id, SLUG, "done")

    from jobsmith.db import get_apply_run_by_slug, open_pipeline_db

    db = open_pipeline_db(tmp_path / "private" / "jobsmith.db")
    try:
        row = get_apply_run_by_slug(db, SLUG)
        assert row is not None
        assert row["status"] == "done"
        assert row["phase"] == "render"
        assert row["run_id"] == run_id
    finally:
        db.close()


def test_run_record_finalize_failed_status(tmp_path: Path) -> None:
    (tmp_path / ".apply-config.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "private").mkdir(parents=True, exist_ok=True)

    conn, run_id = open_run_record(tmp_path, slug=SLUG)
    finalize_run(conn, run_id, SLUG, "failed")

    from jobsmith.db import get_apply_run_by_slug, open_pipeline_db

    db = open_pipeline_db(tmp_path / "private" / "jobsmith.db")
    try:
        row = get_apply_run_by_slug(db, SLUG)
        assert row is not None and row["status"] == "failed"
    finally:
        db.close()
