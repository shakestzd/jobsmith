"""Internal helper: resolve user's repo root for sourcing config files.

Used by jobsmith.sourcing.scoring to locate private/capacity/ YAML files
(scoring-weights.yaml, shakes-profile.yaml, red-flag-patterns.yaml) without
depending on the full jobsmith.paths module (which requires .apply-config.yaml).

Design decision A4: scoring YAML files live in the user's private repo next
to .apply-config.yaml. This module resolves the root using the same 4-tier
chain as repo_root_for() but never raises — degrades gracefully to None so
the scorer can return zero scores when the user hasn't set up their repo yet.
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_repo_root_best_effort() -> Path | None:
    """Try to find the user's repo root. Returns None when unresolvable.

    Resolution order (same as paths.repo_root_for):
      1. JOBSMITH_REPO_ROOT env var
      2. settings.toml repo_root
      3. walk-up from cwd for .apply-config.yaml
    """
    # Tier 1 — env var
    env_root = os.environ.get("JOBSMITH_REPO_ROOT")
    if env_root:
        candidate = Path(env_root)
        if candidate.is_dir():
            return candidate

    # Tier 2 — settings.toml
    try:
        from .settings import read_repo_root as _read_repo_root

        settings_root = _read_repo_root()
        if settings_root is not None and settings_root.is_dir():
            return settings_root
    except Exception:
        pass

    # Tier 3 — walk-up from cwd
    try:
        from .config import find_config

        config_path = find_config(Path.cwd())
        if config_path is not None:
            return config_path.parent
    except Exception:
        pass

    return None
