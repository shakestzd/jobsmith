"""JD text fetching: httpx fast path → Playwright headless fallback.

Graceful degradation (feat-0c74180d, slice 4)
---------------------------------------------
The Playwright fallback needs a Chromium binary that, on the desktop build, is
downloaded on first use. When Chromium is absent or cannot launch (e.g. offline
before the one-time download), the fallback MUST NOT hard-crash the scrape:
``fetch_jd`` returns whatever httpx produced plus a structured, retryable
``browser_unavailable`` signal so callers can prompt a browser install and
retry. A genuine bot-wall (Playwright launched but content is still blocked)
remains a hard ``RuntimeError`` — that is not retryable by installing Chromium.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

_BOT_WALL_SIGNALS = [
    "enable javascript",
    "checking your browser",
    "cf-browser-verification",
    "cloudflare",
    "please wait",
    "access denied",
    "403 forbidden",
    "robot",
    "captcha",
    "just a moment",
]

# Substrings that identify a Playwright/Chromium *environment* failure (browser
# missing, driver absent, failed to launch) — distinct from a navigation or
# bot-wall failure. Matched case-insensitively against the exception text.
_BROWSER_UNAVAILABLE_SIGNALS = (
    "executable doesn't exist",
    "executable does not exist",
    "playwright install",
    "looking for chromium",
    "host system is missing dependencies",
    "browsertype.launch",
    "no module named 'playwright'",
)

_REALISTIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

_BROWSER_UNAVAILABLE_DETAIL = (
    "Chromium is unavailable, so the JS-rendered fallback was skipped. "
    "Install the desktop browser and retry for full content."
)


def _looks_blocked(text: str) -> bool:
    if len(text.strip()) < 300:
        return True
    lower = text.lower()
    return any(signal in lower for signal in _BOT_WALL_SIGNALS)


def _is_browser_unavailable(exc: Exception) -> bool:
    """True when *exc* indicates Chromium/Playwright is missing or unlaunchable."""
    if isinstance(exc, (ImportError, ModuleNotFoundError, FileNotFoundError)):
        return True
    message = str(exc).lower()
    return any(signal in message for signal in _BROWSER_UNAVAILABLE_SIGNALS)


async def _fetch_with_httpx(url: str) -> str:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15.0,
        headers=_REALISTIC_HEADERS,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def _fetch_with_playwright(url: str) -> str:
    from playwright.async_api import async_playwright  # lazy import

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                user_agent=_REALISTIC_HEADERS["User-Agent"],
                locale="en-US",
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            text = await page.inner_text("body")
            return text
        finally:
            await browser.close()


FetchMethod = Literal["httpx", "playwright"]


@dataclass
class FetchResult:
    """Outcome of :func:`fetch_jd`.

    ``browser_unavailable`` is True when the Playwright/Chromium fallback could
    not run; ``detail`` carries a human-readable, retryable explanation. ``text``
    still holds whatever httpx produced (possibly empty / bot-blocked) so the
    caller degrades gracefully rather than crashing.
    """

    text: str
    method: FetchMethod
    browser_unavailable: bool = False
    detail: str | None = None


async def fetch_jd(url: str) -> FetchResult:
    """Return a :class:`FetchResult` — tries httpx then Playwright.

    Raises ``RuntimeError`` only when Playwright launched but the content is
    still bot-blocked (not retryable by installing Chromium). When Chromium is
    unavailable the result carries ``browser_unavailable=True`` instead.
    """
    httpx_text: str | None = None
    httpx_error: str | None = None

    # Fast path
    try:
        httpx_text = await _fetch_with_httpx(url)
        if not _looks_blocked(httpx_text):
            logger.info("jd-fetch httpx ok (%d chars) for %s", len(httpx_text), url)
            return FetchResult(text=httpx_text, method="httpx")
        logger.info("jd-fetch httpx content looks bot-blocked, trying playwright")
    except Exception as exc:
        httpx_error = str(exc)
        logger.warning("jd-fetch httpx failed (%s), trying playwright", exc)

    # Playwright fallback. A launch/navigation EXCEPTION is inspected for
    # browser-unavailability; a successful-but-blocked PAGE is a hard error
    # (raised outside the except so it is never mistaken for unavailability).
    try:
        text = await _fetch_with_playwright(url)
    except Exception as exc:
        if not _is_browser_unavailable(exc):
            raise RuntimeError(f"Playwright fetch failed: {exc}") from exc
        # Graceful degradation: Chromium absent / cannot launch. Never crash —
        # return whatever httpx produced with a structured retryable signal.
        if httpx_text is not None:
            logger.warning(
                "jd-fetch browser unavailable (%s); returning httpx text", exc
            )
            return FetchResult(
                text=httpx_text,
                method="httpx",
                browser_unavailable=True,
                detail=_BROWSER_UNAVAILABLE_DETAIL,
            )
        # httpx also failed → no text, but still surface a structured signal
        # rather than an opaque crash.
        logger.warning("jd-fetch browser unavailable (%s) and httpx failed", exc)
        detail = _BROWSER_UNAVAILABLE_DETAIL
        if httpx_error:
            detail = f"{detail} (initial fetch error: {httpx_error})"
        return FetchResult(
            text="",
            method="httpx",
            browser_unavailable=True,
            detail=detail,
        )

    # Playwright launched successfully.
    if _looks_blocked(text):
        # Not retryable by installing Chromium — a genuine bot wall.
        raise RuntimeError("Playwright content still looks bot-blocked")
    logger.info("jd-fetch playwright ok (%d chars) for %s", len(text), url)
    return FetchResult(text=text, method="playwright")
