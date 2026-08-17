"""RSS Adapter — fetch, parse, and normalize RSS/Atom feed entries.

Supports multiple feed URLs, RSS 2.0 and Atom formats, and dedup by
link/guid.

V2.3: Added content relevance filtering (keyword whitelist + domain blacklist)
to filter out non-trading-related news (e.g., BBC general news).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import socket
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

# Default socket timeout for RSS feed fetches (seconds).
# Prevents indefinite hangs on unreachable feeds.
_RSS_SOCKET_TIMEOUT: int = 15

from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode
from src.core.config import get_settings
from src.ingestion.severity_enricher import rule_based_severity
from src.utils.content_fetcher import ContentResult
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt
from src.utils.yaml_parser import strip_yaml_front_matter

logger = get_logger(__name__)


def _extract_published(entry: dict[str, Any]) -> datetime:
    """Extract published date from a feed entry.

    Tries: published_parsed → updated_parsed → current UTC time.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    logger.warning(
        "Entry '%s' has no valid published date, using current UTC time",
        entry.get("title", "")[:50],
    )
    return now_hkt()


def _build_episode_body(title: str, summary: str | None) -> str:
    """Build a Markdown-formatted episode body."""
    body = f"## {title}\n\n"
    if summary and summary.strip():
        body += summary.strip()
    return body


_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "in", "on", "at", "for",
    "to", "of", "with", "by", "from", "as", "it", "its", "this", "that", "not", "but",
    "has", "have", "had", "be", "been", "being", "will", "can", "may", "could", "should",
    "would", "do", "does", "did", "if", "than", "then", "so", "no", "up", "out", "about",
    "into", "over", "after", "more", "most", "some", "any", "all", "each", "every", "new",
    "also", "how", "why", "what", "when", "where", "which", "who", "whom", "their", "our",
    "your", "his", "her", "we", "they", "you", "he", "she", "i", "me", "my", "us", "them",
    "s", "t", "re", "ve", "ll", "d", "m",  # contractions
})


def _extract_keywords(title: str, body: str) -> list[str]:
    """Extract meaningful keywords using TF-based scoring with stopword filtering."""
    # Weight title 2x
    text = f"{title} {title} {body}"
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]{3,}", text.lower())
    words = [w for w in words if w not in _STOP_WORDS]

    freq = Counter(words)
    return [w for w, _ in freq.most_common(8)]


def _extract_entities(title: str, body: str) -> list[dict]:
    """Extract entities from RSS content using pattern matching."""
    entities: list[dict] = []
    text = f"{title} {body}"

    # Stock tickers: $AAPL, $TSLA
    tickers = re.findall(r'\$([A-Z]{2,5})\b', text)
    for ticker in set(tickers):
        entities.append({"type": "stock", "name": ticker, "ticker": ticker, "sector": None, "exchange": None})

    # Countries/regions (curated list)
    countries = [
        "United States", "China", "European Union", "Japan", "Russia",
        "India", "UK", "Germany", "France", "Brazil", "Canada",
        "Australia", "South Korea",
    ]
    for country in countries:
        if re.search(rf'\b{re.escape(country)}\b', text, re.IGNORECASE):
            entities.append({"type": "country", "name": country, "ticker": None, "sector": None, "exchange": None})

    # Commodities
    commodities = {
        r"\b(?:crude\s+)?oil\b": "Crude Oil",
        r"\bgold\b": "Gold",
        r"\bsilver\b": "Silver",
        r"\bcopper\b": "Copper",
        r"\bnatural\s+gas\b": "Natural Gas",
        r"\bwheat\b": "Wheat",
        r"\bcorn\b": "Corn",
        r"\bsoybean\b": "Soybeans",
    }
    matched_commodities: set[str] = set()
    for pattern, name in commodities.items():
        if re.search(pattern, text, re.IGNORECASE) and name not in matched_commodities:
            entities.append({"type": "commodity", "name": name, "ticker": None, "sector": None, "exchange": None})
            matched_commodities.add(name)

    # Themes (financial concepts)
    themes = {
        r"\binflation\b": "Inflation",
        r"\binterest\s+rate\b": "Interest Rates",
        r"\bGDP\b": "GDP Growth",
        r"\bunemployment\b": "Unemployment",
        r"\brecession\b": "Recession",
        r"\btariff\b": "Tariffs",
        r"\btrade\s+war\b": "Trade War",
        r"\bsanction\b": "Sanctions",
        r"\bIPO\b": "IPO",
        r"\bmerger\b": "M&A",
    }
    matched_themes: set[str] = set()
    for pattern, name in themes.items():
        if re.search(pattern, text, re.IGNORECASE) and name not in matched_themes:
            entities.append({"type": "theme", "name": name, "ticker": None, "sector": None, "exchange": None})
            matched_themes.add(name)

    return entities[:10]  # Cap at 10 entities


# ── Content Relevance Filter ─────────────────────────────────────────

# Keywords that indicate trading/market relevance (case-insensitive)
RSS_RELEVANCE_KEYWORDS = [
    # English
    "fed", "ecb", "rate", "inflation", "gdp", "oil", "gold", "copper",
    "tariff", "trade", "market", "stock", "bond", "forex", "currency",
    "central bank", "monetary policy", "interest rate", "treasury",
    "commodity", "crude", "natural gas", "silver", "platinum",
    "geopolitical", "sanction", "embargo", "war", "conflict",
    "earnings", "revenue", "profit", "loss", "ipo", "merger", "acquisition",
    "recession", "growth", "employment", "unemployment", "cpi", "ppi",
    # Chinese
    "央行", "利率", "通胀", "关税", "贸易", "股市", "债券", "汇率",
    "原油", "黄金", "白银", "铜", "天然气", "大宗商品",
    "地缘政治", "制裁", "禁运", "战争", "冲突",
    "财报", "营收", "利润", "亏损", "ipo", "并购", "收购",
    "衰退", "增长", "就业", "失业", "cpi", "ppi",
]

# Domains to exclude (too general, not trading-focused)
RSS_BLACKLIST_DOMAINS = [
    "bbc.com",
    "bbc.co.uk",
    "bbci.co.uk",  # BBC RSS feeds subdomain
]


def _is_trading_relevant(title: str, summary: str | None, feed_url: str) -> bool:
    """Check if an RSS entry is relevant to trading/markets.

    Args:
        title: Article title
        summary: Article summary/description (optional)
        feed_url: Source feed URL

    Returns:
        True if relevant, False if should be filtered out
    """
    import re

    # Domain blacklist check
    for domain in RSS_BLACKLIST_DOMAINS:
        if domain in feed_url.lower():
            logger.debug(
                "RSS entry filtered by domain blacklist: %s (feed: %s)",
                title[:50],
                feed_url,
            )
            return False

    # Keyword whitelist check (title + summary)
    # Use word boundary matching to avoid false positives (e.g., "market" in "supermarket")
    text = (title + " " + (summary or "")).lower()
    for keyword in RSS_RELEVANCE_KEYWORDS:
        # Use word boundary \b for precise matching
        pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
        if re.search(pattern, text):
            return True

    # No keyword match — filter out
    logger.debug(
        "RSS entry filtered by keyword whitelist: %s",
        title[:50],
    )
    return False


class RssAdapter(BaseAdapter):
    """RSS/Atom feed adapter.

    Fetches one or more RSS feeds, parses entries, and normalizes to
    NormalizedEpisode with content_scope=MACRO.

    V2.2: Zero pre-ingestion filtering. All entries are preserved because
    RSS sources (MarketWatch + FT) are already curated financial content.
    """

    def __init__(
        self,
        feed_urls: list[str] | None = None,
        dedup_cache: set[str] | None = None,
        content_fetcher: Any | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.feed_urls = feed_urls or []
        self._content_fetcher = content_fetcher

    # ── feed fetching ────────────────────────────────────────────────

    def _fetch_single(self, feed_url: str) -> list[dict[str, Any]]:
        """Fetch and parse a single RSS/Atom feed.

        Args:
            feed_url: URL of the RSS/Atom feed.

        Returns:
            List of entry dicts, or empty list on failure.
        """
        try:
            logger.info("Fetching RSS feed: %s", feed_url)
            # Set socket timeout to prevent indefinite hangs
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(_RSS_SOCKET_TIMEOUT)
            try:
                # Use a browser-like User-Agent to avoid 403 blocks from sites like mining.com
                user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                parsed = feedparser.parse(feed_url, agent=user_agent)
            finally:
                socket.setdefaulttimeout(old_timeout)
        except Exception as exc:
            logger.warning(
                "Failed to fetch RSS feed %s: %s", feed_url, exc
            )
            return []

        # Check HTTP status — feedparser silently follows redirects but doesn't expose status
        http_status = parsed.get("status", 0)
        if http_status and http_status >= 400:
            logger.warning(
                "RSS feed %s returned HTTP %d — feed is likely dead or moved",
                feed_url,
                http_status,
            )
            return []

        if parsed.bozo and not parsed.entries:
            # Diagnose: HTML returned instead of XML is the most common cause
            bozo_exc = str(parsed.bozo_exception)
            content_type = parsed.get("content-type", "")
            if "not well-formed" in bozo_exc or "mismatched tag" in bozo_exc or "syntax error" in bozo_exc:
                logger.warning(
                    "RSS feed %s returned non-XML content (likely HTML or JSON). "
                    "Content-Type: %s. Feed URL may be dead — consider disabling it.",
                    feed_url,
                    content_type or "unknown",
                )
            else:
                logger.warning(
                    "RSS feed %s parse error (bozo): %s",
                    feed_url,
                    parsed.bozo_exception,
                )
            return []

        entries: list[dict[str, Any]] = []
        for raw_entry in parsed.entries:
            entry: dict[str, Any] = {
                "title": raw_entry.get("title", ""),
                "link": raw_entry.get("link", ""),
                "id": raw_entry.get("id", ""),
                "summary": raw_entry.get("summary", ""),
                "published": raw_entry.get("published", ""),
                "published_parsed": raw_entry.get("published_parsed"),
                "updated_parsed": raw_entry.get("updated_parsed"),
                "authors": raw_entry.get("authors", []),
                "feed_url": feed_url,
            }
            # Handle Atom feeds: content[0].value → summary
            if not entry["summary"]:
                content = raw_entry.get("content")
                if content and len(content) > 0:
                    entry["summary"] = content[0].get("value", "")
            # Handle Atom: updated → published fallback
            if not entry["published"]:
                entry["published"] = raw_entry.get("updated", "")
            entries.append(entry)

        logger.info(
            "Parsed %d entries from %s", len(entries), feed_url
        )
        return entries

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch entries from all configured RSS feeds.

        V2.3: Apply content relevance filtering (keyword whitelist + domain blacklist).
        """
        all_entries: list[dict] = []
        if not self.feed_urls:
            logger.warning("No RSS feed URLs configured")
            return []

        for feed_url in self.feed_urls:
            entries = self._fetch_single(feed_url)
            all_entries.extend(entries)

        # V2.3: Apply relevance filtering
        filtered_entries = [
            e for e in all_entries
            if _is_trading_relevant(
                e.get("title", ""),
                e.get("summary", ""),
                e.get("feed_url", ""),
            )
        ]

        logger.info(
            "RSS fetch: %d entries → %d after filtering (%.1f%% filtered)",
            len(all_entries),
            len(filtered_entries),
            (1 - len(filtered_entries) / len(all_entries)) * 100 if all_entries else 0,
        )
        return filtered_entries

    # ── run override — batch fetch + normalize ──────────────────────

    async def run(self, **kwargs: Any) -> list[NormalizedEpisode]:
        """Full pipeline: fetch → batch content fetch → normalize → dedup.

        Phase 1: Fetch raw RSS records.
        Phase 2: Batch-fetch all article links using NewsSpider.
        Phase 3: Normalize each record (passing pre-fetched content).
        Phase 4: Dedup.

        This avoids the overhead of creating a separate browser session
        per URL (saves ~3s startup per URL).
        """
        records = await self.fetch(**kwargs)

        # Phase 1: batch fetch all article links
        links = [r.get("link") for r in records if r.get("link")]
        fetch_results: dict[str, ContentResult] = {}
        if self._content_fetcher and links:
            try:
                results = await self._content_fetcher.fetch_batch(links)
                fetch_results = {r.url: r for r in results}
                logger.debug(
                    "Batch-fetched %d/%d RSS article contents",
                    sum(1 for r in results if r.success),
                    len(results),
                )
            except Exception as exc:
                logger.warning(
                    "Batch content fetch failed for RSS: %s — falling back to per-URL",
                    exc,
                )

        # Phase 2: normalize all records with pre-fetched content
        episodes = await asyncio.gather(
            *[self.normalize(r, fetch_results=fetch_results) for r in records],
        )
        episodes = [e for e in episodes if e is not None]
        return self.dedup(list(episodes))

    # ── normalization ────────────────────────────────────────────────

    async def normalize(
        self,
        record: dict,
        fetch_results: dict[str, ContentResult] | None = None,
    ) -> NormalizedEpisode:
        """Convert a single RSS entry to NormalizedEpisode.

        If ``self._content_fetcher`` is configured and ``fetch_results`` is
        provided, uses the pre-fetched ContentResult from the batch fetch
        to avoid per-URL browser startup overhead.

        When ``fetch_results`` is ``None``, falls back to single-URL fetch
        via ``self._content_fetcher.fetch_async()`` for backward compatibility.

        Sets content_scope=MACRO in metadata.
        """
        title = record.get("title", "")
        link = record.get("link", "") or None
        summary = record.get("summary", "") or None
        feed_url = record.get("feed_url", "unknown")

        # Date window cutoff — drop entries older than news_max_age_days
        settings = get_settings()
        valid_at_candidate = _extract_published(record)
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.news_max_age_days)
        if valid_at_candidate < cutoff:
            logger.debug(
                "RSS entry '%s' older than %d days — dropping",
                title[:50],
                settings.news_max_age_days,
            )
            return None

        # Feed name from URL
        feed_name = feed_url.split("/")[2] if "//" in feed_url else "feed"

        metadata: dict[str, Any] = {
            "content_scope": "MACRO",
            "feed_url": feed_url,
            "content_fetched": False,
        }

        # ContentFetcher enrichment
        # Plan: success → title + full_text; failure → title + summary (fallback)
        full_text: str | None = None
        if self._content_fetcher and link:
            # Prefer pre-fetched result from batch
            if fetch_results and link in fetch_results:
                result = fetch_results[link]
                if result.success and result.text:
                    full_text = result.text
                    metadata["content_fetched"] = True
                else:
                    logger.debug(
                        "Pre-fetched content failed for %s: %s — using feed summary only",
                        link,
                        result.error,
                    )
            else:
                try:
                    result = await self._content_fetcher.fetch_async(link)
                    if result.success and result.text:
                        full_text = result.text
                        metadata["content_fetched"] = True
                    else:
                        logger.debug(
                            "ContentFetcher failed for %s: %s — using feed summary only",
                            link,
                            result.error,
                        )
                except Exception as exc:
                    logger.debug(
                        "ContentFetcher error for %s: %s — using feed summary only",
                        link,
                        exc,
                    )

        # Build episode body: prefer full_text over summary (never mix both)
        if full_text:
            pure_text, yaml_meta = strip_yaml_front_matter(full_text)
            episode_body = _build_episode_body(title, pure_text)
            if yaml_meta:
                metadata["extracted_metadata"] = yaml_meta
        else:
            episode_body = _build_episode_body(title, summary)

        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = valid_at_candidate
        keywords = _extract_keywords(title, episode_body)

        # Severity via rule-based enricher
        severity = rule_based_severity(episode_body)

        name = NormalizedEpisode.make_name(
            source_type="rss",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=feed_name,
        )

        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description=f"RSS Feed: {feed_url}",
            source_type="rss",
            source_url=link,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=keywords,
            entities=_extract_entities(title, episode_body),
            metadata=metadata,
        )
