"""jobsmith — tailored resume and cover-letter pipeline.

Master-first, no fabrication, anchor-preserving. Ships as a Claude Code
plugin and a standalone Python CLI sharing one core package.

See https://jobsmith.dev and the README for usage.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

__version__ = "0.1.0"


def plugin_dir() -> Path:
    """Return the absolute path of the embedded Claude Code plugin directory.

    Works in both editable (``uv pip install -e .``) and wheel installs:
    the plugin subtree lives at ``src/jobsmith/plugin/`` in the repo and is
    packaged under ``jobsmith/plugin/`` in the wheel.

    Example::

        >>> import jobsmith
        >>> p = jobsmith.plugin_dir()
        >>> (p / "plugin.json").exists()
        True
    """
    return Path(str(files("jobsmith") / "plugin"))


__all__ = ["__version__", "plugin_dir"]
