"""apply.py — deprecation shim for the jobsmith apply pipeline.

.. deprecated:: 0.8.5
    This module is a re-export shim. Import from the canonical locations:

    * ``jobsmith.core.slug`` — :func:`derive_slug`, :func:`reconcile_canonical_slug`
    * ``jobsmith.core.paths`` — :func:`apply_state_dir`, :func:`pipeline_db_path`
    * ``jobsmith.core.session`` — :func:`get_or_create_session_id`
    * ``jobsmith.core.manifest`` — :func:`load_manifest`, :func:`phase_completed`
    * ``jobsmith.core.url_index`` — :func:`load_url_index`, :func:`record_url_mapping`
    * ``jobsmith.core.pipeline`` — :func:`build_phase_prompt`, :func:`run_phase_iter`
    * ``jobsmith.core.events`` — :class:`PipelineEvent`
    * ``jobsmith._cli_apply`` — :func:`run_apply`, :func:`dual_write_phase_artifacts`
    * ``jobsmith._init`` — :func:`ensure_bootstrap`

    Planned for deletion in 0.8.6 once all callers migrate.
"""
from __future__ import annotations

import logging
from pathlib import Path

import click

from . import headless  # noqa: F401 — re-exported for monkeypatch in tests
from . import plugin_dir as get_plugin_dir  # noqa: F401 — re-exported for tests
from .config import CONFIG_FILENAME
from .guard import check_anchors  # noqa: F401 — re-exported for monkeypatch in tests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core re-exports — slug, paths, session, manifest, url_index, pipeline
# ---------------------------------------------------------------------------

from jobsmith.core.events import PipelineEvent  # noqa: E402,F401
from jobsmith.core.slug import (  # noqa: E402,F401
    _slugify_part,
    derive_slug,
    reconcile_canonical_slug,
    resolve_canonical_slug,
)

# Back-compat alias — old code calls _reconcile_canonical_slug
_reconcile_canonical_slug = reconcile_canonical_slug

from jobsmith.core.paths import (  # noqa: E402,F401
    applications_dir,
    apply_state_dir,
    build_paths,
    pipeline_db_path,
)

# Back-compat aliases — old code uses leading-underscore names
_apply_state_dir = apply_state_dir
_applications_dir = applications_dir
_build_paths = build_paths
_pipeline_db_path = pipeline_db_path

from jobsmith.core.session import (  # noqa: E402,F401
    claude_session_file_path,
    get_or_create_session_id,
)

# Back-compat aliases — old code uses leading-underscore names
_claude_session_file_path = claude_session_file_path
_get_or_create_session_id = get_or_create_session_id

from jobsmith.core.manifest import (  # noqa: E402,F401
    PHASE_REQUIRED_SPECIALISTS,
    load_manifest,
    phase_completed,
)

# Back-compat aliases — old code uses leading-underscore names
_load_manifest = load_manifest
_phase_completed = phase_completed
_PHASE_REQUIRED_SPECIALISTS = PHASE_REQUIRED_SPECIALISTS

from jobsmith.core.url_index import (  # noqa: E402,F401
    URL_INDEX_FILENAME,
    load_url_index,
    record_url_mapping,
    resolve_starting_slug,
    save_url_index,
    scan_for_url_match,
)

# Back-compat aliases — old code uses leading-underscore names
_load_url_index = load_url_index
_record_url_mapping = record_url_mapping
_resolve_starting_slug = resolve_starting_slug
_save_url_index = save_url_index
_scan_for_url_match = scan_for_url_match

from jobsmith.core.pipeline import (  # noqa: E402,F401
    _PHASE_MAX_TURNS,
    _PHASES,
    _auto_freeze_contracts,
    _db_now_iso,
    _open_pipeline_db_for_run,
    _snapshot_phase_drafts,
    build_phase_prompt,
    core_run_apply,
    run_phase_iter,
)

# ---------------------------------------------------------------------------
# Private module re-exports — back-compat aliases so test patches keep working
# ---------------------------------------------------------------------------

from jobsmith._init import _run_init  # noqa: E402,F401

from jobsmith._cli_apply import (  # noqa: E402,F401
    _build_client_if_enabled,
    _coerce_to_dict,
    _dual_write_enabled,
    _render_event,
    _run_apply_phases,
    _run_step45_orchestration,
    dual_write_phase_artifacts,
    run_apply,
)

# ---------------------------------------------------------------------------
# Bootstrap check — calls _run_init via this module's namespace so that
# patch("jobsmith.apply._run_init") works in tests.
# ---------------------------------------------------------------------------


def ensure_bootstrap(cwd: Path) -> None:
    """Ensure `.apply-config.yaml` exists in *cwd*, auto-calling `jobsmith init` if not.

    This is the programmatic path: imports and calls the init logic directly
    without spawning a subprocess, so it shares the same code path as
    `jobsmith init`.  Idempotent — no-op when already bootstrapped.

    Parameters
    ----------
    cwd:
        Directory to check/initialize (typically the current working directory).
    """
    config_path = cwd / CONFIG_FILENAME
    if config_path.exists():
        return

    click.echo(
        f"No {CONFIG_FILENAME} found in {cwd}. Auto-running `jobsmith init`...",
        err=True,
    )
    # Call _run_init via this module's namespace so that
    # patch("jobsmith.apply._run_init") propagates correctly in tests.
    _run_init(cwd)


# ---------------------------------------------------------------------------
# Public helpers — kept here (not relocated) to avoid churn on stable API
# ---------------------------------------------------------------------------


def required_specialists_for_phase(phase: str) -> tuple[str, ...]:
    """Return the tuple of specialist slugs required to mark *phase* complete.

    Public accessor on top of the private ``_PHASE_REQUIRED_SPECIALISTS``
    map so other modules (db_ingest backfill, slice-8 re-runs) can ask
    "did this phase finish?" without depending on apply.py internals.
    Returns an empty tuple for unknown phases.
    """
    return _PHASE_REQUIRED_SPECIALISTS.get(phase, ())


def phase_for_specialist(specialist_name: str) -> str:
    """Return the phase name that contains *specialist_name*.

    Parameters
    ----------
    specialist_name:
        A specialist slug such as ``"apply-fit-scorer"`` or
        ``"apply-prose-writer"``.

    Returns
    -------
    str
        One of ``"gather"``, ``"draft"``, or ``"render"``.

    Raises
    ------
    ValueError
        When *specialist_name* is not found in any phase.
    """
    for phase, specialists in _PHASE_REQUIRED_SPECIALISTS.items():
        if specialist_name in specialists:
            return phase
    raise ValueError(f"unknown specialist: {specialist_name!r}")
