"""jobsmith.core.slug — URL-to-slug derivation helpers (feat-55152c31, Slice 2).

Pure helpers with no Rich/Click/Typer dependencies. Relocated from apply.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _slugify_part(s: str) -> str:
    """Convert an arbitrary string into a lowercase hyphenated slug component.

    Lowercases, replaces non-alphanumeric runs with a single hyphen, and
    strips leading/trailing hyphens.  Used by both :func:`derive_slug` and
    :func:`reconcile_canonical_slug` so slug-cleaning logic stays DRY.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def derive_slug(url: str) -> str:
    """Derive an application slug from a JD URL.

    Sanitizes to lowercase, alphanumeric + hyphens, max 60 chars.
    Falls back to a 12-char URL hash if no useful path segment exists.

    Parameters
    ----------
    url:
        Job description URL (or any string identifier).

    Returns
    -------
    str
        A filesystem-safe slug string.
    """
    try:
        parsed = urlparse(url)
        # Use the last non-empty path segment
        path_parts = [p for p in parsed.path.split("/") if p]
        raw = path_parts[-1] if path_parts else ""
    except Exception:
        raw = ""

    if raw:
        # Encode non-ASCII bytes as their hex representation, then lowercase
        try:
            raw = raw.encode("ascii").decode("ascii")
        except (UnicodeEncodeError, UnicodeDecodeError):
            # Replace non-ASCII with hyphens
            raw = raw.encode("ascii", errors="replace").decode("ascii")

        slug = _slugify_part(raw)
    else:
        slug = ""

    # Fall back to URL hash if nothing useful remains
    if not slug:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        slug = digest

    # Enforce max 60 chars
    return slug[:60]


def reconcile_canonical_slug(
    active_slug: str, cwd: Path, started_at: float
) -> tuple[str, bool]:
    """Return ``(slug, reconciled)`` after an optional directory rename.

    After phase 1 (gather), the specialist ``apply-jd-parser`` derives a
    canonical slug of the form ``{company-slug}-{position-slug}`` and writes
    artifacts under *that* directory.  The wrapper may have pre-created a
    different directory from the raw URL.  This helper reconciles the two.

    The second return value, ``reconciled``, signals whether the slug was
    successfully derived from a ``jd-parsed.json`` (i.e. the helper produced
    a true canonical slug, not a fallback).  Callers MUST gate persistence
    of the URL → slug mapping on this flag — recording a non-canonical slug
    would corrupt future resume lookups.

    Strategy
    --------
    1. Try ``applications/{active_slug}/.apply-state/jd-parsed.json``.
    2. Fallback: glob ``applications/*/.apply-state/jd-parsed.json`` and pick
       the most recently modified candidate **whose mtime is >= started_at**
       (produced during this run).  Stale prior-run artifacts are skipped to
       prevent picking up an unrelated job's slug.
    3. Compute canonical = ``_slugify_part(company) + "-" + _slugify_part(position)``.
    4. If the *owning* directory's name differs from canonical, rename it.
       If the canonical directory already exists and is non-empty, halt with
       a controlled error and return ``(active_slug, False)`` — the user
       must resolve the collision manually, and the index must NOT be
       updated to point at the active (non-canonical) slug.
    5. Return ``(canonical, True)``.

    When no qualifying ``jd-parsed.json`` is found, returns
    ``(active_slug, False)`` and logs a warning.
    """
    from jobsmith.config import find_config, load_config
    from jobsmith.paths import resolve

    config_path = find_config(cwd)
    if config_path is None:
        logger.warning(
            "reconcile: cannot locate .apply-config.yaml — skipping slug reconciliation."
        )
        return active_slug, False

    config = load_config(config_path)
    repo_root = config_path.parent
    apps_dir = resolve(config.output.applications_dir, repo_root)

    # 1. Primary: check the active slug's apply-state
    primary = apps_dir / active_slug / ".apply-state" / "jd-parsed.json"
    if primary.exists():
        jd_path = primary
        owning_dir = apps_dir / active_slug
    else:
        # 2. Fallback: candidates produced during this run only
        # (mtime >= started_at, with a 1s buffer for filesystem clock drift).
        threshold = started_at - 1.0
        candidates = [
            p
            for p in apps_dir.glob("*/.apply-state/jd-parsed.json")
            if p.stat().st_mtime >= threshold
        ]
        if not candidates:
            logger.warning(
                "reconcile: no jd-parsed.json produced in this run — cannot derive canonical slug."
            )
            return active_slug, False
        jd_path = max(candidates, key=lambda p: p.stat().st_mtime)
        owning_dir = jd_path.parent.parent  # strip "/.apply-state/jd-parsed.json"

    # 3. Derive canonical slug from company + position fields
    try:
        jd_data = json.loads(jd_path.read_text())
        company = jd_data.get("company", "")
        position = jd_data.get("position", "")
    except Exception as exc:
        logger.warning("reconcile: failed to parse jd-parsed.json (%s) — skipping.", exc)
        return active_slug, False

    if not company or not position:
        logger.warning("reconcile: jd-parsed.json missing company or position — skipping.")
        return active_slug, False

    canonical = f"{_slugify_part(company)}-{_slugify_part(position)}"

    # 4. Rename owning dir if it doesn't already have the canonical name.
    # shutil.move into an existing directory NESTS the source inside the
    # target rather than renaming, which would leave phase 2/3 reading stale
    # canonical artifacts. Detect collisions and refuse to merge.
    if owning_dir.name != canonical:
        canonical_dir = apps_dir / canonical
        if canonical_dir.exists():
            try:
                same = canonical_dir.resolve() == owning_dir.resolve()
            except OSError:
                same = False
            if same:
                pass
            elif any(canonical_dir.iterdir()):
                logger.warning(
                    "reconcile: canonical dir %s already exists and is non-empty; "
                    "refusing to merge with %r. Resolve manually and re-run.",
                    canonical_dir,
                    owning_dir.name,
                )
                return active_slug, False
            else:
                canonical_dir.rmdir()
                shutil.move(str(owning_dir), str(canonical_dir))
                logger.info("reconcile: renamed %r → %r", owning_dir.name, canonical)
        else:
            shutil.move(str(owning_dir), str(canonical_dir))
            logger.info("reconcile: renamed %r → %r", owning_dir.name, canonical)

    return canonical, True


def resolve_canonical_slug(url: str, cwd: Path) -> str:
    """Public accessor on top of :func:`resolve_starting_slug`.

    External callers (slice-4 NotebookRunner, slice-8 single-specialist
    re-runs) need the same canonical slug that ``run_phase_iter`` will use
    so DB rows, manifest resets, and post-phase ingestion all target the
    same application directory. Returns just the slug; the boolean
    "from_index" flag is an internal concern.
    """
    from jobsmith.core.url_index import resolve_starting_slug

    slug, _from_index = resolve_starting_slug(url, cwd)
    return slug
