"""Integration tests for GdeltAdapter Events-first pipeline.

Tests cover:
- Events-first normalization (_normalize_event_record)
- Event episode body generation (_build_event_episode_body)
- Events-first full pipeline (mock data → filter → normalize)
- GKG fallback path
- _events_tuple_to_dict conversion
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Use a recent date for test data to pass staleness filter (news_max_age_days=14)
_TEST_EVENT_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

from src.adapters.gdelt_adapter import (
    GdeltAdapter,
    _build_event_episode_body,
    _map_goldstein_to_severity,
)
from src.adapters.gdelt_events_parser import EventRecord
from src.adapters.gdelt_mentions_parser import MentionRecord
from src.adapters.models import NormalizedEpisode
from src.ingestion.events_pipeline_filter import (
    DEFAULT_EVENTS_FILTER_CONFIG,
    EventsPipelineFilter,
)


# ── Helpers ──────────────────────────────────────────────────────────


def make_event(
    event_id: str = "1314163330",
    cameo_code: str = "163",
    goldstein_scale: float | None = -7.5,
    actor1_name: str = "United States",
    actor2_name: str = "China",
    source_url: str = "https://reuters.com/article1",
    avg_tone: float | None = -4.2,
    event_date: str | None = None,
) -> EventRecord:
    return EventRecord(
        event_id=event_id,
        event_date=event_date or _TEST_EVENT_DATE,
        actor1_code="USA",
        actor1_name=actor1_name,
        actor2_code="CHN",
        actor2_name=actor2_name,
        cameo_code=cameo_code,
        goldstein_scale=goldstein_scale,
        avg_tone=avg_tone,
        lat=40.0,
        lon=-74.0,
        source_url=source_url,
    )


def make_mention(
    event_id: str = "1314163330",
    confidence: int = 80,
    document_identifier: str = "https://reuters.com/article1",
) -> MentionRecord:
    return MentionRecord(
        event_id=event_id,
        mention_time="20250722000000",
        source_common_name="reuters.com",
        document_identifier=document_identifier,
        mention_confidence=confidence,
        mention_type=1,
    )


# ── Goldstein → Severity mapping ────────────────────────────────────


class TestGoldsteinToSeverity:
    """_map_goldstein_to_severity boundary and edge cases."""

    def test_critical_threshold(self):
        assert _map_goldstein_to_severity(8.0) == "critical"
        assert _map_goldstein_to_severity(9.5) == "critical"
        assert _map_goldstein_to_severity(-8.0) == "critical"

    def test_high_threshold(self):
        assert _map_goldstein_to_severity(6.0) == "high"
        assert _map_goldstein_to_severity(-6.0) == "high"
        assert _map_goldstein_to_severity(7.9) == "high"

    def test_medium_threshold(self):
        assert _map_goldstein_to_severity(4.0) == "medium"
        assert _map_goldstein_to_severity(-4.0) == "medium"
        assert _map_goldstein_to_severity(5.9) == "medium"

    def test_low_threshold(self):
        assert _map_goldstein_to_severity(3.9) == "low"
        assert _map_goldstein_to_severity(-3.9) == "low"
        assert _map_goldstein_to_severity(0.0) == "low"

    def test_none_returns_medium(self):
        assert _map_goldstein_to_severity(None) == "medium"


# ── Event episode body generation ────────────────────────────────────


class TestBuildEventEpisodeBody:
    """_build_event_episode_body() CAMEO-centric Markdown generation."""

    def test_body_contains_cameo_info(self):
        ev = make_event()
        body = _build_event_episode_body(ev, [])
        assert "## GDELT Events Report" in body
        assert "CAMEO 163" in body
        assert "United States" in body
        assert "China" in body
        assert "Goldstein" in body

    def test_body_with_resolved_urls(self):
        ev = make_event()
        urls = [
            "https://reuters.com/article1",
            "https://bloomberg.com/article2",
        ]
        body = _build_event_episode_body(ev, urls)
        assert "reuters.com" in body
        assert "bloomberg.com" in body

    def test_body_with_single_source_url(self):
        ev = make_event(source_url="https://wsj.com/article")
        body = _build_event_episode_body(ev, [])
        assert "wsj.com" in body

    def test_body_shows_event_date(self):
        ev = make_event()
        body = _build_event_episode_body(ev, [])
        assert _TEST_EVENT_DATE in body

    def test_body_shows_tone(self):
        ev = make_event(avg_tone=-4.2)
        body = _build_event_episode_body(ev, [])
        assert "-4.2" in body or "Tone" in body

    def test_body_shows_goldstein_severity(self):
        ev = make_event(goldstein_scale=-7.5)
        body = _build_event_episode_body(ev, [])
        assert "high" in body.lower()

    def test_body_with_multiple_sources(self):
        """Resolved URLs are numbered in the body."""
        ev = make_event()
        urls = ["https://url1.com", "https://url2.com", "https://url3.com"]
        body = _build_event_episode_body(ev, urls)
        assert "1. https://url1.com" in body
        assert "2. https://url2.com" in body
        assert "3. https://url3.com" in body

    def test_body_with_no_actor2(self):
        """When actor2 fields are empty, body shows only actor1."""
        ev = EventRecord(
            event_id="1",
            event_date="2025-07-22",
            actor1_code="USA",
            actor1_name="United States",
            actor2_code="",
            actor2_name="",
            cameo_code="163",
            goldstein_scale=-7.5,
            avg_tone=-4.2,
            lat=None,
            lon=None,
            source_url="https://example.com",
        )
        body = _build_event_episode_body(ev, [])
        assert "United States" in body
        # No "→" with empty actor2 — should not have "United States →"
        assert "United States →" not in body

    def test_body_markdown_integrity(self):
        """Body should be valid Markdown (no broken delimiters)."""
        ev = make_event()
        body = _build_event_episode_body(ev, [])
        assert body.startswith("## GDELT Events Report")
        assert body.endswith("reuters.com/article1")
        # Bold markers should be properly paired (even count)
        assert body.count("**") % 2 == 0


# ── Events normalization ─────────────────────────────────────────────


class TestNormalizeEventRecord:
    """GdeltAdapter._normalize_event_record() — EventRecord → NormalizedEpisode."""

    @pytest.mark.asyncio
    async def test_normalize_event_output_fields(self):
        """Verify all NormalizedEpisode fields from event normalization."""
        adapter = GdeltAdapter()
        ev = make_event()
        resolved_urls = ["https://reuters.com/article1"]
        record_dict = adapter._events_tuple_to_dict(ev, [], resolved_urls)

        episode = await adapter._normalize_event_record(record_dict)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "gdelt_events"
        assert episode.source_description == "GDELT Events V2"
        assert episode.source_url == "https://reuters.com/article1"
        assert episode.severity == "high"  # |Goldstein| = 7.5

        # Verify entities
        assert len(episode.entities) > 0
        entity_names = {e.name for e in episode.entities}
        assert "United States" in entity_names
        assert "China" in entity_names

        # Verify content_hash
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Verify name format
        assert episode.name.startswith("gdelt_events-")
        assert ev.event_id in episode.name

        # Verify body has CAMEO content
        assert "## GDELT Events Report" in episode.episode_body
        assert "CAMEO 163" in episode.episode_body

        # Verify keywords
        assert len(episode.keywords) >= 1

    @pytest.mark.asyncio
    async def test_normalize_event_without_mention_urls(self):
        """Event with no resolved URLs uses source_url."""
        adapter = GdeltAdapter()
        ev = make_event(source_url="https://wsj.com/fallback")
        record_dict = adapter._events_tuple_to_dict(ev, [], [])
        episode = await adapter._normalize_event_record(record_dict)

        assert episode.source_url == "https://wsj.com/fallback"

    @pytest.mark.asyncio
    async def test_normalize_event_no_actor2(self):
        """Event with only actor1 produces single country entity."""
        adapter = GdeltAdapter()
        ev = make_event(actor1_name="United States", actor2_name="")
        record_dict = adapter._events_tuple_to_dict(ev, [], [])
        episode = await adapter._normalize_event_record(record_dict)

        entity_names = {e.name for e in episode.entities}
        assert "United States" in entity_names
        assert "China" not in entity_names

    @pytest.mark.asyncio
    async def test_normalize_event_no_entities_for_untranslated_codes(self):
        """When actor names equal codes, no entities added."""
        adapter = GdeltAdapter()
        ev = make_event(actor1_name="USA", actor2_name="CHN")
        record_dict = adapter._events_tuple_to_dict(ev, [], [])
        episode = await adapter._normalize_event_record(record_dict)

        # USA == actor1_code "USA", so no entity with name "USA"
        entity_names = {e.name for e in episode.entities}
        assert "USA" not in entity_names

    @pytest.mark.asyncio
    async def test_normalize_event_low_severity(self):
        """Low Goldstein score produces low severity."""
        adapter = GdeltAdapter()
        ev = make_event(goldstein_scale=2.0)
        record_dict = adapter._events_tuple_to_dict(ev, [], [])
        episode = await adapter._normalize_event_record(record_dict)
        assert episode.severity == "low"

    @pytest.mark.asyncio
    async def test_normalize_event_critical_severity(self):
        """High Goldstein score produces critical severity."""
        adapter = GdeltAdapter()
        ev = make_event(goldstein_scale=-9.0)
        record_dict = adapter._events_tuple_to_dict(ev, [], [])
        episode = await adapter._normalize_event_record(record_dict)
        assert episode.severity == "critical"


# ── Events tuple to dict ─────────────────────────────────────────────


class TestEventsTupleToDict:
    """GdeltAdapter._events_tuple_to_dict() conversion."""

    def test_conversion_contains_all_fields(self):
        adapter = GdeltAdapter()
        ev = make_event()
        urls = ["https://reuters.com/article1", "https://bloomberg.com/article2"]
        d = adapter._events_tuple_to_dict(ev, [], urls)

        assert d["cameo_code"] == "163"
        assert d["goldstein_scale"] == -7.5
        assert d["actor1_name"] == "United States"
        assert d["actor2_name"] == "China"
        assert d["source_url"] == "https://reuters.com/article1"
        assert d["resolved_urls"] == urls
        assert d["_event_record"] is ev
        expected_valid_at = _TEST_EVENT_DATE.replace("-", "") + "000000"
        assert d["valid_at"] == expected_valid_at

    def test_empty_resolved_urls(self):
        adapter = GdeltAdapter()
        ev = make_event(source_url="https://fallback.com")
        d = adapter._events_tuple_to_dict(ev, [], [])
        assert d["source_url"] == ""  # no resolved URLs → empty
        assert d["resolved_urls"] == []


# ── Full pipeline integration tests ──────────────────────────────────


class TestEventsFirstPipeline:
    """Full Events-first pipeline (mock data → filter → normalize)."""

    @pytest.mark.asyncio
    async def test_events_first_full_pipeline(self):
        """Events-first pipeline: mock download → filter → normalize → episodes."""
        adapter = GdeltAdapter()
        events = [
            make_event(event_id="1", cameo_code="141", goldstein_scale=7.2),
            make_event(event_id="2", cameo_code="163", goldstein_scale=-8.5),
            make_event(event_id="3", cameo_code="010", goldstein_scale=5.0),  # CAMEO: reject
            make_event(event_id="4", cameo_code="141", goldstein_scale=2.0),  # Goldstein: reject
        ]
        mentions = {
            "1": [make_mention(event_id="1", confidence=90, document_identifier="https://r1.com") for _ in range(5)],
            "2": [make_mention(event_id="2", confidence=80, document_identifier="https://r2.com") for _ in range(5)],
            "4": [make_mention(event_id="4", confidence=70, document_identifier="https://r4.com") for _ in range(5)],
        }

        # Run the events pipeline
        result_dicts = await adapter._run_events_pipeline(events, mentions)
        assert len(result_dicts) == 2  # events 1 and 2 pass

        # Normalize
        episodes = await asyncio.gather(*[adapter.normalize(r) for r in result_dicts])
        assert len(episodes) == 2
        for ep in episodes:
            assert ep.source_type == "gdelt_events"
            assert "GDELT Events Report" in ep.episode_body

    @pytest.mark.asyncio
    async def test_gkg_fallback_when_events_produce_zero(self):
        """GKG fallback when Events pipeline produces zero results."""
        adapter = GdeltAdapter()
        events = [
            make_event(event_id="1", cameo_code="010", goldstein_scale=5.0),  # CAMEO: reject
        ]
        mentions: dict[str, list[MentionRecord]] = {}

        # Run events pipeline — should produce 0
        result_dicts = await adapter._run_events_pipeline(events, mentions)
        assert len(result_dicts) == 0

        # Simulate GKG fallback merge
        gkg_records = [{
            "global_event_id": "20260722000000-0",
            "valid_at": "20260722000000",
            "source_collection": "1",
            "domain": "reuters.com",
            "source_url": "https://reuters.com/gkg-article",
            "language": "Eng",
            "themes": "ECON_FINANCIAL_MARKET",
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "-4.5,0.5,2.0",
        }]
        merged = adapter._merge_gkg_data(gkg_records)
        assert len(merged) == 1
        assert merged[0]["cameo_code"] is None  # GKG passthrough → None

    @pytest.mark.asyncio
    async def test_events_tuple_to_dict_with_mention_flow(self):
        """End-to-end: EventRecord → resolve_urls → tuple_to_dict → normalize."""
        adapter = GdeltAdapter()
        ev = make_event()
        ment_list = [make_mention(confidence=90, document_identifier="https://top.com")]

        config = DEFAULT_EVENTS_FILTER_CONFIG
        resolved = EventsPipelineFilter.resolve_urls(ev, ment_list, config)

        record_dict = adapter._events_tuple_to_dict(ev, ment_list, resolved)
        episode = await adapter._normalize_event_record(record_dict)

        assert episode.source_type == "gdelt_events"
        assert episode.source_url == "https://top.com"

    @pytest.mark.asyncio
    async def test_normalize_routes_to_events_path(self):
        """GdeltAdapter.normalize() routes to _normalize_event_record() when _event_record present."""
        adapter = GdeltAdapter()
        ev = make_event()
        record_dict = adapter._events_tuple_to_dict(ev, [], ["https://reuters.com/a1"])
        episode = await adapter.normalize(record_dict)
        assert episode.source_type == "gdelt_events"

    @pytest.mark.asyncio
    async def test_normalize_routes_to_gkg_path(self):
        """GdeltAdapter.normalize() routes to GKG path when no _event_record."""
        adapter = GdeltAdapter()
        gkg_record = {
            "global_event_id": "123",
            "valid_at": _TEST_EVENT_DATE.replace("-", "") + "000000",
            "source_collection": "1",
            "domain": "reuters.com",
            "source_url": "https://reuters.com/article",
            "language": "Eng",
            "themes": "ECON_FINANCIAL_MARKET",
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "0.0,0.0",
            "cameo_code": None,
        }
        episode = await adapter.normalize(gkg_record)
        assert episode.source_type == "gdelt_csv"
