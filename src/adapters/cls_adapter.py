"""CLS (财联社) telegraph adapter — fetches real-time financial news.

V6.1: Phase 1 of stock news quality improvement.
Replaces EastMoney as the primary stock news source with higher-quality
financial telegraph from CLS (财联社).

API: cls.cn/v1/roll/get_roll_list
Auth: Local signature calculation (md5(sha1(sorted query string)))
Content: Full text in API response (no need to fetch article pages)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode
from src.core.config import get_settings
from src.ingestion.severity_enricher import rule_based_severity
from src.utils.logging_config import get_logger
from src.utils.yaml_parser import strip_yaml_front_matter

logger = get_logger(__name__)


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode entities."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


def _build_episode_body(title: str, content: str) -> str:
    """Build episode body from title and content."""
    if content and len(content) > len(title):
        return f"{title}\n\n{content}"
    return title


def _extract_keywords(title: str) -> list[str]:
    """Extract simple keywords from title (Chinese word segmentation lite)."""
    # Simple heuristic: split on common delimiters
    import re
    keywords = re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z]+", title)
    return [k for k in keywords if len(k) >= 2][:10]


def _parse_cls_time(ts: int | None) -> datetime:
    """Parse CLS timestamp (Unix epoch) to datetime."""
    if not ts:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class CLSAdapter(BaseAdapter):
    """CLS (财联社) telegraph adapter.

    Fetches real-time financial news from CLS telegraph API.
    Full text is returned directly in the API response — no need
    to fetch article pages separately.

    V6.1.1 (Phase 1.5): Enhanced entity extraction and metadata.
    - Uses API-returned stock_list for precise stock entity extraction
    - Extracts subjects (topic tags) into metadata
    - Uses CLS article ID for precise deduplication
    - Preserves level (A/B/C importance) in metadata for downstream reference

    Args:
        page_size: Number of articles to fetch per request (default: 50).
        dedup_cache: Shared dedup cache across adapters.
    """

    SOURCE_TYPE = "cls_telegraph"

    def __init__(
        self,
        page_size: int = 50,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.page_size = page_size
        # V6.1.1: ticker_whitelist kept for backward compatibility but not used
        # Entity extraction now uses API-returned stock_list
        self.ticker_whitelist: list[dict[str, str]] = []
        self._name_map: dict[str, dict[str, str]] = {}

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch raw CLS telegraph records.

        Returns:
            List of raw record dicts from CLS API.
        """
        records = await self._fetch_cls_telegraph()
        self._pre_filter_count = len(records)
        return records

    async def _fetch_cls_telegraph(self) -> list[dict]:
        """Fetch CLS telegraph using v1 API with local signature."""
        params = {
            "appName": "CailianpressWeb",
            "os": "web",
            "sv": "7.7.5",
            "last_time": "",
            "refresh_type": "1",
            "rn": str(self.page_size),
        }

        # Signature: md5(sha1(sorted query string))
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(
            hashlib.sha1(qs.encode()).hexdigest().encode()
        ).hexdigest()

        url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.cls.cn/",
        }

        try:
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.get(url, headers=headers, timeout=15),
            )
            response.raise_for_status()
            data = response.json()

            if data.get("errno") != 0:
                logger.warning("CLS API error: errno=%s", data.get("errno"))
                return []

            roll_data = data.get("data", {}).get("roll_data", []) or []
            logger.info("CLS telegraph: fetched %d records", len(roll_data))
            return roll_data

        except Exception as exc:
            logger.error("CLS telegraph fetch failed: %s", exc)
            return []

    async def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single CLS telegraph record to NormalizedEpisode.

        V6.1.1 (Phase 1.5):
        - Uses API-returned stock_list for precise entity extraction
        - Extracts subjects (topic tags) into metadata
        - Uses CLS article ID for precise deduplication
        - Preserves level (A/B/C importance) in metadata
        """
        title = _strip_html(record.get("title", "") or record.get("brief", ""))
        content = _strip_html(record.get("content", "") or record.get("brief", ""))
        link = record.get("shareurl", "") or None
        ts = record.get("ctime")
        article_id = record.get("id")  # V6.1.1: CLS article ID for dedup
        level = record.get("level", "")  # V6.1.1: A/B/C importance level
        stock_list = record.get("stock_list", [])  # V6.1.1: API-returned stock entities
        subjects = record.get("subjects", [])  # V6.1.1: topic tags

        # Build episode body
        episode_body = _build_episode_body(title, content)
        severity = rule_based_severity(episode_body)
        
        # V6.1.1: Use article_id for content_hash if available (precise dedup)
        if article_id:
            content_hash = hashlib.sha256(f"cls-{article_id}".encode("utf-8")).hexdigest()
        else:
            content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        
        valid_at = _parse_cls_time(ts)
        keywords = _extract_keywords(title)

        # Date window cutoff
        settings = get_settings()
        max_age_days = settings.news_max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if valid_at < cutoff:
            return None  # type: ignore[return-value]

        # V6.1.1: Extract entities from API-returned stock_list (precise)
        entities = self._extract_entities_from_stock_list(stock_list)
        
        # V6.1.1: Extract subject names for metadata
        subject_names = [s.get("subject_name", "") for s in (subjects or []) if s.get("subject_name")]

        name = NormalizedEpisode.make_name(
            source_type="cls_telegraph",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id="cls",
        )

        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description="CLS Telegraph (财联社电报)",
            source_type="cls_telegraph",
            source_url=link,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=keywords,
            entities=entities,
            metadata={
                "content_scope": "MACRO",  # CLS is market-wide, not stock-specific
                "adapter": "cls",
                "content_fetched": True,  # API returns full text
                # V6.1.1: Enhanced metadata
                "cls_article_id": article_id,  # Precise dedup key
                "cls_level": level,  # A/B/C importance level
                "cls_subjects": subject_names,  # Topic tags for filtering
                "cls_stock_count": len(stock_list),  # Number of related stocks
            },
        )

    def _extract_entities_from_stock_list(
        self, stock_list: list[dict]
    ) -> list[EntityItem]:
        """Extract stock entities from API-returned stock_list.

        V6.1.1: Uses CLS API's editor-annotated stock_list for precise
        entity extraction, replacing the old name_map text matching.

        Args:
            stock_list: List of stock dicts from CLS API, each containing:
                - name: Stock name (e.g., "兆易创新")
                - StockID: Stock code (e.g., "sh603986")
                - RiseRange: Price change % (e.g., 3.3)
                - last: Latest price
                - is_stib: Whether it's STAR Market (科创板)

        Returns:
            List of EntityItem with precise stock info.
        """
        entities: list[EntityItem] = []

        for stock in stock_list:
            name = stock.get("name", "")
            stock_id = stock.get("StockID", "")
            rise_range = stock.get("RiseRange")
            is_stib = stock.get("is_stib", False)

            if not name or not stock_id:
                continue

            # Parse exchange from StockID (e.g., "sh603986" -> "SH")
            exchange = ""
            if stock_id.startswith("sh"):
                exchange = "SH"
            elif stock_id.startswith("sz"):
                exchange = "SZ"

            kwargs: dict[str, Any] = {
                "type": "stock",
                "name": name,
                "ticker": stock_id.upper(),  # e.g., "SH603986"
            }
            if exchange:
                kwargs["exchange"] = exchange
            if is_stib:
                kwargs["sector"] = "STAR Market"  # 科创板

            entities.append(EntityItem(**kwargs))

        return entities


# Required import for asyncio
import asyncio


__all__ = ["CLSAdapter"]
