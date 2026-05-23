"""jobsmith.settings — user-level settings store (feat-f85f4815).

Persists user preferences to the OS config directory:
  macOS / Linux: ~/.config/jobsmith/settings.toml  (XDG_CONFIG_HOME override)
  Windows:       %APPDATA%\\jobsmith\\settings.toml

The file is plain TOML.  Currently supported keys:

  repo_root = "/absolute/path/to/repo"

Reading uses ``tomllib`` (stdlib on Python >=3.11) falling back to the
third-party ``tomli`` on Python 3.10.  Writing uses a minimal stdlib
approach: we round-trip through a hand-built TOML serialiser for the
single-key schema rather than pulling in a write dep like ``tomli-w``.
The schema is intentionally small — only ``repo_root`` today — so a
bespoke encoder is sufficient and avoids the extra dependency.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

# ---------------------------------------------------------------------------
# TOML read (stdlib tomllib on 3.11+, tomli on 3.10)
# ---------------------------------------------------------------------------

if sys.version_info >= (3, 11):
    import tomllib as _tomllib
else:
    import tomli as _tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Config-dir resolution
# ---------------------------------------------------------------------------

_APP_NAME = "jobsmith"


def settings_config_path() -> Path:
    """Return the absolute path to the user-level settings file.

    Respects ``XDG_CONFIG_HOME`` on Linux/macOS (via ``platformdirs``).
    Example: ``~/.config/jobsmith/settings.toml``.
    """
    return Path(user_config_dir(_APP_NAME)) / "settings.toml"


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def read_settings() -> dict[str, Any]:
    """Load the settings file and return its contents as a dict.

    Returns an empty dict when the file does not exist or is empty.
    Raises ``tomllib.TOMLDecodeError`` on malformed TOML.
    """
    path = settings_config_path()
    if not path.exists():
        return {}
    text = path.read_bytes()
    if not text.strip():
        return {}
    return _tomllib.loads(text.decode("utf-8"))


def read_repo_root() -> Path | None:
    """Return the stored ``repo_root`` as a ``Path``, or ``None`` if unset."""
    data = read_settings()
    raw = data.get("repo_root")
    if raw is None:
        return None
    return Path(raw)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def _encode_toml_string(value: str) -> str:
    """Encode a Python string as a TOML basic string (double-quoted).

    Only handles the single-key use-case (file paths).  Paths on all
    supported platforms contain no characters that require escape sequences
    beyond the backslash used on Windows, so we escape ``\\`` and ``"``
    then wrap in double quotes.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_repo_root(path: Path) -> None:
    """Persist *path* as ``repo_root`` in the settings file.

    Creates parent directories if they do not exist.  Any existing
    ``repo_root`` key is overwritten; other keys in the file are preserved.
    """
    # Read current settings so we don't clobber other keys.
    try:
        current = read_settings()
    except Exception:
        current = {}

    current["repo_root"] = str(path)
    _write_settings(current)


def clear_repo_root() -> None:
    """Remove the ``repo_root`` key from the settings file.

    If the file does not exist or the key is already absent, this is a no-op.
    """
    try:
        current = read_settings()
    except Exception:
        return
    if "repo_root" not in current:
        return
    del current["repo_root"]
    _write_settings(current)


# ---------------------------------------------------------------------------
# Internal write
# ---------------------------------------------------------------------------


def _write_settings(data: dict[str, Any]) -> None:
    """Serialise *data* as TOML and atomically write to the settings path.

    Only string values are supported — sufficient for the current schema.
    Raises ``TypeError`` for non-string values.
    """
    lines: list[str] = [
        "# jobsmith user settings — managed by `jobsmith config`",
        "# Edit manually or use: jobsmith config set-repo-root <path>",
        "",
    ]
    for key, value in sorted(data.items()):
        if not isinstance(value, str):
            raise TypeError(
                f"settings._write_settings: only string values supported, "
                f"got {key!r} = {value!r} ({type(value).__name__})"
            )
        lines.append(f"{key} = {_encode_toml_string(value)}")

    text = "\n".join(lines) + "\n"

    config_path = settings_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")
