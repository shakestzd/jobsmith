"""Path resolution for jobsmith.

Resolves master YAML paths, applications directory, and tracking DB
based on the loaded JobsmithConfig.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import CONFIG_FILENAME, JobsmithConfig, find_config

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class RepoRootNotFoundError(RuntimeError):
    """Raised by ``repo_root_for(require=True)`` when no repo root resolves.

    Provides an actionable message listing the resolution tiers that were
    tried so the user knows how to fix the problem.
    """


# ---------------------------------------------------------------------------
# repo_root_for — multi-tier resolver (feat-f85f4815)
# ---------------------------------------------------------------------------


def repo_root_for(
    start: Path | None = None,
    *,
    cwd: Path | None = None,
    repo_root: Path | None = None,
    require: bool = False,
) -> Path:
    """Resolve the jobsmith repo root using a 4-tier precedence chain.

    Precedence (highest → lowest):
      1. *repo_root* — explicit path passed by the caller (e.g. ``--repo-root``
         CLI flag).  Returned as-is without any existence check; the caller is
         responsible for validating the path.
      2. ``JOBSMITH_REPO_ROOT`` environment variable — absolute path to a
         directory that contains ``.apply-config.yaml``.
      3. ``settings.toml`` ``repo_root`` — persisted user preference written
         by ``jobsmith config set-repo-root``.
      4. ``find_config(cwd)`` walk-up — walks parent directories looking for
         ``.apply-config.yaml`` and returns its containing directory.
      5. Fallback / error — when *require* is ``False`` (default), returns
         *cwd* (or ``Path.cwd()`` if *cwd* is also ``None``) for backwards
         compatibility.  When *require* is ``True``, raises
         :exc:`RepoRootNotFoundError` with an actionable message.

    Parameters
    ----------
    start:
        Deprecated alias for *cwd* kept for backwards compatibility.
        When both are provided, *cwd* takes precedence.
    cwd:
        Directory from which to start the walk-up search (tier 4).
        Defaults to ``Path.cwd()``.
    repo_root:
        Explicit repo root path (tier 1).  When provided all other tiers
        are skipped.
    require:
        When ``True``, raise :exc:`RepoRootNotFoundError` if no tier
        resolves to a directory containing ``.apply-config.yaml``.
        Default ``False`` preserves legacy behaviour (falls back to *cwd*).
    """
    # Tier 1 — explicit param (e.g. --repo-root CLI flag)
    if repo_root is not None:
        return repo_root

    effective_cwd = cwd or start or Path.cwd()

    # Tier 2 — JOBSMITH_REPO_ROOT env var
    env_root = os.environ.get("JOBSMITH_REPO_ROOT")
    if env_root:
        candidate = Path(env_root)
        if candidate.is_dir():
            return candidate
        # Env var set but points to non-existent dir — skip silently,
        # fall through to lower tiers so the caller isn't completely broken.

    # Tier 3 — settings.toml repo_root
    try:
        from .settings import read_repo_root as _read_repo_root

        settings_root = _read_repo_root()
        if settings_root is not None and settings_root.is_dir():
            return settings_root
    except Exception:
        # settings.py unavailable or corrupt — treat as absent
        pass

    # Tier 4 — find_config walk-up
    config_path = find_config(effective_cwd)
    if config_path is not None:
        return config_path.parent

    # Tier 5 — fallback or error
    if require:
        raise RepoRootNotFoundError(
            f"Could not locate {CONFIG_FILENAME!r}. "
            "Tried the following resolution tiers in order:\n"
            "  1. --repo-root CLI flag (not provided)\n"
            f"  2. JOBSMITH_REPO_ROOT env var ({env_root or 'unset'})\n"
            "  3. settings.toml repo_root (not set or dir absent)\n"
            f"  4. walk-up from {effective_cwd} (no {CONFIG_FILENAME} found)\n"
            "\nTo fix: run `jobsmith init` in your repo, or set the repo root with:\n"
            "  jobsmith config set-repo-root /path/to/your/repo"
        )

    # Legacy fallback — return cwd so callers that don't pass require=True
    # keep working as they did before this feature was added.
    return effective_cwd


# ---------------------------------------------------------------------------
# Remaining helpers (unchanged from original)
# ---------------------------------------------------------------------------


def resolve(path: Path, repo_root: Path | None = None) -> Path:
    """Resolve a config-relative path against the repo root.

    Absolute paths are returned as-is. Relative paths are joined to
    repo_root (the directory containing `.apply-config.yaml`).
    """
    if path.is_absolute():
        return path
    root = repo_root or repo_root_for()
    return (root / path).resolve()


def all_master_paths(config: JobsmithConfig, repo_root: Path | None = None) -> list[Path]:
    """All defined master YAML paths, resolved against the repo root."""
    root = repo_root or repo_root_for()
    paths: list[Path] = [
        resolve(config.master.work_yml, root),
        resolve(config.master.skill_yml, root),
        resolve(config.master.education_yml, root),
        resolve(config.master.author_yml, root),
    ]
    if config.master.publication_yml is not None:
        paths.append(resolve(config.master.publication_yml, root))
    if config.master.award_yml is not None:
        paths.append(resolve(config.master.award_yml, root))
    return paths


__all__ = ["RepoRootNotFoundError", "all_master_paths", "repo_root_for", "resolve"]
