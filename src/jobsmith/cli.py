"""jobsmith CLI — Typer entry point.

Exposes:
    jobsmith init          — scaffold an application repo
    jobsmith validate      — validate .apply-config.yaml
    jobsmith status        — show repo state (config, master YAMLs, db)
    jobsmith doctor        — diagnose common setup issues
    jobsmith fact-check    — verify draft claims against master YAMLs
    jobsmith anchor-check  — verify anchor bullets are preserved
    jobsmith --version
"""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .assemble import assemble_all, assemble_application
from .config import CONFIG_FILENAME, find_config, load_config
from .factcheck import check_draft
from .guard import check_anchors, render_diff_md
from .paths import all_master_paths, repo_root_for, resolve

app = typer.Typer(
    name="jobsmith",
    help="Tailored resume + cover-letter pipeline. Master-first, no fabrication, anchor-preserving.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Path to the package root for locating example master YAML
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES_DIR = PACKAGE_ROOT / "examples" / "master-yaml"


CONFIG_TEMPLATE = dedent(
    """\
    # jobsmith configuration
    #
    # See <jobsmith-plugin-root>/config-schema.yaml for the full reference.

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

PROFILE_TEMPLATE = dedent(
    """\
    # Profile YAML — used by apply-fit-scorer for evidence-weighted reasoning.
    #
    # Structure your background as discrete claims a scorer can reason over.

    user:
      name: ""

    stack:
      python_advanced: true
      sql_advanced: true

    specialties: {}

    domain: {}

    years:
      total_quantitative: 0
      dedicated_data_engineering: 0
      dedicated_ai_ml: 0
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


# ---------- commands ----------


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
):
    if version:
        console.print(f"jobsmith {__version__}")
        raise typer.Exit()


@app.command()
def init(
    target: Path = typer.Argument(Path("."), help="Target directory (default: cwd)"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
    no_examples: bool = typer.Option(
        False,
        "--no-examples",
        help="Don't copy example master YAML; write empty stubs instead",
    ),
):
    """Scaffold a fresh application repo with master YAML stubs and config."""
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]jobsmith init[/bold] -> {target}")

    # Master YAML files
    console.print("\n[bold]Master YAML files:[/bold]")
    if no_examples:
        for name in ("work.yml", "skill.yml", "education.yml", "author.yml", "publication.yml"):
            _write_file(
                target / "assets" / "content" / name,
                "# Populate me with your master content\n",
                force,
            )
    else:
        if not EXAMPLES_DIR.exists():
            console.print(f"[red]ERROR:[/red] examples directory not found at {EXAMPLES_DIR}")
            raise typer.Exit(code=1)
        for src in EXAMPLES_DIR.glob("*.yml"):
            _copy_file(src, target / "assets" / "content" / src.name, force)

    # Config + tracking dirs
    console.print("\n[bold]Config and tracking dirs:[/bold]")
    _write_file(target / CONFIG_FILENAME, CONFIG_TEMPLATE, force)
    _write_file(target / "private" / "capacity" / "profile.yaml", PROFILE_TEMPLATE, force)
    (target / "private" / "applications").mkdir(parents=True, exist_ok=True)
    console.print(f"  ENSURED {target / 'private' / 'applications'}")

    # .gitignore additions
    console.print("\n[bold].gitignore:[/bold]")
    gitignore = target / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text()
        if "jobsmith" not in existing:
            gitignore.write_text(existing.rstrip() + "\n" + GITIGNORE_ADDITIONS)
            console.print(f"  APPENDED to {gitignore}")
        else:
            console.print(f"  ALREADY HAS jobsmith section ({gitignore})")
    else:
        gitignore.write_text(GITIGNORE_ADDITIONS.lstrip())
        console.print(f"  WROTE {gitignore}")

    console.print("\n[bold green]Next steps:[/bold green]")
    console.print(f"  1. cd {target}")
    console.print("  2. Edit assets/content/*.yml with your real history")
    console.print("  3. Edit .apply-config.yaml — set user.name, user.email, etc.")
    console.print("  4. Edit private/capacity/profile.yaml with your stack/years")
    console.print("  5. From Claude Code: /apply <job-url>")


@app.command()
def validate(
    config_path: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit config path (default: walk up from cwd)",
    ),
):
    """Validate `.apply-config.yaml` and report any errors."""
    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[red]CONFIG INVALID:[/red] {e}")
        raise typer.Exit(code=1) from e

    found = config_path or find_config(Path.cwd())
    if found is None:
        console.print("[yellow]No .apply-config.yaml found[/yellow] — using defaults")
    else:
        console.print(f"[green]CONFIG VALID:[/green] {found}")
    console.print(f"  master.work_yml      = {config.master.work_yml}")
    console.print(f"  output.applications  = {config.output.applications_dir}")
    console.print(f"  user.name            = {config.user.name or '(unset)'}")
    console.print(f"  anchor.money_min_usd = ${config.anchor_thresholds.money_min_usd:,}")


@app.command()
def status():
    """Show jobsmith repo state — config, master YAMLs, applications dir, DB."""
    config = load_config()
    repo_root = repo_root_for()
    console.print(f"[bold]Repo:[/bold] {repo_root}")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Resource")
    table.add_column("Path")
    table.add_column("Status")

    # Config
    config_path = find_config(Path.cwd())
    table.add_row(
        ".apply-config.yaml",
        str(config_path or CONFIG_FILENAME),
        "[green]OK[/green]" if config_path else "[yellow]missing — using defaults[/yellow]",
    )

    # Master YAMLs
    for path in all_master_paths(config, repo_root):
        rel = path.relative_to(repo_root) if path.is_relative_to(repo_root) else path
        table.add_row(
            f"master/{path.name}",
            str(rel),
            "[green]OK[/green]" if path.exists() else "[red]missing[/red]",
        )

    # Applications dir
    apps_dir = resolve(config.output.applications_dir, repo_root)
    if apps_dir.exists():
        count = sum(1 for p in apps_dir.iterdir() if p.is_dir())
        table.add_row("applications/", str(apps_dir.relative_to(repo_root)), f"{count} apps")
    else:
        table.add_row(
            "applications/",
            str(config.output.applications_dir),
            "[yellow]missing[/yellow]",
        )

    # Job search DB
    db_path = resolve(config.output.job_search_db, repo_root)
    table.add_row(
        "job_search.db",
        str(config.output.job_search_db),
        "[green]OK[/green]" if db_path.exists() else "[yellow]missing[/yellow]",
    )

    console.print(table)


@app.command()
def doctor():
    """Diagnose common setup issues. Exit non-zero if any are blocking."""
    issues: list[tuple[str, str, bool]] = []  # (category, description, is_blocking)

    config = load_config()
    repo_root = repo_root_for()

    # 1. Config presence
    config_path = find_config(Path.cwd())
    if config_path is None:
        issues.append(
            ("config", "No .apply-config.yaml found — run `jobsmith init` first", True)
        )
        config_dir = Path.cwd()
    else:
        config_dir = config_path.parent

    # 2. Master YAML presence
    for path in all_master_paths(config, config_dir):
        if not path.exists():
            issues.append(("master", f"Missing: {path}", True))

    # 3. User identity
    if not config.user.name:
        issues.append(("identity", "user.name is unset — cover letters will be unsigned", False))
    if not config.user.email:
        issues.append(("identity", "user.email is unset", False))

    # 4. Quarto presence
    if shutil.which("quarto") is None:
        issues.append(("tooling", "quarto not found on PATH — required for resume rendering", True))

    # 5. uv presence
    if shutil.which("uv") is None:
        issues.append(("tooling", "uv not found on PATH — recommended for Python execution", False))

    # 6. pdftotext (used by ATS checker)
    if shutil.which("pdftotext") is None:
        issues.append(
            (
                "tooling",
                "pdftotext not found — install poppler (brew install poppler)",
                False,
            )
        )

    # Render results
    if not issues:
        console.print("[bold green]All checks passed.[/bold green]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Severity")
    table.add_column("Category")
    table.add_column("Issue")
    blocking = 0
    for category, description, is_blocking in issues:
        if is_blocking:
            blocking += 1
            table.add_row("[red]BLOCK[/red]", category, description)
        else:
            table.add_row("[yellow]WARN[/yellow]", category, description)
    console.print(table)
    if blocking:
        raise typer.Exit(code=1)


@app.command()
def assemble(
    slug: str | None = typer.Argument(
        None,
        help="Application slug to assemble. Omit and pass --all to assemble every application.",
    ),
    all_apps: bool = typer.Option(
        False,
        "--all",
        help="Assemble every application under output.applications_dir. Used as the Quarto pre-render hook.",
    ),
    applications_dir: Path | None = typer.Option(
        None,
        "--applications-dir",
        help="Override output.applications_dir from .apply-config.yaml.",
    ),
):
    """Read .apply-state/* and write _variables.yml for Quarto consumption.

    Two modes:
      jobsmith assemble <slug>   → assemble one application
      jobsmith assemble --all    → assemble every application (site-wide pre-render hook)
    """
    config = load_config()
    repo_root = repo_root_for()
    apps_dir = applications_dir or resolve(config.output.applications_dir, repo_root)

    if all_apps and slug:
        console.print("[red]ERROR:[/red] pass either <slug> or --all, not both")
        raise typer.Exit(code=1)
    if not all_apps and not slug:
        console.print("[red]ERROR:[/red] specify a slug or pass --all")
        raise typer.Exit(code=1)

    if all_apps:
        try:
            written = assemble_all(apps_dir)
        except ValueError as e:
            console.print(f"[red]ERROR:[/red] {e}")
            raise typer.Exit(code=1) from e
        console.print(f"[green]Assembled {len(written)} application(s):[/green]")
        for path in written:
            console.print(f"  WROTE {path}")
        return

    try:
        written = assemble_application(slug, apps_dir)
    except ValueError as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]Assembled[/green] {written}")


@app.command(name="fact-check")
def fact_check_cmd(
    draft: Path = typer.Argument(..., help="Path to the draft markdown file"),
    content_dir: Path | None = typer.Option(
        None,
        "--master-content-dir",
        help="Master content directory (default: from .apply-config.yaml)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Verify every hard claim in a draft against master YAMLs.

    Exit 0 = all claims verified; non-zero = at least one fabrication
    or unverified claim detected.
    """
    if not draft.exists():
        console.print(f"[red]ERROR:[/red] draft not found: {draft}")
        raise typer.Exit(code=1)

    if content_dir is None:
        config = load_config()
        repo_root = repo_root_for()
        content_dir = resolve(config.master.work_yml, repo_root).parent

    result = check_draft(draft.read_text(), content_dir)

    if result.passed:
        console.print(
            f"[green]✓ fact check passed[/green] — "
            f"{len(result.verified_claims)} claims verified"
        )
        if verbose:
            for v in result.verified_claims:
                console.print(f"  [dim][{v.kind:12s}][/dim] {v.claim!r} → {v.source_file}")
        raise typer.Exit(code=0)

    console.print(
        f"[red]✗ fact check FAILED[/red] — "
        f"{len(result.failed_claims)} unverified claim(s):"
    )
    for claim in result.failed_claims:
        console.print(f"  [red]✗[/red] {claim!r}")
    if verbose:
        verified_count = sum(1 for v in result.verified_claims if v.verified)
        console.print(f"\n[bold]Verified ({verified_count}):[/bold]")
        for v in result.verified_claims:
            if v.verified:
                console.print(f"  [green]✓[/green] [{v.kind}] {v.claim!r} → {v.source_file}")
    raise typer.Exit(code=1)


@app.command(name="anchor-check")
def anchor_check_cmd(
    selection: Path = typer.Argument(..., help="Path to bullet-selection.json"),
    decisions: Path | None = typer.Option(
        None,
        "--decisions",
        help="Path to bullet-decisions.json (logs anchor-drop reasons)",
    ),
    diff_out: Path | None = typer.Option(
        None,
        "--diff-out",
        help="Where to write the human-readable bullet-diff.md",
    ),
    master: Path | None = typer.Option(
        None,
        "--master",
        help="Override the master work.yml path (default: from .apply-config.yaml)",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
):
    """Verify that anchor bullets are preserved in a tailored bullet-selection.json.

    Exit 0 = all anchors preserved (or drops have logged reasons);
    1 = anchor dropped without reason; 2 = internal error.
    """
    config = load_config()
    repo_root = repo_root_for()
    master_path = master or resolve(config.master.work_yml, repo_root)

    if not master_path.exists():
        console.print(f"[red]ERROR:[/red] master work.yml not found: {master_path}")
        raise typer.Exit(code=2)

    try:
        result = check_anchors(
            master_path=master_path,
            selection_path=selection,
            decisions_path=decisions,
            money_threshold=config.anchor_thresholds.money_min_usd,
            percent_threshold=config.anchor_thresholds.percent_min,
            asset_count_threshold=config.anchor_thresholds.asset_count_min,
        )
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(code=2) from e

    if diff_out:
        diff_out.parent.mkdir(parents=True, exist_ok=True)
        diff_out.write_text(render_diff_md(result, master_path))

    if not quiet:
        color = "red" if result.exit_code != 0 else "green"
        console.print(
            f"[{color}]anchor-guard:[/{color}] "
            f"{len(result.kept)} kept, "
            f"{len(result.dropped_with_reason)} dropped-with-reason, "
            f"{len(result.dropped_without_reason)} dropped-without-reason "
            f"(exit {result.exit_code})"
        )

    raise typer.Exit(code=result.exit_code)


# ---------- helpers ----------


def _write_file(path: Path, content: str, force: bool) -> bool:
    if path.exists() and not force:
        console.print(f"  [dim]SKIP {path} (exists; --force to overwrite)[/dim]")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    console.print(f"  WROTE {path}")
    return True


def _copy_file(src: Path, dst: Path, force: bool) -> bool:
    if dst.exists() and not force:
        console.print(f"  [dim]SKIP {dst} (exists; --force to overwrite)[/dim]")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    console.print(f"  COPIED {dst}")
    return True


if __name__ == "__main__":
    app()
