"""jobsmith.core.session — Claude session-ID helpers (feat-55152c31, Slice 2c).

Pure helpers with no Rich/Click/Typer dependencies. Relocated from apply.py.
"""
from __future__ import annotations

import sys
import uuid
from collections.abc import Callable
from pathlib import Path


def claude_session_file_path(session_id: str, cwd: Path) -> Path:
    """Return the Claude Code SDK session file path for *session_id*.

    The SDK stores conversation history under::

        ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl

    where ``<encoded-cwd>`` is the absolute cwd path with every ``/`` replaced
    by ``-`` (yielding a leading ``-`` for absolute paths).
    """
    encoded = str(cwd).replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def get_or_create_session_id(
    application_dir: Path,
    cwd: Path,
    *,
    _load_manifest: Callable[[Path, Path], dict | None] | None = None,
    _phase_completed: Callable[[dict | None, str], bool] | None = None,
) -> str:
    """Return a stable session UUID for *application_dir*, creating it if absent.

    The UUID is persisted at ``<application_dir>/.apply-state/session-id`` so
    that repeated invocations (phase 2/3 ``--resume``) always receive the same
    session identifier that phase 1 (gather) originally registered with Claude.

    A fresh :func:`uuid.uuid4` is generated on first call; subsequent calls
    read and return the stored value.  Using uuid4 (random) rather than uuid5
    (deterministic) means a failed-then-retried gather gets a new session ID
    that the Claude Code SDK will accept without the "already in use" error.

    **Stale-orphan detection:** if the persisted ID refers to a gather run that
    never completed (gather is not marked complete in the manifest), AND the
    corresponding SDK ``.jsonl`` session file still exists on disk (the orphan),
    the stored ID is treated as stale.  A new uuid4 is generated and persisted
    so the next gather invocation succeeds without a "Session ID … already in
    use" error.  The regeneration is logged to stderr.

    Parameters
    ----------
    application_dir:
        Directory for the specific job application (contains ``.apply-state/``).
    cwd:
        Working directory used to locate the pipeline DB and encode SDK paths.
    _load_manifest:
        Optional callable ``(app_dir, cwd) -> dict | None`` that reads the
        manifest.  Defaults to a lazy import from ``jobsmith.apply`` when
        ``None``.  Pass an explicit stub in tests to avoid DB access.
    _phase_completed:
        Optional callable ``(manifest, phase_name) -> bool``.  Same default
        lazy-import strategy as *_load_manifest*.
    """
    if _load_manifest is None:
        from jobsmith.core.manifest import load_manifest as _lm  # noqa: PLC0415
        _load_manifest = _lm
    if _phase_completed is None:
        from jobsmith.core.manifest import phase_completed as _pc  # noqa: PLC0415
        _phase_completed = _pc

    state_dir = application_dir / ".apply-state"
    session_file = state_dir / "session-id"
    if session_file.exists():
        stored = session_file.read_text(encoding="utf-8").strip()
        if stored:
            manifest = _load_manifest(application_dir, cwd)
            if _phase_completed(manifest, "gather"):
                # Gather succeeded — the session ID is legitimately reusable
                # for phase-2/3 --resume; return it unchanged.
                return stored
            # Gather was not completed. Check whether an orphan SDK session
            # file exists for this ID.
            orphan_path = claude_session_file_path(stored, cwd)
            if orphan_path.exists():
                # Stale orphan: regenerate so Claude Code SDK won't reject it.
                print(
                    f"jobsmith: regenerated session ID; previous orphan at {orphan_path}",
                    file=sys.stderr,
                )
                state_dir.mkdir(parents=True, exist_ok=True)
                new_id = str(uuid.uuid4())
                session_file.write_text(new_id, encoding="utf-8")
                return new_id
            # No orphan file — the persisted ID is safe to reuse.
            return stored
    state_dir.mkdir(parents=True, exist_ok=True)
    new_id = str(uuid.uuid4())
    session_file.write_text(new_id, encoding="utf-8")
    return new_id
