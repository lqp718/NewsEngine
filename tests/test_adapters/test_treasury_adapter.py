"""Unit tests for TreasuryAdapter (Phase 2+ skeleton).

Tests the skeleton class, normalize logic with synthetic data,
and yield curve inversion detection.
"""

from __future__ import annotations

import pytest

from src.adapters.treasury_adapter import (
    TreasuryAdapter,
    _detect_inversion,
    _build_yield_curve_body,
)
from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode


class TestTreasurySkeleton:
    """Verify Phase 1 skeleton contract."""

    def test_class_exists_and_inherits_base(self):
        adapter = TreasuryAdapter()
        assert isinstance(adapter, BaseAdapter)

    async def test_fetch_returns_empty(self):
        adapter = TreasuryAdapter()
        result = await adapter.fetch()
        assert result == []

    def test_normalize_mock_record(self, sample_treasury_record):
        adapter = TreasuryAdapter()
        episode = adapter.normalize(sample_treasury_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "treasury"
        assert episode.source_description == "US Treasury Yield Curve"
        assert episode.severity == "high"  # 2yr=4.35 > 10yr=4.28 → inverted

        # Verify entity
        assert len(episode.entities) == 1
        assert episode.entities[0].type == "country"
        assert episode.entities[0].name == "United States"

        # Structured metadata
        assert episode.metadata.get("_structured") is True

        # Content hash
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Episode body has yield curve data
        assert "US Treasury Yield Curve" in episode.episode_body
        assert "4.35" in episode.episode_body  # 2yr rate
        assert "4.28" in episode.episode_body  # 10yr rate


class TestInversionDetection:
    """Yield curve inversion detection."""

    def test_inverted_2yr_greater_10yr(self):
        """2yr > 10yr → inverted → high severity."""
        severity = _detect_inversion({"2yr": 4.5, "10yr": 4.3})
        assert severity == "high"

    def test_normal_curve(self):
        """Normal upward-sloping curve → low severity."""
        severity = _detect_inversion({"2yr": 4.0, "10yr": 4.5, "30yr": 4.8})
        assert severity == "low"

    def test_flattening_curve(self):
        """Spread < 0.25 → medium severity."""
        severity = _detect_inversion({"2yr": 4.3, "10yr": 4.4})
        assert severity == "medium"

    def test_no_2yr_10yr_data(self):
        """Missing key terms → default low."""
        severity = _detect_inversion({"3mo": 4.5})
        assert severity == "low"


class TestYieldCurveBody:
    """Episode body formatting."""

    def test_body_contains_term_rates(self):
        body = _build_yield_curve_body(
            {"3mo": 4.52, "2yr": 4.35, "10yr": 4.28, "30yr": 4.55},
            "2025-06-09",
        )
        assert "2025-06-09" in body
        assert "4.35" in body
        assert "4.28" in body
        assert "Spread" in body
        # 4.28 < 4.35 → inverted technically, but spread = -7bp
        # -7 > -25 → shape = "flat" in the builder logic
        assert "flat" in body or "inverted" in body

    def test_body_normal_curve(self):
        body = _build_yield_curve_body(
            {"2yr": 4.0, "10yr": 4.5},
            "2025-06-09",
        )
        assert "normal" in body


@pytest.mark.skip(reason="Phase 2+")
class TestRealTreasuryFetch:
    """Real Treasury API fetch — Phase 2+ only."""

    async def test_real_fetch(self):
        adapter = TreasuryAdapter()
        result = await adapter.fetch()
        assert len(result) > 0
