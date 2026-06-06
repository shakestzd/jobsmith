"""Tests for jobsmith.research — company-research cache helpers."""

from __future__ import annotations

import time
from pathlib import Path

from jobsmith.research import cache_path_for, is_fresh, slugify

# ---------- slugify ----------


def test_slugify_basic() -> None:
    assert slugify("Acme Corp") == "acme-corp"


def test_slugify_special_chars() -> None:
    assert slugify("PwC") == "pwc"
    assert slugify("Google") == "google"
    assert slugify("Microsoft Corp.") == "microsoft-corp"


def test_slugify_multiple_spaces() -> None:
    assert slugify("Foo  Bar") == "foo-bar"


def test_slugify_leading_trailing() -> None:
    assert slugify("  Netflix  ") == "netflix"


def test_slugify_ampersand() -> None:
    # Non-word characters stripped, spaces become hyphens
    result = slugify("Smith & Wesson")
    assert result == "smith-wesson"


# ---------- is_fresh ----------


def test_cache_hit_within_window(tmp_path: Path) -> None:
    """A cache file with mtime 5 days ago is fresh with a 30-day window."""
    cache_file = tmp_path / "acme-corp.md"
    cache_file.write_text("# Company Research")
    # Backdate mtime by 5 days
    five_days_ago = time.time() - 5 * 86400
    import os
    os.utime(cache_file, (five_days_ago, five_days_ago))

    assert is_fresh(cache_file, window_days=30) is True


def test_cache_miss_outside_window(tmp_path: Path) -> None:
    """A cache file with mtime 45 days ago is stale with a 30-day window."""
    cache_file = tmp_path / "acme-corp.md"
    cache_file.write_text("# Company Research")
    forty_five_days_ago = time.time() - 45 * 86400
    import os
    os.utime(cache_file, (forty_five_days_ago, forty_five_days_ago))

    assert is_fresh(cache_file, window_days=30) is False


def test_cache_miss_no_file(tmp_path: Path) -> None:
    """A nonexistent path is not fresh."""
    nonexistent = tmp_path / "does-not-exist.md"
    assert is_fresh(nonexistent, window_days=30) is False


def test_is_fresh_default_window(tmp_path: Path) -> None:
    """Default window is 30 days; a brand-new file is always fresh."""
    cache_file = tmp_path / "new.md"
    cache_file.write_text("fresh")
    assert is_fresh(cache_file) is True


# ---------- cache_path_for ----------


def test_cache_path_for_returns_correct_path(tmp_path: Path) -> None:
    """cache_path_for constructs private/companies/<slug>.md under the given root."""
    path = cache_path_for("Acme Corp", repo_root=tmp_path)
    assert path == tmp_path / "private" / "companies" / "acme-corp.md"


def test_cache_path_for_no_repo_root() -> None:
    """Without a repo_root, cache_path_for returns a Path under private/companies/."""
    path = cache_path_for("Google")
    assert path.name == "google.md"
    assert path.parent.name == "companies"
