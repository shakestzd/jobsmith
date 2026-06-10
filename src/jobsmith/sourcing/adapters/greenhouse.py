"""Greenhouse boards-api adapter (feat-5531c54b).

Public API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
With ?content=true the response includes JD body as escaped HTML.

Ported from shakestzd/private/scripts/sources/greenhouse.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
from bs4 import BeautifulSoup

from .base import ATSSourceAdapter, Role, SourceFetchError

logger = logging.getLogger("jobsmith.sourcing.adapters.greenhouse")

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def _strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text("\n", strip=True)


def parse_greenhouse_payload(
    payload: dict,
    source_slug: str,
    company_name: str | None = None,
) -> Iterable[Role]:
    """Convert a Greenhouse boards-api JSON response into Role iterables.

    `company_name` is the canonical name from sourcing.yaml (e.g.
    "Oscar Health", "Dagster Labs"). When None, falls back to
    `source_slug.title()` which often produces non-canonical spellings
    that break the rejection filter's exact-match.
    """
    jobs = payload.get("jobs") or []
    canonical = company_name or source_slug.title()
    for j in jobs:
        try:
            job_id = str(j.get("id", ""))
            title = (j.get("title") or "").strip()
            url = (j.get("absolute_url") or "").strip()
            location = ((j.get("location") or {}).get("name") or "").strip()
            jd_text = _strip_html(j.get("content") or "")
            posted = (j.get("updated_at") or "")[:10]  # YYYY-MM-DD slice
            yield Role(
                id=f"greenhouse:{source_slug}:{job_id}",
                source="greenhouse",
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
            logger.warning("greenhouse parse error: %s", exc)
            continue


class GreenhouseAdapter(ATSSourceAdapter):
    name = "greenhouse"

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        content: bool = True,
        company_name: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.content = content
        self.company_name = company_name

    def fetch(self, slug: str) -> Iterable[Role]:
        url = f"{API_BASE}/{slug}/jobs"
        params = {"content": "true"} if self.content else None
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("greenhouse fetch failed for %s: %s", slug, exc)
            raise SourceFetchError(f"greenhouse/{slug}: {exc}") from exc
        return parse_greenhouse_payload(
            payload,
            source_slug=slug,
            company_name=self.company_name,
        )
