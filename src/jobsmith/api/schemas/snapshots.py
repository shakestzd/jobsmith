"""Pydantic schemas for the DB→FS snapshot endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SnapshotRequest(BaseModel):
    """Optional body for POST .../snapshot.

    Omit entirely for a full snapshot of all artifacts in the run.
    """

    kinds: list[str] | None = Field(
        default=None,
        description=(
            "Artifact kinds to snapshot. When None, all artifacts in the run "
            "are written. Example: ['jd-parsed', 'fit-score']."
        ),
    )
    target: Literal["apply-state", "slug-root", "both"] = Field(
        default="both",
        description=(
            "Which directory tree(s) to write to. "
            "'apply-state' → <slug>/.apply-state/ only. "
            "'slug-root' → <slug>/ root only. "
            "'both' → both (default)."
        ),
    )


class SnapshotFile(BaseModel):
    """Metadata for a single file written by the snapshot endpoint."""

    path: str
    """Absolute path of the written file."""

    kind: str
    """Artifact kind that produced this file."""

    bytes_written: int
    """Number of bytes written to disk."""


class SnapshotResult(BaseModel):
    """Response body returned by POST .../snapshot."""

    slug: str
    run_id: str
    files: list[SnapshotFile]
    total_bytes: int

    @classmethod
    def from_files(
        cls, slug: str, run_id: str, files: list[SnapshotFile]
    ) -> SnapshotResult:
        return cls(
            slug=slug,
            run_id=run_id,
            files=files,
            total_bytes=sum(f.bytes_written for f in files),
        )


__all__ = ["SnapshotFile", "SnapshotRequest", "SnapshotResult"]
