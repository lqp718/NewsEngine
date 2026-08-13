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
from src.adapters.fred_adapter import (
    FredAdapter,
    _FRED_SERIES,
    _map_fred_severity,
    _build_fred_body,
)
from src.adapters.sanctions_adapter import (
    SanctionsAdapter,
    _map_ofac_type,
    _map_sanctions_severity,
    _build_sanctions_body,
    _OFAC_SDN_CSV_URL,
    _OPEN_SANCTIONS_SEARCH_URL,
)
from src.adapters.acled_adapter import (
    AcledAdapter,
    _map_acled_severity,
    _build_acled_body,
    _ACLED_API_URL,
)
from src.adapters.eia_adapter import (
    EiaAdapter,
    _EIA_SERIES,
    _map_eia_severity,
    _build_eia_body,
)
from src.adapters.bls_adapter import (
    BlsAdapter,
    _BLS_SERIES,
    _parse_bls_period,
    _map_bls_severity,
    _build_bls_body,
)

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
    # ── Phase 1 macro adapters (add-phase1-macro-adapters) ──
    "FredAdapter",
    "_FRED_SERIES",
    "_map_fred_severity",
    "_build_fred_body",
    "SanctionsAdapter",
    "_map_ofac_type",
    "_map_sanctions_severity",
    "_build_sanctions_body",
    "_OFAC_SDN_CSV_URL",
    "_OPEN_SANCTIONS_SEARCH_URL",
    "AcledAdapter",
    "_map_acled_severity",
    "_build_acled_body",
    "_ACLED_API_URL",
    "EiaAdapter",
    "_EIA_SERIES",
    "_map_eia_severity",
    "_build_eia_body",
    "BlsAdapter",
    "_BLS_SERIES",
    "_parse_bls_period",
    "_map_bls_severity",
    "_build_bls_body",
]
