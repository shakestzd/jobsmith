"""jobsmith._init — bootstrap/init helpers for `jobsmith init` and auto-bootstrap.

Extracted from ``jobsmith.apply`` as part of trk-ad6d8227 (slice 6).
``jobsmith.apply`` re-exports ``_run_init`` for back-compat so that
``patch("jobsmith.apply._run_init")`` continues to work in tests.

Public API
----------
- :func:`scaffold_repo` — callable library function for scaffolding a repo
  (used by both ``jobsmith init`` and ``jobsmith onboard``).
- :func:`_run_init` — thin wrapper around ``scaffold_repo`` for back-compat.
"""
from __future__ import annotations

from pathlib import Path

import click

from .config import CONFIG_FILENAME


def scaffold_repo(target: Path, *, force: bool = False) -> None:
    """Scaffold a jobsmith repo at *target*.

    This is the canonical library function for repo bootstrapping. It is
    called by both ``jobsmith init`` (via the CLI command) and
    ``jobsmith onboard`` (phase 0 bootstrap). Extracted so slices that need
    to conditionally scaffold (e.g. onboard) can call it without importing
    the Typer-decorated ``init`` command, which raises ``SystemExit``.

    Parameters
    ----------
    target:
        Directory to scaffold. Created if it does not exist.
    force:
        When ``True``, overwrite existing files (e.g. ``.apply-config.yaml``).
        Default ``False`` preserves existing files (idempotent/safe re-run).
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
            if not dst.exists() or force:
                shutil.copy(src, dst)
    else:
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml", "publication.yml"):
            stub = content_dir / name
            if not stub.exists() or force:
                stub.write_text("# Populate me with your master content\n")

    # Config file
    config_path = target / CONFIG_FILENAME
    if not config_path.exists() or force:
        config_path.write_text(CONFIG_TEMPLATE)

    # Profile YAML
    profile_path = target / "private" / "capacity" / "profile.yaml"
    if not profile_path.exists() or force:
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


def _run_init(target: Path) -> None:
    """Run the jobsmith scaffold logic programmatically (mirrors cli.init).

    Writes `.apply-config.yaml` and creates the standard directory structure.
    Avoids importing the Typer-decorated command directly (which raises
    SystemExit) — instead calls the underlying helpers.

    .. deprecated::
        Use :func:`scaffold_repo` directly. This wrapper is kept for
        back-compat so that ``patch("jobsmith.apply._run_init")`` works in tests.
    """
    scaffold_repo(target, force=False)
    click.echo(
        f"Bootstrapped jobsmith repo at {target}. "
        "Edit assets/content/*.yml and .apply-config.yaml before running apply.",
        err=True,
    )
