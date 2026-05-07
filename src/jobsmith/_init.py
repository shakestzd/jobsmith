"""jobsmith._init — bootstrap/init helpers for `jobsmith init` and auto-bootstrap.

Extracted from ``jobsmith.apply`` as part of trk-ad6d8227 (slice 6).
``jobsmith.apply`` re-exports ``_run_init`` for back-compat so that
``patch("jobsmith.apply._run_init")`` continues to work in tests.
"""
from __future__ import annotations

from pathlib import Path

import click

from .config import CONFIG_FILENAME


def _run_init(target: Path) -> None:
    """Run the jobsmith scaffold logic programmatically (mirrors cli.init).

    Writes `.apply-config.yaml` and creates the standard directory structure.
    Avoids importing the Typer-decorated command directly (which raises
    SystemExit) — instead calls the underlying helpers.
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
            if not dst.exists():
                shutil.copy(src, dst)
    else:
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml", "publication.yml"):
            stub = content_dir / name
            if not stub.exists():
                stub.write_text("# Populate me with your master content\n")

    # Config file
    config_path = target / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(CONFIG_TEMPLATE)

    # Profile YAML
    profile_path = target / "private" / "capacity" / "profile.yaml"
    if not profile_path.exists():
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

    click.echo(
        f"Bootstrapped jobsmith repo at {target}. "
        "Edit assets/content/*.yml and .apply-config.yaml before running apply.",
        err=True,
    )
