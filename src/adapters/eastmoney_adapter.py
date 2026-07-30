"""EastMoney Adapter — fetch individual stock news via East Money search API.

Uses stock name (Chinese) as keyword to search the East Money API directly,
returning up to ``pageSize`` results per stock. Uses curl_cffi to bypass
anti-crawl protections.

Fallback chain in scheduler:
    EastMoneyAdapter (name search, 20 results)
      -> AkShareAdapter (symbol search, 10 results)
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode
from src.core.config import get_settings
from src.ingestion.severity_enricher import rule_based_severity
from src.utils.content_fetcher import ContentResult
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt
from src.utils.yaml_parser import strip_yaml_front_matter

logger = get_logger(__name__)

# ── API constants ──────────────────────────────────────────────────────

_EASTMONEY_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"

# Browser-like headers to avoid being blocked
_HEADERS_TEMPLATE = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en,zh-CN;q=0.9,zh;q=0.8",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "script",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
}


def _strip_html(text: str) -> str:
    """Remove all HTML tags and decode entities."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = html_lib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _build_jsonp_callback() -> tuple[str, str, str]:
    """Build a JSONP callback name and timestamp parameter."""
    ts = int(time.time() * 1000)
    cb = f"jQuery{ts}_{ts}"
    return cb, str(ts), str(ts + 1)


def _parse_eastmoney_time(date_str: str | None) -> datetime:
    """Parse East Money API date string to UTC datetime.

    The API returns dates like ``"2025-06-09 10:30:00"`` in China Standard Time.
    """
    if not date_str:
        return now_hkt()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    logger.warning("Could not parse EastMoney time '%s', using current HKT time", date_str)
    return now_hkt()


def _build_episode_body(title: str, content: str | None) -> str:
    """Build Markdown-formatted episode body from title and content."""
    body = f"## {title}\n\n"
    if content and content.strip():
        body += content.strip()
    return body


def _extract_keywords(title: str) -> list[str]:
    """Simple keyword extraction from title."""
    words = re.findall(r"[A-Za-z\u4e00-\u9fff]+", title)
    return words[:8]


# ── Adapter ────────────────────────────────────────────────────────────


class EastMoneyAdapter(BaseAdapter):
    """East Money stock news adapter (name-based search).

    Fetches news for each stock in the ticker whitelist using the **stock
    name** (Chinese name from the whitelist) as the search keyword,
    calling the same East Money API that ``akshare.stock_news_em()`` uses
    but with a larger ``pageSize`` (default 20).

    Falls back to AkShareAdapter in the scheduler if this adapter returns
    zero episodes or fails.
    """

    SOURCE_TYPE = "eastmoney"

    def __init__(
        self,
        page_size: int = 20,
        rate_limit_sec: float = 0.5,
        dedup_cache: set[str] | None = None,
        content_fetcher: Any | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.page_size = page_size
        self.rate_limit_sec = rate_limit_sec
        self.ticker_whitelist: list[dict[str, str]] = []
        # name -> whitelist entry map (populated by scheduler)
        self._name_map: dict[str, dict[str, str]] = {}
        self._content_fetcher = content_fetcher

    # ── single stock fetch ──────────────────────────────────────────

    def _fetch_single(self, stock_name: str) -> list[dict[str, Any]]:
        """Fetch news for a single stock by Chinese name via East Money API.

        Args:
            stock_name: Chinese stock name (e.g. "腾讯控股").

        Returns:
            List of raw news item dicts, or empty list on failure.
        """
        try:
            from curl_cffi import requests as curl_requests

            # Build JSONP callback
            cb, ts, underscore = _build_jsonp_callback()

            inner_param = {
                "uid": "",
                "keyword": stock_name,
                "type": ["cmsArticleWebOld"],
                "client": "web",
                "clientType": "web",
                "clientVersion": "curr",
                "param": {
                    "cmsArticleWebOld": {
                        "searchScope": "default",
                        "sort": "default",
                        "pageIndex": 1,
                        "pageSize": self.page_size,
                        "preTag": "<em>",
                        "postTag": "</em>",
                    }
                },
            }

            # Build full URL manually to avoid curl_cffi encoding issues
            param_json = json.dumps(inner_param, ensure_ascii=False)
            full_url = (
                f"{_EASTMONEY_SEARCH_URL}"
                f"?cb={cb}"
                f"&param={quote(param_json, safe='')}"
                f"&_={underscore}"
            )

            headers = dict(_HEADERS_TEMPLATE)
            headers["referer"] = (
                f"https://so.eastmoney.com/news/s?keyword={quote(stock_name)}"
            )
            headers["host"] = "search-api-web.eastmoney.com"

            logger.info(
                "EastMoney searching '%s' (pageSize=%d)",
                stock_name,
                self.page_size,
            )

            r = curl_requests.get(
                full_url,
                headers=headers,
                impersonate="chrome",
                timeout=30,
            )
            r.raise_for_status()

            # Strip JSONP callback wrapper: cb({...})
            data_text = r.text.strip()
            prefix = cb + "("
            suffix = ")"
            if data_text.startswith(prefix) and data_text.endswith(suffix):
                data_text = data_text[len(prefix) : -len(suffix)]
            else:
                logger.warning(
                    "Unexpected JSONP format for '%s', trying raw parse",
                    stock_name,
                )

            data_json = json.loads(data_text)
            articles = data_json.get("result", {}).get("cmsArticleWebOld", [])

            if not articles:
                logger.info("EastMoney returned 0 articles for '%s'", stock_name)
                return []

            logger.info(
                "EastMoney fetched %d articles for '%s'",
                len(articles),
                stock_name,
            )
            return articles

        except ImportError:
            logger.error(
                "curl_cffi not installed. Install with: pip install curl_cffi"
            )
            return []
        except json.JSONDecodeError as exc:
            logger.warning(
                "EastMoney JSON decode error for '%s': %s",
                stock_name,
                exc,
            )
            return []
        except Exception as exc:
            logger.warning(
                "EastMoney fetch failed for '%s': %s",
                stock_name,
                exc,
            )
            return []

    # ── batch fetch ──────────────────────────────────────────────────

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch news for all whitelisted stocks (name-based search)."""
        all_items: list[dict] = []

        for name, meta in self._name_map.items():
            articles = self._fetch_single(name)
            for article in articles:
                # Parse fields from raw API response
                title = _strip_html(article.get("title", ""))
                content = _strip_html(article.get("content", ""))
                date_str = article.get("date", "")
                media_name = article.get("mediaName", "")
                code = article.get("code", "")
                article_url = (
                    f"http://finance.eastmoney.com/a/{code}.html" if code else ""
                )

                item = {
                    "title": title,
                    "content": content,
                    "time": date_str,
                    "source": media_name,
                    "link": article_url,
                    "symbol": meta.get("biz_code", ""),
                    "_ticker_name": meta.get("name", ""),
                    "_ticker_full": meta.get("ticker", ""),
                    "_ticker_sector": meta.get("sector", ""),
                    "_ticker_exchange": meta.get("exchange", ""),
                }
                all_items.append(item)

            # Rate limiting between stocks
            if self.rate_limit_sec > 0:
                await asyncio.sleep(self.rate_limit_sec)

        # Set pre-filter count for pipeline stats
        self._pre_filter_count = len(all_items)

        logger.info(
            "Total EastMoney items fetched: %d (from %d stocks)",
            len(all_items),
            len(self._name_map),
        )
        return all_items

    # ── run override — batch fetch + normalize ──────────────────────

    async def run(self, **kwargs: Any) -> list[NormalizedEpisode]:
        """Full pipeline: fetch → batch content fetch → normalize → dedup.

        Phase 1: Fetch raw EastMoney records.
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
                    "Batch-fetched %d/%d EastMoney article contents",
                    sum(1 for r in results if r.success),
                    len(results),
                )
            except Exception as exc:
                logger.warning(
                    "Batch content fetch failed for EastMoney: %s — falling back to API summary",
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
        """Convert a single East Money article to NormalizedEpisode.

        If ``fetch_results`` is provided, uses the pre-fetched ContentResult
        to get full article text. Falls back to API summary on failure.
        """
        title = _strip_html(record.get("title", ""))
        content_raw = record.get("content", "")
        api_summary = _strip_html(content_raw) if content_raw else None
        link = record.get("link", "") or None
        symbol = record.get("symbol", "")
        ticker_name = record.get("_ticker_name", "")
        ticker_full = record.get("_ticker_full", "")
        ticker_sector = record.get("_ticker_sector", "")
        ticker_exchange = record.get("_ticker_exchange", "")

        # Prefer pre-fetched full text over API summary
        full_text: str | None = None
        content_fetched = False
        if fetch_results and link and link in fetch_results:
            result = fetch_results[link]
            if result.success and result.text:
                # Strip YAML front matter from Trafilatura extract_with_metadata
                pure_text, _yaml_meta = strip_yaml_front_matter(result.text)
                full_text = pure_text
                content_fetched = True
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
        valid_at = _parse_eastmoney_time(record.get("time"))
        keywords = _extract_keywords(title)

        # Date window cutoff — discard articles older than news_max_age_days
        settings = get_settings()
        max_age_days = settings.news_max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if valid_at < cutoff:
            return None  # type: ignore[return-value]

        # Build entity from whitelist metadata
        entities: list[EntityItem] = []
        if ticker_full:
            kwargs: dict[str, Any] = {
                "type": "stock",
                "name": ticker_name or symbol,
                "ticker": ticker_full,
            }
            if ticker_sector:
                kwargs["sector"] = ticker_sector
            if ticker_exchange:
                kwargs["exchange"] = ticker_exchange
            entities.append(EntityItem(**kwargs))

        name = NormalizedEpisode.make_name(
            source_type="eastmoney",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=symbol,
        )

        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description=f"EastMoney Stock News: {symbol} ({ticker_name})",
            source_type="eastmoney",
            source_url=record.get("link") or None,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=keywords,
            entities=entities,
            metadata={
                "content_scope": "SYMBOL",
                "symbol": symbol,
                "adapter": "eastmoney",
                "content_fetched": content_fetched,
            },
        )
