"""Path resolution for jobsmith.

Resolves master YAML paths, applications directory, and tracking DB
based on the loaded JobsmithConfig.
"""

from __future__ import annotations

from pathlib import Path

from .config import JobsmithConfig, find_config


def repo_root_for(start: Path | None = None) -> Path:
    """The directory containing `.apply-config.yaml`, or cwd if none found."""
    config_path = find_config(start or Path.cwd())
    if config_path is None:
        return Path.cwd()
    return config_path.parent


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


__all__ = ["all_master_paths", "repo_root_for", "resolve"]
