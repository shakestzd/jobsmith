"""Graceful-fallback tests for jobsmith.jd.fetcher (feat-0c74180d, slice 4).

When Chromium is unavailable the Playwright fallback must NOT crash the scrape:
``fetch_jd`` returns the httpx result plus a structured, retryable
``browser_unavailable`` signal. No pytest-asyncio dependency — coroutines run
via ``asyncio.run`` (the suite sets --strict-markers).
"""

from __future__ import annotations

import asyncio

import pytest

from jobsmith.jd import fetcher
from jobsmith.jd.fetcher import FetchResult, fetch_jd

_GOOD_HTML = "Senior Engineer role. " + ("Responsibilities and requirements. " * 40)
_BLOCKED_HTML = "Just a moment... checking your browser before access."


def _run(coro):
    return asyncio.run(coro)


def test_httpx_fast_path_no_browser(monkeypatch):
    async def fake_httpx(url):  # noqa: ARG001
        return _GOOD_HTML

    monkeypatch.setattr(fetcher, "_fetch_with_httpx", fake_httpx)
    result = _run(fetch_jd("https://example.com/job"))
    assert isinstance(result, FetchResult)
    assert result.method == "httpx"
    assert result.browser_unavailable is False
    assert result.text == _GOOD_HTML


def test_browser_unavailable_returns_httpx_text(monkeypatch):
    """httpx looks blocked + Chromium missing → httpx text + retryable signal."""

    async def fake_httpx(url):  # noqa: ARG001
        return _BLOCKED_HTML

    async def fake_playwright(url):  # noqa: ARG001
        raise RuntimeError(
            "BrowserType.launch: Executable doesn't exist at "
            "/data/ms-playwright/chromium-1097/chrome-linux/chrome. "
            "Run `playwright install`."
        )

    monkeypatch.setattr(fetcher, "_fetch_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_fetch_with_playwright", fake_playwright)

    result = _run(fetch_jd("https://example.com/job"))
    assert result.browser_unavailable is True
    assert result.method == "httpx"
    assert result.text == _BLOCKED_HTML
    assert result.detail and "Install the desktop browser" in result.detail


def test_browser_unavailable_via_import_error(monkeypatch):
    """A missing playwright package counts as browser-unavailable, not a crash."""

    async def fake_httpx(url):  # noqa: ARG001
        return _BLOCKED_HTML

    async def fake_playwright(url):  # noqa: ARG001
        raise ModuleNotFoundError("No module named 'playwright'")

    monkeypatch.setattr(fetcher, "_fetch_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_fetch_with_playwright", fake_playwright)

    result = _run(fetch_jd("https://example.com/job"))
    assert result.browser_unavailable is True
    assert result.method == "httpx"


def test_browser_unavailable_and_httpx_failed(monkeypatch):
    """Both transports down → still a structured result (no exception)."""

    async def fake_httpx(url):  # noqa: ARG001
        raise OSError("network is unreachable")

    async def fake_playwright(url):  # noqa: ARG001
        raise FileNotFoundError("chromium executable missing")

    monkeypatch.setattr(fetcher, "_fetch_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_fetch_with_playwright", fake_playwright)

    result = _run(fetch_jd("https://example.com/job"))
    assert result.browser_unavailable is True
    assert result.text == ""
    assert result.detail and "network is unreachable" in result.detail


def test_bot_wall_after_playwright_still_raises(monkeypatch):
    """Playwright launched but content is still blocked → hard RuntimeError."""

    async def fake_httpx(url):  # noqa: ARG001
        return _BLOCKED_HTML

    async def fake_playwright(url):  # noqa: ARG001
        return _BLOCKED_HTML

    monkeypatch.setattr(fetcher, "_fetch_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_fetch_with_playwright", fake_playwright)

    with pytest.raises(RuntimeError, match="still looks bot-blocked"):
        _run(fetch_jd("https://example.com/job"))


def test_genuine_playwright_error_still_raises(monkeypatch):
    """A non-environment Playwright error remains a hard failure (502 upstream)."""

    async def fake_httpx(url):  # noqa: ARG001
        return _BLOCKED_HTML

    async def fake_playwright(url):  # noqa: ARG001
        raise RuntimeError("Timeout 30000ms exceeded navigating to URL")

    monkeypatch.setattr(fetcher, "_fetch_with_httpx", fake_httpx)
    monkeypatch.setattr(fetcher, "_fetch_with_playwright", fake_playwright)

    with pytest.raises(RuntimeError, match="Playwright fetch failed"):
        _run(fetch_jd("https://example.com/job"))
