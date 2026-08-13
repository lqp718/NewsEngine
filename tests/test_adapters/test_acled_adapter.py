"""Unit tests for AcledAdapter (Phase 1 macro adapter).

Covers: BaseAdapter contract, fetch degradation without key/email,
normalize (awaited — no latent un-awaited bug) with severity boundary
assertions, dedup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters.acled_adapter import (
    AcledAdapter,
    _build_acled_body,
    _map_acled_severity,
)
from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode


class TestAcledContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = AcledAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert AcledAdapter.SOURCE_TYPE == "acled"


class TestAcledFetchDegrade:
    """fetch() degrades gracefully when key or email is unconfigured."""

    @pytest.mark.asyncio
    async def test_fetch_empty_without_key_and_email(self, monkeypatch):
        fake_settings = SimpleNamespace(acled_api_key="", acled_email="")
        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = AcledAdapter()
        result = await adapter.fetch()
        assert result == []
        assert adapter._pre_filter_count == 0

    @pytest.mark.asyncio
    async def test_fetch_empty_without_email(self, monkeypatch):
        """Key present but email missing → still degrade."""
        fake_settings = SimpleNamespace(
            acled_api_key="some-key", acled_email=""
        )
        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = AcledAdapter()
        result = await adapter.fetch()
        assert result == []


class TestAcledSeverity:
    """Fatality-based severity mapping boundaries."""

    def test_critical_at_100_plus(self):
        assert _map_acled_severity(120, "Battles") == "critical"
        assert _map_acled_severity(100, "Battles") == "critical"

    def test_high_at_25_plus(self):
        assert _map_acled_severity(25, "Riots") == "high"
        assert _map_acled_severity(26, "Riots") == "high"

    def test_medium_at_1_plus(self):
        assert _map_acled_severity(1, "Riots") == "medium"
        assert _map_acled_severity(24, "Riots") == "medium"

    def test_low_at_zero_fatalities(self):
        assert _map_acled_severity(0, "Riots") == "low"
        assert _map_acled_severity(0, "Protests") == "low"

    def test_battles_minimum_medium(self):
        """Battles/explosions are at least medium even with 0 fatalities."""
        assert _map_acled_severity(0, "Battles") == "medium"
        assert _map_acled_severity(0, "Explosions/Remote violence") == "medium"


class TestAcledNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_high_fatality_battle(self, sample_acled_record):
        adapter = AcledAdapter()
        episode = await adapter.normalize(sample_acled_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "acled"
        assert episode.source_description == "ACLED Armed Conflict Location & Event Data"

        # Fatality-based severity: 120 fatalities → critical
        assert episode.severity == "critical"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["event_type"] == "Battles"
        assert episode.metadata["country"] == "Ukraine"
        assert episode.metadata["fatalities"] == 120
        assert episode.metadata["actor1"] == "Military Forces of Russia"
        assert episode.metadata["actor2"] == "Military Forces of Ukraine"

        # Entities: country + actor organizations
        entity_types = {e.type for e in episode.entities}
        assert "country" in entity_types
        assert "organization" in entity_types
        assert any(e.name == "Ukraine" for e in episode.entities)

        # Episode body
        assert "Battles" in episode.episode_body
        assert "Ukraine" in episode.episode_body
        assert "120" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("acled-")

    @pytest.mark.asyncio
    async def test_normalize_zero_fatality_riot_low(self):
        """Zero-fatality riot → low severity."""
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        record = {
            "event_id_cnty": "USA9999",
            "event_date": recent,
            "event_type": "Riots",
            "country": "United States",
            "actor1": "Protesters",
            "actor2": "",
            "fatalities": 0,
            "notes": "",
        }
        adapter = AcledAdapter()
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.severity == "low"
        assert episode.metadata["fatalities"] == 0

    @pytest.mark.asyncio
    async def test_normalize_date_cutoff_returns_none(self, monkeypatch):
        """Event older than news_max_age_days → None (skipped)."""
        fake_settings = SimpleNamespace(news_max_age_days=14)
        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings",
            lambda: fake_settings,
        )

        record = {
            "event_id_cnty": "OLD0001",
            "event_date": "2015-03-01",
            "event_type": "Battles",
            "country": "Syria",
            "actor1": "Government",
            "actor2": "Rebels",
            "fatalities": 5,
            "notes": "",
        }
        adapter = AcledAdapter()
        episode = await adapter.normalize(record)
        assert episode is None

    @pytest.mark.asyncio
    async def test_normalize_invalid_date_returns_none(self):
        adapter = AcledAdapter()
        record = {
            "event_id_cnty": "BAD0001",
            "event_date": "not-a-date",
            "event_type": "Battles",
            "country": "Ukraine",
            "fatalities": 1,
            "notes": "",
        }
        episode = await adapter.normalize(record)
        assert episode is None


class TestAcledDedup:
    """Cross-cycle dedup of identical conflict events."""

    @pytest.mark.asyncio
    async def test_dedup_identical_events(self, sample_acled_record):
        adapter = AcledAdapter()
        ep1 = await adapter.normalize(sample_acled_record)
        ep2 = await adapter.normalize(sample_acled_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


class TestAcledHelpers:
    """Module-level helper functions."""

    def test_build_acled_body(self):
        body = _build_acled_body(
            "UKR1234",
            "2026-08-10",
            "Battles",
            "Ukraine",
            "Actor A",
            "Actor B",
            12,
            "Some notes",
        )
        assert "UKR1234" in body
        assert "Battles" in body
        assert "Ukraine" in body
        assert "12" in body
        assert "Actor A" in body
        assert "Some notes" in body

    def test_build_acled_body_trims_long_notes(self):
        long_notes = "x" * 600
        body = _build_acled_body(
            "UKR1", "2026-08-10", "Riots", "USA", "", "", 0, long_notes
        )
        assert len(body) < len(long_notes) + 300
        assert "..." in body
