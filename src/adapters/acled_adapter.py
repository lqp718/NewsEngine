"""ACLED Adapter — armed conflict event data.

Data source: ACLED API (https://api.acleddata.com/acled/read, free
registration requires an API key + email).

Phase 1 (add-phase1-macro-adapters): key-gated fetch of recent conflict
events (battles / explosions / protests / riots) within the
``news_max_age_days`` window. Each event becomes one NormalizedEpisode.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient; returns [] + warning when key/email unconfigured
- normalize(): one NormalizedEpisode per event; fatality-based severity
- severity: module-level `_map_acled_severity` (>=100 → critical,
  >=25 → high, >=1 → medium, 0 → low; battles/explosions >= medium)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.core.config import get_settings
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Module-level constants ─────────────────────────────────────────────

_ACLED_API_URL = "https://api.acleddata.com/acled/read"

# ACLED fields requested from the API.
_ACLED_FIELDS = (
    "event_id_cnty,event_date,event_type,country,admin1,"
    "actor1,actor2,fatalities,notes,latitude,longitude"
)

# Event types that are treated as at-least-medium regardless of fatalities.
_HIGH_IMPACT_EVENT_TYPES = {"Battles", "Explosions/Remote violence"}


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
    """ACLED armed-conflict events adapter.

    Degrades gracefully to ``[]`` (with a warning) when
    ``acled_api_key`` or ``acled_email`` is unconfigured.
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
            ``longitude``. Empty list (with warning) when key or email
            is unconfigured.
        """
        settings = get_settings()
        if not settings.acled_api_key or not settings.acled_email:
            logger.warning(
                "ACLED API key or email not configured — skipping ACLED fetch "
                "(set ACLED_API_KEY and ACLED_EMAIL to enable)"
            )
            return []

        # Backfill window: events from (now - news_max_age_days)
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.news_max_age_days
        )
        start_date = cutoff.strftime("%Y-%m-%d")

        params = {
            "key": settings.acled_api_key,
            "email": settings.acled_email,
            "event_date": start_date,
            "event_date_where": ">=",
            "limit": "500",
            "fields": _ACLED_FIELDS,
        }

        try:
            async with httpx.AsyncClient(
                timeout=settings.acled_timeout_sec
            ) as client:
                resp = await client.get(_ACLED_API_URL, params=params)
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
    "_map_acled_severity",
    "_build_acled_body",
    "_ACLED_API_URL",
    "_HIGH_IMPACT_EVENT_TYPES",
]
