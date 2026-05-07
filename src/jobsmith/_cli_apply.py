"""jobsmith._cli_apply — CLI-coupled phase orchestration for `jobsmith apply`.

Extracted from ``jobsmith.apply`` as part of trk-ad6d8227 (slice 6).
``jobsmith.apply`` re-exports the public names (``run_apply``,
``dual_write_phase_artifacts``) and provides back-compat aliases for the
private names (``_run_apply_phases``, ``_run_step45_orchestration``, etc.)
so that test patches via ``patch("jobsmith.apply._X")`` keep working.

Back-compat patching note
-------------------------
Several tests patch ``jobsmith.apply._run_apply_phases`` or
``jobsmith.apply._run_step45_orchestration`` and then invoke ``run_apply``
to stub out behaviour.  To preserve that capability after the relocation,
functions look up their patchable dependencies through
``sys.modules['jobsmith.apply']`` at runtime rather than from this module's
own namespace:

* ``run_apply``'s ``_phase_runner`` closure calls ``_run_apply_phases``
  through the apply-shim namespace so that patching
  ``jobsmith.apply._run_apply_phases`` takes effect.
* ``_run_apply_phases`` calls ``_run_step45_orchestration`` through the
  apply-shim namespace so that patching
  ``jobsmith.apply._run_step45_orchestration`` takes effect.

This relies on the fact that by the time any of these callables are invoked
both ``jobsmith.apply`` and ``jobsmith._cli_apply`` are fully loaded, making
the ``sys.modules`` lookup safe.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading  # noqa: F401 — referenced in optional cancel_event annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from . import headless
from . import plugin_dir as get_plugin_dir
from ._state_readers import ARTIFACT_READERS
from .config import find_config, load_config
from .guard import check_anchors
from .paths import resolve
from .render import ApplyRenderer

if TYPE_CHECKING:
    from .api.client import JobsmithClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Patch-resolution helper
# ---------------------------------------------------------------------------


def _resolve_from_apply(name: str, fallback: object) -> object:
    """Return ``getattr(jobsmith.apply, name)`` when the shim is loaded, else *fallback*.

    Used by ``run_apply`` and ``_run_apply_phases`` to look up their
    patchable dependencies through the ``jobsmith.apply`` namespace at
    call-time rather than at import-time, so that monkeypatches on
    ``jobsmith.apply._X`` propagate correctly even though the
    implementation now lives here.
    """
    apply_mod = sys.modules.get("jobsmith.apply")
    if apply_mod is None:
        return fallback
    return getattr(apply_mod, name, fallback)


# ---------------------------------------------------------------------------
# Legacy dual-write helpers
# ---------------------------------------------------------------------------


def _dual_write_enabled() -> bool:
    """Return True only when JOBSMITH_DUAL_WRITE=1 is explicitly set.

    Default flipped from "1" → "0" in S4 of trk-144d42b1 (feat-9b021f76).
    The DB ingest path is now primary; the legacy shadow-write hook only
    runs when JOBSMITH_DUAL_WRITE=1 is opted in.
    """
    return os.environ.get("JOBSMITH_DUAL_WRITE", "0") == "1"


def _build_client_if_enabled() -> JobsmithClient | None:
    """Construct a JobsmithClient for the dual-write hook, or None when disabled.

    Returns None when JOBSMITH_DUAL_WRITE=0 or when client construction fails
    (typically because no API token is resolvable in the supervisor's env).
    """
    if not _dual_write_enabled():
        return None
    try:
        from .api.client import JobsmithClient as _JsClient

        return _JsClient()
    except Exception as exc:  # noqa: BLE001 — never fail phase on client setup
        logger.warning("dual-write disabled — could not build JobsmithClient: %s", exc)
        return None


def _coerce_to_dict(payload: Any, kind: str) -> dict[str, Any]:
    """Wrap a non-dict artifact payload into the {text: ...} envelope expected
    by the API for text-only kinds."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return {"text": payload}
    return {"value": payload}


def dual_write_phase_artifacts(
    *,
    client: JobsmithClient,
    slug: str,
    run_id: str,
    state_dir: Path,
) -> None:
    """Mirror every loadable artifact in *state_dir* to the DB via *client*.

    Iterates ARTIFACT_READERS. For each reader that returns non-None, calls
    ``client.put_artifact(slug, run_id, kind, output)``. Wraps each PUT in
    try/except — failures log a WARNING but never raise (Phase 1: FS is still
    authoritative; DB is shadow).

    Skipped entirely when ``JOBSMITH_DUAL_WRITE=0``.
    """
    if not _dual_write_enabled():
        return

    for filename, (kind, reader) in ARTIFACT_READERS.items():
        try:
            payload = reader(state_dir)
        except Exception:  # noqa: BLE001 — readers must never fail the phase
            logger.warning(
                "dual-write reader failed for %s (kind=%s); skipping",
                filename,
                kind,
            )
            continue
        if payload is None:
            continue
        # Some readers return {} when the source file is absent — skip those too,
        # otherwise we'd PUT an empty payload for every kind on an empty state dir.
        if isinstance(payload, dict) and not payload:
            continue
        if isinstance(payload, str) and not payload:
            continue
        output = _coerce_to_dict(payload, kind)
        try:
            client.put_artifact(slug, run_id, kind, output)
        except Exception as exc:  # noqa: BLE001 — never fail phase on PUT error
            logger.warning(
                "dual-write PUT failed for kind=%s slug=%s run=%s: %s",
                kind,
                slug,
                run_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------


def _render_event(event: headless.Event) -> str | None:
    """Format a single event as a one-line summary for stderr output.

    Returns None for events that should be skipped (e.g. verbose text).
    """
    if event.type == "tool_use":
        short_input = ""
        if event.tool_input:
            # Grab the most informative key: command, path, url, query, or first key
            for key in ("command", "path", "url", "query"):
                val = event.tool_input.get(key)
                if val:
                    short_input = f"{key}={str(val)[:60]!r}"
                    break
            if not short_input:
                first_key = next(iter(event.tool_input), None)
                if first_key:
                    short_input = f"{first_key}={str(event.tool_input[first_key])[:40]!r}"
        return f"  -> {event.tool_name}({short_input})"

    if event.type == "tool_result":
        preview = (event.tool_result or "")[:80].replace("\n", " ")
        return f"  <- {preview}"

    if event.type == "error":
        return f"  x {event.error}"

    if event.type == "phase_complete":
        return f"  [ok] phase {event.name} complete"

    if event.type == "phase_failed":
        reason = f": {event.error}" if event.error else ""
        return f"  [fail] phase {event.name} failed{reason}"

    # Skip text events (too verbose) and other pass-through types
    return None


# ---------------------------------------------------------------------------
# Steps 4-5: between-phase anchor guard + relevance inquiry
# ---------------------------------------------------------------------------


def _run_step45_orchestration(slug: str, cwd: Path) -> int:
    """Run Steps 4-5 between gather (phase 1) and draft (phase 2).

    Step 4 (anchor guard): cross-references anchor bullets in the master
    work YAML against the gather phase's ``bullet-selection.json``. Anchor
    bullets dropped without a reason are reported.

    Step 5 (relevance inquiry): currently surfaces unresolved drops to the
    user with a remediation hint. Full automation of the relevance-inquiry
    sub-pipeline is a follow-up; for now this guarantees the draft phase
    cannot start with missing or stale ``bullet-decisions.json``.

    Writes ``bullet-decisions.json`` (an empty mapping when anchors are
    clean — selection-side ``reason_if_dropped`` carries any drop rationale).

    Returns
    -------
    int
        ``0`` on success, ``2`` when anchors require manual resolution, or
        ``1`` when prerequisite artifacts are missing.
    """
    import json as _json

    config_path = find_config(cwd)
    if config_path is None:
        click.echo(
            "Step 4-5: cannot locate .apply-config.yaml — skipping anchor guard.",
            err=True,
        )
        return 1
    config = load_config(config_path)
    repo_root = config_path.parent

    apply_state = (
        resolve(config.output.applications_dir, repo_root) / slug / ".apply-state"
    )
    selection_path = apply_state / "bullet-selection.json"
    decisions_path = apply_state / "bullet-decisions.json"
    master_path = resolve(config.master.work_yml, repo_root)

    if not selection_path.exists():
        click.echo(
            f"Step 4-5: bullet-selection.json missing at {selection_path}. "
            "Phase 1 (gather) must produce it before draft can proceed.",
            err=True,
        )
        return 1

    # Resolve check_anchors through apply's namespace to support test patches
    # like patch("jobsmith.apply.check_anchors", ...).
    _check_anchors_fn = _resolve_from_apply("check_anchors", check_anchors)
    result = _check_anchors_fn(master_path, selection_path)

    if result.exit_code == 0:
        # All anchors preserved (or dropped with reason via selection's
        # reason_if_dropped). Guarantee bullet-decisions.json exists so
        # the draft prompt's "MUST already exist" precondition is met.
        if not decisions_path.exists():
            apply_state.mkdir(parents=True, exist_ok=True)
            decisions_path.write_text(_json.dumps({}) + "\n")
        click.echo(
            f"Step 4: anchor guard passed — {len(result.kept)} kept, "
            f"{len(result.dropped_with_reason)} dropped with reason.",
            err=True,
        )
        return 0

    click.echo(
        f"Step 4: anchor guard FAILED — {len(result.dropped_without_reason)} "
        "anchor bullet(s) dropped without a reason:",
        err=True,
    )
    for bullet in result.dropped_without_reason[:5]:
        click.echo(f"  - {bullet.text[:100]}", err=True)
    if len(result.dropped_without_reason) > 5:
        click.echo(
            f"  ... and {len(result.dropped_without_reason) - 5} more", err=True
        )
    click.echo(
        "\nStep 5 (relevance inquiry) is not yet automated. Manually edit "
        f"{decisions_path} with reasons keyed by bullet_id, then re-invoke "
        "`jobsmith apply` to resume.",
        err=True,
    )
    return 2


# ---------------------------------------------------------------------------
# Phase loop
# ---------------------------------------------------------------------------


def _run_apply_phases(
    *,
    url: str,
    resolved_cwd: Path,
    rdr: ApplyRenderer,
    plugin_directory: Path,
    slug: str,
    apps_dir: Path | None,
    session_id: str,
    phase_done: dict[str, bool],
    total_phases: int,
    skip_confirm: bool,
    started_at: float,
    db_conn,
    db_run_id: str,
    db_slug_ref: list[str],
    db_status_ref: list[str],
    jd_text_file: Path | None = None,
    cancel_event: "threading.Event | None" = None,
) -> int:
    """Phase-loop body extracted so the surrounding ``run_apply`` can wrap it
    with the apply_runs DB lifecycle (insert before, UPDATE after, with the
    canonical slug reflected via ``db_slug_ref[0]``).

    ``db_status_ref`` (single-element list) is mutated to ``"cancelled"``
    when the user declines an inter-phase confirm gate, distinguishing
    that case from a full pipeline completion (``run_apply``'s finally
    maps ``rc=0 + status_ref="cancelled"`` to apply_runs.status=cancelled
    and ``rc=0 + status_ref!="cancelled"`` to status=done).

    All parameters are pre-resolved by ``run_apply``; this helper performs no
    bootstrap or slug resolution of its own.

    Back-compat patching: all patchable dependencies are resolved through
    ``jobsmith.apply``'s namespace at call-time so that
    ``patch.object(apply_mod, "_run_step45_orchestration", ...)`` and similar
    monkeypatches propagate into this function when called through the shim.
    """
    from jobsmith.core.paths import apply_state_dir as _apply_state_dir_core
    from jobsmith.core.paths import build_paths as _build_paths_core
    from jobsmith.core.paths import pipeline_db_path as _pipeline_db_path_core
    from jobsmith.core.pipeline import (
        _PHASE_MAX_TURNS,
        _PHASES,
        _auto_freeze_contracts,
        _snapshot_phase_drafts as _snapshot_phase_drafts_core,
        build_phase_prompt as _build_phase_prompt_core,
    )
    from jobsmith.core.session import get_or_create_session_id as _get_or_create_session_id_core
    from jobsmith.core.slug import reconcile_canonical_slug as _reconcile_canonical_slug_core
    from jobsmith.core.url_index import record_url_mapping as _record_url_mapping_core

    # Resolve all patchable dependencies through apply's namespace.
    # This ensures that patch.object(apply_mod, "_X", ...) propagates when
    # tests call _run_apply_phases via the jobsmith.apply back-compat alias.
    _apply_state_dir = _resolve_from_apply("_apply_state_dir", _apply_state_dir_core)
    _build_paths = _resolve_from_apply("_build_paths", _build_paths_core)
    _pipeline_db_path = _resolve_from_apply("_pipeline_db_path", _pipeline_db_path_core)
    _snapshot_phase_drafts = _resolve_from_apply("_snapshot_phase_drafts", _snapshot_phase_drafts_core)
    build_phase_prompt = _resolve_from_apply("build_phase_prompt", _build_phase_prompt_core)
    _get_or_create_session_id = _resolve_from_apply("_get_or_create_session_id", _get_or_create_session_id_core)
    _reconcile_canonical_slug = _resolve_from_apply("_reconcile_canonical_slug", _reconcile_canonical_slug_core)
    _record_url_mapping = _resolve_from_apply("_record_url_mapping", _record_url_mapping_core)
    _step45_fn = _resolve_from_apply("_run_step45_orchestration", _run_step45_orchestration)
    _build_client = _resolve_from_apply("_build_client_if_enabled", _build_client_if_enabled)
    _dual_write = _resolve_from_apply("dual_write_phase_artifacts", dual_write_phase_artifacts)

    # Auto-freeze specialist contracts on first apply (feat-385f3405).
    # Must run before the gather phase so the agent sees frozen_at non-null.
    _auto_freeze_contracts(
        plugin_directory / "agents" / "apply" / "specialist-contracts.yaml"
    )

    for phase_name, phase_num in _PHASES:
        # Step 3pre: just-in-time wrapper-owned prerequisites that must run
        # regardless of whether the prior phase ran or was skipped.  Step 4/5
        # produces ``bullet-decisions.json`` (a wrapper artifact, not a
        # specialist artifact); a prior run can mark all gather specialists
        # as ``ok`` but stop at the anchor guard, so we re-run step 4/5
        # before draft any time it is missing.
        if phase_name == "draft" and not phase_done["draft"]:
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None and not (state_dir / "bullet-decisions.json").exists():
                rc = _step45_fn(slug, resolved_cwd)
                if rc != 0:
                    return rc

        # Skip phases already marked complete in manifest.json.
        if phase_done[phase_name]:
            rdr.print_phase_skipped(phase_num, phase_name)
            continue

        # Finding 1 fix (Option A): each non-gather phase gets its own fresh
        # session at the phase boundary.  Deleting the persisted session-id
        # forces _get_or_create_session_id to mint a new uuid4.  Because the
        # new JSONL does not yet exist in ~/.claude/projects/, session_exists()
        # returns False → resume stays False → run_phase uses --session-id
        # (not --resume), giving every phase a clean turn budget.
        # For draft this runs after Step 3g has already reconciled the slug,
        # so the new ID is minted under the correct (canonical) app_dir.
        if phase_name != "gather" and apps_dir is not None:
            (apps_dir / slug / ".apply-state" / "session-id").unlink(
                missing_ok=True
            )
            session_id = _get_or_create_session_id(apps_dir / slug, resolved_cwd)

        # Step 3a: determine resume flag (Claude session continuity).
        # Invariant: session_id was freshly minted just above for phase 2/3,
        # so its JSONL does not yet exist in ~/.claude/projects/…
        # session_exists() therefore returns False, resume stays False, and
        # _build_command uses --session-id (claim a new session) rather than
        # --resume.  This gives each phase a clean turn budget and avoids
        # inheriting any prior phase's spent turns.
        resume = (phase_name != "gather") and headless.session_exists(
            session_id, cwd=resolved_cwd
        )

        # Step 3b: resolve system prompt path
        system_prompt = plugin_directory / "system-prompts" / f"phase-{phase_num}-{phase_name}.md"
        if not system_prompt.exists():
            rdr.print_error(f"ERROR: system prompt not found: {system_prompt}")
            return 1

        # Step 3c: build paths dict for this phase (uses current slug — after reconcile
        # for draft/render, slug may have changed from the URL-derived value).
        phase_paths = _build_paths(slug, resolved_cwd, plugin_directory)

        # Step 3d: build prompt text. jd_text_file is gather-only; pass
        # None for other phases so the kwarg propagates cleanly.
        prompt_text = build_phase_prompt(
            phase_name,
            slug,
            url,
            paths=phase_paths,
            jd_text_file=jd_text_file if phase_name == "gather" else None,
        )

        # Step 3e: render phase header and start spinner
        rdr.print_header(phase_num, total_phases, phase_name)
        rdr.start_phase(phase_name)

        # Step 3e2: open transcript context — every event lands in
        # apply_state_log filtered by run_id; the SSE producer polls that
        # table to feed the live transcript pane.
        rdr.open_transcript(
            phase_name,
            slug=slug,
            db_path=_pipeline_db_path(resolved_cwd),
            run_id=db_run_id,
        )

        # Step 3f: stream events
        phase_succeeded = False
        try:
            for event in headless.run_phase(
                phase=phase_name,
                session_id=session_id,
                prompt=prompt_text,
                plugin_dir=plugin_directory,
                system_prompt=system_prompt,
                resume=resume,
                cwd=resolved_cwd,
                max_turns=_PHASE_MAX_TURNS[phase_name],
                cancel_event=cancel_event,
            ):
                rdr.render_event(event)

                if event.type == "phase_complete":
                    phase_succeeded = True
                    break

                if event.type == "phase_failed":
                    rdr.close_transcript()
                    rdr.print_error(
                        "Aborting before subsequent phases. "
                        "If the error mentions contracts not frozen, run: "
                        "jobsmith doctor  (or set frozen_at in specialist-contracts.yaml)"
                    )
                    return 3

                if event.type == "error":
                    rdr.stop_phase()
                    rdr.close_transcript()
                    rdr.print_error(f"Phase {phase_name} encountered an error. Aborting.")
                    return 2
        except Exception as exc:
            rdr.stop_phase()
            rdr.close_transcript()
            rdr.print_error(f"Unexpected error in phase {phase_name}: {exc}")
            return 2

        rdr.close_transcript()

        if not phase_succeeded:
            rdr.stop_phase()
            rdr.print_error(
                f"Phase {phase_name} did not emit a phase_complete signal. "
                "Check output above for errors."
            )
            return 2

        # Step 3f-snap: snapshot agent drafts so `jobsmith feedback record`
        # has a stable baseline to diff user edits against. Done immediately
        # after the phase succeeds, before the user can edit anything.
        _snapshot_phase_drafts(phase_name, slug, resolved_cwd)

        # Step 3f-dual-write (feat-e3d87579): mirror FS artifacts to the DB.
        # Phase 1 dual-write — FS remains authoritative; PUT failures log a
        # WARNING but never fail the phase. Skipped when JOBSMITH_DUAL_WRITE=0.
        client = _build_client()
        if client is not None:
            phase_state_dir = _apply_state_dir(slug, resolved_cwd)
            if phase_state_dir is not None:
                try:
                    _dual_write(
                        client=client,
                        slug=slug,
                        run_id=db_run_id,
                        state_dir=phase_state_dir,
                    )
                except Exception as exc:  # noqa: BLE001 — never fail phase
                    logger.warning("dual-write hook failed: %s", exc)

        # Step 3f-snapshot (feat-60be8c3a): before the render phase invokes
        # quarto, materialise DB-only artifacts back to FS so quarto can read
        # _quarto.yml / _variables.yml / prose-draft.md / cover-letter-draft.md.
        # Once specialists drop FS writes (follow-up), this is the only path
        # that puts those files on disk for the render step.
        if phase_name == "render" and client is not None:
            try:
                client.snapshot_run(slug, db_run_id)
            except Exception as exc:  # noqa: BLE001 — never fail render
                logger.warning(
                    "render-phase snapshot failed (FS may be stale): %s", exc
                )

        # Step 3g: between-phase orchestration. After gather we reconcile the
        # canonical slug (phase 1 may have written artifacts under a different
        # directory than the URL-derived slug), and recompute session_id
        # (Option A: phase 2/3 run under a fresh session keyed on the
        # canonical slug — phase prompts read .apply-state/* directly so
        # conversation continuity is not required).  The URL → slug mapping
        # is persisted only when reconciliation actually produced a canonical
        # slug; otherwise we'd corrupt the index by recording a fallback
        # (URL-derived) slug as if it were canonical.  Step 4/5 runs before
        # phase 2 (in Step 3pre above) regardless of how gather got "done".
        if phase_name == "gather":
            pre_reconcile_slug = slug
            new_slug, reconciled = _reconcile_canonical_slug(
                slug, resolved_cwd, started_at
            )
            if new_slug != slug:
                slug = new_slug
                # When reconcile renames the directory the session-id file
                # is copied verbatim into the new location.  Remove it now so
                # the per-phase fresh-session block at the top of each
                # non-gather iteration (Finding 1 fix) generates a clean
                # uuid4 rather than re-using the carried-over value.
                if apps_dir is not None:
                    carried_session_file = (
                        apps_dir / slug / ".apply-state" / "session-id"
                    )
                    carried_session_file.unlink(missing_ok=True)
            if reconciled:
                _record_url_mapping(url, slug, resolved_cwd)
            # Propagate the canonical slug to the outer apply_runs row so the
            # final UPDATE in run_apply's finally-block records the correct
            # application directory (roborev #923 HIGH 2).
            db_slug_ref[0] = slug

            # Roborev job 956 MEDIUM: catch any apply_state_log row the
            # renderer wrote between the orchestrator's mid-phase
            # ``rekey-slug`` and the gather event-loop close (the
            # renderer's ``_transcript_slug`` is captured at
            # ``open_transcript`` time and is the URL-derived starting
            # slug for phase 1; transcript rows after rekey continue
            # tagging that slug). Re-rekey idempotently from the old
            # slug to the canonical one so apply_state and apply_state_log
            # are fully consolidated under a single slug per run; subsequent
            # ``jobsmith db list-state --slug`` and ``reset-state --slug``
            # operations cover everything.
            if (
                reconciled
                and pre_reconcile_slug != slug
                and db_conn is not None
            ):
                with contextlib.suppress(Exception):
                    from .db import rekey_slug as _rekey_slug

                    _rekey_slug(
                        db_conn, from_slug=pre_reconcile_slug, to_slug=slug
                    )

            # Render per-phase summary panel before the confirm gate
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None:
                rdr.render_phase_summary("gather", state_dir)

        # Post-phase ingest into specialist_outputs. Mirrors the marimo
        # runner's behavior so `jobsmith review <slug>` sees rows immediately
        # after `jobsmith apply <url>` (roborev #923 HIGH 2). Wrapped in
        # suppress: a single broken artifact must not abort the pipeline.
        # Also runs ingest_standalone_artifacts so the 4 orphaned kinds
        # (cover-letter-draft, _quarto.yml, _variables.yml, .agent.md) land
        # in the DB on live runs — without this, JOBSMITH_DUAL_WRITE=0
        # (S4 default) leaves them invisible until manual backfill.
        # Closes roborev branch-review HIGH (feat-b1a883a1).
        if db_conn is not None:
            with contextlib.suppress(Exception):
                from .db_ingest import (
                    ingest_phase_outputs as _ingest_phase_outputs,
                )
                from .db_ingest import (
                    ingest_standalone_artifacts as _ingest_standalone_artifacts,
                )

                state_dir_for_ingest = _apply_state_dir(slug, resolved_cwd)
                if state_dir_for_ingest is not None:
                    _ingest_phase_outputs(
                        db_conn,
                        slug=slug,
                        run_id=db_run_id,
                        phase=phase_name,
                        state_dir=state_dir_for_ingest,
                    )
                    _ingest_standalone_artifacts(
                        db_conn,
                        run_id=db_run_id,
                        state_dir=state_dir_for_ingest,
                    )

        # Step 3h: confirm gate (not after the last phase, and not after a
        # phase that was skipped — only fresh-run phases prompt).
        if not skip_confirm and phase_name != "render":
            rdr.pause_before_confirm()
            if not click.confirm(f"Phase {phase_num} ({phase_name}) complete. Proceed to next phase?"):
                rdr.print_info("Stopped at user request. Partial work saved.")
                # Roborev job 967 MEDIUM: signal user-decline to the
                # outer ``run_apply`` so apply_runs.status is recorded
                # as "cancelled" rather than "done". rc stays 0
                # because the user's choice is not a CLI failure.
                db_status_ref[0] = "cancelled"
                return 0

    rdr.print_complete()
    return 0


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def run_apply(
    url: str,
    *,
    cwd: Path | None = None,
    skip_confirm: bool = False,
    force: bool = False,
    verbosity: int = 0,
    renderer: ApplyRenderer | None = None,
    jd_text: str | None = None,
    slug: str | None = None,
    run_id: str | None = None,
    cancel_event: "threading.Event | None" = None,
) -> int:
    """Run the three-phase apply pipeline.

    Thin CLI wrapper around :func:`~jobsmith.core.pipeline.core_run_apply`.
    Constructs the :class:`~jobsmith.render.ApplyRenderer` for terminal output
    and provides the renderer-coupled ``_phase_runner`` closure before
    delegating to the business-logic core.

    Parameters
    ----------
    url:
        Job description URL.
    cwd:
        Working directory (defaults to current directory).
    skip_confirm:
        When True, phase-gate confirmations are bypassed (--yes flag).
    force:
        When True, ignore ``.url-index.json`` and any prior ``manifest.json``;
        start fresh from phase 1 even if a canonical directory already
        contains completed work.  Maps to the ``--force`` CLI flag.
    verbosity:
        0 = quiet (default), 1 = -v (filtered tool calls), 2 = -vv (all).
    renderer:
        Optional :class:`~jobsmith.render.ApplyRenderer` instance.  When None,
        one is constructed automatically (TTY-aware, using *skip_confirm* for
        the ``yes`` flag).
    jd_text:
        Optional JD body text supplied out-of-band, for cases where
        ``WebFetch`` cannot scrape the URL (JS-rendered career portals like
        Netflix careers, some Workday tenants). Maps to the
        ``--jd-text`` / ``--jd-text-file`` CLI flags.

    Returns
    -------
    int
        Exit code: 0 on success or clean user abort, non-zero on error.
    """
    from jobsmith.core.pipeline import _PHASES, core_run_apply
    from jobsmith.core.session import get_or_create_session_id as _get_or_create_session_id

    resolved_cwd = cwd or Path.cwd()
    rdr = renderer or ApplyRenderer(yes=skip_confirm, verbosity=verbosity)

    def _phase_runner(
        *,
        url: str,
        resolved_cwd: Path,
        events: object,
        confirm: object,
        skip_confirm: bool,
        force: bool,
        verbosity: int,
        slug: str,
        apps_dir,
        phase_done: dict,
        started_at: float,
        db_conn,
        db_run_id: str,
        db_slug_ref: list,
        db_status_ref: list,
        jd_text_file,
    ) -> int:
        """CLI-coupled phase runner: constructs session_id, prints banners,
        delegates to _run_apply_phases with the Rich renderer.

        Back-compat patching: ``get_plugin_dir`` and ``_run_apply_phases`` are
        looked up via ``jobsmith.apply`` at call-time so that monkeypatches on
        those names in the apply shim propagate into this closure.
        """
        # Resolve plugin directory so monkeypatches on jobsmith.apply.get_plugin_dir work.
        _get_plugin_dir_fn = _resolve_from_apply("get_plugin_dir", get_plugin_dir)
        plugin_directory = _get_plugin_dir_fn()
        app_dir = apps_dir / slug if apps_dir is not None else None

        # Compute session ID (persisted per-application file).
        session_id = (
            _get_or_create_session_id(app_dir, resolved_cwd)
            if app_dir is not None
            else headless.deterministic_session_id(slug)
        )

        # Banners: force and info.
        if force:
            rdr.print_force_banner()
        rdr.print_info(f"jobsmith apply: slug={slug!r}  session={session_id}")

        # Resume banner above the first phase that will actually run.
        first_to_run = next(name for name, _ in _PHASES if not phase_done[name])
        if first_to_run != "gather":
            first_phase_num = next(
                num for name, num in _PHASES if name == first_to_run
            )
            rdr.print_resume_banner(slug, first_phase_num, first_to_run)

        total_phases = len(_PHASES)

        # Look up _run_apply_phases through apply's namespace to support test
        # patches like patch("jobsmith.apply._run_apply_phases", fake_phases).
        _phases_fn = _resolve_from_apply("_run_apply_phases", _run_apply_phases)
        return _phases_fn(
            url=url,
            resolved_cwd=resolved_cwd,
            rdr=rdr,
            plugin_directory=plugin_directory,
            slug=slug,
            apps_dir=apps_dir,
            session_id=session_id,
            phase_done=phase_done,
            total_phases=total_phases,
            skip_confirm=skip_confirm,
            started_at=started_at,
            db_conn=db_conn,
            db_run_id=db_run_id,
            db_slug_ref=db_slug_ref,
            db_status_ref=db_status_ref,
            jd_text_file=jd_text_file,
            cancel_event=cancel_event,
        )

    # Look up ensure_bootstrap through apply's namespace so that
    # patches on jobsmith.apply._run_init propagate (ensure_bootstrap
    # in apply.py calls _run_init from apply's module namespace).
    _bootstrap_fn = _resolve_from_apply("ensure_bootstrap", None)

    return core_run_apply(
        url,
        cwd=resolved_cwd,
        events=rdr,
        skip_confirm=skip_confirm,
        force=force,
        verbosity=verbosity,
        jd_text=jd_text,
        slug=slug,
        run_id=run_id,
        phase_runner=_phase_runner,
        bootstrap=_bootstrap_fn,
    )
