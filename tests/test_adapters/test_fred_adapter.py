"""Unit tests for FredAdapter (Phase 1 macro adapter).

Covers: BaseAdapter contract, fetch degradation without API key,
normalize (awaited — no latent un-awaited bug), dedup, helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.adapters.base import BaseAdapter
from src.adapters.fred_adapter import (
    FredAdapter,
    _FRED_SERIES,
    _build_fred_body,
    _map_fred_severity,
)
from src.adapters.models import NormalizedEpisode


class TestFredContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = FredAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert FredAdapter.SOURCE_TYPE == "fred"

    def test_default_series_constant(self):
        assert "GDP" in _FRED_SERIES
        assert "CPIAUCSL" in _FRED_SERIES
        assert "UNRATE" in _FRED_SERIES
        assert "DFF" in _FRED_SERIES
        assert "PPIACO" in _FRED_SERIES


class TestFredFetchDegrade:
    """fetch() degrades gracefully when fred_api_key is unconfigured."""

    @pytest.mark.asyncio
    async def test_fetch_empty_without_key(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = FredAdapter()
        result = await adapter.fetch()
        assert result == []
        assert adapter._pre_filter_count == 0


class TestFredNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_gdp_snapshot(self, sample_fred_record):
        adapter = FredAdapter()
        episode = await adapter.normalize(sample_fred_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "fred"
        assert episode.source_description == "FRED (Federal Reserve Economic Data)"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["series_id"] == "GDP"
        assert episode.metadata["value"] == "29200.0"
        assert episode.metadata["previous_value"] == "29000.0"
        assert episode.metadata["units"] == "Bil. of $"

        # Entities: country + theme
        entity_types = {e.type for e in episode.entities}
        assert "country" in entity_types
        assert "theme" in entity_types
        assert any(e.name == "United States" for e in episode.entities)

        # Episode body: latest value + change vs previous
        assert "29200.0" in episode.episode_body
        assert "Bil. of $" in episode.episode_body
        assert "+200.00" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format embeds series_id (group_id)
        assert episode.name.startswith("fred-")
        assert "GDP" in episode.name

        # Source URL traceability
        assert episode.source_url == "https://fred.stlouisfed.org/series/GDP"

    @pytest.mark.asyncio
    async def test_normalize_date_cutoff_returns_none(self, monkeypatch):
        """Observation older than news_max_age_days → None (skipped)."""
        fake_settings = SimpleNamespace(news_max_age_days=14)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )

        record = {
            "series_id": "GDP",
            "date": "2020-01-01",
            "realtime_start": "2020-01-01",
            "value": "21000.0",
            "previous_value": None,
            "units": "Bil. of $",
            "name": "Gross Domestic Product",
            "topic": "GDP Growth",
        }
        adapter = FredAdapter()
        episode = await adapter.normalize(record)
        assert episode is None


class TestFredDedup:
    """Cross-cycle dedup of unchanged snapshots."""

    @pytest.mark.asyncio
    async def test_dedup_identical_snapshots(self, sample_fred_record):
        adapter = FredAdapter()
        ep1 = await adapter.normalize(sample_fred_record)
        ep2 = await adapter.normalize(sample_fred_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


class TestFredHelpers:
    """Module-level helper functions."""

    def test_map_fred_severity_unrate_jump_high(self):
        assert _map_fred_severity("UNRATE", 5.0, 3.5) == "high"

    def test_map_fred_severity_dff_move_high(self):
        assert _map_fred_severity("DFF", 4.5, 3.5) == "high"

    def test_map_fred_severity_default_medium(self):
        assert _map_fred_severity("GDP", 29200.0, 29000.0) == "medium"

    def test_build_fred_body_contains_change(self):
        body = _build_fred_body(
            "GDP",
            "Gross Domestic Product",
            "2026-08-01",
            "29200.0",
            "29000.0",
            "Bil. of $",
        )
        assert "2026-08-01" in body
        assert "29200.0" in body
        assert "+200.00" in body
