"""jobsmith CLI — Typer entry point.

Exposes:
    jobsmith init          — scaffold an application repo
    jobsmith validate      — validate .apply-config.yaml
    jobsmith status        — show repo state (config, master YAMLs, db)
    jobsmith doctor        — diagnose common setup issues
    jobsmith fact-check    — verify draft claims against master YAMLs
    jobsmith anchor-check  — verify anchor bullets are preserved
    jobsmith site init     — scaffold the listings website project
    jobsmith site list     — print every assembled application in a Rich table
    jobsmith site render   — quarto render the site (private or --public)
    jobsmith site serve    — quarto preview the site (live reload)
    jobsmith site review   — open one application's index.html in the browser
    jobsmith --version
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .assemble import PACKAGE_ROOT, assemble_all, assemble_application
from .site import discover_applications, init_site, render_site
from .config import CONFIG_FILENAME, find_config, load_config
from .factcheck import check_draft
from .guard import check_anchors, render_diff_md
from .paths import all_master_paths, repo_root_for, resolve
from .voice import _extract_bullets_from_qmd

app = typer.Typer(
    name="jobsmith",
    help="Tailored resume + cover-letter pipeline. Master-first, no fabrication, anchor-preserving.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

# Path to the package root for locating example master YAML
# PACKAGE_ROOT is sourced from jobsmith.assemble._find_package_root so the
# same discovery logic handles both checkout (templates/ as sibling of src/)
# and wheel (templates/ inside the package) layouts. Don't recompute here —
# the parent.parent.parent walk only works for editable installs.
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
    private/benchmarks/
    private/feedback/
    """
)

FEEDBACK_README = dedent(
    """\
    # private/feedback/ — learning feedback from user edits

    Jobsmith writes JSON records here when you run `feedback record`
    after hand-editing the agent's drafts. The diff is taken between
    the live files and the immutable `.agent.md` snapshots that the
    apply pipeline captures at phase completion:

      - `.apply-state/prose-draft.md` (live, user-edited)
        vs `.apply-state/prose-draft.agent.md` (snapshot)
      - `<app>/cover-letter-draft.md` (live, user-edited at app root)
        vs `.apply-state/cover-letter-draft.agent.md` (snapshot)

    Each record captures what the agent wrote vs. what you changed, so
    future runs can learn from your edits.

    ## Usage

    ```
    jobsmith feedback record <slug>   # diff latest user edits
    jobsmith feedback list            # view records
    jobsmith feedback export          # YAML summary — review before sharing
    jobsmith feedback prune --older-than 90d
    ```

    ## Privacy

    - Raw records contain private application text — the `before`/`after`
      fields store the actual prose you edited, which can include company
      names, metrics, and other sensitive details. Treat the directory as
      private.
    - `jobsmith feedback export` drops slug + per-app metadata (company /
      role context) but copies `lesson` strings verbatim. If you author
      lessons that name a specific employer, claim, or person, that text
      appears in the export. **Review the YAML before syncing it across
      machines or pasting it anywhere shared.**
    - This directory is gitignored by default so personal data stays local;
      prefer the export over the raw JSON, and double-check what's in the
      export before sharing.
    """
)

BENCHMARKS_README = dedent(
    """\
    # private/benchmarks/ — personal style references

    Place your best previous application files here so jobsmith can use them
    as quality benchmarks.  Symlinks work well (e.g., link to the best version
    of your resume from a previous application cycle).

    ## Files to add

    | File                | Purpose                                      |
    |---------------------|----------------------------------------------|
    | resume.qmd          | Quarto source of your favourite resume       |
    | resume.pdf          | Rendered PDF of the same                     |
    | cover-letter.md     | Markdown source of your best cover letter    |
    | cover-letter.pdf    | PDF render of the cover letter (optional)    |
    | workflow.html       | Rendered workflow review page (optional)     |

    ## Wire up in .apply-config.yaml

    ```yaml
    benchmarks:
      resume_qmd:       private/benchmarks/resume.qmd
      resume_pdf:       private/benchmarks/resume.pdf
      cover_letter_md:  private/benchmarks/cover-letter.md
      required: false   # set true once you have all five files in place
    ```

    ## Notes

    - Benchmarks are **style references only** — their content is never copied
      into a new application.
    - This directory is gitignored so your personal data stays private.
    - Run `jobsmith doctor` to verify all paths resolve correctly.
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
    (target / "private" / "feedback").mkdir(parents=True, exist_ok=True)
    console.print(f"  ENSURED {target / 'private' / 'feedback'}")
    _write_file(
        target / "private" / "feedback" / "README.md",
        FEEDBACK_README,
        force,
    )

    # Benchmarks scaffold
    console.print("\n[bold]Benchmark scaffold:[/bold]")
    _write_file(
        target / "private" / "benchmarks" / "README.md",
        BENCHMARKS_README,
        force,
    )

    # .gitignore additions
    console.print("\n[bold].gitignore:[/bold]")
    gitignore = target / ".gitignore"
    if gitignore.exists():
        existing = gitignore.read_text()
        new_lines: list[str] = []
        if "jobsmith" not in existing:
            new_lines.append(GITIGNORE_ADDITIONS)
        else:
            # jobsmith block already present — ensure individual rules are there
            for rule in ("private/benchmarks/", "private/feedback/"):
                if rule not in existing:
                    new_lines.append(f"\n{rule}\n")
        if new_lines:
            gitignore.write_text(existing.rstrip() + "\n" + "".join(new_lines))
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
def doctor() -> None:
    """Run preflight environment checks."""
    from .doctor import preflight
    raise typer.Exit(0 if preflight() else 1)


@app.command()
def apply(
    url: str = typer.Argument(..., help="Job description URL"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip phase-gate confirmations"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Restart the pipeline from phase 1 even if prior artifacts exist.",
    ),
    verbose: int = typer.Option(
        0,
        "-v",
        "--verbose",
        count=True,
        help="Verbosity: -v shows filtered tool calls; -vv shows all tool calls.",
    ),
) -> None:
    """Run the three-phase apply pipeline against a JD URL."""
    from .apply import run_apply
    raise typer.Exit(run_apply(url, skip_confirm=yes, force=force, verbosity=verbose))


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


@app.command()
def lint(
    master: Path = typer.Option(
        Path("master"),
        help="Path to master/ directory containing *.yml files",
    ),
    benchmark: Path | None = typer.Option(
        None,
        help="Path to benchmark resume.qmd (optional)",
    ),
) -> None:
    """Validate master YAML schema and benchmark before assemble.

    Checks:
      - Each *.yml in master/ parses as a valid YAML list of positions
      - Each position has the expected keys (title, details list)
      - benchmark.qmd (if given) contains at least one bullet line
    Prints errors with filename; exits non-zero on any error.
    """
    errors: list[str] = []

    # Validate master YAML files
    if not master.exists():
        console.print(f"[red]ERROR:[/red] master directory not found: {master}")
        raise typer.Exit(code=1)

    yml_files = sorted(master.glob("*.yml"))
    if not yml_files:
        console.print(f"[yellow]WARNING:[/yellow] No *.yml files found in {master}")

    for yml_path in yml_files:
        try:
            data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{yml_path}: YAML parse error — {exc}")
            continue

        if data is None:
            # Empty file is acceptable (no positions)
            continue

        # work.yml must be a list of positions
        if yml_path.name == "work.yml":
            if not isinstance(data, list):
                errors.append(
                    f"{yml_path}:1: root must be a list of positions, "
                    f"got {type(data).__name__}"
                )
                continue
            for i, pos in enumerate(data):
                if not isinstance(pos, dict):
                    errors.append(
                        f"{yml_path}: position[{i}] must be a mapping, "
                        f"got {type(pos).__name__}"
                    )
                    continue
                details = pos.get("details")
                if details is not None and not isinstance(details, list):
                    errors.append(
                        f"{yml_path}: position[{i}].details must be a list, "
                        f"got {type(details).__name__}"
                    )

    # Validate benchmark if provided
    if benchmark is not None:
        if not benchmark.exists():
            errors.append(f"benchmark: file not found — {benchmark}")
        else:
            bullets = _extract_bullets_from_qmd(benchmark)
            if not bullets:
                errors.append(
                    f"benchmark {benchmark}: no bullet lines found (lines starting with '- '). "
                    "Ensure the benchmark resume contains at least one bullet."
                )

    if errors:
        for err in errors:
            console.print(f"[red]LINT ERROR:[/red] {err}")
        raise typer.Exit(code=1)

    console.print("[green]lint passed[/green]")


# ---------- site subcommand group ----------


site_app = typer.Typer(
    name="site",
    help="Aggregator website over private/applications/. Init, render, serve, list, review.",
    no_args_is_help=True,
)
app.add_typer(site_app)


@site_app.command("init")
def site_init_cmd(
    root: Path = typer.Argument(
        Path("."),
        help="Root of the user's repo. The website project is scaffolded here.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing _quarto.yml / index.qmd / styles/jobsmith.scss.",
    ),
) -> None:
    """Scaffold the listings website project (`_quarto.yml`, `index.qmd`,
    `styles/jobsmith.scss`, `.gitignore`) into *root*. By default existing
    files are preserved; pass `--force` to overwrite."""
    written = init_site(root.resolve(), overwrite=force)
    if not written:
        console.print(
            "[yellow]No files written[/yellow] — every site template already "
            "exists. Pass --force to refresh from the bundled templates."
        )
        return
    console.print(
        f"[green]Scaffolded {len(written)} file(s):[/green]"
    )
    for path in written:
        console.print(f"  WROTE {path}")
    console.print(
        "\nNext: run [cyan]jobsmith assemble --all[/cyan] to populate per-app "
        "index.qmd files, then [cyan]jobsmith site serve[/cyan] to preview."
    )


@site_app.command("list")
def site_list_cmd(
    root: Path = typer.Argument(
        Path("."), help="Root of the user's repo (must contain private/applications/)."
    ),
) -> None:
    """Print every assembled application as a Rich table.

    Reads each app's index.qmd frontmatter (company, position, status,
    fit_score, date_found) and surfaces them sorted by fit_score desc.
    Apps without an index.qmd or .apply-state/ are skipped silently —
    same convention as `assemble_all` / Quarto listings.
    """
    apps = discover_applications(root.resolve())
    if not apps:
        console.print(
            "[yellow]No assembled applications found[/yellow] under "
            f"{(root / 'private' / 'applications').resolve()}.\n"
            "Run [cyan]jobsmith assemble <slug>[/cyan] (or "
            "[cyan]jobsmith assemble --all[/cyan]) first."
        )
        raise typer.Exit(code=0)

    table = Table(title="Assembled applications", show_lines=False)
    table.add_column("Slug", style="cyan", no_wrap=True)
    table.add_column("Company")
    table.add_column("Position")
    table.add_column("Status")
    table.add_column("Fit", justify="right")
    table.add_column("Found")

    rows = []
    for app_dir in apps:
        meta = _read_index_frontmatter(app_dir / "index.qmd")
        rows.append(
            (
                app_dir.name,
                meta.get("company") or "—",
                meta.get("position") or "—",
                meta.get("status") or "—",
                meta.get("fit_score"),
                meta.get("date_found") or "—",
            )
        )

    # Sort: highest fit_score first, then by date_found desc.
    def _sort_key(row: tuple) -> tuple:
        fit = row[4]
        return (-(fit if isinstance(fit, (int, float)) else -1), str(row[5]))

    rows.sort(key=_sort_key)
    for slug, company, position, status, fit, date_found in rows:
        fit_display = f"{fit:.2f}" if isinstance(fit, (int, float)) else "—"
        table.add_row(slug, str(company), str(position), str(status), fit_display, str(date_found))

    console.print(table)


@site_app.command("render")
def site_render_cmd(
    root: Path = typer.Argument(
        Path("."), help="Root of the website project (contains _quarto.yml)."
    ),
    public: bool = typer.Option(
        False,
        "--public",
        help="Render the sanitized public variant to _site-public/ (strips "
        "salary, fit_score, hm_*, etc. — see docs/architecture.md "
        "Privacy model). Default is private (everything → _site/).",
    ),
) -> None:
    """Run `quarto render` on the website project.

    Privacy model: default mode renders to `_site/` (gitignored). The
    `--public` flag re-renders to `_site-public/` after applying
    `jobsmith.site.sanitize_variables` so sensitive keys are stripped.
    """
    mode = "public" if public else "private"
    try:
        out = render_site(root.resolve(), mode=mode)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    label = "[red bold]PUBLIC[/red bold]" if public else "[green]private[/green]"
    console.print(f"Rendered ({label}) to: [cyan]{out}[/cyan]")


@site_app.command("serve")
def site_serve_cmd(
    root: Path = typer.Argument(
        Path("."), help="Root of the website project (contains _quarto.yml)."
    ),
) -> None:
    """Run `quarto preview` for live-reload development.

    Always serves the private variant — the public sanitization step only
    runs at render-time, not preview-time. If you need to QA the public
    variant before sharing, run `jobsmith site render --public` and open
    `_site-public/index.html` directly.
    """
    quarto = shutil.which("quarto")
    if quarto is None:
        console.print(
            "[red]quarto is not on PATH.[/red] "
            "Install Quarto (https://quarto.org/docs/get-started/) and re-run."
        )
        raise typer.Exit(code=2)

    import subprocess

    console.print(f"[cyan]quarto preview[/cyan] at {root.resolve()}")
    result = subprocess.run([quarto, "preview", str(root.resolve())])
    raise typer.Exit(code=result.returncode)


@site_app.command("review")
def site_review_cmd(
    slug: str = typer.Argument(..., help="Application slug (directory name)."),
    root: Path = typer.Option(
        Path("."), "--root", help="Root of the user's repo."
    ),
) -> None:
    """Open the rendered review surface for one application in the default browser.

    Resolves to `<root>/_site/private/applications/<slug>/index.html` if the
    site has been rendered; otherwise falls back to opening
    `<root>/private/applications/<slug>/index.qmd` so the user can render manually.
    """
    import webbrowser

    site_html = (
        root.resolve()
        / "_site"
        / "private"
        / "applications"
        / slug
        / "index.html"
    )
    raw_qmd = (
        root.resolve()
        / "private"
        / "applications"
        / slug
        / "index.qmd"
    )

    if site_html.is_file():
        target = site_html
    elif raw_qmd.is_file():
        console.print(
            f"[yellow]No rendered HTML found for {slug}.[/yellow] "
            "Opening the source QMD instead — run "
            "[cyan]jobsmith site render[/cyan] first to get rendered output."
        )
        target = raw_qmd
    else:
        console.print(
            f"[red]Application {slug!r} not found[/red] under "
            f"{(root / 'private' / 'applications').resolve()}."
        )
        raise typer.Exit(code=2)

    webbrowser.open(target.as_uri())
    console.print(f"Opened: [cyan]{target}[/cyan]")


# ---------- feedback subcommand group ----------


feedback_app = typer.Typer(
    help="Capture and manage learning feedback from user edits.",
    no_args_is_help=True,
)
app.add_typer(feedback_app, name="feedback")


@feedback_app.command("record")
def feedback_record(slug: str = typer.Argument(...)) -> None:
    """Diff user edits against agent output and write feedback JSON records."""
    from .feedback import record as _record

    repo_root = repo_root_for()
    # Honour `output.applications_dir` from .apply-config.yaml — repos with a
    # custom layout (e.g. archive/applications/) would otherwise be invisible
    # to feedback record. Fall back to the default when no config exists.
    config_path = find_config(repo_root)
    if config_path is not None:
        cfg = load_config(config_path)
        apps_dir = resolve(cfg.output.applications_dir, config_path.parent)
    else:
        apps_dir = repo_root / "private" / "applications"
    app_dir = apps_dir / slug
    feedback_dir = repo_root / "private" / "feedback"

    if not app_dir.exists():
        console.print(f"[red]ERROR:[/red] application directory not found: {app_dir}")
        raise typer.Exit(code=1)

    records = _record(slug, app_dir=app_dir, feedback_dir=feedback_dir)
    if not records:
        console.print(f"[yellow]No significant edits detected for {slug!r}.[/yellow]")
    else:
        console.print(f"[green]Recorded {len(records)} feedback item(s) for {slug!r}:[/green]")
        for r in records:
            console.print(f"  [{r['kind']}] {r['before'][:60]!r} → {r['after'][:60]!r}")


@feedback_app.command("list")
def feedback_list(
    kind: str | None = typer.Option(None, "--kind", help="Filter by kind (prose-bullet or cover-letter-paragraph)"),
    since: str | None = typer.Option(None, "--since", help="ISO date or 'Nd' for N days ago"),
) -> None:
    """List feedback records, optionally filtered by kind or date."""
    from datetime import timedelta

    from .feedback import list_records

    since_dt = None
    if since is not None:
        if since.endswith("d") and since[:-1].isdigit():
            days = int(since[:-1])
            since_dt = datetime.now(timezone.utc) - timedelta(days=days)
        else:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                console.print(f"[red]ERROR:[/red] Cannot parse --since value: {since!r}")
                raise typer.Exit(code=1) from None

    records = list_records(filter_kind=kind, since=since_dt)

    if not records:
        console.print("[yellow]No feedback records found.[/yellow]")
        return

    table = Table(title="Feedback records", show_lines=True)
    table.add_column("Timestamp", style="dim")
    table.add_column("Slug", style="cyan")
    table.add_column("Kind")
    table.add_column("Before", max_width=40)
    table.add_column("After", max_width=40)
    table.add_column("Lesson")

    for r in records:
        table.add_row(
            r.get("timestamp", "")[:19],
            r.get("slug", ""),
            r.get("kind", ""),
            r.get("before", "")[:40],
            r.get("after", "")[:40],
            r.get("lesson", ""),
        )

    console.print(table)


@feedback_app.command("prune")
def feedback_prune(
    older_than: str = typer.Option("90d", "--older-than", help="Delete records older than N days (e.g. 90d)"),
) -> None:
    """Delete feedback records older than a threshold."""
    from .feedback import prune

    if older_than.endswith("d") and older_than[:-1].isdigit():
        days = int(older_than[:-1])
    elif older_than.isdigit():
        days = int(older_than)
    else:
        console.print(f"[red]ERROR:[/red] Cannot parse --older-than value: {older_than!r}. Use e.g. '90d'.")
        raise typer.Exit(code=1)

    deleted = prune(older_than_days=days)
    console.print(f"[green]Pruned {deleted} feedback record(s) older than {days} days.[/green]")


@feedback_app.command("export")
def feedback_export(
    out: Path | None = typer.Option(None, "--out", help="Output path (default: stdout)"),  # noqa: B008
) -> None:
    """Export a sanitized YAML summary of feedback patterns."""
    from .feedback import export

    yaml_str = export()

    if out is None:
        console.print(yaml_str, end="")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml_str)
        console.print(f"[green]Exported feedback summary to {out}[/green]")


def _read_index_frontmatter(path: Path) -> dict:
    """Extract the YAML frontmatter from a Quarto index.qmd as a dict.

    Returns an empty dict if the file has no frontmatter, parsing fails, or
    the file does not exist. Used by `jobsmith site list` to surface
    company / position / status / fit_score per row without re-running the
    full assemble pipeline.
    """
    if not path.is_file():
        return {}

    text = path.read_text()
    if not text.startswith("---"):
        return {}

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}

    import yaml

    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}

    return meta if isinstance(meta, dict) else {}


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
