"""Treasury Adapter — fetch US Treasury yield curve data.

Phase 1: Skeleton implementation (stub fetch).
Phase 2+: Full production implementation with HTTP API integration.

Data source (Phase 2+): https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve&field_tdr_date_value=2025
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.adapters.base import BaseAdapter
from src.adapters.models import EntityItem, NormalizedEpisode, Severity
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

logger = get_logger(__name__)


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
            # Inversion: 2yr yield > 10yr yield → recession signal
            return "high"
        if spread > -0.25:
            # Flattening: spread is positive but less than 0.25
            # Actually spread here is negative since 10yr > 2yr in normal curve
            # Flattening = difference between 10yr and 2yr is very small
            # So abs(spread) < 0.25
            return "medium"

    # Default to low (normal curve)
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
        if spread_bp > 0:
            shape = "normal"
        elif spread_bp > -25:
            shape = "flat"
        else:
            shape = "inverted"
        lines.append(f"Spread (2s10s): {spread_bp:.1f} bp | Shape: {shape}")

    return "\n".join(lines)


class TreasuryAdapter(BaseAdapter):
    """US Treasury yield curve adapter.

    Phase 1: Skeleton — fetch() returns empty list.
    Phase 2+: Full HTTP fetch from Treasury API.
    """

    SOURCE_TYPE = "treasury"

    def __init__(
        self,
        api_url: str | None = None,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        # TODO Phase 2+: Configure real Treasury API URL
        self.api_url = api_url
        self._last_record: dict | None = None

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Fetch US Treasury yield curve data.

        Phase 1: Returns empty list (stub).
        Phase 2+: Will fetch from Treasury API.
        """
        # TODO Phase 2+: Implement real HTTP fetch from Treasury API
        logger.info(
            "TreasuryAdapter.fetch() — Phase 1 skeleton, returning empty list"
        )
        return []

    def _detect_inversion(self, term_rates: dict[str, float]) -> Severity:
        """Detect yield curve inversion. Delegates to module-level function."""
        return _detect_inversion(term_rates)

    def _build_yield_curve_body(
        self, term_rates: dict[str, float], date_str: str
    ) -> str:
        """Build yield curve body text. Delegates to module-level function."""
        return _build_yield_curve_body(term_rates, date_str)

    async def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a Treasury yield curve record to NormalizedEpisode.

        Args:
            record: Dict with keys 'fetch_time' (datetime), 'term_rates' (dict),
                   'raw_response' (optional).

        Returns:
            NormalizedEpisode with yield curve data.
        """
        fetch_time = record.get("fetch_time", now_hkt())
        date_str = fetch_time.strftime("%Y-%m-%d")
        term_rates: dict[str, float] = record.get("term_rates", {})

        severity = _detect_inversion(term_rates)
        episode_body = _build_yield_curve_body(term_rates, date_str)
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
            source_url=None,
            valid_at=fetch_time,
            content_hash=content_hash,
            severity=severity,
            keywords=["treasury", "yield curve", "UST"],
            entities=entities,
            metadata={"_structured": True},
        )
