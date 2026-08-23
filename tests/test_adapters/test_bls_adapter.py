"""Unit tests for BlsAdapter (Phase 1 macro adapter).

Covers: BaseAdapter contract, fetch normal path + network degradation,
normalize (awaited — no latent un-awaited bug), dedup, helpers.
"""

from __future__ import annotations

import httpx
import pytest

from src.adapters.base import BaseAdapter
from src.adapters.bls_adapter import (
    BlsAdapter,
    _BLS_SERIES,
    _build_bls_body,
    _map_bls_severity,
    _parse_bls_period,
)
from src.adapters.models import NormalizedEpisode


class _FakeJsonResponse:
    """Minimal fake httpx.Response with .json() and raise_for_status()."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeBlsClient:
    """Fake httpx.AsyncClient returning a canned BLS POST response."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeBlsClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None):
        return _FakeJsonResponse(self._payload)


class _FailingClient:
    """Fake httpx.AsyncClient that always raises a network error."""

    async def __aenter__(self) -> "_FailingClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, *args: object, **kwargs: object):
        raise httpx.ConnectError("connection refused")


class TestBlsContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = BlsAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert BlsAdapter.SOURCE_TYPE == "bls"

    def test_default_series_constant(self):
        assert "CES0000000001" in _BLS_SERIES
        assert "LNS14000000" in _BLS_SERIES
        assert "CUUR0000SA0" in _BLS_SERIES


class TestBlsFetch:
    """fetch() normal path and network degradation."""

    @pytest.mark.asyncio
    async def test_fetch_success_without_key(self, monkeypatch):
        """BLS responds REQUEST_SUCCEEDED → per-series snapshot records."""
        payload = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": "CES0000000001",
                        "data": [
                            {"year": "2026", "period": "M07", "periodName": "July", "value": "159000"},
                            {"year": "2026", "period": "M06", "periodName": "June", "value": "158000"},
                        ],
                    }
                ]
            },
        }
        fake = _FakeBlsClient(payload)
        monkeypatch.setattr(
            "src.adapters.bls_adapter.httpx.AsyncClient",
            lambda *a, **k: fake,
        )

        adapter = BlsAdapter()
        records = await adapter.fetch()
        assert len(records) == 1
        assert records[0]["series_id"] == "CES0000000001"
        assert records[0]["value"] == "159000"
        assert records[0]["previous_value"] == "158000"
        assert records[0]["periodName"] == "July"
        assert adapter._pre_filter_count == 1

    @pytest.mark.asyncio
    async def test_fetch_api_error_returns_empty(self, monkeypatch):
        """BLS returns REQUEST_FAILED → [] without raising."""
        payload = {"status": "REQUEST_FAILED", "message": "invalid seriesid"}
        fake = _FakeBlsClient(payload)
        monkeypatch.setattr(
            "src.adapters.bls_adapter.httpx.AsyncClient",
            lambda *a, **k: fake,
        )

        adapter = BlsAdapter()
        records = await adapter.fetch()
        assert records == []
        assert adapter._pre_filter_count == 0

    @pytest.mark.asyncio
    async def test_fetch_network_failure_returns_empty(self, monkeypatch):
        """Network unreachable → [] without raising."""
        monkeypatch.setattr(
            "src.adapters.bls_adapter.httpx.AsyncClient",
            lambda *a, **k: _FailingClient(),
        )

        adapter = BlsAdapter()
        records = await adapter.fetch()
        assert records == []
        assert adapter._pre_filter_count == 0


class TestBlsNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_nonfarm_payrolls(self, sample_bls_record):
        adapter = BlsAdapter()
        episode = await adapter.normalize(sample_bls_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "bls"
        assert episode.source_description == "BLS (US Bureau of Labor Statistics)"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["series_id"] == "CES0000000001"
        assert episode.metadata["value"] == "159000"
        assert episode.metadata["previous_value"] == "120000"
        assert episode.metadata["period_name"] == sample_bls_record["periodName"]

        # valid_at derived from year + period (first day of the month)
        assert episode.valid_at.year == int(sample_bls_record["year"])
        assert episode.valid_at.month == int(sample_bls_record["period"][1:])
        assert episode.valid_at.day == 1

        # Entities: country + theme
        entity_types = {e.type for e in episode.entities}
        assert "country" in entity_types
        assert "theme" in entity_types
        assert any(e.name == "美国" for e in episode.entities)

        # Episode body: latest value + change vs previous period
        assert "159000" in episode.episode_body
        assert "Thousands" in episode.episode_body
        assert "+39000.00" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format embeds series_id (group_id)
        assert episode.name.startswith("bls-")
        assert "CES0000000001" in episode.name

        # Source URL traceability
        assert episode.source_url == "https://data.bls.gov/timeseries/CES0000000001"


class TestBlsDedup:
    """Cross-cycle dedup of unchanged labor snapshots."""

    @pytest.mark.asyncio
    async def test_dedup_identical_snapshots(self, sample_bls_record):
        adapter = BlsAdapter()
        ep1 = await adapter.normalize(sample_bls_record)
        ep2 = await adapter.normalize(sample_bls_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


class TestBlsHelpers:
    """Module-level helper functions."""

    def test_parse_bls_period_monthly(self):
        dt = _parse_bls_period("2026", "M07")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 7
        assert dt.day == 1

    def test_parse_bls_period_annual(self):
        dt = _parse_bls_period("2026", "M13")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 12
        assert dt.day == 31

    def test_parse_bls_period_invalid(self):
        assert _parse_bls_period("bad", "M07") is None
        assert _parse_bls_period("2026", "") is None
        assert _parse_bls_period("2026", "M00") is None

    def test_map_bls_severity_unemployment_jump_high(self):
        assert _map_bls_severity("LNS14000000", 5.5, 4.2) == "high"

    def test_map_bls_severity_payroll_loss_high(self):
        # Small loss (-200) → medium; >= 500k loss → high
        assert _map_bls_severity("CES0000000001", 158000.0, 158200.0) == "medium"
        assert _map_bls_severity("CES0000000001", 158000.0, 158600.0) == "high"

    def test_map_bls_severity_default_medium(self):
        assert _map_bls_severity("CUUR0000SA0", 320.0, 318.0) == "medium"

    def test_build_bls_body_contains_change(self):
        body = _build_bls_body(
            "CES0000000001",
            "All Employees, Total Nonfarm (Nonfarm Payrolls)",
            "2026",
            "M07",
            "July",
            "159000",
            "120000",
            "Thousands",
        )
        assert "July 2026" in body
        assert "159000" in body
        assert "+39000.00" in body
