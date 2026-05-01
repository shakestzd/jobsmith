"""Site rendering with privacy model + listings scaffolding.

Two modes:
- private (default): renders to _site/, includes everything (gitignored by the
  user's repo — see docs/architecture.md "Privacy model")
- public: renders to _site-public/, strips sensitive keys

Sensitive: salary, fit_score, must_have_table, bullet_decisions, bullet_diff,
gap_resolutions, hm_*, outreach_snippets, humanizer_audit.

CLI wiring is handled by feat-9377b64d (jobsmith site render / --public flag).
This module exposes the sanitization core, the listings discovery helpers,
and the init_site scaffolder so the CLI can import them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Bundled site templates live next to the package (templates/site/) so
# `init_site` can copy them into the user's repo. Resolved as
# <package_root>/templates/site/ (matches DEFAULT_PARTIALS_SRC convention
# in jobsmith.assemble).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SITE_TEMPLATE_SRC = PACKAGE_ROOT / "templates" / "site"

# ---------------------------------------------------------------------------
# Privacy contract: keys stripped in public mode
# ---------------------------------------------------------------------------

SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # Compensation intelligence
        "salary_range",
        "salary",
        # Scoring / private analysis
        "fit_score",
        "must_have_table",  # evidence column + full table
        "bullet_decisions",
        "bullet_diff",
        "gap_resolutions",
        # Hiring-manager intelligence
        "hm_name",
        "hm_email",
        "hm_signals",
        # Outreach & AI-tell internals
        "outreach_snippets",
        "humanizer_audit",
    }
)

_VALID_MODES = frozenset({"private", "public"})


# ---------------------------------------------------------------------------
# Core sanitization
# ---------------------------------------------------------------------------


def sanitize_variables(vars_dict: dict, mode: str = "private") -> dict:
    """Return a copy of *vars_dict* with sensitive keys removed when mode is 'public'.

    Args:
        vars_dict: Assembled variables dict (e.g. from _variables.yml assembly).
        mode: 'private' (identity, returns full dict) or 'public' (strips
              SENSITIVE_KEYS before returning).

    Returns:
        A new dict.  The input is never mutated.

    Raises:
        ValueError: If *mode* is not 'private' or 'public'.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid mode {mode!r}. Expected one of {sorted(_VALID_MODES)}."
        )

    if mode == "private":
        return dict(vars_dict)

    # public: strip sensitive keys (missing keys are silently ignored)
    return {k: v for k, v in vars_dict.items() if k not in SENSITIVE_KEYS}


# ---------------------------------------------------------------------------
# Site rendering (stub — quarto drives the actual render)
# ---------------------------------------------------------------------------


def render_site(
    root: Path,
    mode: str = "private",
    output_dir: Path | None = None,
) -> Path:
    """Render the Quarto portfolio site with the given privacy mode.

    Computes the output directory, sanitizes variables, then delegates to
    ``quarto render``.  When quarto is not on PATH a ``NotImplementedError``
    is raised so callers can gate on quarto availability.

    Args:
        root: Root of the Quarto project (the directory containing _quarto.yml).
        mode: 'private' → output to ``<root>/_site/``;
              'public'  → output to ``<root>/_site-public/``.
        output_dir: Override the default output directory.  When supplied this
                    takes precedence over the mode-derived default.

    Returns:
        The resolved output directory path.

    Raises:
        ValueError: If *mode* is not 'private' or 'public'.
        NotImplementedError: If ``quarto`` is not found on PATH.

    Note:
        The private output directory (``_site/``) should be listed in the
        user's ``.gitignore`` to prevent accidental publication.  The public
        directory (``_site-public/``) is also gitignored by default; an
        explicit ``--public`` flag at the CLI layer signals intentional
        publication.  See docs/architecture.md "Privacy model" for details.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid mode {mode!r}. Expected one of {sorted(_VALID_MODES)}."
        )

    # Resolve output directory
    if output_dir is None:
        output_dir = root / ("_site" if mode == "private" else "_site-public")

    # Quarto availability check
    if shutil.which("quarto") is None:
        raise NotImplementedError(
            "quarto is not available on PATH. "
            "Install Quarto (https://quarto.org/docs/get-started/) and re-run. "
            f"Resolved output dir would have been: {output_dir}"
        )

    # --- actual render (future implementation) ---
    # The CLI layer (feat-9377b64d) will wire the full render call, which will:
    #   1. Run `jobsmith assemble --all` to refresh _variables.yml for every app
    #   2. Apply sanitize_variables() in public mode before writing _variables.yml
    #   3. Invoke: quarto render <root> --output-dir <output_dir>
    #   4. Restore full _variables.yml after render (so private state is not lost)
    #
    # import subprocess
    # subprocess.run(
    #     ["quarto", "render", str(root), "--output-dir", str(output_dir)],
    #     check=True,
    # )

    return output_dir


# ---------------------------------------------------------------------------
# Site scaffolding — copies templates/site/ into the user's repo
# ---------------------------------------------------------------------------


def init_site(
    root: Path,
    template_src: Path | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """Scaffold the website project at *root*.

    Copies bundled `templates/site/` (the project ``_quarto.yml``, listings
    ``index.qmd``, listings stylesheet, and ``.gitignore``) into the target
    repository so ``quarto render`` produces an aggregator page over every
    ``private/applications/<slug>/index.qmd``.

    Args:
        root: Destination directory (the user's repo root). Created if missing.
        template_src: Override the bundled templates directory. Defaults to
            ``DEFAULT_SITE_TEMPLATE_SRC``.
        overwrite: When False (default) existing files are left untouched —
            jobsmith never clobbers user edits to ``_quarto.yml`` /
            ``index.qmd``. When True every file is replaced.

    Returns:
        List of files that were written (in copy order). May be empty when
        every file already existed and *overwrite* is False.

    Raises:
        FileNotFoundError: If *template_src* does not exist.
    """
    src = template_src or DEFAULT_SITE_TEMPLATE_SRC
    if not src.is_dir():
        raise FileNotFoundError(
            f"site template directory not found: {src}. "
            "Reinstall jobsmith or pass an explicit template_src."
        )

    root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for source_file in sorted(src.rglob("*")):
        if source_file.is_dir():
            continue
        rel = source_file.relative_to(src)
        dest = root / rel
        if dest.exists() and not overwrite:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, dest)
        written.append(dest)

    return written


# ---------------------------------------------------------------------------
# Listings discovery — used by the CLI's `site list` command
# ---------------------------------------------------------------------------


def discover_applications(root: Path) -> list[Path]:
    """Return every application directory under ``private/applications/`` that
    has been assembled by jobsmith (i.e. contains both ``index.qmd`` and
    ``.apply-state/``). Stable sort: alphabetical by slug.

    Empty directories, directories without ``.apply-state/``, and directories
    whose name starts with ``_`` or ``.`` are skipped — same convention as
    ``jobsmith.assemble.assemble_all``.
    """
    apps_root = root / "private" / "applications"
    if not apps_root.is_dir():
        return []

    discovered: list[Path] = []
    for entry in sorted(apps_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        if not (entry / ".apply-state").is_dir():
            continue
        if not (entry / "index.qmd").is_file():
            continue
        discovered.append(entry)
    return discovered
