"""Sourcing configuration loader (feat-5531c54b).

Loads and validates sourcing.yaml from the user's repo root.  Ships with
package-level defaults (empty sources list) so the package works before the
user creates their sourcing.yaml.

Config split (design decision A4):
  - Package ships with default_sourcing_config() (empty sources list, sensible
    defaults for expiry_days, circuit_breaker_threshold, etc.).
  - User's sourcing list lives in REPO_ROOT/sourcing.yaml next to
    .apply-config.yaml, resolved via the standard JOBSMITH_REPO_ROOT /
    walk-up / settings.toml chain.

Expected sourcing.yaml schema
-------------------------------
expiry_days: 21          # default: 21 — postings not re-sighted expire after N days
max_per_source: 100      # default: 100
global_timeout_sec: 300  # default: 300

sources:
  - type: greenhouse
    slug: stripe
    name: Stripe            # human name (shown in UI)
    company: Stripe         # canonical company name passed to adapter
    enabled: true           # default: true

  - type: lever
    slug: netflix
    name: Netflix
    company: Netflix

  - type: ashby
    slug: linear
    name: Linear
    company: Linear

  - type: hn_whos_hiring
    slug: "43022668"        # the HN thread ID for the current month's "Who is hiring?" post
    name: HN Who's Hiring
    enabled: false          # disable until thread ID is set

  - type: climatebase
    slug: "data engineer"   # search query
    name: Climatebase
    enabled: true
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .._sourcing_config import _resolve_repo_root_best_effort

SOURCING_CONFIG_FILENAME = "sourcing.yaml"

# Package-level defaults — used when no sourcing.yaml is found.
_PACKAGE_DEFAULTS: dict = {
    "expiry_days": 21,
    "max_per_source": 100,
    "global_timeout_sec": 300,
    "rescore_n_cap": 30,
    "rescore_budget_usd": 1.0,
    "sources": [],
}


@dataclass
class SourcingConfig:
    """Typed representation of a loaded sourcing.yaml."""

    expiry_days: int = 21
    max_per_source: int = 100
    global_timeout_sec: int = 300
    # LLM triage rescore settings (feat-1602d64c)
    rescore_n_cap: int = 30         # top-N by fast_score to send to LLM
    rescore_budget_usd: float = 1.0  # soft USD cap for the rescore pass
    sources: list[dict] = field(default_factory=list)


def find_sourcing_config(start: Path | None = None) -> Path | None:
    """Locate sourcing.yaml by walking up from start (default: cwd)."""
    root = _resolve_repo_root_best_effort()
    if root is not None:
        candidate = root / SOURCING_CONFIG_FILENAME
        if candidate.exists():
            return candidate
    # Walk-up fallback from start
    cwd = start or Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / SOURCING_CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_sourcing_config(path: Path | None = None) -> SourcingConfig:
    """Load and parse sourcing.yaml from *path* (or auto-discover).

    Falls back to package defaults (empty sources list) when no file is found.
    """
    if path is None:
        path = find_sourcing_config()
    if path is None or not path.exists():
        return SourcingConfig(**_PACKAGE_DEFAULTS)

    raw = yaml.safe_load(path.read_text()) or {}

    # Merge defaults with file values
    expiry_days = int(raw.get("expiry_days", _PACKAGE_DEFAULTS["expiry_days"]))
    max_per_source = int(raw.get("max_per_source", _PACKAGE_DEFAULTS["max_per_source"]))
    global_timeout_sec = int(
        raw.get("global_timeout_sec", _PACKAGE_DEFAULTS["global_timeout_sec"])
    )
    rescore_n_cap = int(raw.get("rescore_n_cap", _PACKAGE_DEFAULTS["rescore_n_cap"]))
    rescore_budget_usd = float(
        raw.get("rescore_budget_usd", _PACKAGE_DEFAULTS["rescore_budget_usd"])
    )
    sources: list[dict] = []
    for spec in raw.get("sources") or []:
        if not isinstance(spec, dict):
            continue
        if spec.get("enabled", True) is False:
            continue
        sources.append(spec)

    return SourcingConfig(
        expiry_days=expiry_days,
        max_per_source=max_per_source,
        global_timeout_sec=global_timeout_sec,
        rescore_n_cap=rescore_n_cap,
        rescore_budget_usd=rescore_budget_usd,
        sources=sources,
    )


def default_sourcing_config() -> SourcingConfig:
    """Return the package-level defaults (no sources, sensible timeouts)."""
    return SourcingConfig(**_PACKAGE_DEFAULTS)
