"""Hacker News 'Who's Hiring' adapter (feat-5531c54b).

Algolia HN search API:
    https://hn.algolia.com/api/v1/search?tags=comment,story_{thread_id}

The slug for this adapter is the thread ID of the current month's
"Ask HN: Who is hiring?" post. Thread IDs change monthly — look up the
current one in your sourcing.yaml or leave the source disabled until
populated.

Ported from shakestzd/private/scripts/sources/hn_whos_hiring.py.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

import httpx
from bs4 import BeautifulSoup

from .base import ATSSourceAdapter, Role, SourceFetchError

logger = logging.getLogger("jobsmith.sourcing.adapters.hn")

API_URL = "https://hn.algolia.com/api/v1/search"

# Crude REMOTE | LOCATION | COMPANY | TITLE pattern matchers for HN's
# free-form comment text. Most "Who's Hiring" comments lead with company.
_TITLE_LINE_RE = re.compile(r"^([A-Z][A-Za-z0-9 .,&'-]{3,80}?)\s*[\|\-—]", re.MULTILINE)
_REMOTE_RE = re.compile(r"\bREMOTE\b", re.IGNORECASE)


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def parse_hn_payload(payload: dict, thread_id: str) -> Iterable[Role]:
    hits = payload.get("hits") or []
    for h in hits:
        try:
            comment_id = str(h.get("objectID", ""))
            html = h.get("comment_text") or ""
            text = _strip_html(html)
            if not text:
                continue
            first_line = text.split("\n", 1)[0]
            company = first_line.strip()[:80]
            location = "Remote" if _REMOTE_RE.search(text) else "Unknown"
            title = "(see comment)"
            yield Role(
                id=f"hn:{thread_id}:{comment_id}",
                source="hn_whos_hiring",
                source_slug=thread_id,
                company=company,
                title=title,
                location=location,
                url=f"https://news.ycombinator.com/item?id={comment_id}",
                jd_text=text,
                posted_date=(h.get("created_at") or "")[:10],
                raw_metadata={"comment_id": comment_id},
            )
        except Exception as exc:
            logger.warning("hn parse error: %s", exc)
            continue


class HNWhosHiringAdapter(ATSSourceAdapter):
    name = "hn_whos_hiring"

    def __init__(self, *, timeout: float = 10.0, hits_per_page: int = 100) -> None:
        self.timeout = timeout
        self.hits_per_page = hits_per_page

    def fetch(self, slug: str) -> Iterable[Role]:
        if not slug:
            logger.info("hn adapter: no thread_id slug — skipping")
            return iter(())
        params = {
            "tags": f"comment,story_{slug}",
            "hitsPerPage": self.hits_per_page,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(API_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("hn fetch failed for thread %s: %s", slug, exc)
            raise SourceFetchError(f"hn/{slug}: {exc}") from exc
        return parse_hn_payload(payload, thread_id=slug)
