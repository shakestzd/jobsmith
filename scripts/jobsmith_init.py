#!/usr/bin/env python3
"""jobsmith init — scaffold a fresh application repo with master YAML stubs and config.

Run from the directory where you want your application repo:

    python /path/to/jobsmith/scripts/jobsmith_init.py

Or, when jobsmith is installed as a Claude Code plugin:

    claude /jobsmith-init

This script:
- Creates `assets/content/` with stubs for work.yml, skill.yml, education.yml, author.yml, publication.yml
- Creates `private/applications/` (where /apply outputs land)
- Creates `private/capacity/profile.yaml` (used by apply-fit-scorer)
- Writes `.apply-config.yaml` with sensible defaults pointing at the above paths
- Adds .gitignore entries for `.apply-state/`, rendered PDFs, and the SQLite DB

It will refuse to overwrite existing files unless --force is passed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from textwrap import dedent

JOBSMITH_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = JOBSMITH_ROOT / "examples" / "master-yaml"


def write_file(path: Path, content: str, force: bool) -> bool:
    if path.exists() and not force:
        print(f"  SKIP {path} (already exists; use --force to overwrite)")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  WROTE {path}")
    return True


def copy_file(src: Path, dst: Path, force: bool) -> bool:
    if dst.exists() and not force:
        print(f"  SKIP {dst} (already exists; use --force to overwrite)")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    print(f"  COPIED {dst}")
    return True


CONFIG_TEMPLATE = dedent(
    """\
    # jobsmith configuration
    #
    # See `<jobsmith-plugin-root>/config-schema.yaml` for full reference.

    master:
      work_yml: assets/content/work.yml
      skill_yml: assets/content/skill.yml
      education_yml: assets/content/education.yml
      author_yml: assets/content/author.yml
      publication_yml: assets/content/publication.yml

    output:
      applications_dir: private/applications
      job_search_db: private/job_search.db

    user:
      name: ""
      email: ""
      phone: ""
      location: ""
      github: ""
      linkedin: ""

    voice:
      voice_guide_path: null
      employment_gap_snippet: null

    anchor_thresholds:
      money_min_usd: 10000000
      percent_min: 50.0
      asset_count_min: 100000

    cover_letter:
      framework: careerfair-io
      default_salutation: "Hello,"

    resume:
      max_pages: 1
      layout_iteration_limit: 2

    fit_scorer:
      fast_threshold: 0.70
      profile_yaml: private/capacity/profile.yaml
    """
)

GITIGNORE_ADDITIONS = dedent(
    """\

    # jobsmith
    .apply-state/
    private/applications/*/documents/*.pdf
    private/applications/*/documents/*.typ
    private/job_search.db
    """
)

PROFILE_TEMPLATE = dedent(
    """\
    # Profile YAML — used by apply-fit-scorer for evidence-weighted reasoning.
    #
    # Structure your background as discrete claims a scorer can reason over.
    # Pull from your master YAMLs but in a more granular form.

    user:
      name: ""

    stack:
      python_advanced: true
      sql_advanced: true
      # Add tools/frameworks you have production experience with — one per key.

    specialties:
      # Optional. Named "specialty" buckets the fit-scorer prioritizes.
      # Examples: tax_equity, ai_research, climate_data, geospatial_ml
      # Add buckets that describe your differentiator.

    domain:
      # Optional. Industries / sectors with real production experience.

    years:
      total_quantitative: 0
      dedicated_data_engineering: 0
      dedicated_ai_ml: 0
    """
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a jobsmith application repo")
    parser.add_argument("target", nargs="?", default=".", help="Target directory (default: cwd)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--no-examples", action="store_true", help="Don't copy example master YAML; write empty stubs")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(f"jobsmith init -> {target}")
    print()

    # Master YAML files
    print("Master YAML files:")
    if args.no_examples:
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml", "publication.yml"):
            write_file(target / "assets" / "content" / name, "# Populate me with your master content\n", args.force)
    else:
        if not EXAMPLES_DIR.exists():
            print(f"  ERROR: examples directory not found at {EXAMPLES_DIR}", file=sys.stderr)
            return 1
        for src in EXAMPLES_DIR.glob("*.yml"):
            copy_file(src, target / "assets" / "content" / src.name, args.force)

    print()
    print("Config and tracking dirs:")
    write_file(target / ".apply-config.yaml", CONFIG_TEMPLATE, args.force)
    write_file(target / "private" / "capacity" / "profile.yaml", PROFILE_TEMPLATE, args.force)
    (target / "private" / "applications").mkdir(parents=True, exist_ok=True)
    print(f"  ENSURED {target / 'private' / 'applications'}")

    # .gitignore additions
    print()
    print(".gitignore:")
    gitignore = target / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text()
        if "jobsmith" not in existing:
            gitignore.write_text(existing.rstrip() + "\n" + GITIGNORE_ADDITIONS)
            print(f"  APPENDED to {gitignore}")
        else:
            print(f"  ALREADY HAS jobsmith section ({gitignore})")
    else:
        gitignore.write_text(GITIGNORE_ADDITIONS.lstrip())
        print(f"  WROTE {gitignore}")

    print()
    print("Next steps:")
    print(f"  1. cd {target}")
    print("  2. Edit assets/content/*.yml with your real history")
    print("  3. Edit .apply-config.yaml — set user.name, user.email, etc.")
    print("  4. Edit private/capacity/profile.yaml with your stack/specialty/years")
    print("  5. From Claude Code: /apply <job-url>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
