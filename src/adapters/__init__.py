"""NewsEngine data-source adapters.

Each adapter converts raw data from a specific source into the unified
NormalizedEpisode format.
"""

from src.adapters.base import BaseAdapter
from src.adapters.models import (
    EntityItem,
    NormalizedEpisode,
    Severity,
    SourceType,
    build_entity_suffix,
)
from src.adapters.gdelt_adapter import (
    GdeltAdapter,
    GdeltDownloadError,
    GdeltFetchError,
    _map_tone_to_severity,
    _parse_location,
)
from src.adapters.rss_adapter import RssAdapter
from src.adapters.akshare_adapter import AkShareAdapter
from src.adapters.treasury_adapter import TreasuryAdapter, _detect_inversion

__all__ = [
    "BaseAdapter",
    "NormalizedEpisode",
    "EntityItem",
    "Severity",
    "SourceType",
    "build_entity_suffix",
    "GdeltAdapter",
    "GdeltDownloadError",
    "GdeltFetchError",
    "_map_tone_to_severity",
    "_parse_location",
    "RssAdapter",
    "AkShareAdapter",
    "TreasuryAdapter",
    "_detect_inversion",
]
