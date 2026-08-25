"""GDELT CSV Adapter — fetch, parse, and normalize GDELT GKG V2 data.

Data flow::
    fetch_lastupdate() → download_all_csvs() → parse_gkg() + parse_events + parse_mentions
    → [Events-first] merge_event_data() → EventsPipelineFilter.filter() → _normalize_event_record()
    → [GKG fallback] parse_gkg() → filter_relevant() → normalize()
    → dedup()

Only the HTTP CSV data plane (http://data.gdeltproject.org/) is used.
HTTPS is avoided because the HTTPS endpoints are blocked by the GFW.

V2.2 → G5: Refactored for triple CSV source integration.
V2.3 → GKG pipeline fix: GKG passthrough merge, Events/Mentions preserved.
V3.0 → Events-first architecture (V3.0):
- Events CSV is the primary data source
- Three-stage filter (CAMEO, Goldstein, NumMentions) with JSON config
- GKG is the safety net fallback when Events produces zero results
- Events-derived episodes use source_type="gdelt_events"
- GKG-derived episodes retain source_type="gdelt_csv"
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import os
import tempfile
import zipfile

# GDELT GKG themes/entities CSV fields can reach multiple MB
# Python's default csv field size limit (128KB) is too small
csv.field_size_limit(10485760)  # 10MB
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.adapters.base import BaseAdapter
from src.adapters.gdelt_codebook import translate_actor, translate_cameo, translate_theme
from src.adapters.gdelt_events_parser import EventRecord, parse_events_file
from src.adapters.gdelt_mentions_parser import MentionRecord, parse_mentions, fetch_mentions_csv
from src.adapters.models import (
    EntityItem,
    NormalizedEpisode,
    Severity,
)
from src.adapters.macro_themes import MACRO_THEME_KEYWORDS
from src.adapters.cameo_event_codes_whitelist import CAMEO_EVENT_CODES_WHITELIST
from src.ingestion.events_pipeline_filter import EventsPipelineFilter
from src.core.config import get_settings
from src.utils.content_fetcher import ContentResult
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt
from src.utils.yaml_parser import strip_yaml_front_matter

logger = get_logger(__name__)

GKG_V2_COLUMN_NAMES: list[str] = [
    # Verified against actual GKG V2 CSV data (27 columns, tab-separated)
    # Core fields used by adapter are noted with (*)
    "v2_0_global_event_id",       # 0  (*) — YYYYMMDDHHMMSS-N format (timestamp-sequence)
    "v2_1_date",                  # 1  (*) — YYYYMMDDHHMMSS
    "v2_2_source_collection",     # 2  (*) — collection method (1=web, 2=social)
    "v2_3_domain",                # 3  (*) — source domain (e.g. reuters.com)
    "v2_4_source_url",            # 4  (*) — full source URL
    "v2_5_language",              # 5  (*) — language code
    "v2_6_untagged",              # 6     — reserved/unused
    "v2_7_themes",                # 7  (*) — semicolon-separated theme codes
    "v2_8_untagged",              # 8     — reserved/unused
    "v2_9_locations",             # 9  (*) — GDELT location encoding
    "v2_10_untagged",             # 10    — reserved/unused
    "v2_11_persons",              # 11 (*) — semicolon-separated person names
    "v2_12_untagged",             # 12    — reserved/unused
    "v2_13_organizations",        # 13 (*) — semicolon-separated org names
    "v2_14_untagged",             # 14    — reserved/unused
    "v2_15_tone",                 # 15 (*) — comma-separated: avg_tone,pos,neg,polarity,...
    "v2_16_positive_score",       # 16
    "v2_17_negative_score",       # 17
    "v2_18_polarity",             # 18
    "v2_19_activity_refs",        # 19
    "v2_20_activity_geo",         # 20
    "v2_21_activity_maybe",       # 21
    "v2_22_activity_geo_maybe",   # 22
    "v2_23_relations",            # 23
    "v2_24_relation_geo",         # 24
    "v2_25_relation_maybe",       # 25
    "v2_26_relation_geo_maybe",   # 26
]

LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"


class GdeltFetchError(Exception):
    """Raised when fetching lastupdate.txt fails after all retries."""


class GdeltDownloadError(Exception):
    """Raised when downloading a CSV zip fails after all retries."""


# ── tone-based severity (GKG path) ──────────────────────────────────


def _map_tone_to_severity(tone_str: str | None) -> Severity:
    """Map GKG V2 tone (col 15) to severity.

    Tone format is comma-separated floats: avg_tone,positive_score,negative_score,polarity,...
    Extracts the first value (average tone) for mapping.

    | Tone range    | severity  |
    |---------------|-----------|
    | > 5.0         | low       |
    | -5.0 ~ 5.0    | medium    |
    | -10.0 ~ -5.0  | high      |
    | < -10.0       | critical  |

    Returns "medium" for invalid/missing tone values.
    """
    if not tone_str:
        return "medium"
    try:
        first_val = tone_str.split(",")[0].strip()
        tone_val = float(first_val)
    except (ValueError, IndexError):
        return "medium"
    if tone_val > 5.0:
        return "low"
    elif tone_val >= -5.0:
        return "medium"
    elif tone_val >= -10.0:
        return "high"
    else:
        return "critical"


# ── goldstein-based severity (Events path) ──────────────────────────


def _map_goldstein_to_severity(goldstein: float | None) -> Severity:
    """Map |Goldstein Scale| to severity for Events-derived episodes.

    | |Goldstein| range | severity  |
    |-------------------|-----------|
    | ≥ 8               | critical  |
    | ≥ 6               | high      |
    | ≥ 4               | medium    |
    | < 4               | low       |

    Returns "medium" for None.
    """
    if goldstein is None:
        return "medium"
    val = abs(goldstein)
    if val >= 8.0:
        return "critical"
    elif val >= 6.0:
        return "high"
    elif val >= 4.0:
        return "medium"
    else:
        return "low"


# ── GKG helpers ─────────────────────────────────────────────────────


def _parse_persons_organizations(raw: str) -> str:
    """Parse GKG V2 Persons (col 5) or Organizations (col 6) field.

    GKG V2 can contain two formats:
    1. Plain text: "John Doe; Jane Smith" (normal names, ";" separated)
    2. GDELT entity mention encoding: "KILL#2##0######; CRISISLEX_CRISISLEXREC#2##0######"
       (each segment is CODE#count##offset#####length or similar)

    For plain text: split by ";" and return the joined names.
    For GDELT encoding (contains "#"): split by ";", then take only
    segments that look like real names (no "#"). If none found, return empty string
    so downstream code doesn't create spurious entities.

    Returns a "; "-joined string of parsed names, or empty string.
    """
    raw = raw.strip()
    if not raw:
        return ""

    # Check if this is GDELT encoded format (contains "#")
    if "#" in raw:
        # GDELT encoding: split by ";" and filter out encoded segments
        clean_names: list[str] = []
        for segment in raw.split(";"):
            segment = segment.strip()
            if not segment:
                continue
            # If the segment contains "#", it's a GDELT code — skip it
            if "#" in segment:
                continue
            clean_names.append(segment)
        return "; ".join(clean_names)

    # Plain text format — just clean up whitespace
    names = [n.strip() for n in raw.split(";") if n.strip()]
    return "; ".join(names)


def _parse_location(location_str: str) -> str:
    """Clean a GKG V2.7 Location field, stripping coordinate metadata.

    Input:  "#1#2#Beijing,Beijing,China#CN#CN|#VNM"
    Output: "Beijing, China (China)"

    If a country code is present (sub[4]), it is translated via
    ``translate_actor()`` and appended to the cleaned name.

    Multiple locations separated by '|' are joined with '; '.
    """
    parts = location_str.split("|")
    cleaned_parts: list[str] = []
    for part in parts:
        sub = part.split("#")
        # Typical format: #N#N#Name#CountryCode#SubCode
        # The name is at index 3 if there are at least 4 fragments
        if len(sub) >= 4:
            name = sub[3].strip()
        else:
            continue

        if not name:
            continue

        # If name contains comma-separated parts like "Beijing,Beijing,China"
        # We want just the city and country (first + last meaningful)
        name_parts = [p.strip() for p in name.split(",") if p.strip()]
        if len(name_parts) >= 3:
            # e.g. "Beijing,Beijing,China" → "Beijing, China"
            cleaned = f"{name_parts[0]}, {name_parts[-1]}"
        else:
            cleaned = ", ".join(name_parts)

        # Translate country code (sub[4]) via translate_actor and append
        # Format: "Beijing, China (China)"
        if len(sub) >= 5:
            country_code = sub[4].strip()
            if country_code:
                translated = translate_actor(country_code)
                if translated != country_code:
                    # Translation succeeded — append as " (translated_name)"
                    cleaned = f"{cleaned} ({translated})"

        cleaned_parts.append(cleaned)

    return "; ".join(cleaned_parts) if cleaned_parts else ""


def _parse_entities_from_record(record: dict) -> list[EntityItem]:
    """Extract EntityItem list from a parsed GKG record.

    Returns entities for Persons, Organizations, Locations only.
    Themes are NOT included as entities — they go into keywords instead.
    Events Actor entities (country/organization) are added when available.
    """
    entities: list[EntityItem] = []

    # V2.5 — Persons
    persons_raw = record.get("persons", "") or ""
    for name in persons_raw.split(";"):
        name = name.strip()
        if name:
            entities.append(EntityItem(type="person", name=name))

    # V2.6 — Organizations
    orgs_raw = record.get("organizations", "") or ""
    for name in orgs_raw.split(";"):
        name = name.strip()
        if name:
            entities.append(EntityItem(type="organization", name=name))

    # V2.7 — Locations
    locs_raw = record.get("locations", "") or ""
    for loc_str in locs_raw.split(";"):
        loc_str = loc_str.strip()
        if not loc_str:
            continue
        cleaned = _parse_location(loc_str)
        if cleaned:
            entities.append(EntityItem(type="location", name=cleaned))
        # If _parse_location returned empty string, the location is invalid (e.g. theme code)
        # — don't add it as an entity

    # Events Actor entities — when merge data provides translatable actor codes
    cameo_code = record.get("cameo_code") or None
    actor1_code = record.get("actor1_code") or None
    actor1_name = record.get("actor1_name") or ""
    actor2_code = record.get("actor2_code") or None
    actor2_name = record.get("actor2_name") or ""

    # Add actor1 as entity if it's a translatable country/organization
    if actor1_code and actor1_name and actor1_name != actor1_code:
        entities.append(EntityItem(type="country", name=actor1_name))

    # Add actor2 as entity if it's a translatable country/organization
    if actor2_code and actor2_name and actor2_name != actor2_code:
        entities.append(EntityItem(type="country", name=actor2_name))

    return entities


def _build_episode_body(record: dict) -> str:
    """Build a themes-based natural-language Markdown episode body.

    Generates summary from GKG fields only — does NOT depend on
    CAMEO event codes or actor names (these are always None after
    GKG-Events decoupling).

    Summary strategy:
        1. Translate top 3-5 themes via ``translate_theme()``
        2. Generate "News coverage related to ..." sentence
        3. Fallback: "A news development from {domain or unknown source}"
    """
    lines: list[str] = []

    themes_raw = record.get("themes", "") or ""
    source_url = record.get("source_url", "") or ""
    domain = record.get("domain", "") or ""
    valid_at = record.get("valid_at", "") or ""
    persons_raw = record.get("persons", "") or ""
    organizations_raw = record.get("organizations", "") or ""
    locations_raw = record.get("locations", "") or ""

    lines.append("## GDELT News Report")
    lines.append("")

    # Date
    if valid_at:
        lines.append(f"**Date**: {valid_at} UTC")

    # Domain
    if domain:
        lines.append(f"**Domain**: {domain}")

    lines.append("")

    # Summary paragraph — themes-based natural language
    theme_codes = [t.strip() for t in themes_raw.split(";") if t.strip()]
    translated_themes = [
        translate_theme(t.split(",")[0].strip())
        for t in theme_codes
    ]
    filtered_themes = [t for t in translated_themes if t]

    if filtered_themes:
        # Take top 3-5 for summary sentence
        summary_themes = filtered_themes[:5]
        if len(summary_themes) == 1:
            summary = f"News coverage related to {summary_themes[0]}."
        elif len(summary_themes) == 2:
            summary = f"News coverage related to {summary_themes[0]} and {summary_themes[1]}."
        else:
            summary = (
                "News coverage related to "
                + ", ".join(summary_themes[:-1])
                + f", and {summary_themes[-1]}."
            )
    elif domain:
        summary = f"A news development from {domain}."
    else:
        summary = "A news development from an unknown source."

    lines.append(f"**Summary**: {summary}")
    lines.append("")

    # Key Topics (themes) — top 10 translated themes
    if filtered_themes:
        lines.append(f"**Key Topics**: {', '.join(filtered_themes[:10])}")
        lines.append("")

    # Key Persons
    if persons_raw:
        persons_list = [n.strip() for n in persons_raw.split(";") if n.strip()]
        lines.append(f"**Key Persons**: {', '.join(persons_list)}")
        lines.append("")

    # Key Organizations
    if organizations_raw:
        orgs_list = [n.strip() for n in organizations_raw.split(";") if n.strip()]
        lines.append(f"**Key Organizations**: {', '.join(orgs_list)}")
        lines.append("")

    # Key Locations
    if locations_raw:
        locs = locations_raw.split(";")
        cleaned_locs: list[str] = []
        for loc_str in locs:
            loc_str = loc_str.strip()
            if not loc_str:
                continue
            cleaned = _parse_location(loc_str)
            if cleaned:
                cleaned_locs.append(cleaned)
        if cleaned_locs:
            lines.append(f"**Key Locations**: {'; '.join(cleaned_locs)}")
            lines.append("")

    # Source
    if source_url:
        lines.append(f"**Source**: {source_url}")

    return "\n".join(lines)


def _build_episode_body_with_full_text(record: dict, full_text: str) -> str:
    """Build episode body with ONLY full article text.

    When full text is available, use it directly without any metadata prefix/suffix.
    This avoids data pollution from redundant metadata (Date, Domain, Source)
    that Graphiti doesn't need.

    Args:
        record: GKG record dict (unused, kept for API compatibility).
        full_text: Full article text from ContentFetcher.

    Returns:
        Full article text as-is.
    """
    return full_text


# ── Events-specific body (CAMEO-centric) ────────────────────────────


def _build_event_episode_body(
    event_record: EventRecord,
    resolved_urls: list[str],
) -> str:
    """Build a CAMEO-centric Markdown episode body for Events-derived episodes.

    Includes CAMEO code + human-readable translation, actor names, Goldstein
    score, tone, event date, and resolved source URLs.

    Args:
        event_record: The EventRecord to build body for.
        resolved_urls: Resolved URLs from mentions_first strategy.

    Returns:
        Markdown-formatted episode body string.
    """
    lines: list[str] = []

    lines.append("## GDELT Events Report")
    lines.append("")

    # Date
    if event_record.event_date:
        lines.append(f"**Date**: {event_record.event_date}")

    # CAMEO event description
    cameo_translated = translate_cameo(event_record.cameo_code)
    lines.append(
        f"**Event**: {cameo_translated} (CAMEO {event_record.cameo_code})"
    )

    # Actors
    actor1 = event_record.actor1_name or event_record.actor1_code
    actor2 = event_record.actor2_name or event_record.actor2_code
    if actor1 or actor2:
        actor_line = f"**Actors**: {actor1}"
        if actor2:
            actor_line += f" → {actor2}"
        lines.append(actor_line)

    # Goldstein score
    if event_record.goldstein_scale is not None:
        severity = _map_goldstein_to_severity(event_record.goldstein_scale)
        lines.append(
            f"**Goldstein Score**: {event_record.goldstein_scale:+.1f} ({severity})"
        )

    # Tone
    if event_record.avg_tone is not None:
        lines.append(f"**Tone**: {event_record.avg_tone:+.1f}")

    lines.append("")

    # Sources
    if resolved_urls:
        lines.append("**Sources**:")
        for i, url in enumerate(resolved_urls, start=1):
            lines.append(f"{i}. {url}")
    elif event_record.source_url:
        lines.append(f"**Source**: {event_record.source_url}")

    return "\n".join(lines)


# ── Adapter class ────────────────────────────────────────────────────


class GdeltAdapter(BaseAdapter):
    """GDELT GKG V2 CSV adapter (Events-first architecture).

    V3.0 — Events-first with GKG fallback:
        1. Downloads Events, Mentions, and GKG CSVs concurrently.
        2. Events-first path: merge_event_data() → EventsPipelineFilter.filter()
           → _normalize_event_record().
        3. GKG fallback (when Events produce 0 results): parse_gkg()
           → filter_relevant() → normalize().
        4. Events-derived episodes use ``source_type="gdelt_events"``.
        5. GKG-derived episodes retain ``source_type="gdelt_csv"``.
    """

    def __init__(
        self,
        macro_theme_keywords: set[str] | None = None,
        cameo_event_codes_whitelist: set[str] | None = None,
        # ── REMOVED: domain_whitelist, enable_domain_filter ──
        max_retries: int = 3,
        backoff_base: float = 1.0,
        csv_download_timeout: float = 200.0,
        total_download_timeout: float = 300.0,
        dedup_cache: set[str] | None = None,
        content_fetcher: Any | None = None,
        events_filter_config_path: str = "data/gdelt_events_filter.json",
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self._macro_theme_keywords = macro_theme_keywords if macro_theme_keywords is not None else MACRO_THEME_KEYWORDS
        self._cameo_whitelist = cameo_event_codes_whitelist if cameo_event_codes_whitelist is not None else CAMEO_EVENT_CODES_WHITELIST
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # Async download timeout guards (see download_all_csvs). requests.get(
        # timeout=60) can hang indefinitely when the connection stalls through a
        # local proxy, so we backstop it at the asyncio layer: per-CSV wall and
        # an overall wall across all three concurrent downloads. On timeout the
        # affected source(s) are skipped — the capture cycle never blocks.
        self.csv_download_timeout = csv_download_timeout
        self.total_download_timeout = total_download_timeout
        self._last_records: list[dict] = []
        self._lastupdate_url = LASTUPDATE_URL
        self._content_fetcher = content_fetcher
        self._events_filter = EventsPipelineFilter(
            config_path=events_filter_config_path
        )

    # ── fetch helpers ────────────────────────────────────────────────

    def _make_retry_decorator(self) -> Any:
        """Create a tenacity retry decorator for HTTP calls."""
        return retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(
                multiplier=self.backoff_base, min=1, max=16
            ),
            retry=retry_if_exception_type(
                (requests.ConnectionError, requests.Timeout, requests.HTTPError)
            ),
            reraise=True,
        )

    def fetch_lastupdate(self) -> tuple[str, str, str]:
        """Fetch the latest Events, Mentions, and GKG CSV URLs from lastupdate.txt.

        ``lastupdate.txt`` contains three lines (events, mentions, GKG).
        Each line has format: ``<date> <time> <csv_url>``.

        Returns:
            A tuple of (events_url, mentions_url, gkg_url).

        Raises:
            GdeltFetchError: If all retry attempts fail or format is invalid.
        """
        retry_dec = self._make_retry_decorator()

        @retry_dec
        def _do_fetch() -> tuple[str, str, str]:
            logger.info("Fetching lastupdate.txt from %s", self._lastupdate_url)
            resp = requests.get(self._lastupdate_url, timeout=30)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) < 3:
                raise GdeltFetchError(
                    f"Expected ≥3 lines, got {len(lines)}: {resp.text[:200]}"
                )

            urls: list[str] = []
            for i, line in enumerate(lines[:3]):
                parts = line.strip().split()
                if len(parts) < 3:
                    raise GdeltFetchError(
                        f"Expected ≥3 fields on line {i}, got {len(parts)}: {line[:200]}"
                    )
                url = parts[2].strip()
                if not url.startswith("http"):
                    raise GdeltFetchError(f"Invalid URL on line {i}: {url}")
                urls.append(url)

            events_url, mentions_url, gkg_url = urls
            logger.info(
                "Latest CSV URLs: events=%s mentions=%s gkg=%s",
                events_url, mentions_url, gkg_url,
            )
            return events_url, mentions_url, gkg_url

        try:
            return _do_fetch()
        except Exception as exc:
            raise GdeltFetchError(
                f"Failed to fetch lastupdate.txt after {self.max_retries} retries: {exc}"
            ) from exc

    def _download_single_csv(
        self, csv_url: str, label: str
    ) -> tuple[str, tempfile.TemporaryDirectory]:
        """Download a single .csv.zip file and extract the CSV.

        Uses ``tempfile.TemporaryDirectory`` and cleans up on failure.
        The directory handle is returned as part of a tuple for the caller
        to manage the lifecycle.

        Args:
            csv_url: The URL of the .csv.zip file.
            label: Source label for logging (e.g. "events", "mentions", "gkg").

        Returns:
            A tuple of (csv_path, tmp_dir). The caller is responsible
            for cleaning up tmp_dir after use.

        Raises:
            GdeltDownloadError: If all retry attempts fail.
        """
        retry_dec = self._make_retry_decorator()

        @retry_dec
        def _do_download() -> tuple[str, tempfile.TemporaryDirectory]:
            _tmp = tempfile.TemporaryDirectory()
            try:
                logger.info("Downloading %s CSV from %s", label, csv_url)
                resp = requests.get(csv_url, timeout=60)
                resp.raise_for_status()
                zip_path = os.path.join(_tmp.name, f"{label}.zip")
                with open(zip_path, "wb") as f:
                    f.write(resp.content)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                    if not csv_names:
                        raise GdeltDownloadError(
                            f"No CSV found in {label} zip archive: {zf.namelist()}"
                        )
                    csv_name = csv_names[0]
                    zf.extract(csv_name, _tmp.name)
                    csv_path = os.path.join(_tmp.name, csv_name)
                    logger.info("Extracted %s CSV: %s", label, csv_path)
                return csv_path, _tmp
            except Exception:
                _tmp.cleanup()
                raise

        try:
            return _do_download()
        except Exception as exc:
            raise GdeltDownloadError(
                f"Failed to download {label} CSV after {self.max_retries} retries: {exc}"
            ) from exc

    async def download_all_csvs(
        self, events_url: str, mentions_url: str, gkg_url: str
    ) -> tuple[list[EventRecord], dict[str, list[MentionRecord]], list[dict]]:
        """Download and parse all three CSV files concurrently using asyncio.gather().

        Each CSV download and parse runs in a thread executor to avoid blocking
        the event loop.

        Args:
            events_url: URL to Events CSV zip.
            mentions_url: URL to Mentions CSV zip.
            gkg_url: URL to GKG CSV zip.

        Returns:
            A tuple of (events_records, mentions_by_event, gkg_records).
            Failed sources return empty data.
        """
        # Local dict to hold temp directory handles, avoiding cross-coroutine shared state
        _tmp_dirs: list[tempfile.TemporaryDirectory] = []

        async def _do_one(label: str, url: str) -> tuple[str, tempfile.TemporaryDirectory] | None:
            """Download one CSV zip and return (csv_path, tmp_dir), or None on failure.

            The executor call is wrapped in ``asyncio.wait_for`` because
            ``requests.get(timeout=60)`` may never fire through a hung local
            proxy — the asyncio wall is the reliable backstop. On timeout the
            source is skipped (returns None) instead of blocking forever. The
            orphaned executor thread keeps running in the background until the
            process exits; it cannot stall the event loop.
            """
            try:
                csv_path, _tmp = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, self._download_single_csv, url, label
                    ),
                    timeout=self.csv_download_timeout,
                )
                return csv_path, _tmp
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out downloading %s CSV after %.0fs "
                    "(csv_download_timeout); skipping source",
                    label, self.csv_download_timeout,
                )
                return None
            except GdeltDownloadError as exc:
                logger.warning("Failed to download %s CSV: %s", label, exc)
                return None

        # Download all three concurrently, bounded by an overall timeout so a
        # hung proxy can never stall the whole capture cycle. Per-CSV walls
        # above already skip individual sources; this wall is the final safety
        # net (e.g. executor starvation). On timeout we degrade to empty data.
        try:
            gkg_result, events_result, mentions_result = await asyncio.wait_for(
                asyncio.gather(
                    _do_one("gkg", gkg_url),
                    _do_one("events", events_url),
                    _do_one("mentions", mentions_url),
                ),
                timeout=self.total_download_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Overall GDELT CSV download timed out after %.0fs "
                "(total_download_timeout); skipping all CSV sources",
                self.total_download_timeout,
            )
            return [], {}, []

        gkg_path = gkg_result[0] if gkg_result else None
        events_path = events_result[0] if events_result else None
        mentions_path = mentions_result[0] if mentions_result else None

        # Collect tmp dir handles for later cleanup
        for result in (gkg_result, events_result, mentions_result):
            if result is not None:
                _tmp_dirs.append(result[1])

        # Parse GKG
        gkg_records: list[dict] = []
        events_records: list[EventRecord] = []
        mentions_by_event: dict[str, list[MentionRecord]] = {}

        if gkg_path is not None:
            try:
                gkg_records = self.parse_gkg(gkg_path)
            except Exception as exc:
                logger.warning("Failed to parse GKG CSV: %s", exc)

        if events_path is not None:
            try:
                events_records = await asyncio.get_event_loop().run_in_executor(
                    None, parse_events_file, events_path
                )
            except Exception as exc:
                logger.warning("Failed to parse Events CSV: %s", exc)

        if mentions_path is not None:
            try:
                raw_mentions = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_mentions_csv, mentions_path
                )
                mentions_by_event = parse_mentions(raw_mentions)
            except Exception as exc:
                logger.warning("Failed to parse Mentions CSV: %s", exc)

        # Cleanup: remove all temporary directories after parsing is complete
        for _tmp in _tmp_dirs:
            try:
                _tmp.cleanup()
            except Exception:
                pass  # Best-effort cleanup; directories under /tmp are ephemeral

        logger.info(
            "Download/parse results: gkg=%d events=%d mentions=%d event_groups",
            len(gkg_records), len(events_records), len(mentions_by_event),
        )
        return events_records, mentions_by_event, gkg_records

    def download_gkg(self, csv_url: str) -> str:
        """Download the GKG CSV zip file and extract the CSV (legacy single-source).

        Kept for backward compatibility with the GKG-only fallback path.

        Args:
            csv_url: The URL of the .csv.zip file.

        Returns:
            Path to the extracted CSV file.

        Raises:
            GdeltDownloadError: If all retry attempts fail.
        """
        csv_path, _tmp = self._download_single_csv(csv_url, "gkg")
        _tmp.cleanup()
        return csv_path

    def parse_gkg(self, csv_path: str) -> list[dict]:
        """Parse a GKG V2 CSV file (tab-separated, 27 columns).

        Corrected column mapping (V2.3):
            col 0  → global_event_id
            col 1  → valid_at (YYYYMMDDHHMMSS)
            col 2  → source_collection
            col 3  → domain (source domain, for Layer C filtering)
            col 4  → source_url (full URL)
            col 5  → language
            col 7  → themes (semicolon-separated theme codes)
            col 9  → locations (GDELT location encoding)
            col 11 → persons (semicolon-separated names)
            col 13 → organizations (semicolon-separated names)
            col 15 → tone (comma-separated: avg_tone,positive,negative,polarity,...)

        Args:
            csv_path: Local path to the extracted .csv file.

        Returns:
            List of parsed record dicts.
        """
        records: list[dict] = []
        # We need at least 16 columns (0-15) for tone
        min_required_cols = 16

        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter="\t")
            for row_num, row in enumerate(reader, start=1):
                if not row or len(row) < 2:
                    continue  # skip completely empty lines
                if len(row) < min_required_cols:
                    logger.warning(
                        "Skipping row %d: expected %d columns, got %d",
                        row_num,
                        min_required_cols,
                        len(row),
                    )
                    continue

                record: dict[str, Any] = {}
                record["global_event_id"] = row[0].strip()
                record["valid_at"] = row[1].strip()
                record["source_collection"] = row[2].strip()
                record["domain"] = row[3].strip()
                record["source_url"] = row[4].strip()
                record["language"] = row[5].strip()
                record["themes"] = row[7].strip()
                record["locations"] = row[9].strip()
                # Parse persons and organizations — they can be GDELT encoded or plain text
                record["persons"] = _parse_persons_organizations(row[11].strip())
                record["organizations"] = _parse_persons_organizations(row[13].strip())
                record["tone"] = row[15].strip()
                records.append(record)

        logger.info("Parsed %d records from GKG CSV", len(records))
        return records

    # ── merge ─────────────────────────────────────────────────────────

    def merge_event_data(
        self,
        events_records: list[EventRecord] | None = None,
        mentions_by_event: dict[str, list[MentionRecord]] | None = None,
    ) -> list[tuple[EventRecord, list[MentionRecord]]]:
        """Events LEFT JOIN Mentions — primary data path (Events-first).

        Builds an Events index keyed by ``event_id`` and LEFT JOINs
        Mentions lists on matching IDs.

        Returns empty list when Events data is not available, triggering
        the GKG fallback in ``fetch()``.

        Args:
            events_records: Parsed EventRecord list (may be None or empty).
            mentions_by_event: Mentions grouped by event_id (may be None).

        Returns:
            List of ``(EventRecord, list[MentionRecord])`` tuples.
            Tuples with no matching mentions get an empty mention list.
        """
        if not events_records:
            logger.debug("merge_event_data: no Events data — returning empty list")
            return []

        mentions = mentions_by_event or {}
        result: list[tuple[EventRecord, list[MentionRecord]]] = []

        for ev in events_records:
            ev_mentions = mentions.get(ev.event_id, [])
            result.append((ev, ev_mentions))

        logger.debug(
            "merge_event_data (Events-first): %d events, %d with mentions",
            len(result),
            sum(1 for _, m in result if m),
        )
        return result

    def _merge_gkg_data(
        self,
        gkg_records: list[dict],
    ) -> list[dict]:
        """Passthrough merge — returns GKG records without Events/Mentions JOIN.

        Used by the GKG fallback path. Sets all Events/Mentions derived
        fields to ``None`` (same as V2.3 behavior).

        Args:
            gkg_records: Parsed GKG records.

        Returns:
            List of GKG record dicts with Events/Mentions fields set to None.
        """
        merged: list[dict] = []
        for gkg_rec in gkg_records:
            rec = dict(gkg_rec)
            rec["cameo_code"] = None
            rec["actor1_code"] = None
            rec["actor1_name"] = None
            rec["actor2_code"] = None
            rec["actor2_name"] = None
            rec["goldstein_scale"] = None
            rec["avg_tone"] = None
            rec["event_date"] = None
            rec["mentions"] = None
            merged.append(rec)

        logger.debug(
            "_merge_gkg_data passthrough: %d GKG records",
            len(merged),
        )
        return merged

    # ── Events pipeline helpers ──────────────────────────────────────

    def _events_tuple_to_dict(
        self,
        event_record: EventRecord,
        mentions: list[MentionRecord],
        resolved_urls: list[str],
    ) -> dict[str, Any]:
        """Convert an (EventRecord, mentions) pair to a dict for ``normalize()``.

        The returned dict carries the ``_event_record`` key so that
        ``_normalize_event_record()`` can access the original EventRecord
        for body generation.

        Args:
            event_record: The EventRecord.
            mentions: List of MentionRecords for this event.
            resolved_urls: Resolved URLs from mentions_first strategy.

        Returns:
            Dict with event fields for normalization.
        """
        # Build a valid_at string compatible with _parse_gkg_datetime
        # Use event_date as basis: "2025-07-22" → "20250722000000"
        event_date_raw = event_record.event_date.replace("-", "")
        valid_at_str = f"{event_date_raw}000000"

        return {
            "cameo_code": event_record.cameo_code,
            "actor1_code": event_record.actor1_code,
            "actor1_name": event_record.actor1_name,
            "actor2_code": event_record.actor2_code,
            "actor2_name": event_record.actor2_name,
            "goldstein_scale": event_record.goldstein_scale,
            "avg_tone": event_record.avg_tone,
            "event_date": event_record.event_date,
            "source_url": resolved_urls[0] if resolved_urls else "",
            "resolved_urls": resolved_urls,
            "valid_at": valid_at_str,
            "domain": "",
            "themes": "",
            "locations": "",
            "persons": "",
            "organizations": "",
            "tone": "",
            "_event_record": event_record,
            "_mentions": mentions,
        }

    # ── record-level methods ─────────────────────────────────────────

    @property
    def _authoritative_domains(self) -> set[str]:
        """Lazy-load authoritative media domains (Plan D)."""
        try:
            return self.__authoritative_domains
        except AttributeError:
            from src.adapters.authoritative_media import AUTHORITATIVE_MEDIA_DOMAINS
            self.__authoritative_domains = AUTHORITATIVE_MEDIA_DOMAINS
            return self.__authoritative_domains

    def filter_relevant(
        self, records: list[dict]
    ) -> list[dict]:
        """Filter records by Plan D: authoritative media OR macro theme.

        Plan D replaces the former Layer A + Layer C AND-logic:

        1. **Authoritative media**: If the record's domain is in
           ``AUTHORITATIVE_MEDIA_DOMAINS``, it passes unconditionally
           (no theme keyword check).

        2. **Non-authoritative media with macro theme**: If the domain
           is NOT authoritative, the record passes only if its GKG Themes
           contain at least one ``MACRO_THEME_KEYWORDS`` substring.

        3. **Otherwise**: Record is rejected.

        Layer B (CAMEO) remains inactive.

        Filter logic: authoritative_domain OR (Layer A themes match)

        If macro_theme_keywords is empty, returns all records
        (no filtering applied).
        """
        if not self._macro_theme_keywords:
            return records

        matched: list[dict] = []
        authoritative_passed = 0
        theme_passed = 0
        rejected = 0

        authoritative_domains = self._authoritative_domains

        for rec in records:
            domain = (rec.get("domain") or "").strip().lower()

            # Plan D check 1: authoritative domain -> unconditional pass
            if domain and domain in authoritative_domains:
                matched.append(rec)
                authoritative_passed += 1
                continue

            # Plan D check 2: non-authoritative -> macro theme match required
            import re
            themes_text = (rec.get("themes") or "").upper()

            # Check if any macro theme keyword appears as a whole word
            matched_theme = None
            for kw in self._macro_theme_keywords:
                # Word boundary match: keyword must be surrounded by word boundaries
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, themes_text):
                    matched_theme = kw
                    break

            if matched_theme:
                matched.append(rec)
                theme_passed += 1
                continue

            # Plan D check 3: reject
            rejected += 1

        logger.info(
            "Plan D filter: %d -> %d records (authoritative_passed=%d, theme_passed=%d, rejected=%d)",
            len(records),
            len(matched),
            authoritative_passed,
            theme_passed,
            rejected,
        )
        return matched

    async def normalize(
        self,
        record: dict,
        fetch_results: dict[str, ContentResult] | None = None,
    ) -> NormalizedEpisode:
        """Convert a single merged record to NormalizedEpisode.

        Routes to Events-specific normalization (``_normalize_event_record()``)
        when the record is Events-derived (detected by presence of
        ``_event_record`` key), otherwise falls through to GKG normalization.

        If ``fetch_results`` is provided, uses pre-fetched ContentResult
        from batch fetch to avoid per-URL fetch overhead.

        Args:
            record: A dict from merge_event_data().
            fetch_results: Optional dict of pre-fetched content results
                keyed by URL (from batch fetch).

        Returns:
            NormalizedEpisode with content_scope=MACRO in metadata.
        """
        # Route to Events-specific normalization if applicable
        if record.get("_event_record") is not None:
            return await self._normalize_event_record(record, fetch_results)

        # ── GKG normalization path (unchanged) ───────────────────────
        raw_dt = record.get("valid_at", "")
        valid_at = _parse_gkg_datetime(raw_dt)

        tone_str = record.get("tone", "")
        severity = _map_tone_to_severity(tone_str)

        entities = _parse_entities_from_record(record)

        source_url = record.get("source_url", "") or None

        # Build episode body: prefer full_text over summary (never mix both)
        # Same pattern as RSS adapter for Graphiti consistency
        metadata: dict[str, Any] = {"content_scope": "MACRO", "content_fetched": False}
        full_text: str | None = None

        if self._content_fetcher and source_url:
            # Prefer pre-fetched result from batch
            if fetch_results and source_url in fetch_results:
                result = fetch_results[source_url]
                if result.success and result.text:
                    full_text = result.text
                    metadata["content_fetched"] = True
                else:
                    logger.debug(
                        "Pre-fetched content failed for %s: %s — using GKG summary only",
                        source_url,
                        result.error,
                    )
            else:
                try:
                    result = self._content_fetcher.fetch(source_url)
                    if result.success and result.text:
                        full_text = result.text
                        metadata["content_fetched"] = True
                    else:
                        logger.debug(
                            "ContentFetcher failed for %s: %s — using GKG summary only",
                            source_url,
                            result.error,
                        )
                except Exception as exc:
                    logger.debug(
                        "ContentFetcher error for %s: %s — using GKG summary only",
                        source_url,
                        exc,
                    )

        if full_text:
            pure_text, yaml_meta = strip_yaml_front_matter(full_text)
            body = _build_episode_body_with_full_text(record, pure_text)
            if yaml_meta:
                metadata["extracted_metadata"] = yaml_meta
        else:
            body = _build_episode_body(record)

        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        keywords_raw: set[str] = set()
        for theme_str in (record.get("themes", "") or "").split(";"):
            theme = theme_str.strip()
            if theme:
                # Strip ",数字" suffix before lookup (e.g. "WB_2453_ORGANIZED_CRIME,810")
                translated = translate_theme(theme.split(",")[0].strip())
                if translated:
                    keywords_raw.add(translated)
        keywords = sorted(keywords_raw)[:20]
        if len(keywords_raw) > 20:
            logger.debug(
                "Keywords truncated: %d unique themes → %d keywords (capped at 20)",
                len(keywords_raw),
                len(keywords),
            )

        name = NormalizedEpisode.make_name(
            source_type="gdelt_csv",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id="gkg",
        )

        return NormalizedEpisode(
            episode_body=body,
            name=name,
            source_description="GDELT GKG V2",
            source_type="gdelt_csv",
            source_url=source_url,
            valid_at=valid_at,
            content_hash=content_hash,
            entities=entities,
            severity=severity,
            keywords=keywords,
            metadata={"content_scope": "MACRO"},
        )

    async def _normalize_event_record(
        self,
        record: dict,
        fetch_results: dict[str, ContentResult] | None = None,
    ) -> NormalizedEpisode:
        """Convert an Events-derived record dict to a NormalizedEpisode.

        Uses the original ``EventRecord`` (stored in ``record["_event_record"]``)
        and resolved URLs (stored in ``record["resolved_urls"]``) for
        body generation and entity extraction.

        Args:
            record: Dict from ``_events_tuple_to_dict()``.
            fetch_results: Optional pre-fetched content results.

        Returns:
            NormalizedEpisode with ``source_type="gdelt_events"``.
        """
        event_record: EventRecord = record["_event_record"]
        resolved_urls: list[str] = record.get("resolved_urls", [])

        # Parse valid_at from event_date
        event_date_raw = event_record.event_date.replace("-", "")
        valid_at_str = f"{event_date_raw}000000"
        valid_at = _parse_gkg_datetime(valid_at_str)

        # Severity from Goldstein scale
        severity = _map_goldstein_to_severity(event_record.goldstein_scale)

        # Source URL
        source_url: str | None = resolved_urls[0] if resolved_urls else (
            event_record.source_url if event_record.source_url else None
        )

        # Build episode body: prefer full_text, else CAMEO-centric summary
        metadata: dict[str, Any] = {"content_scope": "MACRO", "content_fetched": False}
        full_text: str | None = None

        if self._content_fetcher and source_url:
            if fetch_results and source_url in fetch_results:
                result = fetch_results[source_url]
                if result.success and result.text:
                    full_text = result.text
                    metadata["content_fetched"] = True
                else:
                    logger.debug(
                        "Pre-fetched content failed for Events record %s: %s — using CAMEO summary",
                        event_record.event_id,
                        result.error,
                    )
            else:
                try:
                    result = self._content_fetcher.fetch(source_url)
                    if result.success and result.text:
                        full_text = result.text
                        metadata["content_fetched"] = True
                except Exception as exc:
                    logger.debug(
                        "ContentFetcher error for Events record %s: %s — using CAMEO summary",
                        event_record.event_id,
                        exc,
                    )

        if full_text:
            # Full text available: use ONLY the article content (no CAMEO summary)
            # This avoids data pollution from prepending redundant metadata
            pure_text, yaml_meta = strip_yaml_front_matter(full_text)
            body = pure_text
            if yaml_meta:
                metadata["extracted_metadata"] = yaml_meta
        else:
            # No full text: fall back to CAMEO summary (~300 chars)
            body = _build_event_episode_body(event_record, resolved_urls)

        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        # Entities: actors as country entities
        entities: list[EntityItem] = []
        if event_record.actor1_name and event_record.actor1_name != event_record.actor1_code:
            entities.append(EntityItem(
                type="country",
                name=event_record.actor1_name,
            ))
        if event_record.actor2_name and event_record.actor2_name != event_record.actor2_code:
            entities.append(EntityItem(
                type="country",
                name=event_record.actor2_name,
            ))

        # Keywords: CAMEO code description
        keywords: list[str] = []
        cameo_desc = translate_cameo(event_record.cameo_code)
        if cameo_desc:
            keywords.append(cameo_desc)

        name = NormalizedEpisode.make_name(
            source_type="gdelt_events",
            valid_at=valid_at,
            content_hash=content_hash,
            group_id=f"ev-{event_record.event_id}",
        )

        return NormalizedEpisode(
            episode_body=body,
            name=name,
            source_description="GDELT Events V2",
            source_type="gdelt_events",
            source_url=source_url,
            valid_at=valid_at,
            content_hash=content_hash,
            entities=entities,
            severity=severity,
            keywords=keywords,
            metadata=metadata,
        )

    # ── fetch orchestration ──────────────────────────────────────────

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Full fetch pipeline: Events-first → GKG fallback.

        Events-first (primary):
            1. Download and parse Events + Mentions + GKG CSVs.
            2. LEFT JOIN Events with Mentions.
            3. Apply EventsPipelineFilter (CAMEO → Goldstein → NumMentions).
            4. Resolve URLs from Mentions (mentions_first strategy).
            5. If results exist → return events dicts.

        GKG fallback (when Events produce 0 results):
            1. Parse GKG records.
            2. Passthrough merge (set Events/Mentions fields to None).
            3. Plan D filter (authoritative domain OR macro theme match).
            4. Return filtered GKG records.

        If all CSV downloads fail, returns cached records from last successful run.

        Returns:
            List of merged record dicts (Events-derived or GKG-derived).
        """
        try:
            # Step 1: Get all three URLs
            events_url, mentions_url, gkg_url = self.fetch_lastupdate()

            # Step 2: Download and parse all three concurrently
            events_records, mentions_by_event, gkg_records = await self.download_all_csvs(
                events_url, mentions_url, gkg_url
            )

            # Step 3: Events-first path
            if events_records:
                events_passed = await self._run_events_pipeline(
                    events_records, mentions_by_event
                )
                if events_passed:
                    logger.info(
                        "Events-first pipeline: %d events passed all filters",
                        len(events_passed),
                    )
                    self._pre_filter_count = len(events_records)
                    self._last_records = events_passed
                    return events_passed

                logger.info(
                    "Events pipeline returned 0 records — falling back to GKG "
                    "(events=%d parsed, all filtered out)",
                    len(events_records),
                )

            # Step 4: GKG fallback
            if not gkg_records:
                logger.warning("GKG records are empty, falling back to cached records")
                return self._last_records

            merged_gkg = self._merge_gkg_data(gkg_records)
            self._pre_filter_count = len(merged_gkg)
            filtered = self.filter_relevant(merged_gkg)
            # Mark as degraded (Events tried but failed)
            for rec in filtered:
                rec["_degraded"] = True
            self._last_records = filtered
            return filtered

        except GdeltFetchError as exc:
            # All URLs failed — fall back to cached records
            logger.warning(
                "GDELT fetch failed: %s. Returning %d cached records.",
                exc,
                len(self._last_records),
            )
            if not self._last_records:
                # Try GKG-only fallback
                try:
                    gkg_url = self._fallback_fetch_gkg_only()
                    csv_path = self.download_gkg(gkg_url)
                    gkg_records = self.parse_gkg(csv_path)
                    merged = self._merge_gkg_data(gkg_records)
                    self._pre_filter_count = len(merged)
                    filtered = self.filter_relevant(merged)
                    for rec in filtered:
                        rec["_degraded"] = True
                    self._last_records = filtered
                except Exception as fallback_exc:
                    logger.warning(
                        "GKG-only fallback also failed: %s",
                        fallback_exc,
                    )
                    return self._last_records
            return self._last_records

    async def _run_events_pipeline(
        self,
        events_records: list[EventRecord],
        mentions_by_event: dict[str, list[MentionRecord]],
    ) -> list[dict]:
        """Run the Events-first pipeline: merge → filter → resolve URLs → to dicts.

        Args:
            events_records: Parsed EventRecord list.
            mentions_by_event: Mentions grouped by event_id.

        Returns:
            List of dicts suitable for ``normalize()``, or empty list if
            all events were filtered out.
        """
        # Step 1: Events LEFT JOIN Mentions
        merged = self.merge_event_data(events_records, mentions_by_event)
        if not merged:
            return []

        # Step 1.5: Staleness filter — reject events with event_date older than news_max_age_days
        settings = get_settings()
        cutoff_date = (now_hkt() - timedelta(days=settings.news_max_age_days)).strftime("%Y-%m-%d")
        fresh_events = [ev for ev in events_records if ev.event_date >= cutoff_date]
        stale_count = len(events_records) - len(fresh_events)
        if stale_count > 0:
            logger.info(
                "Staleness filter: %d/%d events rejected (event_date < %s)",
                stale_count,
                len(events_records),
                cutoff_date,
            )
        if not fresh_events:
            return []
        events_records = fresh_events

        # Step 2: Three-stage filter
        config = self._events_filter._load_config()
        filtered = self._events_filter.filter(events_records, mentions_by_event)
        if not filtered:
            return []

        # Step 3: Resolve URLs and convert to dicts
        result: list[dict] = []
        for event_rec, mentions in filtered:
            resolved_urls = self._events_filter.resolve_urls(
                event_rec, mentions, config
            )
            record_dict = self._events_tuple_to_dict(
                event_rec, mentions, resolved_urls
            )
            result.append(record_dict)

        return result

    def _fallback_fetch_gkg_only(self) -> str:
        """Fallback: fetch only the GKG URL from lastupdate.txt.

        Used when Events-first and triple-source fetch both fail.

        Returns:
            GKG CSV URL string.

        Raises:
            GdeltFetchError: If fetching GKG URL fails.
        """
        retry_dec = self._make_retry_decorator()

        @retry_dec
        def _do_fetch() -> str:
            logger.info("GKG-only fallback: fetching lastupdate.txt from %s", self._lastupdate_url)
            resp = requests.get(self._lastupdate_url, timeout=30)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) < 3:
                raise GdeltFetchError(
                    f"Expected ≥3 lines, got {len(lines)}: {resp.text[:200]}"
                )
            gkg_line = lines[2].strip()
            parts = gkg_line.split()
            if len(parts) < 3:
                raise GdeltFetchError(
                    f"Expected ≥3 fields on GKG line, got {len(parts)}: {gkg_line[:200]}"
                )
            url = parts[2].strip()
            if not url.startswith("http"):
                raise GdeltFetchError(f"Invalid GKG URL: {url}")
            logger.info("GKG-only fallback URL: %s", url)
            return url

        return _do_fetch()

    async def run(self, **kwargs: Any) -> list[NormalizedEpisode]:
        """Full pipeline with batch content fetch + degraded metadata.

        V3.0 Events-first pipeline flow:
            1. Fetch raw records (Events-first → GKG fallback).
            2. Batch-fetch all article source URLs using ContentFetcher.
            3. Normalize each record (Events path → _normalize_event_record,
               GKG path → normalize).
            4. Dedup + degraded metadata tagging.
        """
        records = await self.fetch(**kwargs)

        # Phase 1: batch fetch all article source URLs
        source_urls = [
            r.get("source_url") for r in records if r.get("source_url")
        ]
        fetch_results: dict[str, ContentResult] = {}
        if self._content_fetcher and source_urls:
            try:
                # 30s timeout per batch to avoid hanging on slow/Cloudflare sites
                results = await self._content_fetcher.fetch_batch(
                    source_urls, batch_timeout=180.0
                )
                fetch_results = {r.url: r for r in results}
                logger.info(
                    "Batch-fetched %d/%d GDELT article contents",
                    sum(1 for r in results if r.success),
                    len(results),
                )
            except Exception as exc:
                logger.warning(
                    "Batch content fetch failed for GDELT: %s — "
                    "falling back to per-URL fetch",
                    exc,
                )

        # Phase 2: normalize all records with pre-fetched content
        episodes = await asyncio.gather(
            *[
                self.normalize(r, fetch_results=fetch_results)
                for r in records
            ],
        )

        # Phase 3 (json-persistence-layer): 不再截断 episode。
        # 设计 §2.3: capture 阶段把所有 episode 落盘（LandingStore 写 JSONL +
        # 登记 pending），入库由独立 IngestWorker 后台消化（~7.7s/ep 串行），
        # 截断的历史假设（下个 cycle 会重试被截断的 episode）对 GDELT 窗口式
        # 源不成立 — 旧数据已随 15 分钟窗口滑走，截断即永久丢弃。

        episodes = self.dedup(list(episodes))

        # Tag episodes originated from degraded fallback
        if self._last_records and any(
            r.get("_degraded") for r in self._last_records
        ):
            for ep in episodes:
                ep.metadata["_degraded"] = True
        return episodes


def _parse_gkg_datetime(raw: str) -> datetime:
    """Parse GKG datetime string (YYYYMMDDHHMMSS) to UTC datetime.

    Falls back to current UTC time on parse failure.
    """
    raw = raw.strip()
    if len(raw) >= 14:
        try:
            return datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    logger.warning("Could not parse GKG datetime '%s', using current HKT time", raw)
    return now_hkt()
