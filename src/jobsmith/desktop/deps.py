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

import importlib.util
import re
import shutil
import subprocess

import httpx

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


# ---------------------------------------------------------------------------
# Local LLM backend detection (feat-aaa91b6d, slice 7)
# ---------------------------------------------------------------------------
#
# Detect whether a local OpenAI-compatible LLM server is reachable and whether
# the corresponding runtime is installed, for two backends the desktop app can
# eventually route chat + scoring to:
#
#   - MLX:    `mlx_lm.server` (Apple-silicon), default 127.0.0.1:8080
#   - Ollama: `ollama serve`, default 127.0.0.1:11434
#
# Both expose an OpenAI-compatible `GET /v1/models` (verified against
# github.com/ml-explore/mlx-lm SERVER.md and docs.ollama.com/api/openai-compat).
# REDUCED SCOPE: this is detection + status ONLY. The pluggable-backend `llm`
# config (provider=openai_compatible) that wires chat/scoring to these servers
# is deferred to plan-938f735b — nothing here reads or writes any config.

# Default OpenAI-compatible base URLs (loopback only; we never probe remote
# hosts from a status check).
_MLX_BASE_URL = "http://127.0.0.1:8080"
_OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# `mlx_lm.server` ships as a console-script AND an importable module; either
# one means the runtime is present. `ollama` is a single binary on PATH.
_MLX_RUNTIME_BINARY = "mlx_lm.server"
_MLX_MODULE = "mlx_lm"
_OLLAMA_BINARY = "ollama"

# Keep probes fast + bounded: a closed port refuses immediately, and an open
# but wedged port must not stall a status request. Half a second is plenty for
# a loopback round-trip.
_LLM_PROBE_TIMEOUT_S = 0.5


def _module_installed(name: str) -> bool:
    """Return True when ``name`` is importable, without importing it.

    Uses :func:`importlib.util.find_spec` so we never trigger MLX's heavy
    (and Apple-silicon-only) import side effects just to detect presence.
    Tolerates the odd failure modes find_spec can raise (a broken parent
    package surfaces as ``ImportError``/``ValueError``).
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _first_model_id(payload: object) -> str | None:
    """Pull the first model id from an OpenAI ``/v1/models`` body, or None.

    The contract is ``{"data": [{"id": "..."}, ...]}``; anything that does not
    match that shape yields ``None`` rather than raising.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                return model_id
    return None


def _probe_openai_models(
    base_url: str, timeout: float = _LLM_PROBE_TIMEOUT_S
) -> tuple[bool, str | None]:
    """Probe ``<base_url>/v1/models`` and report (reachable, first_model_id).

    Reachable means a 200 response. A closed port, timeout, DNS failure, or
    non-200 status all yield ``(False, None)`` — never an exception. A 200 with
    an unparseable body is still ``reachable`` (``(True, None)``) so a quirky
    server does not read as "offline".
    """
    try:
        resp = httpx.get(f"{base_url}/v1/models", timeout=timeout)
    except (httpx.HTTPError, OSError):
        return False, None
    if resp.status_code != 200:
        return False, None
    try:
        payload = resp.json()
    except ValueError:
        return True, None
    return True, _first_model_id(payload)


def _backend_status(*, base_url: str, runtime_installed: bool) -> dict:
    """Compose one backend's status dict (probe runs at call time)."""
    reachable, model = _probe_openai_models(base_url)
    return {
        "reachable": reachable,
        "base_url": base_url,
        "runtime_installed": runtime_installed,
        "model": model,
    }


def llm_status() -> dict:
    """Detect local OpenAI-compatible LLM backends (MLX, Ollama).

    Returns
    -------
    dict
        Keyed by backend name, each value::

            {
              "reachable": bool,          # a server answers GET /v1/models 200
              "base_url": str,            # the loopback URL probed
              "runtime_installed": bool,  # the runtime is installed (may be off)
              "model": str | None,        # first advertised model id, if any
            }

        Probes are fast and timeout-bounded; a closed port returns quickly.
        Never raises — every failure mode degrades to ``reachable: False``.
    """
    mlx_installed = (
        shutil.which(_MLX_RUNTIME_BINARY) is not None
        or _module_installed(_MLX_MODULE)
    )
    ollama_installed = shutil.which(_OLLAMA_BINARY) is not None
    return {
        "mlx": _backend_status(
            base_url=_MLX_BASE_URL, runtime_installed=mlx_installed
        ),
        "ollama": _backend_status(
            base_url=_OLLAMA_BASE_URL, runtime_installed=ollama_installed
        ),
    }


# ---------------------------------------------------------------------------
# Local apply engine detection: vllm-mlx (feat-0d2f3df4, slice 4)
# ---------------------------------------------------------------------------
#
# The code-orchestrated LOCAL apply path serves gemma-4 via the `vllm-mlx`
# engine (see docs/spikes/byo-model-apply.md). Detection here is presence-only
# and MUST NOT raise: when the runtime is absent we surface a guided
# `uv pip install vllm-mlx` hint so the desktop app can degrade gracefully
# instead of crashing. The engine *lifecycle* lives in jobsmith.llm.vllm_mlx;
# this probe deliberately stays import-light (PATH + importable-module checks
# only) and self-contained, mirroring claude_status()/llm_status() above.

# Console script (hyphen) and importable module (underscore); either one means
# the runtime can be launched.
_VLLM_MLX_BINARY = "vllm-mlx"
_VLLM_MLX_MODULE = "vllm_mlx"

# Guided remediation (kept in sync with jobsmith.llm.vllm_mlx.INSTALL_HINT).
_VLLM_MLX_INSTALL_HINT = "uv pip install vllm-mlx"


def vllm_mlx_status() -> dict:
    """Report whether the ``vllm-mlx`` local apply engine is installed.

    Returns
    -------
    dict
        ``{"installed": bool, "path": str | None, "install_hint": str | None}``.
        ``installed`` is ``True`` when the console script resolves on PATH or the
        ``vllm_mlx`` module is importable; ``path`` is the resolved console-script
        path (``None`` when only the module is present). ``install_hint`` carries
        the guided ``uv pip install vllm-mlx`` command when the runtime is absent
        and is ``None`` otherwise. Never raises.
    """
    path = shutil.which(_VLLM_MLX_BINARY)
    installed = path is not None or _module_installed(_VLLM_MLX_MODULE)
    return {
        "installed": installed,
        "path": path,
        "install_hint": None if installed else _VLLM_MLX_INSTALL_HINT,
    }


__all__ = ["claude_status", "llm_status", "vllm_mlx_status"]
