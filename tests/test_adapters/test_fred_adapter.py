"""Unit tests for FredAdapter (Phase 1 macro adapter).

Covers: BaseAdapter contract, fetch degradation without API key,
normalize (awaited — no latent un-awaited bug), dedup, helpers,
and the robustness layer (retry on 429/5xx/transport errors with
Retry-After honor, per-series isolation, server-side
``observation_start`` filtering).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from src.adapters.base import BaseAdapter
from src.adapters.fred_adapter import (
    FredAdapter,
    _FRED_FETCH_LOOKBACK_DAYS,
    _FRED_SERIES,
    _build_fred_body,
    _extract_error_message,
    _map_fred_severity,
    _parse_retry_after,
)
from src.adapters.models import NormalizedEpisode


class TestFredContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = FredAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert FredAdapter.SOURCE_TYPE == "fred"

    def test_default_series_constant(self):
        assert "GDP" in _FRED_SERIES
        assert "CPIAUCSL" in _FRED_SERIES
        assert "UNRATE" in _FRED_SERIES
        assert "DFF" in _FRED_SERIES
        assert "PPIACO" in _FRED_SERIES


class TestFredFetchDegrade:
    """fetch() degrades gracefully when fred_api_key is unconfigured."""

    @pytest.mark.asyncio
    async def test_fetch_empty_without_key(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = FredAdapter()
        result = await adapter.fetch()
        assert result == []
        assert adapter._pre_filter_count == 0


class _FakeResponse:
    """Minimal httpx.Response stand-in for helper/retry unit tests."""

    def __init__(self, status_code=200, json_data=None, retry_after=None):
        self.status_code = status_code
        self._json_data = json_data
        self.reason_phrase = "reason"
        self.headers = {}
        if retry_after is not None:
            self.headers["retry-after"] = retry_after

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


class _FakeAsyncClient:
    """Duck-typed httpx.AsyncClient used as an async context manager."""

    def __init__(self, get_fn):
        self._get = get_fn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        return await self._get(url, params=params, headers=headers)


def _fred_payload(observations):
    """A FRED series/observations JSON payload with the given rows."""
    return {
        "realtime_start": "2026-08-14",
        "realtime_end": "2026-08-14",
        "observations": observations,
    }


def _obs(date, value):
    return {"realtime_start": date, "realtime_end": "2026-08-14", "date": date, "value": value}


class TestFredErrorHelpers:
    """FRED error-body and Retry-After parsing (errors.html)."""

    def test_parse_retry_after_seconds(self):
        resp = _FakeResponse(status_code=429, retry_after="2")
        assert _parse_retry_after(resp) == 2.0

    def test_parse_retry_after_absent(self):
        resp = _FakeResponse(status_code=429)
        assert _parse_retry_after(resp) is None

    def test_parse_retry_after_invalid(self):
        resp = _FakeResponse(status_code=429, retry_after="not-a-date")
        assert _parse_retry_after(resp) is None

    def test_extract_error_message_json(self):
        resp = _FakeResponse(
            status_code=400,
            json_data={"error_code": 400, "error_message": "Bad Request."},
        )
        assert _extract_error_message(resp) == "Bad Request."

    def test_extract_error_message_missing(self):
        resp = _FakeResponse(status_code=500, json_data={"unexpected": 1})
        assert _extract_error_message(resp) == ""

    def test_extract_error_message_non_json(self):
        resp = _FakeResponse(status_code=500)
        assert _extract_error_message(resp) == ""


class TestFredFetchRetry:
    """fetch() retries transient failures and honors Retry-After."""

    @pytest.mark.asyncio
    async def test_fetch_retries_429_then_succeeds(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="key", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )
        monkeypatch.setattr(
            "src.adapters.fred_adapter._FRED_SERIES", ["UNRATE"]
        )
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(
            "src.adapters.fred_adapter.asyncio.sleep", fake_sleep
        )

        calls = {"n": 0}

        async def fake_get(url, params=None, headers=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(
                    status_code=429,
                    json_data={"error_code": 429, "error_message": "rate limited"},
                    retry_after="2",
                )
            return _FakeResponse(
                status_code=200,
                json_data=_fred_payload(
                    [_obs("2026-08-13", "5.0"), _obs("2026-08-12", "4.5")]
                ),
            )

        monkeypatch.setattr(
            "src.adapters.fred_adapter.httpx.AsyncClient",
            lambda timeout: _FakeAsyncClient(fake_get),
        )

        adapter = FredAdapter()
        records = await adapter.fetch()
        assert len(records) == 1
        assert records[0]["series_id"] == "UNRATE"
        assert records[0]["value"] == "5.0"
        assert records[0]["previous_value"] == "4.5"
        # First attempt (429) + one retry (200).
        assert calls["n"] == 2
        # Retry-After (2s) honored, not the 1s backoff base.
        assert sleeps == [2.0]

    @pytest.mark.asyncio
    async def test_fetch_exhausts_retries_on_429(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="key", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )
        monkeypatch.setattr(
            "src.adapters.fred_adapter._FRED_SERIES", ["UNRATE"]
        )
        monkeypatch.setattr(
            "src.adapters.fred_adapter._FRED_MAX_RETRIES", 2
        )

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(
            "src.adapters.fred_adapter.asyncio.sleep", fake_sleep
        )

        calls = {"n": 0}

        async def fake_get(url, params=None, headers=None):
            calls["n"] += 1
            return _FakeResponse(
                status_code=429,
                json_data={"error_code": 429, "error_message": "rate limited"},
            )

        monkeypatch.setattr(
            "src.adapters.fred_adapter.httpx.AsyncClient",
            lambda timeout: _FakeAsyncClient(fake_get),
        )

        adapter = FredAdapter()
        records = await adapter.fetch()
        assert records == []
        assert adapter._pre_filter_count == 0
        # Initial attempt + 2 retries, then give up.
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_fetch_retries_5xx_then_succeeds(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="key", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )
        monkeypatch.setattr(
            "src.adapters.fred_adapter._FRED_SERIES", ["GDP"]
        )
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(
            "src.adapters.fred_adapter.asyncio.sleep", fake_sleep
        )

        calls = {"n": 0}

        async def fake_get(url, params=None, headers=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(status_code=503, json_data=None)
            return _FakeResponse(
                status_code=200,
                json_data=_fred_payload(
                    [_obs("2026-08-01", "29200.0"), _obs("2026-07-01", "29000.0")]
                ),
            )

        monkeypatch.setattr(
            "src.adapters.fred_adapter.httpx.AsyncClient",
            lambda timeout: _FakeAsyncClient(fake_get),
        )

        adapter = FredAdapter()
        records = await adapter.fetch()
        assert len(records) == 1
        assert records[0]["series_id"] == "GDP"
        assert calls["n"] == 2
        # No Retry-After on the 503 → exponential backoff base 1.0s.
        assert sleeps == [1.0]

    @pytest.mark.asyncio
    async def test_fetch_retries_transport_error_then_succeeds(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="key", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )
        monkeypatch.setattr(
            "src.adapters.fred_adapter._FRED_SERIES", ["UNRATE"]
        )

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(
            "src.adapters.fred_adapter.asyncio.sleep", fake_sleep
        )

        calls = {"n": 0}

        async def fake_get(url, params=None, headers=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("connection refused")
            return _FakeResponse(
                status_code=200,
                json_data=_fred_payload([_obs("2026-08-13", "5.0")]),
            )

        monkeypatch.setattr(
            "src.adapters.fred_adapter.httpx.AsyncClient",
            lambda timeout: _FakeAsyncClient(fake_get),
        )

        adapter = FredAdapter()
        records = await adapter.fetch()
        assert len(records) == 1
        assert records[0]["value"] == "5.0"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_fetch_isolates_per_series_failure(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="key", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(
            "src.adapters.fred_adapter.asyncio.sleep", fake_sleep
        )

        async def fake_get(url, params=None, headers=None):
            series_id = params["series_id"]
            if series_id == "GDP":
                return _FakeResponse(
                    status_code=404,
                    json_data={
                        "error_code": 404,
                        "error_message": "Series not found.",
                    },
                )
            return _FakeResponse(
                status_code=200,
                json_data=_fred_payload([_obs("2026-08-13", "100.0")]),
            )

        monkeypatch.setattr(
            "src.adapters.fred_adapter.httpx.AsyncClient",
            lambda timeout: _FakeAsyncClient(fake_get),
        )

        adapter = FredAdapter()
        records = await adapter.fetch()
        # GDP fails permanently (404, not retried); the other 4 succeed.
        assert len(records) == 4
        assert all(r["series_id"] != "GDP" for r in records)
        assert adapter._pre_filter_count == 4


class TestFredFetchObservationStart:
    """fetch() sends the server-side observation_start date filter."""

    @pytest.mark.asyncio
    async def test_fetch_sends_observation_start_param(self, monkeypatch):
        fake_settings = SimpleNamespace(fred_api_key="key", fred_timeout_sec=30)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )
        monkeypatch.setattr(
            "src.adapters.fred_adapter._FRED_SERIES", ["UNRATE"]
        )

        captured = {}

        async def fake_get(url, params=None, headers=None):
            captured["params"] = dict(params)
            return _FakeResponse(
                status_code=200,
                json_data=_fred_payload(
                    [_obs("2026-08-13", "5.0"), _obs("2026-08-12", "4.5")]
                ),
            )

        monkeypatch.setattr(
            "src.adapters.fred_adapter.httpx.AsyncClient",
            lambda timeout: _FakeAsyncClient(fake_get),
        )

        adapter = FredAdapter()
        await adapter.fetch()

        params = captured["params"]
        # Server-side date filter present, YYYY-MM-DD, ≈ 90 days back.
        assert "observation_start" in params
        obs_start = params["observation_start"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", obs_start)
        expected = (
            datetime.now(timezone.utc) - timedelta(days=_FRED_FETCH_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")
        delta_days = abs(
            (
                datetime.strptime(obs_start, "%Y-%m-%d")
                - datetime.strptime(expected, "%Y-%m-%d")
            ).days
        )
        assert delta_days <= 1  # tolerate a midnight rollover
        # Client-side limit kept (server filter + client limit double).
        assert params["limit"] == "3"
        assert params["sort_order"] == "desc"
        assert params["file_type"] == "json"


class TestFredNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_gdp_snapshot(self, sample_fred_record):
        adapter = FredAdapter()
        episode = await adapter.normalize(sample_fred_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "fred"
        assert episode.source_description == "FRED (Federal Reserve Economic Data)"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["series_id"] == "GDP"
        assert episode.metadata["value"] == "29200.0"
        assert episode.metadata["previous_value"] == "29000.0"
        assert episode.metadata["units"] == "Bil. of $"

        # Entities: country + theme
        entity_types = {e.type for e in episode.entities}
        assert "country" in entity_types
        assert "theme" in entity_types
        assert any(e.name == "United States" for e in episode.entities)

        # Episode body: latest value + change vs previous
        assert "29200.0" in episode.episode_body
        assert "Bil. of $" in episode.episode_body
        assert "+200.00" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format embeds series_id (group_id)
        assert episode.name.startswith("fred-")
        assert "GDP" in episode.name

        # Source URL traceability
        assert episode.source_url == "https://fred.stlouisfed.org/series/GDP"

    @pytest.mark.asyncio
    async def test_normalize_date_cutoff_returns_none(self, monkeypatch):
        """Observation older than news_max_age_days → None (skipped)."""
        fake_settings = SimpleNamespace(news_max_age_days=14)
        monkeypatch.setattr(
            "src.adapters.fred_adapter.get_settings",
            lambda: fake_settings,
        )

        record = {
            "series_id": "GDP",
            "date": "2020-01-01",
            "realtime_start": "2020-01-01",
            "value": "21000.0",
            "previous_value": None,
            "units": "Bil. of $",
            "name": "Gross Domestic Product",
            "topic": "GDP Growth",
        }
        adapter = FredAdapter()
        episode = await adapter.normalize(record)
        assert episode is None


class TestFredDedup:
    """Cross-cycle dedup of unchanged snapshots."""

    @pytest.mark.asyncio
    async def test_dedup_identical_snapshots(self, sample_fred_record):
        adapter = FredAdapter()
        ep1 = await adapter.normalize(sample_fred_record)
        ep2 = await adapter.normalize(sample_fred_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


class TestFredHelpers:
    """Module-level helper functions."""

    def test_map_fred_severity_unrate_jump_high(self):
        assert _map_fred_severity("UNRATE", 5.0, 3.5) == "high"

    def test_map_fred_severity_dff_move_high(self):
        assert _map_fred_severity("DFF", 4.5, 3.5) == "high"

    def test_map_fred_severity_default_medium(self):
        assert _map_fred_severity("GDP", 29200.0, 29000.0) == "medium"

    def test_build_fred_body_contains_change(self):
        body = _build_fred_body(
            "GDP",
            "Gross Domestic Product",
            "2026-08-01",
            "29200.0",
            "29000.0",
            "Bil. of $",
        )
        assert "2026-08-01" in body
        assert "29200.0" in body
        assert "+200.00" in body
