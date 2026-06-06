"""jobsmith.core.pipeline — phase-iterator generator for the apply pipeline.

Moved here from ``jobsmith.apply`` as part of trk-ad6d8227 (slice 3b).

This module deliberately has **no** dependencies on ``rich``, ``click``,
``typer``, or any other CLI/rendering library.  CLI-coupled helpers
(``ensure_bootstrap``, ``_run_step45_orchestration``) are injected by
``jobsmith.apply`` via the ``bootstrap`` and ``anchor_guard`` keyword
arguments; the defaults provided here are safe no-ops so call-sites that
do not need those side-effects (tests, API path) can omit them.

``jobsmith.apply`` re-exports ``run_phase_iter`` unchanged so all existing
import sites continue to work.
"""
from __future__ import annotations

import contextlib
import json
import logging
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from jobsmith.core.events import PipelineEvent
from jobsmith.core.manifest import (
    PHASE_REQUIRED_SPECIALISTS,
    load_manifest,
    phase_completed,
)
from jobsmith.core.paths import (
    applications_dir,
    apply_state_dir,
    build_paths,
    pipeline_db_path,
)
from jobsmith.core.slug import derive_slug, reconcile_canonical_slug
from jobsmith.core.url_index import load_url_index, record_url_mapping, resolve_starting_slug

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase definitions (mirrored from apply.py; pipeline owns these now)
# ---------------------------------------------------------------------------

_PHASES = [
    ("gather", 1),
    ("draft", 2),
    ("render", 3),
]

_PHASE_MAX_TURNS: dict[str, int] = {
    "gather": 30,
    "draft": 30,
    "render": 60,
}


# ---------------------------------------------------------------------------
# Helpers: build_phase_prompt, _auto_freeze_contracts, _snapshot_phase_drafts
# ---------------------------------------------------------------------------


def _format_paths_block(paths: dict[str, str]) -> str:
    """Format a deterministic Paths block for injection into a phase prompt."""
    if not paths:
        return ""
    lines = ["Paths (use these absolute paths verbatim — do NOT search for them):"]
    for key in sorted(paths.keys()):
        lines.append(f"  {key}: {paths[key]}")
    return "\n".join(lines)


def build_phase_prompt(
    phase: str,
    slug: str,
    url: str,
    *,
    paths: dict[str, str] | None = None,
    jd_text_file: Path | None = None,
) -> str:
    """Return the user prompt text for a given phase.

    The system prompt (loaded via ``--system-prompt-file``) carries the full
    phase instructions.  This user prompt gives the agent its immediate task.

    Parameters
    ----------
    phase:
        One of ``"gather"``, ``"draft"``, ``"render"``.
    slug:
        Application slug derived from the URL.
    url:
        Original JD URL.
    paths:
        Optional flat string→string mapping of absolute paths to inject.
    jd_text_file:
        Gather phase only.  Absolute path to an out-of-band JD text file.

    Returns
    -------
    str
        User prompt text, optionally with an injected Paths block.

    Raises
    ------
    ValueError
        If *phase* is not one of the three recognised phases.
    """
    effective_paths: dict[str, str] = paths if paths is not None else {}
    paths_block = _format_paths_block(effective_paths)

    def _with_paths(text: str) -> str:
        if paths_block:
            return f"{text}\n\n{paths_block}"
        return text

    if phase == "gather":
        prompt = (
            f"Process this JD: {url}. Application slug: {slug}. "
            "Begin Phase 1 (gather): jd-parse, fit, HM enrichment, company research, "
            "bullet selection. Pause at the analysis gate as instructed."
        )
        if jd_text_file is not None:
            prompt += (
                "\n\nThe user has provided the JD body text out-of-band "
                f"(useful for JS-rendered portals like Netflix careers). "
                f"Read it from this absolute path and copy the full contents "
                f"into the spec.json `inputs.jd_text` field that you write for "
                f"apply-jd-parser, so the parser skips its WebFetch and uses "
                f"the supplied text directly: {jd_text_file}"
            )
        return _with_paths(prompt)
    if phase == "draft":
        return _with_paths(
            f"Resume Phase 2 (draft) for slug {slug}. "
            "Read .apply-state/ artifacts. Run prose-writer + prose-qa loop until pass."
        )
    if phase == "render":
        return _with_paths(
            f"Resume Phase 3 (render) for slug {slug}. "
            "Render resume PDF, ATS check, visual layout, cover letter, index page, "
            "then jobsmith assemble."
        )
    raise ValueError(f"Unknown phase: {phase!r}. Expected one of: gather, draft, render.")


def _auto_freeze_contracts(contracts_path: Path) -> None:
    """Stamp ``frozen_at`` with today's ISO date if it is currently null.

    Idempotent: if ``frozen_at`` is already set the file is not modified.
    No-op if the file does not exist.  Never raises.
    """
    if not contracts_path.exists():
        return
    try:
        import yaml as _yaml

        raw = contracts_path.read_text(encoding="utf-8")
        data = _yaml.safe_load(raw)
        if not isinstance(data, dict):
            return
        if data.get("frozen_at") is not None:
            return
        from datetime import date as _date

        today = _date.today().isoformat()
        if "frozen_at: null" in raw:
            updated = raw.replace("frozen_at: null", f"frozen_at: '{today}'", 1)
        else:
            updated = raw.rstrip("\n") + f"\nfrozen_at: '{today}'\n"
        contracts_path.write_text(updated, encoding="utf-8")
        logger.info(
            "Auto-froze specialist contracts at %s (frozen_at=%s)",
            contracts_path,
            today,
        )
    except Exception as exc:  # noqa: BLE001 — never abort apply on freeze failure
        logger.warning(
            "Could not auto-freeze contracts at %s: %s", contracts_path, exc
        )


def _load_reuse_plan_from_state(state_dir: Path) -> object | None:
    """Load reuse-plan.json from *state_dir*; return parsed dict or None.

    Returns None when the file is missing, malformed, or the draft decision
    is absent.  The pipeline uses this to detect a warm-start trigger.
    Never raises — any error degrades to None (regenerate path).
    """
    plan_path = state_dir / "reuse-plan.json"
    if not plan_path.exists():
        return None
    try:
        import json as _json

        return _json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline: could not load reuse-plan.json: %s", exc)
        return None


def _build_warmstart_prompt_suffix(
    slug: str,
    resolved_cwd: Path,
    reuse_plan_dict: dict,
) -> str:
    """Build a prompt suffix for the warm-start draft path.

    Computes the warm-start delta and returns a text block to append to the
    draft phase prompt.  The block tells the prose-writer agent exactly:
    - which bullets are carried forward verbatim (anchors + reused)
    - which requirement hashes must be freshly addressed

    Returns an empty string on any error (falls back to full regeneration).
    """
    try:
        bullet_map: dict[str, str] = reuse_plan_dict.get("bullet_map") or {}
        matched_slug: str | None = reuse_plan_dict.get("matched_slug")
        if not matched_slug:
            return ""

        apps_dir = applications_dir(resolved_cwd)
        if apps_dir is None:
            return ""

        prior_state_dir = apps_dir / matched_slug / ".apply-state"
        if not prior_state_dir.is_dir():
            logger.debug(
                "pipeline: warm-start prior state dir missing: %s", prior_state_dir
            )
            return ""

        # Load current requirement hashes from the current app's state dir
        current_state_dir = apply_state_dir(slug, resolved_cwd)
        current_req_hashes: list[str] = []
        if current_state_dir is not None:
            jd_parsed_path = current_state_dir / "jd-parsed.json"
            if jd_parsed_path.exists():
                try:
                    import json as _json2

                    jd_parsed = _json2.loads(
                        jd_parsed_path.read_text(encoding="utf-8")
                    )
                    must_haves = jd_parsed.get("must_haves") or []
                    nice_to_haves = jd_parsed.get("nice_to_haves") or []
                    from jobsmith.reuse.store import content_hash as _content_hash

                    for req in must_haves + nice_to_haves:
                        raw = req.get("raw", "") if isinstance(req, dict) else str(req)
                        if raw:
                            current_req_hashes.append(_content_hash(raw))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "pipeline: warm-start could not load jd-parsed.json: %s", exc
                    )

        from jobsmith.reuse.warmstart import compute_warm_start

        ws = compute_warm_start(
            prior_state_dir=prior_state_dir,
            current_requirement_hashes=current_req_hashes,
            bullet_map=bullet_map,
        )

        anchor_ids = [
            b.get("master_bullet_id", "")
            for b in ws.anchors_carried
            if b.get("master_bullet_id")
        ]
        lines = [
            "",
            "## Warm-start mode (diff-and-tweak)",
            "",
            f"Prior application: {matched_slug}",
            f"Reused bullet IDs (no rewrite needed): {ws.reused_bullet_ids or 'none'}",
            f"Anchor bullets (carry VERBATIM, do NOT rewrite): {anchor_ids or 'none'}",
            f"Delta requirement hashes (MUST address): {ws.delta_requirement_hashes or 'none'}",
            f"Escalated requirements (full generation): {ws.escalated_requirement_hashes or 'none'}",
            "",
            "Instructions:",
            "- Load the prior prose-draft.md from the matched application as your base.",
            "- Anchor bullets are SACRED — copy them verbatim, never rewrite.",
            "- Reused bullet IDs are already covered — keep them with minor JD-keyword tuning only.",
            "- Delta requirements need fresh bullets — write new bullets from master YAML only.",
            "- Escalated requirements need full generation — treat as normal draft for those bullets.",
            "- Do NOT fabricate. Every metric must be in master YAML or gap-resolutions.",
        ]
        return "\n".join(lines)

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pipeline: warm-start prompt suffix failed — falling back to full draft: %s", exc
        )
        return ""


def _snapshot_phase_drafts(phase: str, slug: str, cwd: Path) -> None:
    """Persist immutable agent-draft snapshots after a phase completes.

    - After phase ``draft``: snapshot ``prose-draft.md`` → ``prose-draft.agent.md``.
    - After phase ``render``: snapshot the cover-letter draft.

    Silently no-ops when source files are missing.
    """
    state_dir = apply_state_dir(slug, cwd)
    if state_dir is None or not state_dir.is_dir():
        return
    app_dir = state_dir.parent

    if phase == "draft":
        src = state_dir / "prose-draft.md"
        if src.exists():
            shutil.copy2(src, state_dir / "prose-draft.agent.md")

    elif phase == "render":
        src_root = app_dir / "cover-letter-draft.md"
        src_state = state_dir / "cover-letter-draft.md"
        src = src_root if src_root.exists() else src_state
        if src.exists():
            shutil.copy2(src, state_dir / "cover-letter-draft.agent.md")


# ---------------------------------------------------------------------------
# Correctness backstop helper (slice-8)
# ---------------------------------------------------------------------------


def _run_backstop_gate(slug: str, resolved_cwd: Path) -> None:
    """Run the correctness backstop (guard + factcheck) after render phase.

    This is UNCONDITIONAL — it runs whether or not reuse/warm-start was used.
    ``BackstopError`` is intentionally NOT caught here: when all retries and
    fallback are exhausted the error propagates to the phase loop's outer
    ``except Exception`` handler, which emits a ``phase_failed`` event and
    stops the render phase.  Only non-critical infrastructure errors (config
    loading, DB metric writes, path resolution) are suppressed.

    Config (``config.reuse.regen_retry_bound``) drives the retry bound.
    """
    from jobsmith.config import find_config, load_config

    config_path = find_config(resolved_cwd)
    regen_retry_bound = 3  # default
    if config_path is not None:
        try:
            cfg = load_config(config_path)
            regen_retry_bound = cfg.reuse.regen_retry_bound
        except Exception:  # noqa: BLE001
            pass

    apps_dir = applications_dir(resolved_cwd)
    if apps_dir is None:
        logger.debug("backstop: no applications_dir — skipping")
        return

    app_dir = apps_dir / slug
    state_dir = apply_state_dir(slug, resolved_cwd)
    if state_dir is None or not state_dir.is_dir():
        logger.debug("backstop: state_dir missing for %s — skipping", slug)
        return

    # Locate artifacts: resume prose-draft and cover-letter-draft
    resume_path = state_dir / "prose-draft.md"
    cl_candidates = [
        app_dir / "cover-letter-draft.md",
        state_dir / "cover-letter-draft.md",
    ]
    cl_path = next((p for p in cl_candidates if p.exists()), None)

    resume_text = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
    cover_letter_text = cl_path.read_text(encoding="utf-8") if cl_path else ""

    if not resume_text and not cover_letter_text:
        logger.debug("backstop: no artifact text found for %s — skipping", slug)
        return

    # Locate gate inputs
    master_path = state_dir.parent.parent / "assets" / "content" / "work.yml"
    if config_path is not None and not master_path.exists():
        try:
            cfg = load_config(config_path)
            master_path = (config_path.parent / cfg.master.work_yml).resolve()
        except Exception:  # noqa: BLE001
            pass

    content_dir = master_path.parent if master_path.exists() else resolved_cwd
    selection_path = state_dir / "bullet-selection.json"
    decisions_path = state_dir / "bullet-decisions.json"

    # Open DB connection for metric recording (best-effort, non-critical)
    db_conn = None
    with contextlib.suppress(Exception):
        db_conn = _open_pipeline_db_for_run(resolved_cwd)

    try:
        from jobsmith.reuse.backstop import run_backstop

        # BackstopError propagates uncaught — correctness gates MUST pass.
        run_backstop(
            slug=slug,
            resume_text=resume_text,
            cover_letter_text=cover_letter_text,
            master_path=master_path,
            content_dir=content_dir,
            selection_path=selection_path,
            decisions_path=decisions_path if decisions_path.exists() else None,
            regen_retry_bound=regen_retry_bound,
            db_conn=db_conn,
        )
    finally:
        # DB close is non-critical; suppress any close error.
        if db_conn is not None:
            with contextlib.suppress(Exception):
                db_conn.close()


# ---------------------------------------------------------------------------
# Public generator
# ---------------------------------------------------------------------------


def run_phase_iter(
    url: str,
    *,
    cwd: Path | None = None,
    skip_confirm: bool = False,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    phases: list[str] | None = None,
    jd_text: str | None = None,
    # Dependency injection: CLI-coupled side-effects.
    # apply.py passes real implementations; tests / API path use defaults.
    bootstrap: Callable[[Path], None] | None = None,
    anchor_guard: Callable[[str, Path], int] | None = None,
    events: object = None,  # EventSink — unused here, accepted for API compat
) -> Iterator[PipelineEvent]:
    """Yield :class:`PipelineEvent` for each phase of the apply pipeline.

    Phase-granular events ONLY (not per-specialist).  Per-specialist
    granularity is deferred to slice 8 (manifest-polling ingestor).

    Parameters
    ----------
    url:
        Job description URL.
    cwd:
        Working directory (defaults to current directory).
    skip_confirm:
        Bypass phase-gate confirmations.
    force:
        Ignore existing manifest / URL index; re-run all phases.
    cancel_event:
        When set, the generator stops after the current phase completes.
    phases:
        When provided, restrict execution to the named phases.  ``None``
        means "run all not-yet-complete phases" (the default).
    jd_text:
        Optional JD body text supplied out-of-band.
    bootstrap:
        Callable ``(cwd: Path) -> None`` that ensures the working directory
        is initialised.  Defaults to a no-op (the API path / tests skip
        the interactive ``jobsmith init`` prompt).
    anchor_guard:
        Callable ``(slug: str, cwd: Path) -> int`` that runs the between-
        phase anchor check.  Returns 0 on success, non-zero on failure.
        Defaults to a no-op that always returns 0.
    events:
        An optional :class:`~jobsmith.core.protocols.EventSink` instance.
        Not used by the generator itself (all events are *yielded*), but
        accepted so callers can satisfy the signature without error.

    Yields
    ------
    PipelineEvent
        Events in order: ``phase_started`` → ``phase_complete`` (or
        ``phase_failed`` / ``guard_failed``) per phase, with an optional
        ``slug_changed`` event between gather and draft.  A ``cancelled``
        event is the final event when ``cancel_event`` was set.
    """
    resolved_cwd = cwd or Path.cwd()

    # Materialise jd_text into a temp file once (only for gather phase).
    jd_text_file: Path | None = None
    if jd_text is not None and jd_text.strip():
        import os as _os
        import tempfile as _tempfile

        _fd, _tmp_path = _tempfile.mkstemp(
            prefix=f"jobsmith-jdtext-{derive_slug(url)}-",
            suffix=".txt",
        )
        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(jd_text)
        jd_text_file = Path(_tmp_path)

    try:
        yield from _run_phase_iter_body(
            url=url,
            resolved_cwd=resolved_cwd,
            force=force,
            cancel_event=cancel_event,
            phases=phases,
            jd_text_file=jd_text_file,
            bootstrap=bootstrap,
            anchor_guard=anchor_guard,
        )
    finally:
        if jd_text_file is not None:
            with contextlib.suppress(OSError):
                jd_text_file.unlink()


def _run_phase_iter_body(
    *,
    url: str,
    resolved_cwd: Path,
    force: bool,
    cancel_event: threading.Event | None,
    phases: list[str] | None,
    jd_text_file: Path | None,
    bootstrap: Callable[[Path], None] | None,
    anchor_guard: Callable[[str, Path], int] | None,
) -> Iterator[PipelineEvent]:
    """Generator body for :func:`run_phase_iter`, extracted so the wrapper
    can ``try/finally``-clean the temp file even when the consumer stops
    iterating early (roborev #928 LOW).
    """
    import time as _time

    from jobsmith import headless
    from jobsmith import plugin_dir as get_plugin_dir
    from jobsmith.config import find_config

    # Step 1: bootstrap
    if bootstrap is not None:
        bootstrap(resolved_cwd)

    # Step 1b: seed master_content from disk YAMLs if not already loaded
    # (roborev job 962 MEDIUM).
    try:
        _config_path = find_config(resolved_cwd)
        _db_path = pipeline_db_path(resolved_cwd)
        if _config_path is not None and _db_path is not None:
            from jobsmith.master_ingest import ensure_master_loaded as _ensure_master

            _ensure_master(_db_path, repo_root=_config_path.parent)
    except Exception as exc:  # noqa: BLE001 — degrade rather than abort.
        logger.warning("master_content seed (run_phase_iter) failed: %s", exc)

    # Step 2: resolve starting slug
    started_at = _time.time()
    plugin_directory = get_plugin_dir()

    if force:
        index = load_url_index(resolved_cwd)
        slug = index[url] if url in index else derive_slug(url)
    else:
        slug, _from_index = resolve_starting_slug(url, resolved_cwd)

    # Roborev job 959 HIGH + job 960 HIGH: scoped DB reset on force reruns.
    if force:
        _force_db_path = pipeline_db_path(resolved_cwd)
        if _force_db_path is not None and _force_db_path.exists():
            with contextlib.suppress(Exception):
                from jobsmith.db import open_pipeline_db as _open_pipe_db
                from jobsmith.db import reset_state as _reset_state

                full_reset = phases is None or "gather" in phases
                _conn = _open_pipe_db(_force_db_path)
                try:
                    if full_reset:
                        _reset_state(_conn, slug=slug)
                    else:
                        # Delete only per-specialist rows for targeted phases.
                        scoped_specs: set[str] = set()
                        for ph in phases:  # type: ignore[union-attr]
                            for s in PHASE_REQUIRED_SPECIALISTS.get(ph, ()):
                                scoped_specs.add(s)
                        kinds_to_drop = []
                        for s in scoped_specs:
                            kinds_to_drop.extend(
                                [f"spec-{s}", f"{s}-result"]
                            )
                        if kinds_to_drop:
                            placeholders = ",".join("?" for _ in kinds_to_drop)
                            _conn.execute(
                                f"DELETE FROM apply_state WHERE slug = ? "
                                f"AND kind IN ({placeholders})",
                                (slug, *kinds_to_drop),
                            )
                            _conn.commit()

                        # Roborev job 963 MEDIUM: strip manifest invocations
                        # for targeted phase specialists.
                        from jobsmith.db import (
                            get_state as _get_state,
                        )
                        from jobsmith.db import (
                            put_state as _put_state,
                        )

                        manifest_blob = _get_state(
                            _conn, slug=slug, kind="manifest"
                        )
                        if manifest_blob:
                            try:
                                manifest_data = json.loads(manifest_blob)
                            except json.JSONDecodeError:
                                manifest_data = None
                            if isinstance(manifest_data, dict):
                                invocations = manifest_data.get("invocations")
                                if isinstance(invocations, list):
                                    pruned = [
                                        inv
                                        for inv in invocations
                                        if not (
                                            isinstance(inv, dict)
                                            and inv.get("specialist")
                                            in scoped_specs
                                        )
                                    ]
                                    if len(pruned) != len(invocations):
                                        manifest_data["invocations"] = pruned
                                        _put_state(
                                            _conn,
                                            slug=slug,
                                            kind="manifest",
                                            content_blob=json.dumps(
                                                manifest_data
                                            ),
                                        )
                finally:
                    _conn.close()

    # Step 3: phase-completion gating
    apps_dir = applications_dir(resolved_cwd)
    app_dir = apps_dir / slug if apps_dir is not None else None
    manifest = (
        None
        if force or app_dir is None
        else load_manifest(app_dir, resolved_cwd)
    )

    session_id = headless.deterministic_session_id(slug)

    phase_done: dict[str, bool] = {
        name: phase_completed(manifest, name) for name, _ in _PHASES
    }

    # All done → no events to yield
    if all(phase_done.values()) and app_dir is not None:
        return

    # Filter to the requested phases (preserving canonical ordering).
    if phases is not None:
        requested = set(phases)
        active_phases = [(n, num) for n, num in _PHASES if n in requested]
    else:
        active_phases = list(_PHASES)

    # Auto-freeze specialist contracts on first apply (feat-385f3405).
    _auto_freeze_contracts(
        plugin_directory / "agents" / "apply" / "specialist-contracts.yaml"
    )

    for phase_name, phase_num in active_phases:
        # Check cancel before starting each phase
        if cancel_event is not None and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase=phase_name)
            return

        # Step 3pre: anchor guard before draft
        if phase_name == "draft" and not phase_done["draft"]:
            state_dir = apply_state_dir(slug, resolved_cwd)
            if state_dir is not None and not (
                state_dir / "bullet-decisions.json"
            ).exists():
                _guard = anchor_guard if anchor_guard is not None else lambda s, c: 0
                rc = _guard(slug, resolved_cwd)
                if rc != 0:
                    yield PipelineEvent(
                        kind="guard_failed",
                        phase=phase_name,
                        payload={"rc": rc},
                    )
                    return

        # Skip completed phases
        if phase_done[phase_name]:
            continue

        yield PipelineEvent(kind="phase_started", phase=phase_name)

        # Step 3a: session continuity
        resume = (phase_name != "gather") and headless.session_exists(
            session_id, cwd=resolved_cwd
        )

        # Step 3b: system prompt
        system_prompt = (
            plugin_directory / "system-prompts" / f"phase-{phase_num}-{phase_name}.md"
        )
        if not system_prompt.exists():
            raise FileNotFoundError(
                f"System prompt not found: {system_prompt}"
            )

        # Step 3c: paths + prompt
        phase_paths = build_paths(slug, resolved_cwd, plugin_directory)
        prompt_text = build_phase_prompt(
            phase_name,
            slug,
            url,
            paths=phase_paths,
            jd_text_file=jd_text_file if phase_name == "gather" else None,
        )

        # Step 3c-warmstart: when the reuse plan says warm-start for draft,
        # append the delta/anchor context to the prompt so the prose-writer
        # only rewrites the delta bullets and carries anchors verbatim.
        if phase_name == "draft":
            _state_dir_for_plan = apply_state_dir(slug, resolved_cwd)
            if _state_dir_for_plan is not None:
                _reuse_plan_dict = _load_reuse_plan_from_state(_state_dir_for_plan)
                if (
                    _reuse_plan_dict is not None
                    and isinstance(_reuse_plan_dict, dict)
                    and (_reuse_plan_dict.get("draft") or {}).get("decision")
                    == "warm-start"
                ):
                    _ws_suffix = _build_warmstart_prompt_suffix(
                        slug, resolved_cwd, _reuse_plan_dict
                    )
                    if _ws_suffix:
                        prompt_text = prompt_text + _ws_suffix

        # Step 3f: stream events from headless
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
                if event.type == "phase_complete":
                    phase_succeeded = True
                    break
                if event.type == "phase_failed":
                    yield PipelineEvent(
                        kind="phase_failed",
                        phase=phase_name,
                        payload={"error": event.error},
                    )
                    return
                if event.type == "error":
                    yield PipelineEvent(
                        kind="phase_failed",
                        phase=phase_name,
                        payload={"error": event.error},
                    )
                    return

                # Stop draining if cancelled mid-phase
                if cancel_event is not None and cancel_event.is_set():
                    yield PipelineEvent(kind="cancelled", phase=phase_name)
                    return
        except Exception as exc:  # noqa: BLE001
            yield PipelineEvent(
                kind="phase_failed",
                phase=phase_name,
                payload={"error": f"{type(exc).__name__}: {exc}"},
            )
            return

        if not phase_succeeded:
            yield PipelineEvent(
                kind="phase_failed",
                phase=phase_name,
                payload={"error": "no phase_complete signal emitted"},
            )
            return

        # Step 3f-snap: snapshot agent drafts
        _snapshot_phase_drafts(phase_name, slug, resolved_cwd)

        # Step 3g: between-phase orchestration after gather
        if phase_name == "gather":
            new_slug, reconciled = reconcile_canonical_slug(
                slug, resolved_cwd, started_at
            )
            if new_slug != slug:
                old_slug = slug
                slug = new_slug
                session_id = headless.deterministic_session_id(slug)
                ev = PipelineEvent(
                    kind="slug_changed",
                    phase=phase_name,
                    payload={"old_slug": old_slug, "new_slug": slug},
                )
                yield ev
                _time.sleep(0)  # cooperative yield point
            if reconciled:
                record_url_mapping(url, slug, resolved_cwd)

        # Step 3h: correctness backstop after render (UNCONDITIONAL — runs on
        # every completed render, whether reuse was active or not).
        if phase_name == "render":
            _run_backstop_gate(slug, resolved_cwd)

        yield PipelineEvent(kind="phase_complete", phase=phase_name)

        # Check cancel after phase completes (before starting next)
        if cancel_event is not None and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase=phase_name)
            return


# ---------------------------------------------------------------------------
# DB lifecycle helpers (moved from apply.py — Slice 3c of trk-ad6d8227)
# No dependency on rich / click / typer.
# ---------------------------------------------------------------------------


def _db_now_iso() -> str:
    """ISO-8601 UTC timestamp; matches marimo runner's apply_runs format."""
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()


def _open_pipeline_db_for_run(cwd: Path):
    """Open the pipeline DB if config is present; otherwise return None.

    Returns None silently when ``.apply-config.yaml`` is missing — the apply
    pipeline must keep working in scratch directories without a config (the
    bootstrap path will create one, but unit tests stub a minimal config that
    still resolves a default ``private/jobsmith.db`` path beneath ``cwd``).
    """
    from jobsmith.config import find_config, load_config
    from jobsmith.paths import resolve

    config_path = find_config(cwd)
    if config_path is None:
        return None
    try:
        from jobsmith.db import open_pipeline_db as _open_pipeline_db

        config = load_config(config_path)
        db_path = resolve(config.output.jobsmith_db, config_path.parent)
        return _open_pipeline_db(db_path)
    except Exception:  # noqa: BLE001 — DB is secondary to the pipeline
        return None


def core_run_apply(
    url: str,
    *,
    cwd: Path | None = None,
    events: object = None,  # EventSink — receives PipelineEvent via emit()
    confirm: object = None,  # ConfirmGate — decides inter-phase continuation
    skip_confirm: bool = False,
    force: bool = False,
    verbosity: int = 0,
    jd_text: str | None = None,
    slug: str | None = None,
    run_id: str | None = None,
    # Dependency injection: the CLI-coupled phase runner.
    # apply.py passes _run_apply_phases (Rich renderer aware).
    # API path / tests can pass a stub or None (no-op).
    phase_runner: Callable | None = None,
    # Bootstrap injected so core/pipeline.py stays CLI-free.
    bootstrap: Callable[[Path], None] | None = None,
    # When set to "gather", "draft", or "render", treat all earlier phases as
    # done and force-run from the named phase, bypassing the all-done early exit.
    start_from_phase: str | None = None,
) -> int:
    """Orchestrate the three-phase apply pipeline — business-logic entry point.

    This function is the pure-orchestration core of ``run_apply``. It handles:
    - Bootstrap (via injected *bootstrap* callable)
    - Master-content seeding
    - Slug resolution and force-reset
    - DB lifecycle: ``apply_runs`` INSERT before → UPDATE after
    - Temp-file management for out-of-band JD text
    - Delegation to *phase_runner* for the actual per-phase execution

    Parameters
    ----------
    url:
        Job description URL.
    cwd:
        Working directory (defaults to current directory).
    events:
        An :class:`~jobsmith.core.protocols.EventSink` instance. Passed
        through to *phase_runner* when provided.
    confirm:
        A :class:`~jobsmith.core.protocols.ConfirmGate` instance. Passed
        through to *phase_runner* when provided.
    skip_confirm:
        When True, phase-gate confirmations are bypassed (--yes flag).
    force:
        Ignore ``.url-index.json`` and any prior ``manifest.json``; start
        fresh from phase 1.
    verbosity:
        0 = quiet (default), 1 = -v, 2 = -vv.
    jd_text:
        Optional JD body text supplied out-of-band.
    slug:
        Explicit slug override (API path; bypasses URL index lookup).
    run_id:
        Explicit run ID (API path; supervisor correlates DB rows).
    phase_runner:
        Callable that executes all phases and returns an int exit code.
        Signature: ``phase_runner(url, resolved_cwd, slug, ...) -> int``.
        When None, returns 0 immediately (useful for tests that only need
        the DB scaffolding).
    bootstrap:
        Callable ``(cwd: Path) -> None`` that ensures the working directory
        is initialised. When None, bootstrap is skipped.

    Returns
    -------
    int
        Exit code: 0 on success or clean user abort, non-zero on error.
    """
    import contextlib as _contextlib
    import time
    import uuid as _uuid

    from jobsmith.config import find_config
    from jobsmith.core.manifest import load_manifest, phase_completed
    from jobsmith.core.paths import applications_dir, pipeline_db_path
    from jobsmith.core.slug import derive_slug
    from jobsmith.core.url_index import load_url_index, resolve_starting_slug

    resolved_cwd = cwd or Path.cwd()

    # Materialise jd_text into a temp file once.
    jd_text_file: Path | None = None
    if jd_text is not None and jd_text.strip():
        import os as _os
        import tempfile as _tempfile

        _fd, _tmp_path = _tempfile.mkstemp(
            prefix="jobsmith-jdtext-",
            suffix=".txt",
        )
        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(jd_text)
        jd_text_file = Path(_tmp_path)

    def _cleanup_jd_text_file() -> None:
        # roborev job 970 LOW: ensure the temp file is unlinked even on the
        # early-return paths (bootstrap failure, already-complete short-circuit)
        # that bypass the outer try/finally below.
        if jd_text_file is not None:
            with _contextlib.suppress(OSError):
                jd_text_file.unlink()

    # Step 1: ensure bootstrap
    if bootstrap is not None:
        try:
            bootstrap(resolved_cwd)
        except Exception as exc:
            if events is not None and hasattr(events, "emit"):
                events.emit(
                    PipelineEvent(
                        kind="phase_failed",
                        phase="bootstrap",
                        payload={"error": f"Bootstrap failed: {exc}"},
                    )
                )
            _cleanup_jd_text_file()
            return 1

    # Step 1b: seed master_content from disk YAMLs if not already loaded.
    try:
        _config_path = find_config(resolved_cwd)
        _db_path = pipeline_db_path(resolved_cwd)
        if _config_path is not None and _db_path is not None:
            from jobsmith.master_ingest import ensure_master_loaded as _ensure_master

            _ensure_master(_db_path, repo_root=_config_path.parent)
    except Exception as exc:  # noqa: BLE001 — degrade rather than abort.
        logger.warning("master_content seed failed: %s", exc)

    # Step 2: resolve starting slug.
    started_at = time.time()

    if slug is not None:
        from_index = False
        # caller-supplied slug: no force banner — handled by phase_runner
    elif force:
        index = load_url_index(resolved_cwd)
        if url in index:
            slug = index[url]
            from_index = True
        else:
            slug = derive_slug(url)
            from_index = False
    else:
        slug, from_index = resolve_starting_slug(url, resolved_cwd)

    # Step 3: phase-completion gating.
    apps_dir = applications_dir(resolved_cwd)
    app_dir = apps_dir / slug if apps_dir is not None else None
    manifest = None if force or app_dir is None else load_manifest(app_dir, resolved_cwd)

    # Force reset: wipe stale DB state so agents don't reuse prior envelopes.
    if force and app_dir is not None:
        _session_id_file = app_dir / ".apply-state" / "session-id"
        _session_id_file.unlink(missing_ok=True)
        _force_db_path = pipeline_db_path(resolved_cwd)
        if _force_db_path is not None and _force_db_path.exists():
            with _contextlib.suppress(Exception):
                from jobsmith.db import open_pipeline_db as _open_pipe_db
                from jobsmith.db import reset_state as _reset_state

                _conn = _open_pipe_db(_force_db_path)
                try:
                    _reset_state(_conn, slug=slug)
                finally:
                    _conn.close()

    phase_done: dict[str, bool] = {
        name: phase_completed(manifest, name) for name, _ in _PHASES
    }

    # start_from_phase override: mark earlier phases done, force-run the target.
    if start_from_phase is not None:
        phase_order = [name for name, _ in _PHASES]
        if start_from_phase in phase_order:
            target_idx = phase_order.index(start_from_phase)
            for i, name in enumerate(phase_order):
                if i < target_idx:
                    phase_done[name] = True
                elif i == target_idx:
                    phase_done[name] = False

    # All phases done → exit cleanly unless --force or start_from_phase overrides.
    if all(phase_done.values()) and app_dir is not None and start_from_phase is None:
        if events is not None and hasattr(events, "emit"):
            events.emit(
                PipelineEvent(
                    kind="already_complete",
                    phase="render",
                    payload={"app_dir": str(app_dir)},
                )
            )
        _cleanup_jd_text_file()
        return 0

    # DB scaffolding: insert apply_runs row before, UPDATE after.
    db_run_id = run_id or str(_uuid.uuid4())
    db_started_at_iso = _db_now_iso()
    db_conn = _open_pipeline_db_for_run(resolved_cwd)
    db_final_status = "failed"
    db_slug_ref = [slug]
    db_status_ref = ["unset"]

    if db_conn is not None:
        with _contextlib.suppress(Exception):
            from jobsmith.db import insert_apply_run as _insert_apply_run

            _insert_apply_run(
                db_conn,
                run_id=db_run_id,
                slug=slug,
                phase="unknown",
                started_at=db_started_at_iso,
                finished_at=None,
                status="running",
            )

    try:
        if phase_runner is None:
            # No runner injected — DB row is open but nothing executes.
            db_final_status = "done"
            return 0

        rc = phase_runner(
            url=url,
            resolved_cwd=resolved_cwd,
            events=events,
            confirm=confirm,
            skip_confirm=skip_confirm,
            force=force,
            verbosity=verbosity,
            slug=slug,
            apps_dir=apps_dir,
            phase_done=phase_done,
            started_at=started_at,
            db_conn=db_conn,
            db_run_id=db_run_id,
            db_slug_ref=db_slug_ref,
            db_status_ref=db_status_ref,
            jd_text_file=jd_text_file,
            start_from_phase=start_from_phase,
        )
        if rc != 0:
            db_final_status = "failed"
        elif db_status_ref[0] == "cancelled":
            db_final_status = "cancelled"
        else:
            db_final_status = "done"
        return rc
    finally:
        if jd_text_file is not None:
            with _contextlib.suppress(OSError):
                jd_text_file.unlink()
        if db_conn is not None:
            try:
                with _contextlib.suppress(Exception):
                    db_conn.execute(
                        "UPDATE apply_runs "
                        "SET status=?, finished_at=?, slug=? "
                        "WHERE run_id=?",
                        (
                            db_final_status,
                            _db_now_iso(),
                            db_slug_ref[0],
                            db_run_id,
                        ),
                    )
                    db_conn.commit()
            finally:
                db_conn.close()
