"""NewsSpider — concurrent URL fetcher with anti-bot bypass and domain rate-limiting.

Uses Scrapling's FetcherSession (lightweight) and AsyncStealthySession (browser)
internally. Provides a Spider-like interface with:

- ``concurrent_requests=10`` — global concurrency cap
- ``concurrent_requests_per_domain=2`` — per-domain cap
- Two session tiers: "fast" (FetcherSession) and "stealth" (AsyncStealthySession with
  ``solve_cloudflare=True``)
- Auto-retry with stealth fallback when the fast session gets blocked (HTTP 403/429/503)

Usage::

    results = await fetch_urls_with_spider(
        urls=["https://example.com/a", "https://example.com/b"],
        timeout_ms=30000,
    )
    for url, response in results:
        print(url, response.status, len(response.html_content or ""))
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


# ── Configuration ──────────────────────────────────────────────────────

DEFAULT_CONCURRENT_REQUESTS: int = 10
"""Global maximum concurrent fetches."""

DEFAULT_CONCURRENT_PER_DOMAIN: int = 2
"""Maximum concurrent fetches per domain."""

DEFAULT_TIMEOUT_MS: int = 30000
"""Default request timeout in milliseconds."""

STEALTH_SESSION_TIMEOUT_MS: int = 45000
"""Timeout for stealth (browser) sessions — longer because of browser startup."""

BLOCKED_STATUS_CODES: frozenset[int] = frozenset({403, 429, 503})
"""HTTP status codes that trigger an automatic stealth retry."""

BATCH_SIZE: int = 50
"""Number of URLs to process in one batch."""


# ── Data classes ──────────────────────────────────────────────────────


@dataclass
class SpiderResult:
    """Result of fetching a single URL through NewsSpider.

    Attributes:
        url: The requested URL.
        status: HTTP status code (0 if fetch failed entirely).
        html_content: Raw HTML string from the page (or empty).
        error: Human-readable error message if the request failed.
        used_stealth: Whether the request was retried via stealth session.
    """

    url: str
    status: int = 0
    html_content: str = ""
    error: str | None = None
    used_stealth: bool = False


# ── Session wrappers ──────────────────────────────────────────────────


class _FastSessionPool:
    """Pool of lightweight FetcherSession instances for fast fetches."""

    def __init__(self, size: int = 3, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self._size = size
        self._timeout_s = timeout_ms / 1000.0
        self._sessions: list[Any] = []

    async def start(self) -> None:
        from scrapling.fetchers import FetcherSession

        self._sessions = [
            FetcherSession(impersonate="chrome", timeout=self._timeout_s)
            for _ in range(self._size)
        ]

    async def close(self) -> None:
        self._sessions.clear()

    async def fetch(self, url: str) -> SpiderResult:
        """Fetch URL using a randomly-chosen fast session."""
        session = self._sessions[id(url) % len(self._sessions)]
        try:
            resp = session.get(url)
            return SpiderResult(
                url=url,
                status=resp.status,
                html_content=resp.html_content or "",
            )
        except Exception as exc:
            return SpiderResult(
                url=url,
                status=0,
                error=f"FastSession error: {exc}",
            )


class _StealthSessionWrapper:
    """Wrapper around AsyncStealthySession with Cloudflare bypass.

    Lazily creates the browser session on first use (saves ~3s startup
    if no URLs need stealth).
    """

    def __init__(self, timeout_ms: int = STEALTH_SESSION_TIMEOUT_MS) -> None:
        self._timeout_ms = timeout_ms
        self._session: Any = None
        self._started: bool = False

    async def start(self) -> None:
        """Create the AsyncStealthySession instance (lazy, called on first use)."""
        if self._started:
            return
        from scrapling.fetchers import AsyncStealthySession

        self._session = AsyncStealthySession(
            max_pages=5,
            headless=True,
            solve_cloudflare=True,
            humanize=True,
            timeout=self._timeout_ms,
        )
        self._started = True
        logger.debug("Stealth session initialized (solve_cloudflare=True)")

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None
        self._started = False

    async def fetch(self, url: str) -> SpiderResult:
        """Fetch URL via stealth browser session."""
        try:
            await self.start()
            resp = await self._session.fetch(url)
            return SpiderResult(
                url=url,
                status=resp.status,
                html_content=resp.html_content or "",
                used_stealth=True,
            )
        except Exception as exc:
            return SpiderResult(
                url=url,
                status=0,
                error=f"StealthSession error: {exc}",
                used_stealth=True,
            )


# ── NewsSpider ────────────────────────────────────────────────────────


class NewsSpider:
    """Concurrent URL spider with domain rate limiting and stealth fallback.

    Design mirrors Scrapling's ``Spider`` interface (planned for a future
    version) but implemented directly against the available fetchers.

    Flow::

        1. Try "fast" (FetcherSession — lightweight HTTP)
        2. On HTTP 403/429/503 → retry with "stealth" (AsyncStealthySession with CF bypass)
        3. Domain-level semaphore ensures at most ``concurrent_requests_per_domain``
           requests to the same domain run simultaneously.
    """

    def __init__(
        self,
        concurrent_requests: int = DEFAULT_CONCURRENT_REQUESTS,
        concurrent_per_domain: int = DEFAULT_CONCURRENT_PER_DOMAIN,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.concurrent_requests = concurrent_requests
        self.concurrent_per_domain = concurrent_per_domain
        self._timeout_ms = timeout_ms
        self._batch_size = batch_size

        # Internal
        self._global_sem: asyncio.Semaphore | None = None
        self._domain_sems: dict[str, asyncio.Semaphore] = {}
        self._domain_lock: asyncio.Lock | None = None
        self._fast_pool: _FastSessionPool | None = None
        self._stealth: _StealthSessionWrapper | None = None

    async def start(self) -> None:
        """Initialize sessions and semaphores."""
        self._global_sem = asyncio.Semaphore(self.concurrent_requests)
        self._domain_lock = asyncio.Lock()
        self._fast_pool = _FastSessionPool(timeout_ms=self._timeout_ms)
        self._stealth = _StealthSessionWrapper(
            timeout_ms=min(self._timeout_ms * 2, STEALTH_SESSION_TIMEOUT_MS),
        )
        await self._fast_pool.start()
        # Stealth session is lazy-started on first use

    async def close(self) -> None:
        """Clean up all sessions."""
        if self._fast_pool:
            await self._fast_pool.close()
        if self._stealth:
            await self._stealth.close()

    async def __aenter__(self) -> NewsSpider:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Per-domain semaphore helper ──────────────────────────────────

    async def _get_domain_sem(self, domain: str) -> asyncio.Semaphore:
        """Get (or create) the semaphore for a domain."""
        assert self._domain_lock is not None
        async with self._domain_lock:
            if domain not in self._domain_sems:
                self._domain_sems[domain] = asyncio.Semaphore(
                    self.concurrent_per_domain
                )
            return self._domain_sems[domain]

    # ── Single URL fetch with retry ──────────────────────────────────

    async def _fetch_one(self, url: str) -> SpiderResult:
        """Fetch a single URL: attempt fast → retry stealth if blocked.

        Args:
            url: The URL to fetch.

        Returns:
            SpiderResult with fetch outcome.
        """
        assert self._global_sem is not None
        assert self._fast_pool is not None
        assert self._stealth is not None

        # Domain extraction for rate limiting
        from urllib.parse import urlparse
        domain = urlparse(url).hostname or "unknown"

        domain_sem = await self._get_domain_sem(domain)
        async with domain_sem:
            async with self._global_sem:
                # Phase 1: fast session
                result = await self._fast_pool.fetch(url)

                # Phase 2: retry with stealth if blocked
                if result.status in BLOCKED_STATUS_CODES or (
                    result.status == 0 and result.error
                ):
                    logger.debug(
                        "Fast session blocked for %s (status=%s, error=%s) — "
                        "retrying with stealth",
                        url,
                        result.status,
                        result.error,
                    )
                    result = await self._stealth.fetch(url)

                return result

    # ── Batch fetch ──────────────────────────────────────────────────

    async def fetch_many(
        self, urls: list[str]
    ) -> list[SpiderResult]:
        """Fetch multiple URLs in batches with concurrency control.

        Args:
            urls: List of URLs to fetch.

        Returns:
            List of SpiderResult in the same order as input URLs.
        """
        results: list[SpiderResult] = []

        for batch_start in range(0, len(urls), self._batch_size):
            batch = urls[batch_start:batch_start + self._batch_size]
            batch_results = await asyncio.gather(
                *[self._fetch_one(url) for url in batch],
                return_exceptions=True,
            )
            for i, url in enumerate(batch):
                item = batch_results[i]
                if isinstance(item, SpiderResult):
                    results.append(item)
                elif isinstance(item, Exception):
                    results.append(
                        SpiderResult(
                            url=url,
                            status=0,
                            error=f"Unhandled exception: {item}",
                        )
                    )
                else:
                    results.append(
                        SpiderResult(url=url, status=0, error="Unknown error")
                    )

        return results


# ── Convenience function ──────────────────────────────────────────────


async def fetch_urls_with_spider(
    urls: list[str],
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    concurrent_requests: int = DEFAULT_CONCURRENT_REQUESTS,
    concurrent_per_domain: int = DEFAULT_CONCURRENT_PER_DOMAIN,
) -> list[SpiderResult]:
    """Convenience: create a NewsSpider, fetch URLs, return results.

    Args:
        urls: URLs to fetch.
        timeout_ms: Request timeout in milliseconds.
        concurrent_requests: Global concurrency cap.
        concurrent_per_domain: Per-domain concurrency cap.

    Returns:
        List of SpiderResult in input order.
    """
    spider = NewsSpider(
        concurrent_requests=concurrent_requests,
        concurrent_per_domain=concurrent_per_domain,
        timeout_ms=timeout_ms,
    )
    async with spider:
        return await spider.fetch_many(urls)


__all__ = [
    "NewsSpider",
    "SpiderResult",
    "fetch_urls_with_spider",
    "DEFAULT_CONCURRENT_REQUESTS",
    "DEFAULT_CONCURRENT_PER_DOMAIN",
    "DEFAULT_TIMEOUT_MS",
    "BLOCKED_STATUS_CODES",
    "BATCH_SIZE",
]
