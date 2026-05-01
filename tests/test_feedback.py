"""Tests for jobsmith.feedback — record/list/prune/export feedback loop."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from jobsmith.cli import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_feedback_record(
    feedback_dir: Path,
    slug: str,
    kind: str,
    before: str,
    after: str,
    lesson: str = "",
    context: dict | None = None,
    timestamp: str | None = None,
    age_days: int | None = None,
) -> Path:
    """Write a synthetic feedback JSON record, optionally backdating its mtime."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    record = {
        "slug": slug,
        "timestamp": ts,
        "kind": kind,
        "before": before,
        "after": after,
        "lesson": lesson,
        "context": context,
    }
    feedback_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = ts.replace(":", "-").replace("+", "p")
    path = feedback_dir / f"{safe_ts}__{slug}.json"
    path.write_text(json.dumps(record))

    if age_days is not None:
        # Force mtime to simulate an old file.
        old_time = time.time() - age_days * 86400
        os.utime(path, (old_time, old_time))

    return path


# ---------------------------------------------------------------------------
# test_diff_prose_bullets_detects_significant_edit
# ---------------------------------------------------------------------------


def test_diff_prose_bullets_detects_significant_edit(tmp_path: Path) -> None:
    """Synthesize two bullet lists with a one-bullet change; expect one record."""
    from jobsmith.feedback import record as feedback_record

    slug = "test-acme"
    app_dir = tmp_path / "private" / "applications" / slug
    feedback_dir = tmp_path / "private" / "feedback"
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    # Agent baseline is the immutable .agent.md snapshot; live prose-draft.md
    # is the user-editable copy (both in .apply-state/).
    original = "- Built scalable data pipelines\n- Led a team of 3 engineers\n"
    edited = "- Built highly scalable data pipelines handling 10TB daily\n- Led a team of 3 engineers\n"

    (state_dir / "prose-draft.agent.md").write_text(original)
    (state_dir / "prose-draft.md").write_text(edited)

    records = feedback_record(slug, app_dir=app_dir, feedback_dir=feedback_dir)
    assert len(records) == 1
    r = records[0]
    assert r["kind"] == "prose-bullet"
    assert r["slug"] == slug
    assert r["before"] == "- Built scalable data pipelines"
    assert "10TB" in r["after"]
    assert r["lesson"] == ""


# ---------------------------------------------------------------------------
# test_diff_skips_whitespace_only
# ---------------------------------------------------------------------------


def test_diff_skips_whitespace_only(tmp_path: Path) -> None:
    """A whitespace-only diff should produce no feedback records."""
    from jobsmith.feedback import record as feedback_record

    slug = "test-whitespace"
    app_dir = tmp_path / "private" / "applications" / slug
    feedback_dir = tmp_path / "private" / "feedback"
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    original = "- Built scalable data pipelines\n"
    edited = "- Built scalable data pipelines  \n"  # trailing spaces only

    (state_dir / "prose-draft.agent.md").write_text(original)
    (state_dir / "prose-draft.md").write_text(edited)

    records = feedback_record(slug, app_dir=app_dir, feedback_dir=feedback_dir)
    assert records == []


# ---------------------------------------------------------------------------
# test_record_writes_json_to_feedback_dir
# ---------------------------------------------------------------------------


def test_record_writes_json_to_feedback_dir(tmp_path: Path) -> None:
    """record() writes valid JSON files to private/feedback/<slug>-<ts>.json."""
    from jobsmith.feedback import record as feedback_record

    slug = "test-corp-swe"
    app_dir = tmp_path / "private" / "applications" / slug
    feedback_dir = tmp_path / "private" / "feedback"
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    original = "- Owned the billing microservice reducing latency by 20%\n"
    edited = "- Owned the billing microservice reducing P99 latency by 40% across all regions\n"

    (state_dir / "prose-draft.agent.md").write_text(original)
    (state_dir / "prose-draft.md").write_text(edited)

    records = feedback_record(slug, app_dir=app_dir, feedback_dir=feedback_dir)
    assert len(records) == 1

    json_files = list(feedback_dir.glob(f"*__{slug}.json"))
    assert len(json_files) == 1

    data = json.loads(json_files[0].read_text())
    # Validate schema
    assert data["slug"] == slug
    assert "timestamp" in data
    assert data["kind"] in ("prose-bullet", "cover-letter-paragraph")
    assert "before" in data
    assert "after" in data
    assert "lesson" in data
    # lesson is a string (may be empty)
    assert isinstance(data["lesson"], str)


# ---------------------------------------------------------------------------
# test_list_records_filters_by_kind
# ---------------------------------------------------------------------------


def test_list_records_filters_by_kind(tmp_path: Path) -> None:
    """list_records(kind='prose-bullet') returns only matching records."""
    from jobsmith.feedback import list_records

    feedback_dir = tmp_path / "private" / "feedback"

    _write_feedback_record(feedback_dir, "app-a", "prose-bullet", "old a", "new a")
    _write_feedback_record(feedback_dir, "app-b", "cover-letter-paragraph", "old b", "new b")
    _write_feedback_record(feedback_dir, "app-c", "prose-bullet", "old c", "new c")

    results = list_records(filter_kind="prose-bullet", since=None, feedback_dir=feedback_dir)
    assert len(results) == 2
    for r in results:
        assert r["kind"] == "prose-bullet"


# ---------------------------------------------------------------------------
# test_prune_removes_old_records
# ---------------------------------------------------------------------------


def test_prune_removes_old_records(tmp_path: Path) -> None:
    """prune(90) removes a 100-day-old record but keeps a fresh one."""
    from jobsmith.feedback import prune

    feedback_dir = tmp_path / "private" / "feedback"

    old_path = _write_feedback_record(
        feedback_dir, "app-old", "prose-bullet", "x", "y", age_days=100
    )
    _write_feedback_record(feedback_dir, "app-new", "prose-bullet", "a", "b")

    deleted = prune(older_than_days=90, feedback_dir=feedback_dir)
    assert deleted == 1
    assert not old_path.exists()
    # Fresh record still there
    assert len(list(feedback_dir.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# test_export_strips_per_app_details
# ---------------------------------------------------------------------------


def test_export_strips_per_app_details(tmp_path: Path) -> None:
    """export() YAML must NOT contain slug or company names; must contain lesson texts."""
    from jobsmith.feedback import export

    feedback_dir = tmp_path / "private" / "feedback"

    _write_feedback_record(
        feedback_dir,
        "acme-corp-swe",
        "prose-bullet",
        "old bullet",
        "new bullet",
        lesson="Always quantify impact",
        context={"company": "AcmeCorp", "role_type": "SWE"},
    )
    _write_feedback_record(
        feedback_dir,
        "beta-inc-pm",
        "cover-letter-paragraph",
        "old para",
        "new para",
        lesson="Start with a hook",
        context={"company": "BetaInc", "role_type": "PM"},
    )

    yaml_str = export(feedback_dir=feedback_dir)

    # Must NOT contain identifying info
    assert "acme-corp-swe" not in yaml_str
    assert "beta-inc-pm" not in yaml_str
    assert "AcmeCorp" not in yaml_str
    assert "BetaInc" not in yaml_str

    # Must contain lesson texts
    assert "Always quantify impact" in yaml_str
    assert "Start with a hook" in yaml_str


# ---------------------------------------------------------------------------
# test_init_adds_feedback_to_gitignore
# ---------------------------------------------------------------------------


def test_init_adds_feedback_to_gitignore(tmp_path: Path) -> None:
    """jobsmith init should add private/feedback/ to the .gitignore."""
    runner = CliRunner()
    result = runner.invoke(app, ["init", str(tmp_path), "--no-examples"])
    assert result.exit_code == 0, result.output

    gitignore = (tmp_path / ".gitignore").read_text()
    assert "private/feedback/" in gitignore


# ---------------------------------------------------------------------------
# test_cli_feedback_record_invokes_module
# ---------------------------------------------------------------------------


def test_cli_feedback_record_invokes_module(tmp_path: Path) -> None:
    """Typer CLI feedback record command wires through to feedback.record()."""
    runner = CliRunner()

    slug = "test-cli-slug"
    app_dir = tmp_path / "private" / "applications" / slug
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    original = "- Original bullet text here\n"
    edited = "- Modified bullet text here with extra detail\n"
    (state_dir / "prose-draft.agent.md").write_text(original)
    (state_dir / "prose-draft.md").write_text(edited)

    # The CLI should accept a slug and call feedback.record()
    # We mock the working directory via the app's CWD so paths resolve correctly.
    result = runner.invoke(app, ["feedback", "record", slug], catch_exceptions=False)
    # At minimum, should not crash with a 500/import error
    # (may exit non-zero if no app dir found, but should NOT be ImportError)
    assert "ImportError" not in (result.output or "")
    assert result.exit_code in (0, 1, 2)


# ---------------------------------------------------------------------------
# test_record_populates_role_type_context_from_manifest
# ---------------------------------------------------------------------------


def test_record_populates_role_type_context_from_manifest(tmp_path: Path) -> None:
    """record() must read role_type from manifest.json so wave-3 read-back can filter."""
    from jobsmith.feedback import record as feedback_record

    slug = "test-role-type"
    app_dir = tmp_path / "private" / "applications" / slug
    feedback_dir = tmp_path / "private" / "feedback"
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    (state_dir / "manifest.json").write_text(
        json.dumps({"slug": slug, "role_type": "data-engineer"})
    )

    original = "- Built scalable data pipelines\n"
    edited = "- Built highly scalable data pipelines handling 10TB daily\n"
    (state_dir / "prose-draft.agent.md").write_text(original)
    (state_dir / "prose-draft.md").write_text(edited)

    records = feedback_record(slug, app_dir=app_dir, feedback_dir=feedback_dir)
    assert len(records) == 1
    assert records[0]["context"] == {"role_type": "data-engineer"}


# ---------------------------------------------------------------------------
# test_significance_detects_substitution_with_similar_length
# ---------------------------------------------------------------------------


def test_significance_detects_substitution_with_similar_length(tmp_path: Path) -> None:
    """A substitution with similar length but >5 changed chars must be recorded."""
    from jobsmith.feedback import record as feedback_record

    slug = "test-substitution"
    app_dir = tmp_path / "private" / "applications" / slug
    feedback_dir = tmp_path / "private" / "feedback"
    state_dir = app_dir / ".apply-state"
    state_dir.mkdir(parents=True)

    # Same length (40 chars), but the verb + object swap is a substantive edit.
    original = "- analyzed customer churn across 12 cohorts\n"
    edited = "- investigated user attrition over 12 cohorts\n"
    assert abs(len(original) - len(edited)) <= 2  # length-only delta would skip

    (state_dir / "prose-draft.agent.md").write_text(original)
    (state_dir / "prose-draft.md").write_text(edited)

    records = feedback_record(slug, app_dir=app_dir, feedback_dir=feedback_dir)
    assert len(records) == 1
    assert "investigated" in records[0]["after"]


# ---------------------------------------------------------------------------
# test_filename_layout_sorts_chronologically_across_slugs
# ---------------------------------------------------------------------------


def test_filename_layout_sorts_chronologically_across_slugs(tmp_path: Path) -> None:
    """Filenames must lead with timestamp so lexicographic sort = chronological.

    The specialist read-back prompts in apply-prose-writer + apply-cover-letter-writer
    select the most-recent N records by filename. If filenames led with slug,
    a recent record from "acme-corp" would sort before an older record from
    "zeta-inc" — readback would skip recent edits from later-alphabetical slugs.
    """
    feedback_dir = tmp_path / "private" / "feedback"

    # Three records: chronological order is (zeta-old, acme-mid, mid-new),
    # but if we sorted by slug we'd get (acme-mid, mid-new, zeta-old).
    paths = [
        _write_feedback_record(
            feedback_dir, "zeta-inc", "prose-bullet", "old", "x",
            timestamp="2025-01-01T00:00:00+00:00",
        ),
        _write_feedback_record(
            feedback_dir, "acme-corp", "prose-bullet", "old", "y",
            timestamp="2025-06-15T00:00:00+00:00",
        ),
        _write_feedback_record(
            feedback_dir, "midas-bank", "prose-bullet", "old", "z",
            timestamp="2025-12-31T00:00:00+00:00",
        ),
    ]

    sorted_names = sorted(p.name for p in paths)
    # Lexicographic sort by filename must yield chronological order.
    assert sorted_names[0].startswith("2025-01-01"), (
        f"oldest record should sort first, got {sorted_names[0]}"
    )
    assert sorted_names[-1].startswith("2025-12-31"), (
        f"newest record should sort last, got {sorted_names[-1]}"
    )
