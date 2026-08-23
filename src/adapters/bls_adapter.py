"""BLS Adapter — US Bureau of Labor Statistics data.

Data source: BLS Public API v2 (https://api.bls.gov/publicAPI/v2/timeseries/data/,
no API key required for public data).

Phase 1 (add-phase1-macro-adapters): fetch key labor series (nonfarm
payrolls / unemployment rate / average hourly earnings / CPI-U), one
snapshot episode per series per cycle.

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): httpx.AsyncClient POST; never raises — returns [] + warning
  on network/API failure
- normalize(): one NormalizedEpisode per series snapshot; date-window
  cutoff (derived from year + period) returns None
- severity: default ``medium`` (module-level `_map_bls_severity`)
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

_BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# BLS is monthly data with ~1 month publication lag.
# The global ``news_max_age_days`` (14) would filter out the latest
# observation past mid-month.  Use a source-specific 90-day window.
_BLS_MAX_AGE_DAYS = 90

# Default series (design.md): nonfarm payrolls / unemployment rate /
# average hourly earnings / CPI-U (all items).
_BLS_SERIES: dict[str, dict[str, str]] = {
    "CES0000000001": {
        "name": "All Employees, Total Nonfarm (Nonfarm Payrolls)",
        "units": "Thousands",
        "topic": "Nonfarm Payrolls",
        "context": "Nonfarm payrolls measure the number of employed people excluding farm workers, government employees, and nonprofit organization employees. Released monthly (first Friday). Key labor market indicator — strong numbers signal economic growth, weak numbers may signal recession.",
    },
    "LNS14000000": {
        "name": "Unemployment Rate",
        "units": "Percent",
        "topic": "Unemployment",
        "context": "Percentage of the labor force that is unemployed and actively seeking work. Lagging indicator — rises after recession ends, falls during recovery. Part of the Fed's dual mandate (maximum employment). Below 4% is historically tight.",
    },
    "CES0500000003": {
        "name": "Average Hourly Earnings of All Employees, Total Private",
        "units": "Dollars per Hour",
        "topic": "Wage Growth",
        "context": "Average hourly earnings for private-sector workers. Key wage inflation indicator — sustained wage growth above 3-4% may feed into consumer price inflation and prompt Fed tightening.",
    },
    "CUUR0000SA0": {
        "name": "Consumer Price Index for All Urban Consumers, All Items",
        "units": "Index 1982-1984=100",
        "topic": "Inflation",
        "context": "CPI tracks the average change in prices paid by urban consumers for a basket of goods and services. Primary inflation gauge for the Fed. Rising CPI may trigger rate hikes; falling CPI may signal easing.",
    },
}


# ── Module-level helper functions ──────────────────────────────────────


def _parse_bls_period(year: str, period: str) -> datetime | None:
    """Derive a UTC datetime from a BLS year + period.

    ``period`` is ``M01``..``M12`` (monthly) or ``M13`` (annual). Monthly
    series resolve to the first day of the reported month; annual series
    resolve to December 31 of the year.
    """
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return None
    if not period or not period.startswith("M"):
        return None
    try:
        month_int = int(period[1:])
    except ValueError:
        return None

    if month_int == 13:  # Annual
        return datetime(year_int, 12, 31, tzinfo=timezone.utc)
    if 1 <= month_int <= 12:
        return datetime(year_int, month_int, 1, tzinfo=timezone.utc)
    return None


def _bls_sort_key(item: dict) -> tuple[int, int]:
    """Sort key for BLS data items: (year, period month), descending."""
    try:
        year = int(item.get("year", 0))
    except (TypeError, ValueError):
        year = 0
    period = item.get("period", "")
    month = 0
    if period.startswith("M"):
        try:
            month = int(period[1:])
        except ValueError:
            month = 0
    return (year, month)


def _map_bls_severity(
    series_id: str,
    value: float | None,
    previous_value: float | None,
) -> Severity:
    """Map a BLS series snapshot to a severity level.

    Default ``medium`` (design.md ADR-5). Unemployment-rate or
    payroll changes beyond threshold are elevated to ``high``.
    """
    if value is None or previous_value is None:
        return "medium"
    try:
        change = float(value) - float(previous_value)
    except (TypeError, ValueError):
        return "medium"

    if series_id == "LNS14000000" and change >= 1.0:
        # >= 1pp unemployment jump is a strong recession signal
        return "high"
    if series_id == "CES0000000001" and change <= -500:
        # >= 500k monthly payroll loss is a strong signal
        return "high"
    return "medium"


def _build_bls_body(
    series_id: str,
    name: str,
    year: str,
    period: str,
    period_name: str,
    value: str,
    previous_value: str | None,
    units: str,
    context: str | None = None,
) -> str:
    """Build a structured Markdown episode body for one BLS snapshot."""
    lines = [f"## BLS: {name} ({series_id})", ""]
    lines.append(f"- Period: {period_name} {year}")
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


# ── Adapter ────────────────────────────────────────────────────────────


class BlsAdapter(BaseAdapter):
    """BLS (US Bureau of Labor Statistics) adapter.

    One snapshot episode per series per cycle. No API key required;
    degrades to ``[]`` + warning when the API is unreachable.
    """

    SOURCE_TYPE = "bls"

    def __init__(self, dedup_cache: set[str] | None = None) -> None:
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch the latest observations for the configured BLS series.

        POSTs ``{seriesid, startyear, endyear}`` (no registration key).
        The year window covers the last two calendar years to bound the
        response size while covering the ``news_max_age_days`` window.

        Returns:
            List of per-series snapshot records with keys ``series_id`` /
            ``year`` / ``period`` / ``periodName`` / ``value`` /
            ``previous_value`` / ``units`` / ``name`` / ``topic``.
            Empty list (with warning) on network/API failure.
        """
        settings = get_settings()
        records: list[dict] = []

        now = datetime.now(timezone.utc)
        start_year = str(now.year - 2)
        end_year = str(now.year)
        payload = {
            "seriesid": list(_BLS_SERIES.keys()),
            "startyear": start_year,
            "endyear": end_year,
        }

        try:
            async with httpx.AsyncClient(
                timeout=settings.bls_timeout_sec
            ) as client:
                resp = await client.post(_BLS_API_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("BLS fetch failed: %s", exc)
            return []

        if data.get("status") != "REQUEST_SUCCEEDED":
            logger.warning("BLS API error: %s", data.get("message"))
            return []

        results = data.get("Results") or {}
        for series in results.get("series") or []:
            series_id = series.get("seriesID", "")
            cfg = _BLS_SERIES.get(series_id, {})
            items = series.get("data") or []
            if not items:
                continue
            # Sort descending by (year, period) — latest first
            items_sorted = sorted(items, key=_bls_sort_key, reverse=True)
            latest = items_sorted[0]
            prev = (
                items_sorted[1] if len(items_sorted) > 1 else None
            )
            records.append(
                {
                    "series_id": series_id,
                    "year": latest.get("year", ""),
                    "period": latest.get("period", ""),
                    "periodName": latest.get("periodName", ""),
                    "value": latest.get("value", ""),
                    "previous_value": prev.get("value") if prev else None,
                    "units": cfg.get("units", ""),
                    "name": cfg.get("name", series_id),
                    "topic": cfg.get("topic", series_id),
                    "context": cfg.get("context", ""),
                }
            )

        self._pre_filter_count = len(records)
        logger.info("BLS: fetched %d series snapshots", len(records))
        return records

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert one BLS series snapshot to a NormalizedEpisode.

        Returns ``None`` for periods outside the ``news_max_age_days``
        window (derived from ``year`` + ``period``).
        """
        series_id = str(record.get("series_id", ""))
        year = str(record.get("year", ""))
        period = str(record.get("period", ""))
        period_name = str(record.get("periodName", "") or period)
        value = str(record.get("value", ""))
        previous_value = record.get("previous_value")
        units = str(record.get("units", ""))
        name = str(record.get("name", series_id))
        topic = str(record.get("topic", series_id))
        context = str(record.get("context", "") or "")

        valid_at = _parse_bls_period(year, period)
        if valid_at is None:
            logger.debug("BLS: invalid year/period, skipping: %r", record)
            return None

        settings = get_settings()
        cutoff = datetime.now(timezone.utc) - timedelta(days=_BLS_MAX_AGE_DAYS)
        if valid_at < cutoff:
            logger.debug(
                "BLS: %s period older than %d days — skipping",
                series_id,
                _BLS_MAX_AGE_DAYS,
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

        severity = _map_bls_severity(series_id, value_f, previous_f)
        episode_body = _build_bls_body(
            series_id,
            name,
            year,
            period,
            period_name,
            value,
            previous_value,
            units,
            context=context or None,
        )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        ep_name = NormalizedEpisode.make_name(
            source_type="bls",
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
            source_description="BLS (US Bureau of Labor Statistics)",
            source_type="bls",
            source_url=f"https://data.bls.gov/timeseries/{series_id}",
            valid_at=valid_at,
            content_hash=content_hash,
            severity=severity,
            keywords=["bls", series_id, topic.lower()],
            entities=entities,
            metadata={
                "_structured": True,
                "content_scope": "MACRO",
                "series_id": series_id,
                "value": value,
                "previous_value": previous_value,
                "period_name": period_name,
            },
        )


__all__ = [
    "BlsAdapter",
    "_BLS_SERIES",
    "_parse_bls_period",
    "_map_bls_severity",
    "_build_bls_body",
]
