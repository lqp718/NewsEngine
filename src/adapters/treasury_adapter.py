"""Treasury Adapter — fetch US Treasury yield curve data.

Data source: Daily Treasury Par Yield Curve Rates (CSV download)
https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve

CSV URL pattern:
https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YEAR}/all?type=daily_treasury_yield_curve&field_tdr_date_value={YEAR}&page=&year=&yearclass={YEAR}

Key signals:
- 2Y-10Y inversion (2Y > 10Y) = classic recession warning (severity=high)
- 2Y-10Y flattening (spread < 0.25) = slowing cycle (severity=medium)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

# CSV download URL template — {year} is replaced with current year
_TREASURY_CSV_URL_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}"
    "&page=&year=&yearclass={year}"
)

# Column name mapping: CSV header → our internal key
_COLUMN_MAP = {
    "1 Mo": "1mo",
    "1.5 Month": "1.5mo",
    "2 Mo": "2mo",
    "3 Mo": "3mo",
    "4 Mo": "4mo",
    "6 Mo": "6mo",
    "1 Yr": "1yr",
    "2 Yr": "2yr",
    "3 Yr": "3yr",
    "5 Yr": "5yr",
    "7 Yr": "7yr",
    "10 Yr": "10yr",
    "20 Yr": "20yr",
    "30 Yr": "30yr",
}

# Max age for considering data "fresh" (daily data, 7 days covers weekends/holidays)
_TREASURY_MAX_AGE_DAYS = 7

# HTTP timeout
_TREASURY_TIMEOUT_SEC = 30

# Codebook: indicator name translations (lazy loaded, see _load_codebook)
_CODEBOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "codebooks" / "treasury_indicators.json"
)
_treasury_lock = Lock()

# Yield curve shape classification thresholds (2s10s spread, in bp)
_SPREAD_NORMAL_THRESHOLD = 0      # bp: spread above this → normal
_SPREAD_FLAT_THRESHOLD = -25      # bp: spread below this → inverted

# Yield curve shape → natural-language description (threshold logic lives in
# code, not the codebook — codebook only defines what indicators are).
_SHAPE_NARRATIVE = {
    "normal": "收益率曲线形态正常，无倒挂风险信号。",
    "flat": "收益率曲线形态趋平，接近倒挂区间。",
    "inverted": "收益率曲线形态倒挂，倒挂是衰退预警信号。",
}


# ── Module-level helper functions ──────────────────────────────────────────


@lru_cache(maxsize=1)
def _load_codebook() -> dict[str, Any]:
    """Load the Treasury indicators codebook from disk (lazy, cached, thread-safe).

    Returns ``{}`` if the file is missing or unparseable (fail-open) so
    callers fall back to the Markdown body format.
    """
    with _treasury_lock:
        if not _CODEBOOK_PATH.exists():
            return {}
        try:
            with open(_CODEBOOK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Treasury: codebook load failed (%s), using Markdown fallback", exc)
            return {}


def _classify_shape(term_rates: dict[str, float]) -> str:
    """Classify yield curve shape from 2s10s geometry.

    Mirrors the thresholds used by ``_build_yield_curve_body`` so the
    narrative and the fallback body never disagree.
    """
    if "2yr" in term_rates and "10yr" in term_rates:
        spread_bp = (term_rates["10yr"] - term_rates["2yr"]) * 100
        if spread_bp > _SPREAD_NORMAL_THRESHOLD:
            return "normal"
        if spread_bp > _SPREAD_FLAT_THRESHOLD:
            return "flat"
    return "inverted"


def _build_narrative(term_rates: dict[str, float], date_str: str) -> str:
    """Build a natural-language narrative for the yield curve.

    Translates each maturity via the codebook (skipping terms the codebook
    does not define, e.g. ``1.5mo``), appends the 2s10s spread and a shape
    sentence. The codebook name ends with "国债收益率", which is redundant
    after the "美国国债收益率曲线" header, so it is stripped per item.

    Requires the codebook; callers must fall back to
    ``_build_yield_curve_body`` when ``_load_codebook()`` returns {}.
    """
    codebook = _load_codebook()
    indicators = codebook.get("indicators", {})

    items = []
    for key, name in indicators.items():  # dict order: 1mo → 30yr
        if key not in term_rates:
            continue
        # Core name = part before the explanatory clause, e.g.
        # "2年期国债收益率，反映市场对短期利率的预期" → "2年期国债收益率".
        # The trailing "国债收益率" is redundant after the header, so strip it:
        # "2年期国债收益率" → "2年期".
        core = name.split("，")[0]
        short = core[:-5] if core.endswith("国债收益率") else core
        items.append(f"{short}{term_rates[key]:.2f}%")

    parts = [f"美国国债收益率曲线（{date_str}）：" + "，".join(items) + "。"]

    if "2yr" in term_rates and "10yr" in term_rates:
        spread_bp = (term_rates["10yr"] - term_rates["2yr"]) * 100
        metric_name = codebook.get("metrics", {}).get("2s10s_spread", "2s10s利差")
        metric_short = metric_name.split("（")[0].split("，")[0]
        sign = "+" if spread_bp > 0 else ""
        shape = _classify_shape(term_rates)
        parts.append(f"{metric_short}{sign}{spread_bp:.0f}bp，{_SHAPE_NARRATIVE[shape]}")

    return "".join(parts)


def _detect_inversion(term_rates: dict[str, float]) -> Severity:
    """Detect yield curve shape from term rates.

    Args:
        term_rates: Dict mapping term label → rate (e.g. {"2yr": 4.5, "10yr": 4.3}).

    Returns:
        "high" — inverted (2yr > 10yr)
        "medium" — flattening (2s10s spread < 0.25)
        "low" — normal (monotonically increasing)
    """
    if "2yr" in term_rates and "10yr" in term_rates:
        spread = term_rates["2yr"] - term_rates["10yr"]
        if spread > 0:
            return "high"
        if spread > -0.25:
            return "medium"
    return "low"


def _build_yield_curve_body(term_rates: dict[str, float], date_str: str) -> str:
    """Build Markdown-formatted yield curve description."""
    lines = [f"## US Treasury Yield Curve — {date_str}\n"]
    for term in ["3mo", "6mo", "1yr", "2yr", "3yr", "5yr", "7yr", "10yr", "20yr", "30yr"]:
        if term in term_rates:
            lines.append(f"- {term.capitalize()}: {term_rates[term]:.2f}%")

    lines.append("")

    if "2yr" in term_rates and "10yr" in term_rates:
        spread_bp = (term_rates["10yr"] - term_rates["2yr"]) * 100
        if spread_bp > _SPREAD_NORMAL_THRESHOLD:
            shape = "normal"
        elif spread_bp > _SPREAD_FLAT_THRESHOLD:
            shape = "flat"
        else:
            shape = "inverted"
        lines.append(f"Spread (2s10s): {spread_bp:.1f} bp | Shape: {shape}")

    return "\n".join(lines)


def _parse_rate(value_text: str | None) -> float | None:
    """Parse a rate value from CSV. Returns None for missing/N/A values."""
    if value_text is None:
        return None
    v = value_text.strip()
    if not v or v.upper() in ("N/A", "NA", ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_csv(csv_text: str) -> list[dict]:
    """Parse the Treasury CSV into a list of record dicts.

    CSV format:
    Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
    08/17/2026,3.79,3.80,3.82,3.87,3.89,3.95,4.00,4.19,4.25,4.38,4.54,4.72,5.30,5.31

    Returns records sorted by date descending (most recent first).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    records: list[dict] = []

    for row in reader:
        date_str = row.get("Date", "").strip()
        if not date_str:
            continue

        # Parse date: MM/DD/YYYY
        try:
            fetch_time = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            logger.debug("Treasury: unparseable date: %s", date_str)
            continue

        # Extract rates
        term_rates: dict[str, float] = {}
        for csv_col, rate_key in _COLUMN_MAP.items():
            rate = _parse_rate(row.get(csv_col))
            if rate is not None:
                term_rates[rate_key] = rate

        if not term_rates:
            logger.debug("Treasury: no rates found for %s", date_str)
            continue

        records.append({
            "fetch_time": fetch_time,
            "term_rates": term_rates,
        })

    # Sort by date descending
    records.sort(key=lambda r: r["fetch_time"], reverse=True)
    return records


# ── Adapter ────────────────────────────────────────────────────────────────


class TreasuryAdapter(BaseAdapter):
    """US Treasury yield curve adapter.

    Fetches daily yield curve data from the Treasury CSV download.
    Produces one episode per trading day with the full yield curve
    and inversion detection.
    """

    SOURCE_TYPE = "treasury"

    def __init__(
        self,
        api_url: str | None = None,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self.api_url = api_url  # If provided, use directly (for testing)

    def _get_csv_url(self) -> str:
        """Get the CSV URL for the current year."""
        if self.api_url:
            return self.api_url
        year = datetime.now(timezone.utc).year
        return _TREASURY_CSV_URL_TEMPLATE.format(year=year)

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch US Treasury yield curve data from the CSV download.

        Returns:
            List of record dicts with keys:
            - fetch_time (datetime): trading date
            - term_rates (dict[str, float]): e.g. {"2yr": 4.35, "10yr": 4.28}
        """
        url = self._get_csv_url()
        logger.info("Treasury: fetching from %s", url)

        try:
            async with httpx.AsyncClient(timeout=_TREASURY_TIMEOUT_SEC) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Treasury fetch failed: %s", exc)
            return []

        csv_text = resp.text
        if not csv_text or not csv_text.strip():
            logger.warning("Treasury: empty response body")
            return []

        records = _parse_csv(csv_text)
        if not records:
            logger.warning("Treasury: no records parsed from CSV")
            return []

        # Apply max-age filter (keep only recent data)
        cutoff = datetime.now(timezone.utc) - timedelta(days=_TREASURY_MAX_AGE_DAYS)
        recent = [r for r in records if r["fetch_time"] >= cutoff]

        if not recent:
            logger.info(
                "Treasury: all %d records older than %d days, using most recent",
                len(records),
                _TREASURY_MAX_AGE_DAYS,
            )
            # Return at least the most recent entry even if slightly old
            recent = records[:1]

        self._pre_filter_count = len(recent)
        logger.info(
            "Treasury: fetched %d records (most recent: %s)",
            len(recent),
            recent[0]["fetch_time"].strftime("%Y-%m-%d") if recent else "N/A",
        )
        return recent

    def _detect_inversion(self, term_rates: dict[str, float]) -> Severity:
        """Detect yield curve inversion. Delegates to module-level function."""
        return _detect_inversion(term_rates)

    def _load_codebook(self) -> dict[str, Any]:
        """Load the Treasury indicators codebook (lazy, thread-safe)."""
        return _load_codebook()

    def _build_narrative(self, term_rates: dict[str, float], date_str: str) -> str:
        """Build natural-language narrative. Delegates to module-level function."""
        return _build_narrative(term_rates, date_str)

    def _build_yield_curve_body(
        self, term_rates: dict[str, float], date_str: str
    ) -> str:
        """Build yield curve body text. Delegates to module-level function."""
        return _build_yield_curve_body(term_rates, date_str)

    async def normalize(self, record: dict) -> NormalizedEpisode | None:
        """Convert a Treasury yield curve record to NormalizedEpisode.

        Args:
            record: Dict with keys 'fetch_time' (datetime), 'term_rates' (dict).

        Returns:
            NormalizedEpisode with yield curve data, or None if invalid.
        """
        fetch_time = record.get("fetch_time")
        if fetch_time is None:
            logger.debug("Treasury: record missing fetch_time, skipping")
            return None

        date_str = fetch_time.strftime("%Y-%m-%d")
        term_rates: dict[str, float] = record.get("term_rates", {})

        if not term_rates:
            logger.debug("Treasury: no term rates for %s, skipping", date_str)
            return None

        severity = _detect_inversion(term_rates)
        if self._load_codebook():
            episode_body = self._build_narrative(term_rates, date_str)
        else:
            # Codebook unavailable → fall back to the Markdown list format.
            episode_body = self._build_yield_curve_body(term_rates, date_str)
        content_hash = hashlib.sha256(episode_body.encode("utf-8")).hexdigest()

        name = NormalizedEpisode.make_name(
            source_type="treasury",
            valid_at=fetch_time,
            content_hash=content_hash,
            group_id="YC",
        )

        entities = [
            EntityItem(type="country", name="United States"),
        ]

        return NormalizedEpisode(
            episode_body=episode_body,
            name=name,
            source_description="US Treasury Yield Curve",
            source_type="treasury",
            source_url="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve",
            valid_at=fetch_time,
            content_hash=content_hash,
            severity=severity,
            keywords=["treasury", "yield curve", "UST", "interest rates"],
            entities=entities,
            metadata={"_structured": True, "content_scope": "MACRO"},
        )


__all__ = [
    "TreasuryAdapter",
    "_detect_inversion",
    "_build_yield_curve_body",
    "_build_narrative",
    "_classify_shape",
    "_load_codebook",
    "_parse_csv",
    "_parse_rate",
]
