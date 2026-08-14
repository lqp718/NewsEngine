"""ACLED Adapter — armed conflict event data.

Data source: ACLED API (https://acleddata.com/api/acled/read, requires a
myACLED account). Authentication uses the OAuth2 resource-owner password
grant (https://acleddata.com/api-documentation/getting-started):

1. POST ``https://acleddata.com/oauth/token`` with username + password
   → ``access_token`` (valid 24h) + ``refresh_token`` (valid 14 days)
2. Every data request carries ``Authorization: Bearer <access_token>``
3. Expired access tokens are refreshed via ``grant_type=refresh_token``
   before falling back to a fresh password grant

Phase 1 (add-phase1-macro-adapters): OAuth-gated fetch of recent conflict
events (battles / explosions / protests / riots) within the
``news_max_age_days`` window. Each event becomes one NormalizedEpisode.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient; returns [] + warning when username/password
  unconfigured; returns [] + explicit ERROR with the OAuth failure reason
  when credentials are rejected or the account is blocked
- normalize(): one NormalizedEpisode per event; fatality-based severity
- severity: module-level `_map_acled_severity` (>=100 → critical,
  >=25 → high, >=1 → medium, 0 → low; battles/explosions >= medium)
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.core.config import get_settings
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Module-level constants ─────────────────────────────────────────────

_ACLED_OAUTH_TOKEN_URL = "https://acleddata.com/oauth/token"
_ACLED_API_URL = "https://acleddata.com/api/acled/read"

# OAuth2 password-grant constants (per official ACLED docs).
_ACLED_CLIENT_ID = "acled"
_ACLED_SCOPE = "authenticated"

# Access tokens are valid for 24h (86400s) per ACLED docs. Refresh with a
# safety margin so requests never ride a token that is about to expire.
_ACLED_TOKEN_SAFETY_MARGIN_SEC = 300

# ACLED fields requested from the API.
_ACLED_FIELDS = (
    "event_id_cnty,event_date,event_type,country,admin1,"
    "actor1,actor2,fatalities,notes,latitude,longitude"
)

# Event types that are treated as at-least-medium regardless of fatalities.
_HIGH_IMPACT_EVENT_TYPES = {"Battles", "Explosions/Remote violence"}


# ── OAuth token cache (module-level, process-lifetime) ─────────────────


class AcledAuthError(Exception):
    """ACLED OAuth authentication failure with a human-readable reason.

    Raised by ``_get_access_token`` when the token endpoint rejects the
    credentials (wrong password, unverified account) or rate-limits the
    account (flood_user_blocked). The message is specific enough to log
    directly — never a generic "fetch failed".
    """


_token_cache: dict[str, Any] = {
    "access_token": None,
    "refresh_token": None,
    "expires_at": 0.0,  # epoch seconds
}
_token_lock = asyncio.Lock()


def _clear_token_cache() -> None:
    """Drop cached tokens (call after a 401 or an auth failure)."""
    _token_cache["access_token"] = None
    _token_cache["refresh_token"] = None
    _token_cache["expires_at"] = 0.0


def _oauth_error_reason(status_code: int, payload: dict[str, Any]) -> str:
    """Map a failed token-endpoint response to a clear, actionable message."""
    error = str(payload.get("error", "")).lower()
    desc = str(payload.get("error_description", "") or "")

    if error == "invalid_grant":
        return (
            "ACLED 用户名或密码错误 (invalid_grant: "
            "The user credentials were incorrect) — 请检查 ACLED_USERNAME / "
            "ACLED_PASSWORD，或确认账号已激活"
        )
    if error == "flood_user_blocked":
        return (
            "ACLED 账号因多次登录失败被暂时封禁 (flood_user_blocked) — "
            "请稍后再试，或确认密码正确"
        )
    if error:
        base = f"ACLED OAuth 认证被拒绝 (error={error}"
        if desc:
            base += f": {desc}"
        return base + ")"
    return f"ACLED OAuth 认证失败 (HTTP {status_code})"


async def _request_token(form_data: dict[str, str], timeout_sec: float) -> dict:
    """POST the ACLED token endpoint; return the parsed JSON payload.

    Raises:
        AcledAuthError: token endpoint rejected the request (non-2xx or an
            ``error`` field in the payload — e.g. invalid_grant /
            flood_user_blocked).
        httpx.HTTPError / ValueError: transport-level failure (network,
            timeout, bad JSON) — callers treat these as transient.
    """
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        resp = await client.post(
            _ACLED_OAUTH_TOKEN_URL,
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {}

    if resp.status_code != 200 or payload.get("error"):
        raise AcledAuthError(_oauth_error_reason(resp.status_code, payload))
    if not payload.get("access_token"):
        raise AcledAuthError(
            f"ACLED OAuth 响应缺少 access_token (HTTP {resp.status_code})"
        )
    return payload


async def _get_access_token(settings: Any) -> str:
    """Return a valid Bearer access token (cached, refreshed, or new).

    Order of attempts, all serialized by ``_token_lock``:
    1. Cached token still valid (with safety margin) → return it
    2. Cached refresh token → ``grant_type=refresh_token``
    3. Fresh password grant with settings credentials

    Raises:
        AcledAuthError: credentials rejected / account blocked / no usable
            refresh token and password grant fails.
        httpx.HTTPError / ValueError: transient transport failure.
    """
    async with _token_lock:
        now = time.time()
        cached = _token_cache["access_token"]
        if cached and _token_cache["expires_at"] > now + _ACLED_TOKEN_SAFETY_MARGIN_SEC:
            return cached

        # Try refresh first — avoids re-entering credentials every 24h.
        refresh_token = _token_cache.get("refresh_token")
        if refresh_token:
            try:
                payload = await _request_token(
                    {
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                        "client_id": _ACLED_CLIENT_ID,
                    },
                    settings.acled_timeout_sec,
                )
                _token_cache["access_token"] = payload["access_token"]
                _token_cache["refresh_token"] = payload.get(
                    "refresh_token", refresh_token
                )
                _token_cache["expires_at"] = now + int(
                    payload.get("expires_in", 86400)
                )
                return payload["access_token"]
            except AcledAuthError as exc:
                # Refresh token rejected/expired → fall back to password grant.
                logger.warning(
                    "ACLED refresh token rejected (%s) — re-authenticating "
                    "with credentials",
                    exc,
                )
                _token_cache["refresh_token"] = None

        # Fresh password grant.
        payload = await _request_token(
            {
                "username": settings.acled_username,
                "password": settings.acled_password,
                "grant_type": "password",
                "client_id": _ACLED_CLIENT_ID,
                "scope": _ACLED_SCOPE,
            },
            settings.acled_timeout_sec,
        )
        _token_cache["access_token"] = payload["access_token"]
        _token_cache["refresh_token"] = payload.get("refresh_token")
        _token_cache["expires_at"] = now + int(payload.get("expires_in", 86400))
        return payload["access_token"]


# ── Module-level helper functions ──────────────────────────────────────


def _map_acled_severity(fatalities: int, event_type: str) -> Severity:
    """Map ACLED event fatalities + type to a severity level.

    Rules (design.md / spec):
    - fatalities >= 100 → ``critical``
    - fatalities >= 25 → ``high``
    - fatalities >= 1 → ``medium``
    - fatalities == 0 → ``low``, except battles/explosions → ``medium``
    """
    if fatalities >= 100:
        return "critical"
    if fatalities >= 25:
        return "high"
    if fatalities >= 1:
        return "medium"
    # Zero fatalities: battles/explosions are still medium-impact events
    if event_type in _HIGH_IMPACT_EVENT_TYPES:
        return "medium"
    return "low"


def _safe_int(value: Any, default: int = 0) -> int:
    """Parse an integer defensively (ACLED may return numeric strings)."""
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


def _build_acled_body(
    event_id: str,
    event_date: str,
    event_type: str,
    country: str,
    actor1: str,
    actor2: str,
    fatalities: int,
    notes: str,
) -> str:
    """Build a structured Markdown episode body for one ACLED event."""
    lines = [
        f"## ACLED: {event_type} in {country}",
        "",
        f"- Event ID: {event_id}",
        f"- Event date: {event_date}",
        f"- Event type: {event_type}",
        f"- Country: {country}",
        f"- Fatalities: {fatalities}",
    ]
    if actor1:
        lines.append(f"- Actor 1: {actor1}")
    if actor2:
        lines.append(f"- Actor 2: {actor2}")
    if notes:
        # Trim very long notes to keep episode bodies bounded
        notes_trimmed = notes if len(notes) <= 500 else notes[:497] + "..."
        lines.append(f"- Notes: {notes_trimmed}")
    return "\n".join(lines)


# ── Adapter ────────────────────────────────────────────────────────────


class AcledAdapter(BaseAdapter):
    """ACLED armed-conflict events adapter (OAuth2 password grant).

    Degrades gracefully to ``[]`` (with a warning) when
    ``acled_username`` or ``acled_password`` is unconfigured, and to ``[]``
    (with an explicit ERROR carrying the OAuth failure reason) when the
    token endpoint rejects the credentials or blocks the account.
    """

    SOURCE_TYPE = "acled"

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch recent ACLED conflict events.

        Returns:
            List of raw event records with ``event_id_cnty`` /
            ``event_date`` / ``event_type`` / ``country`` / ``actor1`` /
            ``actor2`` / ``fatalities`` / ``notes`` / ``latitude`` /
            ``longitude``. Empty list (with warning) when username or
            password is unconfigured; empty list (with explicit error)
            when OAuth authentication fails.
        """
        settings = get_settings()
        if not settings.acled_username or not settings.acled_password:
            logger.warning(
                "ACLED username or password not configured — skipping ACLED "
                "fetch (set ACLED_USERNAME and ACLED_PASSWORD to enable)"
            )
            return []

        # Backfill window: events from (now - news_max_age_days)
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.news_max_age_days
        )
        start_date = cutoff.strftime("%Y-%m-%d")

        params = {
            "event_date": start_date,
            "event_date_where": ">=",
            "limit": "500",
            "fields": _ACLED_FIELDS,
        }

        try:
            token = await _get_access_token(settings)
        except AcledAuthError as exc:
            logger.error("ACLED authentication failed: %s", exc)
            return []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ACLED OAuth request failed: %s", exc)
            return []

        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with httpx.AsyncClient(
                timeout=settings.acled_timeout_sec
            ) as client:
                resp = await client.get(_ACLED_API_URL, params=params, headers=headers)
                if resp.status_code == 401:
                    # Stale/revoked token → clear cache and re-authenticate once.
                    logger.info(
                        "ACLED API returned 401 — re-authenticating and retrying"
                    )
                    _clear_token_cache()
                    try:
                        token = await _get_access_token(settings)
                    except AcledAuthError as exc:
                        logger.error("ACLED authentication failed: %s", exc)
                        return []
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.warning("ACLED OAuth request failed: %s", exc)
                        return []
                    headers = {"Authorization": f"Bearer {token}"}
                    resp = await client.get(
                        _ACLED_API_URL, params=params, headers=headers
                    )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ACLED fetch failed: %s", exc)
            return []

        data = payload.get("data") or []
        records: list[dict] = []
        for item in data:
            records.append(
                {
                    "event_id_cnty": item.get("event_id_cnty", ""),
                    "event_date": item.get("event_date", ""),
                    "event_type": item.get("event_type", ""),
                    "country": item.get("country", ""),
                    "admin1": item.get("admin1", ""),
                    "actor1": item.get("actor1", ""),
                    "actor2": item.get("actor2", ""),
                    "fatalities": _safe_int(item.get("fatalities")),
                    "notes": item.get("notes", ""),
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                }
            )

        self._pre_filter_count = len(records)
        logger.info("ACLED: fetched %d conflict events", len(records))
        return records

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert one ACLED event to a NormalizedEpisode.

        Returns ``None`` for events outside the recency window.
        """
        event_id = str(record.get("event_id_cnty", "")).strip()
        event_date = str(record.get("event_date", ""))
        event_type = str(record.get("event_type", "Unknown"))
        country = str(record.get("country", ""))
        actor1 = str(record.get("actor1", "") or "")
        actor2 = str(record.get("actor2", "") or "")
        fatalities = _safe_int(record.get("fatalities"))
        notes = str(record.get("notes", "") or "")

        valid_at = _parse_event_date(event_date)
        if valid_at is None:
            logger.debug("ACLED: invalid event date, skipping: %r", record)
            return None

        # Date window cutoff
        settings = get_settings()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.news_max_age_days
        )
        if valid_at < cutoff:
            logger.debug(
                "ACLED: event %s older than %d days — skipping",
                event_id,
                settings.news_max_age_days,
            )
            return None

        severity = _map_acled_severity(fatalities, event_type)
        episode_body = _build_acled_body(
            event_id,
            event_date,
            event_type,
            country,
            actor1,
            actor2,
            fatalities,
            notes,
        )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        ep_name = NormalizedEpisode.make_name(
            source_type="acled",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id="conflict",
        )

        # Entities: country of occurrence + actors as organizations
        entities: list[EntityItem] = []
        if country:
            entities.append(EntityItem(type="country", name=country))
        for actor in (actor1, actor2):
            if actor and actor.lower() != "unknown":
                entities.append(EntityItem(type="organization", name=actor))

        return NormalizedEpisode(
            episode_body=episode_body,
            name=ep_name,
            source_description="ACLED Armed Conflict Location & Event Data",
            source_type="acled",
            source_url=None,
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=["acled", event_type.lower(), "conflict"],
            entities=entities,
            metadata={
                "_structured": True,
                "event_type": event_type,
                "country": country,
                "fatalities": fatalities,
                "actor1": actor1,
                "actor2": actor2,
            },
        )


def _parse_event_date(date_str: str) -> datetime | None:
    """Parse an ACLED event date (YYYY-MM-DD) to a UTC datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        logger.debug("ACLED: unparseable event date: %s", date_str)
        return None


__all__ = [
    "AcledAdapter",
    "AcledAuthError",
    "_clear_token_cache",
    "_get_access_token",
    "_map_acled_severity",
    "_oauth_error_reason",
    "_build_acled_body",
    "_ACLED_API_URL",
    "_ACLED_OAUTH_TOKEN_URL",
    "_HIGH_IMPACT_EVENT_TYPES",
]
