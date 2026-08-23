"""FRED Adapter — fetch US Federal Reserve economic data.

Data source: FRED API (https://api.stlouisfed.org/fred, free API key
registration: https://fred.stlouisfed.org/docs/api/api_key.html).

Phase 1 (add-phase1-macro-adapters): key-gated fetch of latest
observations for a fixed set of key macro series (GDP / CPI /
unemployment rate / federal funds rate / PPI), one snapshot episode
per series per cycle.

Robustness (fred-adapter-optimization): serial requests, one series
at a time; per-series retry for transient failures (429 rate limit /
5xx / transport errors) with exponential backoff that honors the
``Retry-After`` header, so a failing series never blocks the others;
server-side ``observation_start`` filter (90-day lookback) keeps
payloads small on top of the client-side ``limit``.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient; returns [] + warning when api_key is unconfigured
- normalize(): one NormalizedEpisode per series snapshot; date-window
  cutoff (release date) returns None
- severity: module-level `_map_fred_severity` (default medium, threshold
  helpers for unemployment / policy-rate jumps)
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.core.config import get_settings
from src.utils.entity_canonical import canonical_name
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Module-level constants ─────────────────────────────────────────────

_FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"

# Default series to track (design.md ADR: module-level constant).
_FRED_SERIES = ["GDP", "CPIAUCSL", "UNRATE", "DFF", "PPIACO"]

# How many observations to request per series (latest first, desc).
# Latest + one previous is enough for the change delta; a third guards
# against a masked/missing most-recent value.
_FRED_FETCH_LENGTH = 3

# How far back the server-side ``observation_start`` filter reaches.
# 90 days covers every series' publication lag (GDP ~4-6 weeks,
# CPI/PPI ~2-3 weeks, UNRATE ~1 week, DFF daily) while keeping the
# payload small; normalize() still applies the authoritative
# ``news_max_age_days`` window on top.
_FRED_FETCH_LOOKBACK_DAYS = 90

# FRED includes monthly/quarterly data (GDP, CPI, PPI, UNRATE) with
# publication lags of 1-6 weeks.  The global ``news_max_age_days`` (14)
# would filter out the latest observation for slow-moving series.
# Use a source-specific 90-day window.
_FRED_MAX_AGE_DAYS = 90

# Retry policy for transient failures. FRED rate-limits per key and
# returns 429 when exceeded (errors.html); 5xx are transient server
# errors. Exponential backoff: 1s, 3s, 9s (base 1.0s, multiplier 3.0).
# Permanent 4xx (400/404/423 …) are logged and never retried.
_FRED_MAX_RETRIES = 3  # retries after the initial attempt
_FRED_RETRY_BACKOFF_BASE = 1.0  # seconds, multiplied by 3**attempt
_FRED_RETRY_BACKOFF_MULTIPLIER = 3.0
_FRED_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Per-series display metadata: human name, units, topic entity name.
_FRED_SERIES_META: dict[str, dict[str, str]] = {
    "GDP": {
        "name": "Gross Domestic Product",
        "units": "Bil. of $",
        "topic": "GDP Growth",
        "context": "GDP measures the total value of goods and services produced in the US. Released quarterly. Leading indicator for economic health. Strong GDP growth typically supports equity markets and may prompt Fed tightening.",
    },
    "CPIAUCSL": {
        "name": "Consumer Price Index for All Urban Consumers",
        "units": "Index 1982-1984=100",
        "topic": "Inflation",
        "context": "CPI tracks the average change in prices paid by urban consumers for a basket of goods and services. Primary inflation gauge for the Fed. Rising CPI may trigger rate hikes; falling CPI may signal easing.",
    },
    "UNRATE": {
        "name": "Unemployment Rate",
        "units": "Percent",
        "topic": "Unemployment",
        "context": "Percentage of the labor force that is unemployed and actively seeking work. Lagging indicator — rises after recession ends, falls during recovery. Part of the Fed's dual mandate (maximum employment).",
    },
    "DFF": {
        "name": "Federal Funds Rate",
        "units": "Percent",
        "topic": "Federal Funds Rate",
        "context": "Interest rate at which depository institutions lend reserve balances to other institutions overnight. Primary tool for Fed monetary policy. Rate hikes tighten money supply; cuts stimulate economy.",
    },
    "PPIACO": {
        "name": "Producer Price Index",
        "units": "Index 1982=100",
        "topic": "Producer Prices",
        "context": "PPI measures the average change in selling prices received by domestic producers. Leading indicator for consumer inflation — producer cost increases often pass through to consumer prices.",
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


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header (delay-seconds) if present.

    FRED returns delay-seconds on 429 (errors.html). Returns ``None``
    when the header is absent or not a plain number (RFC 7231 also
    allows an HTTP-date; FRED does not use it).
    """
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.debug("FRED: unparseable Retry-After: %s", raw)
        return None


def _extract_error_message(resp: httpx.Response) -> str:
    """Extract FRED's error message from a non-2xx JSON body.

    FRED error bodies look like ``{"error_code": 400, "error_message":
    "..."}`` (errors.html). Returns ``""`` when the body is not
    parseable or has no message.
    """
    try:
        data = resp.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        return str(data.get("error_message") or data.get("message") or "")
    return ""


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
    context: str | None = None,
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
    if context:
        lines.append(f"\n**Context**: {context}")
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
        # Server-side date filter: only observations from the last
        # ``_FRED_FETCH_LOOKBACK_DAYS`` days. Deliberately wider than the
        # client-side ``news_max_age_days`` window so the API-side filter
        # is a loose efficiency hint; normalize() stays authoritative.
        observation_start = (
            datetime.now(timezone.utc) - timedelta(days=_FRED_FETCH_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Serial requests, one series at a time (deliberate: keep the
            # request rate well inside FRED's 120 req/min/key limit and
            # keep retry/backoff behavior deterministic).
            for series_id in _FRED_SERIES:
                try:
                    params = {
                        "series_id": series_id,
                        "api_key": settings.fred_api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        # Server-side + client-side limits together: the
                        # API filters by date range first, then we cap
                        # how many rows come back.
                        "observation_start": observation_start,
                        "limit": str(_FRED_FETCH_LENGTH),
                    }
                    resp = await self._get_with_retry(client, params, series_id)
                    if resp is None:
                        continue
                    if resp.status_code != 200:
                        logger.warning(
                            "FRED: series %s failed — HTTP %d: %s",
                            series_id,
                            resp.status_code,
                            _extract_error_message(resp) or resp.reason_phrase,
                        )
                        continue

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
                            "context": meta.get("context", ""),
                        }
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning(
                        "FRED fetch failed for series %s: %s", series_id, exc
                    )

        self._pre_filter_count = len(records)
        logger.info("FRED: fetched %d series snapshots", len(records))
        return records

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        params: dict[str, Any],
        series_id: str,
    ) -> httpx.Response | None:
        """GET the observations endpoint, retrying transient failures.

        Retries 429 (rate limit) and 5xx / transport errors up to
        ``_FRED_MAX_RETRIES`` times with exponential backoff
        (``_FRED_RETRY_BACKOFF_BASE * multiplier**attempt`` → 1s/3s/9s),
        honoring the ``Retry-After`` header when present (429).

        Returns:
            The final response (retryable statuses that exhausted their
            attempts are returned as-is so the caller can log them), or
            ``None`` when the connection itself keeps failing.
        """
        resp: httpx.Response | None = None
        for attempt in range(_FRED_MAX_RETRIES + 1):
            try:
                resp = await client.get(_FRED_API_URL, params=params)
            except httpx.HTTPError as exc:
                if attempt >= _FRED_MAX_RETRIES:
                    logger.warning(
                        "FRED: request failed for series %s after %d attempts: %s",
                        series_id,
                        _FRED_MAX_RETRIES + 1,
                        exc,
                    )
                    return None
                delay = _FRED_RETRY_BACKOFF_BASE * (
                    _FRED_RETRY_BACKOFF_MULTIPLIER**attempt
                )
                logger.warning(
                    "FRED: request error for series %s (attempt %d/%d): %s"
                    " — retrying in %.1fs",
                    series_id,
                    attempt + 1,
                    _FRED_MAX_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code not in _FRED_RETRYABLE_STATUS:
                return resp
            if attempt >= _FRED_MAX_RETRIES:
                return resp

            retry_after = _parse_retry_after(resp)
            delay = (
                retry_after
                if retry_after is not None
                else _FRED_RETRY_BACKOFF_BASE
                * (_FRED_RETRY_BACKOFF_MULTIPLIER**attempt)
            )
            logger.warning(
                "FRED: HTTP %d for series %s (attempt %d/%d): %s"
                " — retrying in %.1fs",
                resp.status_code,
                series_id,
                attempt + 1,
                _FRED_MAX_RETRIES,
                _extract_error_message(resp) or resp.reason_phrase,
                delay,
            )
            await asyncio.sleep(delay)
        return resp

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
        context = str(record.get("context", "") or "")

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
        cutoff = datetime.now(timezone.utc) - timedelta(days=_FRED_MAX_AGE_DAYS)
        if cutoff_date < cutoff:
            logger.debug(
                "FRED: %s observation older than %d days — skipping",
                series_id,
                _FRED_MAX_AGE_DAYS,
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
            series_id, name, date_str, value, previous_value, units,
            context=context or None,
        )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        ep_name = NormalizedEpisode.make_name(
            source_type="fred",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=series_id,
        )

        entities = [
            EntityItem(type="country", name=canonical_name("United States", "country")),
            EntityItem(type="theme", name=canonical_name(topic, "theme")),
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
                "content_scope": "MACRO",
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
    "_FRED_FETCH_LENGTH",
    "_FRED_FETCH_LOOKBACK_DAYS",
    "_FRED_MAX_RETRIES",
    "_parse_retry_after",
    "_extract_error_message",
    "_map_fred_severity",
    "_build_fred_body",
]
