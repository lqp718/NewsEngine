"""Unit tests for EiaAdapter (Phase 1 macro adapter).

Covers: BaseAdapter contract, fetch degradation without API key,
normalize (awaited — no latent un-awaited bug), dedup, helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.adapters.base import BaseAdapter
from src.adapters.eia_adapter import (
    EiaAdapter,
    _EIA_SERIES,
    _build_eia_body,
    _map_eia_severity,
)
from src.adapters.models import NormalizedEpisode


class TestEiaContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = EiaAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert EiaAdapter.SOURCE_TYPE == "eia"

    def test_default_series_constant(self):
        assert "WCRSTUS1" in _EIA_SERIES
        assert "WCRFPUS2" in _EIA_SERIES
        assert "WGASUS1" in _EIA_SERIES


class TestEiaFetchDegrade:
    """fetch() degrades gracefully when eia_api_key is unconfigured."""

    @pytest.mark.asyncio
    async def test_fetch_empty_without_key(self, monkeypatch):
        fake_settings = SimpleNamespace(eia_api_key="", eia_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.eia_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = EiaAdapter()
        result = await adapter.fetch()
        assert result == []
        assert adapter._pre_filter_count == 0


class TestEiaNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_crude_oil_snapshot(self, sample_eia_record):
        adapter = EiaAdapter()
        episode = await adapter.normalize(sample_eia_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "eia"
        assert episode.source_description == "EIA (US Energy Information Administration)"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["series_id"] == "WCRSTUS1"
        assert episode.metadata["value"] == "430500"
        assert episode.metadata["previous_value"] == "432000"
        assert episode.metadata["units"] == "Thousand Barrels"

        # Entities: country + theme
        entity_types = {e.type for e in episode.entities}
        assert "country" in entity_types
        assert "theme" in entity_types
        assert any(e.name == "United States" for e in episode.entities)

        # Episode body: latest value + change vs previous period
        assert "430500" in episode.episode_body
        assert "Thousand Barrels" in episode.episode_body
        assert "-1500.00" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format embeds series_id (group_id)
        assert episode.name.startswith("eia-")
        assert "WCRSTUS1" in episode.name

        # Source URL traceability
        assert episode.source_url.startswith("https://api.eia.gov/v2/")

    @pytest.mark.asyncio
    async def test_normalize_date_cutoff_returns_none(self, monkeypatch):
        """Period older than news_max_age_days → None (skipped)."""
        fake_settings = SimpleNamespace(news_max_age_days=14)
        monkeypatch.setattr(
            "src.adapters.eia_adapter.get_settings",
            lambda: fake_settings,
        )

        record = {
            "series_id": "WCRSTUS1",
            "period": "2015-03-01",
            "value": "400000",
            "previous_value": None,
            "units": "Thousand Barrels",
            "name": "Weekly U.S. Crude Oil Ending Stocks",
            "topic": "Crude Oil Inventories",
        }
        adapter = EiaAdapter()
        episode = await adapter.normalize(record)
        assert episode is None


class TestEiaDedup:
    """Cross-cycle dedup of unchanged energy snapshots."""

    @pytest.mark.asyncio
    async def test_dedup_identical_snapshots(self, sample_eia_record):
        adapter = EiaAdapter()
        ep1 = await adapter.normalize(sample_eia_record)
        ep2 = await adapter.normalize(sample_eia_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


class TestEiaHelpers:
    """Module-level helper functions."""

    def test_map_eia_severity_large_inventory_swing_high(self):
        assert _map_eia_severity("WCRSTUS1", 440000.0, 429000.0) == "high"

    def test_map_eia_severity_gasoline_move_high(self):
        assert _map_eia_severity("WGASUS1", 3.5, 3.2) == "high"

    def test_map_eia_severity_default_medium(self):
        assert _map_eia_severity("WCRFPUS2", 13500.0, 13400.0) == "medium"

    def test_build_eia_body_contains_change(self):
        body = _build_eia_body(
            "WCRSTUS1",
            "Weekly U.S. Crude Oil Ending Stocks",
            "2026-08-08",
            "430500",
            "432000",
            "Thousand Barrels",
        )
        assert "2026-08-08" in body
        assert "430500" in body
        assert "-1500.00" in body
