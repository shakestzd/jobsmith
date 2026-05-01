"""headless.py — subprocess wrapper for ``claude -p`` with JSONL stream parsing.

This module exposes a thin, well-tested layer around the Claude Code CLI's
``--output-format stream-json`` mode so that higher-level jobsmith features
(e.g. ``jobsmith apply``) can drive multi-phase agentic sessions without
taking a direct dependency on the claude SDK.

Public API
----------
- :class:`Event` — typed representation of a single JSONL line
- :func:`run_phase` — spawn ``claude -p`` and yield :class:`Event` objects
- :func:`deterministic_session_id` — stable UUID5 from a slug
- :func:`session_exists` — check whether a previous session JSONL file exists

Encoding note (session_exists)
-------------------------------
Claude Code stores session JSONL files at::

    ~/.claude/projects/<encoded_cwd>/<session_id>.jsonl

where ``<encoded_cwd>`` is the working-directory path with every ``/``
replaced by a ``-`` and a leading ``-`` inserted (the first ``/`` becomes the
prefix dash).  This matches what the Claude CLI itself writes, as observed in
practice.  Example::

    /Users/alice/projects/jobsmith  →  -Users-alice-projects-jobsmith

This encoding is a best-effort first cut; it works for all POSIX absolute
paths but may diverge from the official implementation for edge cases (e.g.
paths containing ``-``).  Tests override ``HOME`` via ``monkeypatch`` and
create the directory/file directly, so they do not rely on the live CLI.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Namespace constant
# ---------------------------------------------------------------------------

# Fixed namespace for all jobsmith session IDs.  Derived once via:
#   uuid.uuid5(uuid.NAMESPACE_DNS, "jobsmith.headless")
# and frozen here so the value never changes across releases.
JOBSMITH_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "jobsmith.headless")

# Regex that signals the end of a phase inside a text block.
_PHASE_COMPLETE_RE = re.compile(r"<<PHASE_COMPLETE:\s*(\w+)\s*>>>")
_PHASE_FAILED_RE = re.compile(r"<<PHASE_FAILED:\s*(\w+)\s*(?::\s*([^>]+?)\s*)?>>>")

# Marker text fragment scanned for tool results.
_TOOL_RESULT_TYPE = "tool_result"


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """A single event emitted from a ``claude -p`` streaming session.

    Attributes
    ----------
    type:
        One of ``"text"``, ``"tool_use"``, ``"tool_result"``,
        ``"phase_complete"``, ``"error"``, ``"system"``, or ``"result"``.
    raw:
        The original decoded JSONL payload (or a minimal synthetic dict for
        synthetic events such as ``phase_complete``).
    text:
        Populated for ``type == "text"`` events.
    tool_name:
        Populated for ``type in ("tool_use", "tool_result")`` events.
    tool_input:
        Populated for ``type == "tool_use"`` events.
    tool_result:
        Populated for ``type == "tool_result"`` events; contains the textual
        content of the tool result.
    name:
        Populated for ``type == "phase_complete"`` events; the phase name
        extracted from the ``<<PHASE_COMPLETE: <name>>>`` marker.
    error:
        Populated for ``type == "error"`` events.
    """

    type: str
    raw: dict = field(default_factory=dict)
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_result: str | None = None
    name: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def deterministic_session_id(slug: str) -> str:
    """Return a stable UUID5 string derived from *slug*.

    Uses :data:`JOBSMITH_NAMESPACE` so that the same slug always produces the
    same UUID regardless of platform or runtime state.

    Parameters
    ----------
    slug:
        Any string that uniquely identifies the session (e.g. a company slug
        or a job-posting hash).

    Returns
    -------
    str
        A lowercase UUID string such as ``"3f2504e0-4f89-11d3-9a0c-0305e82c3301"``.
    """
    return str(uuid.uuid5(JOBSMITH_NAMESPACE, slug))


def _encode_cwd(cwd: Path) -> str:
    """Encode a POSIX path the way Claude Code names its project directories.

    Replaces every ``/`` separator with ``-``.  For an absolute path this
    means the leading ``/`` becomes a leading ``-``, giving::

        /Users/alice/foo  →  -Users-alice-foo

    This is intentionally simple — see module docstring for caveats.
    """
    return str(cwd).replace("/", "-")


def session_exists(session_id: str, cwd: Path | None = None) -> bool:
    """Return True if a previous ``claude -p`` session file exists on disk.

    Claude Code stores sessions under::

        ~/.claude/projects/<encoded_cwd>/<session_id>.jsonl

    Parameters
    ----------
    session_id:
        The session UUID string (e.g. from :func:`deterministic_session_id`).
    cwd:
        The working directory that was active when the session was created.
        Defaults to :func:`pathlib.Path.cwd`.

    Returns
    -------
    bool
        ``True`` if the JSONL file exists, ``False`` otherwise.
    """
    resolved_cwd = cwd or Path.cwd()
    encoded = _encode_cwd(resolved_cwd)
    session_file = Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
    return session_file.exists()


# ---------------------------------------------------------------------------
# Core streaming runner
# ---------------------------------------------------------------------------


def _build_command(
    phase: str,  # noqa: ARG001 — reserved for future phase-specific flags
    session_id: str,
    prompt: str,
    plugin_dir: Path,
    system_prompt: Path,
    resume: bool,
    max_turns: int,
) -> list[str]:
    # No --bare: allow claude to read macOS keychain / OAuth so that
    # Claude Max / Pro subscribers work without an API key.
    # ANTHROPIC_API_KEY is still honoured by claude when set.
    cmd = [
        "claude",
        "-p",
        "--plugin-dir",
        str(plugin_dir),
        "--system-prompt-file",
        str(system_prompt),
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        "Agent Bash WebFetch Read Edit Write",
        "--max-turns",
        str(max_turns),
    ]
    # --session-id and --resume are mutually exclusive with claude.
    # On fresh runs (resume=False): claim the session ID.
    # On resuming (resume=True): continue the existing session.
    if resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]
    cmd.append(prompt)
    return cmd


def _parse_line(line: str) -> Event:
    """Parse a single JSONL line into one or more :class:`Event` objects.

    Returns a *list* so that an ``assistant`` message with multiple content
    blocks can expand to multiple events.  Callers flatten the result.
    """
    payload = json.loads(line)
    msg_type = payload.get("type", "")

    if msg_type == "assistant":
        return _parse_assistant(payload)
    if msg_type == "user":
        return _parse_user(payload)
    # system, result, or anything else — pass through verbatim
    return [Event(type=msg_type or "system", raw=payload)]


def _parse_assistant(payload: dict) -> list[Event]:
    events: list[Event] = []
    message = payload.get("message", payload)
    content = message.get("content", [])
    if isinstance(content, str):
        # Flat text shorthand
        events.append(Event(type="text", text=content, raw=payload))
        return events
    for block in content:
        block_type = block.get("type", "")
        if block_type == "text":
            events.append(Event(type="text", text=block.get("text", ""), raw=payload))
        elif block_type == "tool_use":
            events.append(
                Event(
                    type="tool_use",
                    tool_name=block.get("name"),
                    tool_input=block.get("input"),
                    raw=payload,
                )
            )
    if not events:
        events.append(Event(type="assistant", raw=payload))
    return events


def _parse_user(payload: dict) -> list[Event]:
    events: list[Event] = []
    message = payload.get("message", payload)
    content = message.get("content", [])
    if isinstance(content, str):
        return [Event(type="user", raw=payload)]
    for block in content:
        block_type = block.get("type", "")
        if block_type == _TOOL_RESULT_TYPE:
            # Extract textual content from the tool result block.
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                parts = [
                    c.get("text", "") for c in result_content if c.get("type") == "text"
                ]
                result_content = "\n".join(parts)
            events.append(
                Event(
                    type="tool_result",
                    tool_name=block.get("tool_use_id"),  # closest available identifier
                    tool_result=str(result_content),
                    raw=payload,
                )
            )
    if not events:
        events.append(Event(type="user", raw=payload))
    return events


def run_phase(
    phase: str,
    session_id: str,
    prompt: str,
    plugin_dir: Path,
    system_prompt: Path,
    resume: bool = False,
    *,
    cwd: Path | None = None,
    max_turns: int = 30,
) -> Iterator[Event]:
    """Spawn ``claude -p`` and yield :class:`Event` objects as JSONL lines arrive.

    Parameters
    ----------
    phase:
        Logical phase name (e.g. ``"gather"``, ``"draft"``, ``"render"``).
        Reserved for future phase-specific CLI overrides; not currently sent
        to the subprocess.
    session_id:
        Claude session identifier (see :func:`deterministic_session_id`).
    prompt:
        The user prompt forwarded verbatim to ``claude -p``.
    plugin_dir:
        Path to the embedded jobsmith plugin directory
        (``jobsmith.plugin_dir()``).
    system_prompt:
        Path to the system-prompt file for this phase.
    resume:
        When ``True``, append ``--resume <session_id>`` so Claude continues
        an existing session.
    cwd:
        Working directory for the subprocess.  Defaults to the current
        process's working directory.
    max_turns:
        Maximum agentic turns forwarded to ``--max-turns``.

    Yields
    ------
    Event
        One event per content block (or one synthetic event for
        ``phase_complete`` / ``error``).
    """
    cmd = _build_command(phase, session_id, prompt, plugin_dir, system_prompt, resume, max_turns)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(cwd) if cwd else None,
    )

    emitted: int = 0

    # Read stderr in a non-blocking way is tricky; we collect it after the
    # process exits.  For long-running sessions this is fine because we are
    # iterating stdout line-by-line.
    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                events = _parse_line(line)
            except (json.JSONDecodeError, ValueError) as exc:
                yield Event(type="error", error=str(exc), raw={"line": line})
                continue

            for event in events:
                emitted += 1
                yield event

                # Check for phase-boundary markers in text events. Both markers
                # signal that the phase is over; the caller distinguishes
                # success vs failure on the synthetic event type.
                if event.type == "text" and event.text:
                    failed = _PHASE_FAILED_RE.search(event.text)
                    if failed:
                        yield Event(
                            type="phase_failed",
                            name=failed.group(1),
                            raw={},
                            error=(failed.group(2) or None),
                        )
                        return
                    completed = _PHASE_COMPLETE_RE.search(event.text)
                    if completed:
                        yield Event(type="phase_complete", name=completed.group(1), raw={})
                        return
    finally:
        # Reap the subprocess BEFORE reading stderr — otherwise proc.stderr.read()
        # can block indefinitely if the caller broke out of the loop and claude
        # has not yet exited (the subprocess keeps stderr open until it dies).
        with contextlib.suppress(Exception):
            proc.stdout.close()  # type: ignore[union-attr]
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        stderr_output = ""
        with contextlib.suppress(Exception):
            stderr_output = proc.stderr.read()  # type: ignore[union-attr]

    rc = proc.returncode
    if rc != 0 and emitted == 0:
        tail = (stderr_output or "").strip()
        yield Event(type="error", error=tail or f"claude exited with code {rc}", raw={"returncode": rc})
