"""NewsSpider — concurrent URL fetcher with anti-bot bypass and domain rate-limiting.

V7.0: 5-tier funnel (4 fetch tiers + 1 extraction tier).

Uses Scrapling's official ``scrapling.spiders.Spider`` base class with:

- ``FetcherSession(impersonate="chrome")`` for fast requests (Tier 1)
  - Uses curl_cffi with chrome146 TLS fingerprint
  - Injects pooled ``cf_clearance`` cookies from ``(domain, "chrome146")``
- Alternate TLS fingerprints ``firefox135`` / ``safari15_5`` (Tier 1.5)
  - Independent ``FetcherSession`` per fingerprint, runs after Tier 1 stream
- ``CloakBrowser`` (patched Chromium) for blocked requests (Tier 2)
  - 71 C++ source-level stealth patches
  - Native Playwright async API, stable multi-page concurrency
  - 30s timeout for page loads
  - On success, extracts cookies (incl. ``cf_clearance``) into the pool
- ``Camoufox`` (Firefox engine + Juggler protocol) ultimate fallback (Tier 3)
  - Optional soft dependency: lazily imported, skipped when not installed
  - On success, writes cookies into the pool under ``(domain, "firefox135")``

Architecture (4 fetch tiers + 1 extraction tier):
    Tier 1: FetcherSession (curl_cffi, chrome146) → fast, ~1s/page
        ↓ blocked (403/429/503 + CF challenge) or connection failure
    Tier 1.5: FetcherSession firefox135 → safari15_5 → ~1s/page each
        ↓ still failed
    Tier 2: CloakBrowser (Chromium, C++ stealth) → stealth, ~5-15s/page
        ↓ failed / crashed
    Tier 3: Camoufox (Firefox + Juggler) → ultimate stealth, ~10-15s/page
        ↓ failed
    Mark as failed, return to caller

Usage::

    results = await fetch_urls_with_spider(
        urls=["https://example.com/a", "https://example.com/b"],
        timeout_ms=30000,
    )
    for result in results:
        print(result.url, result.status, result.fetch_tier, len(result.html_content or ""))
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

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

CLOAK_TIMEOUT_MS: int = 30000
"""Timeout for CloakBrowser page loads."""

CLOAK_MAX_CONCURRENT: int = 1
"""Maximum concurrent CloakBrowser pages (free version limits 1 session)."""

BLOCKED_STATUS_CODES: frozenset[int] = frozenset({403, 429, 503})
"""HTTP status codes that trigger Tier 2 (CloakBrowser) fallback."""

BATCH_SIZE: int = 15
"""Number of URLs to process in one batch (for interface compatibility)."""

TIER1_FINGERPRINT: str = "chrome146"
"""TLS fingerprint label used by Tier 1 (curl_cffi ``impersonate="chrome"`` → chrome146).

Also the pool key fingerprint for cookies harvested by CloakBrowser (D7):
CloakBrowser is a Chromium kernel, so its JA3 matches chrome146 closely and
its ``cf_clearance`` can be reused by Tier 1 requests.
"""

TIER15_FINGERPRINTS: tuple[str, ...] = ("firefox135", "safari15_5")
"""TLS fingerprints tried in order by Tier 1.5 (D5)."""

CAMOUFOX_FINGERPRINT: str = "firefox135"
"""Pool key fingerprint for cookies harvested by Camoufox (D13):
Camoufox is a Firefox kernel, its TLS/JA3 fingerprint is closest to
firefox135, so Tier 1.5 firefox135 requests can reuse those cookies.
"""

COOKIE_TTL_SEC: float = 25 * 60
"""TTL for pooled cookies in seconds (D8: < cf_clearance's common ~30min lifetime)."""

CAMOUFOX_PAGE_TIMEOUT_SEC: int = 15
"""Page load timeout for Camoufox (Tier 3) in seconds."""


# ── In-memory cookie pool (module-level, process memory only) ─────────
# Cloudflare ``cf_clearance`` is bound to the TLS/JA3 fingerprint that
# obtained it, so the pool is keyed by ``(domain, fingerprint)`` (D6).


@dataclass
class _CookieEntry:
    """A single pooled cookie set with its fetch timestamp."""

    cookies: dict[str, str]  # name -> value (may include cf_clearance)
    fetched_at: float  # time.monotonic()


def _domain_of(url: str) -> str:
    """Extract the lowercase hostname from a URL (empty string if unparseable)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


_domain_cookie_pool: dict[tuple[str, str], _CookieEntry] = {}
"""Module-level cookie pool keyed by ``(domain, fingerprint)``."""

_pool_lock = asyncio.Lock()
"""Protects concurrent reads/writes of the cookie pool."""


async def pool_get(domain: str, fingerprint: str) -> dict[str, str] | None:
    """Return non-expired pooled cookies for ``(domain, fingerprint)``.

    Lazily evicts expired entries (D8): an entry older than ``COOKIE_TTL_SEC``
    is removed on read and treated as a miss.

    Returns:
        A copy of the cookie dict, or ``None`` when no valid entry exists.
    """
    key = (domain, fingerprint)
    async with _pool_lock:
        entry = _domain_cookie_pool.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.fetched_at > COOKIE_TTL_SEC:
            del _domain_cookie_pool[key]
            logger.debug(
                "Evicted expired pooled cookies for %s (%s)", domain, fingerprint
            )
            return None
        return dict(entry.cookies)


async def pool_put(
    domain: str, fingerprint: str, cookies: dict[str, str]
) -> None:
    """Store cookies in the pool under ``(domain, fingerprint)``.

    Empty cookie dicts are ignored. Existing entries are replaced with a
    fresh timestamp.
    """
    if not cookies:
        return
    key = (domain, fingerprint)
    async with _pool_lock:
        _domain_cookie_pool[key] = _CookieEntry(
            cookies=dict(cookies),
            fetched_at=time.monotonic(),
        )
    logger.debug(
        "Pooled %d cookies for %s (%s)", len(cookies), domain, fingerprint
    )


async def pool_invalidate(domain: str, fingerprint: str) -> None:
    """Remove a pool entry — injected cookies failed to bypass the block.

    Keeps stale ``cf_clearance`` cookies from being reused repeatedly
    against the same wall (D8).
    """
    key = (domain, fingerprint)
    async with _pool_lock:
        removed = _domain_cookie_pool.pop(key, None)
    if removed is not None:
        logger.debug(
            "Invalidated pooled cookies for %s (%s)", domain, fingerprint
        )


# ── Data classes ──────────────────────────────────────────────────────


class SpiderResult:
    """Result of fetching a single URL through NewsSpider.

    Attributes:
        url: The requested URL.
        status: HTTP status code (0 if fetch failed entirely).
        html_content: Raw HTML string from the page (or empty).
        error: Human-readable error message if the request failed.
        used_stealth: Whether the request was fetched via a stealth browser
            (Tier 2 CloakBrowser or Tier 3 Camoufox).
        was_blocked: Whether the request was detected as blocked.
        fetch_tier: Which fetch tier produced the HTML — "1" | "1.5" | "2" | "3"
            (D10; defaults to "1", failed results may keep the attempted tier).
    """

    __slots__ = (
        "url",
        "status",
        "html_content",
        "error",
        "used_stealth",
        "was_blocked",
        "fetch_tier",
    )

    def __init__(
        self,
        url: str,
        status: int = 0,
        html_content: str = "",
        error: str | None = None,
        used_stealth: bool = False,
        was_blocked: bool = False,
        fetch_tier: str = "1",
    ) -> None:
        self.url = url
        self.status = status
        self.html_content = html_content
        self.error = error
        self.used_stealth = used_stealth
        self.was_blocked = was_blocked
        self.fetch_tier = fetch_tier

    def __repr__(self) -> str:
        parts = [f"url={self.url!r}", f"status={self.status}"]
        if self.error:
            parts.append(f"error={self.error!r}")
        if self.used_stealth:
            parts.append("used_stealth=True")
        if self.fetch_tier != "1":
            parts.append(f"fetch_tier={self.fetch_tier!r}")
        return f"SpiderResult({', '.join(parts)})"


# ── Spider ────────────────────────────────────────────────────────────


class NewsSpider(Spider):
    """Concurrent URL spider with CloakBrowser stealth fallback.

    V6.0 architecture:
    * **Tier 1** (``FetcherSession`` — curl_cffi with chrome146 TLS fingerprint)
    * **Tier 2** (``CloakBrowser`` — patched Chromium with 71 C++ stealth patches)

    When Tier 1 returns a blocked status code (403/429/503) with Cloudflare
    challenge markers, the spider falls back to CloakBrowser for that URL.

    CloakBrowser uses a shared browser instance with concurrent page pool.
    Chromium handles multi-page concurrency much better than Firefox (Camoufox).
    """

    name = "news_spider"
    concurrent_requests = DEFAULT_CONCURRENT_REQUESTS
    concurrent_requests_per_domain = DEFAULT_CONCURRENT_PER_DOMAIN
    # V5.2: Disable Scrapling's retry for TLS errors — they won't succeed on retry
    # because the same engine is used. Failed URLs go directly to CloakBrowser fallback
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

        # ── CloakBrowser pool ─────────────────────────────────────────
        # Shared browser instance with concurrent page pool
        self._cloak_browser: Any = None  # Playwright Browser (from launch_async)
        self._cloak_lock = asyncio.Lock()  # Protects browser initialization
        self._cloak_semaphore = asyncio.Semaphore(CLOAK_MAX_CONCURRENT)

    # ── Session configuration ─────────────────────────────────────────

    def configure_sessions(self, manager):
        """Configure Tier 1 session only (CloakBrowser is handled in parse callback).
        
        V5.2: retries disabled FetcherSession's internal retry for TLS errors.
        TLS errors won't succeed on retry (same engine), so we skip directly to
        Tier 1.5 / CloakBrowser fallback in fetch_urls_with_spider().
        
        V7.0: ``retries=1`` (not ``retries=0``) — scrapling 0.4.14 builds the
        attempt loop as ``range(max_retries)``, so ``retries=0`` never executes
        a single request and raises "No active session available". ``retries=1``
        preserves the original no-internal-retry intent (exactly one attempt).
        """
        # follow_redirects=True bypasses SSRF protection for sites like EastMoney
        # that may have redirect chains through internal IPs (anti-bot mechanisms)
        # impersonate="chrome" maps to chrome146 (latest) in curl_cffi
        manager.add("fast", FetcherSession(
            impersonate="chrome",
            follow_redirects=True,
            retries=1,  # V7.0: 1 attempt (0 = never sends request in scrapling 0.4.14)
        ))

    # ── Request generation ────────────────────────────────────────────

    async def start_requests(self):
        """Yield one Request per URL, starting with Tier 1 (fast session).

        V7.0: injects pooled cookies for ``(domain, "chrome146")`` via the
        Scrapling Request-level ``cookies`` parameter (D9).
        """
        for url in self._urls:
            cookies = await pool_get(_domain_of(url), TIER1_FINGERPRINT)
            if cookies:
                logger.debug(
                    "Injecting %d pooled cookies for %s (chrome146)",
                    len(cookies),
                    url,
                )
                yield Request(url, sid="fast", cookies=cookies)
            else:
                yield Request(url, sid="fast")

    # ── Response parsing ──────────────────────────────────────────────

    async def parse(self, response: Response):
        """Process response, falling back to CloakBrowser if blocked.

        V6.0: When Tier 1 is blocked (403/429/503 + CF challenge),
        fetch via CloakBrowser directly in the parse callback.
        V7.0: blocked results first go through Tier 1.5 (alternate TLS
        fingerprints) in fetch_urls_with_spider() before Tier 2.
        """
        # Use the original request URL (handles redirects correctly)
        url = getattr(response.request, "url", response.url)
        self._completed_urls.add(url)

        # Check if blocked → defer to Tier 1.5 / Tier 2 AFTER spider completes
        if self._is_blocked_response(response):
            # V7.0: injected pooled cookies still got blocked → invalidate them
            request_kwargs = getattr(response.request, "_session_kwargs", None) or {}
            if request_kwargs.get("cookies"):
                await pool_invalidate(_domain_of(url), TIER1_FINGERPRINT)
                logger.debug(
                    "Invalidated pooled cookies for %s (chrome146) after continued block",
                    url,
                )
            logger.warning(
                "Tier 1 (chrome146) blocked for %s (status=%d) — queued for Tier 1.5/2 retry",
                url,
                response.status,
            )
            # IMPORTANT: Do NOT call CloakBrowser here. Scrapling's Spider engine
            # shares the event loop with Tier 1 concurrent requests, which interferes
            # with Playwright's WebSocket connection (ERR_CONNECTION_CLOSED).
            # Tier 1.5 / Tier 2 run after spider.stream() completes in fetch_urls_with_spider().
            yield {
                "url": url,
                "status": response.status,
                "html_content": "",
                "error": "Tier 1 blocked, queued for retry",
                "used_stealth": False,
                "fetch_tier": "1",
            }
        else:
            # Tier 1 succeeded
            yield {
                "url": url,
                "status": response.status,
                "html_content": str(response.html_content),
                "error": None,
                "used_stealth": False,
                "fetch_tier": "1",
            }

    # ── CloakBrowser pool ─────────────────────────────────────────────

    async def _get_cloak_browser(self) -> Any:
        """Get or create the shared CloakBrowser instance.
        
        Thread-safe: uses asyncio.Lock to prevent race conditions
        during lazy initialization.
        
        Returns a Playwright async Browser object.
        
        V6.0: CloakBrowser parameters:
        - headless=True: Run without visible window
        - geoip=True: Auto-set timezone/language based on IP
        - humanize=True: Enable human-like mouse/keyboard behavior
        """
        async with self._cloak_lock:
            if self._cloak_browser is None:
                logger.info("Initializing CloakBrowser (shared instance)...")
                try:
                    from cloakbrowser import launch_async
                    
                    self._cloak_browser = await launch_async(
                        headless=True,
                        geoip=True,
                        humanize=True,
                    )
                    logger.info("CloakBrowser initialized successfully")
                except ImportError:
                    logger.error(
                        "CloakBrowser not installed. Run: pip install cloakbrowser"
                    )
                    raise
                except Exception as e:
                    logger.error("Failed to initialize CloakBrowser: %s", e)
                    raise
        return self._cloak_browser

    async def _fetch_with_cloak(self, url: str) -> tuple[str, str | None]:
        """Fetch a URL using the shared CloakBrowser.

        Uses semaphore to limit concurrent pages.
        Timeout: 30s for page loads.

        V7.0: on success, extracts ``page.context.cookies()`` and, when the
        response context contains ``cf_clearance``, writes the cookies into
        the pool under ``(domain, "chrome146")`` (D7) for Tier 1 reuse.

        Args:
            url: URL to fetch.

        Returns:
            Tuple of (html_content, error_message).
            On success: (html, None)
            On failure: ("", error_string)
        """
        # Acquire semaphore to limit concurrent CloakBrowser pages
        await self._cloak_semaphore.acquire()
        try:
            browser = await self._get_cloak_browser()
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=CLOAK_TIMEOUT_MS, wait_until="domcontentloaded")
                html = await page.content()
                
                # Check if the returned HTML is an anti-bot challenge page
                if html and self._has_cloudflare_challenge(html):
                    # Verify it's actually a challenge page, not just a normal page mentioning CF
                    if len(html) < 50000 or "challenge-platform" in html.lower() or "just a moment" in html.lower():
                        error = "CloakBrowser returned anti-bot challenge page"
                        logger.warning("%s: %s", url, error)
                        return "", error
                
                # V7.0: harvest cookies for the cookie pool
                if html:
                    try:
                        raw_cookies = await page.context.cookies()
                        cookie_dict = {
                            c["name"]: c["value"]
                            for c in raw_cookies
                            if c.get("name") and c.get("value") is not None
                        }
                        if "cf_clearance" in cookie_dict:
                            await pool_put(
                                _domain_of(url),
                                TIER1_FINGERPRINT,
                                cookie_dict,
                            )
                    except Exception as e:
                        logger.debug(
                            "Failed to harvest cookies from CloakBrowser for %s: %s",
                            url,
                            e,
                        )
                
                logger.debug(
                    "CloakBrowser fetched %d chars from %s",
                    len(html or ""),
                    url,
                )
                return html or "", None
            except asyncio.TimeoutError:
                error = f"CloakBrowser timeout after {CLOAK_TIMEOUT_MS}ms"
                logger.warning("%s: %s", url, error)
                return "", error
            except Exception as e:
                error = f"CloakBrowser error: {e}"
                logger.warning("%s: %s", url, error)
                return "", error
            finally:
                # Safe close — suppress errors
                try:
                    await page.close()
                except Exception:
                    pass
        finally:
            self._cloak_semaphore.release()

    async def _close_cloak_browser(self):
        """Close the shared CloakBrowser instance.
        
        Called when the spider closes to prevent zombie processes.
        """
        async with self._cloak_lock:
            if self._cloak_browser is not None:
                try:
                    logger.info("Closing CloakBrowser...")
                    await self._cloak_browser.close()
                    self._cloak_browser = None
                    logger.info("CloakBrowser closed")
                except Exception as e:
                    logger.warning("Error closing CloakBrowser: %s", e)

    # ── Tier 1.5: alternate TLS fingerprint retry ────────────────────

    async def _retry_with_alt_fingerprints(
        self,
        results: list[SpiderResult],
        failed_urls: list[tuple[int, SpiderResult]],
    ) -> list[tuple[int, SpiderResult]]:
        """Tier 1.5 — retry failed URLs with alternate TLS fingerprints.

        Tries ``firefox135`` first, then ``safari15_5`` (D5), using an
        independent ``FetcherSession`` per fingerprint (D4). Pooled cookies
        for ``(domain, fingerprint)`` are injected when available (D9); a
        continued block invalidates that pool entry (D8).

        Args:
            results: The ordered result list (mutated in place on success).
            failed_urls: ``(index, result)`` pairs that failed Tier 1.

        Returns:
            The ``(index, result)`` pairs that STILL failed after all
            fingerprints — these proceed to Tier 2 (CloakBrowser).
        """
        remaining = list(failed_urls)
        timeout = max(self._timeout_ms / 1000.0, 1.0)

        for fp in TIER15_FINGERPRINTS:
            if not remaining:
                break

            logger.info(
                "Tier 1.5: retrying %d URLs with fingerprint %s",
                len(remaining),
                fp,
            )

            session_mgr = FetcherSession(
                impersonate=fp,
                follow_redirects=True,
                retries=1,  # exactly one attempt (retries=0 never sends in scrapling 0.4.14)
            )
            outcomes: list[tuple[int, SpiderResult, Response | None, str | None]] = []
            # ``async with`` guarantees the session is closed in a finally block.
            async with session_mgr as session:
                async def _retry_one(
                    item: tuple[int, SpiderResult],
                ) -> tuple[int, SpiderResult, Response | None, str | None]:
                    idx, result = item
                    domain = _domain_of(result.url)
                    cookies = await pool_get(domain, fp)
                    try:
                        response = await session.get(
                            result.url,
                            timeout=timeout,
                            cookies=cookies or None,
                        )
                        return idx, result, response, None
                    except Exception as exc:
                        return idx, result, None, str(exc)

                outcomes = await asyncio.gather(
                    *[_retry_one(item) for item in remaining]
                )

            still_failed: list[tuple[int, SpiderResult]] = []
            for idx, result, response, err in outcomes:
                if err is not None:
                    logger.warning(
                        "Tier 1.5 (%s) connection failed for %s: %s",
                        fp,
                        result.url,
                        err,
                    )
                    still_failed.append((idx, result))
                    continue

                html = str(response.html_content or "")
                domain = _domain_of(result.url)

                # Blocked status + Cloudflare challenge → invalidate pooled
                # cookies for this fingerprint and keep retrying.
                if response.status in BLOCKED_STATUS_CODES and self._has_cloudflare_challenge(html):
                    await pool_invalidate(domain, fp)
                    logger.warning(
                        "Tier 1.5 (%s) blocked for %s (status=%d) — invalidated pooled cookies",
                        fp,
                        result.url,
                        response.status,
                    )
                    still_failed.append((idx, result))
                    continue

                if not html:
                    logger.warning(
                        "Tier 1.5 (%s) empty response for %s (status=%d)",
                        fp,
                        result.url,
                        response.status,
                    )
                    still_failed.append((idx, result))
                    continue

                # Tier 1.5 success
                results[idx] = SpiderResult(
                    url=result.url,
                    status=response.status,
                    html_content=html,
                    error=None,
                    used_stealth=False,
                    fetch_tier="1.5",
                )
                logger.info(
                    "Tier 1.5 (%s) succeeded for %s (status=%d, %d chars)",
                    fp,
                    result.url,
                    response.status,
                    len(html),
                )

            remaining = still_failed

        return remaining

    # ── Tier 3: Camoufox ultimate fallback ───────────────────────────

    async def _fetch_with_camoufox(
        self, url: str, timeout: int = CAMOUFOX_PAGE_TIMEOUT_SEC
    ) -> SpiderResult:
        """Tier 3 — fetch a URL with Camoufox (Firefox engine + Juggler protocol).

        Ultimate fallback when CloakBrowser (Tier 2) fails or crashes. Uses an
        independent browser instance (D12). On success, extracts cookies and
        writes them into the pool under ``(domain, "firefox135")`` (D13) so
        Tier 1.5 firefox135 requests can reuse them.

        ``camoufox`` is an optional soft dependency: when not installed, Tier 3
        is silently skipped (returns an error result) without blocking the rest
        of the funnel.

        Args:
            url: URL to fetch.
            timeout: Page load timeout in seconds (default 15s).

        Returns:
            ``SpiderResult`` with ``fetch_tier="3"`` — success (used_stealth=True)
            or a failure result carrying the error.
        """
        await self._cloak_semaphore.acquire()
        try:
            try:
                from camoufox.async_api import AsyncCamoufox
            except ImportError:
                logger.warning(
                    "camoufox not installed, Tier 3 unavailable for %s",
                    url,
                )
                return SpiderResult(
                    url=url,
                    error="Tier 3 unavailable: camoufox not installed",
                    fetch_tier="3",
                )

            # Playwright-style TimeoutError (camoufox is built on it);
            # fall back to asyncio.TimeoutError when playwright is unavailable.
            try:
                from playwright._impl._errors import TimeoutError as _PlaywrightTimeoutError
            except ImportError:  # pragma: no cover
                _PlaywrightTimeoutError = asyncio.TimeoutError

            domain = _domain_of(url)
            try:
                async with AsyncCamoufox(
                    headless=True,
                    geoip=True,
                    humanize=True,
                ) as browser:
                    page = await browser.new_page()
                    try:
                        await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=timeout * 1000,
                        )
                        html = await page.content()
                        cookies = await page.context.cookies()
                    finally:
                        # Safe close — suppress errors
                        try:
                            await page.close()
                        except Exception:
                            pass

                    if not html:
                        logger.warning(
                            "Tier 3 Camoufox empty content for %s", url
                        )
                        return SpiderResult(
                            url=url,
                            error="Tier 3 Camoufox returned empty content",
                            fetch_tier="3",
                        )

                    # Validate that HTML contains actual article content, not just
                    # a CF challenge page or JS-rendered empty shell.
                    html_lower = html.lower()
                    has_article_tag = "<article" in html_lower
                    # Strip HTML tags to check text length
                    import re as _re
                    text_content = _re.sub(r"<[^>]+>", " ", html)
                    text_content = _re.sub(r"\s+", " ", text_content).strip()
                    min_text_chars = 200
                    
                    if not has_article_tag and len(text_content) < min_text_chars:
                        logger.warning(
                            "Tier 3 Camoufox got HTML but no article content for %s "
                            "(has_article=%s, text_chars=%d). Likely CF challenge page.",
                            url,
                            has_article_tag,
                            len(text_content),
                        )
                        # Log first 500 chars for debugging
                        logger.debug(
                            "Camoufox HTML snippet for %s: %s",
                            url,
                            html[:500],
                        )
                        return SpiderResult(
                            url=url,
                            error="Tier 3 Camoufox returned non-article content (likely CF challenge)",
                            fetch_tier="3",
                        )

                    # D13: write cookies back to the pool under (domain, "firefox135")
                    cookie_dict = {
                        c["name"]: c["value"]
                        for c in cookies
                        if c.get("name") and c.get("value") is not None
                    }
                    if "cf_clearance" in cookie_dict:
                        await pool_put(domain, CAMOUFOX_FINGERPRINT, cookie_dict)

                    logger.warning(
                        "Tier 3 Camoufox succeeded for %s (%d chars, article_tag=%s, text_chars=%d)",
                        url,
                        len(html),
                        has_article_tag,
                        len(text_content),
                    )
                    return SpiderResult(
                        url=url,
                        status=200,
                        html_content=html,
                        error=None,
                        used_stealth=True,
                        fetch_tier="3",
                    )
            except _PlaywrightTimeoutError:
                logger.warning(
                    "Tier 3 Camoufox timeout for %s after %ds", url, timeout
                )
                return SpiderResult(
                    url=url,
                    error=f"Tier 3 Camoufox timeout after {timeout}s",
                    fetch_tier="3",
                )
            except Exception as exc:
                logger.warning(
                    "Tier 3 Camoufox error for %s: %s (%s)",
                    url,
                    exc,
                    type(exc).__name__,
                )
                return SpiderResult(
                    url=url,
                    error=f"Tier 3 Camoufox error: {exc}",
                    fetch_tier="3",
                )
        finally:
            self._cloak_semaphore.release()

    # ── Blocked detection ─────────────────────────────────────────────

    async def is_blocked(self, response: Response) -> bool:
        """Always return False — blocked detection is handled in parse() callback.
        
        V5.0: We don't use Scrapling's retry mechanism because we handle
        CloakBrowser fallback directly in parse(). This avoids conflicts with
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
                "Tier 1 blocked for %s (status=%d, no HTML) — falling back to CloakBrowser",
                response.url,
                response.status,
            )
            return True

        if self._has_cloudflare_challenge(html):
            logger.warning(
                "Tier 1 blocked for %s (status=%d, CF challenge detected, html_len=%d) — falling back to CloakBrowser",
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
                        fetch_tier=item.get("fetch_tier", "1"),
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

    V7.0 funnel orchestration (D4/D5/D11):

    1. Tier 1 — ``spider.stream()`` (curl_cffi chrome146, pooled cookies injected)
    2. Tier 1.5 — retry blocked/connection-failed URLs with ``firefox135`` →
       ``safari15_5`` (independent ``FetcherSession`` instances)
    3. Tier 2 — remaining failures go to CloakBrowser (existing behavior),
       which harvests ``cf_clearance`` cookies into the pool on success
    4. Tier 3 — URLs that still failed after CloakBrowser are retried with
       Camoufox (ultimate fallback, Firefox engine + Juggler protocol)

    Semantic failures (e.g. HTTP 404 with content) never enter the retry set.

    Args:
        urls: URLs to fetch.
        timeout_ms: Request timeout in milliseconds.
        concurrent_requests: Global concurrency cap (set on spider instance).
        concurrent_per_domain: Per-domain concurrency cap (set on spider instance).

    Returns:
        List of ``SpiderResult``, one per input URL, in input order.
        Successful results carry ``fetch_tier`` = "1" | "1.5" | "2" | "3".
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
        # ── Cleanup: Close CloakBrowser to prevent zombie processes ──
        await spider._close_cloak_browser()

        # Explicitly close session manager to prevent "Event loop is closed"
        # errors on exit. Scrapling's stream() doesn't do this automatically.
        try:
            await spider._session_manager.close()
        except (RuntimeError, Exception):
            pass

    results = spider._collect_results(stream_items)

    # ── Tier 1.5: alternate TLS fingerprints ─────────────────────────
    # Only blocked / connection-failed URLs qualify. Semantic failures
    # (e.g. 404 with content) have no error and never enter the retry set.
    failed_urls = [
        (idx, r)
        for idx, r in enumerate(results)
        if r.error and not r.html_content
    ]
    if failed_urls:
        logger.info(
            "Tier 1.5: %d URLs failed Tier 1 — trying alternate TLS fingerprints",
            len(failed_urls),
        )
        remaining = await spider._retry_with_alt_fingerprints(results, failed_urls)
    else:
        remaining = []

    # ── Tier 2: CloakBrowser (existing behavior) ─────────────────────
    if remaining:
        logger.info(
            "Tier 2: %d URLs still failed after Tier 1.5 — falling back to CloakBrowser",
            len(remaining),
        )
        # Create a temporary spider just for CloakBrowser access
        retry_spider = NewsSpider(urls=[])
        try:
            # Initialize CloakBrowser
            await retry_spider._get_cloak_browser()

            # Retry each remaining URL with CloakBrowser
            retry_tasks = [
                retry_spider._fetch_with_cloak(r.url) for _, r in remaining
            ]
            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)

            # Update results with CloakBrowser responses
            for (idx, result), retry_result in zip(remaining, retry_results):
                if isinstance(retry_result, Exception):
                    logger.warning(
                        "Tier 2 CloakBrowser retry failed for %s: %s",
                        result.url,
                        retry_result,
                    )
                    continue

                html_content, error = retry_result
                if html_content:
                    results[idx] = SpiderResult(
                        url=result.url,
                        status=200,
                        html_content=html_content,
                        error=None,
                        used_stealth=True,
                        fetch_tier="2",
                    )
                    logger.info(
                        "Tier 2 CloakBrowser succeeded for %s (%d chars)",
                        result.url,
                        len(html_content),
                    )
        finally:
            await retry_spider._close_cloak_browser()

    # ── Tier 3: Camoufox ultimate fallback ───────────────────────────
    # Only URLs that failed ALL previous tiers (incl. CloakBrowser) qualify.
    tier3_urls = [
        (idx, r)
        for idx, r in enumerate(results)
        if r.error and not r.html_content
    ]
    if tier3_urls:
        logger.warning(
            "Tier 3: %d URLs failed Tier 2 — starting Camoufox fallback",
            len(tier3_urls),
        )
        for idx, result in tier3_urls:
            tier3_result = await spider._fetch_with_camoufox(
                result.url,
                timeout=CAMOUFOX_PAGE_TIMEOUT_SEC,
            )
            if tier3_result.html_content:
                results[idx] = tier3_result
                logger.warning(
                    "Tier 3 Camoufox recovered %s (%d chars)",
                    result.url,
                    len(tier3_result.html_content),
                )
            else:
                logger.warning(
                    "Tier 3 Camoufox failed for %s: %s",
                    result.url,
                    tier3_result.error,
                )

    return results


__all__ = [
    "NewsSpider",
    "SpiderResult",
    "fetch_urls_with_spider",
    "pool_get",
    "pool_put",
    "pool_invalidate",
    "COOKIE_TTL_SEC",
    "CAMOUFOX_PAGE_TIMEOUT_SEC",
    "TIER1_FINGERPRINT",
    "TIER15_FINGERPRINTS",
    "DEFAULT_CONCURRENT_REQUESTS",
    "DEFAULT_CONCURRENT_PER_DOMAIN",
    "DEFAULT_TIMEOUT_MS",
    "CLOAK_TIMEOUT_MS",
    "CLOAK_MAX_CONCURRENT",
    "BLOCKED_STATUS_CODES",
    "BATCH_SIZE",
]
