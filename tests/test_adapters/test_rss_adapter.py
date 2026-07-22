"""Unit tests for RssAdapter normalisation logic.

All tests use synthetic data — no HTTP requests.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.adapters.models import NormalizedEpisode
from src.adapters.rss_adapter import RssAdapter, _build_episode_body, _extract_published
import asyncio


class TestRssNormalize:
    """RssAdapter.normalize() output fields."""

    @pytest.mark.asyncio
    async def test_normalize_rss_entry(self, sample_rss_entry):
        adapter = RssAdapter()
        episode = await adapter.normalize(sample_rss_entry)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "rss"
        assert episode.source_url == "http://example.com/rss/tencent-earnings"
        assert episode.severity == "medium"
        assert "Tencent Stock Rises" in episode.episode_body
        assert episode.valid_at.year == 2025
        assert episode.valid_at.month == 6
        assert episode.valid_at.day == 9

        # Verify content_hash
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("rss-")
        assert "example.com" in episode.source_description

    @pytest.mark.asyncio
    async def test_normalize_atom_entry(self):
        """Atom format compatibility."""
        from time import struct_time
        import time

        now = time.gmtime()
        entry = {
            "title": "Markets Update",
            "link": "http://atom.example.com/markets",
            "id": "atom-guid-001",
            "summary": "Stock markets rallied today...",
            "published": "2025-06-09T02:00:00Z",
            "published_parsed": struct_time((2025, 6, 9, 2, 0, 0, 0, 0, 0)),
            "updated_parsed": None,
            "authors": [{"name": "Atom Reporter"}],
            "feed_url": "http://atom.example.com/feed",
        }
        adapter = RssAdapter()
        episode = await adapter.normalize(entry)
        assert episode.source_type == "rss"
        assert episode.valid_at.year == 2025

    @pytest.mark.asyncio
    async def test_missing_published_date(self, sample_rss_entry):
        """Missing published date defaults to current UTC time."""
        entry = dict(sample_rss_entry)
        entry["published"] = ""
        entry["published_parsed"] = None
        entry["updated_parsed"] = None

        adapter = RssAdapter()
        episode = await adapter.normalize(entry)

        # Should default to now (within last 10 seconds)
        import time as time_module
        now = datetime.now(tz=episode.valid_at.tzinfo)
        delta = abs((now - episode.valid_at).total_seconds())
        assert delta < 10, f"valid_at too far from now: {episode.valid_at} vs {now}"

    @pytest.mark.asyncio
    async def test_dedup_by_link(self):
        """Same link → only first retained."""
        adapter = RssAdapter()
        entry1 = {
            "title": "Entry 1",
            "link": "http://example.com/dup",
            "id": "guid1",
            "summary": "Content 1",
            "published": "Mon, 09 Jun 2025 01:00:00 GMT",
            "feed_url": "http://example.com/rss",
        }
        entry2 = dict(entry1)
        entry2["title"] = "Entry 2"
        entry2["id"] = "guid2"
        # We need different content_hash for url-based dedup to show
        entry2["summary"] = "Content 2"

        ep1 = await adapter.normalize(entry1)
        ep2 = await adapter.normalize(entry2)

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name

    @pytest.mark.asyncio
    async def test_keywords_extracted(self, sample_rss_entry):
        adapter = RssAdapter()
        episode = await adapter.normalize(sample_rss_entry)
        assert len(episode.keywords) > 0


class TestRssFetchSingle:
    """Helper method tests."""

    def test_episode_body_formatting(self):
        """Verify Markdown formatting of episode body."""
        body = _build_episode_body("Test Title", "Test description here.")
        assert "## Test Title" in body
        assert "Test description here." in body

    def test_episode_body_no_summary(self):
        body = _build_episode_body("Test Title", None)
        assert "## Test Title" in body

    def test_extract_published_from_parsed(self):
        from time import struct_time
        entry = {
            "published_parsed": struct_time((2025, 6, 9, 12, 0, 0, 0, 0, 0)),
            "updated_parsed": None,
        }
        dt = _extract_published(entry)
        assert dt.year == 2025
        assert dt.month == 6

    def test_extract_published_fallback_to_updated(self):
        from time import struct_time
        entry = {
            "published_parsed": None,
            "updated_parsed": struct_time((2025, 6, 8, 10, 30, 0, 0, 0, 0)),
        }
        dt = _extract_published(entry)
        assert dt.day == 8
