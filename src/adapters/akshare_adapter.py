"""AkShare Adapter — fetch individual stock news via akshare.

Uses ak.stock_news_em(symbol) to retrieve company-specific news from the
East Money (东方财富) news API. Supports multiple tickers with rate limiting.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode
from src.core.config import get_settings
from src.ingestion.severity_enricher import rule_based_severity
from src.utils.content_fetcher import ContentResult
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

logger = get_logger(__name__)

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
        return now_hkt()

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

    logger.warning("Could not parse AkShare time '%s', using current HKT time", ts)
    return now_hkt()


def _build_episode_body(title: str, content: str | None) -> str:
    """Build Markdown-formatted episode body from title and content."""
    body = f"## {title}\n\n"
    if content and content.strip():
        body += content.strip()
    return body


def _strip_html(text: str) -> str:
    """Remove all HTML tags and decode entities."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_keywords(title: str) -> list[str]:
    """Simple keyword extraction from title."""
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
        content_fetcher: Any | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.ticker_whitelist = ticker_whitelist or []
        self.rate_limit_sec = rate_limit_sec
        self._content_fetcher = content_fetcher
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
                    "title": row.get("新闻标题", row.get("title", "")),
                    "content": row.get("新闻内容", row.get("content", "")),
                    "time": row.get("发布时间", row.get("time", "")),
                    "source": row.get("文章来源", row.get("source", "")),
                    "link": row.get("新闻链接", row.get("link", "")),
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
                # Attach ticker metadata from whitelist
                # Whitelist entries have keys: ticker, sector, biz_code, name, exchange
                item["_ticker_name"] = meta.get("name", "")
                item["_ticker_full"] = meta.get("ticker", "")
                item["_ticker_sector"] = meta.get("sector", "")
                item["_ticker_exchange"] = meta.get("exchange", "")
            all_items.extend(items)
            # Rate limiting between symbols
            if self.rate_limit_sec > 0:
                await asyncio.sleep(self.rate_limit_sec)

        logger.info("Total AkShare items fetched: %d", len(all_items))
        return all_items

    # ── run override — batch fetch + normalize ──────────────────────

    async def run(self, **kwargs: Any) -> list[NormalizedEpisode]:
        """Full pipeline: fetch → batch content fetch → normalize → dedup.

        Phase 1: Fetch raw AkShare records.
        Phase 2: Batch-fetch all article links using ContentFetcher.
        Phase 3: Normalize each record (passing pre-fetched content).
        Phase 4: Dedup.
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
                    "Batch-fetched %d/%d AkShare article contents",
                    sum(1 for r in results if r.success),
                    len(results),
                )
            except Exception as exc:
                logger.warning(
                    "Batch content fetch failed for AkShare: %s — falling back to API summary",
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
        """Convert a single AkShare news item to NormalizedEpisode.

        If ``fetch_results`` is provided, uses the pre-fetched ContentResult
        to get full article text. Falls back to API summary on failure.
        """
        title = _strip_html(record.get("title", ""))
        api_summary = _strip_html(record.get("content", "")) or None
        link = record.get("link", "") or None
        symbol = record.get("symbol", "")
        ticker_name = record.get("_ticker_name", "")
        ticker_full = record.get("_ticker_full", "")
        ticker_sector = record.get("_ticker_sector", "")
        ticker_exchange = record.get("_ticker_exchange", "")

        # Prefer pre-fetched full text over API summary
        full_text: str | None = None
        if fetch_results and link and link in fetch_results:
            result = fetch_results[link]
            if result.success and result.text:
                full_text = result.text
            else:
                logger.debug(
                    "Pre-fetched content failed for %s: %s — using API summary",
                    link,
                    result.error,
                )

        # Build episode body: prefer full_text, fall back to API summary
        content = full_text or api_summary
        episode_body = _build_episode_body(title, content)
        severity = rule_based_severity(episode_body)
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = _parse_akshare_time(record.get("time"))
        keywords = _extract_keywords(title)

        # Date window cutoff — discard articles older than news_max_age_days
        settings = get_settings()
        max_age_days = settings.news_max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if valid_at < cutoff:
            return None  # type: ignore[return-value]

        # Build entity from whitelist metadata (includes sector/exchange hints)
        entities: list[EntityItem] = []
        if ticker_full:
            kwargs = {
                "type": "stock",
                "name": ticker_name or symbol,
                "ticker": ticker_full,
            }
            # Only attach sector/exchange if whitelist has non-empty values
            if ticker_sector:
                kwargs["sector"] = ticker_sector
            if ticker_exchange:
                kwargs["exchange"] = ticker_exchange
            entities.append(EntityItem(**kwargs))

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
            source_url=record.get("link") or None,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=keywords,
            entities=entities,
            metadata={"content_scope": "SYMBOL", "symbol": symbol},
        )
