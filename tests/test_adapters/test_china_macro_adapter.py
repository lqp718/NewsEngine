"""Tests for ChinaMacroAdapter — date parsing, severity mapping, normalize."""

import pytest
from datetime import datetime, timezone

from src.adapters.china_macro_adapter import (
    ChinaMacroAdapter,
    _map_china_macro_severity,
    _parse_china_date,
    _build_narrative,
)


class TestParseChinaDate:
    """Test Chinese date format parsing."""

    def test_monthly_format(self):
        """'2024年03月份' → datetime(2024, 3, 1)."""
        result = _parse_china_date("2024年03月份")
        assert result == datetime(2024, 3, 1, tzinfo=timezone.utc)

    def test_quarterly_format(self):
        """'2024年第1季度' → datetime(2024, 3, 1)."""
        result = _parse_china_date("2024年第1季度")
        assert result == datetime(2024, 3, 1, tzinfo=timezone.utc)

    def test_quarterly_format_q2(self):
        """'2024年第2季度' → datetime(2024, 6, 1)."""
        result = _parse_china_date("2024年第2季度")
        assert result == datetime(2024, 6, 1, tzinfo=timezone.utc)

    def test_iso_format(self):
        """'2024-03-09' → datetime(2024, 3, 9)."""
        result = _parse_china_date("2024-03-09")
        assert result == datetime(2024, 3, 9, tzinfo=timezone.utc)

    def test_empty_string(self):
        assert _parse_china_date("") is None

    def test_none(self):
        assert _parse_china_date(None) is None

    def test_invalid(self):
        assert _parse_china_date("garbage") is None


class TestMapSeverity:
    """Test severity mapping for Chinese macro indicators."""

    # CPI tests
    def test_cpi_high_inflation(self):
        assert _map_china_macro_severity("cpi", 3.5) == "high"

    def test_cpi_deflation(self):
        assert _map_china_macro_severity("cpi", -0.5) == "high"

    def test_cpi_moderate(self):
        assert _map_china_macro_severity("cpi", 1.5) == "low"

    def test_cpi_elevated(self):
        assert _map_china_macro_severity("cpi", 2.5) == "medium"

    # PPI tests
    def test_ppi_large_swing(self):
        assert _map_china_macro_severity("ppi", 6.0) == "high"

    def test_ppi_moderate(self):
        assert _map_china_macro_severity("ppi", 3.0) == "medium"

    def test_ppi_small(self):
        assert _map_china_macro_severity("ppi", 1.0) == "low"

    # PMI tests
    def test_pmi_strong_expansion(self):
        assert _map_china_macro_severity("pmi", 53.0) == "high"

    def test_pmi_contraction(self):
        assert _map_china_macro_severity("pmi", 47.0) == "high"

    def test_pmi_mild_expansion(self):
        assert _map_china_macro_severity("pmi", 51.5) == "medium"

    def test_pmi_neutral(self):
        assert _map_china_macro_severity("pmi", 50.5) == "low"

    # Caixin PMI tests
    def test_caixin_pmi_same_as_pmi(self):
        assert _map_china_macro_severity("caixin_pmi", 53.0) == "high"
        assert _map_china_macro_severity("caixin_pmi", 47.0) == "high"

    # Unknown indicator
    def test_unknown_indicator(self):
        assert _map_china_macro_severity("unknown", 99.0) == "medium"


class TestChinaMacroAdapterNormalize:
    """Test ChinaMacroAdapter.normalize()."""

    @pytest.mark.asyncio
    async def test_normalize_cpi(self):
        adapter = ChinaMacroAdapter()
        record = {
            "fetch_time": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "indicator": "cpi",
            "value": 0.7,
            "date_str": "2026年03月份",
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.source_type == "china_macro"
        assert "CPI" in episode.source_description
        assert any(e.name == "中国" for e in episode.entities)

    @pytest.mark.asyncio
    async def test_normalize_missing_time(self):
        adapter = ChinaMacroAdapter()
        record = {"indicator": "cpi", "value": 0.7, "date_str": "2026年03月份"}
        episode = await adapter.normalize(record)
        assert episode is None

    @pytest.mark.asyncio
    async def test_normalize_missing_value(self):
        adapter = ChinaMacroAdapter()
        record = {
            "fetch_time": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "indicator": "cpi",
            "value": None,
            "date_str": "2026年03月份",
        }
        episode = await adapter.normalize(record)
        assert episode is None

    @pytest.mark.asyncio
    async def test_normalize_pmi(self):
        adapter = ChinaMacroAdapter()
        record = {
            "fetch_time": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "indicator": "pmi",
            "value": 50.3,
            "date_str": "2026年04月份",
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.severity == "low"  # 50.3 is neutral
        assert "中国PMI（官方制造业采购经理指数）" in episode.episode_body
        assert "50.30" in episode.episode_body
        assert "50以上为扩张区间" in episode.episode_body

    @pytest.mark.asyncio
    async def test_normalize_pmi_contraction(self):
        adapter = ChinaMacroAdapter()
        record = {
            "fetch_time": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "indicator": "pmi",
            "value": 48.5,
            "date_str": "2026年04月份",
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert "48.50" in episode.episode_body
        assert "以下为收缩区间" in episode.episode_body

    @pytest.mark.asyncio
    async def test_normalize_gdp(self):
        adapter = ChinaMacroAdapter()
        record = {
            "fetch_time": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "indicator": "gdp",
            "value": 5.3,
            "date_str": "2026年第1季度",
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert "GDP" in episode.source_description
        assert episode.metadata.get("indicator") == "gdp"
        # GDP value is YoY growth → rendered as growth rate, not 亿元
        assert "同比增长5.30%" in episode.episode_body

    @pytest.mark.asyncio
    async def test_normalize_fallback_without_codebook(self, monkeypatch):
        """Codebook load failure → Markdown body fallback."""
        monkeypatch.setattr(
            "src.adapters.china_macro_adapter._load_codebook", lambda: {}
        )
        adapter = ChinaMacroAdapter()
        record = {
            "fetch_time": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "indicator": "cpi",
            "value": 0.7,
            "date_str": "2026年03月份",
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert "## China CPI — 2026年03月份" in episode.episode_body
        assert "- Value: 0.70 %" in episode.episode_body


class TestBuildNarrative:
    """Test codebook-backed narrative generation."""

    def test_cpi_narrative(self):
        body = _build_narrative("cpi", 0.7, "2026年03月份")
        assert body == (
            "中国CPI（居民消费价格指数），2026年03月份：0.70%。"
            "衡量通胀水平的核心指标。"
        )

    def test_pmi_narrative_no_unit(self):
        body = _build_narrative("pmi", 50.3, "2026年04月份")
        assert "中国PMI（官方制造业采购经理指数），2026年04月份：50.30。" in body

    def test_gdp_narrative_growth_rate(self):
        body = _build_narrative("gdp", 5.3, "2026年第1季度")
        assert "同比增长5.30%" in body

    def test_unknown_indicator_returns_none(self):
        assert _build_narrative("unknown", 1.0, "2026年01月份") is None

    def test_missing_codebook_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "src.adapters.china_macro_adapter._load_codebook", lambda: {}
        )
        assert _build_narrative("cpi", 0.7, "2026年03月份") is None
