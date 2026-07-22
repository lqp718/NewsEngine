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
    """_map_tone_to_severity boundary and edge cases.

    Note: Actual GKG tone format is comma-separated
    (e.g. "-4.26,0.57,3.15,0.82,-1.25"). The first value
    (average tone) is extracted for severity mapping.
    """

    def test_positive_tone_low(self):
        assert _map_tone_to_severity("10.36,0.57") == "low"

    def test_high_positive_tone_low(self):
        assert _map_tone_to_severity("100.0,0.0") == "low"

    def test_neutral_tone_medium(self):
        assert _map_tone_to_severity("2.5,1.0") == "medium"

    def test_zero_tone_medium(self):
        assert _map_tone_to_severity("0.0,0.0") == "medium"

    def test_negative_tone_medium(self):
        assert _map_tone_to_severity("-4.9,0.57") == "medium"

    def test_negative_five_tone_medium(self):
        assert _map_tone_to_severity("-5.0,0.0") == "medium"

    def test_negative_tone_high(self):
        assert _map_tone_to_severity("-8.7,0.57") == "high"

    def test_extreme_negative_tone_critical(self):
        assert _map_tone_to_severity("-18.2,0.0") == "critical"

    def test_empty_tone_default_medium(self):
        assert _map_tone_to_severity(None) == "medium"

    def test_blank_tone_default_medium(self):
        assert _map_tone_to_severity("") == "medium"

    def test_invalid_tone_default_medium(self):
        assert _map_tone_to_severity("not_a_number") == "medium"

    def test_comma_separated_tone_extracts_first_value(self):
        """Extract first value from comma-separated GKG tone format."""
        result = _map_tone_to_severity("-4.26,0.57,3.15,0.82,-1.25")
        assert result == "medium"  # -4.26 is in [-5.0, 5.0] range


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
    """GdeltAdapter.normalize() output fields (V2.3 corrected column mapping)."""

    @pytest.mark.asyncio
    async def test_normalize_output_fields(self, sample_gkg_record):
        adapter = GdeltAdapter()
        episode = await adapter.normalize(sample_gkg_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "gdelt_csv"
        assert episode.source_description == "GDELT GKG V2"
        assert episode.source_url == "http://example.com/article1"
        assert episode.severity == "high"  # first tone value = -8.7
        assert len(episode.entities) > 0

        # Verify entity types present (themes go to keywords, not entities)
        entity_types = {e.type for e in episode.entities}
        assert "person" in entity_types
        assert "organization" in entity_types
        assert "location" in entity_types

        # Verify location cleaning
        locs = [e.name for e in episode.entities if e.type == "location"]
        assert any("Beijing" in l for l in locs)
        assert any("Shanghai" in l for l in locs)

        # Verify content_hash is valid
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("gdelt_csv-")
        assert len(episode.name) > 20

        # Verify keywords are deduplicated
        assert len(episode.keywords) > 0
        assert len(episode.keywords) == len(set(episode.keywords))  # no duplicates

        # Verify episode_body has new format (themes-based summary)
        assert "## GDELT News Report" in episode.episode_body
        assert "**Domain**" in episode.episode_body
        assert "**Summary**" in episode.episode_body
        assert "**Key Topics**" in episode.episode_body
        assert "**Key Persons**" in episode.episode_body
        assert "**Key Organizations**" in episode.episode_body
        assert "**Key Locations**" in episode.episode_body
        assert "**Source**" in episode.episode_body

    @pytest.mark.asyncio
    async def test_normalize_empty_tone(self, sample_gkg_record):
        record = dict(sample_gkg_record)
        record["tone"] = ""
        adapter = GdeltAdapter()
        episode = await adapter.normalize(record)
        assert episode.severity == "medium"

    @pytest.mark.asyncio
    async def test_normalize_no_entities(self):
        record = {
            "global_event_id": "1",
            "valid_at": "20250609010101",
            "source_collection": "1",
            "domain": "",
            "source_url": "http://example.com",
            "language": "Eng",
            "themes": "",
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "0.0,0.0",
        }
        adapter = GdeltAdapter()
        episode = await adapter.normalize(record)
        assert len(episode.entities) == 0
        assert episode.severity == "medium"

    @pytest.mark.asyncio
    async def test_normalize_episode_body_has_themes(self, sample_gkg_record):
        adapter = GdeltAdapter()
        episode = await adapter.normalize(sample_gkg_record)
        assert "News coverage" in episode.episode_body
        assert "Key Topics" in episode.episode_body
        assert "ECON_FINANCIAL_MARKET" in episode.episode_body or "Financial Market" in episode.episode_body

    @pytest.mark.asyncio
    async def test_keywords_deduplicated(self):
        """Keywords should be deduplicated via set and capped at 20."""
        record = {
            "global_event_id": "1",
            "valid_at": "20250609010101",
            "source_collection": "1",
            "domain": "reuters.com",
            "source_url": "http://reuters.com/article",
            "language": "Eng",
            "themes": "ECON_FINANCIAL_MARKET; ECON_FINANCIAL_MARKET; TAX_FNCACT_REG_INVEST; ECON_FINANCIAL_MARKET",
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "0.0,0.0",
        }
        adapter = GdeltAdapter()
        episode = await adapter.normalize(record)
        # Should have 2 unique keywords (not 4)
        assert len(episode.keywords) == 2
        # No duplicates
        assert len(episode.keywords) == len(set(episode.keywords))

    @pytest.mark.asyncio
    async def test_keywords_max_20(self):
        """Keywords should be capped at 20."""
        many_themes = "; ".join([f"THEME_{i}" for i in range(50)])
        record = {
            "global_event_id": "1",
            "valid_at": "20250609010101",
            "source_collection": "1",
            "domain": "reuters.com",
            "source_url": "http://reuters.com/article",
            "language": "Eng",
            "themes": many_themes,
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "0.0,0.0",
        }
        adapter = GdeltAdapter()
        episode = await adapter.normalize(record)
        assert len(episode.keywords) <= 20


class TestGdeltPlanDFilter:
    """Filter_relevant() Plan D filter tests."""

    def _make_record(self, domain: str, themes: str = "TRADE_WARS") -> dict:
        return {
            "global_event_id": "1",
            "valid_at": "20250609010101",
            "source_collection": "1",
            "domain": domain,
            "source_url": f"https://{domain}/article",
            "language": "Eng",
            "themes": themes,
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "0.0,0.0",
            "cameo_code": None,
            "actor1_code": None,
            "actor1_name": None,
            "actor2_code": None,
            "actor2_name": None,
        }

    def test_authoritative_domain_unconditional_pass(self):
        """Authoritative domain passes without theme check."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        # Bloomberg is authoritative; SPORTS_NEWS doesn't match TRADE
        record = self._make_record("bloomberg.com", themes="SPORTS_NEWS")
        filtered = adapter.filter_relevant([record])
        assert len(filtered) == 1

    def test_non_authoritative_theme_pass(self):
        """Non-authoritative domain passes if theme matches."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        record = self._make_record("example-news.com", themes="TRADE_WARS")
        filtered = adapter.filter_relevant([record])
        assert len(filtered) == 1

    def test_non_authoritative_no_theme_rejected(self):
        """Non-authoritative domain without matching theme is rejected."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        record = self._make_record("example-news.com", themes="SPORTS_NEWS")
        filtered = adapter.filter_relevant([record])
        assert len(filtered) == 0

    def test_empty_domain_treated_as_non_authoritative(self):
        """Empty domain is treated as non-authoritative; needs theme match."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        record = self._make_record("", themes="TRADE_WARS")
        filtered = adapter.filter_relevant([record])
        assert len(filtered) == 1

    def test_empty_domain_no_theme_rejected(self):
        """Empty domain with no matching theme is rejected."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        record = self._make_record("", themes="SPORTS_NEWS")
        filtered = adapter.filter_relevant([record])
        assert len(filtered) == 0

    def test_case_insensitive_authoritative_match(self):
        """Mixed-case authoritative domain matches."""

        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        record = self._make_record("Bloomberg.com", themes="SPORTS_NEWS")
        filtered = adapter.filter_relevant([record])
        assert len(filtered) == 1

    def test_mixed_domains(self):
        """Multiple records with mixed authoritative/non-authoritative."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        records = [
            self._make_record("reuters.com", themes="SPORTS_NEWS"),       # authoritative -> pass
            self._make_record("example.com", themes="TRADE_WARS"),        # theme match -> pass
            self._make_record("example.com", themes="SPORTS_NEWS"),       # no match -> reject
            self._make_record("bloomberg.com", themes="ENTERTAINMENT"),   # authoritative -> pass
            self._make_record("wsj.com", themes="CRYPTO_NEWS"),          # authoritative -> pass
        ]
        filtered = adapter.filter_relevant(records)
        assert len(filtered) == 4  # 3 authoritative + 1 theme pass
        assert filtered[0]["domain"] == "reuters.com"
        assert filtered[1]["domain"] == "example.com"
        assert filtered[2]["domain"] == "bloomberg.com"
        assert filtered[3]["domain"] == "wsj.com"

    def test_no_macro_theme_keywords_returns_all(self):
        """When macro_theme_keywords is empty, all records pass."""
        adapter = GdeltAdapter(macro_theme_keywords=set())
        records = [
            self._make_record("reuters.com", themes="SPORTS_NEWS"),
            self._make_record("unknown.com", themes="UNKNOWN"),
        ]
        filtered = adapter.filter_relevant(records)
        assert len(filtered) == 2

    def test_authoritative_domain_matches_known_list(self):
        """Authoritative domains from the known list should be recognized."""
        adapter = GdeltAdapter(macro_theme_keywords={"TRADE"})
        # Test a few domains from AUTHORITATIVE_MEDIA_DOMAINS
        for domain in ["reuters.com", "ft.com", "wsj.com", "bloomberg.com",
                        "bbc.co.uk", "straitstimes.com"]:
            record = self._make_record(domain, themes="WEATHER")
            filtered = adapter.filter_relevant([record])
            assert len(filtered) == 1, f"Domain {domain} should pass unconditionally"
