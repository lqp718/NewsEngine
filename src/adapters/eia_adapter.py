"""EIA Adapter — US Energy Information Administration data.

Data source: EIA Open Data API v2 (https://api.eia.gov/v2, free API key
registration: https://www.eia.gov/opendata/register.php).

Phase 1 (add-phase1-macro-adapters): key-gated fetch of key energy
series (crude oil inventories / production / imports / exports / retail
gasoline price), one snapshot episode per series per cycle.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient; returns [] + warning when api_key unconfigured
- normalize(): one NormalizedEpisode per series snapshot; date-window
  cutoff returns None
- severity: default ``medium`` (module-level `_map_eia_severity`)
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.core.config import get_settings
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Module-level constants ─────────────────────────────────────────────

_EIA_API_BASE = "https://api.eia.gov/v2"

# EIA data is weekly frequency; 14-day window too tight for reliable capture.
# Use 90-day window like BLS/FRED to ensure we don't miss weekly releases.
_EIA_MAX_AGE_DAYS = 90

# Series definitions: series_id → route + facets + display metadata.
# Facets disambiguate the series (area/product), per EIA v2 API docs.
_EIA_SERIES: dict[str, dict[str, Any]] = {
    # Weekly US crude oil ending stocks (thousand barrels)
    # Route: petroleum/sum/sndw (Weekly Supply Estimates, from EIA API v2 metadata)
    # Facets: series=WCRSTUS1 (direct series lookup)
    "WCRSTUS1": {
        "route": "petroleum/sum/sndw",
        "facets": {"series": "WCRSTUS1"},
        "frequency": "weekly",
        "name": "Weekly U.S. Crude Oil Ending Stocks",
        "units": "Thousand Barrels",
        "topic": "Crude Oil Inventories",
        "context": "Weekly crude oil inventories reported by EIA every Wednesday at 10:30 AM ET. Inventory builds (increases) are typically bearish for oil prices; draws (decreases) are bullish. Compare against 5-year average for seasonal context.",
    },
    # Weekly US crude oil field production (thousand barrels/day)
    # Route: petroleum/sum/sndw (Weekly Supply Estimates, from EIA API v2 metadata)
    # Facets: series=WCRFPUS2 (direct series lookup)
    "WCRFPUS2": {
        "route": "petroleum/sum/sndw",
        "facets": {"series": "WCRFPUS2"},
        "frequency": "weekly",
        "name": "Weekly U.S. Field Production of Crude Oil",
        "units": "Thousand Barrels per Day",
        "topic": "Crude Oil Production",
        "context": "Weekly US crude oil production volume. Tracks shale (Permian/Bakken/Eagle Ford) output trends. Production increases can offset OPEC+ cuts; declines may signal capital discipline or infrastructure constraints.",
    },
    # Weekly US crude oil imports (thousand barrels/day)
    # Route: petroleum/sum/sndw (Weekly Supply Estimates, from EIA API v2 metadata)
    # Facets: series=WCRIMUS2 (direct series lookup)
    "WCRIMUS2": {
        "route": "petroleum/sum/sndw",
        "facets": {"series": "WCRIMUS2"},
        "frequency": "weekly",
        "name": "Weekly U.S. Imports of Crude Oil",
        "units": "Thousand Barrels per Day",
        "topic": "Crude Oil Imports",
        "context": "Weekly US crude oil imports. Key for supply/demand balance analysis. Import volumes reflect domestic production gaps and refinery demand. Spikes may signal supply disruptions or seasonal refinery maintenance.",
    },
    # Weekly US crude oil exports (thousand barrels/day)
    # Route: petroleum/sum/sndw (Weekly Supply Estimates, from EIA API v2 metadata)
    # Facets: series=WCREXUS2 (direct series lookup)
    "WCREXUS2": {
        "route": "petroleum/sum/sndw",
        "facets": {"series": "WCREXUS2"},
        "frequency": "weekly",
        "name": "Weekly U.S. Exports of Crude Oil",
        "units": "Thousand Barrels per Day",
        "topic": "Crude Oil Exports",
        "context": "Weekly US crude oil exports. Reflects global demand for US light sweet crude. Export volumes affect domestic inventory levels and are sensitive to international price spreads (Brent-WTI) and geopolitical events.",
    },
    # Weekly US retail regular gasoline price (dollars/gallon)
    "WGASUS1": {
        "route": "petroleum/pri/gnd",
        "facets": {"duoarea": "NUS", "product": "EPM0"},
        "name": "Weekly U.S. Retail Gasoline Price",
        "units": "Dollars per Gallon",
        "topic": "Gasoline Prices",
        "context": "Weekly US retail regular gasoline price. Direct consumer impact of oil market dynamics. Rising prices may signal supply constraints or geopolitical risk; may also influence consumer spending and inflation expectations.",
    },
}

# How many observations to request per series (latest first, desc).
_EIA_FETCH_LENGTH = 3

# How far back the API-side ``start`` filter reaches. Deliberately wider
# than ``_EIA_MAX_AGE_DAYS`` so the API-side filter is a loose efficiency
# hint only; normalize() applies the authoritative freshness window.
_EIA_FETCH_LOOKBACK_DAYS = _EIA_MAX_AGE_DAYS + 5

# Retry policy. EIA docs: exceeding the per-second/per-hour request
# tolerances suspends the key until a cool-down, so honor Retry-After
# and back off exponentially on 429/5xx instead of hammering.
_EIA_MAX_RETRIES = 2
_EIA_RETRY_BACKOFF_BASE = 1.5  # seconds, doubled per attempt
_EIA_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Inter-series delay keeps the request rate comfortably inside EIA's
# per-second throttle (5 series ≈ 1.25s added per cycle).
_EIA_REQUEST_DELAY_SEC = 0.25


# ── Module-level helper functions ──────────────────────────────────────


def _parse_period_date(period: str) -> datetime | None:
    """Parse an EIA period string (YYYY-MM-DD or YYYY-MM) to UTC datetime."""
    if not period:
        return None
    try:
        if len(period) == 7:  # YYYY-MM
            dt = datetime.strptime(period, "%Y-%m")
        else:
            dt = datetime.fromisoformat(period)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.debug("EIA: unparseable period: %s", period)
        return None


def _map_eia_severity(
    series_id: str,
    value: float | None,
    previous_value: float | None,
) -> Severity:
    """Map an EIA series snapshot to a severity level.

    Default ``medium`` (design.md ADR-5). Sharp crude-oil inventory
    builds or price swings are elevated to ``high``.
    """
    if value is None or previous_value is None:
        return "medium"
    try:
        change = float(value) - float(previous_value)
    except (TypeError, ValueError):
        return "medium"

    if series_id == "WCRSTUS1" and abs(change) >= 10000:
        # >= 10M barrel weekly inventory swing is a strong signal
        return "high"
    if series_id == "WGASUS1" and abs(change) >= 0.25:
        # >= 25 cent weekly gasoline price move
        return "high"
    return "medium"


def _build_eia_body(
    series_id: str,
    name: str,
    period: str,
    value: str,
    previous_value: str | None,
    units: str,
    context: str | None = None,
) -> str:
    """Build a structured Markdown episode body for one EIA snapshot."""
    lines = [f"## EIA: {name} ({series_id})", ""]
    lines.append(f"- Period: {period}")
    lines.append(f"- Latest value: {value} {units}")
    if previous_value is not None:
        try:
            delta = float(value) - float(previous_value)
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            lines.append(
                f"- Change vs previous period: {delta:+.2f} {units} ({direction})"
            )
        except (TypeError, ValueError):
            lines.append(f"- Previous value: {previous_value} {units}")
    if context:
        lines.append(f"\n**Context**: {context}")
    return "\n".join(lines)


def _build_eia_source_url(series_id: str) -> str:
    """Build a unique, traceable APIv2 URL for one EIA series.

    The URL carries the route plus this series' facets as a query
    string. Two series on the same route (WCRIMUS2 / WCREXUS2 both on
    petroleum/sum/sndw) therefore get distinct ``source_url`` values
    and survive the URL-based dedup in ``BaseAdapter.dedup()``.
    """
    cfg = _EIA_SERIES.get(series_id, {})
    url = f"{_EIA_API_BASE}/{cfg.get('route', '')}/data/"
    facets = cfg.get('facets', {})
    if facets:
        url += '?' + urlencode(
            [(f'facets[{k}][]', v) for k, v in facets.items()]
        )
    return url


def _eia_error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from an EIA error/warning body."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason_phrase
    error = payload.get('error')
    if error:
        return str(error)
    warning = payload.get('warning')
    if warning:
        description = payload.get('description', '')
        return f'{warning} — {description}' if description else str(warning)
    return resp.reason_phrase or f'HTTP {resp.status_code}'


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header (seconds) if present."""
    header = resp.headers.get('Retry-After')
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


# ── Adapter ────────────────────────────────────────────────────────────


class EiaAdapter(BaseAdapter):
    """EIA (US Energy Information Administration) adapter.

    One snapshot episode per series per cycle. Degrades gracefully to
    ``[]`` (with a warning) when ``eia_api_key`` is unconfigured.
    """

    SOURCE_TYPE = "eia"

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch latest observations for the configured EIA series.

        Returns:
            List of per-series snapshot records with keys ``series_id`` /
            ``period`` / ``value`` / ``previous_value`` / ``units`` /
            ``name`` / ``topic``. Empty list (with warning) when
            ``eia_api_key`` is unconfigured.
        """
        settings = get_settings()
        if not settings.eia_api_key:
            logger.warning(
                "EIA API key not configured — skipping EIA fetch "
                "(set EIA_API_KEY to enable)"
            )
            return []

        records: list[dict] = []
        # APIv2 best practice: constrain with start/facets/length so each
        # response carries only the rows this adapter needs.
        start = (datetime.now(timezone.utc) - timedelta(
            days=_EIA_FETCH_LOOKBACK_DAYS
        )).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=settings.eia_timeout_sec) as client:
            for series_id, cfg in _EIA_SERIES.items():
                try:
                    params: dict[str, Any] = {
                        "api_key": settings.eia_api_key,
                        "data[]": "value",
                        "frequency": "weekly",
                        "start": start,
                        "sort[0][column]": "period",
                        "sort[0][direction]": "desc",
                        "length": str(_EIA_FETCH_LENGTH),
                    }
                    for facet, value in cfg["facets"].items():
                        params[f"facets[{facet}][]"] = value

                    url = f"{_EIA_API_BASE}/{cfg['route']}/data/"
                    resp = await self._get_with_retry(
                        client, url, params, series_id
                    )
                    if resp is None:
                        continue
                    if resp.status_code != 200:
                        logger.warning(
                            "EIA: series %s failed — HTTP %d: %s",
                            series_id,
                            resp.status_code,
                            _eia_error_message(resp),
                        )
                        continue

                    payload = resp.json()
                    warning = payload.get("warning")
                    if warning:
                        description = payload.get("description", "")
                        logger.warning(
                            "EIA: series %s response warning: %s%s",
                            series_id,
                            warning,
                            f" — {description}" if description else "",
                        )
                    data = (
                        (payload.get("response") or {}).get("data") or []
                    )
                    if not data:
                        logger.debug("EIA: no data for %s", series_id)
                        continue

                    latest = data[0]
                    prev = data[1] if len(data) > 1 else None
                    records.append(
                        {
                            "series_id": series_id,
                            "period": latest.get("period"),
                            "value": latest.get("value"),
                            "previous_value": prev.get("value") if prev else None,
                            "units": cfg["units"],
                            "name": cfg["name"],
                            "topic": cfg["topic"],
                            "context": cfg.get("context", ""),
                        }
                    )
                    logger.debug(
                        "EIA: %s period=%s value=%s",
                        series_id,
                        latest.get("period"),
                        latest.get("value"),
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning(
                        "EIA fetch failed for series %s: %s", series_id, exc
                    )
                # Polite pacing between series (EIA rate-limit guidance).
                await asyncio.sleep(_EIA_REQUEST_DELAY_SEC)

        self._pre_filter_count = len(records)
        logger.info("EIA: fetched %d series snapshots", len(records))
        return records

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, Any],
        series_id: str,
    ) -> httpx.Response | None:
        """GET ``url``, retrying transient failures (EIA rate limits / 5xx).

        Retries up to ``_EIA_MAX_RETRIES`` times with exponential backoff,
        honoring a ``Retry-After`` header when present. Returns the last
        response, or ``None`` when the connection itself keeps failing
        (already logged).
        """
        resp: httpx.Response | None = None
        for attempt in range(_EIA_MAX_RETRIES + 1):
            try:
                resp = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt >= _EIA_MAX_RETRIES:
                    logger.warning(
                        "EIA: request failed for series %s after %d attempts: %s",
                        series_id,
                        _EIA_MAX_RETRIES + 1,
                        exc,
                    )
                    return None
                delay = _EIA_RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "EIA: request error for series %s (attempt %d/%d): %s"
                    " — retrying in %.1fs",
                    series_id, attempt + 1, _EIA_MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code not in _EIA_RETRYABLE_STATUS:
                return resp
            if attempt >= _EIA_MAX_RETRIES:
                return resp

            retry_after = _parse_retry_after(resp)
            delay = (
                retry_after
                if retry_after is not None
                else _EIA_RETRY_BACKOFF_BASE * (2 ** attempt)
            )
            logger.warning(
                "EIA: HTTP %d for series %s (attempt %d/%d): %s"
                " — retrying in %.1fs",
                resp.status_code,
                series_id,
                attempt + 1,
                _EIA_MAX_RETRIES,
                _eia_error_message(resp),
                delay,
            )
            await asyncio.sleep(delay)
        return resp

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert one EIA series snapshot to a NormalizedEpisode.

        Returns ``None`` for periods outside the ``news_max_age_days``
        window.
        """
        series_id = str(record.get("series_id", ""))
        period = str(record.get("period", ""))
        value = str(record.get("value", ""))
        previous_value = record.get("previous_value")
        units = str(record.get("units", ""))
        name = str(record.get("name", series_id))
        topic = str(record.get("topic", series_id))
        context = str(record.get("context", "") or "")

        valid_at = _parse_period_date(period)
        if valid_at is None:
            logger.debug("EIA: invalid period, skipping: %r", record)
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(days=_EIA_MAX_AGE_DAYS)
        if valid_at < cutoff:
            logger.debug(
                "EIA: %s period older than %d days — skipping",
                series_id,
                _EIA_MAX_AGE_DAYS,
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

        severity = _map_eia_severity(series_id, value_f, previous_f)
        episode_body = _build_eia_body(
            series_id, name, period, value, previous_value, units,
            context=context or None,
        )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        ep_name = NormalizedEpisode.make_name(
            source_type="eia",
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
            source_description="EIA (US Energy Information Administration)",
            source_type="eia",
            # Unique per-series source URL (route + facets) so same-route
            # series survive URL-based dedup — see _build_eia_source_url().
            source_url=_build_eia_source_url(series_id),
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=["eia", series_id, topic.lower()],
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
    "EiaAdapter",
    "_EIA_SERIES",
    "_EIA_MAX_AGE_DAYS",
    "_EIA_FETCH_LENGTH",
    "_build_eia_source_url",
    "_eia_error_message",
    "_map_eia_severity",
    "_build_eia_body",
]
