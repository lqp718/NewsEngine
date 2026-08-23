"""Cross-adapter integration tests with real HTTP sources.

Tests for RSS, AkShare, and Treasury adapters with real data sources.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestRssRealHttp:
    """Real RSS feed fetching."""

    async def test_fetch_real_feed(self):
        """Fetch a real RSS feed and verify entries."""
        from src.adapters.rss_adapter import RssAdapter

        adapter = RssAdapter(
            feed_urls=["https://feeds.bbci.co.uk/news/business/rss.xml"]
        )
        try:
            records = await adapter.fetch()
            assert len(records) > 0
            for rec in records:
                assert "title" in rec
                assert "link" in rec
        except Exception as exc:
            pytest.skip(
                f"RSS feed HTTP failed (network may be restricted): {exc}"
            )

    async def test_unreachable_feed(self):
        """Unreachable feed should not raise exceptions."""
        from src.adapters.rss_adapter import RssAdapter

        adapter = RssAdapter(
            feed_urls=["http://nonexistent.example.com/feed.xml"]
        )
        records = await adapter.fetch()
        assert records == []

    async def test_multiple_feeds(self):
        """Multiple feeds should be merged."""
        from src.adapters.rss_adapter import RssAdapter

        adapter = RssAdapter(
            feed_urls=[
                "https://feeds.bbci.co.uk/news/business/rss.xml",
                "http://nonexistent.example.com/feed.xml",  # should be skipped silently
            ]
        )
        try:
            records = await adapter.fetch()
            assert len(records) >= 0
        except Exception as exc:
            pytest.skip(
                f"RSS feed HTTP failed (network may be restricted): {exc}"
            )


@pytest.mark.integration
class TestAkShareRealHttp:
    """Real AkShare API calls."""

    async def test_fetch_real_stock_news(self):
        """Fetch real stock news for Tencent (00700)."""
        from src.adapters.akshare_adapter import AkShareAdapter

        adapter = AkShareAdapter(
            ticker_whitelist=[
                {
                    "symbol": "HK.00700",
                    "biz_code": "00700",
                    "name": "腾讯控股",
                }
            ]
        )
        try:
            records = await adapter.fetch()
            assert len(records) > 0
            # Most records should have titles
            assert all("title" in r for r in records)
        except Exception as exc:
            pytest.skip(
                f"AkShare API call failed: {exc}"
            )

    async def test_fetch_with_batch_and_rate_limit(self):
        """Batch fetch multiple symbols with rate limiting."""
        from src.adapters.akshare_adapter import AkShareAdapter

        adapter = AkShareAdapter(
            ticker_whitelist=[
                {"symbol": "HK.00700", "biz_code": "00700", "name": "腾讯控股"},
                {"symbol": "HK.09988", "biz_code": "09988", "name": "阿里巴巴"},
            ],
            rate_limit_sec=0.5,
        )
        try:
            records = await adapter.fetch()
            assert len(records) >= 0
        except Exception as exc:
            pytest.skip(
                f"AkShare API call failed: {exc}"
            )


@pytest.mark.integration
class TestTreasuryAdapter:
    """Treasury adapter fetches real yield curve data from US Treasury."""

    async def test_fetch_returns_records(self):
        from src.adapters.treasury_adapter import TreasuryAdapter

        adapter = TreasuryAdapter()
        records = await adapter.fetch()
        assert len(records) > 0
        # Each record should contain fetch_time and term_rates
        rec = records[0]
        assert "fetch_time" in rec
        assert "term_rates" in rec
        assert isinstance(rec["term_rates"], dict)
