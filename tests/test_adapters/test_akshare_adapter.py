"""Unit tests for AkShareAdapter normalisation logic.

All tests use synthetic data — no HTTP requests.
"""

from __future__ import annotations

import pytest

from src.adapters.akshare_adapter import AkShareAdapter, _build_episode_body, _parse_akshare_time
from src.adapters.models import NormalizedEpisode


class TestAkShareNormalize:
    """AkShareAdapter.normalize() output fields."""

    @pytest.mark.asyncio
    async def test_normalize_valid_item(self, sample_akshare_item):
        adapter = AkShareAdapter(ticker_whitelist=[])
        episode = await adapter.normalize(sample_akshare_item)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "akshare"
        assert episode.source_url is None  # AkShare doesn't provide URLs
        assert episode.severity == "medium"
        assert "腾讯控股" in episode.episode_body

        # Verify entity from whitelist
        assert len(episode.entities) == 1
        assert episode.entities[0].type == "stock"
        assert episode.entities[0].ticker == "00700.HK"
        assert episode.entities[0].name == "腾讯控股"

        # Verify metadata
        assert episode.metadata["symbol"] == "00700"

        # content_hash
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("akshare-")
        assert "00700" in episode.name

    @pytest.mark.asyncio
    async def test_normalize_empty_content(self, sample_akshare_item):
        """Empty content → only title in body."""
        item = dict(sample_akshare_item)
        item["content"] = ""
        adapter = AkShareAdapter()
        episode = await adapter.normalize(item)
        assert "## 腾讯控股股价创历史新高" in episode.episode_body

    @pytest.mark.asyncio
    async def test_normalize_no_entity_without_whitelist_data(self):
        """No ticker metadata → entities list empty."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        item = {
            "title": "Some Stock News",
            "content": "General market news...",
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "东方财富",
            "symbol": "00001",
            "_ticker_name": "",
            "_ticker_full": "",
        }
        adapter = AkShareAdapter()
        episode = await adapter.normalize(item)
        assert len(episode.entities) == 0

    @pytest.mark.asyncio
    async def test_dedup_by_hash(self, sample_akshare_item):
        """Same content_hash → only first retained."""
        adapter = AkShareAdapter()
        ep1 = await adapter.normalize(sample_akshare_item)
        ep2 = await adapter.normalize(sample_akshare_item)  # same body → same hash
        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1


class TestAkShareHelpers:
    """Helper function tests."""

    def test_parse_akshare_time_pandas_like(self):
        """Mock pandas Timestamp-like datetime parsing."""
        dt = _parse_akshare_time("2025-06-09 10:30:00")
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 9

    def test_parse_akshare_time_none(self):
        dt = _parse_akshare_time(None)
        assert dt is not None  # should default to now

    def test_parse_akshare_time_invalid(self):
        dt = _parse_akshare_time("not_a_date")
        assert dt is not None

    def test_build_episode_body_with_content(self):
        body = _build_episode_body("Test", "Some content here")
        assert "## Test" in body
        assert "Some content here" in body

    def test_build_episode_body_empty_content(self):
        body = _build_episode_body("Test", None)
        assert "## Test" in body
