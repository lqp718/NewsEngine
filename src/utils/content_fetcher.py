"""ContentFetcher — fetch and extract article body text from URLs.

Uses Scrapling Spider AsyncStealthySession for unified fetch+extract
with built-in anti-bot and Cloudflare bypass capabilities.

V3.0: Refactored to use Scrapling Spider framework exclusively.

Usage::
    fetcher = ContentFetcher()
    result = await fetcher.fetch_async("https://example.com/article")
    if result.success:
        print(result.text[:200])

    # Batch fetch
    results = await fetcher.fetch_batch([
        "https://example.com/a",
        "https://example.com/b",
    ])
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_TIMEOUT: int = 30
"""Default HTTP request timeout in seconds."""

DOMAIN_RATE_LIMIT_SEC: float = 2.0
"""Minimum gap (seconds) between requests to the same domain."""

MAX_CONCURRENT: int = 5
"""Max concurrent fetches in a batch."""

BATCH_SIZE: int = 50
"""Number of URLs per batch."""

BATCH_COOLDOWN_SEC: float = 5.0
"""Cooldown seconds between batches."""

SPIDER_MAX_PAGES: int = 5
"""Max pages per AsyncStealthySession instance."""

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 NewsEngine/1.0"
)
"""Standard User-Agent header."""


# ── Data classes ───────────────────────────────────────────────────────


@dataclass
class ContentResult:
    """Result of a content fetch operation.

    Attributes:
        url: The requested URL.
        text: Extracted article body text (or empty string on failure).
        success: Whether content extraction succeeded.
        engine: Name of the extraction engine used.
        error: Error message if any.
        content_length: Length of extracted text in characters.
    """

    url: str
    text: str = ""
    success: bool = False
    engine: str | None = None
    error: str | None = None
    content_length: int = 0

    def __post_init__(self) -> None:
        self.content_length = len(self.text)


# ── ContentFetcher ─────────────────────────────────────────────────────


class ContentFetcher:
    """Fetch and extract article body text from news article URLs.

    Uses Scrapling Spider AsyncStealthySession for unified crawling
    with automatic anti-bot detection bypass and Cloudflare solving.

    Rate-limited per domain with configurable minimum gap.
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        rate_limit: float = DOMAIN_RATE_LIMIT_SEC,
        max_concurrent: int = MAX_CONCURRENT,
        batch_size: int = BATCH_SIZE,
        batch_cooldown: float = BATCH_COOLDOWN_SEC,
    ) -> None:
        """Initialize ContentFetcher.

        Args:
            timeout: HTTP request timeout in seconds.
            rate_limit: Minimum seconds between requests to same domain.
            max_concurrent: Max concurrent fetches.
            batch_size: URLs per batch.
            batch_cooldown: Seconds between batches.
        """
        self._timeout = timeout
        self._rate_limit = rate_limit
        self._max_concurrent = max_concurrent
        self._batch_size = batch_size
        self._batch_cooldown = batch_cooldown
        self._domain_last_fetch: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ── Public sync fetch ─────────────────────────────────────────────

    def fetch(self, url: str) -> ContentResult:
        """Fetch and extract article body text (sync version).

        Args:
            url: The article URL to fetch.

        Returns:
            A ``ContentResult`` with extracted text or failure details.
        """
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                self._fetch_single(url)
            )
            return future.result(timeout=120)

    # ── Async methods ─────────────────────────────────────────────────

    async def fetch_async(self, url: str) -> ContentResult:
        """Fetch and extract article body text (async version).

        Args:
            url: The article URL to fetch.

        Returns:
            A ``ContentResult`` with extracted text or failure details.
        """
        return await self._fetch_single(url)

    async def fetch_batch(self, urls: list[str]) -> list[ContentResult]:
        """Fetch multiple URLs concurrently.

        Args:
            urls: List of article URLs to fetch.

        Returns:
            List of ``ContentResult`` in the same order as input URLs.
        """
        results: list[ContentResult] = []

        for batch_idx, batch in enumerate(self._split_batches(urls)):
            if batch_idx > 0:
                await asyncio.sleep(self._batch_cooldown)

            batch_results = await self._fetch_batch_internal(batch)
            results.extend(batch_results)

        return results

    # ── Internal: Unified fetch with Spider ───────────────────────────

    async def _fetch_single(self, url: str) -> ContentResult:
        """Fetch a single URL using Spider.

        Args:
            url: URL to fetch.

        Returns:
            ContentResult with extracted text.
        """
        await self._enforce_rate_limit(url)

        try:
            from scrapling.fetchers import AsyncStealthySession

            async with AsyncStealthySession(max_pages=1) as session:
                page = await session.fetch(url)
                if page:
                    return self._process_page(page, url)
                else:
                    return ContentResult(
                        url=url,
                        success=False,
                        error="AsyncStealthySession returned no page"
                    )

        except ImportError as exc:
            logger.error("AsyncStealthySession not available: %s", exc)
            return ContentResult(
                url=url,
                success=False,
                error=f"AsyncStealthySession not available: {exc}"
            )
        except Exception as exc:
            logger.warning("AsyncStealthySession fetch failed for %s: %s", url, exc)
            return ContentResult(
                url=url,
                success=False,
                error=str(exc)
            )

    async def _fetch_batch_internal(self, urls: list[str]) -> list[ContentResult]:
        """Fetch a batch of URLs using Spider with session reuse.

        Args:
            urls: URLs to fetch.

        Returns:
            List of ContentResult in input order.
        """
        try:
            from scrapling.fetchers import AsyncStealthySession

            async with AsyncStealthySession(max_pages=SPIDER_MAX_PAGES) as session:
                # Fetch all URLs concurrently using asyncio.gather
                tasks = [session.fetch(url) for url in urls]
                pages = await asyncio.gather(*tasks, return_exceptions=True)

                # Process results
                results: list[ContentResult] = []
                for i, url in enumerate(urls):
                    page = pages[i]
                    if page is not None and not isinstance(page, Exception):
                        result = self._process_page(page, url)
                        results.append(result)
                    else:
                        error_msg = str(page) if isinstance(page, Exception) else "No page returned"
                        results.append(ContentResult(
                            url=url,
                            success=False,
                            error=error_msg
                        ))

                return results

        except ImportError as exc:
            logger.error("AsyncStealthySession not available: %s", exc)
            return [
                ContentResult(url=url, success=False, error=f"AsyncStealthySession not available: {exc}")
                for url in urls
            ]
        except Exception as exc:
            logger.error("AsyncStealthySession batch fetch failed: %s", exc)
            return [
                ContentResult(url=url, success=False, error=str(exc))
                for url in urls
            ]

    def _process_page(self, page: Any, url: str) -> ContentResult:
        """Process a fetched page and extract content.

        Args:
            page: Scrapling Page object.
            url: Original URL.

        Returns:
            ContentResult with extracted text.
        """
        try:
            if hasattr(page, 'status') and page.status != 200:
                return ContentResult(
                    url=url,
                    success=False,
                    error=f"HTTP {page.status}"
                )

            html_content = page.html_content if hasattr(page, 'html_content') else None
            if not html_content:
                return ContentResult(
                    url=url,
                    success=False,
                    error="Empty HTML content"
                )

            # Extract with Trafilatura
            extracted = self._extract_content(html_content, url)

            if extracted and extracted.strip():
                logger.debug(
                    "Extracted %d chars from %s",
                    len(extracted.strip()),
                    url
                )
                return ContentResult(
                    url=url,
                    text=extracted.strip(),
                    success=True,
                    engine="spider+trafilatura"
                )
            else:
                return ContentResult(
                    url=url,
                    success=False,
                    error="Trafilatura returned empty content"
                )

        except Exception as exc:
            logger.warning("Content processing failed for %s: %s", url, exc)
            return ContentResult(
                url=url,
                success=False,
                error=str(exc)
            )

    def _extract_content(self, html: str, url: str) -> str | None:
        """Extract clean article text using Trafilatura.

        Args:
            html: HTML content.
            url: Source URL for context.

        Returns:
            Extracted text or None.
        """
        try:
            import trafilatura

            extracted = trafilatura.extract(
                html,
                url=url,
                no_fallback=False,
                favor_precision=True,
                output_format="txt"
            )
            return extracted

        except ImportError:
            logger.warning("Trafilatura not installed")
            return None
        except Exception as exc:
            logger.debug("Trafilatura extraction failed: %s", exc)
            return None

    # ── Internal: Rate limiting ───────────────────────────────────────

    async def _enforce_rate_limit(self, url: str) -> None:
        """Enforce minimum gap between requests to the same domain."""
        from urllib.parse import urlparse

        domain = urlparse(url).hostname or ""
        if not domain:
            return

        async with self._lock:
            last_fetch = self._domain_last_fetch.get(domain, 0.0)
            elapsed = time.monotonic() - last_fetch
            if elapsed < self._rate_limit:
                wait = self._rate_limit - elapsed
                logger.debug(
                    "Rate-limiting domain %s: waiting %.1fs",
                    domain,
                    wait
                )
                await asyncio.sleep(wait)
            self._domain_last_fetch[domain] = time.monotonic()

    def _split_batches(self, urls: list[str]) -> list[list[str]]:
        """Split URL list into batches.

        Args:
            urls: Full list of URLs.

        Returns:
            List of URL sub-lists.
        """
        return [
            urls[i:i + self._batch_size]
            for i in range(0, len(urls), self._batch_size)
        ]


__all__ = [
    "ContentFetcher",
    "ContentResult",
    "DEFAULT_TIMEOUT",
    "DOMAIN_RATE_LIMIT_SEC",
    "MAX_CONCURRENT",
    "BATCH_SIZE",
    "BATCH_COOLDOWN_SEC",
    "SPIDER_MAX_PAGES",
]