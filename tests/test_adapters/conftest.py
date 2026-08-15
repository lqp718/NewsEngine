"""Shared fixtures for adapter unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from src.adapters.models import NormalizedEpisode


@pytest.fixture
def sample_gkg_record() -> dict[str, Any]:
    """A synthetic GKG V2 record for unit testing.

    Reflects the corrected column mapping (V2.3):
    - ``domain`` from CSV col 3 (added)
    - ``source_url`` from CSV col 4 (full URL, not domain)
    - ``themes`` from CSV col 7
    - ``locations`` from CSV col 9
    - ``persons`` from CSV col 11
    - ``organizations`` from CSV col 13
    - ``tone`` from CSV col 15 (comma-separated avg_tone,pos,neg,...)
    """
    return {
        "global_event_id": "1234567890",
        "valid_at": "20250609010101",
        "source_collection": "1",
        "domain": "example.com",
        "source_url": "http://example.com/article1",
        "language": "Eng",
        "themes": "ECON_FINANCIAL_MARKET; TAX_FNCACT_REG_INVEST",
        "locations": "#1#2#Beijing,Beijing,China#CN#CN|#1#2#Shanghai,Shanghai,China#CN#CN",
        "persons": "Xi Jinping; John Doe",
        "organizations": "Tencent; Alibaba",
        "tone": "-8.7,0.57,3.15,0.82,-1.25",
    }


@pytest.fixture
def sample_rss_entry() -> dict[str, Any]:
    """A synthetic RSS entry for unit testing."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "title": "Tencent Stock Rises on Strong Earnings",
        "link": "http://example.com/rss/tencent-earnings",
        "id": "guid-12345",
        "summary": "Tencent Holdings reported strong quarterly earnings...",
        "published": now.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "published_parsed": now.timetuple(),
        "updated_parsed": None,
        "authors": [{"name": "John Reporter"}],
        "feed_url": "http://example.com/rss",
    }


@pytest.fixture
def sample_akshare_item() -> dict[str, Any]:
    """A synthetic AkShare news item for unit testing."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return {
        "title": "腾讯控股股价创历史新高",
        "content": "腾讯控股今日股价突破500港元，创历史新高。",
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "东方财富",
        "symbol": "00700",
        "_ticker_name": "腾讯控股",
        "_ticker_full": "00700.HK",
    }


@pytest.fixture
def sample_treasury_record() -> dict[str, Any]:
    """A synthetic Treasury yield curve record for unit testing."""
    from datetime import datetime, timezone

    return {
        "fetch_time": datetime.now(timezone.utc),
        "term_rates": {
            "3mo": 4.52,
            "2yr": 4.35,
            "5yr": 4.15,
            "10yr": 4.28,
            "30yr": 4.55,
        },
        "raw_response": {},
    }


# ── Phase 1 macro adapter fixtures (add-phase1-macro-adapters) ────────
# Dates are computed relative to "now" so tests stay green regardless of
# when they run (adapters apply the news_max_age_days window cutoff).


def _recent_date_str(days_ago: int = 2) -> str:
    """Return a YYYY-MM-DD string a few days in the past (UTC)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%d"
    )


@pytest.fixture
def sample_fred_record() -> dict[str, Any]:
    """A synthetic FRED series snapshot for unit testing."""
    recent = _recent_date_str(2)
    return {
        "series_id": "GDP",
        "date": recent,
        "realtime_start": recent,
        "value": "29200.0",
        "previous_value": "29000.0",
        "units": "Bil. of $",
        "name": "Gross Domestic Product",
        "topic": "GDP Growth",
    }


@pytest.fixture
def sample_sanctions_record() -> dict[str, Any]:
    """A synthetic OFAC/OpenSanctions entry for unit testing."""
    return {
        "entity_name": "Example Corp",
        "target_type": "legalEntity",
        "country": "Russia",
        "sanction_program": "SDN",
        "listing_date": None,
        "source_url": "https://sanctionssearch.ofac.treas.gov/Details.aspx?id=1",
        "source": "ofac",
    }


@pytest.fixture
def sample_acled_record() -> dict[str, Any]:
    """A synthetic ACLED conflict event for unit testing."""
    return {
        "event_id_cnty": "UKR1234",
        "event_date": _recent_date_str(1),
        "event_type": "Battles",
        "country": "Ukraine",
        "admin1": "",
        "actor1": "Military Forces of Russia",
        "actor2": "Military Forces of Ukraine",
        "fatalities": 120,
        "notes": "Artillery exchange near the front line.",
        "latitude": "48.37",
        "longitude": "31.17",
    }


@pytest.fixture
def sample_eia_record() -> dict[str, Any]:
    """A synthetic EIA series snapshot for unit testing."""
    return {
        "series_id": "WCRSTUS1",
        "period": _recent_date_str(2),
        "value": "430500",
        "previous_value": "432000",
        "units": "Thousand Barrels",
        "name": "Weekly U.S. Crude Oil Ending Stocks",
        "topic": "Crude Oil Inventories",
    }


@pytest.fixture
def sample_bls_record() -> dict[str, Any]:
    """A synthetic BLS series snapshot for unit testing."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "series_id": "CES0000000001",
        "year": str(now.year),
        "period": f"M{now.month:02d}",
        "periodName": now.strftime("%B"),
        "value": "159000",
        "previous_value": "120000",
        "units": "Thousands",
        "name": "All Employees, Total Nonfarm (Nonfarm Payrolls)",
        "topic": "Nonfarm Payrolls",
        "context": "Nonfarm payrolls measure the number of employed people excluding farm workers, government employees, and nonprofit organization employees. Released monthly (first Friday). Key labor market indicator — strong numbers signal economic growth, weak numbers may signal recession.",
    }
