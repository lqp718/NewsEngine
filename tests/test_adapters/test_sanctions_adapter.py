"""Unit tests for SanctionsAdapter (Phase 1 macro adapter).

Covers: BaseAdapter contract, fetch degradation (network failure → []),
normalize (awaited — no latent un-awaited bug), dedup, helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from src.adapters.base import BaseAdapter
from src.adapters.sanctions_adapter import (
    SanctionsAdapter,
    _build_sanctions_body,
    _map_ofac_type,
    _map_sanctions_severity,
)
from src.adapters.models import NormalizedEpisode
from src.core.config import reload_settings


class _FakeJsonResponse:
    """Minimal fake httpx.Response with .json() and raise_for_status()."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeTextResponse:
    """Minimal fake httpx.Response with .text and raise_for_status()."""

    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        return None

    @property
    def text(self) -> str:
        return self._text


class _FakeSanctionsClient:
    """Fake httpx.AsyncClient routing OpenSanctions JSON vs OFAC CSV."""

    def __init__(self, json_payload: dict, csv_text: str) -> None:
        self._json = json_payload
        self._csv = csv_text

    async def __aenter__(self) -> "_FakeSanctionsClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        if "opensanctions.org" in url:
            return _FakeJsonResponse(self._json)
        return _FakeTextResponse(self._csv)


class _FailingClient:
    """Fake httpx.AsyncClient that always raises a network error."""

    async def __aenter__(self) -> "_FailingClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, *args: object, **kwargs: object):
        raise httpx.ConnectError("connection refused")


class TestSanctionsContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = SanctionsAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert SanctionsAdapter.SOURCE_TYPE == "sanctions"


class TestSanctionsFetch:
    """fetch() normal path and network degradation."""

    @pytest.mark.asyncio
    async def test_fetch_opensanctions_success(self, monkeypatch):
        """OpenSanctions returns results → records with source tag."""
        # Set API key so adapter attempts OpenSanctions (not just OFAC fallback)
        monkeypatch.setenv("OPEN_SANCTIONS_API_KEY", "test-key")
        reload_settings()  # Force reload to pick up the new env var
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        payload = {
            "results": [
                {
                    "id": "Q12345",
                    "caption": "Example Corp",
                    "schema": "Company",
                    "properties": {
                        "countries": ["Russia"],
                        "programs": ["SDN"],
                        "first_seen": [recent],
                    },
                }
            ]
        }
        fake = _FakeSanctionsClient(payload, "")
        monkeypatch.setattr(
            "src.adapters.sanctions_adapter.httpx.AsyncClient",
            lambda *a, **k: fake,
        )

        adapter = SanctionsAdapter()
        records = await adapter.fetch()
        assert len(records) == 1
        assert records[0]["entity_name"] == "Example Corp"
        assert records[0]["source"] == "opensanctions"
        assert records[0]["target_type"] == "legalEntity"
        assert adapter._pre_filter_count == 1

    @pytest.mark.asyncio
    async def test_fetch_ofac_sdn_fallback(self, monkeypatch):
        """OpenSanctions empty → OFAC SDN CSV fallback."""
        # Set API key so adapter attempts OpenSanctions (and falls back when empty)
        monkeypatch.setenv("OPEN_SANCTIONS_API_KEY", "test-key")
        reload_settings()  # Force reload to pick up the new env var
        # Minimal OFAC CSV rows: ent_num,name,type,program
        csv_text = (
            "1,Example Corp,entity,SDGT\n"
            "2,John Doe,individual,-0-\n"
        )
        fake = _FakeSanctionsClient({"results": []}, csv_text)
        monkeypatch.setattr(
            "src.adapters.sanctions_adapter.httpx.AsyncClient",
            lambda *a, **k: fake,
        )

        adapter = SanctionsAdapter()
        records = await adapter.fetch()
        assert len(records) == 2
        by_name = {r["entity_name"]: r for r in records}
        assert by_name["Example Corp"]["source"] == "ofac"
        assert by_name["Example Corp"]["target_type"] == "legalEntity"
        assert by_name["Example Corp"]["sanction_program"] == "SDGT"
        assert by_name["John Doe"]["target_type"] == "person"
        assert by_name["John Doe"]["sanction_program"] == ""

    @pytest.mark.asyncio
    async def test_fetch_network_failure_returns_empty(self, monkeypatch):
        """Both sources unreachable → [] without raising."""
        monkeypatch.setattr(
            "src.adapters.sanctions_adapter.httpx.AsyncClient",
            lambda *a, **k: _FailingClient(),
        )

        adapter = SanctionsAdapter()
        records = await adapter.fetch()
        assert records == []
        assert adapter._pre_filter_count == 0


class TestSanctionsNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_legal_entity(self, sample_sanctions_record):
        adapter = SanctionsAdapter()
        episode = await adapter.normalize(sample_sanctions_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "sanctions"
        assert episode.source_description == "OFAC SDN / OpenSanctions Sanctions List"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["target_type"] == "legalEntity"
        assert episode.metadata["sanction_program"] == "SDN"
        assert episode.metadata["source"] == "ofac"

        # Severity: sanctions are strong signals by default
        assert episode.severity == "high"

        # Entities: organization for legal entity + country
        assert episode.entities[0].type == "organization"
        assert episode.entities[0].name == "Example Corp"
        assert any(e.type == "country" and e.name == "Russia" for e in episode.entities)

        # Episode body
        assert "Example Corp" in episode.episode_body
        assert "SDN" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("sanctions-")

    @pytest.mark.asyncio
    async def test_normalize_person_entity(self):
        adapter = SanctionsAdapter()
        record = {
            "entity_name": "Ivan Petrov",
            "target_type": "person",
            "country": "Russia",
            "sanction_program": "SDGT",
            "listing_date": None,
            "source_url": "https://sanctionssearch.ofac.treas.gov/Details.aspx?id=99",
            "source": "ofac",
        }
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.entities[0].type == "person"
        assert episode.entities[0].name == "Ivan Petrov"
        assert episode.severity == "high"

    @pytest.mark.asyncio
    async def test_normalize_empty_entity_name_returns_none(self):
        adapter = SanctionsAdapter()
        episode = await adapter.normalize(
            {"entity_name": "  ", "target_type": "legalEntity"}
        )
        assert episode is None

    @pytest.mark.asyncio
    async def test_normalize_date_cutoff_returns_none(self, monkeypatch):
        """Entry with listing date outside window → None."""
        fake_settings = SimpleNamespace(news_max_age_days=14)
        monkeypatch.setattr(
            "src.adapters.sanctions_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = SanctionsAdapter()
        record = {
            "entity_name": "Old Corp",
            "target_type": "legalEntity",
            "country": "Russia",
            "sanction_program": "SDN",
            "listing_date": "2015-03-01",
            "source_url": "https://www.opensanctions.org/entities/Q1/",
            "source": "opensanctions",
        }
        episode = await adapter.normalize(record)
        assert episode is None


class TestSanctionsDedup:
    """Cross-cycle dedup of identical sanction entries."""

    @pytest.mark.asyncio
    async def test_dedup_identical_entries(self, sample_sanctions_record):
        adapter = SanctionsAdapter()
        ep1 = await adapter.normalize(sample_sanctions_record)
        ep2 = await adapter.normalize(sample_sanctions_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


class TestSanctionsHelpers:
    """Module-level helper functions."""

    def test_map_ofac_type_individual(self):
        assert _map_ofac_type("individual") == "person"

    def test_map_ofac_type_entity(self):
        assert _map_ofac_type("entity") == "legalEntity"

    def test_map_ofac_type_missing(self):
        assert _map_ofac_type("-0-") == "legalEntity"

    def test_map_sanctions_severity(self):
        assert _map_sanctions_severity("person") == "high"
        assert _map_sanctions_severity("legalEntity") == "high"

    def test_build_sanctions_body(self):
        body = _build_sanctions_body(
            "Example Corp", "legalEntity", "Russia", "SDN", "2026-08-10", "ofac"
        )
        assert "Example Corp" in body
        assert "Russia" in body
        assert "SDN" in body
        assert "2026-08-10" in body
        assert "ofac" in body
