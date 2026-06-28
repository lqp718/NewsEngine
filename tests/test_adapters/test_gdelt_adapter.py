"""Unit tests for GdeltAdapter normalisation logic.

All tests use synthetic data — no HTTP requests.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.adapters.gdelt_adapter import (
    _map_tone_to_severity,
    _parse_gkg_datetime,
    _parse_location,
    GdeltAdapter,
)
from src.adapters.models import NormalizedEpisode


class TestToneToSeverity:
    """_map_tone_to_severity boundary and edge cases."""

    def test_positive_tone_low(self):
        assert _map_tone_to_severity("10.36") == "low"

    def test_high_positive_tone_low(self):
        assert _map_tone_to_severity("100.0") == "low"

    def test_neutral_tone_medium(self):
        assert _map_tone_to_severity("2.5") == "medium"

    def test_zero_tone_medium(self):
        assert _map_tone_to_severity("0.0") == "medium"

    def test_negative_tone_medium(self):
        assert _map_tone_to_severity("-4.9") == "medium"

    def test_negative_five_tone_medium(self):
        assert _map_tone_to_severity("-5.0") == "medium"

    def test_negative_tone_high(self):
        assert _map_tone_to_severity("-8.7") == "high"

    def test_extreme_negative_tone_critical(self):
        assert _map_tone_to_severity("-18.2") == "critical"

    def test_empty_tone_default_medium(self):
        assert _map_tone_to_severity(None) == "medium"

    def test_blank_tone_default_medium(self):
        assert _map_tone_to_severity("") == "medium"

    def test_invalid_tone_default_medium(self):
        assert _map_tone_to_severity("not_a_number") == "medium"


class TestParseLocation:
    """_parse_location coordinate cleaning."""

    def test_single_location(self):
        result = _parse_location(
            "#1#2#Beijing,Beijing,China#CN#CN"
        )
        assert "Beijing" in result
        assert "China" in result

    def test_multiple_locations(self):
        result = _parse_location(
            "#1#2#Beijing,Beijing,China#CN#CN|#1#2#Shanghai,Shanghai,China#CN#CN"
        )
        assert "Beijing" in result
        assert "Shanghai" in result
        assert "China" in result

    def test_location_with_vnm_suffix(self):
        result = _parse_location(
            "#1#2#Beijing,Beijing,China#CN#CN|#VNM"
        )
        assert "Beijing" in result
        assert "China" in result

    def test_empty_location(self):
        result = _parse_location("")
        assert result == ""

    def test_location_with_country_code_translated(self):
        """Country code (sub[4]) is translated via translate_actor and appended."""
        result = _parse_location(
            "#1#2#Beijing,Beijing,China#CHN#CN"
        )
        # CHN translates to "China" → output: "Beijing, China (China)"
        assert "Beijing" in result
        assert " (China)" in result

    def test_location_with_untranslated_country_code(self):
        """2-letter country code not in actor codebook leaves name unchanged."""
        result = _parse_location(
            "#1#2#Beijing,Beijing,China#CN#CN"
        )
        # CN is not a 3-letter ISO code in the actor codebook, so no translation
        assert "Beijing" in result
        assert "China" in result
        # No parenthetical appended
        assert " (" not in result

    def test_location_with_vnm_translated(self):
        """VNM is in the actor codebook and should be translated."""
        result = _parse_location(
            "#1#2#Hanoi,Hanoi,Vietnam#VNM#VN"
        )
        # VNM translates to "Vietnam" → output: "Hanoi, Vietnam (Vietnam)"
        assert "Hanoi" in result
        assert " (Vietnam)" in result


class TestParseDatetime:
    """GKG datetime parsing."""

    def test_valid_14_digit(self):
        dt = _parse_gkg_datetime("20250609010101")
        assert isinstance(dt, datetime)
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 9

    def test_invalid_fallback_to_now(self):
        dt = _parse_gkg_datetime("not_a_date")
        assert isinstance(dt, datetime)


class TestGdeltNormalize:
    """GdeltAdapter.normalize() output fields."""

    def test_normalize_output_fields(self, sample_gkg_record):
        adapter = GdeltAdapter()
        episode = adapter.normalize(sample_gkg_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "gdelt_csv"
        assert episode.source_description == "GDELT GKG V2"
        assert episode.source_url == "http://example.com/article1"
        assert episode.severity == "high"  # tone=-8.7
        assert len(episode.entities) > 0

        # Verify entity types present
        entity_types = {e.type for e in episode.entities}
        assert "person" in entity_types
        assert "organization" in entity_types
        assert "location" in entity_types
        assert "theme" in entity_types

        # Verify location cleaning
        locs = [e.name for e in episode.entities if e.type == "location"]
        assert any("Beijing" in l for l in locs)
        assert any("Shanghai" in l for l in locs)

        # Verify theme entity names are translated
        themes = [e.name for e in episode.entities if e.type == "theme"]
        assert any("ECON_FINANCIAL_MARKET" in t for t in themes)
        assert any("TAX_FNCACT_REG_INVEST" in t for t in themes)

        # Verify content_hash is valid
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("gdelt_csv-")
        assert len(episode.name) > 20

    def test_normalize_empty_tone(self, sample_gkg_record):
        record = dict(sample_gkg_record)
        record["tone"] = ""
        adapter = GdeltAdapter()
        episode = adapter.normalize(record)
        assert episode.severity == "medium"

    def test_normalize_no_entities(self):
        record = {
            "global_event_id": "1",
            "valid_at": "20250609010101",
            "source_collection": "1",
            "source_url": "http://example.com",
            "language": "Eng",
            "persons": "",
            "organizations": "",
            "locations": "",
            "themes": "",
            "tone": "0.0",
        }
        adapter = GdeltAdapter()
        episode = adapter.normalize(record)
        assert len(episode.entities) == 0
        assert episode.severity == "medium"

    def test_normalize_episode_body_has_themes(self, sample_gkg_record):
        adapter = GdeltAdapter()
        episode = adapter.normalize(sample_gkg_record)
        assert "Themes:" in episode.episode_body
        assert "ECON_FINANCIAL_MARKET" in episode.episode_body
