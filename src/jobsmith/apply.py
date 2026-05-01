"""apply.py — three-phase apply pipeline for `jobsmith apply <url>`.

Public API
----------
- :func:`derive_slug` — sanitize a JD URL into a filesystem-safe slug
- :func:`ensure_bootstrap` — auto-bootstrap `.apply-config.yaml` if missing
- :func:`build_phase_prompt` — construct the user prompt for each phase
- :func:`run_apply` — orchestrate all three phases with confirm gates
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click

from . import plugin_dir as get_plugin_dir
from . import headless
from .config import CONFIG_FILENAME, find_config, load_config
from .guard import check_anchors
from .paths import resolve

# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------

_PHASES = [
    ("gather", 1),
    ("draft", 2),
    ("render", 3),
]


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


def derive_slug(url: str) -> str:
    """Derive an application slug from a JD URL.

    Sanitizes to lowercase, alphanumeric + hyphens, max 60 chars.
    Falls back to a 12-char URL hash if no useful path segment exists.

    Parameters
    ----------
    url:
        Job description URL (or any string identifier).

    Returns
    -------
    str
        A filesystem-safe slug string.
    """
    try:
        parsed = urlparse(url)
        # Use the last non-empty path segment
        path_parts = [p for p in parsed.path.split("/") if p]
        raw = path_parts[-1] if path_parts else ""
    except Exception:
        raw = ""

    if raw:
        # Encode non-ASCII bytes as their hex representation, then lowercase
        try:
            raw = raw.encode("ascii").decode("ascii")
        except (UnicodeEncodeError, UnicodeDecodeError):
            # Replace non-ASCII with hyphens
            raw = raw.encode("ascii", errors="replace").decode("ascii")

        slug = raw.lower()
        # Replace any non-alphanumeric character with a hyphen
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        # Collapse consecutive hyphens
        slug = re.sub(r"-{2,}", "-", slug)
        # Strip leading/trailing hyphens
        slug = slug.strip("-")
    else:
        slug = ""

    # Fall back to URL hash if nothing useful remains
    if not slug:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        slug = digest

    # Enforce max 60 chars
    return slug[:60]


# ---------------------------------------------------------------------------
# Bootstrap check
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
    # Programmatic call to the init module's internals via the cli.init function.
    # We replicate the relevant scaffold logic to avoid Typer's Exit handling.
    _run_init(cwd)


def _run_init(target: Path) -> None:
    """Run the jobsmith scaffold logic programmatically (mirrors cli.init).

    Writes `.apply-config.yaml` and creates the standard directory structure.
    Avoids importing the Typer-decorated command directly (which raises
    SystemExit) — instead calls the underlying helpers.
    """
    from .cli import CONFIG_TEMPLATE, PROFILE_TEMPLATE, GITIGNORE_ADDITIONS, EXAMPLES_DIR

    target.mkdir(parents=True, exist_ok=True)

    # Master YAML stubs from examples (or empty stubs if examples missing)
    content_dir = target / "assets" / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    if EXAMPLES_DIR.exists():
        import shutil
        for src in EXAMPLES_DIR.glob("*.yml"):
            dst = content_dir / src.name
            if not dst.exists():
                shutil.copy(src, dst)
    else:
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml", "publication.yml"):
            stub = content_dir / name
            if not stub.exists():
                stub.write_text("# Populate me with your master content\n")

    # Config file
    config_path = target / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE)

    # Profile YAML
    profile_path = target / "private" / "capacity" / "profile.yaml"
    if not profile_path.exists():
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(PROFILE_TEMPLATE)

    # Applications dir
    apps_dir = target / "private" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)

    # .gitignore
    gitignore = target / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text()
        if "jobsmith" not in existing:
            gitignore.write_text(existing.rstrip() + "\n" + GITIGNORE_ADDITIONS)
    else:
        gitignore.write_text(GITIGNORE_ADDITIONS.lstrip())

    click.echo(
        f"Bootstrapped jobsmith repo at {target}. "
        "Edit assets/content/*.yml and .apply-config.yaml before running apply.",
        err=True,
    )


# ---------------------------------------------------------------------------
# Phase prompt construction
# ---------------------------------------------------------------------------


def build_phase_prompt(phase: str, slug: str, url: str) -> str:
    """Return the user prompt text for a given phase.

    The system prompt (loaded via ``--system-prompt-file``) carries the full
    phase instructions. This user prompt gives the agent its immediate task.

    Parameters
    ----------
    phase:
        One of ``"gather"``, ``"draft"``, ``"render"``.
    slug:
        Application slug derived from the URL.
    url:
        Original JD URL.

    Returns
    -------
    str
        Short user prompt text.

    Raises
    ------
    ValueError
        If *phase* is not one of the three recognised phases.
    """
    if phase == "gather":
        return (
            f"Process this JD: {url}. Application slug: {slug}. "
            "Begin Phase 1 (gather): jd-parse, fit, HM enrichment, company research, "
            "bullet selection. Pause at the analysis gate as instructed."
        )
    if phase == "draft":
        return (
            f"Resume Phase 2 (draft) for slug {slug}. "
            "Read .apply-state/ artifacts. Run prose-writer + prose-qa loop until pass."
        )
    if phase == "render":
        return (
            f"Resume Phase 3 (render) for slug {slug}. "
            "Render resume PDF, ATS check, visual layout, cover letter, index page, "
            "then jobsmith assemble."
        )
    raise ValueError(f"Unknown phase: {phase!r}. Expected one of: gather, draft, render.")


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------


def _render_event(event: headless.Event) -> Optional[str]:
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

    result = check_anchors(master_path, selection_path)

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
# Top-level pipeline
# ---------------------------------------------------------------------------


def run_apply(url: str, *, cwd: Optional[Path] = None, skip_confirm: bool = False) -> int:
    """Run the three-phase apply pipeline.

    Parameters
    ----------
    url:
        Job description URL.
    cwd:
        Working directory (defaults to current directory).
    skip_confirm:
        When True, phase-gate confirmations are bypassed (--yes flag).

    Returns
    -------
    int
        Exit code: 0 on success or clean user abort, non-zero on error.
    """
    resolved_cwd = cwd or Path.cwd()

    # Step 1: ensure bootstrap
    try:
        ensure_bootstrap(resolved_cwd)
    except Exception as exc:
        click.echo(f"Bootstrap failed: {exc}", err=True)
        return 1

    # Step 2: derive slug and session
    slug = derive_slug(url)
    session_id = headless.deterministic_session_id(slug)
    plugin_directory = get_plugin_dir()

    click.echo(f"jobsmith apply: slug={slug!r}  session={session_id}", err=True)

    for phase_name, phase_num in _PHASES:
        # Step 3a: determine resume flag
        resume = (phase_name != "gather") and headless.session_exists(session_id, cwd=resolved_cwd)

        # Step 3b: resolve system prompt path
        system_prompt = plugin_directory / "system-prompts" / f"phase-{phase_num}-{phase_name}.md"
        if not system_prompt.exists():
            click.echo(f"ERROR: system prompt not found: {system_prompt}", err=True)
            return 1

        # Step 3c: build prompt text
        prompt_text = build_phase_prompt(phase_name, slug, url)

        click.echo(f"\n--- Phase {phase_num} ({phase_name}) ---", err=True)

        # Step 3d: stream events
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
            ):
                line = _render_event(event)
                if line:
                    click.echo(line, err=True)

                if event.type == "phase_complete":
                    phase_succeeded = True
                    break

                if event.type == "phase_failed":
                    click.echo(
                        f"Phase {event.name} failed: {event.error or '(no reason given)'}. "
                        "Aborting before subsequent phases.",
                        err=True,
                    )
                    return 3

                if event.type == "error":
                    click.echo(f"Phase {phase_name} encountered an error. Aborting.", err=True)
                    return 2
        except Exception as exc:
            click.echo(f"Unexpected error in phase {phase_name}: {exc}", err=True)
            return 2

        if not phase_succeeded:
            click.echo(
                f"Phase {phase_name} did not emit a phase_complete signal. "
                "Check output above for errors.",
                err=True,
            )
            return 2

        # Step 3e: between-phase orchestration. After gather (phase 1) we
        # run the anchor guard + relevance inquiry to produce the
        # bullet-decisions.json that phase 2 (draft) requires as input.
        if phase_name == "gather":
            rc = _run_step45_orchestration(slug, resolved_cwd)
            if rc != 0:
                return rc

        # Step 3f: confirm gate (not after the last phase)
        if not skip_confirm and phase_name != "render":
            if not click.confirm(f"Phase {phase_num} ({phase_name}) complete. Proceed to next phase?"):
                click.echo("Stopped at user request. Partial work saved.", err=True)
                return 0

    click.echo("\njobsmith apply complete.", err=True)
    return 0
