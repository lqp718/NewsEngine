"""NewsSpider — concurrent URL fetcher with anti-bot bypass and domain rate-limiting.

Uses Scrapling's official ``scrapling.spiders.Spider`` base class with:

- ``FetcherSession(impersonate="chrome")`` for fast requests (sid="fast")
- ``AsyncStealthySession(solve_cloudflare=True, humanize=True)`` for blocked requests
  (sid="stealth", lazy=True)
- Automatic fallback via ``retry_blocked_request()`` — when the fast session gets
  blocked (HTTP 403/429/503), the request is automatically retried via the stealth
  session.

Usage::

    results = await fetch_urls_with_spider(
        urls=["https://example.com/a", "https://example.com/b"],
        timeout_ms=30000,
    )
    for url, response in results:
        print(url, response.status, len(response.html_content or ""))
"""

from __future__ import annotations

from typing import Any

from scrapling.fetchers import AsyncStealthySession, FetcherSession
from scrapling.spiders import Request, Response, Spider

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
"""Number of URLs to process in one batch (not used directly by Spider —
   spider handles concurrency natively, but kept for interface compatibility)."""


# ── Data classes ──────────────────────────────────────────────────────


class SpiderResult:
    """Result of fetching a single URL through NewsSpider.

    Attributes:
        url: The requested URL.
        status: HTTP status code (0 if fetch failed entirely).
        html_content: Raw HTML string from the page (or empty).
        error: Human-readable error message if the request failed.
        used_stealth: Whether the request was retried via stealth session.
    """

    __slots__ = ("url", "status", "html_content", "error", "used_stealth")

    def __init__(
        self,
        url: str,
        status: int = 0,
        html_content: str = "",
        error: str | None = None,
        used_stealth: bool = False,
    ) -> None:
        self.url = url
        self.status = status
        self.html_content = html_content
        self.error = error
        self.used_stealth = used_stealth

    def __repr__(self) -> str:
        parts = [f"url={self.url!r}", f"status={self.status}"]
        if self.error:
            parts.append(f"error={self.error!r}")
        if self.used_stealth:
            parts.append("used_stealth=True")
        return f"SpiderResult({', '.join(parts)})"


# ── Spider ────────────────────────────────────────────────────────────


class NewsSpider(Spider):
    """Concurrent URL spider with domain rate limiting and stealth fallback.

    Built on Scrapling's official ``Spider`` class.  Provides two session tiers:

    * **fast** (``FetcherSession`` — lightweight HTTP with browser fingerprint)
    * **stealth** (``AsyncStealthySession`` — headless browser with Cloudflare bypass)

    When a request returns a blocked status code (403/429/503), the spider
    automatically retries it via the stealth session.

    Override ``concurrent_requests`` / ``concurrent_requests_per_domain``
    via class-level attributes or by setting them in ``__init__``.
    """

    name = "news_spider"
    concurrent_requests = DEFAULT_CONCURRENT_REQUESTS
    concurrent_requests_per_domain = DEFAULT_CONCURRENT_PER_DOMAIN

    def __init__(
        self,
        urls: list[str],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        super().__init__(crawldir=None)

        self._urls = urls
        # Internal error tracker — populated by on_error()
        self._errors: dict[str, str] = {}
        # Internal tracking for gap-fill detection
        self._completed_urls: set[str] = set()

    # ── Session configuration ─────────────────────────────────────────

    def configure_sessions(self, manager):
        """Configure two session tiers: fast (default) and stealth (lazy)."""
        manager.add("fast", FetcherSession(impersonate="chrome"))
        manager.add(
            "stealth",
            AsyncStealthySession(
                solve_cloudflare=True,
                humanize=True,
            ),
            lazy=True,
        )

    # ── Request generation ────────────────────────────────────────────

    async def start_requests(self):
        """Yield one Request per URL, starting with the fast session."""
        for url in self._urls:
            yield Request(url, sid="fast")

    # ── Response parsing ──────────────────────────────────────────────

    async def parse(self, response: Response):
        """Process a successfully fetched response.

        Yields a dict matching SpiderResult fields.
        """
        # Use the original request URL (handles redirects correctly)
        url = getattr(response.request, "url", response.url)
        self._completed_urls.add(url)

        # Detect which session was used
        sid = getattr(response.request, "sid", "fast") if response.request else "fast"
        used_stealth = sid == "stealth"

        yield {
            "url": url,
            "status": response.status,
            "html_content": str(response.html_content),
            "error": None,
            "used_stealth": used_stealth,
        }

    # ── Blocked detection & retry ─────────────────────────────────────

    async def is_blocked(self, response: Response) -> bool:
        """Return True only for the status codes that trigger stealth retry."""
        return response.status in BLOCKED_STATUS_CODES

    async def retry_blocked_request(
        self, request: Request, response: Response
    ) -> Request:
        """Retry blocked requests using the stealth session."""
        logger.debug(
            "Fast session blocked for %s (status=%s) — retrying with stealth",
            request.url,
            response.status,
        )
        request.sid = "stealth"
        return request

    # ── Error handling ────────────────────────────────────────────────

    async def on_error(self, request: Request, error: Exception) -> None:
        """Record errors for requests that completely failed all retries."""
        logger.debug(
            "Request failed for %s after all retries: %s", request.url, error
        )
        self._errors[request.url] = str(error)

    # ── Result collection ─────────────────────────────────────────────

    def _collect_results(self, stream_items: list[dict]) -> list[SpiderResult]:
        """Build an ordered ``SpiderResult`` list from stream items and error tracking.

        Args:
            stream_items: Items yielded by ``stream()`` (each is a dict with
                keys matching SpiderResult fields).

        Returns:
            List of ``SpiderResult``, one per input URL, in input order.
        """
        # Build a lookup from stream results
        result_map: dict[str, dict] = {}
        for item in stream_items:
            if isinstance(item, dict) and "url" in item:
                result_map[item["url"]] = item

        # Build ordered results
        results: list[SpiderResult] = []
        for url in self._urls:
            item = result_map.get(url)
            if item is not None:
                results.append(
                    SpiderResult(
                        url=url,
                        status=item.get("status", 0),
                        html_content=item.get("html_content", ""),
                        error=item.get("error"),
                        used_stealth=item.get("used_stealth", False),
                    )
                )
            elif url in self._errors:
                results.append(
                    SpiderResult(
                        url=url,
                        status=0,
                        error=self._errors[url],
                    )
                )
            else:
                results.append(
                    SpiderResult(
                        url=url,
                        status=0,
                        error="Unknown error: request never completed",
                    )
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
        concurrent_requests: Global concurrency cap (set on spider instance).
        concurrent_per_domain: Per-domain concurrency cap (set on spider instance).

    Returns:
        List of ``SpiderResult``, one per input URL, in input order.
    """
    spider = NewsSpider(urls=urls, timeout_ms=timeout_ms)
    # Allow overrides via instance attributes
    spider.concurrent_requests = concurrent_requests
    spider.concurrent_requests_per_domain = concurrent_per_domain

    stream_items: list[dict[str, Any]] = []
    try:
        async for item in spider.stream():
            stream_items.append(item)
    finally:
        pass  # stream()'s finally block handles engine cleanup

    return spider._collect_results(stream_items)


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
