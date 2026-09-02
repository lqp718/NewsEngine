"""Unit tests for RssAdapter normalisation logic.

All tests use synthetic data — no HTTP requests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.models import NormalizedEpisode
from src.adapters.rss_adapter import RssAdapter, _build_episode_body, _extract_published


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
        # Verify valid_at is recent (within last 10 seconds)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        delta = abs((now - episode.valid_at).total_seconds())
        assert delta < 10, f"valid_at too far from now: {episode.valid_at} vs {now}"

        # Verify content_hash
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("rss-")
        assert "example.com" in episode.source_description

    @pytest.mark.asyncio
    async def test_normalize_atom_entry(self):
        """Atom format compatibility."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        entry = {
            "title": "Markets Update",
            "link": "http://atom.example.com/markets",
            "id": "atom-guid-001",
            "summary": "Stock markets rallied today...",
            "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published_parsed": now.timetuple(),
            "updated_parsed": None,
            "authors": [{"name": "Atom Reporter"}],
            "feed_url": "http://atom.example.com/feed",
        }
        adapter = RssAdapter()
        episode = await adapter.normalize(entry)
        assert episode.source_type == "rss"
        # Verify valid_at is recent (within last 10 seconds)
        delta = abs((now - episode.valid_at).total_seconds())
        assert delta < 10, f"valid_at too far from now: {episode.valid_at} vs {now}"

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


class TestRssLLMPreprocessing:
    """RSS LLM preprocessing opt-in behavior (compress only, no synthesize)."""

    @staticmethod
    def _settings(enabled: bool):
        from types import SimpleNamespace

        s = SimpleNamespace()
        s.llm_preprocessor_enabled = enabled
        s.llm_preprocessor_endpoint = "http://test/v1"
        s.llm_preprocessor_model = "test-model"
        s.llm_preprocessor_compress_threshold = 5000
        s.llm_preprocessor_compress_target = 2500
        s.llm_preprocessor_synthesize_target = 1500
        s.llm_preprocessor_timeout = 30.0
        return s

    def test_init_creates_preprocessor_when_enabled(self):
        with patch(
            "src.adapters.rss_adapter.get_settings",
            return_value=self._settings(True),
        ):
            adapter = RssAdapter()
        assert adapter._llm_preprocessor is not None
        assert adapter._llm_preprocessor.compress_threshold == 5000

    def test_init_no_preprocessor_when_disabled(self):
        with patch(
            "src.adapters.rss_adapter.get_settings",
            return_value=self._settings(False),
        ):
            adapter = RssAdapter()
        assert adapter._llm_preprocessor is None

    @pytest.mark.asyncio
    async def test_compress_called_for_long_content(self, sample_rss_entry):
        adapter = RssAdapter()
        mock_pp = MagicMock()
        mock_pp.compress_threshold = 100
        mock_pp.preprocess = AsyncMock(return_value="RSS COMPRESSED BODY")
        adapter._llm_preprocessor = mock_pp
        adapter._content_fetcher = MagicMock()
        adapter._content_fetcher.fetch_async = AsyncMock(return_value=MagicMock(
            success=True, text="long rss article text " * 20
        ))

        episode = await adapter.normalize(sample_rss_entry)

        mock_pp.preprocess.assert_awaited_once()
        _, kwargs = mock_pp.preprocess.await_args
        assert kwargs["mode"] == "compress"
        assert "RSS COMPRESSED BODY" in episode.episode_body

    @pytest.mark.asyncio
    async def test_fetch_fail_no_synthesis(self, sample_rss_entry):
        """Fetch fails → feed summary used as-is; NO LLM synthesis for RSS."""
        adapter = RssAdapter()  # no content_fetcher → fetch fails
        mock_pp = MagicMock()
        mock_pp.preprocess = AsyncMock()
        adapter._llm_preprocessor = mock_pp

        episode = await adapter.normalize(sample_rss_entry)

        mock_pp.preprocess.assert_not_awaited()
        # Feed summary (natural language) is preserved as-is
        assert "Tencent Holdings reported strong quarterly earnings" in episode.episode_body

    @pytest.mark.asyncio
    async def test_short_content_not_compressed(self, sample_rss_entry):
        """Content below threshold → no compress, original preserved."""
        adapter = RssAdapter()
        mock_pp = MagicMock()
        mock_pp.compress_threshold = 5000
        mock_pp.preprocess = AsyncMock()
        adapter._llm_preprocessor = mock_pp
        adapter._content_fetcher = MagicMock()
        adapter._content_fetcher.fetch_async = AsyncMock(return_value=MagicMock(
            success=True, text="short article"
        ))

        episode = await adapter.normalize(sample_rss_entry)

        mock_pp.preprocess.assert_not_awaited()
        assert "short article" in episode.episode_body
