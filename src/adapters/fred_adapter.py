"""FRED Adapter — fetch US Federal Reserve economic data.

Data source: FRED API (https://api.stlouisfed.org/fred, free API key
registration: https://fred.stlouisfed.org/docs/api/api_key.html).

Phase 1 (add-phase1-macro-adapters): key-gated fetch of latest
observations for a fixed set of key macro series (GDP / CPI /
unemployment rate / federal funds rate / PPI), one snapshot episode
per series per cycle.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient; returns [] + warning when api_key is unconfigured
- normalize(): one NormalizedEpisode per series snapshot; date-window
  cutoff (release date) returns None
- severity: module-level `_map_fred_severity` (default medium, threshold
  helpers for unemployment / policy-rate jumps)
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

_FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"

# Default series to track (design.md ADR: module-level constant).
_FRED_SERIES = ["GDP", "CPIAUCSL", "UNRATE", "DFF", "PPIACO"]

# Per-series display metadata: human name, units, topic entity name.
_FRED_SERIES_META: dict[str, dict[str, str]] = {
    "GDP": {
        "name": "Gross Domestic Product",
        "units": "Bil. of $",
        "topic": "GDP Growth",
    },
    "CPIAUCSL": {
        "name": "Consumer Price Index for All Urban Consumers",
        "units": "Index 1982-1984=100",
        "topic": "Inflation",
    },
    "UNRATE": {
        "name": "Unemployment Rate",
        "units": "Percent",
        "topic": "Unemployment",
    },
    "DFF": {
        "name": "Federal Funds Rate",
        "units": "Percent",
        "topic": "Federal Funds Rate",
    },
    "PPIACO": {
        "name": "Producer Price Index",
        "units": "Index 1982=100",
        "topic": "Producer Prices",
    },
}


# ── Module-level helper functions ──────────────────────────────────────


def _parse_observation_date(date_str: str | None) -> datetime | None:
    """Parse a FRED observation date (YYYY-MM-DD) into a UTC datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        logger.debug("FRED: unparseable observation date: %s", date_str)
        return None


def _map_fred_severity(
    series_id: str,
    value: float | None,
    previous_value: float | None,
) -> Severity:
    """Map a series snapshot to a severity level.

    Default is ``medium``; threshold helpers raise the severity for
    sharp macro moves (design.md ADR-5 extension point):
    - UNRATE  (unemployment rate): +1.0pp or more → ``high``
    - DFF     (federal funds rate): |Δ| >= 0.75pp → ``high``
    - CPIAUCSL / PPIACO (price indices): Δ >= 1.0 → ``high``
    """
    if value is None or previous_value is None:
        return "medium"
    try:
        change = float(value) - float(previous_value)
    except (TypeError, ValueError):
        return "medium"

    if series_id == "UNRATE" and change >= 1.0:
        return "high"
    if series_id == "DFF" and abs(change) >= 0.75:
        return "high"
    if series_id in ("CPIAUCSL", "PPIACO") and change >= 1.0:
        return "high"
    return "medium"


def _build_fred_body(
    series_id: str,
    name: str,
    date_str: str,
    value: str,
    previous_value: str | None,
    units: str,
) -> str:
    """Build a structured Markdown episode body for one series snapshot."""
    lines = [f"## FRED: {name} ({series_id})", ""]
    lines.append(f"- Observation date: {date_str}")
    lines.append(f"- Latest value: {value} {units}")
    if previous_value is not None:
        try:
            delta = float(value) - float(previous_value)
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            lines.append(
                f"- Change vs previous observation: {delta:+.2f} {units} ({direction})"
            )
        except (TypeError, ValueError):
            lines.append(f"- Previous value: {previous_value} {units}")
    return "\n".join(lines)


# ── Adapter ────────────────────────────────────────────────────────────


class FredAdapter(BaseAdapter):
    """FRED (Federal Reserve Economic Data) adapter.

    One snapshot episode per series per cycle. Degrades gracefully to
    ``[]`` (with a warning) when ``fred_api_key`` is unconfigured.
    """

    SOURCE_TYPE = "fred"

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch latest observations for the default FRED series.

        Returns:
            List of per-series snapshot records, each with keys:
            ``series_id`` / ``date`` / ``value`` / ``previous_value`` /
            ``units`` / ``name`` / ``topic`` / ``realtime_start``.
            Empty list (with warning) when ``fred_api_key`` is unconfigured.
        """
        settings = get_settings()
        if not settings.fred_api_key:
            logger.warning(
                "FRED API key not configured — skipping FRED fetch "
                "(set FRED_API_KEY to enable)"
            )
            return []

        records: list[dict] = []
        timeout = settings.fred_timeout_sec
        async with httpx.AsyncClient(timeout=timeout) as client:
            for series_id in _FRED_SERIES:
                try:
                    params = {
                        "series_id": series_id,
                        "api_key": settings.fred_api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": "3",
                    }
                    resp = await client.get(_FRED_API_URL, params=params)
                    resp.raise_for_status()
                    payload = resp.json()
                    observations = payload.get("observations") or []
                    if not observations:
                        logger.debug("FRED: no observations for %s", series_id)
                        continue

                    meta = _FRED_SERIES_META.get(series_id, {})
                    latest = observations[0]
                    prev = observations[1] if len(observations) > 1 else None
                    records.append(
                        {
                            "series_id": series_id,
                            "date": latest.get("date"),
                            "realtime_start": latest.get("realtime_start"),
                            "value": latest.get("value"),
                            "previous_value": prev.get("value") if prev else None,
                            "units": meta.get("units", ""),
                            "name": meta.get("name", series_id),
                            "topic": meta.get("topic", series_id),
                        }
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning(
                        "FRED fetch failed for series %s: %s", series_id, exc
                    )

        self._pre_filter_count = len(records)
        logger.info("FRED: fetched %d series snapshots", len(records))
        return records

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert one FRED series snapshot to a NormalizedEpisode.

        Returns ``None`` when the observation is outside the
        ``news_max_age_days`` window (checked against the release date
        ``realtime_start`` when present, else the observation date).
        """
        series_id = str(record.get("series_id", ""))
        date_str = record.get("date", "")
        value = str(record.get("value", ""))
        previous_value = record.get("previous_value")
        units = str(record.get("units", ""))
        name = str(record.get("name", series_id))
        topic = str(record.get("topic", series_id))

        valid_at = _parse_observation_date(date_str)
        if valid_at is None:
            logger.debug("FRED: invalid observation date, skipping: %r", record)
            return None

        # Date window cutoff — use release date (realtime_start) when
        # available: macro series are published with a lag, so recency is
        # measured from release, falling back to the observation date.
        cutoff_date = _parse_observation_date(record.get("realtime_start"))
        if cutoff_date is None:
            cutoff_date = valid_at
        settings = get_settings()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.news_max_age_days
        )
        if cutoff_date < cutoff:
            logger.debug(
                "FRED: %s observation older than %d days — skipping",
                series_id,
                settings.news_max_age_days,
            )
            return None

        try:
            value_f = float(value)
        except (TypeError, ValueError):
            value_f = None
        try:
            previous_f = float(previous_value) if previous_value is not None else None
        except (TypeError, ValueError):
            previous_f = None

        severity = _map_fred_severity(series_id, value_f, previous_f)
        episode_body = _build_fred_body(
            series_id, name, date_str, value, previous_value, units
        )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        ep_name = NormalizedEpisode.make_name(
            source_type="fred",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=series_id,
        )

        entities = [
            EntityItem(type="country", name="United States"),
            EntityItem(type="theme", name=topic),
        ]

        return NormalizedEpisode(
            episode_body=episode_body,
            name=ep_name,
            source_description="FRED (Federal Reserve Economic Data)",
            source_type="fred",
            source_url=f"https://fred.stlouisfed.org/series/{series_id}",
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=["fred", series_id, topic.lower()],
            entities=entities,
            metadata={
                "_structured": True,
                "series_id": series_id,
                "value": value,
                "previous_value": previous_value,
                "units": units,
            },
        )


__all__ = [
    "FredAdapter",
    "_FRED_SERIES",
    "_FRED_SERIES_META",
    "_map_fred_severity",
    "_build_fred_body",
]
