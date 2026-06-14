"""GDELT CSV Adapter — fetch, parse, and normalize GDELT GKG V2 data.

Data flow::
    fetch_lastupdate() → download_gkg() → parse_gkg() → filter_relevant()
    → normalize() → dedup()

Only the HTTP CSV data plane (http://data.gdeltproject.org/) is used.
HTTPS is avoided because the HTTPS endpoints are blocked by the GFW.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Literal

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.adapters.base import BaseAdapter
from src.adapters.models import (
    EntityItem,
    NormalizedEpisode,
    Severity,
)
from src.adapters.macro_themes import MACRO_THEME_KEYWORDS
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
    """Raised when downloading the GKG CSV zip fails after all retries."""


def _map_tone_to_severity(tone_str: str | None) -> Severity:
    """Map GKG V2.14 Tone value to severity.

    Mapping rules:
        >  5.0  → "low"
        -5.0 ~ 5.0 → "medium"
        -15.0 ~ -5.0 → "high"
        < -15.0 → "critical"
        invalid / None → "medium"
    """
    if tone_str is None or tone_str.strip() == "":
        logger.warning("Empty tone value, defaulting to medium")
        return "medium"
    try:
        tone = float(tone_str.strip())
    except (ValueError, TypeError):
        logger.warning("Invalid tone value '%s', defaulting to medium", tone_str)
        return "medium"

    if tone > 5.0:
        return "low"
    if tone >= -5.0:  # -5.0 ~ 5.0 (inclusive)
        return "medium"
    if tone >= -15.0:  # -15.0 ~ -5.0 (exclusive upper bound -5.0)
        return "high"
    # < -15.0
    return "critical"


def _parse_location(location_str: str) -> str:
    """Clean a GKG V2.7 Location field, stripping coordinate metadata.

    Input:  "#1#2#Beijing,Beijing,China#CN#CN|#VNM"
    Output: "Beijing, China"

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

        # Try to extract country from second code or from name itself
        # If name contains comma-separated parts like "Beijing,Beijing,China"
        # We want just the city and country (first + last meaningful)
        name_parts = [p.strip() for p in name.split(",") if p.strip()]
        if len(name_parts) >= 3:
            # e.g. "Beijing,Beijing,China" → "Beijing, China"
            cleaned = f"{name_parts[0]}, {name_parts[-1]}"
        else:
            cleaned = ", ".join(name_parts)

        cleaned_parts.append(cleaned)

    return "; ".join(cleaned_parts) if cleaned_parts else location_str


def _parse_entities_from_record(record: dict) -> list[EntityItem]:
    """Extract EntityItem list from a parsed GKG record."""
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

    # V2.8 — Themes
    themes_raw = record.get("themes", "") or ""
    for name in themes_raw.split(";"):
        name = name.strip()
        if name:
            entities.append(EntityItem(type="theme", name=name))

    return entities


def _build_episode_body(record: dict) -> str:
    """Build a human-readable episode body from a GKG record."""
    parts: list[str] = []

    themes = record.get("themes", "") or ""
    parts.append(f"Themes: {themes}")

    persons = record.get("persons", "") or ""
    if persons:
        parts.append(f"Persons: {persons}")

    organizations = record.get("organizations", "") or ""
    if organizations:
        parts.append(f"Organizations: {organizations}")

    locations = record.get("locations", "") or ""
    if locations:
        # Clean locations for readability
        loc_cleaned = "; ".join(
            _parse_location(l) for l in locations.split(";") if l.strip()
        )
        if loc_cleaned:
            parts.append(f"Locations: {loc_cleaned}")

    source_url = record.get("source_url", "") or ""
    if source_url:
        parts.append(f"Source: {source_url}")

    return " | ".join(parts)


class GdeltAdapter(BaseAdapter):
    """GDELT GKG V2 CSV adapter.

    Fetches the latest GKG CSV from data.gdeltproject.org, parses the
    27-column tab-separated format, filters by 19 core macro themes (OR
    matching), and normalises to NormalizedEpisode with content_scope=MACRO.

    V2.2: Replaced ticker_whitelist filtering with macro theme filtering.
    No longer receives or uses ticker whitelist.
    """

    def __init__(
        self,
        macro_theme_keywords: set[str] | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        dedup_cache: set[str] | None = None,
    ) -> None:
        super().__init__(dedup_cache=dedup_cache)
        self._macro_theme_keywords = macro_theme_keywords or MACRO_THEME_KEYWORDS
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

    def fetch_lastupdate(self) -> str:
        """Fetch the latest GKG CSV URL from lastupdate.txt.

        ``lastupdate.txt`` contains three lines (events, mentions, GKG).
        The third line is the GKG CSV zip URL.

        Returns the URL string for the GKG CSV zip file.

        Raises:
            GdeltFetchError: If all retry attempts fail.
        """
        retry_dec = self._make_retry_decorator()

        @retry_dec
        def _do_fetch() -> str:
            logger.info("Fetching lastupdate.txt from %s", self._lastupdate_url)
            resp = requests.get(self._lastupdate_url, timeout=30)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) < 3:
                raise GdeltFetchError(
                    f"Expected ≥3 lines, got {len(lines)}: {resp.text[:200]}"
                )
            # Third line (index 2) is the GKG CSV line
            gkg_line = lines[2].strip()
            parts = gkg_line.split()
            if len(parts) < 3:
                raise GdeltFetchError(
                    f"Expected ≥3 fields on GKG line, got {len(parts)}: {gkg_line[:200]}"
                )
            url = parts[2].strip()  # URL is the 3rd field on each line
            if not url.startswith("http"):
                raise GdeltFetchError(f"Invalid GKG URL: {url}")
            logger.info("Latest GKG CSV URL: %s", url)
            return url

        try:
            return _do_fetch()
        except Exception as exc:
            raise GdeltFetchError(
                f"Failed to fetch lastupdate.txt after {self.max_retries} retries: {exc}"
            ) from exc

    def download_gkg(self, csv_url: str) -> str:
        """Download the GKG CSV zip file and extract the CSV.

        Args:
            csv_url: The URL of the .csv.zip file.

        Returns:
            Path to the extracted CSV file.

        Raises:
            GdeltDownloadError: If all retry attempts fail.

        Note:
            Each tenacity retry creates a fresh TemporaryDirectory so that
            a failed attempt's cleanup does not invalidate subsequent retries.
        """
        retry_dec = self._make_retry_decorator()

        @retry_dec
        def _do_download() -> str:
            # Create a new temp dir per retry attempt so cleanup from a failed
            # attempt does not affect the next retry's temp dir.
            _tmp = tempfile.TemporaryDirectory()
            try:
                logger.info("Downloading GKG CSV from %s", csv_url)
                resp = requests.get(csv_url, timeout=60)
                resp.raise_for_status()
                zip_path = os.path.join(_tmp.name, "gkg.zip")
                with open(zip_path, "wb") as f:
                    f.write(resp.content)
                # Extract
                with zipfile.ZipFile(zip_path, "r") as zf:
                    csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
                    if not csv_names:
                        raise GdeltDownloadError(
                            f"No CSV found in zip archive: {zf.namelist()}"
                        )
                    csv_name = csv_names[0]
                    zf.extract(csv_name, _tmp.name)
                    csv_path = os.path.join(_tmp.name, csv_name)
                    logger.info("Extracted CSV: %s", csv_path)
                # Keep a reference to prevent GC before parse_gkg reads the file
                self._download_tmp_dir = _tmp  # type: ignore[attr-defined]
                return csv_path
            except Exception:
                _tmp.cleanup()
                raise

        try:
            return _do_download()
        except Exception as exc:
            raise GdeltDownloadError(
                f"Failed to download GKG CSV after {self.max_retries} retries: {exc}"
            ) from exc

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

    # ── record-level methods ─────────────────────────────────────────

    def filter_relevant(
        self, records: list[dict]
    ) -> list[dict]:
        """Filter records by 19 core macro themes (OR matching).

        V2.2: Replaced ticker_whitelist filtering. Matches GKG V2.8 Themes
        column against the 19 core financial theme keywords. Any single
        match retains the record.

        If ``_macro_theme_keywords`` is empty, returns all records.
        """
        if not self._macro_theme_keywords:
            return records

        matched: list[dict] = []
        for rec in records:
            themes_text = (rec.get("themes") or "").lower()
            if any(kw.lower() in themes_text for kw in self._macro_theme_keywords):
                matched.append(rec)

        logger.info(
            "GDELT theme filter: %d → %d records (themes=%s)",
            len(records),
            len(matched),
            "OR".join(sorted(self._macro_theme_keywords))[:80]
        )
        return matched

    def normalize(self, record: dict) -> NormalizedEpisode:
        """Convert a single parsed GKG record to NormalizedEpisode.

        Args:
            record: A dict from parse_gkg().

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
                keywords.append(theme)

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
        """Full fetch pipeline: lastupdate → download → parse → filter.

        Falls back to last successful records when download fails.
        """
        try:
            csv_url = self.fetch_lastupdate()
            csv_path = self.download_gkg(csv_url)
            records = self.parse_gkg(csv_path)
            records = self.filter_relevant(records)
            self._last_records = records
            return records
        except (GdeltFetchError, GdeltDownloadError) as exc:
            logger.warning(
                "GDELT fetch failed: %s. Falling back to %d cached records.",
                exc,
                len(self._last_records),
            )
            # Mark degraded episodes
            for rec in self._last_records:
                rec["_degraded"] = True
            return self._last_records

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



