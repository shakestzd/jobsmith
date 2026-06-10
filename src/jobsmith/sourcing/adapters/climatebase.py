"""Climatebase HTML scraper adapter (feat-5531c54b).

Climatebase does not expose a public API. We scrape the search results
page with BeautifulSoup. If the site structure changes the adapter logs
a warning and yields nothing — it never crashes the orchestrator.

The slug for this adapter is a search query string (e.g. "data engineer").

Ported from shakestzd/private/scripts/sources/climatebase.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
from bs4 import BeautifulSoup

from .base import ATSSourceAdapter, Role, SourceFetchError

logger = logging.getLogger("jobsmith.sourcing.adapters.climatebase")

BASE_URL = "https://climatebase.org/jobs"


def parse_climatebase_html(html: str, query: str) -> Iterable[Role]:
    """Best-effort parse. Selectors WILL drift — guard everything."""
    if not html:
        return
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("climatebase: html parse failed: %s", exc)
        return

    cards = soup.select("[data-testid='job-card'], a.job-card, li.job-listing")
    if not cards:
        logger.info("climatebase: no job cards found for query=%r", query)
        return

    for card in cards:
        try:
            title_el = card.select_one("h3, .job-title, [data-testid='job-title']")
            company_el = card.select_one(".company, [data-testid='company']")
            link_el = card if card.name == "a" else card.select_one("a[href]")
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else "Unknown"
            href = link_el.get("href") if link_el else ""
            if href and href.startswith("/"):
                href = "https://climatebase.org" + href
            yield Role(
                id=f"climatebase:{query}:{href}",
                source="climatebase",
                source_slug=query,
                company=company,
                title=title,
                location="Remote",  # climatebase filters often default to remote
                url=href or "",
                jd_text="",  # full JD requires per-role fetch; deferred
                posted_date="",
                raw_metadata={"query": query},
            )
        except Exception as exc:
            logger.warning("climatebase card parse error: %s", exc)
            continue


class ClimatebaseAdapter(ATSSourceAdapter):
    name = "climatebase"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def fetch(self, slug: str) -> Iterable[Role]:
        params = {"q": slug, "location": "remote"}
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                resp = client.get(BASE_URL, params=params)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("climatebase fetch failed for %s: %s", slug, exc)
            raise SourceFetchError(f"climatebase/{slug}: {exc}") from exc
        return parse_climatebase_html(resp.text, query=slug)
