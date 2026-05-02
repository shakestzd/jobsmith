"""Tests for jobsmith lint subcommand.

TDD: written before the implementation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_lint(*args: str) -> subprocess.CompletedProcess:
    """Run `uv run jobsmith lint <args>` and return the result."""
    return subprocess.run(
        ["uv", "run", "jobsmith", "lint", *args],
        capture_output=True,
        text=True,
    )


def _write_valid_work_yml(path: Path) -> None:
    """Write a minimal valid work.yml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "title": "Software Engineer",
            "location": "Acme Corp",
            "date": "Jan 2022 - Present",
            "description": "Remote",
            "details": [
                "Reduced onboarding time by 40% via automated tooling",
                "Built internal SDK used by 200+ developers",
            ],
        }
    ]
    path.write_text(yaml.safe_dump(data))


VALID_QMD = """\
---
title: "Test Resume"
---

## Experience

### Software Engineer

- Reduced onboarding time by 40% via automated tooling
- Built internal SDK used by 200+ developers
"""

INVALID_QMD_NO_BULLETS = """\
---
title: "Empty Resume"
---

## Experience

Just some prose, no bullets.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lint_passes_on_valid_pat_doe_master() -> None:
    """jobsmith lint --master <examples/master-yaml> exits 0 on valid files."""
    result = _run_lint("--master", "examples/master-yaml")
    assert result.returncode == 0, (
        f"Expected exit 0 on valid master YAML, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_lint_fails_on_malformed_anchor(tmp_path: Path) -> None:
    """work.yml with anchor: 'maybe' (invalid bool) → exits non-zero with file:line."""
    master_dir = tmp_path / "master"
    master_dir.mkdir()

    # Write an invalid work.yml — root is a dict, not a list
    bad_work = master_dir / "work.yml"
    bad_work.write_text("anchor: maybe\ntitle: bad\n")

    result = _run_lint("--master", str(master_dir))
    assert result.returncode != 0, (
        f"Expected non-zero exit on malformed YAML, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    # Should include the filename in the error output
    assert "work.yml" in combined, f"Expected filename in error output: {combined}"


def test_lint_fails_on_malformed_benchmark(tmp_path: Path) -> None:
    """A benchmark qmd with no bullets → exits non-zero."""
    master_dir = tmp_path / "master"
    master_dir.mkdir()

    # A valid work.yml so master validates fine
    _write_valid_work_yml(master_dir / "work.yml")

    bad_qmd = tmp_path / "resume.qmd"
    bad_qmd.write_text(INVALID_QMD_NO_BULLETS)

    result = _run_lint("--master", str(master_dir), "--benchmark", str(bad_qmd))
    assert result.returncode != 0, (
        f"Expected non-zero exit on benchmark with no bullets, got {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "benchmark" in combined.lower() or "bullet" in combined.lower(), (
        f"Expected 'benchmark' or 'bullet' in error output: {combined}"
    )
