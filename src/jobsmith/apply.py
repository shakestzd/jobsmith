"""apply.py — three-phase apply pipeline for `jobsmith apply <url>`.

Public API
----------
- :func:`derive_slug` — sanitize a JD URL into a filesystem-safe slug
- :func:`ensure_bootstrap` — auto-bootstrap `.apply-config.yaml` if missing
- :func:`build_phase_prompt` — construct the user prompt for each phase
- :func:`run_phase_iter` — generator yielding :class:`PipelineEvent` objects
  per phase completion (gather→draft→render). Consumers observe phase-granular
  events; per-specialist granularity is deferred to slice 8.
- :func:`run_apply` — orchestrate all three phases with confirm gates and
  per-phase resume from completed work (via ``manifest.json`` + URL index).
  Internally drives ``run_phase_iter()`` and discards events for the CLI path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import click

from . import headless
from . import plugin_dir as get_plugin_dir
from .benchmarks import resolve_benchmark_or_fallback
from .config import CONFIG_FILENAME, find_config, load_config
from .guard import check_anchors
from .paths import resolve
from .render import ApplyRenderer

# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------

_PHASES = [
    ("gather", 1),
    ("draft", 2),
    ("render", 3),
]

# render runs 6 specialists sequentially; gather/draft finish well under 30
_PHASE_MAX_TURNS: dict[str, int] = {
    "gather": 30,
    "draft": 30,
    "render": 60,
}


# ---------------------------------------------------------------------------
# PipelineEvent — phase-granular events from run_phase_iter()
# ---------------------------------------------------------------------------


@dataclass
class PipelineEvent:
    """A phase-granular event emitted by :func:`run_phase_iter`.

    Attributes
    ----------
    kind:
        Event kind. One of:
        - ``"phase_started"``  — phase loop entered.
        - ``"phase_complete"`` — phase emitted ``<<PHASE_COMPLETE>>``.
        - ``"phase_failed"``   — phase emitted ``<<PHASE_FAILED>>``.
        - ``"slug_changed"``   — canonical slug differs from starting slug
          after gather reconciliation.
        - ``"guard_failed"``   — ``_run_step45_orchestration`` returned non-zero.
        - ``"cancelled"``      — generator stopped because ``cancel_event`` was set.
    phase:
        Phase name at time of event (``"gather"``, ``"draft"``, ``"render"``).
    payload:
        Kind-specific data dict. ``"slug_changed"`` carries
        ``{"old_slug": ..., "new_slug": ...}``; ``"guard_failed"`` carries
        ``{"rc": ...}``; others are ``{}``.
    """

    kind: str
    phase: str
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


def _slugify_part(s: str) -> str:
    """Convert an arbitrary string into a lowercase hyphenated slug component.

    Lowercases, replaces non-alphanumeric runs with a single hyphen, and
    strips leading/trailing hyphens.  Used by both :func:`derive_slug` and
    :func:`_reconcile_canonical_slug` so slug-cleaning logic stays DRY.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


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

        slug = _slugify_part(raw)
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
    from .cli import CONFIG_TEMPLATE, EXAMPLES_DIR, GITIGNORE_ADDITIONS, PROFILE_TEMPLATE

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


def _build_paths(slug: str, cwd: Path, plugin_directory: Path) -> dict[str, str]:
    """Build the paths dict injected into each phase prompt.

    Called once per phase so that ``apply_state_dir`` always uses the
    current (possibly post-reconcile) slug.

    Returns a flat string→string mapping of absolute paths.  Optional
    master YAMLs (``publication_yml``) are omitted when not configured.
    When ``.apply-config.yaml`` cannot be found the dict contains only the
    plugin-side paths (agent still gets them).
    """
    config_path = find_config(cwd)

    result: dict[str, str] = {
        "plugin_dir": str(plugin_directory.resolve()),
        "agent_dir": str((plugin_directory / "agents").resolve()),
        "specialist_contracts": str(
            (plugin_directory / "agents" / "apply" / "specialist-contracts.yaml").resolve()
        ),
    }

    if config_path is not None:
        result["config"] = str(config_path.resolve())
        config = load_config(config_path)
        repo_root = config_path.parent

        # Master YAMLs — include only those that are configured (non-None)
        result["master.work_yml"] = str(resolve(config.master.work_yml, repo_root))
        result["master.skill_yml"] = str(resolve(config.master.skill_yml, repo_root))
        result["master.education_yml"] = str(resolve(config.master.education_yml, repo_root))
        result["master.author_yml"] = str(resolve(config.master.author_yml, repo_root))
        if config.master.publication_yml is not None:
            result["master.publication_yml"] = str(
                resolve(config.master.publication_yml, repo_root)
            )
        if config.master.award_yml is not None:
            result["master.award_yml"] = str(resolve(config.master.award_yml, repo_root))
        # Slice C: projects schema. Inject the raw path AND a filtered JSON
        # so the bullet-selector can include the projects already pre-filtered
        # (excluded_from_resume / excluded_kinds / is_project / homepage URL).
        # The pre-filter happens here rather than in the agent so the agent
        # never sees suppressed entries.
        if config.master.projects_yml is not None:
            projects_path = resolve(config.master.projects_yml, repo_root)
            if projects_path.exists():
                result["master.projects_yml"] = str(projects_path)

        # apply_state_dir — absolute path for the current slug
        apps_dir = resolve(config.output.applications_dir, repo_root)
        result["apply_state_dir"] = str(apps_dir / slug / ".apply-state")

        # Benchmark paths — resolve for the three specialists that consume them.
        # Falls back to Pat Doe files when user hasn't configured benchmarks.
        # Skip the key entirely when no benchmark is available (resolver returned
        # None) so we never inject a non-existent path. The bundled Pat Doe pack
        # only ships resume.qmd + cover-letter.md, so resume_pdf has no fallback;
        # specialists treat the absent key as "no benchmark available for this
        # field" rather than reading a missing file.
        # Raises BenchmarkRequiredError only when benchmarks.required=True and
        # the field is unset — in that case we propagate up to the caller.
        for field, key in (
            ("resume_qmd", "benchmark.resume_qmd"),
            ("cover_letter_md", "benchmark.cover_letter_md"),
            ("resume_pdf", "benchmark.resume_pdf"),
        ):
            path = resolve_benchmark_or_fallback(field, config, repo_root)
            if path is not None:
                result[key] = str(path)

        # Feedback directory — soft style lessons for prose-writer + cover-letter-writer.
        # Present only when the directory exists; absent key means "no feedback yet".
        feedback_dir = repo_root / "private" / "feedback"
        if feedback_dir.exists():
            result["feedback.dir"] = str(feedback_dir.resolve())

        # Voice profile (Slice B.1) — derived from benchmarks.resume_qmd by
        # voice.load_voice_profile() and cached at .apply-state/voice-profile.json.
        # tell-fixer / prose-writer / cover-letter-writer read banned_verbs /
        # banned_adjectives / result_verbs from this JSON instead of inlining
        # them. We compute the profile here so the cache is written before any
        # specialist runs; load_voice_profile() handles cache hit/miss internally.
        # Pass the already-resolved benchmark path so voice.py never has to
        # re-resolve relative to CWD (would silently miss the file when
        # `jobsmith apply` is invoked from a subdirectory).
        from .voice import load_voice_profile  # local import — avoid circular at module load
        voice_cache_dir = apps_dir / slug / ".apply-state"
        resolved_benchmark = result.get("benchmark.resume_qmd")
        # Voice profile is non-blocking: if computation fails (corrupt
        # benchmark, etc.), specialists fall back to seed defaults.
        with contextlib.suppress(Exception):
            load_voice_profile(
                config,
                cache_dir=voice_cache_dir,
                benchmark_path_override=Path(resolved_benchmark) if resolved_benchmark else None,
            )
        result["voice_profile_json"] = str(voice_cache_dir / "voice-profile.json")

        # Slice C: pre-filter projects.yml and emit projects-filtered.json so
        # bullet-selector consumes only entries that pass the kind / homepage /
        # excluded_from_resume / is_project filters. The agent never sees
        # suppressed entries — this prevents the Clay bug where the user's
        # portfolio site was wrongly listed as a project deliverable.
        if config.master.projects_yml is not None:
            projects_path = resolve(config.master.projects_yml, repo_root)
            if projects_path.exists():
                from .assemble import load_projects
                # author.homepage may not be loaded yet; we resolve it best-effort
                # from author.yml so the URL-matches filter works.
                author_yml_path = resolve(config.master.author_yml, repo_root)
                author_homepage: str | None = None
                if author_yml_path.exists():
                    try:
                        import yaml as _yaml  # local — only here for one-shot read
                        ay = _yaml.safe_load(author_yml_path.read_text())
                        author = (ay or {}).get("author")
                        if isinstance(author, list) and author:
                            author = author[0]
                        if isinstance(author, dict):
                            author_homepage = (author.get("homepage") or "").strip() or None
                    except Exception:
                        author_homepage = None
                try:
                    # Pass the EXACT projects file path — load_projects accepts
                    # a file or directory. Earlier we passed parent which only
                    # worked for files literally named "projects.yml".
                    filtered = load_projects(
                        projects_path, config.resume, author_homepage
                    )
                    voice_cache_dir.mkdir(parents=True, exist_ok=True)
                    filtered_path = voice_cache_dir / "projects-filtered.json"
                    import json as _json
                    filtered_path.write_text(_json.dumps(filtered, indent=2))
                    result["projects_filtered_json"] = str(filtered_path)
                except Exception:
                    # Pre-filter is non-blocking; bullet-selector falls back
                    # to "no projects" when the key is absent.
                    pass

    return result


def _format_paths_block(paths: dict[str, str]) -> str:
    """Format a deterministic Paths block for injection into a phase prompt.

    Keys are sorted alphabetically so the block is stable across runs.
    Returns an empty string when *paths* is empty (omit block from prompt).
    """
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
) -> str:
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
    paths:
        Optional flat string→string mapping of absolute paths to inject.
        When provided (and non-empty) a "Paths" block is appended to the
        prompt so the agent does not need to search the filesystem.
        Defaults to ``{}`` — omits the block (useful for unit tests that
        do not need path injection).

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
        return _with_paths(
            f"Process this JD: {url}. Application slug: {slug}. "
            "Begin Phase 1 (gather): jd-parse, fit, HM enrichment, company research, "
            "bullet selection. Pause at the analysis gate as instructed."
        )
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
# Slug reconciliation — canonical slug from jd-parsed.json
# ---------------------------------------------------------------------------


def _reconcile_canonical_slug(
    active_slug: str, cwd: Path, started_at: float
) -> tuple[str, bool]:
    """Return ``(slug, reconciled)`` after an optional directory rename.

    After phase 1 (gather), the specialist ``apply-jd-parser`` derives a
    canonical slug of the form ``{company-slug}-{position-slug}`` and writes
    artifacts under *that* directory.  The wrapper may have pre-created a
    different directory from the raw URL.  This helper reconciles the two.

    The second return value, ``reconciled``, signals whether the slug was
    successfully derived from a ``jd-parsed.json`` (i.e. the helper produced
    a true canonical slug, not a fallback).  Callers MUST gate persistence
    of the URL → slug mapping on this flag — recording a non-canonical slug
    would corrupt future resume lookups.

    Strategy
    --------
    1. Try ``applications/{active_slug}/.apply-state/jd-parsed.json``.
    2. Fallback: glob ``applications/*/.apply-state/jd-parsed.json`` and pick
       the most recently modified candidate **whose mtime is >= started_at**
       (produced during this run).  Stale prior-run artifacts are skipped to
       prevent picking up an unrelated job's slug.
    3. Compute canonical = ``_slugify_part(company) + "-" + _slugify_part(position)``.
    4. If the *owning* directory's name differs from canonical, rename it.
       If the canonical directory already exists and is non-empty, halt with
       a controlled error and return ``(active_slug, False)`` — the user
       must resolve the collision manually, and the index must NOT be
       updated to point at the active (non-canonical) slug.
    5. Return ``(canonical, True)``.

    When no qualifying ``jd-parsed.json`` is found, returns
    ``(active_slug, False)`` and logs a warning.
    """
    config_path = find_config(cwd)
    if config_path is None:
        click.echo(
            "reconcile: cannot locate .apply-config.yaml — skipping slug reconciliation.",
            err=True,
        )
        return active_slug, False

    config = load_config(config_path)
    repo_root = config_path.parent
    apps_dir = resolve(config.output.applications_dir, repo_root)

    # 1. Primary: check the active slug's apply-state
    primary = apps_dir / active_slug / ".apply-state" / "jd-parsed.json"
    if primary.exists():
        jd_path = primary
        owning_dir = apps_dir / active_slug
    else:
        # 2. Fallback: candidates produced during this run only
        # (mtime >= started_at, with a 1s buffer for filesystem clock drift).
        threshold = started_at - 1.0
        candidates = [
            p
            for p in apps_dir.glob("*/.apply-state/jd-parsed.json")
            if p.stat().st_mtime >= threshold
        ]
        if not candidates:
            click.echo(
                "reconcile: no jd-parsed.json produced in this run — cannot derive canonical slug.",
                err=True,
            )
            return active_slug, False
        jd_path = max(candidates, key=lambda p: p.stat().st_mtime)
        owning_dir = jd_path.parent.parent  # strip "/.apply-state/jd-parsed.json"

    # 3. Derive canonical slug from company + position fields
    try:
        jd_data = json.loads(jd_path.read_text())
        company = jd_data.get("company", "")
        position = jd_data.get("position", "")
    except Exception as exc:
        click.echo(
            f"reconcile: failed to parse jd-parsed.json ({exc}) — skipping.",
            err=True,
        )
        return active_slug, False

    if not company or not position:
        click.echo(
            "reconcile: jd-parsed.json missing company or position — skipping.",
            err=True,
        )
        return active_slug, False

    canonical = f"{_slugify_part(company)}-{_slugify_part(position)}"

    # 4. Rename owning dir if it doesn't already have the canonical name.
    # shutil.move into an existing directory NESTS the source inside the
    # target rather than renaming, which would leave phase 2/3 reading stale
    # canonical artifacts. Detect collisions and refuse to merge.
    if owning_dir.name != canonical:
        canonical_dir = apps_dir / canonical
        if canonical_dir.exists():
            try:
                same = canonical_dir.resolve() == owning_dir.resolve()
            except OSError:
                same = False
            if same:
                pass
            elif any(canonical_dir.iterdir()):
                click.echo(
                    f"reconcile: canonical dir {canonical_dir} already exists "
                    f"and is non-empty; refusing to merge with {owning_dir.name!r}. "
                    "Resolve manually (rename or remove the stale directory) and re-run.",
                    err=True,
                )
                return active_slug, False
            else:
                canonical_dir.rmdir()
                shutil.move(str(owning_dir), str(canonical_dir))
                click.echo(
                    f"reconcile: renamed {owning_dir.name!r} → {canonical!r}",
                    err=True,
                )
        else:
            shutil.move(str(owning_dir), str(canonical_dir))
            click.echo(
                f"reconcile: renamed {owning_dir.name!r} → {canonical!r}",
                err=True,
            )

    return canonical, True


# ---------------------------------------------------------------------------
# Path resolution helper
# ---------------------------------------------------------------------------


def _apply_state_dir(slug: str, cwd: Path) -> Path | None:
    """Resolve the ``.apply-state`` directory for *slug* under the project root.

    Returns None if ``.apply-config.yaml`` cannot be found, so callers can
    silently skip operations that require the directory.
    """
    config_path = find_config(cwd)
    if config_path is None:
        return None
    config = load_config(config_path)
    repo_root = config_path.parent
    return resolve(config.output.applications_dir, repo_root) / slug / ".apply-state"


def _snapshot_phase_drafts(phase: str, slug: str, cwd: Path) -> None:
    """Persist immutable agent-draft snapshots after a phase completes.

    `jobsmith feedback record` needs a stable agent baseline to diff against
    user edits. Specialists overwrite their drafts on revision (and the user
    may edit the live files), so we snapshot once at phase-completion and
    treat ``*-draft.agent.md`` as read-only thereafter.

    - After phase ``draft`` (phase 2): snapshot
      ``.apply-state/prose-draft.md`` → ``.apply-state/prose-draft.agent.md``.
    - After phase ``render`` (phase 3): snapshot the agent-written cover
      letter — usually at ``<app>/cover-letter-draft.md`` (where
      apply-cover-letter-writer writes), with a fallback to
      ``.apply-state/cover-letter-draft.md`` if the humanizer pass left one
      there — into ``.apply-state/cover-letter-draft.agent.md``.

    Silently no-ops when source files are missing or the state dir cannot
    be resolved; the apply pipeline is the source of truth and we don't
    want to fail a successful phase on a snapshot hiccup.
    """
    state_dir = _apply_state_dir(slug, cwd)
    if state_dir is None or not state_dir.is_dir():
        return
    app_dir = state_dir.parent

    if phase == "draft":
        src = state_dir / "prose-draft.md"
        if src.exists():
            shutil.copy2(src, state_dir / "prose-draft.agent.md")

    elif phase == "render":
        # Prefer the app-root copy (where apply-cover-letter-writer writes
        # per its prompt); fall back to the state-dir humanizer artifact.
        src_root = app_dir / "cover-letter-draft.md"
        src_state = state_dir / "cover-letter-draft.md"
        src = src_root if src_root.exists() else src_state
        if src.exists():
            shutil.copy2(src, state_dir / "cover-letter-draft.agent.md")


# ---------------------------------------------------------------------------
# URL → canonical slug index + per-phase resume helpers
# ---------------------------------------------------------------------------

URL_INDEX_FILENAME = ".url-index.json"

# Specialists whose successful invocation marks each phase as complete.
# A specialist is considered "done" when manifest.json.invocations contains an
# entry with that ``specialist`` name and ``status`` == "ok".
_PHASE_REQUIRED_SPECIALISTS: dict[str, tuple[str, ...]] = {
    "gather": (
        "apply-jd-parser",
        "apply-fit-scorer",
        "apply-hm-enricher",
        "apply-bullet-selector",
        "apply-company-research",
    ),
    "draft": (
        "apply-prose-writer",
        "apply-prose-qa",
    ),
    "render": (
        "apply-resume-renderer",
        "apply-cover-letter-writer",
        "apply-index-writer",
    ),
}


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


def _applications_dir(cwd: Path) -> Path | None:
    """Resolve the absolute ``applications/`` directory, or None if config absent."""
    config_path = find_config(cwd)
    if config_path is None:
        return None
    config = load_config(config_path)
    repo_root = config_path.parent
    return resolve(config.output.applications_dir, repo_root)


def _url_index_path(cwd: Path) -> Path | None:
    """Return the absolute path of ``applications/.url-index.json``."""
    apps_dir = _applications_dir(cwd)
    if apps_dir is None:
        return None
    return apps_dir / URL_INDEX_FILENAME


def _load_url_index(cwd: Path) -> dict[str, str]:
    """Read the URL → canonical-slug index. Returns ``{}`` on missing/malformed."""
    idx_path = _url_index_path(cwd)
    if idx_path is None or not idx_path.exists():
        return {}
    try:
        data = json.loads(idx_path.read_text())
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_url_index(cwd: Path, index: dict[str, str]) -> None:
    """Atomically write the URL → canonical-slug index."""
    idx_path = _url_index_path(cwd)
    if idx_path is None:
        return
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(idx_path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    tmp.replace(idx_path)


def _scan_for_url_match(url: str, cwd: Path) -> str | None:
    """Scan ``applications/*/.apply-state/jd-parsed.json`` for one matching *url*.

    Checks ``jd_url``, ``url``, and ``apply_url`` fields in that order.  Returns
    the slug of the matching directory, or None if no candidate matches.
    """
    apps_dir = _applications_dir(cwd)
    if apps_dir is None or not apps_dir.exists():
        return None
    for jd_path in apps_dir.glob("*/.apply-state/jd-parsed.json"):
        try:
            data = json.loads(jd_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("jd_url", "url", "apply_url"):
            if data.get(key) == url:
                return jd_path.parent.parent.name
    return None


def _resolve_starting_slug(url: str, cwd: Path) -> tuple[str, bool]:
    """Resolve which slug to start the run under.

    Returns ``(slug, from_index)`` where ``from_index`` is True iff the slug
    came from the persisted URL index or a one-time migration scan.  Falls
    back to the URL-derived slug when neither lookup succeeds.
    """
    index = _load_url_index(cwd)
    if url in index:
        return index[url], True
    # One-time migration: if the URL isn't in the index, scan jd-parsed.json
    # files under applications/* for a matching jd_url/url/apply_url field.
    scanned = _scan_for_url_match(url, cwd)
    if scanned:
        index[url] = scanned
        _save_url_index(cwd, index)
        return scanned, True
    return derive_slug(url), False


def resolve_canonical_slug(url: str, cwd: Path) -> str:
    """Public accessor on top of :func:`_resolve_starting_slug`.

    External callers (slice-4 NotebookRunner, slice-8 single-specialist
    re-runs) need the same canonical slug that ``run_phase_iter`` will use
    so DB rows, manifest resets, and post-phase ingestion all target the
    same application directory. Returns just the slug; the boolean
    "from_index" flag is an internal concern.
    """
    slug, _from_index = _resolve_starting_slug(url, cwd)
    return slug


def _record_url_mapping(url: str, canonical_slug: str, cwd: Path) -> None:
    """Persist URL → canonical slug into the index, creating it if absent."""
    index = _load_url_index(cwd)
    if index.get(url) == canonical_slug:
        return
    index[url] = canonical_slug
    _save_url_index(cwd, index)


def _load_manifest(app_dir: Path) -> dict | None:
    """Read ``app_dir/.apply-state/manifest.json``. Returns None on missing/malformed."""
    manifest_path = app_dir / ".apply-state" / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _phase_completed(manifest: dict | None, phase_name: str) -> bool:
    """Return True iff every required specialist for *phase_name* is done.

    "Done" means the manifest's ``invocations`` list contains at least one
    entry per required specialist with ``status == "ok"``.  Missing manifest
    or malformed invocations always return False — callers re-run the phase.
    """
    if not manifest:
        return False
    required = _PHASE_REQUIRED_SPECIALISTS.get(phase_name, ())
    if not required:
        return False
    invocations = manifest.get("invocations")
    if not isinstance(invocations, list):
        return False
    completed_specialists = {
        inv.get("specialist")
        for inv in invocations
        if isinstance(inv, dict) and inv.get("status") == "ok"
    }
    return all(spec in completed_specialists for spec in required)


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
# Generator: run_phase_iter()
# ---------------------------------------------------------------------------


def run_phase_iter(
    url: str,
    *,
    cwd: Path | None = None,
    skip_confirm: bool = False,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    phases: list[str] | None = None,
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
        When set, the generator stops after the current phase completes and
        does not start subsequent phases.  Consumers MUST also propagate the
        event to ``headless.run_phase`` (via the ``cancel_event`` kwarg) so
        a running subprocess is terminated.
    phases:
        When provided, restrict execution to the named phases (in their
        canonical gather → draft → render order). ``None`` means "run all
        not-yet-complete phases" (the default). Used by slice-8 single-
        specialist re-runs to avoid re-running upstream phases (roborev #921).

    Yields
    ------
    PipelineEvent
        Events in order: ``phase_started`` → ``phase_complete`` (or
        ``phase_failed`` / ``guard_failed``) per phase, with an optional
        ``slug_changed`` event between gather and draft.  A ``cancelled``
        event is the final event when ``cancel_event`` was set.
    """
    import time as _time

    resolved_cwd = cwd or Path.cwd()

    # Step 1: bootstrap
    ensure_bootstrap(resolved_cwd)

    # Step 2: resolve starting slug
    started_at = _time.time()
    plugin_directory = get_plugin_dir()

    if force:
        index = _load_url_index(resolved_cwd)
        slug = index[url] if url in index else derive_slug(url)
    else:
        slug, _from_index = _resolve_starting_slug(url, resolved_cwd)

    # Step 3: phase-completion gating
    apps_dir = _applications_dir(resolved_cwd)
    app_dir = apps_dir / slug if apps_dir is not None else None
    manifest = None if force or app_dir is None else _load_manifest(app_dir)

    session_id = headless.deterministic_session_id(slug)

    phase_done: dict[str, bool] = {
        name: _phase_completed(manifest, name) for name, _ in _PHASES
    }

    # All done → no events to yield
    if all(phase_done.values()) and app_dir is not None:
        return

    # Filter to the requested phases (preserving canonical ordering).
    # phases=None means "run every phase that's not yet complete" — the
    # historical behavior. phases=[name] from slice-8 re-runs scopes work
    # to a single phase so re-running apply-prose-writer (draft) does NOT
    # re-fire the gather phase first (roborev #921 HIGH).
    if phases is not None:
        requested = set(phases)
        active_phases = [(n, num) for n, num in _PHASES if n in requested]
    else:
        active_phases = list(_PHASES)

    for phase_name, phase_num in active_phases:
        # Check cancel before starting each phase
        if cancel_event is not None and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase=phase_name)
            return

        # Step 3pre: anchor guard before draft
        if phase_name == "draft" and not phase_done["draft"]:
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None and not (state_dir / "bullet-decisions.json").exists():
                rc = _run_step45_orchestration(slug, resolved_cwd)
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
        phase_paths = _build_paths(slug, resolved_cwd, plugin_directory)
        prompt_text = build_phase_prompt(phase_name, slug, url, paths=phase_paths)

        # Step 3f: stream events from headless
        phase_succeeded = False
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
            new_slug, reconciled = _reconcile_canonical_slug(
                slug, resolved_cwd, started_at
            )
            if new_slug != slug:
                old_slug = slug
                slug = new_slug
                session_id = headless.deterministic_session_id(slug)
                # Emit slug_changed and pause up to 1s for consumer ack.
                # The consumer MUST rebind their slug variable; the runner
                # does not hold any file handles to the app dir across the rename.
                ev = PipelineEvent(
                    kind="slug_changed",
                    phase=phase_name,
                    payload={"old_slug": old_slug, "new_slug": slug},
                )
                yield ev
                # 1-second timeout: we cannot block indefinitely waiting for
                # the consumer to ack. The event has already been yielded; if
                # the consumer cares it reads the event synchronously. The
                # timeout here is just a documentation-level signal.
                _time.sleep(0)  # cooperative yield point
            if reconciled:
                _record_url_mapping(url, slug, resolved_cwd)

        yield PipelineEvent(kind="phase_complete", phase=phase_name)

        # Check cancel after phase completes (before starting next)
        if cancel_event is not None and cancel_event.is_set():
            yield PipelineEvent(kind="cancelled", phase=phase_name)
            return


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
) -> int:
    """Run the three-phase apply pipeline.

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

    Returns
    -------
    int
        Exit code: 0 on success or clean user abort, non-zero on error.
    """
    resolved_cwd = cwd or Path.cwd()
    rdr = renderer or ApplyRenderer(yes=skip_confirm, verbosity=verbosity)

    # Step 1: ensure bootstrap
    try:
        ensure_bootstrap(resolved_cwd)
    except Exception as exc:
        rdr.print_error(f"Bootstrap failed: {exc}")
        return 1

    # Step 2: resolve starting slug. With --force, bypass the URL index and
    # use the URL-derived slug (a fresh run). Otherwise look up the URL in
    # the persisted index, falling back to a one-time migration scan, and
    # finally to the URL-derived slug.
    import time

    started_at = time.time()
    plugin_directory = get_plugin_dir()

    if force:
        # --force restarts the pipeline but must still target the existing
        # canonical directory if we know about it; otherwise phase 1 writes
        # under the URL slug and the post-phase-1 reconcile would refuse to
        # merge into the non-empty canonical dir, leaving us in a broken
        # state that corrupts the URL index. Consult the persisted index
        # first; fall back to URL-derived slug only when the URL is unknown.
        index = _load_url_index(resolved_cwd)
        if url in index:
            slug = index[url]
            from_index = True
        else:
            slug = derive_slug(url)
            from_index = False
        rdr.print_force_banner()
    else:
        slug, from_index = _resolve_starting_slug(url, resolved_cwd)

    session_id = headless.deterministic_session_id(slug)

    rdr.print_info(f"jobsmith apply: slug={slug!r}  session={session_id}")

    # Step 3: phase-completion gating. Read manifest at the resolved app dir
    # (when not --force) and decide which phases to skip. Manifest absence,
    # malformed JSON, or missing invocations all fall through to "rerun".
    apps_dir = _applications_dir(resolved_cwd)
    app_dir = apps_dir / slug if apps_dir is not None else None
    manifest = None if force or app_dir is None else _load_manifest(app_dir)

    phase_done: dict[str, bool] = {
        name: _phase_completed(manifest, name) for name, _ in _PHASES
    }

    # All phases done → print summary and exit cleanly unless --force.
    if all(phase_done.values()) and app_dir is not None:
        rdr.print_already_complete(app_dir)
        return 0

    # Resume banner above the first phase that will actually run, when
    # phases earlier than that one are being skipped.
    first_to_run = next(name for name, _ in _PHASES if not phase_done[name])
    if first_to_run != "gather":
        first_phase_num = next(
            num for name, num in _PHASES if name == first_to_run
        )
        rdr.print_resume_banner(slug, first_phase_num, first_to_run)

    total_phases = len(_PHASES)

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
                rc = _run_step45_orchestration(slug, resolved_cwd)
                if rc != 0:
                    return rc

        # Skip phases already marked complete in manifest.json.
        if phase_done[phase_name]:
            rdr.print_phase_skipped(phase_num, phase_name)
            continue

        # Step 3a: determine resume flag (Claude session continuity).
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

        # Step 3d: build prompt text
        prompt_text = build_phase_prompt(phase_name, slug, url, paths=phase_paths)

        # Step 3e: render phase header and start spinner
        rdr.print_header(phase_num, total_phases, phase_name)
        rdr.start_phase(phase_name)

        # Step 3e2: open transcript for this phase (always, regardless of verbosity)
        state_dir_for_transcript = _apply_state_dir(slug, resolved_cwd)
        if state_dir_for_transcript is not None:
            transcript_path = state_dir_for_transcript / "transcript.jsonl"
            rdr.open_transcript(transcript_path, phase_name)

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
            ):
                rdr.render_event(event)

                if event.type == "phase_complete":
                    phase_succeeded = True
                    break

                if event.type == "phase_failed":
                    rdr.close_transcript()
                    rdr.print_error(
                        "Aborting before subsequent phases."
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
            new_slug, reconciled = _reconcile_canonical_slug(
                slug, resolved_cwd, started_at
            )
            if new_slug != slug:
                slug = new_slug
                session_id = headless.deterministic_session_id(slug)
            if reconciled:
                _record_url_mapping(url, slug, resolved_cwd)
            # Render per-phase summary panel before the confirm gate
            state_dir = _apply_state_dir(slug, resolved_cwd)
            if state_dir is not None:
                rdr.render_phase_summary("gather", state_dir)

        # Step 3h: confirm gate (not after the last phase, and not after a
        # phase that was skipped — only fresh-run phases prompt).
        if not skip_confirm and phase_name != "render":
            rdr.pause_before_confirm()
            if not click.confirm(f"Phase {phase_num} ({phase_name}) complete. Proceed to next phase?"):
                rdr.print_info("Stopped at user request. Partial work saved.")
                return 0

    rdr.print_complete()
    return 0
