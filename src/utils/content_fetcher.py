"""ContentFetcher — fetch and extract article body text from URLs.

Uses NewsSpider (built on Scrapling FetcherSession + AsyncStealthySession)
for unified fetch+extract with built-in anti-bot and Cloudflare bypass capabilities.

V5.0: Tier 0 static extraction — before falling back to Trafilatura, try to
pull article text from in-page structured data:

- ``extract_next_data`` — ``<script id="__NEXT_DATA__">`` JSON (Next.js sites)
- ``extract_json_ld`` — ``application/ld+json`` blocks (NewsArticle/Article)

Tier 0 runs on the HTML of ANY fetch tier, improving extraction quality for
Next.js / structured-data sites and reducing reliance on Trafilatura heuristics.

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
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from src.utils.news_spider import (
    DEFAULT_CONCURRENT_PER_DOMAIN,
    DEFAULT_CONCURRENT_REQUESTS,
    DEFAULT_TIMEOUT_MS,
    SpiderResult,
    fetch_urls_with_spider,
)
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

DEFAULT_TIMEOUT: int = 30
"""Default HTTP request timeout in seconds."""

TIER0_MIN_TEXT_LEN: int = 200
"""Minimum article-text length (chars) for Tier 0 static extraction to be a hit.

Prevents navigation snippets / short summaries from being treated as article
bodies (D3). Below this threshold the pipeline falls back to Trafilatura.
"""

_MAX_TIER0_SEARCH_DEPTH: int = 8
"""Max nesting depth for the recursive __NEXT_DATA__ path search."""

_NEXT_DATA_PREFERRED_KEYS: frozenset[str] = frozenset(
    {
        "articlebody",
        "article_body",
        "bodyhtml",
        "articlebodyhtml",
        "articletext",
        "maincontent",
        "contentbody",
        "article_content",
    }
)
"""Leaf keys that are strongly preferred when searching __NEXT_DATA__ JSON."""

_JSONLD_ARTICLE_TYPES: frozenset[str] = frozenset(
    {"newsarticle", "article", "blogposting"}
)
"""JSON-LD @type values that count as article nodes."""

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

DOMAIN_RATE_LIMIT_SEC: float = 2.0
"""Minimum gap (seconds) between requests to the same domain."""

MAX_CONCURRENT: int = 5
"""Max concurrent fetches in a batch."""

BATCH_SIZE: int = 50
"""Number of URLs per batch."""

BATCH_COOLDOWN_SEC: float = 1.0
"""Cooldown seconds between batches."""

BATCH_TIMEOUT_SEC: float = 180.0
"""Default timeout in seconds for each batch of URLs."""

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


# ── Tier 0: static extraction ──────────────────────────────────────────


def _strip_html_tags(text: str) -> str:
    """Remove HTML tags and collapse whitespace in a candidate string."""
    cleaned = _HTML_TAG_RE.sub(" ", text)
    cleaned = _WS_RE.sub(" ", cleaned)
    return cleaned.strip()


def _walk_json_texts(
    node: Any,
    depth: int,
    path: tuple[str, ...],
    preferred: list[str],
    all_strings: list[str],
) -> None:
    """Recursively collect string leaves from parsed JSON.

    Args:
        node: Current JSON node (dict/list/str/scalar).
        depth: Current nesting depth (capped by ``_MAX_TIER0_SEARCH_DEPTH``).
        path: Key path leading to this node.
        preferred: Output list for strings under preferred article keys.
        all_strings: Output list for every string ≥ ``TIER0_MIN_TEXT_LEN``.
    """
    if depth > _MAX_TIER0_SEARCH_DEPTH or node is None:
        return
    if isinstance(node, str):
        if len(node) < TIER0_MIN_TEXT_LEN:
            return
        all_strings.append(node)
        leaf = path[-1].lower() if path else ""
        parent = path[-2].lower() if len(path) >= 2 else ""
        if leaf in _NEXT_DATA_PREFERRED_KEYS or (
            parent in ("article", "page", "post") and leaf in ("body", "content")
        ):
            preferred.append(node)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            _walk_json_texts(value, depth + 1, path + (key,), preferred, all_strings)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_json_texts(
                item, depth + 1, path + (f"[{index}]",), preferred, all_strings
            )


def _search_next_data_text(data: Any) -> str | None:
    """Locate the best article-text candidate inside parsed __NEXT_DATA__ JSON.

    Prefers strings under known article keys (``articleBody``, ``article.body``,
    ``bodyHtml``, ...), otherwise falls back to the longest qualifying string
    leaf. The quality threshold (``TIER0_MIN_TEXT_LEN``) is the final guard.
    """
    preferred: list[str] = []
    all_strings: list[str] = []
    _walk_json_texts(data, 0, (), preferred, all_strings)

    pool = preferred if preferred else all_strings
    if not pool:
        return None
    best = max(pool, key=len)
    if "<" in best:
        best = _strip_html_tags(best)
    if not best or len(best) < TIER0_MIN_TEXT_LEN:
        return None
    return best


def extract_next_data(html: str) -> str | None:
    """Tier 0a — extract article text from ``<script id="__NEXT_DATA__">``.

    Locates the script block with ``re``, parses its JSON with the standard
    library ``json`` module, then recursively searches the JSON for article
    body candidates. If ``json.loads`` fails, ``chompjs`` is tried as a lazy
    fallback (skipped silently when not installed).

    Args:
        html: Raw page HTML.

    Returns:
        Extracted article text (≥ ``TIER0_MIN_TEXT_LEN`` chars) or ``None``
        when the page has no usable ``__NEXT_DATA__``.
    """
    if not html:
        return None

    match = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None

    raw = match.group(1).strip()
    if not raw:
        return None

    data: Any = None
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        # Lazy chompjs fallback — silently skipped when not installed (D2)
        try:
            import chompjs  # noqa: PLC0415
        except ImportError:
            chompjs = None
        if chompjs is not None:
            try:
                data = chompjs.parse_js_object(raw)
            except Exception:
                data = None

    if not isinstance(data, dict):
        return None
    return _search_next_data_text(data)


def _jsonld_text_from_node(node: dict[str, Any]) -> str | None:
    """Extract article text from a single JSON-LD node.

    Only NewsArticle / Article / BlogPosting nodes qualify. ``articleBody``
    is preferred over ``description``; both must pass the quality threshold.
    """
    node_type = node.get("@type")
    if isinstance(node_type, list):
        types = {str(t).lower() for t in node_type if t}
    else:
        types = {str(node_type).lower()} if node_type else set()

    if not types or not (types & _JSONLD_ARTICLE_TYPES):
        return None

    for key in ("articleBody", "article_body", "articlebody"):
        value = node.get(key)
        if isinstance(value, str) and len(value) >= TIER0_MIN_TEXT_LEN:
            return value

    description = node.get("description")
    if isinstance(description, str) and len(description) >= TIER0_MIN_TEXT_LEN:
        return description

    return None


def _iter_jsonld_nodes(data: Any):
    """Yield article-candidate dict nodes from a parsed JSON-LD payload.

    Handles: a single node dict, an array of nodes, and ``@graph`` expansion.
    """
    if isinstance(data, dict):
        if isinstance(data.get("@graph"), list):
            for node in data["@graph"]:
                if isinstance(node, dict):
                    yield node
        yield data
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                if isinstance(item.get("@graph"), list):
                    for node in item["@graph"]:
                        if isinstance(node, dict):
                            yield node
                yield item


def extract_json_ld(html: str) -> str | None:
    """Tier 0b — extract article text from ``application/ld+json`` blocks.

    Parses every ``<script type="application/ld+json">`` block, expands
    ``@graph`` nesting, and returns the first NewsArticle/Article/BlogPosting
    candidate whose ``articleBody`` (or ``description``) passes the quality
    threshold.

    Args:
        html: Raw page HTML.

    Returns:
        Extracted article text or ``None`` when no block qualifies.
    """
    if not html:
        return None

    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for block in blocks:
        raw = block.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            continue

        for node in _iter_jsonld_nodes(data):
            text = _jsonld_text_from_node(node)
            if text:
                return text
    return None


# ── ContentFetcher ─────────────────────────────────────────────────────


class ContentFetcher:
    """Fetch and extract article body text from news article URLs.

    Uses NewsSpider for concurrent fetching with automatic anti-bot
    detection bypass and Cloudflare solving.

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
        self._timeout_ms = timeout * 1000
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
                self._fetch_single(url),
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

    async def fetch_batch(
        self, urls: list[str], batch_timeout: float | None = BATCH_TIMEOUT_SEC
    ) -> list[ContentResult]:
        """Fetch multiple URLs using NewsSpider.

        Args:
            urls: List of article URLs to fetch.
            batch_timeout: Optional timeout in seconds for each batch.
                If None, no timeout is applied.

        Returns:
            List of ``ContentResult`` in the same order as input URLs.
        """
        results: list[ContentResult] = []

        for batch_idx, batch in enumerate(self._split_batches(urls)):
            if batch_idx > 0:
                await asyncio.sleep(self._batch_cooldown)

            try:
                if batch_timeout:
                    batch_results = await asyncio.wait_for(
                        self._fetch_batch_internal(batch),
                        timeout=batch_timeout,
                    )
                else:
                    batch_results = await self._fetch_batch_internal(batch)
                results.extend(batch_results)
            except asyncio.TimeoutError:
                logger.warning(
                    "Batch %d/%d timed out after %.1fs, skipping %d URLs",
                    batch_idx + 1,
                    len(self._split_batches(urls)),
                    batch_timeout,
                    len(batch),
                )
                # Add failure results for skipped URLs
                for url in batch:
                    results.append(
                        ContentResult(
                            url=url,
                            success=False,
                            error=f"Batch timeout after {batch_timeout}s",
                        )
                    )

        return results

    # ── Internal: Unified fetch with NewsSpider ──────────────────────

    async def _fetch_single(self, url: str) -> ContentResult:
        """Fetch a single URL using NewsSpider.

        Args:
            url: URL to fetch.

        Returns:
            ContentResult with extracted text.
        """
        await self._enforce_rate_limit(url)

        try:
            spider_results = await fetch_urls_with_spider(
                urls=[url],
                timeout_ms=self._timeout_ms,
                concurrent_requests=10,
                concurrent_per_domain=2,
            )
            return self._spider_result_to_content(spider_results[0], url)

        except ImportError as exc:
            logger.error("NewsSpider dependencies not available: %s", exc)
            return ContentResult(
                url=url,
                success=False,
                error=f"NewsSpider dependencies not available: {exc}",
            )
        except Exception as exc:
            logger.warning("NewsSpider fetch failed for %s: %s", url, exc)
            return ContentResult(
                url=url,
                success=False,
                error=str(exc),
            )

    async def _fetch_batch_internal(self, urls: list[str]) -> list[ContentResult]:
        """Fetch a batch of URLs using NewsSpider.

        Args:
            urls: URLs to fetch.

        Returns:
            List of ContentResult in input order.
        """
        try:
            spider_results = await fetch_urls_with_spider(
                urls=urls,
                timeout_ms=self._timeout_ms,
                concurrent_requests=10,
                concurrent_per_domain=2,
            )
            return [
                self._spider_result_to_content(spider_result, url)
                for spider_result, url in zip(spider_results, urls)
            ]

        except ImportError as exc:
            logger.error("NewsSpider not available: %s", exc)
            return [
                ContentResult(
                    url=url, success=False, error=f"NewsSpider not available: {exc}",
                )
                for url in urls
            ]
        except Exception as exc:
            logger.error("NewsSpider batch fetch failed: %s", exc)
            return [
                ContentResult(url=url, success=False, error=str(exc))
                for url in urls
            ]

    def _spider_result_to_content(
        self, spider_result: SpiderResult, url: str
    ) -> ContentResult:
        """Convert a SpiderResult to a ContentResult with text extraction.

        Args:
            spider_result: Raw result from NewsSpider.
            url: Original URL.

        Returns:
            ContentResult with extracted article text.
        """
        # Check fetch-level failure (no HTML content)
        if spider_result.error:
            return ContentResult(
                url=url,
                success=False,
                error=spider_result.error,
            )

        if spider_result.status != 200:
            return ContentResult(
                url=url,
                success=False,
                error=f"HTTP {spider_result.status}",
            )

        html_content = spider_result.html_content
        if not html_content:
            return ContentResult(
                url=url,
                success=False,
                error="Empty HTML content",
            )

        # Tier 0a: __NEXT_DATA__ static extraction (before Trafilatura)
        text = extract_next_data(html_content)
        if text:
            logger.debug(
                "Tier 0 (__NEXT_DATA__) extracted %d chars from %s",
                len(text),
                url,
            )
            return ContentResult(
                url=url,
                text=text.strip(),
                success=True,
                engine="tier0_next_data",
            )

        # Tier 0b: JSON-LD static extraction (before Trafilatura)
        text = extract_json_ld(html_content)
        if text:
            logger.debug(
                "Tier 0 (JSON-LD) extracted %d chars from %s",
                len(text),
                url,
            )
            return ContentResult(
                url=url,
                text=text.strip(),
                success=True,
                engine="tier0_jsonld",
            )

        # Fallback: extract with Trafilatura
        extracted = self._extract_content(html_content, url)
        if extracted and extracted.strip():
            engine = "news_spider+trafilatura"
            text = extracted.strip()
                        
            logger.debug(
                "Extracted %d chars from %s%s",
                len(text),
                url,
                " (stealth)" if spider_result.used_stealth else "",
            )
            return ContentResult(
                url=url,
                text=text,
                success=True,
                engine=engine,
            )
        else:
            return ContentResult(
                url=url,
                success=False,
                error="Trafilatura returned empty content",
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

            # First attempt: favor_precision=True for clean extraction
            result = trafilatura.extract_with_metadata(
                html,
                url=url,
                favor_precision=True,
                include_tables=True,
                output_format="txt",
            )
            # extract_with_metadata 返回 Document 对象
            text = None
            if hasattr(result, 'text'):
                text = result.text
            elif result:
                text = str(result)
            
            # Fallback: if precision mode failed, try with favor_precision=False
            # This helps when Camoufox got real content but trafilatura is too strict
            if not text or not text.strip():
                logger.warning(
                    "Trafilatura precision mode failed for %s, trying fallback",
                    url,
                )
                result = trafilatura.extract_with_metadata(
                    html,
                    url=url,
                    favor_precision=False,
                    include_tables=True,
                    output_format="txt",
                    min_extracted_size=50,  # Lower threshold
                )
                if hasattr(result, 'text'):
                    text = result.text
                elif result:
                    text = str(result)
            
            # If still empty, log warning with HTML snippet for debugging
            if not text or not text.strip():
                logger.warning(
                    "Trafilatura returned empty content for %s. HTML snippet: %s",
                    url,
                    html[:300] if html else "(empty)",
                )
                return None
            
            return text

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
                    wait,
                )
                await asyncio.sleep(wait)
            self._domain_last_fetch[domain] = time.monotonic()

    def _split_batches(self, urls: list[str]) -> list[list[str]]:
        """Split URL list into domain-grouped batches for Cloudflare cookie reuse.

        Groups URLs by domain so each batch contains URLs from the same domain.
        This maximizes Cloudflare cookie reuse — the first request in a batch
        triggers Cloudflare verification, subsequent requests in the same batch
        reuse the cookies.

        Args:
            urls: Full list of URLs.

        Returns:
            List of URL sub-lists, grouped by domain.
        """
        from urllib.parse import urlparse
        from collections import defaultdict

        # Group URLs by domain
        domain_groups: dict[str, list[str]] = defaultdict(list)
        for url in urls:
            try:
                domain = urlparse(url).netloc
            except Exception:
                domain = "unknown"
            domain_groups[domain].append(url)

        # Convert to list of batches
        # Each batch is a list of URLs from the same domain
        batches = list(domain_groups.values())

        return batches


__all__ = [
    "ContentFetcher",
    "ContentResult",
    "extract_next_data",
    "extract_json_ld",
    "TIER0_MIN_TEXT_LEN",
    "DEFAULT_TIMEOUT",
    "DOMAIN_RATE_LIMIT_SEC",
    "MAX_CONCURRENT",
    "BATCH_SIZE",
    "BATCH_COOLDOWN_SEC",
    "SPIDER_MAX_PAGES",
]
