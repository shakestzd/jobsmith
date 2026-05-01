"""Site rendering with privacy model.

Two modes:
- private (default): renders to _site/, includes everything (gitignored by the
  user's repo — see docs/architecture.md "Privacy model")
- public: renders to _site-public/, strips sensitive keys

Sensitive: salary, fit_score, must_have_table, bullet_decisions, bullet_diff,
gap_resolutions, hm_*, outreach_snippets, humanizer_audit.

CLI wiring is handled by feat-9377b64d (jobsmith site render / --public flag).
This module exposes the sanitization core so the CLI can import it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

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
