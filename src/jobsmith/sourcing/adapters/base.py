"""Abstract base class + shared Role dataclass for ATS adapters (feat-5531c54b).

All adapters live under jobsmith/sourcing/adapters/ and implement
ATSSourceAdapter. Role is defined here (not in runner.py) so that
adapters and the orchestrator can both import it without a circular
dependency.

Ported from shakestzd/private/scripts/sources/base.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field


@dataclass
class Role:
    """Common normalized role shape used by adapters and the orchestrator."""

    id: str
    source: str
    source_slug: str
    company: str
    title: str
    location: str
    url: str
    jd_text: str
    posted_date: str = ""
    raw_metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class SourceFetchError(Exception):
    """Raised by adapters when an HTTP/JSON fetch fails.

    The orchestrator's per-source error handler catches this (via the
    broad ``except Exception`` block in run_crawl) and increments
    ``error_counts`` / ``degraded_sources`` accordingly. Raising instead
    of returning an empty iterator ensures that a failed source is never
    silently treated as a zero-role success.
    """


class ATSSourceAdapter(ABC):
    """Adapter contract — implement once per ATS family.

    Adapters MUST raise SourceFetchError on HTTP/JSON failures so the
    orchestrator's circuit breaker can account for them. The
    crawler's per-source ``except Exception`` block catches the error,
    increments error_counts, and appends to degraded_sources after
    CIRCUIT_BREAKER_THRESHOLD consecutive failures.

    Subclasses should accept an optional `company_name` in __init__
    so the crawler can pass the canonical name from sourcing.yaml.
    Without it, parse_*_payload falls back to `slug.title()` which
    produces non-canonical spellings ("Oscarhealth", "Dagsterlabs")
    and breaks the rejection filter's exact company match.
    """

    name: str = ""  # override in subclass

    @abstractmethod
    def fetch(self, slug: str) -> Iterable[Role]:
        raise NotImplementedError
