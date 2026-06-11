"""Ashby job board adapter (feat-5531c54b).

Ashby's public board API lives at
    https://api.ashbyhq.com/posting-api/job-board/{slug}
and is a **GET** endpoint (no request body). POST returns 401 for every
board — verified live 2026-04-14 against linear, vercel, replit, retool.

Returns JSON with a `jobs` array. No auth required.

Ported from shakestzd/private/scripts/sources/ashby.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
from bs4 import BeautifulSoup

from .base import ATSSourceAdapter, Role, SourceFetchError

logger = logging.getLogger("jobsmith.sourcing.adapters.ashby")

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def parse_ashby_payload(
    payload: dict,
    source_slug: str,
    company_name: str | None = None,
) -> Iterable[Role]:
    jobs = payload.get("jobs") or []
    canonical = company_name or source_slug.title()
    for j in jobs:
        try:
            job_id = str(j.get("id", ""))
            title = (j.get("title") or "").strip()
            url = (j.get("jobUrl") or j.get("applyUrl") or "").strip()
            # Ashby live API uses `location` (string) as the primary field;
            # `locationName` is present in some board responses as a fallback.
            primary_loc = (j.get("location") or j.get("locationName") or "").strip()
            # secondaryLocations is a list of dicts like {"location": "Europe", ...}
            secondary: list[str] = [
                str(sec.get("location") or sec.get("locationName") or "").strip()
                for sec in (j.get("secondaryLocations") or [])
                if isinstance(sec, dict) and (sec.get("location") or sec.get("locationName"))
            ]
            if secondary:
                location = "; ".join([primary_loc] + secondary) if primary_loc else "; ".join(secondary)
            else:
                location = primary_loc
            jd_text = _strip_html(j.get("descriptionHtml") or j.get("description") or "")
            posted = (j.get("publishedAt") or "")[:10]
            yield Role(
                id=f"ashby:{source_slug}:{job_id}",
                source="ashby",
                source_slug=source_slug,
                company=canonical,
                title=title,
                location=location,
                url=url,
                jd_text=jd_text,
                posted_date=posted,
                raw_metadata={"job_id": job_id},
            )
        except Exception as exc:
            logger.warning("ashby parse error: %s", exc)
            continue


class AshbyAdapter(ATSSourceAdapter):
    name = "ashby"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        company_name: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.company_name = company_name

    def fetch(self, slug: str) -> Iterable[Role]:
        url = f"{API_BASE}/{slug}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                # GET, not POST. Ashby's public API rejects POST with 401.
                resp = client.get(url)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ashby fetch failed for %s: %s", slug, exc)
            raise SourceFetchError(f"ashby/{slug}: {exc}") from exc
        return parse_ashby_payload(
            payload,
            source_slug=slug,
            company_name=self.company_name,
        )
