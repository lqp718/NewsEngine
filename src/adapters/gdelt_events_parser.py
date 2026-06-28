"""GDELT Events CSV Parser — parse tab-separated Events CSV into EventRecord.

Data flow::

    Events CSV file (61 columns, tab-separated)
            │
            ▼
    load_events_csv(filepath) → list[dict]
            │
            ▼
    parse_event_record(raw) → EventRecord
            │
            ▼
    parse_events_file(filepath) → list[EventRecord]

This module is intentionally separate from ``gdelt_adapter.py``:

- GKG adapter fetches GKG V2 CSV via HTTP and produces NormalizedEpisode
- Events parser reads local Events CSV files and produces EventRecord
  (an intermediate representation, not a cross-layer contract).
- Events data does not require theme filtering or dedup (unlike GKG).

EventRecord uses ``@dataclass(slots=True)`` for memory efficiency during
batch parsing — not Pydantic, because it is an internal intermediate
representation, not a cross-layer data contract.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.adapters.gdelt_codebook import translate_actor
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "EventRecord",
    "load_events_csv",
    "fetch_events_csv",
    "parse_event_record",
    "parse_events",
    "parse_events_file",
]

# ── constants ────────────────────────────────────────────────────────

EXPECTED_COLS: int = 61

# Column index mapping: CSV column index → internal field name.
# Only 12 of the 61 columns are extracted.
COL_MAP: dict[str, int] = {
    "event_id": 0,          # GlobalEventID
    "event_date": 1,        # EventDate (YYYYMMDD)
    "actor1_code": 5,       # Actor1Code
    "actor1_name": 6,       # Actor1Name
    "actor2_code": 15,      # Actor2Code
    "actor2_name": 16,      # Actor2Name
    "cameo_code": 26,       # EventBaseCode
    "goldstein_scale": 30,  # GoldsteinScale
    "avg_tone": 34,         # AvgTone
    "lat": 40,              # Actor1Geo_Lat
    "lon": 41,              # Actor1Geo_Long
    "source_url": 60,       # SOURCEURL
}

# ── EventRecord ──────────────────────────────────────────────────────


@dataclass(slots=True)
class EventRecord:
    """GDELT Events CSV 解析结果的标准化数据结构。

    This is a module-internal intermediate representation — not a
    cross-layer data contract.  The cross-layer contract is
    ``NormalizedEpisode`` (see ``models.py``).

    Attributes:
        event_id: 全局唯一事件 ID (GlobalEventID).
        event_date: 事件发生日期，YYYY-MM-DD 格式.
        actor1_code: Actor1 原始 CAMEO 代码.
        actor1_name: Actor1 名称，经 ``translate_actor()`` 翻译后.
        actor2_code: Actor2 原始 CAMEO 代码.
        actor2_name: Actor2 名称，经 ``translate_actor()`` 翻译后.
        cameo_code: CAMEO 事件基础码 (EventBaseCode).
        goldstein_scale: 冲突-合作评分，范围 -10 ~ +10，可为 None.
        avg_tone: 平均情感评分，范围 -100 ~ +100，可为 None.
        lat: Actor1 地理位置纬度，可为 None.
        lon: Actor1 地理位置经度，可为 None.
        source_url: 新闻来源 URL.
    """

    event_id: str
    event_date: str  # YYYY-MM-DD
    actor1_code: str
    actor1_name: str  # translated by translate_actor()
    actor2_code: str
    actor2_name: str  # translated by translate_actor()
    cameo_code: str
    source_url: str
    goldstein_scale: float | None = None
    avg_tone: float | None = None
    lat: float | None = None
    lon: float | None = None


# ── internal helpers ─────────────────────────────────────────────────


def _safe_float(
    value: str,
    field_name: str,
    row_id: str = "",
) -> float | None:
    """Safely convert a string to float.

    Args:
        value: Raw string value from the CSV.
        field_name: Field name for debug logging.
        row_id: Row identifier for logging context (e.g. event_id).

    Returns:
        Float value, or ``None`` if the value is empty or not convertible.
    """
    if not value or not value.strip():
        return None
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        logger.debug(
            "Cannot convert %s='%s' to float (row: %s)",
            field_name,
            value,
            row_id or "?",
        )
        return None


def _parse_date(raw: str) -> str:
    """Convert GDELT date format YYYYMMDD to YYYY-MM-DD.

    Args:
        raw: Date string in YYYYMMDD format (e.g. ``"20250628"``).

    Returns:
        Date string in YYYY-MM-DD format (e.g. ``"2025-06-28"``).

    Raises:
        ValueError: If the input cannot be parsed as YYYYMMDD.
            Date parsing failures are unrecoverable — a record without
            a valid date is meaningless.
    """
    parsed = datetime.strptime(raw.strip(), "%Y%m%d")
    return parsed.strftime("%Y-%m-%d")


def _build_row_id(row: dict[str, str]) -> str:
    """Build a human-readable row identifier from a raw dict.

    Uses ``GlobalEventID`` (column 0) if available, otherwise returns
    the word ``"unknown"``.
    """
    return row.get("0", "unknown")


# ── public API ───────────────────────────────────────────────────────


def load_events_csv(filepath: str) -> list[dict[str, str]]:
    """Load raw rows from a GDELT Events CSV file.

    Reads a tab-separated CSV file (61 columns, no header) and returns
    a list of dicts keyed by zero-based column index (e.g. ``{"0":
    "1311126102", "1": "20250628", ...}``).

    Empty rows and rows with fewer than ``EXPECTED_COLS`` columns are
    skipped with a warning.  Rows with more columns are truncated.

    Args:
        filepath: Path to the GDELT Events CSV file.

    Returns:
        List of dicts, each representing one raw CSV row with 61
        columns.  Dict keys are string column indices (``"0"`` ..
        ``"60"``).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    rows: list[dict[str, str]] = []
    skipped: int = 0
    col_warnings: int = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f, delimiter="\t")

        for raw_row in reader:
            # Skip empty rows
            if not raw_row or all(col == "" for col in raw_row):
                skipped += 1
                continue

            num_cols = len(raw_row)

            if num_cols < EXPECTED_COLS:
                logger.warning(
                    "Skipping row with %d columns (expected %d): %s",
                    num_cols,
                    EXPECTED_COLS,
                    raw_row[:5],
                )
                skipped += 1
                continue

            if num_cols > EXPECTED_COLS:
                if col_warnings < 3:
                    logger.warning(
                        "Row has %d columns, truncating to %d: %s",
                        num_cols,
                        EXPECTED_COLS,
                        raw_row[:5],
                    )
                col_warnings += 1
                raw_row = raw_row[:EXPECTED_COLS]

            # Build a dict keyed by string column index
            row_dict: dict[str, str] = {
                str(i): raw_row[i] for i in range(EXPECTED_COLS)
            }
            rows.append(row_dict)

    if col_warnings > 3:
        logger.warning(
            "And %d more rows truncated (%d total)",
            col_warnings - 3,
            col_warnings,
        )

    logger.info(
        "Loaded %d rows from %s (skipped %d empty/invalid)",
        len(rows),
        filepath,
        skipped,
    )
    return rows


def parse_event_record(raw: dict[str, str]) -> EventRecord:
    """Convert a single raw row dict into an EventRecord.

    Args:
        raw: A dict keyed by string column index, as returned by
            ``load_events_csv()``.

    Returns:
        An ``EventRecord`` instance with all fields populated.

    Raises:
        ValueError: If the event_date field cannot be parsed as
            YYYYMMDD.  Date format errors are unrecoverable.
        KeyError: If any of the 12 required columns are missing from
            the raw dict.
    """
    event_id = raw[str(COL_MAP["event_id"])].strip()
    event_date = _parse_date(raw[str(COL_MAP["event_date"])])

    actor1_code = raw[str(COL_MAP["actor1_code"])].strip()
    actor1_name = translate_actor(actor1_code)
    actor2_code = raw[str(COL_MAP["actor2_code"])].strip()
    actor2_name = translate_actor(actor2_code)

    cameo_code = raw[str(COL_MAP["cameo_code"])].strip()
    source_url = raw[str(COL_MAP["source_url"])].strip()

    goldstein_scale = _safe_float(
        raw[str(COL_MAP["goldstein_scale"])],
        "goldstein_scale",
        event_id,
    )
    avg_tone = _safe_float(
        raw[str(COL_MAP["avg_tone"])],
        "avg_tone",
        event_id,
    )
    lat = _safe_float(
        raw[str(COL_MAP["lat"])],
        "lat",
        event_id,
    )
    lon = _safe_float(
        raw[str(COL_MAP["lon"])],
        "lon",
        event_id,
    )

    return EventRecord(
        event_id=event_id,
        event_date=event_date,
        actor1_code=actor1_code,
        actor1_name=actor1_name,
        actor2_code=actor2_code,
        actor2_name=actor2_name,
        cameo_code=cameo_code,
        goldstein_scale=goldstein_scale,
        avg_tone=avg_tone,
        lat=lat,
        lon=lon,
        source_url=source_url,
    )


def fetch_events_csv(url: str) -> list[dict[str, str]]:
    """Download and parse a GDELT Events CSV from a URL.

    .. note::

        Currently delegates to ``load_events_csv()`` for local file paths.
        Remote HTTP download support is planned for a future iteration.
        For now, pass a local file path to ``load_events_csv()`` directly.

    Args:
        url: File path (or future HTTP URL) to the Events CSV.

    Returns:
        List of raw row dicts, same format as ``load_events_csv()``.
    """
    return load_events_csv(url)


def parse_events(records: list[dict[str, str]]) -> list[EventRecord]:
    """Parse a list of raw CSV row dicts into EventRecord instances.

    This is a batch convenience wrapper around ``parse_event_record()``.

    Args:
        records: List of raw row dicts from ``load_events_csv()`` or
            ``fetch_events_csv()``.

    Returns:
        List of ``EventRecord`` instances.  Invalid rows are skipped
        with a warning.
    """
    result: list[EventRecord] = []
    errors: int = 0
    for row in records:
        try:
            result.append(parse_event_record(row))
        except (ValueError, KeyError) as e:
            row_id = _build_row_id(row)
            logger.warning(
                "Skipping row (event_id=%s): %s",
                row_id,
                e,
            )
            errors += 1
    if errors:
        logger.info(
            "Parsed %d EventRecord(s) with %d errors",
            len(result),
            errors,
        )
    return result


def parse_events_file(filepath: str) -> list[EventRecord]:
    """Convenience method: load + parse a GDELT Events CSV file.

    Loads the CSV file via ``load_events_csv()`` and parses each row
    via ``parse_event_record()``.  Rows that fail to parse (e.g. bad
    date format) are skipped with a warning.

    Args:
        filepath: Path to the GDELT Events CSV file.

    Returns:
        List of ``EventRecord`` instances.  May be empty if all rows
        were invalid.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    raw_rows = load_events_csv(filepath)
    records: list[EventRecord] = []
    errors: int = 0

    for row in raw_rows:
        try:
            record = parse_event_record(row)
            records.append(record)
        except (ValueError, KeyError) as e:
            row_id = _build_row_id(row)
            logger.warning(
                "Skipping row (event_id=%s): %s",
                row_id,
                e,
            )
            errors += 1

    logger.info(
        "Parsed %d EventRecord(s) from %s (errors: %d)",
        len(records),
        filepath,
        errors,
    )
    return records
