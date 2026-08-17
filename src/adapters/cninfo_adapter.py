"""CNInfo (巨潮资讯) announcement adapter — fetches official company announcements.

V6.2: Phase 2 of stock news quality improvement.
Fetches official announcements from cninfo.com.cn (证监会指定法定信息披露平台).

API: cninfo.com.cn/new/hisAnnouncement/query
Content: PDF download + PyMuPDF full-text extraction → Markdown
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode
from src.core.config import get_settings
from src.ingestion.severity_enricher import rule_based_severity
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


def _parse_cninfo_time(date_str: str | None) -> datetime:
    """Parse CNInfo announcement date (YYYY-MM-DD or timestamp)."""
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        # CNInfo returns announcementTime as Unix timestamp (ms)
        ts = int(date_str) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        # Try YYYY-MM-DD format
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _extract_text_from_pdf(pdf_bytes: bytes, max_pages: int = 10) -> str:
    """Extract text from PDF using PyMuPDF.
    
    Args:
        pdf_bytes: PDF file content
        max_pages: Maximum pages to extract (default 10 to avoid huge texts)
    
    Returns:
        Extracted text as string
    """
    try:
        import pymupdf
        
        doc = pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
        text_parts = []
        
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text_parts.append(page.get_text())
        
        doc.close()
        return "\n".join(text_parts).strip()
    
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return ""


def _build_episode_body(title: str, content: str) -> str:
    """Build episode body from title and content."""
    if content and len(content) > len(title):
        return f"{title}\n\n{content}"
    return title


def _extract_keywords(title: str) -> list[str]:
    """Extract simple keywords from title."""
    keywords = re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z]+", title)
    return [k for k in keywords if len(k) >= 2][:10]


class CNInfoAdapter(BaseAdapter):
    """CNInfo (巨潮资讯) announcement adapter.
    
    Fetches official company announcements from cninfo.com.cn.
    Downloads PDF and extracts full text using PyMuPDF.
    
    Args:
        max_announcements: Max announcements per stock (default: 5).
        max_pdf_pages: Max pages to extract from PDF (default: 10).
        dedup_cache: Shared dedup cache across adapters.
    """
    
    SOURCE_TYPE = "cninfo_announcement"
    
    # CNInfo API endpoint
    API_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    PDF_BASE_URL = "http://static.cninfo.com.cn/"
    
    # Announcement categories to fetch
    # category_ndbg_szsh = 年报, category_bndbg_szsh = 半年报, category_yjdbg_szsh = 一季报
    # category_sjdbg_szsh = 三季报, category_ipo = IPO, category_zygsgg = 重要公告
    CATEGORIES = "category_zygsgg_szsh;category_ipo_szsh"
    
    def __init__(
        self,
        max_announcements: int = 5,
        max_pdf_pages: int = 10,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.max_announcements = max_announcements
        self.max_pdf_pages = max_pdf_pages
        self.ticker_whitelist: list[dict[str, str]] = []
        self._symbol_map: dict[str, dict[str, str]] = {}
        self._name_map: dict[str, dict[str, str]] = {}
    
    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch raw CNInfo announcement records."""
        tickers = kwargs.get("tickers", [])
        if tickers:
            self.ticker_whitelist = tickers
            self._symbol_map = {}
            self._name_map = {}
            for entry in tickers:
                # Support both 'ticker' and 'symbol' fields
                symbol = entry.get("ticker", "") or entry.get("symbol", "")
                name = entry.get("name", "")
                if symbol:
                    self._symbol_map[symbol] = entry
                if name:
                    self._name_map[name] = entry
        
        records = await self._fetch_announcements()
        self._pre_filter_count = len(records)
        return records
    
    async def _fetch_announcements(self) -> list[dict]:
        """Fetch announcements from CNInfo API for all whitelisted stocks."""
        all_records = []
        
        # Fetch for each whitelisted stock
        for symbol, entry in list(self._symbol_map.items())[:20]:  # Limit to 20 stocks
            try:
                records = await self._fetch_stock_announcements(symbol, entry)
                all_records.extend(records)
                logger.debug("CNInfo: fetched %d announcements for %s", len(records), symbol)
            except Exception as exc:
                logger.warning("CNInfo: fetch failed for %s: %s", symbol, exc)
        
        logger.info("CNInfo: total %d announcements from %d stocks", 
                   len(all_records), len(self._symbol_map))
        return all_records
    
    async def _fetch_stock_announcements(
        self, symbol: str, entry: dict[str, str]
    ) -> list[dict]:
        """Fetch announcements for a single stock.
        
        Supports A-shares (SSE/SZSE) and HK stocks (HKEX).
        """
        # Parse ticker format
        exchange = entry.get("exchange", "").upper()
        
        # Determine column and stock_param based on exchange
        if exchange == "HKEX":
            # HK stocks: column="hke", stock="00700,gshk0000700"
            stock_code = symbol.split(".")[0] if "." in symbol else symbol
            stock_code = re.sub(r"^(HK|hk)", "", stock_code)
            # Ensure 5-digit format
            if len(stock_code) < 5 and stock_code.isdigit():
                stock_code = stock_code.zfill(5)
            
            if not stock_code.isdigit() or len(stock_code) != 5:
                logger.debug("CNInfo: skipping invalid HK stock %s", symbol)
                return []
            
            column = "hke"
            org_id = f"gshk{stock_code.zfill(7)}"
            stock_param = f"{stock_code},{org_id}"
        else:
            # A-shares: column="sse" or "szse"
            stock_code = symbol.split(".")[0] if "." in symbol else symbol
            stock_code = re.sub(r"^(SH|SZ|sh|sz)", "", stock_code)
            if len(stock_code) < 6 and stock_code.isdigit():
                stock_code = stock_code.zfill(6)
            
            if not stock_code.isdigit() or len(stock_code) != 6:
                logger.debug("CNInfo: skipping non-A-share stock %s", symbol)
                return []
            
            if stock_code.startswith("6"):
                column = "sse"
                org_id = f"gssh{stock_code.zfill(7)}"
            elif stock_code.startswith("0") or stock_code.startswith("3"):
                column = "szse"
                org_id = f"gssz{stock_code.zfill(7)}"
            else:
                logger.debug("CNInfo: skipping non-A-share code %s", stock_code)
                return []
            
            stock_param = f"{stock_code},{org_id}"
        
        payload = {
            "pageNum": "1",
            "pageSize": str(self.max_announcements),
            "column": column,
            "tabName": "fulltext",
            "stock": stock_param,
            "searchkey": "",
            "secid": "",
            "category": self.CATEGORIES,
            "trade": "",
            "seDate": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "Origin": "http://www.cninfo.com.cn",
            "X-Requested-With": "XMLHttpRequest",
        }
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(self.API_URL, data=payload, headers=headers, timeout=15),
        )
        response.raise_for_status()
        data = response.json()
        
        announcements = data.get("announcements", [])
        if not announcements:
            return []
        
        # Convert to our record format
        records = []
        for ann in announcements:
            record = {
                "ann_id": ann.get("announcementId"),
                "title": ann.get("announcementTitle", ""),
                "sec_code": ann.get("secCode", stock_code),
                "sec_name": ann.get("secName", entry.get("name", "")),
                "ann_date": str(ann.get("announcementTime", "")),
                "adjunct_url": ann.get("adjunctUrl", ""),
                "category": ann.get("announcementType", ""),
                "_entry": entry,
            }
            records.append(record)
        
        return records
    
    async def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single CNInfo announcement to NormalizedEpisode."""
        title = record.get("title", "")
        sec_code = record.get("sec_code", "")
        sec_name = record.get("sec_name", "")
        ann_date = record.get("ann_date", "")
        adjunct_url = record.get("adjunct_url", "")
        entry = record.get("_entry", {})
        
        # Fetch and extract PDF content
        content = await self._fetch_pdf_content(adjunct_url)
        
        # Build episode body
        episode_body = _build_episode_body(title, content)
        severity = rule_based_severity(episode_body)
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = _parse_cninfo_time(ann_date)
        keywords = _extract_keywords(title)
        
        # Date window cutoff
        settings = get_settings()
        max_age_days = settings.news_max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if valid_at < cutoff:
            return None  # type: ignore[return-value]
        
        # Build entity from stock info
        entities = self._build_entity(sec_code, sec_name, entry)
        
        # Build source URL
        ann_id = record.get("ann_id", "")
        source_url = f"http://www.cninfo.com.cn/new/disclosure/detail?announcementId={ann_id}" if ann_id else None
        
        name = NormalizedEpisode.make_name(
            source_type="cninfo_announcement",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=sec_code,
        )
        
        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description=f"CNInfo Announcement: {sec_name} ({sec_code})",
            source_type="cninfo_announcement",
            source_url=source_url,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=keywords,
            entities=entities,
            metadata={
                "content_scope": "SYMBOL",
                "symbol": sec_code,
                "adapter": "cninfo",
                "content_fetched": bool(content),
                "announcement_id": ann_id,
                "announcement_category": record.get("category", ""),
                "pdf_pages_extracted": min(self.max_pdf_pages, len(content) // 2000 + 1) if content else 0,
            },
        )
    
    async def _fetch_pdf_content(self, adjunct_url: str) -> str:
        """Download and extract text from PDF."""
        if not adjunct_url:
            return ""
        
        pdf_url = f"{self.PDF_BASE_URL}{adjunct_url}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://www.cninfo.com.cn/",
        }
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(pdf_url, headers=headers, timeout=30),
            )
            response.raise_for_status()
            
            # Extract text from PDF
            text = _extract_text_from_pdf(response.content, self.max_pdf_pages)
            logger.debug("CNInfo: extracted %d chars from PDF", len(text))
            return text
        
        except Exception as exc:
            logger.warning("CNInfo: PDF fetch/extract failed for %s: %s", adjunct_url, exc)
            return ""
    
    def _build_entity(
        self, sec_code: str, sec_name: str, entry: dict[str, str]
    ) -> list[EntityItem]:
        """Build entity from stock info."""
        entities: list[EntityItem] = []
        
        if sec_code:
            # Determine exchange
            exchange = ""
            if sec_code.startswith("6"):
                exchange = "SH"
            elif sec_code.startswith("0") or sec_code.startswith("3"):
                exchange = "SZ"
            
            kwargs: dict[str, Any] = {
                "type": "stock",
                "name": sec_name or sec_code,
                "ticker": sec_code,
            }
            if exchange:
                kwargs["exchange"] = exchange
            
            sector = entry.get("sector", "")
            if sector:
                kwargs["sector"] = sector
            
            entities.append(EntityItem(**kwargs))
        
        return entities


__all__ = ["CNInfoAdapter"]
