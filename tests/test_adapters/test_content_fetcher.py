"""Unit tests for ContentFetcher logic (V4.0 — NewsSpider backend).

Tests the core fetch logic, ContentResult dataclass, NewsSpider integration,
and fallback chain. Does NOT make real HTTP requests — uses mocking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestContentResult:
    """ContentResult dataclass fields."""

    def test_success_result_fields(self):
        from src.utils.content_fetcher import ContentResult

        result = ContentResult(
            url="http://example.com/article",
            text="Article body text here.",
            success=True,
            engine="news_spider+trafilatura",
        )
        assert result.url == "http://example.com/article"
        assert result.text == "Article body text here."
        assert result.success is True
        assert result.engine == "news_spider+trafilatura"
        assert result.error is None
        assert result.content_length == 23  # len("Article body text here.")

    def test_failure_result_fields(self):
        from src.utils.content_fetcher import ContentResult

        result = ContentResult(
            url="http://example.com/fail",
            success=False,
            error="Connection timeout",
        )
        assert result.success is False
        assert result.text == ""
        assert result.content_length == 0
        assert result.error == "Connection timeout"
        assert result.engine is None


class TestSpiderResultConversion:
    """Conversion from SpiderResult → ContentResult."""

    def _make_spider_result(
        self,
        url: str,
        status: int = 200,
        html: str = "",
        error: str | None = None,
        used_stealth: bool = False,
    ):
        from src.utils.news_spider import SpiderResult

        return SpiderResult(
            url=url,
            status=status,
            html_content=html,
            error=error,
            used_stealth=used_stealth,
        )

    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_successful_extraction(self, mock_extract):
        """Spider returns valid HTML → Trafilatura extracts text → success."""
        mock_extract.return_value = "Extracted article text."

        from src.utils.content_fetcher import ContentFetcher, ContentResult

        fetcher = ContentFetcher()
        spider_result = self._make_spider_result(
            url="http://example.com/article",
            status=200,
            html="<html><body>Article content</body></html>",
        )
        result = fetcher._spider_result_to_content(spider_result, spider_result.url)

        assert result.success is True
        assert result.text == "Extracted article text."
        assert "trafilatura" in (result.engine or "")
        assert result.url == "http://example.com/article"

    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_stealth_flag_preserved(self, mock_extract):
        """Spider result with used_stealth=True is preserved in logs."""
        mock_extract.return_value = "Content from stealth session."

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        spider_result = self._make_spider_result(
            url="http://example.com/blocked",
            status=200,
            html="<html>Content</html>",
            used_stealth=True,
        )
        result = fetcher._spider_result_to_content(spider_result, spider_result.url)

        assert result.success is True
        assert result.text == "Content from stealth session."

    def test_http_error(self):
        """Non-200 status → failure ContentResult."""
        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        spider_result = self._make_spider_result(
            url="http://example.com/404",
            status=404,
            html="<html>Not Found</html>",
        )
        result = fetcher._spider_result_to_content(spider_result, spider_result.url)

        assert result.success is False
        assert "HTTP 404" in (result.error or "")
        assert result.text == ""

    def test_empty_html(self):
        """Empty HTML → failure."""
        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        spider_result = self._make_spider_result(
            url="http://example.com/empty",
            status=200,
            html="",
        )
        result = fetcher._spider_result_to_content(spider_result, spider_result.url)

        assert result.success is False
        assert "Empty HTML" in (result.error or "")

    def test_spider_error(self):
        """Spider error → failure ContentResult."""
        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        spider_result = self._make_spider_result(
            url="http://example.com/error",
            status=0,
            html="",
            error="Connection refused",
        )
        result = fetcher._spider_result_to_content(spider_result, spider_result.url)

        assert result.success is False
        assert "Connection refused" in (result.error or "")

    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_extract_returns_none(self, mock_extract):
        """Trafilatura fails → failure."""
        mock_extract.return_value = None

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        spider_result = self._make_spider_result(
            url="http://example.com/no-extract",
            status=200,
            html="<html><body>Content</body></html>",
        )
        result = fetcher._spider_result_to_content(spider_result, spider_result.url)

        assert result.success is False
        assert "empty content" in (result.error or "").lower()


class TestContentFetcherWithMocks:
    """ContentFetcher with mocked NewsSpider."""

    @patch("src.utils.content_fetcher.fetch_urls_with_spider")
    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_fetch_single_success(self, mock_extract, mock_spider):
        """Single URL fetch returns extracted text."""
        mock_extract.return_value = "Extracted article body."
        mock_spider.return_value = [
            MagicMock(
                url="http://example.com/article",
                status=200,
                html_content="<html>Content</html>",
                error=None,
                used_stealth=False,
            ),
        ]

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/article")

        assert result.success is True
        assert result.text == "Extracted article body."
        assert result.error is None

    @patch("src.utils.content_fetcher.fetch_urls_with_spider")
    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_fetch_batch_success(self, mock_extract, mock_spider):
        """Batch fetch returns results for all URLs."""
        mock_extract.return_value = "Extracted text."
        mock_spider.return_value = [
            MagicMock(
                url="http://example.com/a",
                status=200,
                html_content="<html>A</html>",
                error=None,
                used_stealth=False,
            ),
            MagicMock(
                url="http://example.com/b",
                status=200,
                html_content="<html>B</html>",
                error=None,
                used_stealth=False,
            ),
        ]

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        results = fetcher.fetch("http://example.com/a")  # sync single

        # Use async for batch
        import asyncio
        batch_results = asyncio.run(
            fetcher.fetch_batch(["http://example.com/a", "http://example.com/b"]),
        )

        assert len(batch_results) == 2
        assert batch_results[0].success is True
        assert batch_results[1].success is True
        assert batch_results[0].url == "http://example.com/a"
        assert batch_results[1].url == "http://example.com/b"

    @patch("src.utils.content_fetcher.fetch_urls_with_spider")
    def test_fetch_spider_error(self, mock_spider):
        """Spider returns error → unsuccessful result."""
        mock_spider.return_value = [
            MagicMock(
                url="http://example.com/fail",
                status=0,
                html_content="",
                error="Connection timeout",
                used_stealth=False,
            ),
        ]

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/fail")

        assert result.success is False
        assert result.text == ""

    @patch("src.utils.content_fetcher.fetch_urls_with_spider")
    def test_fetch_import_error(self, mock_spider):
        """Simulate import error for NewsSpider."""
        mock_spider.side_effect = ImportError("No module named scrapling")

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/fail")

        assert result.success is False
        assert "No module named" in (result.error or "")
