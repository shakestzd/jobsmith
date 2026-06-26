"""Desktop dependency detection (feat-dac00175, slice 6).

Small, pure, side-effect-free probes for the external tools the desktop app
shells out to. Slice 6 covers the ``claude`` Claude Code CLI; slice 7 will
EXTEND this same module with LLM-runtime detection (e.g. Ollama / local model
servers) — keep new probes as standalone ``*_status()`` functions that return a
plain ``dict`` so the desktop API can compose them without import-time cost.

Nothing here imports the heavy apply pipeline or raises on a missing binary:
detection must never break import of the API. PATH lookups use
:func:`shutil.which`; version probes shell out with a short timeout and tolerate
every failure mode (missing binary, non-zero exit, timeout, unparseable output).
"""

from __future__ import annotations

import re
import shutil
import subprocess

# The Claude Code CLI binary jobsmith's apply pipeline shells out to
# (see :mod:`jobsmith.headless`).
_CLAUDE_BINARY = "claude"

# `claude --version` prints something like "1.2.3 (Claude Code)". Pull the first
# dotted-numeric token; fall back to the raw line when the shape changes.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+.\w]*)?")

# Bound the version probe so a wedged binary can never hang a status request.
_VERSION_TIMEOUT_S = 5.0


def _probe_version(binary: str) -> str | None:
    """Return the version string reported by ``<binary> --version``, or None.

    Tolerates a missing binary, a non-zero exit, a timeout, and output that
    does not match the expected shape — any of which yields ``None`` rather
    than raising. When the output is present but unparseable, the stripped raw
    line is returned so callers still get a human-readable hint.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    match = _VERSION_RE.search(out)
    return match.group(0) if match else out


def claude_status() -> dict:
    """Report whether the ``claude`` Claude Code CLI is available.

    Returns
    -------
    dict
        ``{"installed": bool, "version": str | None, "path": str | None}``.
        ``installed`` is ``True`` only when the binary resolves on PATH;
        ``version`` is best-effort (``None`` if the probe fails) and ``path``
        is the resolved absolute path (``None`` when not installed).
    """
    path = shutil.which(_CLAUDE_BINARY)
    if path is None:
        return {"installed": False, "version": None, "path": None}
    return {"installed": True, "version": _probe_version(path), "path": path}


__all__ = ["claude_status"]
