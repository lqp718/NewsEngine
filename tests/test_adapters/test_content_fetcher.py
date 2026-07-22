"""Unit tests for ContentFetcher logic.

Tests the core fetch logic, ContentResult dataclass, and fallback chain.
Does NOT make real HTTP requests — uses mocking to avoid network calls.
"""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest


class TestContentResult:
    """ContentResult dataclass fields."""

    def test_success_result_fields(self):
        from src.utils.content_fetcher import ContentResult

        result = ContentResult(
            url="http://example.com/article",
            text="Article body text here.",
            success=True,
            engine="scrapling",
        )
        assert result.url == "http://example.com/article"
        assert result.text == "Article body text here."
        assert result.success is True
        assert result.engine == "scrapling"
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


class TestContentFetcherWithMocks:
    """ContentFetcher with real modules but mocked Fetcher/extract."""

    @patch("scrapling.Fetcher")
    def test_scrapling_success(self, mock_fetcher_class):
        """Scrapling engine returns valid text."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.get_all_text.return_value = "Extracted article text from Scrapling."
        mock_fetcher = MagicMock()
        mock_fetcher.get.return_value = mock_resp
        mock_fetcher_class.return_value = mock_fetcher

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/article")

        assert result.success is True
        assert result.engine == "scrapling"
        assert "Scrapling" in result.text
        assert result.error is None

    @patch("scrapling.Fetcher")
    @patch("httpx.Client")
    @patch("trafilatura.extract")
    def test_scrapling_fallback_to_trafilatura(
        self, mock_extract, mock_httpx_client, mock_fetcher_class
    ):
        """Fallback to Trafilatura when Scrapling returns empty content."""
        # Mock Scrapling — returns empty content
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.get_all_text.return_value = ""
        mock_fetcher = MagicMock()
        mock_fetcher.get.return_value = mock_resp
        mock_fetcher_class.return_value = mock_fetcher

        # Mock httpx for Trafilatura fallback
        mock_http_resp = MagicMock()
        mock_http_resp.text = "<html><body>Article text from Trafilatura.</body></html>"
        mock_http_resp.raise_for_status.return_value = None
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_http_resp
        mock_httpx_client.return_value = mock_client

        # Mock Trafilatura
        mock_extract.return_value = "Article text from Trafilatura."

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/article")

        assert result.success is True
        assert result.engine == "trafilatura"
        assert "Trafilatura" in result.text

    @patch("scrapling.Fetcher")
    @patch("httpx.Client")
    @patch("trafilatura.extract")
    def test_complete_failure(self, mock_extract, mock_httpx_client, mock_fetcher_class):
        """Both engines fail — returns unsuccessful result."""
        # Mock Scrapling — raises exception
        mock_fetcher = MagicMock()
        mock_fetcher.get.side_effect = Exception("Scrapling failed")
        mock_fetcher_class.return_value = mock_fetcher

        # Mock httpx — raises exception
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.side_effect = Exception("HTTP fetch failed")
        mock_httpx_client.return_value = mock_client

        # Mock Trafilatura
        mock_extract.return_value = None

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/article")

        assert result.success is False
        assert result.text == ""

    @patch("scrapling.Fetcher")
    @patch("httpx.Client")
    @patch("trafilatura.extract")
    def test_scrapling_http_error_fallback(
        self, mock_extract, mock_httpx_client, mock_fetcher_class
    ):
        """Scrapling HTTP error triggers fallback to Trafilatura."""
        # Mock Scrapling — HTTP error
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_fetcher = MagicMock()
        mock_fetcher.get.return_value = mock_resp
        mock_fetcher_class.return_value = mock_fetcher

        # Mock httpx for Trafilatura
        mock_http_resp = MagicMock()
        mock_http_resp.text = "<html><body>Fallback article text.</body></html>"
        mock_http_resp.raise_for_status.return_value = None
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_http_resp
        mock_httpx_client.return_value = mock_client

        # Mock Trafilatura
        mock_extract.return_value = "Fallback article text."

        from src.utils.content_fetcher import ContentFetcher

        fetcher = ContentFetcher()
        result = fetcher.fetch("http://example.com/article")

        assert result.success is True
        assert result.engine == "trafilatura"
