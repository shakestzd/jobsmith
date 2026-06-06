"""jobsmith.reuse.metrics — per-run metric persistence helpers (slice-9).

Records model-call counts, wall-clock (real or simulated), and per-candidate
reused-vs-generated tallies into the ``run_metrics`` table.

Metric keys written
-------------------
``run.model_call_count``       — total model invocations for the run (int as str)
``run.wall_clock_seconds``     — elapsed seconds for the run (float str, 4dp)
``candidate.{slug}.source``    — "reused" | "generated" per candidate
``candidate.reused_count``     — total candidates whose artifacts were reused
``candidate.generated_count``  — total candidates whose artifacts were regenerated

Public API
----------
``RunMetrics``              — mutable accumulator; pass one per pipeline run
``record_run_metrics``      — persist a RunMetrics snapshot to ``run_metrics``
``read_run_metrics_summary``— read back a plain dict from ``run_metrics`` rows
"""
from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jobsmith.reuse.store import get_run_metrics, upsert_run_metric

# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


@dataclass
class RunMetrics:
    """Mutable accumulator for a single pipeline run.

    Usage
    -----
    Create one instance before the pipeline run, call helper methods as phases
    complete, then call ``record_run_metrics(conn, slug, metrics)`` at the end.
    """

    model_call_count: int = 0
    wall_clock_seconds: float = 0.0
    # Per-candidate source tracking: slug → "reused" | "generated"
    candidate_sources: dict[str, str] = field(default_factory=dict)

    def increment_model_calls(self, n: int = 1) -> None:
        """Add *n* to the model-call counter."""
        self.model_call_count += n

    def add_wall_clock(self, seconds: float) -> None:
        """Add *seconds* to the accumulated wall-clock total."""
        self.wall_clock_seconds += seconds

    def record_candidate(self, slug: str, source: str) -> None:
        """Record whether a candidate's artifacts were ``"reused"`` or ``"generated"``."""
        self.candidate_sources[slug] = source

    @property
    def reused_count(self) -> int:
        """Number of candidates whose artifacts were reused."""
        return sum(1 for v in self.candidate_sources.values() if v == "reused")

    @property
    def generated_count(self) -> int:
        """Number of candidates whose artifacts were generated."""
        return sum(1 for v in self.candidate_sources.values() if v == "generated")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def record_run_metrics(
    conn: sqlite3.Connection,
    slug: str,
    metrics: RunMetrics,
) -> None:
    """Persist *metrics* snapshot to ``run_metrics`` for *slug*.

    All values are stored as strings in ``metric_value``.  A second call
    for the same slug/key overwrites the previous value (INSERT OR REPLACE).

    Parameters
    ----------
    conn:
        Open SQLite connection to the pipeline DB.
    slug:
        Application slug identifying this run.
    metrics:
        The :class:`RunMetrics` accumulator to persist.
    """
    upsert_run_metric(
        conn,
        slug=slug,
        metric_key="run.model_call_count",
        metric_value=str(metrics.model_call_count),
    )
    upsert_run_metric(
        conn,
        slug=slug,
        metric_key="run.wall_clock_seconds",
        metric_value=f"{metrics.wall_clock_seconds:.4f}",
    )
    upsert_run_metric(
        conn,
        slug=slug,
        metric_key="candidate.reused_count",
        metric_value=str(metrics.reused_count),
    )
    upsert_run_metric(
        conn,
        slug=slug,
        metric_key="candidate.generated_count",
        metric_value=str(metrics.generated_count),
    )
    for candidate_slug, source in metrics.candidate_sources.items():
        upsert_run_metric(
            conn,
            slug=slug,
            metric_key=f"candidate.{candidate_slug}.source",
            metric_value=source,
        )


# ---------------------------------------------------------------------------
# Read-back
# ---------------------------------------------------------------------------


def read_run_metrics_summary(
    conn: sqlite3.Connection,
    slug: str,
) -> dict[str, Any]:
    """Return a plain dict of all ``run_metrics`` rows for *slug*.

    Keys are ``metric_key``, values are ``metric_value`` strings.
    Numeric keys (``run.model_call_count``, ``run.wall_clock_seconds``,
    ``candidate.reused_count``, ``candidate.generated_count``) are coerced
    to their native Python types for convenience.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    slug:
        Application slug to read.

    Returns
    -------
    dict[str, Any]
        ``{metric_key: coerced_value}``.  Empty dict when no rows found.
    """
    rows = get_run_metrics(conn, slug=slug)
    summary: dict[str, Any] = {}
    int_keys = {"run.model_call_count", "candidate.reused_count", "candidate.generated_count"}
    float_keys = {"run.wall_clock_seconds"}
    for row in rows:
        key = row["metric_key"]
        val: Any = row["metric_value"]
        if key in int_keys:
            with contextlib.suppress(ValueError, TypeError):
                val = int(val)
        elif key in float_keys:
            with contextlib.suppress(ValueError, TypeError):
                val = float(val)
        summary[key] = val
    return summary


__all__ = [
    "RunMetrics",
    "record_run_metrics",
    "read_run_metrics_summary",
]
