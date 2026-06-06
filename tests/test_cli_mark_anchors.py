"""Tests for jobsmith mark-anchors interactive CLI (Slice A.1 — feat-beb6becf)."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Fixture YAML with a leading comment, mixed string + dict bullets, and
# specific indentation we want round-tripped intact.
SAMPLE_WORK_YML = """\
# Sample work history.
# Each position has title, location, date, details.

- title: "Engineer"
  location: "TestCo"
  date: "Jan 2022 - Present"
  description: "Remote"
  details:
    - "Built a tool that cut latency by 40%"
    - "Wrote internal docs read by 10 teams"
    - bullet: "Refactored the build pipeline"
      anchor: true
      anchor_reason: "Marked anchor by user previously"

- title: "Junior Engineer"
  location: "PriorCo"
  date: "Jan 2020 - Dec 2021"
  description: "Hybrid"
  details:
    - "Shipped onboarding flow used by 200 customers"
"""


def _run(cwd: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Run jobsmith mark-anchors. Invoked from project root (where uv venv lives).

    Pass --master with an absolute path. The ``cwd`` argument is ignored
    for file resolution because we always invoke from project root, but the
    --batch output uses master.parent so it lands beside the fixture in tmp_path.
    """
    project_root = Path(__file__).parent.parent
    return subprocess.run(
        ["uv", "run", "jobsmith", "mark-anchors", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        input=stdin,
    )


def test_mark_anchors_missing_master_returns_nonzero(tmp_path: Path) -> None:
    """Non-existent master file → exit 2."""
    result = _run(tmp_path, "--master", str(tmp_path / "nope.yml"))
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "not found" in combined.lower()


def test_mark_anchors_dry_run_emits_diff_and_does_not_write(tmp_path: Path) -> None:
    """--dry-run scripts 'a, reason, q' through the first bullet only — diff printed, file unchanged."""
    work = tmp_path / "work.yml"
    work.write_text(SAMPLE_WORK_YML)
    original = work.read_text()

    # Script: mark first bullet as anchor with reason, then quit
    stdin = "a\nKey ops win\nq\n"
    result = _run(tmp_path, "--master", str(work), "--dry-run", stdin=stdin)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Diff printed
    assert "+" in result.stdout and "-" in result.stdout
    # File unchanged
    assert work.read_text() == original


def test_mark_anchors_skip_already_annotated_unless_force(tmp_path: Path) -> None:
    """Bullets already in object form with explicit anchor are skipped without --force."""
    work = tmp_path / "work.yml"
    work.write_text(SAMPLE_WORK_YML)

    # Walk: first bullet (string-form) skip, second bullet (string) skip,
    # third bullet (dict-form) should be SKIPPED automatically (already annotated)
    # so we hit the next position's bullet — skip that — done.
    # Without --force: 3 prompts (string1, string2, junior_string). Without --force the
    # dict-form bullet should NOT be re-prompted.
    stdin = "s\ns\ns\n"
    result = _run(tmp_path, "--master", str(work), "--dry-run", stdin=stdin)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    # No changes were made — so we should see the "no changes" message
    assert "no changes" in result.stdout.lower()


def test_mark_anchors_writes_file_and_preserves_comments(tmp_path: Path) -> None:
    """Apply: scripts mark first bullet as anchor; file written with comments intact."""
    work = tmp_path / "work.yml"
    work.write_text(SAMPLE_WORK_YML)

    # Mark first bullet as anchor with a reason, then quit-and-save
    stdin = "a\nLargest impact bullet\nq\n"
    result = _run(tmp_path, "--master", str(work), stdin=stdin)

    assert result.returncode == 0, f"stderr: {result.stderr}"
    written = work.read_text()
    # Original leading comments preserved
    assert "# Sample work history." in written
    assert "# Each position has title, location, date, details." in written
    # First bullet now in object form with anchor + reason
    assert "anchor: true" in written
    assert "Largest impact bullet" in written
    # Second bullet (string form, was skipped via quit) should still be a string
    assert '- "Wrote internal docs read by 10 teams"' in written or \
           "- Wrote internal docs read by 10 teams" in written


def test_mark_anchors_batch_writes_todo_file(tmp_path: Path) -> None:
    """--batch writes bullet-anchor-todo.md without prompting."""
    work = tmp_path / "work.yml"
    work.write_text(SAMPLE_WORK_YML)

    result = _run(tmp_path, "--master", str(work), "--batch")

    assert result.returncode == 0, f"stderr: {result.stderr}"
    todo = tmp_path / "bullet-anchor-todo.md"
    assert todo.exists()
    todo_text = todo.read_text()
    # Each position is a heading
    assert "## 0. Engineer @ TestCo" in todo_text
    assert "## 1. Junior Engineer @ PriorCo" in todo_text
    # Each string-form bullet appears with [ ] checkbox
    assert "[ ]" in todo_text
    # The pre-annotated bullet shows [a] (already marked anchor)
    assert "[a]" in todo_text
