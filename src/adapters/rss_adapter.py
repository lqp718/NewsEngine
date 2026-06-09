"""RSS Adapter — fetch, parse, and normalize RSS/Atom feed entries.

Supports multiple feed URLs, RSS 2.0 and Atom formats, ticker whitelist
filtering, and dedup by link/guid.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import feedparser

from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode

logger = logging.getLogger(__name__)


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
    return datetime.now(timezone.utc)


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

    Fetches one or more RSS feeds, parses entries, filters by ticker
    whitelist, and normalizes to NormalizedEpisode.
    """

    def __init__(
        self,
        feed_urls: list[str] | None = None,
        ticker_whitelist: list[dict[str, str]] | None = None,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.feed_urls = feed_urls or []
        self.ticker_whitelist = ticker_whitelist or []

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
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            logger.warning(
                "Failed to fetch RSS feed %s: %s", feed_url, exc
            )
            return []

        if parsed.bozo and not parsed.entries:
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
        """Fetch entries from all configured RSS feeds."""
        all_entries: list[dict] = []
        if not self.feed_urls:
            logger.warning("No RSS feed URLs configured")
            return []

        for feed_url in self.feed_urls:
            entries = self._fetch_single(feed_url)
            all_entries.extend(entries)

        # Apply ticker whitelist filter
        all_entries = self.filter_relevant(all_entries)
        return all_entries

    # ── filtering ────────────────────────────────────────────────────

    def filter_relevant(
        self, entries: list[dict]
    ) -> list[dict]:
        """Filter entries by ticker whitelist keywords.

        Searches title and summary for whitelist keywords.
        Returns all entries if whitelist is empty.
        """
        if not self.ticker_whitelist:
            return entries

        keywords: set[str] = set()
        for entry in self.ticker_whitelist:
            if "biz_code" in entry and entry["biz_code"]:
                keywords.add(entry["biz_code"])
            if "name_zh" in entry and entry["name_zh"]:
                keywords.add(entry["name_zh"])
            if "name_en" in entry and entry["name_en"]:
                keywords.add(entry["name_en"])

        if not keywords:
            return entries

        matched: list[dict] = []
        for entry in entries:
            search_text = " ".join(
                [
                    entry.get("title", ""),
                    entry.get("summary", ""),
                ]
            ).lower()
            if any(kw.lower() in search_text for kw in keywords):
                matched.append(entry)

        logger.info(
            "Filtered %d → %d entries by ticker whitelist",
            len(entries),
            len(matched),
        )
        return matched

    # ── normalization ────────────────────────────────────────────────

    def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single RSS entry to NormalizedEpisode."""
        title = record.get("title", "")
        link = record.get("link", "") or None
        summary = record.get("summary", "") or None
        feed_url = record.get("feed_url", "unknown")

        episode_body = _build_episode_body(title, summary)
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = _extract_published(record)
        keywords = _extract_keywords(title)

        # Feed name from URL
        feed_name = feed_url.split("/")[2] if "//" in feed_url else "feed"

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
            metadata={"feed_url": feed_url},
        )
