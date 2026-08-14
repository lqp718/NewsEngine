"""Unit tests for AcledAdapter (Phase 1 macro adapter, OAuth2 password grant).

Covers: BaseAdapter contract, fetch degradation without credentials,
OAuth token acquisition (password grant), token caching across fetches,
refresh-token renewal, 401 re-auth retry, normalize (awaited — no latent
un-awaited bug) with severity boundary assertions, dedup, helpers.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import src.adapters.acled_adapter as acled_mod
from src.adapters.acled_adapter import (
    AcledAdapter,
    AcledAuthError,
    _ACLED_API_URL,
    _ACLED_OAUTH_TOKEN_URL,
    _build_acled_body,
    _clear_token_cache,
    _get_access_token,
    _map_acled_severity,
    _oauth_error_reason,
    _token_cache,
)
from src.adapters.base import BaseAdapter
from src.adapters.models import NormalizedEpisode


# ── Shared helpers ─────────────────────────────────────────────────────


def _recent_date(days_ago: int = 1) -> str:
    """YYYY-MM-DD a few days in the past (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%d"
    )


def _settings(**overrides) -> SimpleNamespace:
    """Fake Settings object with sane ACLED defaults for tests."""
    base = dict(
        acled_username="user@example.com",
        acled_password="s3cret",
        acled_timeout_sec=30,
        news_max_age_days=14,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _form_data(request: httpx.Request) -> dict[str, str]:
    """Decode the urlencoded body of an OAuth token POST."""
    return {
        k: v[0]
        for k, v in urllib.parse.parse_qs(
            request.content.decode("utf-8")
        ).items()
    }


def _token_response(
    access: str = "access-1",
    refresh: str = "refresh-1",
    expires_in: int = 86400,
) -> dict:
    """A standard ACLED OAuth token-endpoint response."""
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
    }


def _data_response() -> dict:
    """A synthetic /api/acled/read response with one recent event."""
    return {
        "data": [
            {
                "event_id_cnty": "UKR9999",
                "event_date": _recent_date(),
                "event_type": "Battles",
                "country": "Ukraine",
                "admin1": "",
                "actor1": "Military Forces of Russia",
                "actor2": "Military Forces of Ukraine",
                "fatalities": 42,
                "notes": "Artillery exchange near the front line.",
                "latitude": "48.37",
                "longitude": "31.17",
            }
        ]
    }


def _mock_client_factory(handler):
    """Build an AsyncClient factory wired to an httpx.MockTransport.

    The adapter constructs its own ``httpx.AsyncClient`` inside fetch();
    patching ``AsyncClient`` with this factory routes every request
    (token + data) through ``handler``. The real client class is captured
    before the patch is applied to avoid self-recursion.
    """
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    return factory


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Reset the module-level OAuth token cache + lock before each test."""
    _clear_token_cache()
    acled_mod._token_lock = asyncio.Lock()
    yield
    _clear_token_cache()


# ── Contract ───────────────────────────────────────────────────────────


class TestAcledContract:
    """BaseAdapter inheritance contract."""

    def test_inherits_base_adapter(self):
        adapter = AcledAdapter()
        assert isinstance(adapter, BaseAdapter)

    def test_source_type_constant(self):
        assert AcledAdapter.SOURCE_TYPE == "acled"

    def test_endpoint_constants(self):
        assert _ACLED_OAUTH_TOKEN_URL == "https://acleddata.com/oauth/token"
        assert _ACLED_API_URL == "https://acleddata.com/api/acled/read"


# ── Degradation ────────────────────────────────────────────────────────


class TestAcledFetchDegrade:
    """fetch() degrades gracefully when credentials are unconfigured."""

    @pytest.mark.asyncio
    async def test_fetch_empty_without_credentials(self, monkeypatch):
        fake_settings = _settings(acled_username="", acled_password="")
        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = AcledAdapter()
        result = await adapter.fetch()
        assert result == []
        assert adapter._pre_filter_count == 0

    @pytest.mark.asyncio
    async def test_fetch_empty_without_password(self, monkeypatch):
        """Username present but password missing → still degrade."""
        fake_settings = _settings(acled_username="user@example.com", acled_password="")
        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings",
            lambda: fake_settings,
        )

        adapter = AcledAdapter()
        result = await adapter.fetch()
        assert result == []


# ── OAuth flow ─────────────────────────────────────────────────────────


class TestAcledOAuthFlow:
    """OAuth2 password-grant: auth, caching, refresh, 401 retry."""

    @pytest.mark.asyncio
    async def test_password_grant_authenticates_and_fetches(self, monkeypatch):
        requests_seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request)
            if request.url.path == "/oauth/token":
                form = _form_data(request)
                assert form["grant_type"] == "password"
                assert form["client_id"] == "acled"
                assert form["scope"] == "authenticated"
                assert form["username"] == "user@example.com"
                assert form["password"] == "s3cret"
                return httpx.Response(200, json=_token_response())
            if request.url.path == "/api/acled/read":
                assert request.headers["authorization"] == "Bearer access-1"
                return httpx.Response(200, json=_data_response())
            return httpx.Response(404)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        records = await adapter.fetch()

        assert len(records) == 1
        assert records[0]["event_id_cnty"] == "UKR9999"
        assert records[0]["event_type"] == "Battles"
        assert records[0]["fatalities"] == 42
        # Exactly one token POST + one data GET
        assert (
            sum(1 for r in requests_seen if r.url.path == "/oauth/token") == 1
        )
        assert (
            sum(1 for r in requests_seen if r.url.path == "/api/acled/read")
            == 1
        )

    @pytest.mark.asyncio
    async def test_cached_token_reused_across_fetches(self, monkeypatch):
        token_posts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                token_posts["count"] += 1
                return httpx.Response(200, json=_token_response())
            if request.url.path == "/api/acled/read":
                return httpx.Response(200, json=_data_response())
            return httpx.Response(404)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        await adapter.fetch()
        await adapter.fetch()

        # Second fetch reuses the cached (still-valid) access token.
        assert token_posts["count"] == 1

    @pytest.mark.asyncio
    async def test_expired_token_refreshed_via_refresh_grant(self, monkeypatch):
        # Seed cache with an expired access token + valid refresh token.
        _token_cache["access_token"] = "stale-token"
        _token_cache["refresh_token"] = "refresh-1"
        _token_cache["expires_at"] = time.time() - 60

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                form = _form_data(request)
                assert form["grant_type"] == "refresh_token"
                assert form["refresh_token"] == "refresh-1"
                assert form["client_id"] == "acled"
                return httpx.Response(
                    200,
                    json=_token_response(
                        access="fresh-token", refresh="refresh-2"
                    ),
                )
            if request.url.path == "/api/acled/read":
                assert (
                    request.headers["authorization"] == "Bearer fresh-token"
                )
                return httpx.Response(200, json=_data_response())
            return httpx.Response(404)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        records = await adapter.fetch()

        assert len(records) == 1
        assert _token_cache["access_token"] == "fresh-token"
        assert _token_cache["refresh_token"] == "refresh-2"

    @pytest.mark.asyncio
    async def test_refresh_failure_falls_back_to_password_grant(
        self, monkeypatch
    ):
        _token_cache["access_token"] = "stale-token"
        _token_cache["refresh_token"] = "dead-refresh"
        _token_cache["expires_at"] = time.time() - 60

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                form = _form_data(request)
                if form.get("grant_type") == "refresh_token":
                    return httpx.Response(
                        401,
                        json={
                            "error": "invalid_grant",
                            "error_description": "The refresh token is invalid",
                        },
                    )
                assert form["grant_type"] == "password"
                return httpx.Response(
                    200, json=_token_response(access="fresh-token")
                )
            if request.url.path == "/api/acled/read":
                return httpx.Response(200, json=_data_response())
            return httpx.Response(404)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        records = await adapter.fetch()

        assert len(records) == 1
        assert _token_cache["access_token"] == "fresh-token"
        # Password grant returns a fresh refresh token → stored.
        assert _token_cache["refresh_token"] == "refresh-1"

    @pytest.mark.asyncio
    async def test_401_clears_cache_and_retries_once(self, monkeypatch):
        # Cached token is unexpired but the server rejects it (revoked).
        _token_cache["access_token"] = "revoked-token"
        _token_cache["refresh_token"] = "refresh-1"
        _token_cache["expires_at"] = time.time() + 3600
        data_hits = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                form = _form_data(request)
                # Cache was cleared → straight to a fresh password grant.
                assert form["grant_type"] == "password"
                return httpx.Response(
                    200, json=_token_response(access="new-token")
                )
            if request.url.path == "/api/acled/read":
                data_hits["count"] += 1
                if data_hits["count"] == 1:
                    assert (
                        request.headers["authorization"]
                        == "Bearer revoked-token"
                    )
                    return httpx.Response(
                        401, json={"error": "invalid_token"}
                    )
                assert (
                    request.headers["authorization"] == "Bearer new-token"
                )
                return httpx.Response(200, json=_data_response())
            return httpx.Response(404)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        records = await adapter.fetch()

        assert len(records) == 1
        assert data_hits["count"] == 2  # failed once, retried once
        assert _token_cache["access_token"] == "new-token"

    @pytest.mark.asyncio
    async def test_fetch_empty_when_auth_rejected(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(
                    401,
                    json={
                        "error": "invalid_grant",
                        "error_description": "The user credentials were incorrect",
                    },
                )
            return httpx.Response(404)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        result = await adapter.fetch()
        assert result == []
        assert adapter._pre_filter_count == 0

    @pytest.mark.asyncio
    async def test_fetch_empty_on_transport_failure(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings", lambda: _settings()
        )
        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        adapter = AcledAdapter()
        result = await adapter.fetch()
        assert result == []


# ── OAuth helpers ──────────────────────────────────────────────────────


class TestAcledOAuthHelpers:
    """Token cache + error-message helpers."""

    def test_clear_token_cache_resets_all(self):
        _token_cache.update(
            access_token="a", refresh_token="r", expires_at=time.time()
        )
        _clear_token_cache()
        assert _token_cache["access_token"] is None
        assert _token_cache["refresh_token"] is None
        assert _token_cache["expires_at"] == 0.0

    def test_oauth_error_reason_invalid_grant(self):
        reason = _oauth_error_reason(
            401, {"error": "invalid_grant", "error_description": "x"}
        )
        assert "用户名或密码错误" in reason

    def test_oauth_error_reason_flood_blocked(self):
        reason = _oauth_error_reason(429, {"error": "flood_user_blocked"})
        assert "封禁" in reason

    def test_oauth_error_reason_generic(self):
        reason = _oauth_error_reason(500, {})
        assert "HTTP 500" in reason

    @pytest.mark.asyncio
    async def test_get_access_token_raises_on_rejection(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_grant"})

        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        with pytest.raises(AcledAuthError):
            await _get_access_token(_settings())

    @pytest.mark.asyncio
    async def test_get_access_token_raises_when_token_missing(self, monkeypatch):
        """200 response without access_token → AcledAuthError, not KeyError."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"expires_in": 86400})

        monkeypatch.setattr(
            acled_mod.httpx, "AsyncClient", _mock_client_factory(handler)
        )

        with pytest.raises(AcledAuthError):
            await _get_access_token(_settings())


# ── Severity ───────────────────────────────────────────────────────────


class TestAcledSeverity:
    """Fatality-based severity mapping boundaries."""

    def test_critical_at_100_plus(self):
        assert _map_acled_severity(120, "Battles") == "critical"
        assert _map_acled_severity(100, "Battles") == "critical"

    def test_high_at_25_plus(self):
        assert _map_acled_severity(25, "Riots") == "high"
        assert _map_acled_severity(26, "Riots") == "high"

    def test_medium_at_1_plus(self):
        assert _map_acled_severity(1, "Riots") == "medium"
        assert _map_acled_severity(24, "Riots") == "medium"

    def test_low_at_zero_fatalities(self):
        assert _map_acled_severity(0, "Riots") == "low"
        assert _map_acled_severity(0, "Protests") == "low"

    def test_battles_minimum_medium(self):
        """Battles/explosions are at least medium even with 0 fatalities."""
        assert _map_acled_severity(0, "Battles") == "medium"
        assert _map_acled_severity(0, "Explosions/Remote violence") == "medium"


# ── Normalize ──────────────────────────────────────────────────────────


class TestAcledNormalize:
    """normalize() — MUST be awaited (no latent un-awaited bug)."""

    @pytest.mark.asyncio
    async def test_normalize_high_fatality_battle(self, sample_acled_record):
        adapter = AcledAdapter()
        episode = await adapter.normalize(sample_acled_record)

        assert isinstance(episode, NormalizedEpisode)
        assert episode.source_type == "acled"
        assert episode.source_description == "ACLED Armed Conflict Location & Event Data"

        # Fatality-based severity: 120 fatalities → critical
        assert episode.severity == "critical"

        # Structured metadata
        assert episode.metadata.get("_structured") is True
        assert episode.metadata["event_type"] == "Battles"
        assert episode.metadata["country"] == "Ukraine"
        assert episode.metadata["fatalities"] == 120
        assert episode.metadata["actor1"] == "Military Forces of Russia"
        assert episode.metadata["actor2"] == "Military Forces of Ukraine"

        # Entities: country + actor organizations
        entity_types = {e.type for e in episode.entities}
        assert "country" in entity_types
        assert "organization" in entity_types
        assert any(e.name == "Ukraine" for e in episode.entities)

        # Episode body
        assert "Battles" in episode.episode_body
        assert "Ukraine" in episode.episode_body
        assert "120" in episode.episode_body

        # Content hash consistency
        assert len(episode.content_hash) == 64
        assert episode.content_hash == episode.compute_hash()

        # Name format
        assert episode.name.startswith("acled-")

    @pytest.mark.asyncio
    async def test_normalize_zero_fatality_riot_low(self):
        """Zero-fatality riot → low severity."""
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        record = {
            "event_id_cnty": "USA9999",
            "event_date": recent,
            "event_type": "Riots",
            "country": "United States",
            "actor1": "Protesters",
            "actor2": "",
            "fatalities": 0,
            "notes": "",
        }
        adapter = AcledAdapter()
        episode = await adapter.normalize(record)
        assert episode is not None
        assert episode.severity == "low"
        assert episode.metadata["fatalities"] == 0

    @pytest.mark.asyncio
    async def test_normalize_date_cutoff_returns_none(self, monkeypatch):
        """Event older than news_max_age_days → None (skipped)."""
        fake_settings = SimpleNamespace(news_max_age_days=14)
        monkeypatch.setattr(
            "src.adapters.acled_adapter.get_settings",
            lambda: fake_settings,
        )

        record = {
            "event_id_cnty": "OLD0001",
            "event_date": "2015-03-01",
            "event_type": "Battles",
            "country": "Syria",
            "actor1": "Government",
            "actor2": "Rebels",
            "fatalities": 5,
            "notes": "",
        }
        adapter = AcledAdapter()
        episode = await adapter.normalize(record)
        assert episode is None

    @pytest.mark.asyncio
    async def test_normalize_invalid_date_returns_none(self):
        adapter = AcledAdapter()
        record = {
            "event_id_cnty": "BAD0001",
            "event_date": "not-a-date",
            "event_type": "Battles",
            "country": "Ukraine",
            "fatalities": 1,
            "notes": "",
        }
        episode = await adapter.normalize(record)
        assert episode is None


# ── Dedup ──────────────────────────────────────────────────────────────


class TestAcledDedup:
    """Cross-cycle dedup of identical conflict events."""

    @pytest.mark.asyncio
    async def test_dedup_identical_events(self, sample_acled_record):
        adapter = AcledAdapter()
        ep1 = await adapter.normalize(sample_acled_record)
        ep2 = await adapter.normalize(sample_acled_record)
        assert ep1 is not None and ep2 is not None
        assert ep1.content_hash == ep2.content_hash

        result = adapter.dedup([ep1, ep2])
        assert len(result) == 1
        assert result[0].name == ep1.name


# ── Helpers ────────────────────────────────────────────────────────────


class TestAcledHelpers:
    """Module-level helper functions."""

    def test_build_acled_body(self):
        body = _build_acled_body(
            "UKR1234",
            "2026-08-10",
            "Battles",
            "Ukraine",
            "Actor A",
            "Actor B",
            12,
            "Some notes",
        )
        assert "UKR1234" in body
        assert "Battles" in body
        assert "Ukraine" in body
        assert "12" in body
        assert "Actor A" in body
        assert "Some notes" in body

    def test_build_acled_body_trims_long_notes(self):
        long_notes = "x" * 600
        body = _build_acled_body(
            "UKR1", "2026-08-10", "Riots", "USA", "", "", 0, long_notes
        )
        assert len(body) < len(long_notes) + 300
        assert "..." in body
