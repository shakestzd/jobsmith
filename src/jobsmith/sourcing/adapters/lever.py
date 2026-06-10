"""Lever postings API adapter (feat-5531c54b).

Public API: https://api.lever.co/v0/postings/{slug}?mode=json
Returns a JSON array of postings.

Ported from shakestzd/private/scripts/sources/lever.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .base import ATSSourceAdapter, Role, SourceFetchError

logger = logging.getLogger("jobsmith.sourcing.adapters.lever")

API_BASE = "https://api.lever.co/v0/postings"


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def parse_lever_payload(
    payload,
    source_slug: str,
    company_name: str | None = None,
) -> Iterable[Role]:
    """Lever returns a top-level JSON array (not an object).

    `company_name` is the canonical name from sourcing.yaml. When
    None, falls back to `source_slug.title()`.
    """
    if not isinstance(payload, list):
        return
    canonical = company_name or source_slug.title()
    for j in payload:
        try:
            job_id = str(j.get("id", ""))
            title = (j.get("text") or "").strip()
            url = (j.get("hostedUrl") or "").strip()
            categories = j.get("categories") or {}
            location = (categories.get("location") or "").strip()
            # Prefer plain description, fall back to stripped HTML
            plain = (j.get("descriptionPlain") or "").strip()
            jd_text = plain or _strip_html(j.get("descriptionHtml") or "")
            posted_ms = j.get("createdAt")
            posted = ""
            if isinstance(posted_ms, (int, float)) and posted_ms > 0:
                posted = datetime.fromtimestamp(
                    posted_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            yield Role(
                id=f"lever:{source_slug}:{job_id}",
                source="lever",
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
            logger.warning("lever parse error: %s", exc)
            continue


class LeverAdapter(ATSSourceAdapter):
    name = "lever"

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
        params = {"mode": "json"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("lever fetch failed for %s: %s", slug, exc)
            raise SourceFetchError(f"lever/{slug}: {exc}") from exc
        return parse_lever_payload(
            payload,
            source_slug=slug,
            company_name=self.company_name,
        )
