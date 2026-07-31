"""NewsSpider — concurrent URL fetcher with anti-bot bypass and domain rate-limiting.

V5.0: Camoufox integration for Tier 2 stealth.

Uses Scrapling's official ``scrapling.spiders.Spider`` base class with:

- ``FetcherSession(impersonate="chrome")`` for fast requests (Tier 1)
  - Uses curl_cffi with chrome146 TLS fingerprint
- ``Camoufox`` (patched Firefox) for blocked requests (Tier 2)
  - C++ level fingerprint spoofing, Juggler protocol (no CDP detection)
  - Shared browser instance with concurrent page pool (max 2)
  - 20s timeout for fast failure

Architecture:
    Tier 1: FetcherSession (curl_cffi, chrome146) → fast, ~1s/page
        ↓ blocked (403/429/503 + CF challenge)
    Tier 2: Camoufox (Firefox, C++ stealth) → stealth, ~5-15s/page
        ↓ failed
    Mark as failed, return to caller

Usage::

    results = await fetch_urls_with_spider(
        urls=["https://example.com/a", "https://example.com/b"],
        timeout_ms=30000,
    )
    for result in results:
        print(result.url, result.status, len(result.html_content or ""))
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from scrapling.fetchers import FetcherSession
from scrapling.spiders import Request, Response, Spider

from src.utils.logging_config import get_logger

logger = get_logger(__name__)


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

DEFAULT_CONCURRENT_REQUESTS: int = 5
"""Global maximum concurrent fetches (reduced from 10 for stability)."""

DEFAULT_CONCURRENT_PER_DOMAIN: int = 1
"""Maximum concurrent fetches per domain (reduced from 2 for cookie reuse)."""

DEFAULT_TIMEOUT_MS: int = 30000
"""Default request timeout in milliseconds."""

CAMOUFOX_TIMEOUT_MS: int = 25000
"""Timeout for Camoufox page loads — fast failure to avoid queue backup."""

CAMOUFOX_MAX_CONCURRENT: int = 1
"""Maximum concurrent Camoufox pages (browser instance is shared)."""

BLOCKED_STATUS_CODES: frozenset[int] = frozenset({403, 429, 503})
"""HTTP status codes that trigger Tier 2 (Camoufox) fallback."""

BATCH_SIZE: int = 15  # V5.5: 50→15, 配合 180s batch timeout
"""Number of URLs to process in one batch (for interface compatibility)."""


# ── Data classes ──────────────────────────────────────────────────────


class SpiderResult:
    """Result of fetching a single URL through NewsSpider.

    Attributes:
        url: The requested URL.
        status: HTTP status code (0 if fetch failed entirely).
        html_content: Raw HTML string from the page (or empty).
        error: Human-readable error message if the request failed.
        used_stealth: Whether the request was fetched via Camoufox (Tier 2).
    """

    __slots__ = ("url", "status", "html_content", "error", "used_stealth", "was_blocked")

    def __init__(
        self,
        url: str,
        status: int = 0,
        html_content: str = "",
        error: str | None = None,
        used_stealth: bool = False,
        was_blocked: bool = False,
    ) -> None:
        self.url = url
        self.status = status
        self.html_content = html_content
        self.error = error
        self.used_stealth = used_stealth
        self.was_blocked = was_blocked

    def __repr__(self) -> str:
        parts = [f"url={self.url!r}", f"status={self.status}"]
        if self.error:
            parts.append(f"error={self.error!r}")
        if self.used_stealth:
            parts.append("used_stealth=True")
        return f"SpiderResult({', '.join(parts)})"


# ── Spider ────────────────────────────────────────────────────────────


class NewsSpider(Spider):
    """Concurrent URL spider with Camoufox stealth fallback.

    V5.0 architecture:
    * **Tier 1** (``FetcherSession`` — curl_cffi with chrome146 TLS fingerprint)
    * **Tier 2** (``Camoufox`` — patched Firefox with C++ level stealth)

    When Tier 1 returns a blocked status code (403/429/503) with Cloudflare
    challenge markers, the spider falls back to Camoufox for that URL.

    Camoufox uses a shared browser instance with concurrent page pool (max 2)
    to avoid memory explosion. The browser is lazily initialized on first
    blocked request and cleaned up when the spider closes.
    """

    name = "news_spider"
    concurrent_requests = DEFAULT_CONCURRENT_REQUESTS
    concurrent_requests_per_domain = DEFAULT_CONCURRENT_PER_DOMAIN
    # V5.2: Disable Scrapling's retry for TLS errors — they won't succeed on retry
    # because the same engine is used. Failed URLs go directly to Camoufox fallback
    # in fetch_urls_with_spider(), which is more effective.
    max_blocked_retries = 0

    def __init__(
        self,
        urls: list[str],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        # Set logging_level BEFORE super().__init__() because Scrapling
        # uses it to configure the logger during initialization
        from src.core.config import get_settings
        settings = get_settings()
        level_name = settings.log_level.upper()
        self.logging_level = getattr(logging, level_name, logging.WARNING)

        super().__init__(crawldir=None)

        self._urls = urls
        self._timeout_ms = timeout_ms
        # Internal error tracker — populated by on_error()
        self._errors: dict[str, str] = {}
        # Internal tracking for gap-fill detection
        self._completed_urls: set[str] = set()

        # ── Camoufox browser pool ─────────────────────────────────────
        # Shared browser instance with concurrent page pool
        self._camoufox_manager: Any = None  # AsyncCamoufox context manager
        self._camoufox_browser: Any = None  # BrowserContext (from __aenter__)
        self._camoufox_lock = asyncio.Lock()  # Protects browser initialization
        self._camoufox_semaphore = asyncio.Semaphore(CAMOUFOX_MAX_CONCURRENT)

    # ── Session configuration ─────────────────────────────────────────

    def configure_sessions(self, manager):
        """Configure Tier 1 session only (Camoufox is handled in parse callback).
        
        V5.2: retries=0 disables FetcherSession's internal retry for TLS errors.
        TLS errors won't succeed on retry (same engine), so we skip directly to
        Camoufox fallback in fetch_urls_with_spider().
        """
        # follow_redirects=True bypasses SSRF protection for sites like EastMoney
        # that may have redirect chains through internal IPs (anti-bot mechanisms)
        # impersonate="chrome" maps to chrome146 (latest) in curl_cffi
        manager.add("fast", FetcherSession(
            impersonate="chrome",
            follow_redirects=True,
            retries=0,  # V5.2: Disable TLS error retry — Camoufox fallback handles it
        ))

    # ── Request generation ────────────────────────────────────────────

    async def start_requests(self):
        """Yield one Request per URL, starting with Tier 1 (fast session)."""
        for url in self._urls:
            yield Request(url, sid="fast")

    # ── Response parsing ──────────────────────────────────────────────

    async def parse(self, response: Response):
        """Process response, falling back to Camoufox if blocked.

        V5.0: When Tier 1 is blocked (403/429/503 + CF challenge),
        fetch via Camoufox directly in the parse callback.
        """
        # Use the original request URL (handles redirects correctly)
        url = getattr(response.request, "url", response.url)
        self._completed_urls.add(url)

        # Check if blocked → fall back to Camoufox
        if self._is_blocked_response(response):
            logger.warning(
                "Tier 1 (chrome146) blocked for %s (status=%d) — falling back to Camoufox",
                url,
                response.status,
            )
            # Fetch via Camoufox (shared browser, concurrent page pool)
            html_content, error = await self._fetch_with_camoufox(url)
            if html_content:
                yield {
                    "url": url,
                    "status": 200,
                    "html_content": html_content,
                    "error": None,
                    "used_stealth": True,
                }
            else:
                yield {
                    "url": url,
                    "status": 0,
                    "html_content": "",
                    "error": error or "Camoufox fetch failed",
                    "used_stealth": True,
                }
        else:
            # Tier 1 succeeded
            yield {
                "url": url,
                "status": response.status,
                "html_content": str(response.html_content),
                "error": None,
                "used_stealth": False,
            }

    # ── Camoufox browser pool ─────────────────────────────────────────

    async def _get_camoufox_browser(self) -> Any:
        """Get or create the shared Camoufox browser context.
        
        Thread-safe: uses asyncio.Lock to prevent race conditions
        during lazy initialization.
        
        Returns the BrowserContext (from __aenter__), not the AsyncCamoufox
        context manager itself.
        
        V5.1: Optimized parameters for Cloudflare bypass:
        - geoip=True: Auto-set timezone/language/coords based on IP
        - os="windows": Match fingerprint to Windows OS
        - block_webrtc=True: Prevent WebRTC IP leak
        - disable_coop=True: Allow clicking CF Turnstile in cross-origin iframes
        - enable_cache=True: Reuse CF cookies for same domain
        """
        async with self._camoufox_lock:
            if self._camoufox_browser is None:
                logger.info("Initializing Camoufox browser (shared instance)...")
                try:
                    from camoufox.async_api import AsyncCamoufox
                    # AsyncCamoufox is a context manager that returns BrowserContext
                    self._camoufox_manager = AsyncCamoufox(
                        headless=True,
                        humanize=True,
                        # V5.1: Optimized for Cloudflare bypass
                        geoip=True,           # Auto-set timezone/language based on IP
                        os="windows",         # Match fingerprint to Windows
                        block_webrtc=True,    # Prevent WebRTC IP leak
                        disable_coop=True,    # Allow clicking CF Turnstile
                        enable_cache=True,    # Reuse CF cookies
                        i_know_what_im_doing=True,  # Suppress LeakWarning for disable_coop
                    )
                    # __aenter__ returns the Browser/BrowserContext
                    self._camoufox_browser = await self._camoufox_manager.__aenter__()
                    logger.info("Camoufox browser initialized successfully")
                except ImportError:
                    logger.error(
                        "Camoufox not installed. Run: pip install 'camoufox[geoip]' && camoufox fetch"
                    )
                    raise
                except Exception as e:
                    logger.error("Failed to initialize Camoufox browser: %s", e)
                    raise
        return self._camoufox_browser

    async def _fetch_with_camoufox(self, url: str) -> tuple[str, str | None]:
        """Fetch a URL using the shared Camoufox browser.

        Uses semaphore to limit concurrent pages.
        Timeout: 25s for fast failure.

        V5.2: page.close() wrapped in try/except to prevent TargetClosedError
        when the page is still navigating at timeout.
        
        V5.3: Detect anti-bot challenge pages in returned HTML. If Camoufox
        successfully fetched HTML but it contains challenge markers, treat as
        failure to avoid saving garbage content.

        Args:
            url: URL to fetch.

        Returns:
            Tuple of (html_content, error_message).
            On success: (html, None)
            On failure: ("", error_string)
        """
        # Acquire semaphore to limit concurrent Camoufox pages
        await self._camoufox_semaphore.acquire()
        try:
            browser = await self._get_camoufox_browser()
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=CAMOUFOX_TIMEOUT_MS)
                html = await page.content()
                
                # V5.3: Check if the returned HTML is an anti-bot challenge page
                # V5.4: Only flag as challenge if it's small (< 50KB) or has specific markers
                # (large pages that mention "cloudflare" are usually real pages using CF CDN)
                if html and self._has_cloudflare_challenge(html):
                    # Verify it's actually a challenge page, not just a normal page mentioning CF
                    if len(html) < 50000 or "challenge-platform" in html.lower() or "just a moment" in html.lower():
                        error = "Camoufox returned anti-bot challenge page"
                        logger.warning("%s: %s", url, error)
                        return "", error
                
                logger.debug(
                    "Camoufox fetched %d chars from %s",
                    len(html or ""),
                    url,
                )
                return html or "", None
            except asyncio.TimeoutError:
                error = f"Camoufox timeout after {CAMOUFOX_TIMEOUT_MS}ms"
                logger.warning("%s: %s", url, error)
                return "", error
            except Exception as e:
                error = f"Camoufox error: {e}"
                logger.warning("%s: %s", url, error)
                return "", error
            finally:
                # V5.2: Safe close — suppress TargetClosedError
                # (page may still be navigating when timeout fires)
                try:
                    await page.close()
                except Exception:
                    pass
        finally:
            self._camoufox_semaphore.release()

    async def _close_camoufox_browser(self):
        """Close the shared Camoufox browser instance.
        
        Called when the spider closes to prevent zombie processes.
        """
        async with self._camoufox_lock:
            if self._camoufox_manager is not None:
                try:
                    logger.info("Closing Camoufox browser...")
                    await self._camoufox_manager.__aexit__(None, None, None)
                    self._camoufox_browser = None
                    self._camoufox_manager = None
                    logger.info("Camoufox browser closed")
                except Exception as e:
                    logger.warning("Error closing Camoufox browser: %s", e)

    # ── Blocked detection ─────────────────────────────────────────────

    async def is_blocked(self, response: Response) -> bool:
        """Always return False — blocked detection is handled in parse() callback.
        
        V5.0: We don't use Scrapling's retry mechanism because we handle
        Camoufox fallback directly in parse(). This avoids conflicts with
        the removed AsyncStealthySession.
        """
        return False

    def _is_blocked_response(self, response: Response) -> bool:
        """Check if response is blocked (used internally in parse callback).
        
        V5.0: Returns True when status is blocked (403/429/503) AND HTML
        contains Cloudflare challenge markers. This avoids false positives
        from pages that happen to mention 'cloudflare' in their content.
        """
        if response.status not in BLOCKED_STATUS_CODES:
            return False

        html = str(response.html_content or "")
        if not html:
            # Blocked status with no HTML — treat as blocked
            logger.warning(
                "Tier 1 blocked for %s (status=%d, no HTML) — falling back to Camoufox",
                response.url,
                response.status,
            )
            return True

        if self._has_cloudflare_challenge(html):
            logger.warning(
                "Tier 1 blocked for %s (status=%d, CF challenge detected, html_len=%d) — falling back to Camoufox",
                response.url,
                response.status,
                len(html),
            )
            return True

        # Blocked status but no CF challenge detected — might be other anti-bot
        logger.debug(
            "Status %d but no Cloudflare challenge detected — not retrying for %s",
            response.status,
            response.url,
        )
        return False

    def _has_cloudflare_challenge(self, html: str) -> bool:
        """Detect whether HTML contains Cloudflare or other anti-bot challenge markers.
        
        V5.3: Extended to detect multiple anti-bot services, not just Cloudflare.
        V5.6: Fixed false positives - only detect actual challenge pages, not
        normal pages that mention these keywords in content.
        
        Challenge pages have specific characteristics:
        - Very small HTML size (< 10KB)
        - Contain specific challenge markers in title or meta
        - Have challenge-platform div or turnstile iframe
        """
        # Skip large pages - challenge pages are always small
        if len(html) > 15000:
            return False
        
        html_lower = html.lower()
        
        # Check for actual challenge page characteristics
        # These are specific to challenge pages, not normal content
        challenge_indicators = [
            # Cloudflare challenge page title
            ("<title>just a moment", 5000),  # Must be in first 5KB
            # Cloudflare challenge platform div
            ("challenge-platform", None),
            # Cloudflare Turnstile iframe
            ("turnstile", None),
            # CF challenge script
            ("cdn-cgi/challenge-platform", None),
            # Generic challenge page title patterns
            ("<title>attention required", 5000),
            ("<title>security check", 5000),
        ]
        
        for indicator, max_pos in challenge_indicators:
            if max_pos:
                # Check if indicator appears early in the page (challenge pages have it at top)
                pos = html_lower.find(indicator)
                if pos != -1 and pos < max_pos:
                    return True
            else:
                if indicator in html_lower:
                    return True
        
        # Check for very small pages with challenge-like content
        # Challenge pages are typically < 5KB and have specific patterns
        if len(html) < 5000:
            # Small page with challenge keywords
            small_page_indicators = [
                "verifying you are human",
                "checking your browser",
                "enable javascript and reload",
                "ray id",  # Cloudflare Ray ID (specific to challenge pages)
                "performance security check",
            ]
            if any(ind in html_lower for ind in small_page_indicators):
                return True
        
        return False

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
                        was_blocked=item.get("_blocked", False),
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

    V5.1: For URLs that failed at Tier 1 (TLS errors, connection refused, etc.),
    retry with Camoufox as a final fallback. This handles cases where curl_cffi
    fails before getting any HTTP response.

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
        # ── Cleanup: Close Camoufox browser to prevent zombie processes ──
        await spider._close_camoufox_browser()

        # Explicitly close session manager to prevent "Event loop is closed"
        # errors on exit. Scrapling's stream() doesn't do this automatically.
        try:
            await spider._session_manager.close()
        except (RuntimeError, Exception):
            pass

    results = spider._collect_results(stream_items)

    # V5.1: Retry failed URLs with Camoufox (handles TLS errors, connection refused, etc.)
    failed_urls = [r for r in results if r.error and not r.html_content]
    if failed_urls:
        logger.info(
            "Retrying %d failed URLs with Camoufox fallback (TLS errors, connection refused, etc.)",
            len(failed_urls),
        )
        # Create a temporary spider just for Camoufox access
        retry_spider = NewsSpider(urls=[])
        try:
            # Initialize Camoufox browser
            await retry_spider._get_camoufox_browser()
            
            # Retry each failed URL with Camoufox
            retry_tasks = [
                retry_spider._fetch_with_camoufox(r.url)
                for r in failed_urls
            ]
            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
            
            # Update results with Camoufox responses
            for i, (result, retry_result) in enumerate(zip(failed_urls, retry_results)):
                if isinstance(retry_result, Exception):
                    logger.warning(
                        "Camoufox retry failed for %s: %s",
                        result.url,
                        retry_result,
                    )
                    continue
                    
                html_content, error = retry_result
                if html_content:
                    idx = results.index(result)
                    results[idx] = SpiderResult(
                        url=result.url,
                        status=200,
                        html_content=html_content,
                        error=None,
                        used_stealth=True,
                    )
                    logger.info(
                        "Camoufox retry succeeded for %s (%d chars)",
                        result.url,
                        len(html_content),
                    )
        finally:
            await retry_spider._close_camoufox_browser()

    return results


__all__ = [
    "NewsSpider",
    "SpiderResult",
    "fetch_urls_with_spider",
    "DEFAULT_CONCURRENT_REQUESTS",
    "DEFAULT_CONCURRENT_PER_DOMAIN",
    "DEFAULT_TIMEOUT_MS",
    "CAMOUFOX_TIMEOUT_MS",
    "CAMOUFOX_MAX_CONCURRENT",
    "BLOCKED_STATUS_CODES",
    "BATCH_SIZE",
]
