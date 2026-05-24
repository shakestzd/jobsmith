"""jobsmith.onboard.parsers — ingest specialists for onboarding (feat-bd145368).

Public entry points
-------------------
run_ingestion(state_dir, repo_root, *, resume_file, linkedin_export,
              linkedin_url, paste, paste_file) -> int
    Top-level orchestrator called by both pipeline entry points.
    Writes candidate-*.json + provenance-*.json into state_dir.
    Returns 0 on success, 1 on non-fatal partial failure.

Individual specialists (for testing/direct use)
-------------------------------------------------
ingest_resume(path, state_dir, *, llm_call) -> dict
ingest_linkedin_export(path, state_dir, *, llm_call) -> dict
ingest_linkedin_url(url, state_dir, *, llm_call) -> dict
ingest_paste(text, state_dir, *, llm_call) -> dict
"""

from .ingest import (
    ingest_linkedin_export,
    ingest_linkedin_url,
    ingest_paste,
    ingest_resume,
    run_ingestion,
)

__all__ = [
    "run_ingestion",
    "ingest_resume",
    "ingest_linkedin_export",
    "ingest_linkedin_url",
    "ingest_paste",
]
