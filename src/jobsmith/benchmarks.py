"""Benchmark resolution helpers for the quality-benchmarks track.

Resolves benchmark file paths, falling back to the generic Pat Doe files
shipped inside the plugin when user paths are not configured.
"""

from __future__ import annotations

from pathlib import Path

from .config import JobsmithConfig

# Maps logical field names to the Pat Doe fallback filenames inside the
# plugin's benchmarks/ subdirectory.
_FALLBACK_FILES: dict[str, str] = {
    "resume_pdf": "resume.pdf",
    "resume_qmd": "resume.qmd",
    "cover_letter_md": "cover-letter.md",
    "cover_letter_pdf": "cover-letter.pdf",
    "workflow_html": "workflow.html",
}


class BenchmarkRequiredError(ValueError):
    """Raised when a benchmark field is required but not configured."""


def _plugin_benchmarks_dir() -> Path:
    """Return the path to the bundled Pat Doe benchmark files."""
    import jobsmith

    return jobsmith.plugin_dir() / "benchmarks"


def resolve_benchmark_or_fallback(
    field: str,
    config: JobsmithConfig,
    repo_root: Path,
) -> Path:
    """Return the path to a benchmark file, falling back to Pat Doe if unset.

    Parameters
    ----------
    field:
        One of ``"resume_qmd"``, ``"resume_pdf"``, ``"cover_letter_md"``,
        ``"cover_letter_pdf"``, ``"workflow_html"``.
    config:
        The loaded ``JobsmithConfig`` (benchmarks section is read from here).
    repo_root:
        The directory containing ``.apply-config.yaml``; used to resolve
        relative user paths.

    Returns
    -------
    Path
        Resolved absolute path to the benchmark file.

    Raises
    ------
    BenchmarkRequiredError
        When ``config.benchmarks.required`` is ``True`` and the user has not
        set the requested field.
    ValueError
        When *field* is not a recognised benchmark field name.
    """
    if field not in _FALLBACK_FILES:
        valid = ", ".join(sorted(_FALLBACK_FILES))
        raise ValueError(
            f"Unknown benchmark field {field!r}. Valid fields: {valid}"
        )

    user_value: Path | None = getattr(config.benchmarks, field)

    if user_value is not None:
        # Resolve relative paths against repo root; keep absolute paths as-is.
        if user_value.is_absolute():
            return user_value
        return (repo_root / user_value).resolve()

    # User has not configured this field.
    if config.benchmarks.required:
        raise BenchmarkRequiredError(
            f"Benchmark field '{field}' is not configured and "
            "benchmarks.required is true. Populate private/benchmarks/ with "
            "your reference files, or set required: false."
        )

    # Fall back to the Pat Doe file shipped with the plugin.
    fallback = _plugin_benchmarks_dir() / _FALLBACK_FILES[field]
    return fallback


def count_user_benchmarks(config: JobsmithConfig) -> int:
    """Return the number of benchmark fields the user has explicitly set."""
    bm = config.benchmarks
    fields = ["resume_pdf", "resume_qmd", "cover_letter_md", "cover_letter_pdf", "workflow_html"]
    return sum(1 for f in fields if getattr(bm, f) is not None)


__all__ = [
    "BenchmarkRequiredError",
    "count_user_benchmarks",
    "resolve_benchmark_or_fallback",
]
