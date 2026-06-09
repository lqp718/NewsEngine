"""Shared fixtures for adapter unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from src.adapters.models import NormalizedEpisode


@pytest.fixture
def sample_gkg_record() -> dict[str, Any]:
    """A synthetic GKG V2 record for unit testing."""
    return {
        "global_event_id": "1234567890",
        "valid_at": "20250609010101",
        "source_collection": "1",
        "source_url": "http://example.com/article1",
        "language": "Eng",
        "persons": "Xi Jinping; John Doe",
        "organizations": "Tencent; Alibaba",
        "locations": "#1#2#Beijing,Beijing,China#CN#CN|#1#2#Shanghai,Shanghai,China#CN#CN",
        "themes": "ECON_FINANCIAL_MARKET; TAX_FNCACT_REG_INVEST",
        "tone": "-8.7",
    }


@pytest.fixture
def sample_rss_entry() -> dict[str, Any]:
    """A synthetic RSS entry for unit testing."""
    from time import struct_time
    import time

    now = time.gmtime()
    return {
        "title": "Tencent Stock Rises on Strong Earnings",
        "link": "http://example.com/rss/tencent-earnings",
        "id": "guid-12345",
        "summary": "Tencent Holdings reported strong quarterly earnings...",
        "published": "Mon, 09 Jun 2025 01:00:00 GMT",
        "published_parsed": struct_time((2025, 6, 9, 1, 0, 0, 0, 0, 0)),
        "updated_parsed": None,
        "authors": [{"name": "John Reporter"}],
        "feed_url": "http://example.com/rss",
    }


@pytest.fixture
def sample_akshare_item() -> dict[str, Any]:
    """A synthetic AkShare news item for unit testing."""
    return {
        "title": "腾讯控股股价创历史新高",
        "content": "腾讯控股今日股价突破500港元，创历史新高。",
        "time": "2025-06-09 10:30:00",
        "source": "东方财富",
        "symbol": "00700",
        "_ticker_name": "腾讯控股",
        "_ticker_full": "0700.HK",
    }


@pytest.fixture
def sample_treasury_record() -> dict[str, Any]:
    """A synthetic Treasury yield curve record for unit testing."""
    from datetime import datetime, timezone

    return {
        "fetch_time": datetime(2025, 6, 9, 12, 0, 0, tzinfo=timezone.utc),
        "term_rates": {
            "3mo": 4.52,
            "2yr": 4.35,
            "5yr": 4.15,
            "10yr": 4.28,
            "30yr": 4.55,
        },
        "raw_response": {},
    }
