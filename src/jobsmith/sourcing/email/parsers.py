"""Template-based per-sender HTML parsers for email job alerts (feat-b1bd050e).

Each parse_* function accepts raw HTML (str) and returns a list of dicts with
keys: title, company, location, url, external_id.

An unparseable alert returns an empty list rather than raising — the caller is
responsible for recording DEGRADED in the sourcing_run record.

Supported senders:
  - linkedin-alert (LinkedIn Jobs Alert emails)
  - indeed-alert   (Indeed Job Alert emails)
  - glassdoor-alert (Glassdoor Job Alert emails)
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

logger = logging.getLogger("jobsmith.sourcing.email.parsers")


def _clean(text: str | None) -> str:
    """Strip whitespace from text; return '' if None."""
    if not text:
        return ""
    return " ".join(text.split())


def _linkedin_job_id(url: str) -> str:
    """Extract the LinkedIn job ID from a LinkedIn job URL."""
    # /jobs/view/3001000001/  or  /jobs/view/3001000001?trk=...
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _indeed_job_id(url: str) -> str:
    """Extract job key from Indeed URL query param ?jk=..."""
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    jk = qs.get("jk", [None])[0]
    if jk:
        return jk
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _glassdoor_job_id(url: str) -> str:
    """Extract job listing ID from Glassdoor URL param ?jl=..."""
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    jl = qs.get("jl", [None])[0]
    if jl:
        return jl
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def parse_linkedin_alert(html: str) -> list[dict]:
    """Parse a LinkedIn Jobs Alert HTML email into job entries.

    Returns a list of dicts with keys: title, company, location, url, external_id.
    Returns [] on parse failure (never raises).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # LinkedIn alert emails: anchors pointing to /jobs/view/<id>/
        job_links = soup.find_all(
            "a", href=re.compile(r"linkedin\.com/jobs/view/\d+")
        )

        for link in job_links:
            url = link.get("href", "").split("?")[0]  # strip tracking params
            title = _clean(link.get_text())
            if not title or not url:
                continue

            # Company and location are siblings after the link in the same cell
            parent = link.parent
            texts = [
                _clean(t) for t in parent.stripped_strings if _clean(t) != title
            ]
            company = texts[0] if len(texts) > 0 else ""
            location = texts[1] if len(texts) > 1 else ""

            external_id = _linkedin_job_id(url)
            results.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "external_id": external_id,
                }
            )

        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_linkedin_alert failed: %s", exc)
        return []


def parse_indeed_alert(html: str) -> list[dict]:
    """Parse an Indeed Job Alert HTML email into job entries.

    Returns a list of dicts with keys: title, company, location, url, external_id.
    Returns [] on parse failure (never raises).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Indeed alert emails: anchors with href containing /viewjob?jk=
        job_links = soup.find_all(
            "a", href=re.compile(r"indeed\.com/viewjob")
        )

        for link in job_links:
            url = link.get("href", "")
            title = _clean(link.get_text())
            if not title or not url:
                continue

            # Company and location are siblings in the same td
            parent = link.find_parent("td")
            if parent is None:
                parent = link.parent

            company_tag = parent.find(class_="company")
            location_tag = parent.find(class_="location")

            company = _clean(company_tag.get_text()) if company_tag else ""
            location = _clean(location_tag.get_text()) if location_tag else ""

            external_id = _indeed_job_id(url)
            results.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "external_id": external_id,
                }
            )

        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_indeed_alert failed: %s", exc)
        return []


def parse_glassdoor_alert(html: str) -> list[dict]:
    """Parse a Glassdoor Job Alert HTML email into job entries.

    Returns a list of dicts with keys: title, company, location, url, external_id.
    Returns [] on parse failure (never raises).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Glassdoor alert emails: anchors in .jobTitle divs with glassdoor.com/job-listing
        job_links = soup.find_all(
            "a", href=re.compile(r"glassdoor\.com/job-listing")
        )

        for link in job_links:
            url = link.get("href", "")
            title = _clean(link.get_text())
            if not title or not url:
                continue

            parent_item = link.find_parent("div", class_="jobItem")
            if parent_item:
                employer_tag = parent_item.find(class_="employer")
                loc_tag = parent_item.find(class_="loc")
                company = _clean(employer_tag.get_text()) if employer_tag else ""
                location = _clean(loc_tag.get_text()) if loc_tag else ""
            else:
                company = ""
                location = ""

            external_id = _glassdoor_job_id(url)
            results.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": url,
                    "external_id": external_id,
                }
            )

        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("parse_glassdoor_alert failed: %s", exc)
        return []


# Registry: sender-name -> parse function
SENDER_PARSERS: dict[str, object] = {
    "linkedin-alert": parse_linkedin_alert,
    "indeed-alert": parse_indeed_alert,
    "glassdoor-alert": parse_glassdoor_alert,
}


def parse_alert_html(sender: str, html: str) -> list[dict]:
    """Dispatch HTML to the right parser based on the sender slug.

    Returns [] when the sender is unknown or parsing fails.
    """
    parser = SENDER_PARSERS.get(sender)
    if parser is None:
        logger.warning("no parser for sender %r — recording as degraded", sender)
        return []
    return parser(html)  # type: ignore[operator]
