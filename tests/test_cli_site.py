"""Tests for `jobsmith site` Typer subcommands.

Covers init / list / review behaviours that don't require quarto on PATH.
render and serve are exercised lightly (error path when quarto is absent
and arg parsing) since the actual quarto call is left to feat-9377b64d's
follow-on integration tests.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jobsmith.cli import app

runner = CliRunner()


# ---------- helpers ----------


def _make_app(root: Path, slug: str, frontmatter: str | None = None) -> Path:
    """Create a fully-assembled application under <root>/private/applications/."""
    app_dir = root / "private" / "applications" / slug
    app_dir.mkdir(parents=True)
    (app_dir / ".apply-state").mkdir()

    fm = frontmatter if frontmatter is not None else (
        '---\n'
        f'title: "{slug}"\n'
        'company: "Acme Corp"\n'
        'position: "Senior Engineer"\n'
        'status: "drafting"\n'
        'fit_score: 0.78\n'
        'date_found: "2026-04-29"\n'
        '---\n'
        f'\n# {slug}\n'
    )
    (app_dir / "index.qmd").write_text(fm)
    return app_dir


# ---------- jobsmith site init ----------


def test_site_init_scaffolds_files(tmp_path: Path) -> None:
    result = runner.invoke(app, ["site", "init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "_quarto.yml").is_file()
    assert (tmp_path / "index.qmd").is_file()
    assert (tmp_path / "styles" / "jobsmith.scss").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert "Scaffolded" in result.output


def test_site_init_preserves_existing_files(tmp_path: Path) -> None:
    (tmp_path / "_quarto.yml").write_text("user-edited\n")

    result = runner.invoke(app, ["site", "init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # User edit not clobbered
    assert (tmp_path / "_quarto.yml").read_text() == "user-edited\n"


def test_site_init_force_overwrites(tmp_path: Path) -> None:
    (tmp_path / "_quarto.yml").write_text("user-edited\n")

    result = runner.invoke(app, ["site", "init", str(tmp_path), "--force"])

    assert result.exit_code == 0, result.output
    assert "user-edited" not in (tmp_path / "_quarto.yml").read_text()


def test_site_init_idempotent_when_no_changes(tmp_path: Path) -> None:
    runner.invoke(app, ["site", "init", str(tmp_path)])  # first scaffold
    result = runner.invoke(app, ["site", "init", str(tmp_path)])  # second run
    assert result.exit_code == 0, result.output
    assert "No files written" in result.output


# ---------- jobsmith site list ----------


def test_site_list_shows_assembled_apps(tmp_path: Path) -> None:
    _make_app(tmp_path, "acme-engineer")
    _make_app(tmp_path, "stripe-platform")

    result = runner.invoke(app, ["site", "list", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "acme-engineer" in result.output
    assert "stripe-platform" in result.output
    assert "Acme Corp" in result.output


def test_site_list_warns_when_no_apps(tmp_path: Path) -> None:
    result = runner.invoke(app, ["site", "list", str(tmp_path)])
    assert result.exit_code == 0
    assert "No assembled applications" in result.output


def test_site_list_handles_missing_frontmatter(tmp_path: Path) -> None:
    """index.qmd without YAML frontmatter still renders the row with em-dashes."""
    _make_app(tmp_path, "no-frontmatter", frontmatter="# bare\n")

    result = runner.invoke(app, ["site", "list", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "no-frontmatter" in result.output


def test_site_list_sorts_by_fit_score_desc(tmp_path: Path) -> None:
    _make_app(
        tmp_path,
        "low-fit",
        frontmatter=(
            '---\ntitle: "low"\ncompany: "L"\nposition: "p"\nstatus: "x"\n'
            'fit_score: 0.32\ndate_found: "2026-04-01"\n---\n'
        ),
    )
    _make_app(
        tmp_path,
        "high-fit",
        frontmatter=(
            '---\ntitle: "high"\ncompany: "H"\nposition: "p"\nstatus: "x"\n'
            'fit_score: 0.91\ndate_found: "2026-04-01"\n---\n'
        ),
    )

    result = runner.invoke(app, ["site", "list", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # high-fit row should come before low-fit in the table
    high_pos = result.output.find("high-fit")
    low_pos = result.output.find("low-fit")
    assert 0 <= high_pos < low_pos, result.output


# ---------- jobsmith site review ----------


def test_site_review_unknown_slug_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["site", "review", "ghost-co-engineer", "--root", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


def test_site_review_falls_back_to_qmd_when_no_html(tmp_path: Path, monkeypatch) -> None:
    """When _site/<slug>/index.html doesn't exist but the source QMD does,
    review opens the QMD and warns the user to render first."""
    _make_app(tmp_path, "acme-engineer")

    opened: list[str] = []
    monkeypatch.setattr(
        "webbrowser.open", lambda url: opened.append(url) or True
    )

    result = runner.invoke(
        app, ["site", "review", "acme-engineer", "--root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Opening the source QMD" in result.output
    assert len(opened) == 1
    assert opened[0].endswith("acme-engineer/index.qmd")


def test_site_review_opens_rendered_html_when_present(tmp_path: Path, monkeypatch) -> None:
    """When _site/<slug>/index.html exists, that's what gets opened (preferred)."""
    _make_app(tmp_path, "acme-engineer")
    site_app_dir = tmp_path / "_site" / "private" / "applications" / "acme-engineer"
    site_app_dir.mkdir(parents=True)
    (site_app_dir / "index.html").write_text("<html><body>rendered</body></html>")

    opened: list[str] = []
    monkeypatch.setattr(
        "webbrowser.open", lambda url: opened.append(url) or True
    )

    result = runner.invoke(
        app, ["site", "review", "acme-engineer", "--root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert opened[0].endswith("_site/private/applications/acme-engineer/index.html")


# ---------- jobsmith site render ----------


def test_site_render_without_quarto_exits_2(tmp_path: Path, monkeypatch) -> None:
    """When quarto is not on PATH, render exits 2 with a clear error message."""
    # Scaffold a minimal _quarto.yml so render_site gets past the existence check
    (tmp_path / "_quarto.yml").write_text("project:\n  type: website\n")
    monkeypatch.setattr("jobsmith.site.shutil.which", lambda name: None)

    result = runner.invoke(app, ["site", "render", str(tmp_path)])

    assert result.exit_code == 2
    assert "quarto" in result.output.lower()


def test_site_render_missing_quarto_yml_exits_2(tmp_path: Path) -> None:
    """When _quarto.yml is missing, render exits 2 and points the user at site init."""
    result = runner.invoke(app, ["site", "render", str(tmp_path)])
    assert result.exit_code == 2
    assert "_quarto.yml" in result.output


def test_site_render_public_flag_parsed(tmp_path: Path, monkeypatch) -> None:
    """The --public flag is accepted and triggers the public render path.

    We don't actually call quarto here — we patch site.render_site and
    confirm it received mode='public'.
    """
    captured: dict = {}

    def fake_render(root: Path, mode: str = "private", output_dir=None, profile: str = "private"):
        captured["mode"] = mode
        captured["profile"] = profile
        return root / ("_site-public" if mode == "public" else "_site")

    monkeypatch.setattr("jobsmith.cli.render_site", fake_render)

    result = runner.invoke(app, ["site", "render", str(tmp_path), "--public"])

    assert result.exit_code == 0, result.output
    assert captured["mode"] == "public"
    assert "PUBLIC" in result.output


def test_site_render_default_is_private(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_render(root: Path, mode: str = "private", output_dir=None, profile: str = "private"):
        captured["mode"] = mode
        captured["profile"] = profile
        return root / "_site"

    monkeypatch.setattr("jobsmith.cli.render_site", fake_render)

    result = runner.invoke(app, ["site", "render", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert captured["mode"] == "private"
    assert "private" in result.output


def test_site_render_default_profile_is_private(tmp_path: Path, monkeypatch) -> None:
    """By default, --profile private is passed to render_site so the
    _quarto-private.yml profile is activated and private/applications/**/*.qmd
    pages are compiled. This is the core fix for bug-08a3ad82."""
    captured: dict = {}

    def fake_render(root: Path, mode: str = "private", output_dir=None, profile: str = "private"):
        captured["profile"] = profile
        return root / "_site"

    monkeypatch.setattr("jobsmith.cli.render_site", fake_render)

    result = runner.invoke(app, ["site", "render", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert captured["profile"] == "private"


def test_site_render_profile_option_overrides_default(tmp_path: Path, monkeypatch) -> None:
    """Passing --profile <name> forwards the custom profile to render_site."""
    captured: dict = {}

    def fake_render(root: Path, mode: str = "private", output_dir=None, profile: str = "private"):
        captured["profile"] = profile
        return root / "_site"

    monkeypatch.setattr("jobsmith.cli.render_site", fake_render)

    result = runner.invoke(app, ["site", "render", str(tmp_path), "--profile", "public"])

    assert result.exit_code == 0, result.output
    assert captured["profile"] == "public"


def test_site_render_profile_empty_string_skips_flag(tmp_path: Path, monkeypatch) -> None:
    """Passing --profile '' forwards an empty string so render_site omits
    the --profile flag from the quarto subprocess call."""
    captured: dict = {}

    def fake_render(root: Path, mode: str = "private", output_dir=None, profile: str = "private"):
        captured["profile"] = profile
        return root / "_site"

    monkeypatch.setattr("jobsmith.cli.render_site", fake_render)

    result = runner.invoke(app, ["site", "render", str(tmp_path), "--profile", ""])

    assert result.exit_code == 0, result.output
    assert captured["profile"] == ""


# ---------- jobsmith init — benchmarks scaffold ----------


def _run_init(tmp_path: Path, extra_args: list[str] | None = None) -> object:
    """Run `jobsmith init <tmp_path>` with example copy patched out."""
    from unittest.mock import patch

    import jobsmith.cli as cli_module

    # Patch EXAMPLES_DIR so init doesn't try to copy real master YAML examples
    with patch.object(cli_module, "EXAMPLES_DIR", tmp_path / "_fake_examples"):
        # create fake examples so init doesn't error on missing dir
        fake_examples = tmp_path / "_fake_examples"
        fake_examples.mkdir(parents=True, exist_ok=True)
        return runner.invoke(app, ["init", str(tmp_path)] + (extra_args or []))


def test_init_creates_benchmarks_readme(tmp_path: Path) -> None:
    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output
    readme = tmp_path / "private" / "benchmarks" / "README.md"
    assert readme.is_file(), f"README not found at {readme}"
    content = readme.read_text()
    assert "benchmarks" in content.lower()


def test_init_adds_benchmarks_to_gitignore(tmp_path: Path) -> None:
    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output
    gitignore = (tmp_path / ".gitignore").read_text()
    assert "private/benchmarks/" in gitignore


def test_init_ignores_pipeline_db_and_review_state(tmp_path: Path) -> None:
    """Roborev #922 MEDIUM: bootstrap must ignore the new private DBs.

    private/jobsmith.db carries specialist outputs (slice 1) and
    private/.review/ carries amendments + chat history (slice 1) —
    both are personal review state that should never land in version
    control. Earlier the bootstrap only listed private/job_search.db.
    """
    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output
    gitignore = (tmp_path / ".gitignore").read_text()
    assert "private/jobsmith.db" in gitignore
    assert "private/jobsmith.db-*" in gitignore  # WAL/SHM sidecars
    assert "private/.review/" in gitignore
    assert "private/.review-backups/" in gitignore


def test_init_does_not_duplicate_gitignore_rules(tmp_path: Path) -> None:
    """Running init twice should not duplicate .gitignore entries."""
    _run_init(tmp_path)
    _run_init(tmp_path)
    gitignore = (tmp_path / ".gitignore").read_text()
    assert gitignore.count("private/benchmarks/") == 1


def test_init_preserves_existing_gitignore_content(tmp_path: Path) -> None:
    existing_gitignore = tmp_path / ".gitignore"
    existing_gitignore.write_text("node_modules/\n.env\n")

    result = _run_init(tmp_path)
    assert result.exit_code == 0, result.output

    content = existing_gitignore.read_text()
    assert "node_modules/" in content
    assert ".env" in content
    assert "private/benchmarks/" in content
