"""China Macro Adapter — fetch Chinese macroeconomic data via AKShare.

Data source: AKShare library (https://akshare.akfamily.xyz/)
- GDP: macro_china_gdp() — quarterly
- CPI: macro_china_cpi() — monthly
- PPI: macro_china_ppi_yearly() — monthly (yearly YoY report)
- PMI (official manufacturing): macro_china_pmi() — monthly
- PMI (Caixin manufacturing): macro_china_cx_pmi_yearly() — monthly

All data is free, no API key required. AKShare wraps data from:
- 国家统计局 (National Bureau of Statistics)
- 金十数据 (Jin10)
- 东方财富 (EastMoney)

Contract: BaseAdapter (fetch → normalize → dedup).
- fetch(): calls AKShare APIs; returns [] on any error
- normalize(): one NormalizedEpisode per indicator snapshot; date-window
  cutoff returns None for stale data
- severity: module-level `_map_china_macro_severity` (default medium,
  threshold helpers for CPI/PPI/PMI extremes)
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

import akshare as ak

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

# Max age for considering data "fresh"
# - GDP: quarterly, 90 days
# - CPI/PPI/PMI: monthly, 60 days
_CHINA_MACRO_MAX_AGE_DAYS = {
    "gdp": 120,
    "cpi": 90,
    "ppi": 90,
    "pmi": 90,
    "caixin_pmi": 90,
}

# Indicator metadata: (display_name, unit, frequency)
_INDICATOR_META = {
    "gdp": ("GDP", "亿元", "quarterly"),
    "cpi": ("CPI", "%", "monthly"),
    "ppi": ("PPI", "%", "monthly"),
    "pmi": ("PMI (Official Manufacturing)", "", "monthly"),
    "caixin_pmi": ("PMI (Caixin Manufacturing)", "", "monthly"),
}

# Codebook: indicator name/unit/frequency/description translations
# (lazy loaded, see _load_codebook)
_CODEBOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "codebooks" / "china_macro_indicators.json"
)
_china_macro_lock = Lock()


# ── Module-level helper functions ──────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_codebook() -> dict[str, Any]:
    """Load the China macro codebook from disk (lazy, cached, thread-safe).

    Returns ``{}`` if the file is missing or unparseable (fail-open) so
    callers fall back to the Markdown body format.
    """
    with _china_macro_lock:
        if not _CODEBOOK_PATH.exists():
            return {}
        try:
            with open(_CODEBOOK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("China macro: codebook load failed (%s), using Markdown fallback", exc)
            return {}


def _build_narrative(
    indicator: str, value: float, date_str: str
) -> str | None:
    """Build a natural-language narrative for one indicator using the codebook.

    Format: ``中国{name}，{date}：{value}。{description}``. Returns ``None``
    when the codebook is unavailable or does not define ``indicator`` — the
    caller falls back to the Markdown body.

    Note: AKShare's GDP record stores YoY growth in ``value`` (the absolute
    value in 亿元 is not carried through normalize), so GDP is rendered as a
    growth rate regardless of the codebook unit.
    """
    codebook = _load_codebook()
    meta = codebook.get("indicators", {}).get(indicator)
    if not meta:
        return None

    name = meta.get("name") or indicator
    unit = meta.get("unit", "")
    description = meta.get("description", "")

    if indicator == "gdp":
        value_text = f"同比增长{value:.2f}%"
    elif unit == "%":
        value_text = f"{value:.2f}%"
    elif unit:
        value_text = f"{value:.2f}{unit}"
    else:
        value_text = f"{value:.2f}"

    body = f"中国{name}，{date_str}：{value_text}。"
    if description:
        body += description if description.endswith("。") else description + "。"
    return body


def _map_china_macro_severity(indicator: str, value: float) -> Severity:
    """Map indicator value to severity.

    Args:
        indicator: One of "cpi", "ppi", "pmi", "caixin_pmi"
        value: The indicator value

    Returns:
        Severity level based on economic significance
    """
    if indicator == "cpi":
        # CPI YoY: high inflation (>3%) or deflation (<0%) = high
        if value > 3.0 or value < 0:
            return "high"
        if value > 2.0 or value < 0.5:
            return "medium"
        return "low"

    if indicator == "ppi":
        # PPI YoY: large swings are significant
        if abs(value) > 5.0:
            return "high"
        if abs(value) > 2.0:
            return "medium"
        return "low"

    if indicator in ("pmi", "caixin_pmi"):
        # PMI: >50 expansion, <50 contraction
        # Extreme readings are significant
        if value > 52.0 or value < 48.0:
            return "high"
        if value > 51.0 or value < 49.0:
            return "medium"
        return "low"

    return "medium"


def _parse_china_date(date_str: str) -> datetime | None:
    """Parse date from AKShare output.

    AKShare returns dates in various formats:
    - "2024年03月份" (monthly)
    - "2024年第1季度" (quarterly)
    - "2024-03-09" (daily)
    """
    if not date_str:
        return None

    date_str = date_str.strip()

    # Try "YYYY年MM月份" format
    if "年" in date_str and "月份" in date_str:
        try:
            year = int(date_str.split("年")[0])
            month = int(date_str.split("年")[1].split("月份")[0])
            return datetime(year, month, 1, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            pass

    # Try "YYYY年第N季度" format
    if "年" in date_str and "季度" in date_str:
        try:
            year = int(date_str.split("年")[0])
            quarter_str = date_str.split("年第")[1].split("季度")[0]
            # Map quarter to month
            quarter_month_map = {
                "1": 3, "2": 6, "3": 9, "4": 12,
                "1-2": 6, "1-3": 9,
            }
            month = quarter_month_map.get(quarter_str, 12)
            return datetime(year, month, 1, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            pass

    # Try ISO format "YYYY-MM-DD"
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    return None


# ── AKShare fetch functions ────────────────────────────────────────────────


def _fetch_gdp() -> list[dict]:
    """Fetch GDP data from AKShare."""
    try:
        df = ak.macro_china_gdp()
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.head(8).iterrows():  # Last 8 quarters
            date_str = str(row.get("季度", ""))
            fetch_time = _parse_china_date(date_str)
            if fetch_time is None:
                continue

            gdp_value = row.get("国内生产总值-绝对值")
            gdp_yoy = row.get("国内生产总值-同比增长")

            records.append({
                "fetch_time": fetch_time,
                "indicator": "gdp",
                "value": float(gdp_yoy) if gdp_yoy else None,
                "raw_value": float(gdp_value) if gdp_value else None,
                "date_str": date_str,
            })
        return records
    except Exception as exc:
        logger.warning("China macro GDP fetch failed: %s", exc)
        return []


def _fetch_cpi() -> list[dict]:
    """Fetch CPI data from AKShare."""
    try:
        df = ak.macro_china_cpi()
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.head(12).iterrows():  # Last 12 months
            date_str = str(row.get("月份", ""))
            fetch_time = _parse_china_date(date_str)
            if fetch_time is None:
                continue

            cpi_yoy = row.get("全国-同比增长")

            records.append({
                "fetch_time": fetch_time,
                "indicator": "cpi",
                "value": float(cpi_yoy) if cpi_yoy else None,
                "date_str": date_str,
            })
        return records
    except Exception as exc:
        logger.warning("China macro CPI fetch failed: %s", exc)
        return []


def _fetch_ppi() -> list[dict]:
    """Fetch PPI data from AKShare."""
    try:
        df = ak.macro_china_ppi_yearly()
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.tail(12).iterrows():  # Last 12 months (newest)
            date_str = str(row.get("日期", ""))
            fetch_time = _parse_china_date(date_str)
            if fetch_time is None:
                continue

            ppi_value = row.get("今值")

            records.append({
                "fetch_time": fetch_time,
                "indicator": "ppi",
                "value": float(ppi_value) if ppi_value else None,
                "date_str": date_str,
            })
        return records
    except Exception as exc:
        logger.warning("China macro PPI fetch failed: %s", exc)
        return []


def _fetch_pmi() -> list[dict]:
    """Fetch official manufacturing PMI data from AKShare."""
    try:
        df = ak.macro_china_pmi()
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.head(12).iterrows():  # Last 12 months
            date_str = str(row.get("月份", ""))
            fetch_time = _parse_china_date(date_str)
            if fetch_time is None:
                continue

            pmi_value = row.get("制造业-指数")

            records.append({
                "fetch_time": fetch_time,
                "indicator": "pmi",
                "value": float(pmi_value) if pmi_value else None,
                "date_str": date_str,
            })
        return records
    except Exception as exc:
        logger.warning("China macro PMI fetch failed: %s", exc)
        return []


def _fetch_caixin_pmi() -> list[dict]:
    """Fetch Caixin manufacturing PMI data from AKShare."""
    try:
        df = ak.macro_china_cx_pmi_yearly()
        if df is None or df.empty:
            return []

        records = []
        for _, row in df.tail(12).iterrows():  # Last 12 months (newest)
            date_str = str(row.get("日期", ""))
            fetch_time = _parse_china_date(date_str)
            if fetch_time is None:
                continue

            pmi_value = row.get("今值")

            records.append({
                "fetch_time": fetch_time,
                "indicator": "caixin_pmi",
                "value": float(pmi_value) if pmi_value else None,
                "date_str": date_str,
            })
        return records
    except Exception as exc:
        logger.warning("China macro Caixin PMI fetch failed: %s", exc)
        return []


# ── Adapter ────────────────────────────────────────────────────────────────


class ChinaMacroAdapter(BaseAdapter):
    """Chinese macroeconomic data adapter.

    Fetches key macro indicators from AKShare:
    - GDP (quarterly)
    - CPI (monthly)
    - PPI (monthly)
    - PMI (official manufacturing, monthly)
    - PMI (Caixin manufacturing, monthly)

    Produces one episode per indicator per reporting period.
    """

    SOURCE_TYPE = "china_macro"

    def __init__(
        self,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch Chinese macroeconomic data from AKShare.

        Returns:
            List of record dicts with keys:
            - fetch_time (datetime): reporting date
            - indicator (str): "gdp", "cpi", "ppi", "pmi", "caixin_pmi"
            - value (float): indicator value
            - date_str (str): original date string
        """
        logger.info("China macro: fetching from AKShare")

        all_records: list[dict] = []

        # Fetch each indicator sequentially (AKShare has rate limits)
        all_records.extend(_fetch_gdp())
        all_records.extend(_fetch_cpi())
        all_records.extend(_fetch_ppi())
        all_records.extend(_fetch_pmi())
        all_records.extend(_fetch_caixin_pmi())

        if not all_records:
            logger.warning("China macro: no records fetched from any indicator")
            return []

        # Apply max-age filter per indicator
        now = datetime.now(timezone.utc)
        recent: list[dict] = []
        by_indicator: dict[str, list[dict]] = {}
        for r in all_records:
            ind = r.get("indicator", "unknown")
            by_indicator.setdefault(ind, []).append(r)

        for ind, ind_records in by_indicator.items():
            ind_records.sort(key=lambda x: x["fetch_time"], reverse=True)
            max_age = _CHINA_MACRO_MAX_AGE_DAYS.get(ind, 90)
            cutoff = now - timedelta(days=max_age)
            fresh = [r for r in ind_records if r["fetch_time"] >= cutoff]
            if fresh:
                recent.extend(fresh)
            else:
                # No fresh data for this indicator, keep the most recent one
                logger.info(
                    "China macro: %s has no data within %d days, using most recent (%s)",
                    ind,
                    max_age,
                    ind_records[0]["date_str"] if ind_records else "N/A",
                )
                recent.append(ind_records[0])

        self._pre_filter_count = len(recent)
        logger.info("China macro: fetched %d records across all indicators", len(recent))
        return recent

    def _load_codebook(self) -> dict[str, Any]:
        """Load the China macro indicators codebook (lazy, thread-safe)."""
        return _load_codebook()

    def _build_narrative(
        self, indicator: str, value: float, date_str: str
    ) -> str | None:
        """Build natural-language narrative. Delegates to module-level function."""
        return _build_narrative(indicator, value, date_str)

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert a China macro record to NormalizedEpisode.

        Args:
            record: Dict with keys 'fetch_time', 'indicator', 'value', 'date_str'

        Returns:
            NormalizedEpisode with macro data, or None if invalid.
        """
        fetch_time = record.get("fetch_time")
        if fetch_time is None:
            logger.debug("China macro: record missing fetch_time, skipping")
            return None

        indicator = record.get("indicator", "")
        value = record.get("value")
        date_str = record.get("date_str", "")

        if value is None or (isinstance(value, float) and math.isnan(value)):
            logger.debug("China macro: no value for %s on %s, skipping", indicator, date_str)
            return None

        # Get indicator metadata
        display_name, unit, frequency = _INDICATOR_META.get(
            indicator, (indicator, "", "unknown")
        )

        severity = _map_china_macro_severity(indicator, value)

        # Build episode body: codebook narrative preferred, Markdown fallback.
        episode_body = self._build_narrative(indicator, value, date_str)
        if episode_body is None:
            # Codebook unavailable or indicator undefined → fall back to the
            # original Markdown body format.
            episode_body = self._build_episode_body(
                indicator, display_name, value, unit, date_str, frequency
            )
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        name = NormalizedEpisode.make_name(
            source_type="china_macro",
            valid_at=fetch_time,
            content_hash=content_hash,
            group_id=indicator.upper(),
        )

        entities = [
            EntityItem(type="country", name="China"),
        ]

        # Use unique source_url per indicator/date to avoid dedup collision
        # (all China macro data comes from AKShare but each indicator is distinct)
        unique_url = f"https://akshare.akfamily.xyz/china_macro/{indicator}/{date_str}"

        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description=f"China {display_name}",
            source_type="china_macro",
            source_url=unique_url,
            valid_at=fetch_time,
            content_hash=content_hash,
            severity=severity,
            keywords=["china", "macro", indicator, "economic data"],
            entities=entities,
            metadata={"_structured": True, "indicator": indicator},
        )

    def _build_episode_body(
        self,
        indicator: str,
        display_name: str,
        value: float,
        unit: str,
        date_str: str,
        frequency: str,
    ) -> str:
        """Build Markdown-formatted episode body for a macro indicator."""
        lines = [f"## China {display_name} — {date_str}\n"]

        if unit:
            lines.append(f"- Value: {value:.2f} {unit}")
        else:
            lines.append(f"- Value: {value:.2f}")

        lines.append(f"- Frequency: {frequency}")
        lines.append("")

        # Add interpretation
        if indicator == "pmi" or indicator == "caixin_pmi":
            if value > 50:
                lines.append(f"**Interpretation**: Expansion (above 50 threshold)")
            else:
                lines.append(f"**Interpretation**: Contraction (below 50 threshold)")
        elif indicator == "cpi":
            if value > 3:
                lines.append(f"**Interpretation**: High inflation (above 3% target)")
            elif value < 0:
                lines.append(f"**Interpretation**: Deflation (negative CPI)")
            else:
                lines.append(f"**Interpretation**: Moderate inflation")
        elif indicator == "ppi":
            if value > 0:
                lines.append(f"**Interpretation**: Producer price inflation")
            else:
                lines.append(f"**Interpretation**: Producer price deflation")

        return "\n".join(lines)


__all__ = [
    "ChinaMacroAdapter",
    "_map_china_macro_severity",
    "_parse_china_date",
    "_build_narrative",
    "_load_codebook",
]
