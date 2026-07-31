"""EastMoney Research Report adapter — fetches analyst research reports.

V6.3: Phase 3 of stock news quality improvement.
Fetches analyst research reports from EastMoney reportapi.

API: reportapi.eastmoney.com/report/list
Content: PDF download + PyMuPDF extraction → Markdown
"""

from __future__ import annotations

import asyncio
import hashlib
import io
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


def _extract_text_from_pdf(pdf_bytes: bytes, max_pages: int = 15) -> str:
    """Extract text from PDF using PyMuPDF.
    
    Args:
        pdf_bytes: PDF file content
        max_pages: Maximum pages to extract (default 15 for research reports)
    
    Returns:
        Extracted text as string
    """
    try:
        import fitz  # PyMuPDF
        
        doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
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


def _parse_eastmoney_time(date_str: str | None) -> datetime:
    """Parse EastMoney publish date."""
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        # Format: "2026-07-31 00:00:00.000"
        dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _build_episode_body(title: str, content: str, metadata: dict) -> str:
    """Build episode body from title, content and metadata."""
    parts = [title]
    
    # Add report metadata
    org = metadata.get("org_name", "")
    author = metadata.get("author", "")
    rating = metadata.get("rating", "")
    
    if org or author or rating:
        meta_line = f"[{org}] {author}" if org and author else org or author
        if rating:
            meta_line += f" | 评级: {rating}"
        parts.append(meta_line)
    
    if content:
        parts.append(content)
    
    return "\n\n".join(parts)


def _extract_keywords(title: str) -> list[str]:
    """Extract simple keywords from title."""
    keywords = re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z]+", title)
    return [k for k in keywords if len(k) >= 2][:10]


class EastMoneyResearchAdapter(BaseAdapter):
    """EastMoney Research Report adapter.
    
    Fetches analyst research reports from EastMoney.
    Downloads PDF and extracts text using PyMuPDF.
    
    Args:
        max_reports: Max reports per stock (default: 3).
        max_pdf_pages: Max pages to extract from PDF (default: 15).
        dedup_cache: Shared dedup cache across adapters.
    """
    
    SOURCE_TYPE = "eastmoney_research"
    
    # EastMoney report API endpoint
    API_URL = "https://reportapi.eastmoney.com/report/list"
    PDF_BASE_URL = "https://pdf.dfcfw.com/pdf/H3_"
    
    def __init__(
        self,
        max_reports: int = 3,
        max_pdf_pages: int = 15,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.max_reports = max_reports
        self.max_pdf_pages = max_pdf_pages
        self.ticker_whitelist: list[dict[str, str]] = []
        self._symbol_map: dict[str, dict[str, str]] = {}
        self._name_map: dict[str, dict[str, str]] = {}
    
    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch raw EastMoney research report records."""
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
        
        records = await self._fetch_research_reports()
        self._pre_filter_count = len(records)
        return records
    
    async def _fetch_research_reports(self) -> list[dict]:
        """Fetch research reports from EastMoney API for all whitelisted stocks."""
        all_records = []
        
        # Fetch for each whitelisted stock
        for symbol, entry in list(self._symbol_map.items())[:20]:  # Limit to 20 stocks
            try:
                records = await self._fetch_stock_reports(symbol, entry)
                all_records.extend(records)
                logger.debug("EastMoney Research: fetched %d reports for %s", len(records), symbol)
            except Exception as exc:
                logger.warning("EastMoney Research: fetch failed for %s: %s", symbol, exc)
        
        logger.info("EastMoney Research: total %d reports from %d stocks", 
                   len(all_records), len(self._symbol_map))
        return all_records
    
    async def _fetch_stock_reports(
        self, symbol: str, entry: dict[str, str]
    ) -> list[dict]:
        """Fetch research reports for a single stock."""
        # Convert ticker format: "0700.HK" -> "00700", "000001.SZ" -> "000001"
        stock_code = symbol.split(".")[0] if "." in symbol else symbol
        # Remove exchange prefix (SH/SZ) if present
        stock_code = re.sub(r"^(SH|SZ|sh|sz)", "", stock_code)
        # Ensure 6-digit format for A-shares
        if len(stock_code) < 6 and stock_code.isdigit():
            stock_code = stock_code.zfill(6)
        
        # Skip non-A-share stocks (EastMoney research only supports A-shares)
        if not stock_code.isdigit() or len(stock_code) != 6:
            logger.debug("EastMoney Research: skipping non-A-share stock %s", symbol)
            return []
        
        # Calculate date range (last 30 days)
        end_date = datetime.now()
        begin_date = end_date - timedelta(days=30)
        
        params = {
            "industryCode": "*",
            "pageSize": str(self.max_reports),
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": begin_date.strftime("%Y-%m-%d"),
            "endTime": end_date.strftime("%Y-%m-%d"),
            "pageNo": "1",
            "fields": "",
            "qType": "0",  # Individual stock research
            "orgCode": "",
            "author": "",
            "code": stock_code,
            "rcode": "",
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(self.API_URL, params=params, headers=headers, timeout=15),
        )
        response.raise_for_status()
        data = response.json()
        
        reports = data.get("data", [])
        if not reports:
            return []
        
        # Convert to our record format
        records = []
        for report in reports:
            record = {
                "info_code": report.get("infoCode", ""),
                "title": report.get("title", ""),
                "stock_name": report.get("stockName", ""),
                "stock_code": report.get("stockCode", stock_code),
                "org_name": report.get("orgSName", ""),
                "author": report.get("researcher", ""),  # Actually 'researcher' field
                "rating": report.get("emRatingName", ""),
                "publish_date": report.get("publishDate", ""),
                "encode_url": report.get("encodeUrl", ""),
                "abstract": report.get("abstract", ""),
                "_entry": entry,
            }
            records.append(record)
        
        return records
    
    async def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single EastMoney research report to NormalizedEpisode."""
        title = record.get("title", "")
        stock_name = record.get("stock_name", "")
        stock_code = record.get("stock_code", "")
        org_name = record.get("org_name", "")
        author = record.get("author", "")
        rating = record.get("rating", "")
        publish_date = record.get("publish_date", "")
        info_code = record.get("info_code", "")
        abstract = record.get("abstract", "")
        entry = record.get("_entry", {})
        
        # Fetch and extract PDF content using info_code
        content = await self._fetch_pdf_content(info_code)
        
        # If PDF extraction failed, use abstract as fallback
        if not content and abstract:
            content = abstract
            logger.debug("EastMoney Research: using abstract as fallback for %s", info_code)
        
        # If PDF extraction failed, use abstract
        if not content and abstract:
            content = abstract
        
        # Build episode body with metadata
        metadata = {
            "org_name": org_name,
            "author": author,
            "rating": rating,
        }
        episode_body = _build_episode_body(title, content, metadata)
        
        severity = rule_based_severity(episode_body)
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()
        valid_at = _parse_eastmoney_time(publish_date)
        keywords = _extract_keywords(title)
        
        # Date window cutoff
        settings = get_settings()
        max_age_days = settings.news_max_age_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if valid_at < cutoff:
            return None  # type: ignore[return-value]
        
        # Build entity from stock info
        entities = self._build_entity(stock_code, stock_name, entry)
        
        # Build source URL
        info_code = record.get("info_code", "")
        source_url = f"https://data.eastmoney.com/report/zw/stock.jshtml?infocode={info_code}" if info_code else None
        
        name = NormalizedEpisode.make_name(
            source_type="eastmoney_research",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=stock_code,
        )
        
        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description=f"EastMoney Research: {stock_name} ({stock_code}) - {org_name}",
            source_type="eastmoney_research",
            source_url=source_url,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=keywords,
            entities=entities,
            metadata={
                "content_scope": "SYMBOL",
                "symbol": stock_code,
                "adapter": "eastmoney_research",
                "content_fetched": bool(content),
                "info_code": info_code,
                "org_name": org_name,
                "author": author,
                "rating": rating,
                "pdf_pages_extracted": min(self.max_pdf_pages, len(content) // 2000 + 1) if content else 0,
            },
        )
    
    async def _fetch_pdf_content(self, info_code: str) -> str:
        """Download and extract text from PDF.
        
        TODO: PDF 反爬问题 - pdf.dfcfw.com 返回 HTTP 200 但 Content-Length: 0
        原因：东财检测到非住宅 IP / WSL2 环境，返回空内容而非 403
        当前状态：使用 abstract 兜底，后续可考虑：
          1. 用 browser 工具模拟浏览器下载（慢但可靠）
          2. 换用 iwencai 研报 API（需要 X-Claw Header）
          3. 接受 abstract 兜底方案
        """
        if not info_code:
            return ""
        
        # PDF URL format: https://pdf.dfcfw.com/pdf/H3_AP{infoCode}_1.pdf
        pdf_url = f"https://pdf.dfcfw.com/pdf/H3_AP{info_code}_1.pdf"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(pdf_url, headers=headers, timeout=30),
            )
            response.raise_for_status()
            
            # Check if response is actually a PDF
            content_type = response.headers.get('Content-Type', '')
            if 'pdf' not in content_type.lower() and not response.content[:4] == b'%PDF':
                logger.warning("EastMoney Research: response is not a PDF for %s", info_code)
                return ""
            
            # Extract text from PDF
            text = _extract_text_from_pdf(response.content, self.max_pdf_pages)
            logger.debug("EastMoney Research: extracted %d chars from PDF", len(text))
            return text
        
        except Exception as exc:
            logger.warning("EastMoney Research: PDF fetch/extract failed for %s: %s", info_code, exc)
            return ""
    
    def _build_entity(
        self, stock_code: str, stock_name: str, entry: dict[str, str]
    ) -> list[EntityItem]:
        """Build entity from stock info."""
        entities: list[EntityItem] = []
        
        if stock_code:
            # Determine exchange
            exchange = ""
            if stock_code.startswith("6"):
                exchange = "SH"
            elif stock_code.startswith("0") or stock_code.startswith("3"):
                exchange = "SZ"
            
            kwargs: dict[str, Any] = {
                "type": "stock",
                "name": stock_name or stock_code,
                "ticker": stock_code,
            }
            if exchange:
                kwargs["exchange"] = exchange
            
            sector = entry.get("sector", "")
            if sector:
                kwargs["sector"] = sector
            
            entities.append(EntityItem(**kwargs))
        
        return entities


__all__ = ["EastMoneyResearchAdapter"]
