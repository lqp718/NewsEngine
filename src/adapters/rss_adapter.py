"""RSS Adapter — fetch, parse, and normalize RSS/Atom feed entries.

Supports multiple feed URLs, RSS 2.0 and Atom formats, and dedup by
link/guid.

V2.2: Removed ticker whitelist filtering. RSS now applies zero
pre-ingestion filtering — all entries are preserved.
"""

from __future__ import annotations

import hashlib
import socket
from datetime import datetime, timezone
from typing import Any

import feedparser

# Default socket timeout for RSS feed fetches (seconds).
# Prevents indefinite hangs on unreachable feeds.
_RSS_SOCKET_TIMEOUT: int = 15

from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

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


def _extract_keywords(title: str) -> list[str]:
    """Simple keyword extraction from title."""
    import re
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]+", title)
    # Return shorter words list as keywords
    return words[:8]


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
                parsed = feedparser.parse(feed_url)
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

        V2.2: Zero pre-ingestion filtering. Every entry is preserved.
        """
        all_entries: list[dict] = []
        if not self.feed_urls:
            logger.warning("No RSS feed URLs configured")
            return []

        for feed_url in self.feed_urls:
            entries = self._fetch_single(feed_url)
            all_entries.extend(entries)

        logger.info(
            "RSS fetch: %d entries from %d feeds (no filtering)",
            len(all_entries),
            len(self.feed_urls),
        )
        return all_entries

    # ── normalization ────────────────────────────────────────────────

    async def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single RSS entry to NormalizedEpisode.

        If ``self._content_fetcher`` is configured, attempts to fetch
        the full article body from the entry's ``link`` URL and append
        it to the episode body.

        Sets content_scope=MACRO in metadata.
        """
        title = record.get("title", "")
        link = record.get("link", "") or None
        summary = record.get("summary", "") or None
        feed_url = record.get("feed_url", "unknown")

        # Feed name from URL
        feed_name = feed_url.split("/")[2] if "//" in feed_url else "feed"

        metadata: dict[str, Any] = {
            "content_scope": "MACRO",
            "feed_url": feed_url,
            "content_fetched": False,
        }

        # ContentFetcher enrichment (V2.4: replace summary with full text on success)
        # Plan: success → title + full_text; failure → title + summary (fallback)
        # V2.5: Use fetch_async() to avoid asyncio.run() in existing event loop
        full_text: str | None = None
        if self._content_fetcher and link:
            try:
                result = await self._content_fetcher.fetch_async(link)
                if result.success and result.text:
                    full_text = result.text
                    metadata["content_fetched"] = True
                else:
                    # Failure fallback: use summary only
                    logger.debug(
                        "ContentFetcher failed for %s: %s — using feed summary only",
                        link,
                        result.error,
                    )
            except Exception as exc:
                # Exception fallback: use summary only
                logger.debug(
                    "ContentFetcher error for %s: %s — using feed summary only",
                    link,
                    exc,
                )

        # Build episode body: prefer full_text over summary (never mix both)
        if full_text:
            episode_body = _build_episode_body(title, full_text)
        else:
            episode_body = _build_episode_body(title, summary)

        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = _extract_published(record)
        keywords = _extract_keywords(title)

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
            severity="medium",
            keywords=keywords,
            entities=[],
            metadata=metadata,
        )
