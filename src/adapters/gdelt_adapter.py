"""GDELT CSV Adapter — fetch, parse, and normalize GDELT GKG V2 data.

Data flow::
    fetch_lastupdate() → download_all_csvs() → parse_gkg() + parse_events + parse_mentions
    → merge_event_data() → filter_relevant() → normalize() → dedup()

Only the HTTP CSV data plane (http://data.gdeltproject.org/) is used.
HTTPS is avoided because the HTTPS endpoints are blocked by the GFW.

V2.2 → G5: Refactored for triple CSV source integration:
- fetch_lastupdate() returns (events_url, mentions_url, gkg_url)
- download_all_csvs() uses asyncio.gather() for concurrent downloads
- merge_event_data() LEFT JOINs by GlobalEventID
- filter_relevant() implements dual-layer (Themes OR CAMEO) filtering
- GKG-only fallback path when all three CSV sources fail
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
from datetime import datetime, timezone
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
from src.adapters.gdelt_events_parser import parse_events_file
from src.adapters.gdelt_mentions_parser import parse_mentions, fetch_mentions_csv
from src.adapters.models import (
    EntityItem,
    NormalizedEpisode,
    Severity,
)
from src.adapters.macro_themes import MACRO_THEME_KEYWORDS
from src.adapters.cameo_event_codes_whitelist import CAMEO_EVENT_CODES_WHITELIST
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt

logger = get_logger(__name__)

GKG_V2_COLUMN_NAMES: list[str] = [
    "v2_1_global_event_id",  # 0  — not used directly
    "v2_1_date",  # 1          — YYYYMMDDHHMMSS (actually col 1 after id)
    "v2_2_source_collection",  # 2
    "v2_3_source_url",  # 3
    "v2_4_language",  # 4
    "v2_5_persons",  # 5
    "v2_6_organizations",  # 6
    "v2_7_locations",  # 7
    "v2_8_themes",  # 8
    "v2_9_tone_img",  # 9
    "v2_10_pagerank_avg",  # 10
    "v2_11_pagerank_max",  # 11
    "v2_12_pagerank_min",  # 12
    "v2_13_parse_count",  # 13
    "v2_14_tone",  # 14
    "v2_15_positive_score",  # 15
    "v2_16_negative_score",  # 16
    "v2_17_polarity",  # 17
    "v2_18_activity_refs",  # 18
    "v2_19_activity_geo",  # 19
    "v2_20_activity_maybe",  # 20
    "v2_21_activity_geo_maybe",  # 21
    "v2_22_relations",  # 22
    "v2_23_relation_geo",  # 23
    "v2_24_relation_maybe",  # 24
    "v2_25_relation_geo_maybe",  # 25
    "v2_26_mentions",  # 26
]

LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"


class GdeltFetchError(Exception):
    """Raised when fetching lastupdate.txt fails after all retries."""


class GdeltDownloadError(Exception):
    """Raised when downloading a CSV zip fails after all retries."""


def _map_tone_to_severity(tone_str: str | None) -> Severity:
    """GDELT GKG tone format ("entity,score;...") is not a single severity value.

    Severity is determined later by L-4 rule-based enricher (severity_enricher.py).
    This function just returns a neutral placeholder.
    """
    return "medium"


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

    return "; ".join(cleaned_parts) if cleaned_parts else location_str


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
    """Build a human-readable episode body from a merged record.

    Include CAMEO event descriptions, actors, themes, persons,
    organizations, locations, and source URL.
    """
    parts: list[str] = []

    # CAMEO Event description (from merged Events data)
    cameo_code = record.get("cameo_code") or ""
    if cameo_code:
        cameo_desc = translate_cameo(cameo_code)
        parts.append(f"[CAMEO Event: {cameo_desc}]")

    # Actors (from merged Events data)
    actor1_name = record.get("actor1_name") or ""
    actor2_name = record.get("actor2_name") or ""
    if actor1_name or actor2_name:
        actor_str = f"[Actors: {actor1_name}" if actor1_name else "[Actors: unknown"
        if actor2_name:
            actor_str += f" → {actor2_name}"
        actor_str += "]"
        parts.append(actor_str)

    # Themes — translate to human-readable
    themes = record.get("themes", "") or ""
    translated_themes = "; ".join(
        translate_theme(t.strip()) for t in themes.split(";") if t.strip()
    )
    if translated_themes:
        parts.append(f"Themes: {translated_themes}")

    # Persons
    persons = record.get("persons", "") or ""
    if persons:
        parts.append(f"Persons: {persons}")

    # Organizations
    organizations = record.get("organizations", "") or ""
    if organizations:
        parts.append(f"Organizations: {organizations}")

    # Locations (cleaned)
    locations = record.get("locations", "") or ""
    if locations:
        loc_cleaned = "; ".join(
            _parse_location(l) for l in locations.split(";") if l.strip()
        )
        if loc_cleaned:
            parts.append(f"Locations: {loc_cleaned}")

    # Source URL
    source_url = record.get("source_url", "") or ""
    if source_url:
        parts.append(f"Source: {source_url}")

    return " | ".join(parts)


class GdeltAdapter(BaseAdapter):
    """GDELT GKG V2 CSV adapter.

    Fetches the latest Events, Mentions, and GKG CSV files from
    data.gdeltproject.org, downloads them concurrently, parses each
    using dedicated parser modules, merges by GlobalEventID (LEFT JOIN),
    and filters by dual-layer (Themes OR CAMEO) criteria.

    G5: Triple CSV source integration with concurrent download and merge.
    """

    def __init__(
        self,
        macro_theme_keywords: set[str] | None = None,
        cameo_event_codes_whitelist: set[str] | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self._macro_theme_keywords = macro_theme_keywords or MACRO_THEME_KEYWORDS
        self._cameo_whitelist = cameo_event_codes_whitelist or CAMEO_EVENT_CODES_WHITELIST
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._last_records: list[dict] = []
        self._lastupdate_url = LASTUPDATE_URL

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
                    csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
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
    ) -> tuple[list[dict], dict[str, list], list[dict]]:
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
            """Download one CSV zip and return (csv_path, tmp_dir), or None on failure."""
            try:
                csv_path, _tmp = await asyncio.get_event_loop().run_in_executor(
                    None, self._download_single_csv, url, label
                )
                return csv_path, _tmp
            except GdeltDownloadError as exc:
                logger.warning("Failed to download %s CSV: %s", label, exc)
                return None

        # Download all three concurrently
        gkg_result, events_result, mentions_result = await asyncio.gather(
            _do_one("gkg", gkg_url),
            _do_one("events", events_url),
            _do_one("mentions", mentions_url),
        )

        gkg_path = gkg_result[0] if gkg_result else None
        events_path = events_result[0] if events_result else None
        mentions_path = mentions_result[0] if mentions_result else None

        # Collect tmp dir handles for later cleanup
        for result in (gkg_result, events_result, mentions_result):
            if result is not None:
                _tmp_dirs.append(result[1])

        # Parse GKG
        gkg_records: list[dict] = []
        events_records: list[dict] = []
        mentions_by_event: dict[str, list] = {}

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

        Args:
            csv_path: Local path to the extracted .csv file.

        Returns:
            List of parsed record dicts.
        """
        records: list[dict] = []
        expected_cols = 27

        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter="\t")
            for row_num, row in enumerate(reader, start=1):
                if not row or len(row) < 2:
                    continue  # skip completely empty lines
                if len(row) < expected_cols:
                    logger.warning(
                        "Skipping row %d: expected %d columns, got %d",
                        row_num,
                        expected_cols,
                        len(row),
                    )
                    continue

                # Pad shorter rows to 27 columns
                if len(row) < expected_cols:
                    row = row + [""] * (expected_cols - len(row))

                record: dict[str, Any] = {}
                record["global_event_id"] = row[0].strip()
                record["valid_at"] = row[1].strip()
                record["source_collection"] = row[2].strip()
                record["source_url"] = row[3].strip()
                record["language"] = row[4].strip()
                record["persons"] = row[5].strip()
                record["organizations"] = row[6].strip()
                record["locations"] = row[7].strip()
                record["themes"] = row[8].strip()
                record["tone"] = row[14].strip()
                records.append(record)

        logger.info("Parsed %d records from GKG CSV", len(records))
        return records

    # ── merge ─────────────────────────────────────────────────────────

    def merge_event_data(
        self,
        gkg_records: list[dict],
        events_records: list[dict] | None = None,
        mentions_by_event: dict[str, list] | None = None,
    ) -> list[dict]:
        """Merge GKG, Events, and Mentions data by GlobalEventID (LEFT JOIN).

        GKG is the primary table. Events and Mentions data are LEFT JOINed
        onto GKG records by GlobalEventID. Records without matching Events
        or Mentions data keep only GKG fields.

        Args:
            gkg_records: Parsed GKG records, each with a ``global_event_id`` key.
            events_records: List of EventRecord objects from parse_events_file(),
                or None if Events data is unavailable.
            mentions_by_event: Dict mapping event_id -> list[MentionRecord],
                or None if Mentions data is unavailable.

        Returns:
            List of merged record dicts, one per GKG record, enriched with
            optional Events/Mentions fields.
        """
        # Build lookup maps for O(1) access
        events_map: dict[str, dict[str, Any]] = {}
        if events_records is not None:
            for ev in events_records:
                # EventRecord dataclass — convert to dict-like access
                eid = str(getattr(ev, "event_id", ""))
                if eid:
                    events_map[eid] = {
                        "cameo_code": getattr(ev, "cameo_code", ""),
                        "actor1_code": getattr(ev, "actor1_code", ""),
                        "actor1_name": getattr(ev, "actor1_name", ""),
                        "actor2_code": getattr(ev, "actor2_code", ""),
                        "actor2_name": getattr(ev, "actor2_name", ""),
                        "goldstein_scale": getattr(ev, "goldstein_scale", None),
                        "avg_tone": getattr(ev, "avg_tone", None),
                        "event_date": getattr(ev, "event_date", ""),
                    }

        merged: list[dict] = []
        for gkg_rec in gkg_records:
            eid = gkg_rec.get("global_event_id", "")

            # Start with GKG fields
            merged_rec = dict(gkg_rec)

            # LEFT JOIN Events data
            ev_data = events_map.get(eid, {})
            merged_rec["cameo_code"] = ev_data.get("cameo_code")
            merged_rec["actor1_code"] = ev_data.get("actor1_code")
            merged_rec["actor1_name"] = ev_data.get("actor1_name")
            merged_rec["actor2_code"] = ev_data.get("actor2_code")
            merged_rec["actor2_name"] = ev_data.get("actor2_name")
            merged_rec["goldstein_scale"] = ev_data.get("goldstein_scale")
            merged_rec["avg_tone"] = ev_data.get("avg_tone")
            merged_rec["event_date"] = ev_data.get("event_date")

            # LEFT JOIN Mentions data
            if mentions_by_event is not None:
                m_records = mentions_by_event.get(eid)
                if m_records:
                    merged_rec["mentions"] = [
                        {
                            "mention_time": getattr(m, "mention_time", ""),
                            "source_common_name": getattr(m, "source_common_name", ""),
                            "document_identifier": getattr(m, "document_identifier", ""),
                            "mention_confidence": getattr(m, "mention_confidence", 0),
                            "mention_type": getattr(m, "mention_type", 0),
                        }
                        for m in m_records
                    ]
                else:
                    merged_rec["mentions"] = None

            merged.append(merged_rec)

        logger.info(
            "Merged %d records (events=%d, mentions=%d groups)",
            len(merged),
            len(events_map),
            len(mentions_by_event) if mentions_by_event else 0,
        )
        return merged

    # ── record-level methods ─────────────────────────────────────────

    def filter_relevant(
        self, records: list[dict]
    ) -> list[dict]:
        """Filter records by dual-layer criteria (Layer A: Themes OR Layer B: CAMEO).

        Layer A (GKG Themes): Match GKG V2.8 Themes column against
        ``MACRO_THEME_KEYWORDS``. Any single match retains the record.

        Layer B (CAMEO Codes): Match merged ``cameo_code`` field against
        ``CAMEO_EVENT_CODES_WHITELIST``. Exact prefix match retains the record.

        Records pass if they match Layer A OR Layer B.

        If both whitelists are empty, returns all records.
        """
        if not self._macro_theme_keywords and not self._cameo_whitelist:
            return records

        matched: list[dict] = []
        layer_a_matches = 0
        layer_b_matches = 0

        for rec in records:
            # Layer A: GKG Themes match
            themes_text = (rec.get("themes") or "").lower()
            layer_a_hit = bool(
                self._macro_theme_keywords
                and any(kw.lower() in themes_text for kw in self._macro_theme_keywords)
            )

            # Layer B: CAMEO code match — prefix matching
            # A whitelist parent code (e.g. "162") must match
            # child codes (e.g. "1621", "1622") via startswith.
            cameo_code = (rec.get("cameo_code") or "").strip()
            layer_b_hit = False
            if self._cameo_whitelist and cameo_code:
                for wl_code in self._cameo_whitelist:
                    if cameo_code.startswith(wl_code):
                        layer_b_hit = True
                        break

            if layer_a_hit or layer_b_hit:
                matched.append(rec)
                if layer_a_hit:
                    layer_a_matches += 1
                if layer_b_hit:
                    layer_b_matches += 1

        logger.info(
            "Dual-layer filter: %d → %d records (Layer A=%d, Layer B=%d, OR=%d)",
            len(records),
            len(matched),
            layer_a_matches,
            layer_b_matches,
            len(matched),
        )
        return matched

    def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single merged record to NormalizedEpisode.

        Args:
            record: A dict from merge_event_data().

        Returns:
            NormalizedEpisode with content_scope=MACRO in metadata.
        """
        raw_dt = record.get("valid_at", "")
        valid_at = _parse_gkg_datetime(raw_dt)

        tone_str = record.get("tone", "")
        severity = _map_tone_to_severity(tone_str)

        entities = _parse_entities_from_record(record)

        body = _build_episode_body(record)
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        source_url = record.get("source_url", "") or None

        keywords: list[str] = []
        for theme_str in (record.get("themes", "") or "").split(";"):
            theme = theme_str.strip()
            if theme:
                keywords.append(translate_theme(theme))

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

    # ── fetch orchestration ──────────────────────────────────────────

    async def fetch(self, **kwargs: Any) -> list[dict]:
        """Full fetch pipeline: lastupdate → download_all → merge → filter.

        Falls back to GKG-only mode when all three CSV sources fail.
        Partial failure (e.g. only GKG available) doesn't trigger degraded.
        """
        try:
            # Step 1: Get all three URLs
            events_url, mentions_url, gkg_url = self.fetch_lastupdate()

            # Step 2: Download and parse all three concurrently
            events_records, mentions_by_event, gkg_records = await self.download_all_csvs(
                events_url, mentions_url, gkg_url
            )

            # Check: did we get at least GKG data?
            if not gkg_records:
                logger.warning("GKG records are empty, falling back to cached records")
                return self._last_records

            # Step 3: Merge by GlobalEventID (LEFT JOIN)
            merged_records = self.merge_event_data(
                gkg_records=gkg_records,
                events_records=events_records if events_records else None,
                mentions_by_event=mentions_by_event if mentions_by_event else None,
            )

            # Step 4: Dual-layer filter
            filtered = self.filter_relevant(merged_records)
            self._last_records = filtered
            return filtered

        except GdeltFetchError as exc:
            # All three URLs failed — fall back to GKG-only mode
            logger.warning(
                "GDELT triple-source fetch failed: %s. "
                "Falling back to GKG-only mode with %d cached records.",
                exc,
                len(self._last_records),
            )
            if not self._last_records:
                # No cached records — try GKG-only fallback
                try:
                    gkg_url = self._fallback_fetch_gkg_only()
                    csv_path = self.download_gkg(gkg_url)
                    gkg_records = self.parse_gkg(csv_path)
                    merged = self.merge_event_data(gkg_records)
                    filtered = self.filter_relevant(merged)
                    self._last_records = filtered
                except Exception as fallback_exc:
                    logger.warning(
                        "GKG-only fallback also failed: %s",
                        fallback_exc,
                    )
                    return self._last_records

            # Mark degraded episodes
            for rec in self._last_records:
                rec["_degraded"] = True
            return self._last_records

    def _fallback_fetch_gkg_only(self) -> str:
        """Fallback: fetch only the GKG URL from lastupdate.txt.

        Used when triple-source fetch fails completely. Returns only
        the GKG CSV URL by re-fetching lastupdate.txt.

        Returns:
            GKG CSV URL string.

        Raises:
            GdeltFetchError: If fetching GKG URL fails.
        """
        # Re-fetch just the GKG URL using the original single-URL logic
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
        """Full pipeline with degraded metadata."""
        episodes = await super().run(**kwargs)
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
