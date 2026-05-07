"""jobsmith.core.url_index — URL → canonical-slug index helpers (feat-55152c31, Slice 2e).

Pure helpers with no Rich/Click/Typer dependencies. Relocated from apply.py.
"""
from __future__ import annotations

import json
from pathlib import Path

URL_INDEX_FILENAME = ".url-index.json"


def _url_index_path(cwd: Path) -> Path | None:
    """Return the absolute path of ``applications/.url-index.json``."""
    from jobsmith.core.paths import applications_dir

    apps_dir = applications_dir(cwd)
    if apps_dir is None:
        return None
    return apps_dir / URL_INDEX_FILENAME


def load_url_index(cwd: Path) -> dict[str, str]:
    """Read the URL → canonical-slug index. Returns ``{}`` on missing/malformed."""
    idx_path = _url_index_path(cwd)
    if idx_path is None or not idx_path.exists():
        return {}
    try:
        data = json.loads(idx_path.read_text())
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_url_index(cwd: Path, index: dict[str, str]) -> None:
    """Atomically write the URL → canonical-slug index."""
    idx_path = _url_index_path(cwd)
    if idx_path is None:
        return
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(idx_path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    tmp.replace(idx_path)


def scan_for_url_match(url: str, cwd: Path) -> str | None:
    """Scan ``applications/*/.apply-state/jd-parsed.json`` for one matching *url*.

    Checks ``jd_url``, ``url``, and ``apply_url`` fields in that order.  Returns
    the slug of the matching directory, or None if no candidate matches.
    """
    from jobsmith.core.paths import applications_dir

    apps_dir = applications_dir(cwd)
    if apps_dir is None or not apps_dir.exists():
        return None
    for jd_path in apps_dir.glob("*/.apply-state/jd-parsed.json"):
        try:
            data = json.loads(jd_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("jd_url", "url", "apply_url"):
            if data.get(key) == url:
                return jd_path.parent.parent.name
    return None


def resolve_starting_slug(url: str, cwd: Path) -> tuple[str, bool]:
    """Resolve which slug to start the run under.

    Returns ``(slug, from_index)`` where ``from_index`` is True iff the slug
    came from the persisted URL index or a one-time migration scan.  Falls
    back to the URL-derived slug when neither lookup succeeds.
    """
    from jobsmith.core.slug import derive_slug

    index = load_url_index(cwd)
    if url in index:
        return index[url], True
    # One-time migration: if the URL isn't in the index, scan jd-parsed.json
    # files under applications/* for a matching jd_url/url/apply_url field.
    scanned = scan_for_url_match(url, cwd)
    if scanned:
        index[url] = scanned
        save_url_index(cwd, index)
        return scanned, True
    return derive_slug(url), False


def record_url_mapping(url: str, canonical_slug: str, cwd: Path) -> None:
    """Persist URL → canonical slug into the index, creating it if absent."""
    index = load_url_index(cwd)
    if index.get(url) == canonical_slug:
        return
    index[url] = canonical_slug
    save_url_index(cwd, index)
