"""Unit tests for EastMoneyAdapter and AkShareAdapter ContentFetcher integration.

Tests the normalize() method's fetch_results branch:
- Success: uses pre-fetched full text
- Failure: falls back to API summary
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.adapters.eastmoney_adapter import EastMoneyAdapter
from src.adapters.akshare_adapter import AkShareAdapter
from src.utils.content_fetcher import ContentResult


def _make_content_result(success: bool, text: str | None, url: str, error: str | None = None) -> ContentResult:
    """Helper to create a ContentResult."""
    result = ContentResult(url=url)
    result.success = success
    result.text = text
    result.error = error
    return result


# ── EastMoneyAdapter Tests ──────────────────────────────────────────────


class TestEastMoneyAdapterNormalize:
    """Tests for EastMoneyAdapter.normalize() with fetch_results."""

    @pytest.fixture
    def adapter(self):
        return EastMoneyAdapter(content_fetcher=MagicMock())

    @pytest.fixture
    def sample_record(self):
        return {
            "title": "腾讯控股<em>大涨</em>5%",
            "content": "<em>API</em> summary text",
            "time": "2025-07-25 10:00:00",
            "source": "东方财富",
            "link": "http://finance.eastmoney.com/a/12345.html",
            "symbol": "00700",
            "_ticker_name": "腾讯控股",
            "_ticker_full": "00700.HK",
            "_ticker_sector": "科技",
            "_ticker_exchange": "HK",
        }

    def test_normalize_with_successful_fetch_results(self, adapter, sample_record):
        """When fetch_results has successful content, use full text."""
        full_text = "This is the full article content fetched from the URL."
        fetch_results = {
            sample_record["link"]: _make_content_result(
                success=True,
                text=full_text,
                url=sample_record["link"],
            )
        }

        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=fetch_results))

        assert full_text in episode.episode_body
        assert "API" not in episode.episode_body  # API summary not used

    def test_normalize_with_failed_fetch_results(self, adapter, sample_record):
        """When fetch_results has failed content, fall back to API summary."""
        fetch_results = {
            sample_record["link"]: _make_content_result(
                success=False,
                text=None,
                url=sample_record["link"],
                error="Connection timeout",
            )
        }

        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=fetch_results))

        assert "API" in episode.episode_body  # API summary used
        assert "summary text" in episode.episode_body

    def test_normalize_without_fetch_results(self, adapter, sample_record):
        """When fetch_results is None, use API summary (backward compat)."""
        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=None))

        assert "API" in episode.episode_body
        assert "summary text" in episode.episode_body

    def test_normalize_with_empty_fetch_results(self, adapter, sample_record):
        """When fetch_results is empty dict, use API summary."""
        episode = asyncio.run(adapter.normalize(sample_record, fetch_results={}))

        assert "API" in episode.episode_body

    def test_normalize_strips_em_tags_from_api_summary(self, adapter, sample_record):
        """API summary should have <em> tags stripped."""
        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=None))

        assert "<em>" not in episode.episode_body
        assert "</em>" not in episode.episode_body


# ── AkShareAdapter Tests ────────────────────────────────────────────────


class TestAkShareAdapterNormalize:
    """Tests for AkShareAdapter.normalize() with fetch_results."""

    @pytest.fixture
    def adapter(self):
        return AkShareAdapter(content_fetcher=MagicMock())

    @pytest.fixture
    def sample_record(self):
        return {
            "title": "A股三大指数集体上涨",
            "content": "API returned summary content",
            "time": "2025-07-25 09:30:00",
            "source": "东方财富",
            "link": "http://finance.eastmoney.com/a/67890.html",
            "symbol": "00700",
            "_ticker_name": "腾讯控股",
            "_ticker_full": "00700.HK",
            "_ticker_sector": "科技",
            "_ticker_exchange": "HK",
        }

    def test_normalize_with_successful_fetch_results(self, adapter, sample_record):
        """When fetch_results has successful content, use full text."""
        full_text = "Full article text fetched from the source URL."
        fetch_results = {
            sample_record["link"]: _make_content_result(
                success=True,
                text=full_text,
                url=sample_record["link"],
            )
        }

        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=fetch_results))

        assert full_text in episode.episode_body
        assert "API returned" not in episode.episode_body

    def test_normalize_with_failed_fetch_results(self, adapter, sample_record):
        """When fetch_results has failed content, fall back to API summary."""
        fetch_results = {
            sample_record["link"]: _make_content_result(
                success=False,
                text=None,
                url=sample_record["link"],
                error="403 Forbidden",
            )
        }

        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=fetch_results))

        assert "API returned" in episode.episode_body
        assert "summary content" in episode.episode_body

    def test_normalize_without_fetch_results(self, adapter, sample_record):
        """When fetch_results is None, use API summary (backward compat)."""
        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=None))

        assert "API returned" in episode.episode_body

    def test_normalize_with_link_not_in_fetch_results(self, adapter, sample_record):
        """When link is not in fetch_results, use API summary."""
        fetch_results = {
            "http://other-url.com/article": _make_content_result(
                success=True,
                text="Other article",
                url="http://other-url.com/article",
            )
        }

        episode = asyncio.run(adapter.normalize(sample_record, fetch_results=fetch_results))

        assert "API returned" in episode.episode_body


# ── Integration-style Tests ─────────────────────────────────────────────


class TestRunMethodIntegration:
    """Tests for the run() method batch-fetch pattern."""

    @pytest.fixture
    def mock_content_fetcher(self):
        fetcher = MagicMock()
        fetcher.fetch_batch = MagicMock()
        return fetcher

    def test_eastmoney_run_calls_fetch_batch(self, mock_content_fetcher):
        """EastMoneyAdapter.run() should call fetch_batch with links."""
        adapter = EastMoneyAdapter(content_fetcher=mock_content_fetcher)
        adapter._name_map = {"腾讯控股": {"biz_code": "00700", "name": "腾讯控股"}}

        # Mock fetch to return sample records
        async def mock_fetch(**kwargs):
            return [
                {
                    "title": "Test",
                    "content": "Summary",
                    "time": "2025-07-25 10:00:00",
                    "source": "Test",
                    "link": "http://finance.eastmoney.com/a/1.html",
                    "symbol": "00700",
                    "_ticker_name": "腾讯",
                    "_ticker_full": "00700.HK",
                    "_ticker_sector": "",
                    "_ticker_exchange": "",
                }
            ]

        adapter.fetch = mock_fetch

        # Mock fetch_batch to return successful result
        async def mock_fetch_batch(urls):
            return [_make_content_result(True, "Full text", urls[0])]

        mock_content_fetcher.fetch_batch = mock_fetch_batch

        episodes = asyncio.run(adapter.run())

        assert len(episodes) == 1
        assert "Full text" in episodes[0].episode_body

    def test_akshare_run_calls_fetch_batch(self, mock_content_fetcher):
        """AkShareAdapter.run() should call fetch_batch with links."""
        adapter = AkShareAdapter(content_fetcher=mock_content_fetcher)
        adapter._symbol_map = {"00700": {"biz_code": "00700", "name": "腾讯控股"}}

        # Mock fetch to return sample records
        async def mock_fetch(**kwargs):
            return [
                {
                    "title": "Test",
                    "content": "Summary",
                    "time": "2025-07-25 10:00:00",
                    "source": "Test",
                    "link": "http://finance.eastmoney.com/a/2.html",
                    "symbol": "00700",
                    "_ticker_name": "腾讯",
                    "_ticker_full": "00700.HK",
                    "_ticker_sector": "",
                    "_ticker_exchange": "",
                }
            ]

        adapter.fetch = mock_fetch

        # Mock fetch_batch to return successful result
        async def mock_fetch_batch(urls):
            return [_make_content_result(True, "Full article", urls[0])]

        mock_content_fetcher.fetch_batch = mock_fetch_batch

        episodes = asyncio.run(adapter.run())

        assert len(episodes) == 1
        assert "Full article" in episodes[0].episode_body
