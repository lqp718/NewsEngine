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
from src.adapters.gdelt_codebook import translate_cameo, translate_actor, translate_theme
from src.adapters.gdelt_events_parser import (
    EventRecord,
    load_events_csv,
    fetch_events_csv,
    parse_event_record,
    parse_events,
    parse_events_file,
)
from src.adapters.gdelt_mentions_parser import (
    MentionRecord,
    fetch_mentions_csv,
    parse_mentions,
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
    "translate_cameo",
    "translate_actor",
    "translate_theme",
    "EventRecord",
    "load_events_csv",
    "fetch_events_csv",
    "parse_event_record",
    "parse_events",
    "parse_events_file",
    "MentionRecord",
    "fetch_mentions_csv",
    "parse_mentions",
    "RssAdapter",
    "AkShareAdapter",
    "TreasuryAdapter",
    "_detect_inversion",
]
