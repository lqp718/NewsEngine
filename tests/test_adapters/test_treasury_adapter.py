"""Tests for TreasuryAdapter — yield curve parsing and inversion detection."""

import pytest
from datetime import datetime, timezone

from src.adapters.treasury_adapter import (
    TreasuryAdapter,
    _detect_inversion,
    _build_yield_curve_body,
    _build_narrative,
    _parse_csv,
    _parse_rate,
)


class TestDetectInversion:
    """Test yield curve inversion detection."""

    def test_normal_curve(self):
        """2yr < 10yr → low severity (normal)."""
        rates = {"2yr": 4.0, "10yr": 4.5}
        assert _detect_inversion(rates) == "low"

    def test_inverted_curve(self):
        """2yr > 10yr → high severity (recession signal)."""
        rates = {"2yr": 4.5, "10yr": 4.2}
        assert _detect_inversion(rates) == "high"

    def test_flat_curve(self):
        """2yr slightly below 10yr (spread < 0.25) → medium severity."""
        rates = {"2yr": 4.3, "10yr": 4.5}
        assert _detect_inversion(rates) == "medium"

    def test_missing_2yr(self):
        """Missing 2yr → low (default)."""
        rates = {"10yr": 4.5}
        assert _detect_inversion(rates) == "low"

    def test_missing_10yr(self):
        """Missing 10yr → low (default)."""
        rates = {"2yr": 4.5}
        assert _detect_inversion(rates) == "low"

    def test_empty_rates(self):
        """Empty dict → low."""
        assert _detect_inversion({}) == "low"

    def test_exact_parity(self):
        """2yr == 10yr → high (spread = 0, which is > 0 is False, so medium? No: spread=0, not > 0, not > -0.25 → medium)."""
        rates = {"2yr": 4.5, "10yr": 4.5}
        # spread = 0, not > 0, but > -0.25 → medium
        assert _detect_inversion(rates) == "medium"


class TestBuildYieldCurveBody:
    """Test yield curve body text generation."""

    def test_basic_body(self):
        rates = {"2yr": 4.19, "10yr": 4.72}
        body = _build_yield_curve_body(rates, "2026-08-17")
        assert "2026-08-17" in body
        assert "2yr" in body.lower() or "2Yr" in body
        assert "4.19" in body
        assert "4.72" in body
        assert "normal" in body

    def test_inverted_body(self):
        rates = {"2yr": 4.72, "10yr": 4.19}
        body = _build_yield_curve_body(rates, "2026-08-17")
        assert "inverted" in body

    def test_flat_body(self):
        # 2yr slightly above 10yr → flat (spread between 0 and -25 bp)
        rates = {"2yr": 4.72, "10yr": 4.60}
        body = _build_yield_curve_body(rates, "2026-08-17")
        assert "flat" in body


class TestBuildNarrative:
    """Test codebook-backed natural-language narrative generation."""

    def test_basic_narrative(self):
        """Normal curve → Chinese narrative with codebook names + spread."""
        rates = {"1mo": 3.79, "3mo": 3.87, "2yr": 4.19, "10yr": 4.72}
        body = _build_narrative(rates, "2026-08-17")
        assert "美国国债收益率曲线（2026-08-17）" in body
        assert "1个月期3.79%" in body
        assert "3个月期3.87%" in body
        assert "2年期4.19%" in body
        assert "10年期4.72%" in body
        assert "2s10s利差+53bp" in body
        assert "收益率曲线形态正常，无倒挂风险信号" in body

    def test_inverted_narrative(self):
        """Inverted curve → negative spread + inversion signal sentence."""
        rates = {"2yr": 4.72, "10yr": 4.19}
        body = _build_narrative(rates, "2026-08-17")
        assert "2s10s利差-53bp" in body
        assert "倒挂" in body

    def test_flat_narrative(self):
        """Flat curve (spread within -25bp) → flattening sentence."""
        rates = {"2yr": 4.72, "10yr": 4.60}
        body = _build_narrative(rates, "2026-08-17")
        assert "2s10s利差-12bp" in body
        assert "趋平" in body

    def test_codebook_order_matches_definitions(self, monkeypatch):
        """Terms render in codebook definition order, skipping unknown terms."""
        rates = {"1.5mo": 3.80, "3mo": 3.87, "10yr": 4.72}  # 1.5mo not in codebook
        body = _build_narrative(rates, "2026-08-17")
        assert "3个月期3.87%" in body
        assert "10年期4.72%" in body
        assert "1.5mo" not in body  # undefined term skipped

    @pytest.mark.asyncio
    async def test_normalize_uses_narrative(self):
        """normalize() produces the natural-language narrative by default."""
        adapter = TreasuryAdapter()
        record = {
            "fetch_time": datetime(2026, 8, 17, tzinfo=timezone.utc),
            "term_rates": {"2yr": 4.19, "10yr": 4.72},
        }
        episode = await adapter.normalize(record)
        assert "美国国债收益率曲线（2026-08-17）" in episode.episode_body
        assert "2年期4.19%" in episode.episode_body

    @pytest.mark.asyncio
    async def test_normalize_fallback_without_codebook(self, monkeypatch):
        """Codebook load failure → Markdown body fallback."""
        monkeypatch.setattr(
            "src.adapters.treasury_adapter._load_codebook", lambda: {}
        )
        adapter = TreasuryAdapter()
        record = {
            "fetch_time": datetime(2026, 8, 17, tzinfo=timezone.utc),
            "term_rates": {"2yr": 4.19, "10yr": 4.72},
        }
        episode = await adapter.normalize(record)
        assert "## US Treasury Yield Curve — 2026-08-17" in episode.episode_body
        assert "- 2yr: 4.19%" in episode.episode_body


class TestParseRate:
    """Test rate value parsing."""

    def test_valid_rate(self):
        assert _parse_rate("4.19") == 4.19

    def test_na_value(self):
        assert _parse_rate("N/A") is None

    def test_empty_string(self):
        assert _parse_rate("") is None

    def test_none(self):
        assert _parse_rate(None) is None

    def test_whitespace(self):
        assert _parse_rate("  ") is None

    def test_invalid(self):
        assert _parse_rate("abc") is None


class TestParseCsv:
    """Test CSV parsing."""

    SAMPLE_CSV = '''Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
08/17/2026,3.79,3.80,3.82,3.87,3.89,3.95,4.00,4.19,4.25,4.38,4.54,4.72,5.30,5.31
08/14/2026,3.79,3.80,3.81,3.86,3.88,3.95,3.98,4.17,4.24,4.36,4.51,4.68,5.25,5.25
'''

    def test_parse_records(self):
        records = _parse_csv(self.SAMPLE_CSV)
        assert len(records) == 2

    def test_most_recent_first(self):
        records = _parse_csv(self.SAMPLE_CSV)
        assert records[0]["fetch_time"] > records[1]["fetch_time"]

    def test_term_rates_extracted(self):
        records = _parse_csv(self.SAMPLE_CSV)
        rates = records[0]["term_rates"]
        assert rates["2yr"] == 4.19
        assert rates["10yr"] == 4.72
        assert rates["3mo"] == 3.87

    def test_date_parsed(self):
        records = _parse_csv(self.SAMPLE_CSV)
        assert records[0]["fetch_time"] == datetime(2026, 8, 17, tzinfo=timezone.utc)

    def test_empty_csv(self):
        records = _parse_csv("")
        assert records == []

    def test_header_only(self):
        csv_text = 'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
        records = _parse_csv(csv_text)
        assert records == []

    def test_na_values_skipped(self):
        csv_text = '''Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
01/02/1990,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,7.87,7.98,7.94,N/A,8.00
'''
        records = _parse_csv(csv_text)
        assert len(records) == 1
        rates = records[0]["term_rates"]
        # Only 10yr, 20yr, 30yr should be present (plus 7yr)
        assert "1mo" not in rates
        assert "10yr" in rates
        assert rates["10yr"] == 7.98


class TestTreasuryAdapterNormalize:
    """Test TreasuryAdapter.normalize()."""

    @pytest.mark.asyncio
    async def test_normalize_basic(self):
        adapter = TreasuryAdapter()
        record = {
            "fetch_time": datetime(2026, 8, 17, tzinfo=timezone.utc),
            "term_rates": {"2yr": 4.19, "10yr": 4.72},
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.source_type == "treasury"
        assert episode.severity == "low"  # normal curve
        assert "treasury" in episode.keywords
        assert any(e.name == "United States" for e in episode.entities)

    @pytest.mark.asyncio
    async def test_normalize_inverted(self):
        adapter = TreasuryAdapter()
        record = {
            "fetch_time": datetime(2026, 8, 17, tzinfo=timezone.utc),
            "term_rates": {"2yr": 4.72, "10yr": 4.19},
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.severity == "high"

    @pytest.mark.asyncio
    async def test_normalize_missing_time(self):
        adapter = TreasuryAdapter()
        record = {"term_rates": {"2yr": 4.19}}
        episode = await adapter.normalize(record)
        assert episode is None

    @pytest.mark.asyncio
    async def test_normalize_empty_rates(self):
        adapter = TreasuryAdapter()
        record = {"fetch_time": datetime(2026, 8, 17, tzinfo=timezone.utc), "term_rates": {}}
        episode = await adapter.normalize(record)
        assert episode is None
