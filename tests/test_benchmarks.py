"""Tests for jobsmith.benchmarks — resolve_benchmark_or_fallback and check_benchmarks."""

from __future__ import annotations

from pathlib import Path

import pytest

import jobsmith
from jobsmith.benchmarks import (
    BenchmarkRequiredError,
    count_user_benchmarks,
    resolve_benchmark_or_fallback,
)
from jobsmith.config import BenchmarkConfig, JobsmithConfig
from jobsmith.doctor import check_benchmarks

# ---------------------------------------------------------------------------
# resolve_benchmark_or_fallback
# ---------------------------------------------------------------------------

def _config(required: bool = False, **kwargs: Path | None) -> JobsmithConfig:
    """Build a JobsmithConfig with given benchmark fields."""
    return JobsmithConfig(benchmarks=BenchmarkConfig(required=required, **kwargs))


def test_returns_user_path_when_set(tmp_path: Path) -> None:
    user_file = tmp_path / "private" / "benchmarks" / "resume.qmd"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("# my resume\n")

    config = _config(resume_qmd=Path("private/benchmarks/resume.qmd"))
    result = resolve_benchmark_or_fallback("resume_qmd", config, tmp_path)
    assert result == user_file.resolve()


def test_returns_absolute_user_path_unchanged(tmp_path: Path) -> None:
    abs_file = tmp_path / "abs_resume.qmd"
    abs_file.write_text("# abs\n")

    config = _config(resume_qmd=abs_file)
    result = resolve_benchmark_or_fallback("resume_qmd", config, tmp_path)
    assert result == abs_file


def test_returns_pat_doe_fallback_when_not_required(tmp_path: Path) -> None:
    config = _config(required=False)
    result = resolve_benchmark_or_fallback("resume_qmd", config, tmp_path)
    plugin_dir = jobsmith.plugin_dir()
    assert result == plugin_dir / "benchmarks" / "resume.qmd"


def test_raises_on_required_and_unset(tmp_path: Path) -> None:
    config = _config(required=True)
    with pytest.raises(BenchmarkRequiredError, match="resume_qmd"):
        resolve_benchmark_or_fallback("resume_qmd", config, tmp_path)


def test_raises_on_unknown_field(tmp_path: Path) -> None:
    config = _config()
    with pytest.raises(ValueError, match="Unknown benchmark field"):
        resolve_benchmark_or_fallback("nonexistent_field", config, tmp_path)


def test_all_valid_fields_fall_back_silently(tmp_path: Path) -> None:
    """Every recognised field returns either a real Path or None — never raises and
    never returns a path to a non-existent file. Markdown fallbacks are
    bundled (resume.qmd, cover-letter.md); PDFs and HTML aren't, and those
    return None when the user hasn't configured them."""
    config = _config(required=False)
    fields = ["resume_pdf", "resume_qmd", "cover_letter_md", "cover_letter_pdf", "workflow_html"]
    for field in fields:
        result = resolve_benchmark_or_fallback(field, config, tmp_path)
        assert result is None or isinstance(result, Path)
        if isinstance(result, Path):
            assert result.exists(), (
                f"{field} fell back to {result!r} which does not exist; "
                "resolver must return None when the shipped fallback is missing"
            )


# ---------------------------------------------------------------------------
# Pat Doe fallback files exist and are non-empty
# ---------------------------------------------------------------------------

def test_pat_doe_resume_qmd_exists_and_nonempty() -> None:
    p = jobsmith.plugin_dir() / "benchmarks" / "resume.qmd"
    assert p.exists(), f"Pat Doe resume.qmd not found at {p}"
    assert len(p.read_text()) > 100


def test_pat_doe_cover_letter_md_exists_and_nonempty() -> None:
    p = jobsmith.plugin_dir() / "benchmarks" / "cover-letter.md"
    assert p.exists(), f"Pat Doe cover-letter.md not found at {p}"
    assert len(p.read_text()) > 100


def test_pat_doe_readme_exists() -> None:
    p = jobsmith.plugin_dir() / "benchmarks" / "README.md"
    assert p.exists(), f"Pat Doe README.md not found at {p}"
    assert len(p.read_text()) > 50


# ---------------------------------------------------------------------------
# count_user_benchmarks
# ---------------------------------------------------------------------------

def test_count_user_benchmarks_zero_when_all_none() -> None:
    config = _config()
    assert count_user_benchmarks(config) == 0


def test_count_user_benchmarks_counts_set_fields(tmp_path: Path) -> None:
    config = _config(
        resume_qmd=Path("private/benchmarks/resume.qmd"),
        cover_letter_md=Path("private/benchmarks/cover-letter.md"),
    )
    assert count_user_benchmarks(config) == 2


# ---------------------------------------------------------------------------
# check_benchmarks (doctor check)
# ---------------------------------------------------------------------------

def _scaffold_config(root: Path, benchmarks_yaml: str = "") -> Path:
    """Write a minimal .apply-config.yaml, optionally with a benchmarks block."""
    cfg = root / ".apply-config.yaml"
    content = "master:\n  work_yml: assets/content/work.yml\n"
    if benchmarks_yaml:
        content += f"\nbenchmarks:\n{benchmarks_yaml}\n"
    cfg.write_text(content)
    return cfg


def test_check_benchmarks_no_config_is_warn_pass(tmp_path: Path) -> None:
    """No config present → PASS (skip/warn) with Pat Doe fallback message."""
    result = check_benchmarks(cwd=tmp_path)
    assert result.ok is True
    assert result.name == "benchmarks"
    assert "Pat Doe" in result.message


def test_check_benchmarks_missing_not_required_is_pass(tmp_path: Path) -> None:
    """Config exists, benchmarks section absent → PASS with fallback message."""
    _scaffold_config(tmp_path)
    result = check_benchmarks(cwd=tmp_path)
    assert result.ok is True
    assert "Pat Doe" in result.message or "0 of 5" in result.message


def test_check_benchmarks_required_and_missing_is_fail(tmp_path: Path) -> None:
    """required=true, no user files set → FAIL."""
    _scaffold_config(tmp_path, "  required: true\n")
    result = check_benchmarks(cwd=tmp_path)
    assert result.ok is False
    assert result.remediation is not None
    assert "required: false" in result.remediation


def test_check_benchmarks_all_set_and_present_is_pass(tmp_path: Path) -> None:
    """All 5 user benchmark files set and present on disk → PASS 5/5."""
    bm_dir = tmp_path / "private" / "benchmarks"
    bm_dir.mkdir(parents=True)
    for fname in ("resume.qmd", "resume.pdf", "cover-letter.md", "cover-letter.pdf", "workflow.html"):
        (bm_dir / fname).write_text(f"# {fname}\n")

    _scaffold_config(
        tmp_path,
        "  resume_qmd: private/benchmarks/resume.qmd\n"
        "  resume_pdf: private/benchmarks/resume.pdf\n"
        "  cover_letter_md: private/benchmarks/cover-letter.md\n"
        "  cover_letter_pdf: private/benchmarks/cover-letter.pdf\n"
        "  workflow_html: private/benchmarks/workflow.html\n"
        "  required: false\n",
    )
    result = check_benchmarks(cwd=tmp_path)
    assert result.ok is True
    assert "5/5" in result.message


def test_check_benchmarks_partial_set_with_required_false(tmp_path: Path) -> None:
    """2 of 5 set, required=false → PASS with fallback message."""
    bm_dir = tmp_path / "private" / "benchmarks"
    bm_dir.mkdir(parents=True)
    (bm_dir / "resume.qmd").write_text("# resume\n")
    (bm_dir / "cover-letter.md").write_text("# letter\n")

    _scaffold_config(
        tmp_path,
        "  resume_qmd: private/benchmarks/resume.qmd\n"
        "  cover_letter_md: private/benchmarks/cover-letter.md\n"
        "  required: false\n",
    )
    result = check_benchmarks(cwd=tmp_path)
    assert result.ok is True
    assert "2 of 5" in result.message or "Pat Doe" in result.message
