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

import asyncio
import logging
from typing import Any

from scrapling.fetchers import AsyncStealthySession, FetcherSession
from scrapling.spiders import Request, Response, Spider

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


# ── Monkey-patch: fix scrapling's Cloudflare solver bugs ──────
# 1. "managed" type is incorrectly handled like "interactive" (clicks checkbox)
#    but "managed" is a JS auto-challenge that needs NO interaction.
# 2. _cloudflare_solver() recurses infinitely when challenge can't be solved.
# See: https://github.com/D4Vinci/Scrapling/pull/330 (closed, not merged)

_MAX_CF_SOLVER_DEPTH = 3
_cf_solver_depths: dict = {}  # page -> current depth


def _patch_cloudflare_solver():
    """Patch AsyncStealthySession._cloudflare_solver to:
    1. Treat 'managed' type like 'non-interactive' (wait, don't click)
    2. Add max recursion depth to prevent infinite loops
    """
    try:
        from scrapling.engines._browsers._stealth import AsyncStealthySession
        from scrapling.engines._browsers._base import StealthySessionMixin
    except ImportError:
        logger.warning("Could not import AsyncStealthySession for Cloudflare solver patch")
        return

    original_solver = AsyncStealthySession._cloudflare_solver
    detect_cloudflare = StealthySessionMixin._detect_cloudflare

    async def _patched_solver(self, page):
        """Patched solver: fix 'managed' type + depth limit."""
        # Get current depth for this page
        depth = _cf_solver_depths.get(page, 0)
        
        if depth >= _MAX_CF_SOLVER_DEPTH:
            logger.warning(
                f"Cloudflare solver reached max depth ({_MAX_CF_SOLVER_DEPTH}), giving up"
            )
            _cf_solver_depths.pop(page, None)
            return None

        # Increment depth
        _cf_solver_depths[page] = depth + 1

        try:
            # Detect challenge type
            from scrapling.engines.toolbelt.convertor import ResponseFactory
            page_content = await ResponseFactory._get_async_page_content(page)
            challenge_type = detect_cloudflare(page_content)

            if not challenge_type:
                logger.error("No Cloudflare challenge found.")
                return None

            logger.info(f'Cloudflare challenge type: "{challenge_type}"')

            # FIX: Treat "managed" like "non-interactive" (wait, don't click)
            # "managed" is a JS auto-challenge, no user interaction needed
            if challenge_type in ("non-interactive", "managed"):
                logger.info(f"Waiting for {challenge_type} challenge to resolve...")
                max_wait = 30  # seconds
                waited = 0
                while "<title>Just a moment...</title>" in page_content and waited < max_wait:
                    await page.wait_for_timeout(1000)
                    waited += 1
                    page_content = await ResponseFactory._get_async_page_content(page)
                
                if "<title>Just a moment...</title>" not in page_content:
                    logger.info("Cloudflare challenge resolved")
                    return None
                else:
                    logger.warning(f"Cloudflare {challenge_type} challenge did not resolve after {max_wait}s")
                    return None

            # For "interactive" and "embedded", use original solver (with depth limit)
            return await original_solver(self, page)

        finally:
            # Decrement depth (or cleanup if done)
            new_depth = _cf_solver_depths.get(page, 1) - 1
            if new_depth <= 0:
                _cf_solver_depths.pop(page, None)
            else:
                _cf_solver_depths[page] = new_depth

    # Apply the patch
    AsyncStealthySession._cloudflare_solver = _patched_solver
    logger.info(f"Patched Cloudflare solver: 'managed' type fixed + max_depth={_MAX_CF_SOLVER_DEPTH}")


# Apply patch on module import
_patch_cloudflare_solver()


# ── Monkey-patch: fix scrapling's engine log bugs ──────────────────
# Scrapling's CrawlerEngine._run_callbacks has two logging bugs:
# 1. Line 157: log.debug(f"...\n{pprint.pformat(processed_result)}")
#    The f-string is eagerly evaluated BEFORE log.debug() checks level.
#    pprint.pformat() on a 100KB+ HTML dict wastes CPU/memory on EVERY request.
# 2. Line 164: log.warning(f"Dropped from ...\n{processed_result}")
#    WARNING is always output → full 100KB+ HTML in log → "Unable to print" error.
# Fix: wrap _run_callbacks to guard expensive formatting.

_MAX_LOG_PREVIEW_CHARS = 500


def _patch_engine_logging():
    """Patch CrawlerEngine._run_callbacks to avoid expensive/huge log formatting."""
    try:
        from scrapling.spiders.engine import CrawlerEngine
    except ImportError:
        logger.warning("Could not import CrawlerEngine for logging patch")
        return

    import pprint as _pprint

    _original_run_callbacks = CrawlerEngine._run_callbacks

    async def _patched_run_callbacks(self, request, response):
        """Patched _run_callbacks: guard expensive log formatting."""
        callback = request.callback if request.callback else self.spider.parse
        try:
            async for result in callback(response):
                if isinstance(result, Request):
                    if self._is_domain_allowed(result):
                        self._normalize_request(result)
                        await self.scheduler.enqueue(result)
                    else:
                        self.stats.offsite_requests_count += 1
                        # Original: log.debug(f"Filtered offsite ...")  — safe, no huge data
                        if self.spider.logger.isEnabledFor(logging.DEBUG):
                            from scrapling.core.utils import log
                            log.debug(f"Filtered offsite request to: {result.url}")
                elif isinstance(result, dict):
                    processed_result = await self.spider.on_scraped_item(result)
                    if processed_result:
                        self.stats.items_scraped += 1
                        # FIX: only pprint when DEBUG is actually enabled
                        if self.spider.logger.isEnabledFor(logging.DEBUG):
                            from scrapling.core.utils import log
                            log.debug(
                                f"Scraped from {str(response)}\n"
                                f"{_pprint.pformat(processed_result)}"
                            )
                        if self._item_stream:
                            await self._item_stream.send(processed_result)
                        else:
                            self._items.append(processed_result)
                    else:
                        self.stats.items_dropped += 1
                        # FIX: truncate dict preview to avoid "Unable to print"
                        from scrapling.core.utils import log
                        preview = str(processed_result)
                        if len(preview) > _MAX_LOG_PREVIEW_CHARS:
                            preview = preview[:_MAX_LOG_PREVIEW_CHARS] + "... [truncated]"
                        log.warning(f"Dropped from {str(response)}\n{preview}")
                elif result is not None:
                    from scrapling.core.utils import log
                    log.error(
                        f"Spider must return Request, dict or None, got '{type(result)}' in {request}"
                    )
        except Exception as e:
            from scrapling.core.utils import log
            msg = f"Spider error processing {request}:\n {e}"
            log.error(msg, exc_info=e)
            await self.spider.on_error(request, e)

    CrawlerEngine._run_callbacks = _patched_run_callbacks
    logger.info(
        f"Patched CrawlerEngine._run_callbacks: lazy DEBUG + truncated WARNING "
        f"(max {_MAX_LOG_PREVIEW_CHARS} chars)"
    )


_patch_engine_logging()


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
    logging_level = logging.WARNING  # Suppress INFO logs; respect user's LOG_LEVEL setting

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
        # follow_redirects=True bypasses SSRF protection for sites like EastMoney
        # that may have redirect chains through internal IPs (anti-bot mechanisms)
        manager.add("fast", FetcherSession(impersonate="chrome", follow_redirects=True))
        manager.add(
            "stealth",
            AsyncStealthySession(
                solve_cloudflare=True,
                humanize=True,
                timeout=60000,  # 60 seconds in milliseconds — Cloudflare solver timeout
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
        """Return True only for blocked status codes WITH Cloudflare challenge."""
        if response.status not in BLOCKED_STATUS_CODES:
            return False

        # Check response body for Cloudflare challenge markers
        html = str(response.html_content or "")
        if self._has_cloudflare_challenge(html):
            return True

        # 403/429/503 but no Cloudflare challenge (other anti-bot) → don't retry
        logger.debug(
            "Status %d but no Cloudflare challenge detected — not retrying for %s",
            response.status,
            response.url,
        )
        return False

    def _has_cloudflare_challenge(self, html: str) -> bool:
        """Detect whether HTML contains Cloudflare challenge markers."""
        cf_markers = [
            "just a moment",       # CF challenge page title "Just a moment..."
            "cloudflare",          # generic keyword
            "cf-challenge",        # CF challenge CSS class
            "challenge-platform",  # CF challenge platform div
            "turnstile",           # Cloudflare Turnstile
        ]
        html_lower = html.lower()
        return any(marker in html_lower for marker in cf_markers)

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
