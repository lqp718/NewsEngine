"""Unit tests for EventsPipelineFilter.

Tests cover:
- CAMEO filter (prefix/exact/contains modes, edge cases)
- Goldstein filter (threshold boundaries, null handling)
- Mentions filter (count check, skip-on-missing)
- URL resolution (confidence sort, dedup, max_urls truncation, fallback)
- Config loading (missing file, invalid JSON, missing fields, validation)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from src.adapters.gdelt_events_parser import EventRecord
from src.adapters.gdelt_mentions_parser import MentionRecord
from src.ingestion.events_pipeline_filter import (
    DEFAULT_EVENTS_FILTER_CONFIG,
    EventsPipelineFilter,
)


# ── Helpers ──────────────────────────────────────────────────────────


def make_event(
    event_id: str = "1",
    cameo_code: str = "141",
    goldstein_scale: float | None = 7.2,
    actor1_name: str = "United States",
    actor2_name: str = "",
    source_url: str = "https://example.com/article",
) -> EventRecord:
    """Create a test EventRecord with minimal required fields."""
    return EventRecord(
        event_id=event_id,
        event_date="2025-07-22",
        actor1_code="USA",
        actor1_name=actor1_name,
        actor2_code="CHN",
        actor2_name=actor2_name,
        cameo_code=cameo_code,
        goldstein_scale=goldstein_scale,
        avg_tone=-4.2,
        lat=40.0,
        lon=-74.0,
        source_url=source_url,
    )


def make_mention(
    event_id: str = "1",
    confidence: int = 50,
    document_identifier: str = "https://reuters.com/article1",
) -> MentionRecord:
    """Create a test MentionRecord."""
    return MentionRecord(
        event_id=event_id,
        mention_time="20250722000000",
        source_common_name="reuters.com",
        document_identifier=document_identifier,
        mention_confidence=confidence,
        mention_type=1,
    )


# ── CAMEO filter tests ──────────────────────────────────────────────


class TestCameoFilter:
    """EventsPipelineFilter._cameo_filter() — three matching modes."""

    def _filter(self, events: list[EventRecord], config: dict | None = None) -> list[EventRecord]:
        cfg = config or DEFAULT_EVENTS_FILTER_CONFIG
        return EventsPipelineFilter._cameo_filter(events, cfg)

    def test_prefix_match_default_codes(self):
        """CAMEO '141' (protest) root '14' matches default prefix list."""
        events = [
            make_event(cameo_code="141"),   # protest → prefix 14
            make_event(cameo_code="010"),   # public statement → no match
        ]
        result = self._filter(events)
        assert len(result) == 1
        assert result[0].cameo_code == "141"

    def test_prefix_match_sanctions(self):
        """CAMEO '163' (sanctions) root '16' matches default codes."""
        events = [make_event(cameo_code="163")]
        result = self._filter(events)
        assert len(result) == 1

    def test_prefix_match_fight(self):
        """CAMEO '190' (conventional force) root '19' matches."""
        events = [make_event(cameo_code="190")]
        result = self._filter(events)
        assert len(result) == 1

    def test_prefix_match_no_match(self):
        """CAMEO '010' (public statement) root '01' not in [14,16,17,18,19,20]."""
        events = [make_event(cameo_code="010")]
        result = self._filter(events)
        assert len(result) == 0

    def test_exact_match_mode(self):
        """Exact mode: only exact CAMEO code match passes."""
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        config["cameo_filter"] = {"mode": "exact", "codes": ["141"]}
        events = [
            make_event(cameo_code="141"),    # exact match
            make_event(cameo_code="1411"),   # not exact
            make_event(cameo_code="140"),    # not exact
        ]
        result = EventsPipelineFilter._cameo_filter(events, config)
        assert len(result) == 1
        assert result[0].cameo_code == "141"

    def test_contains_match_mode(self):
        """Contains mode: code appears anywhere in cameo string."""
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        config["cameo_filter"] = {"mode": "contains", "codes": ["163"]}
        events = [
            make_event(cameo_code="163"),    # direct match
            make_event(cameo_code="1163"),   # contains
            make_event(cameo_code="162"),    # no match
        ]
        result = EventsPipelineFilter._cameo_filter(events, config)
        assert len(result) == 2

    def test_empty_cameo_code(self):
        """Empty cameo_code is rejected."""
        events = [make_event(cameo_code="")]
        result = self._filter(events)
        assert len(result) == 0

    def test_short_cameo_code(self):
        """cameo_code with < 2 chars is rejected."""
        events = [make_event(cameo_code="1")]
        result = self._filter(events)
        assert len(result) == 0

    def test_empty_codes_list_rejects_all(self):
        """Empty codes list rejects all events."""
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        config["cameo_filter"] = {"mode": "prefix_match", "codes": []}
        events = [make_event(cameo_code="141")]
        result = EventsPipelineFilter._cameo_filter(events, config)
        assert len(result) == 0

    def test_mass_violence_prefix_matches(self):
        """CAMEO '201' (mass expulsion) root '20' matches."""
        events = [make_event(cameo_code="201")]
        result = self._filter(events)
        assert len(result) == 1

    def test_coerce_prefix_matches(self):
        """CAMEO '173' (arrest) root '17' matches."""
        events = [make_event(cameo_code="173")]
        result = self._filter(events)
        assert len(result) == 1

    def test_assault_prefix_matches(self):
        """CAMEO '186' (assassination) root '18' matches."""
        events = [make_event(cameo_code="186")]
        result = self._filter(events)
        assert len(result) == 1


# ── Goldstein filter tests ──────────────────────────────────────────


class TestGoldsteinFilter:
    """EventsPipelineFilter._goldstein_filter() — absolute intensity check."""

    def _filter(self, events: list[EventRecord], min_abs: float = 5.0) -> list[EventRecord]:
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        config["goldstein"] = {"min_abs_value": min_abs}
        return EventsPipelineFilter._goldstein_filter(events, config)

    def test_strong_positive_passes(self):
        """Goldstein 7.2 >= 5.0 → passes."""
        events = [make_event(goldstein_scale=7.2)]
        assert len(self._filter(events)) == 1

    def test_strong_negative_passes(self):
        """Goldstein -8.5 >= 5.0 (abs) → passes."""
        events = [make_event(goldstein_scale=-8.5)]
        assert len(self._filter(events)) == 1

    def test_weak_signal_rejected(self):
        """Goldstein 2.3 < 5.0 → rejected."""
        events = [make_event(goldstein_scale=2.3)]
        assert len(self._filter(events)) == 0

    def test_zero_goldstein_rejected(self):
        """Goldstein 0.0 < 5.0 → rejected."""
        events = [make_event(goldstein_scale=0.0)]
        assert len(self._filter(events)) == 0

    def test_null_goldstein_rejected(self):
        """None Goldstein → rejected (null is not >= any min_abs)."""
        events = [make_event(goldstein_scale=None)]
        assert len(self._filter(events)) == 0

    def test_exactly_at_threshold(self):
        """Goldstein 5.0 >= 5.0 → passes."""
        events = [make_event(goldstein_scale=5.0)]
        assert len(self._filter(events)) == 1

    def test_negative_exactly_at_threshold(self):
        """Goldstein -5.0 >= 5.0 → passes (abs)."""
        events = [make_event(goldstein_scale=-5.0)]
        assert len(self._filter(events)) == 1

    def test_high_threshold_rejects_moderate(self):
        """min_abs=8.0, Goldstein 7.0 < 8.0 → rejected."""
        events = [make_event(goldstein_scale=7.0)]
        assert len(self._filter(events, min_abs=8.0)) == 0

    def test_mixed_thresholds(self):
        """Multiple events with varying Goldstein scores."""
        events = [
            make_event(event_id="1", goldstein_scale=7.2),   # pass
            make_event(event_id="2", goldstein_scale=2.3),   # reject
            make_event(event_id="3", goldstein_scale=-8.5),  # pass
            make_event(event_id="4", goldstein_scale=None),  # reject
        ]
        result = self._filter(events)
        assert len(result) == 2
        assert {ev.event_id for ev in result} == {"1", "3"}


# ── Mentions filter tests ───────────────────────────────────────────


class TestMentionsFilter:
    """EventsPipelineFilter._mentions_filter() — minimum mention count."""

    def _filter(
        self,
        events: list[EventRecord],
        mentions_by_event: dict[str, list[MentionRecord]] | None = None,
        min_count: int = 5,
    ) -> list[EventRecord]:
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        config["mentions"] = {"min_count": min_count}
        return EventsPipelineFilter._mentions_filter(
            events, mentions_by_event or {}, config
        )

    def test_sufficient_mentions(self):
        """12 mentions >= 5 → passes."""
        events = [make_event(event_id="1")]
        mentions = {"1": [make_mention() for _ in range(12)]}
        assert len(self._filter(events, mentions)) == 1

    def test_insufficient_mentions(self):
        """2 mentions < 5 → rejected."""
        events = [make_event(event_id="1")]
        mentions = {"1": [make_mention() for _ in range(2)]}
        assert len(self._filter(events, mentions)) == 0

    def test_exactly_at_threshold(self):
        """5 mentions == 5 → passes."""
        events = [make_event(event_id="1")]
        mentions = {"1": [make_mention() for _ in range(5)]}
        assert len(self._filter(events, mentions)) == 1

    def test_no_mentions_for_event(self):
        """Event not in mentions dict → 0 mentions < 5 → rejected."""
        events = [make_event(event_id="1")]
        mentions = {"2": [make_mention()]}
        assert len(self._filter(events, mentions)) == 0

    def test_mentions_not_available_skip(self):
        """Empty mentions_by_event → all events pass (skip filter)."""
        events = [
            make_event(event_id="1"),
            make_event(event_id="2"),
        ]
        assert len(self._filter(events, {})) == 2

    @staticmethod
    def test_mentions_not_available_none():
        """None mentions_by_event → all events pass (skip filter)."""
        events = [make_event(event_id="1")]
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        result = EventsPipelineFilter._mentions_filter(events, {}, config)
        assert len(result) == 1

    def test_mixed_mention_counts(self):
        """Multiple events with varying mention counts."""
        events = [
            make_event(event_id="1"),
            make_event(event_id="2"),
            make_event(event_id="3"),
        ]
        mentions = {
            "1": [make_mention() for _ in range(10)],  # pass
            "2": [make_mention() for _ in range(3)],   # reject
            # "3" not in dict → reject
        }
        result = self._filter(events, mentions)
        assert len(result) == 1
        assert result[0].event_id == "1"


# ── URL resolution tests ────────────────────────────────────────────


class TestResolveUrls:
    """EventsPipelineFilter.resolve_urls() — mentions_first strategy."""

    def _resolve(
        self,
        event: EventRecord,
        mentions: list[MentionRecord] | None = None,
        max_urls: int = 3,
    ) -> list[str]:
        config = dict(DEFAULT_EVENTS_FILTER_CONFIG)
        config["url_resolution"] = {"strategy": "mentions_first", "max_urls_per_event": max_urls}
        return EventsPipelineFilter.resolve_urls(event, mentions or [], config)

    def test_confidence_sort(self):
        """URLs sorted by mention_confidence descending."""
        event = make_event()
        mentions = [
            make_mention(confidence=10, document_identifier="https://low.com"),
            make_mention(confidence=90, document_identifier="https://high.com"),
            make_mention(confidence=50, document_identifier="https://medium.com"),
        ]
        urls = self._resolve(event, mentions)
        assert urls == ["https://high.com", "https://medium.com", "https://low.com"]

    def test_dedup(self):
        """Duplicate document_identifiers are removed."""
        event = make_event()
        mentions = [
            make_mention(confidence=80, document_identifier="https://dup.com"),
            make_mention(confidence=70, document_identifier="https://dup.com"),
            make_mention(confidence=60, document_identifier="https://unique.com"),
        ]
        urls = self._resolve(event, mentions)
        assert urls == ["https://dup.com", "https://unique.com"]

    def test_max_urls_truncation(self):
        """More mentions than max_urls_per_event → only top N kept."""
        event = make_event()
        mentions = [
            make_mention(confidence=i * 10, document_identifier=f"https://url{i}.com")
            for i in range(10)
        ]
        urls = self._resolve(event, mentions, max_urls=3)
        assert len(urls) == 3
        assert urls == ["https://url9.com", "https://url8.com", "https://url7.com"]

    def test_fallback_to_source_url(self):
        """No mentions → fallback to EventRecord.source_url."""
        event = make_event(source_url="https://fallback.com")
        urls = self._resolve(event, [])
        assert urls == ["https://fallback.com"]

    def test_empty_mentions_and_no_source_url(self):
        """No mentions and empty source_url → empty list."""
        event = make_event(source_url="")
        urls = self._resolve(event, [])
        assert urls == []

    def test_all_mentions_empty_document_identifier(self):
        """All mentions have empty document_identifier → fallback to source_url."""
        event = make_event(source_url="https://fallback.com")
        mentions = [
            make_mention(confidence=50, document_identifier=""),
            make_mention(confidence=50, document_identifier="  "),
        ]
        urls = self._resolve(event, mentions)
        assert urls == ["https://fallback.com"]

    def test_empty_strings_skipped(self):
        """Empty document_identifier strings are skipped."""
        event = make_event(source_url="https://fallback.com")
        mentions = [
            make_mention(confidence=80, document_identifier="https://good.com"),
            make_mention(confidence=60, document_identifier=""),
            make_mention(confidence=40, document_identifier="  "),
        ]
        urls = self._resolve(event, mentions)
        assert "https://good.com" in urls
        assert "" not in urls


# ── Config loading tests ────────────────────────────────────────────


class TestLoadConfig:
    """EventsPipelineFilter._load_config() — loading, validation, fallback."""

    @pytest.fixture
    def filter_(self):
        return EventsPipelineFilter()

    def test_default_config_content(self):
        """DEFAULT_EVENTS_FILTER_CONFIG has expected structure."""
        assert DEFAULT_EVENTS_FILTER_CONFIG["version"] == "1.0"
        assert DEFAULT_EVENTS_FILTER_CONFIG["cameo_filter"]["mode"] == "prefix_match"
        assert "14" in DEFAULT_EVENTS_FILTER_CONFIG["cameo_filter"]["codes"]
        assert DEFAULT_EVENTS_FILTER_CONFIG["goldstein"]["min_abs_value"] == 5.0
        assert DEFAULT_EVENTS_FILTER_CONFIG["mentions"]["min_count"] == 5
        assert DEFAULT_EVENTS_FILTER_CONFIG["url_resolution"]["max_urls_per_event"] == 3

    def test_missing_file_uses_defaults(self, filter_):
        """Non-existent config file → returns defaults."""
        f = EventsPipelineFilter(config_path="/tmp/nonexistent_config_file.json")
        config = f._load_config()
        assert config["cameo_filter"]["mode"] == "prefix_match"
        assert config["goldstein"]["min_abs_value"] == 5.0

    def test_invalid_json_uses_defaults(self, filter_):
        """Malformed JSON → returns defaults."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("{invalid json,}")
            tmp_path = f.name
        try:
            filt = EventsPipelineFilter(config_path=tmp_path)
            config = filt._load_config()
            assert config["cameo_filter"]["mode"] == "prefix_match"
        finally:
            os.unlink(tmp_path)

    def test_valid_file_overrides_defaults(self, filter_):
        """Valid config file → returns parsed config."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "version": "1.0",
                "cameo_filter": {"mode": "exact", "codes": ["163"]},
                "goldstein": {"min_abs_value": 8.0},
                "mentions": {"min_count": 10},
                "url_resolution": {"strategy": "mentions_first", "max_urls_per_event": 5},
            }, f)
            tmp_path = f.name
        try:
            filt = EventsPipelineFilter(config_path=tmp_path)
            config = filt._load_config()
            assert config["cameo_filter"]["mode"] == "exact"
            assert config["cameo_filter"]["codes"] == ["163"]
            assert config["goldstein"]["min_abs_value"] == 8.0
            assert config["mentions"]["min_count"] == 10
            assert config["url_resolution"]["max_urls_per_event"] == 5
        finally:
            os.unlink(tmp_path)

    def test_missing_cameo_fields_use_defaults(self, filter_):
        """Missing cameo_filter fields → defaults used."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "version": "1.0",
                "cameo_filter": {"codes": ["163"]},  # missing "mode"
                "goldstein": {"min_abs_value": 7.0},
                "mentions": {"min_count": 8},
                "url_resolution": {"strategy": "mentions_first", "max_urls_per_event": 4},
            }, f)
            tmp_path = f.name
        try:
            filt = EventsPipelineFilter(config_path=tmp_path)
            config = filt._load_config()
            # mode should fall back to default since missing
            assert config["cameo_filter"]["mode"] == "prefix_match"
            assert config["cameo_filter"]["codes"] == ["163"]
            assert config["goldstein"]["min_abs_value"] == 7.0
        finally:
            os.unlink(tmp_path)

    def test_unknown_cameo_mode_falls_back(self, filter_):
        """Unknown cameo_filter.mode → fallback to prefix_match."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "version": "1.0",
                "cameo_filter": {"mode": "regex", "codes": ["14"]},
                "goldstein": {"min_abs_value": 5.0},
                "mentions": {"min_count": 5},
                "url_resolution": {"strategy": "mentions_first", "max_urls_per_event": 3},
            }, f)
            tmp_path = f.name
        try:
            filt = EventsPipelineFilter(config_path=tmp_path)
            config = filt._load_config()
            assert config["cameo_filter"]["mode"] == "prefix_match"
        finally:
            os.unlink(tmp_path)

    def test_negative_goldstein_threshold(self, filter_):
        """Negative goldstein.min_abs_value → default 5.0."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "version": "1.0",
                "cameo_filter": {"mode": "prefix_match", "codes": ["14"]},
                "goldstein": {"min_abs_value": -3.0},
                "mentions": {"min_count": 5},
                "url_resolution": {"strategy": "mentions_first", "max_urls_per_event": 3},
            }, f)
            tmp_path = f.name
        try:
            filt = EventsPipelineFilter(config_path=tmp_path)
            config = filt._load_config()
            assert config["goldstein"]["min_abs_value"] == 5.0  # default
        finally:
            os.unlink(tmp_path)

    def test_zero_mentions_threshold(self, filter_):
        """Zero mentions.min_count → default 5."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({
                "version": "1.0",
                "cameo_filter": {"mode": "prefix_match", "codes": ["14"]},
                "goldstein": {"min_abs_value": 5.0},
                "mentions": {"min_count": 0},
                "url_resolution": {"strategy": "mentions_first", "max_urls_per_event": 3},
            }, f)
            tmp_path = f.name
        try:
            filt = EventsPipelineFilter(config_path=tmp_path)
            config = filt._load_config()
            assert config["mentions"]["min_count"] == 5  # default
        finally:
            os.unlink(tmp_path)


# ── Pipeline integration tests ──────────────────────────────────────


class TestEventsPipelineFilter:
    """Full pipeline integration: all three stages together."""

    def test_all_filters_pass(self):
        """Event passes CAMEO, Goldstein, and Mentions filters."""
        filt = EventsPipelineFilter()
        events = [make_event(cameo_code="141", goldstein_scale=7.2)]
        mentions = {"1": [make_mention() for _ in range(5)]}
        result = filt.filter(events, mentions)
        assert len(result) == 1
        assert result[0][0].event_id == "1"

    def test_cameo_filter_blocks(self):
        """CAMEO filter blocks event with non-matching code."""
        filt = EventsPipelineFilter()
        events = [make_event(cameo_code="010", goldstein_scale=7.2)]
        mentions = {"1": [make_mention() for _ in range(5)]}
        result = filt.filter(events, mentions)
        assert len(result) == 0

    def test_goldstein_filter_blocks(self):
        """Goldstein filter blocks event with low score."""
        filt = EventsPipelineFilter()
        events = [make_event(cameo_code="141", goldstein_scale=2.3)]
        mentions = {"1": [make_mention() for _ in range(5)]}
        result = filt.filter(events, mentions)
        assert len(result) == 0

    def test_mentions_filter_blocks(self):
        """Mentions filter blocks event with insufficient mentions."""
        filt = EventsPipelineFilter()
        events = [make_event(cameo_code="141", goldstein_scale=7.2)]
        mentions = {"1": [make_mention() for _ in range(2)]}
        result = filt.filter(events, mentions)
        assert len(result) == 0

    def test_no_matching_events_returns_empty(self):
        """No events match any filter → empty result."""
        filt = EventsPipelineFilter()
        events = [make_event(cameo_code="010", goldstein_scale=2.3)]
        mentions = {"1": [make_mention() for _ in range(2)]}
        result = filt.filter(events, mentions)
        assert len(result) == 0

    def test_empty_events_list(self):
        """Empty events list → empty result."""
        filt = EventsPipelineFilter()
        result = filt.filter([], {})
        assert len(result) == 0

    def test_multiple_events_mixed_results(self):
        """Mixed events: some pass, some fail different stages."""
        filt = EventsPipelineFilter()
        events = [
            make_event(event_id="1", cameo_code="141", goldstein_scale=7.2),  # all pass
            make_event(event_id="2", cameo_code="010", goldstein_scale=7.2),  # CAMEO fail
            make_event(event_id="3", cameo_code="141", goldstein_scale=2.3),  # Goldstein fail
            make_event(event_id="4", cameo_code="141", goldstein_scale=7.2),  # all pass
            make_event(event_id="5", cameo_code="141", goldstein_scale=7.2),  # all pass
        ]
        mentions = {
            "1": [make_mention() for _ in range(5)],
            "4": [make_mention() for _ in range(5)],
            "5": [make_mention() for _ in range(5)],
        }
        result = filt.filter(events, mentions)
        assert len(result) == 3
        result_ids = {ev.event_id for ev, _ in result}
        assert result_ids == {"1", "4", "5"}

    def test_mentions_attached_to_result(self):
        """Result tuples carry the correct MentionRecords."""
        filt = EventsPipelineFilter()
        event = make_event(event_id="1", cameo_code="141", goldstein_scale=7.2)
        ment = [make_mention(confidence=i * 10) for i in range(5)]
        result = filt.filter([event], {"1": ment})
        assert len(result) == 1
        ev, got_mentions = result[0]
        assert ev.event_id == "1"
        assert len(got_mentions) == 5
