"""JD text fetching: httpx fast path → Playwright headless fallback."""
from __future__ import annotations

import logging
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


def _looks_blocked(text: str) -> bool:
    if len(text.strip()) < 300:
        return True
    lower = text.lower()
    return any(signal in lower for signal in _BOT_WALL_SIGNALS)


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


async def fetch_jd(url: str) -> tuple[str, FetchMethod]:
    """Return (text, method) — tries httpx then Playwright.

    Raises RuntimeError if both methods fail or content is still bot-blocked.
    """
    # Fast path
    try:
        text = await _fetch_with_httpx(url)
        if not _looks_blocked(text):
            logger.info("jd-fetch httpx ok (%d chars) for %s", len(text), url)
            return text, "httpx"
        logger.info("jd-fetch httpx content looks bot-blocked, trying playwright")
    except Exception as exc:
        logger.warning("jd-fetch httpx failed (%s), trying playwright", exc)

    # Playwright fallback
    try:
        text = await _fetch_with_playwright(url)
        if _looks_blocked(text):
            raise RuntimeError("Playwright content still looks bot-blocked")
        logger.info("jd-fetch playwright ok (%d chars) for %s", len(text), url)
        return text, "playwright"
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Playwright fetch failed: {exc}") from exc
