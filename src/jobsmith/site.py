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
# `init_site` can copy them into the user's repo. Resolution mirrors
# jobsmith.assemble._find_package_root: works for both checkout (templates is
# a sibling of src/) and wheel installs (templates is force-included inside
# the package).
from .assemble import PACKAGE_ROOT  # noqa: E402

DEFAULT_SITE_TEMPLATE_SRC = PACKAGE_ROOT / "templates" / "site"

# ---------------------------------------------------------------------------
# Privacy contract — paths stripped from _variables.yml in public mode
# ---------------------------------------------------------------------------
#
# Each entry is a dotted path into the assembled variables dict (matches the
# nested shape produced by jobsmith.assemble.assemble_application). Paths
# resolve recursively: the first segment indexes the top-level dict, the next
# segment indexes the value at that key, etc. Trailing wildcard `*` removes
# every key under the resolved subdict.
#
# Top-level scalars stay listed for clarity; nested keys live under their
# real parents (fit.*, hm.*, bullets.*, jd.must_haves, jd.must_haves_md, …)
# so the public render actually strips what it claims to strip.

SENSITIVE_VARIABLE_PATHS: tuple[tuple[str, ...], ...] = (
    # Compensation
    ("salary_range",),
    ("salary",),
    # Scoring / private analysis
    ("fit", "*"),
    ("fit_score",),                # legacy top-level mirror, if present
    ("must_have_table",),          # legacy top-level mirror
    # Bullet diff / decisions / gap resolutions
    ("bullets", "anchor_bullets_dropped"),
    ("bullet_diff",),
    ("bullet_decisions",),
    ("gap_resolutions",),
    # Hiring-manager intel
    ("hm", "*"),
    ("hm_md",),
    ("hm_name",),
    ("hm_email",),
    ("hm_signals",),
    # Outreach + AI-tell internals
    ("outreach",),
    ("outreach_snippets",),
    ("humanizer_audit",),
    # Cover letter draft is the user's working text — not for public sharing.
    ("cover_letter_draft",),
    # Company research is synthesized intel (mission/values/selected reasons)
    # that the user owns. The block file is also redacted; this strips the
    # raw text mirrored into _variables.yml.
    ("company_research",),
    # Cleaned JD text often contains salary ranges, internal req IDs, and
    # named-HM mentions that the JD parser left in. Redact for public mode.
    ("jd", "text_clean"),
)

# Block files (under <app>/_blocks/) that must be replaced with a redaction
# notice in public mode. Per-app pages include these via Quarto shortcodes,
# so stripping only _variables.yml leaves them visible in the rendered HTML.
SENSITIVE_BLOCK_FILES: frozenset[str] = frozenset(
    {
        "must-have-table.md",
        "matched-evidence.md",
        "concerns.md",
        "hm-dossier.md",
        "outreach-snippets.md",
        "humanizer-audit.md",
        "company-research.md",  # mission/values OK; selected reasons are user voice
        "cover-letter.md",
        "cover-letter-body.md",
    }
)

# Backwards-compat: the original flat SENSITIVE_KEYS frozenset is kept for
# tests / external consumers that imported it. The names match the top-level
# of SENSITIVE_VARIABLE_PATHS.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    path[0] for path in SENSITIVE_VARIABLE_PATHS if len(path) == 1
) | frozenset({"fit", "hm", "must_have_table"})  # nested-parent names too

_VALID_MODES = frozenset({"private", "public"})

# Public-mode redaction text written in place of stripped block files. Kept
# short so the rendered HTML is small; the partial still has a section to
# render rather than crashing on a missing include.
_PUBLIC_REDACTION_BLOCK = (
    '::: {.callout-note appearance="minimal"}\n'
    "_Section omitted in the public variant — contains private analysis "
    "(fit scoring, hiring-manager intel, or unedited prose). View the "
    "private `_site/` build for the full content._\n"
    ":::\n"
)


# ---------------------------------------------------------------------------
# Core sanitization
# ---------------------------------------------------------------------------


def sanitize_variables(vars_dict: dict, mode: str = "private") -> dict:
    """Return a deep copy of *vars_dict* with sensitive paths removed in public mode.

    Args:
        vars_dict: Assembled variables dict (e.g. from _variables.yml assembly).
            Nested — see jobsmith.assemble.assemble_application for the shape.
        mode: 'private' (identity copy, full dict) or 'public' (recursively
            strips every path in SENSITIVE_VARIABLE_PATHS).

    Returns:
        A new dict. The input is never mutated.

    Raises:
        ValueError: If *mode* is not 'private' or 'public'.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid mode {mode!r}. Expected one of {sorted(_VALID_MODES)}."
        )

    import copy

    out = copy.deepcopy(vars_dict)
    if mode == "private":
        return out

    for path in SENSITIVE_VARIABLE_PATHS:
        _strip_path(out, path)
    return out


def _strip_path(target: dict, path: tuple[str, ...]) -> None:
    """Recursively remove *path* from the nested *target* dict in place."""
    if not path:
        return

    head, *rest = path

    if head == "*":
        # Wildcard means: empty the current dict.
        if isinstance(target, dict):
            target.clear()
        return

    if not isinstance(target, dict) or head not in target:
        return

    if not rest:
        del target[head]
        return

    next_target = target[head]
    if rest == ["*"]:
        if isinstance(next_target, dict):
            target[head] = {}  # keep the parent key but empty its contents
        return
    _strip_path(next_target, tuple(rest))


def sanitize_blocks_dir(blocks_dir: Path, mode: str = "private") -> list[Path]:
    """Replace sensitive _blocks/*.md files with a redaction notice in public mode.

    Operates in place on the directory; returns the list of files rewritten
    so callers can log or restore them. In private mode this is a no-op
    (returns []).
    """
    if mode == "private":
        return []
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid mode {mode!r}. Expected one of {sorted(_VALID_MODES)}."
        )
    if not blocks_dir.is_dir():
        return []

    rewritten: list[Path] = []
    for entry in sorted(blocks_dir.iterdir()):
        if entry.name in SENSITIVE_BLOCK_FILES and entry.is_file():
            entry.write_text(_PUBLIC_REDACTION_BLOCK)
            rewritten.append(entry)
    return rewritten


# ---------------------------------------------------------------------------
# Site rendering (stub — quarto drives the actual render)
# ---------------------------------------------------------------------------


def render_site(
    root: Path,
    mode: str = "private",
    output_dir: Path | None = None,
    runner: "subprocess.CompletedProcess | None" = None,  # type: ignore[name-defined]  # noqa: F821
) -> Path:
    """Render the Quarto site with the given privacy mode.

    Pipeline:

    1. Resolve the output directory (``_site/`` for private,
       ``_site-public/`` for public unless *output_dir* overrides).
    2. Verify ``quarto`` is on PATH.
    3. Run ``jobsmith.assemble.assemble_all`` against
       ``<root>/private/applications/`` so every app's ``_variables.yml``
       and ``_blocks/*.md`` are fresh.
    4. **Public mode only** — for every assembled app, rewrite the per-app
       ``_variables.yml`` through ``sanitize_variables`` and overwrite
       sensitive ``_blocks/*.md`` with a redaction notice. The original
       contents are kept in memory and restored on exit so private state
       is never lost.
    5. Invoke ``quarto render <root> --output-dir <output_dir>``. The site
       project's ``_quarto.yml`` only renders the listings page; per-app
       pages must be rendered separately by the user with
       ``quarto render private/applications/<slug>``.
    6. Restore the original variables and block files (public mode).

    Args:
        root: Root of the Quarto site project (the directory containing the
            site-level ``_quarto.yml`` and ``index.qmd``).
        mode: 'private' or 'public'. Public strips sensitive variables and
            block files before render, restores them after.
        output_dir: Override the default output directory.

    Returns:
        The resolved output directory path.

    Raises:
        ValueError: If *mode* is invalid.
        FileNotFoundError: If *root* / its ``_quarto.yml`` is missing.
        RuntimeError: If ``quarto`` is missing or the render exits non-zero.
    """
    if mode not in _VALID_MODES:
        raise ValueError(
            f"Invalid mode {mode!r}. Expected one of {sorted(_VALID_MODES)}."
        )

    root = root.resolve()
    if not (root / "_quarto.yml").is_file():
        raise FileNotFoundError(
            f"No _quarto.yml at {root}. Run `jobsmith site init` first."
        )

    if output_dir is None:
        output_dir = root / ("_site" if mode == "private" else "_site-public")

    quarto = shutil.which("quarto")
    if quarto is None:
        raise RuntimeError(
            "quarto is not available on PATH. "
            "Install Quarto (https://quarto.org/docs/get-started/) and re-run. "
            f"Resolved output dir would have been: {output_dir}"
        )

    # Step 3: refresh per-app artifacts. Imported lazily to avoid a circular
    # import at module load time (assemble doesn't depend on site).
    from .assemble import assemble_all

    apps_root = root / "private" / "applications"
    if apps_root.is_dir():
        assemble_all(apps_root)

    # Step 4: public-mode sanitization snapshot.
    snapshot: list[tuple[Path, str, dict[Path, str]]] = []
    if mode == "public":
        snapshot = _snapshot_and_sanitize(apps_root)

    # Step 5: invoke quarto. 10-minute hard cap so a hung render never
    # leaves snapshot state stripped on disk indefinitely (the finally
    # restores either way).
    import subprocess

    cmd = [quarto, "render", str(root), "--output-dir", str(output_dir)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired as exc:
        # Restore happens in the finally below; re-raise as RuntimeError so
        # callers handle it the same way as a non-zero exit.
        raise RuntimeError(
            f"`quarto render` exceeded {exc.timeout}s timeout. "
            "Check for hung subprocess or split the project."
        ) from exc
    finally:
        # Step 6: ALWAYS restore, even if quarto fails — never leave the
        # private state corrupted by a failed public render.
        if snapshot:
            _restore_snapshot(snapshot)

    if result.returncode != 0:
        raise RuntimeError(
            f"`quarto render` exited {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    return output_dir


def _snapshot_and_sanitize(
    apps_root: Path,
) -> list[tuple[Path, str | None, dict[Path, str]]]:
    """Snapshot every app's _variables.yml + sensitive blocks, then rewrite
    them with sanitized content. Returned snapshot is consumed by
    _restore_snapshot to undo the public-mode mutations.

    Each entry: ``(variables_yml_path, original_yaml_text_or_None, {block_path: original_text})``.
    ``original_yaml_text`` is ``None`` when the file did not exist before the
    sanitize pass and an empty string when it existed but was empty — the
    distinction matters so restore knows whether to write or skip.
    """
    import yaml as _yaml

    snapshot: list[tuple[Path, str | None, dict[Path, str]]] = []
    if not apps_root.is_dir():
        return snapshot

    for app_dir in sorted(apps_root.iterdir()):
        if not app_dir.is_dir():
            continue
        if app_dir.name.startswith("_") or app_dir.name.startswith("."):
            continue

        vars_path = app_dir / "_variables.yml"
        blocks_dir = app_dir / "_blocks"

        original_vars: str | None = (
            vars_path.read_text() if vars_path.is_file() else None
        )
        original_blocks: dict[Path, str] = {}
        if blocks_dir.is_dir():
            for block in sorted(blocks_dir.iterdir()):
                if block.name in SENSITIVE_BLOCK_FILES and block.is_file():
                    original_blocks[block] = block.read_text()

        snapshot.append((vars_path, original_vars, original_blocks))

        if original_vars is not None and original_vars.strip():
            try:
                loaded = _yaml.safe_load(original_vars) or {}
            except _yaml.YAMLError:
                loaded = {}
            if isinstance(loaded, dict):
                sanitized = sanitize_variables(loaded, mode="public")
                vars_path.write_text(
                    _yaml.safe_dump(sanitized, sort_keys=False, allow_unicode=True)
                )

        sanitize_blocks_dir(blocks_dir, mode="public")

    return snapshot


def _restore_snapshot(
    snapshot: list[tuple[Path, str | None, dict[Path, str]]],
) -> None:
    """Undo the public-mode mutations performed by _snapshot_and_sanitize.

    Writes the original ``_variables.yml`` whenever the snapshot recorded
    one (including a present-but-empty file — distinguished from absent via
    ``None``). Block files are always restored verbatim.
    """
    for vars_path, original_vars, original_blocks in snapshot:
        if original_vars is not None:
            vars_path.write_text(original_vars)
        for block_path, original_text in original_blocks.items():
            block_path.write_text(original_text)


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
