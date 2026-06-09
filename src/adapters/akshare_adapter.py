"""AkShare Adapter — fetch individual stock news via akshare.

Uses ak.stock_news_em(symbol) to retrieve company-specific news from the
East Money (东方财富) news API. Supports multiple tickers with rate limiting.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode

logger = logging.getLogger(__name__)

# Mapping of stock exchange prefixes
EXCHANGE_PREFIX: dict[str, str] = {
    "HK": "HK",
    "SH": "SS",
    "SZ": "SZ",
    "US": "",  # US stocks typically just have the ticker
}


def _parse_akshare_time(time_val: Any) -> datetime:
    """Parse AkShare time field to UTC datetime.

    AkShare returns time in various formats; we attempt common ones.
    Falls back to current UTC time on parse failure.
    """
    if time_val is None:
        return datetime.now(timezone.utc)

    # If it's already a datetime-like object from pandas
    if hasattr(time_val, "to_pydatetime"):
        try:
            dt = time_val.to_pydatetime()
            # Handle naive datetime
            if dt.tzinfo is None:
                # AkShare returns times in China Standard Time (UTC+8)
                # Convert to UTC
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                return dt.astimezone(timezone.utc)
            return dt
        except (AttributeError, ValueError, TypeError):
            pass

    ts = str(time_val).strip()
    # Try common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt)
            # Assume CST (UTC+8)
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    logger.warning("Could not parse AkShare time '%s', using current UTC time", ts)
    return datetime.now(timezone.utc)


def _build_episode_body(title: str, content: str | None) -> str:
    """Build Markdown-formatted episode body from title and content."""
    body = f"## {title}\n\n"
    if content and content.strip():
        body += content.strip()
    return body


def _extract_keywords(title: str) -> list[str]:
    """Simple keyword extraction from title."""
    import re
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]+", title)
    return words[:8]


class AkShareAdapter(BaseAdapter):
    """AkShare stock news adapter.

    Fetches news for each stock in the ticker whitelist using
    ak.stock_news_em(symbol), normalizes to NormalizedEpisode,
    and enriches with stock entity metadata.
    """

    def __init__(
        self,
        ticker_whitelist: list[dict[str, str]] | None = None,
        rate_limit_sec: float = 0.5,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.ticker_whitelist = ticker_whitelist or []
        self.rate_limit_sec = rate_limit_sec
        # Build symbol → metadata map from whitelist
        self._symbol_map: dict[str, dict[str, str]] = {}
        for entry in self.ticker_whitelist:
            biz_code = entry.get("biz_code", "")
            if biz_code:
                self._symbol_map[biz_code] = entry

    # ── single symbol fetch ──────────────────────────────────────────

    def _fetch_single(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch news for a single stock symbol via ak.stock_news_em().

        Args:
            symbol: AkShare biz_code (e.g. "00700" for Tencent).

        Returns:
            List of news item dicts, or empty list on failure.
        """
        try:
            import akshare as ak

            logger.info("Fetching AkShare news for symbol %s", symbol)
            df = ak.stock_news_em(symbol=symbol)

            if df is None or df.empty:
                logger.info("No news returned for symbol %s", symbol)
                return []

            items: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                item = {
                    "title": row.get("标题", row.get("title", "")),
                    "content": row.get("内容", row.get("content", "")),
                    "time": row.get("发布时间", row.get("time", "")),
                    "source": row.get("来源", row.get("source", "")),
                    "symbol": symbol,
                }
                items.append(item)

            logger.info("Fetched %d news items for symbol %s", len(items), symbol)
            return items

        except ImportError:
            logger.error("akshare library not installed; run: pip install akshare")
            return []
        except Exception as exc:
            logger.warning(
                "Failed to fetch AkShare news for symbol %s: %s", symbol, exc
            )
            return []

    # ── batch fetch ──────────────────────────────────────────────────

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch news for all whitelisted symbols with rate limiting."""
        all_items: list[dict] = []

        for symbol, meta in self._symbol_map.items():
            items = self._fetch_single(symbol)
            for item in items:
                # Attach ticker metadata
                item["_ticker_name"] = meta.get("name", "")
                item["_ticker_full"] = meta.get("symbol", "")
            all_items.extend(items)
            # Rate limiting between symbols
            if self.rate_limit_sec > 0:
                await asyncio.sleep(self.rate_limit_sec)

        logger.info("Total AkShare items fetched: %d", len(all_items))
        return all_items

    # ── normalization ────────────────────────────────────────────────

    def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single AkShare news item to NormalizedEpisode."""
        title = record.get("title", "")
        content = record.get("content", "") or None
        symbol = record.get("symbol", "")
        ticker_name = record.get("_ticker_name", "")
        ticker_full = record.get("_ticker_full", "")

        episode_body = _build_episode_body(title, content)
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = _parse_akshare_time(record.get("time"))
        keywords = _extract_keywords(title)

        # Build entity from whitelist metadata
        entities: list[EntityItem] = []
        if ticker_full:
            entities.append(
                EntityItem(
                    type="stock",
                    name=ticker_name or symbol,
                    ticker=ticker_full,
                )
            )

        name = NormalizedEpisode.make_name(
            source_type="akshare",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=symbol,
        )

        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description=f"AkShare Stock News: {symbol}",
            source_type="akshare",
            source_url=None,
            valid_at=valid_at,
            content_hash=content_hash,
            severity="medium",
            keywords=keywords,
            entities=entities,
            metadata={"symbol": symbol},
        )
