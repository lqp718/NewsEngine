"""GDELT Mentions CSV Parser — parse tab-separated Mentions CSV into MentionRecords.

Data flow::

    Mentions CSV file (16 columns, tab-separated)
            │
            ▼
    fetch_mentions_csv(url) → list[dict]
            │
            ▼
    _parse_mention_record(raw) → MentionRecord
            │
            ▼
    parse_mentions(records) → dict[str, list[MentionRecord]]

This module is intentionally separate from ``gdelt_adapter.py``:

- GKG adapter fetches GKG V2 CSV via HTTP and produces NormalizedEpisode
- Mentions parser reads local Mentions CSV files and produces MentionRecord
  (an intermediate representation, not a cross-layer contract).
- Mentions data provides event propagation coverage (which article mentioned
  which event, confidence, source).

MentionRecord uses ``@dataclass(slots=True)`` for memory efficiency during
batch parsing — not Pydantic, because it is an internal intermediate
representation, not a cross-layer data contract.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "MentionRecord",
    "fetch_mentions_csv",
    "parse_mentions",
]

# ── constants ────────────────────────────────────────────────────────

EXPECTED_COLS: int = 16

# Column index mapping: CSV column index → internal field name.
# Only 6 of the 16 columns are extracted.
COL_MAP: dict[str, int] = {
    "event_id": 0,               # GlobalEventID
    "mention_time": 2,           # MentionTimeDate (YYYYMMDDTHHMMSS)
    "mention_type": 3,           # MentionType (1 = core event mention)
    "source_common_name": 4,     # MentionSourceName (domain)
    "document_identifier": 5,    # DocumentIdentifier (URL)
    "mention_confidence": 11,    # Confidence (0–100)
}

# ── MentionRecord ────────────────────────────────────────────────────


@dataclass(slots=True)
class MentionRecord:
    """GDELT Mentions CSV 解析结果的标准化数据结构。

    This is a module-internal intermediate representation — not a
    cross-layer data contract.  The cross-layer contract is
    ``NormalizedEpisode`` (see ``models.py``).

    Attributes:
        event_id: 全局唯一事件 ID (GlobalEventID).
        mention_time: 报道时间戳，YYYYMMDDTHHMMSS 原始格式.
        source_common_name: 来源域名 (e.g. ``jpost.com``).
        document_identifier: 报道完整 URL.
        mention_confidence: 置信度评分 0–100，解析失败时默认为 0.
        mention_type: 提及类型码 (1 = 核心事件提及).
    """

    event_id: str
    mention_time: str  # YYYYMMDDTHHMMSS
    source_common_name: str
    document_identifier: str
    mention_confidence: int = 0
    mention_type: int = 0


# ── internal helpers ─────────────────────────────────────────────────


def _safe_int(
    value: str,
    field_name: str,
    row_id: str = "",
) -> int:
    """Safely convert a string to int.

    Args:
        value: Raw string value from the CSV.
        field_name: Field name for debug logging.
        row_id: Row identifier for logging context (e.g. event_id).

    Returns:
        Integer value, or ``0`` if the value is empty or not convertible.
    """
    if not value or not value.strip():
        return 0
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        logger.debug(
            "Cannot convert %s='%s' to int (row: %s)",
            field_name,
            value,
            row_id or "?",
        )
        return 0


def _build_row_id(row: dict[str, str]) -> str:
    """Build a human-readable row identifier from a raw dict.

    Uses ``GlobalEventID`` (column 0) if available, otherwise returns
    the word ``"unknown"``.
    """
    return row.get("0", "unknown")


def _parse_mention_record(raw: dict[str, str]) -> MentionRecord:
    """Convert a single raw row dict into a MentionRecord.

    Args:
        raw: A dict keyed by string column index, as returned by
            ``fetch_mentions_csv()``.

    Returns:
        A ``MentionRecord`` instance with all fields populated.

    Raises:
        KeyError: If any of the 6 required columns are missing from
            the raw dict.
    """
    event_id = raw[str(COL_MAP["event_id"])].strip()
    mention_time = raw[str(COL_MAP["mention_time"])].strip()
    source_common_name = raw[str(COL_MAP["source_common_name"])].strip()
    document_identifier = raw[str(COL_MAP["document_identifier"])].strip()

    mention_confidence = _safe_int(
        raw[str(COL_MAP["mention_confidence"])],
        "mention_confidence",
        event_id,
    )
    mention_type = _safe_int(
        raw[str(COL_MAP["mention_type"])],
        "mention_type",
        event_id,
    )

    return MentionRecord(
        event_id=event_id,
        mention_time=mention_time,
        source_common_name=source_common_name,
        document_identifier=document_identifier,
        mention_confidence=mention_confidence,
        mention_type=mention_type,
    )


# ── public API ───────────────────────────────────────────────────────


def fetch_mentions_csv(url: str) -> list[dict[str, str]]:
    """Load raw rows from a GDELT Mentions CSV file.

    Reads a tab-separated CSV file (16 columns, no header) and returns
    a list of dicts keyed by zero-based column index (e.g. ``{"0":
    "1311126102", "1": "20250628", ...}``).

    Empty rows and rows with fewer than ``EXPECTED_COLS`` columns are
    skipped with a warning.  Rows with more columns are truncated.

    .. note::

        Currently accepts a ``url`` parameter but only reads local file
        paths (same contract as ``gdelt_events_parser.fetch_events_csv``).
        Remote HTTP download support is planned for a future iteration.

    Args:
        url: Path (or future HTTP URL) to the Mentions CSV file.

    Returns:
        List of dicts, each representing one raw CSV row with 16
        columns.  Dict keys are string column indices (``"0"`` ..
        ``"15"``).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    rows: list[dict[str, str]] = []
    skipped: int = 0
    col_warnings: int = 0

    with open(url, "r", encoding="utf-8", errors="replace") as f:
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
        url,
        skipped,
    )
    return rows


def parse_mentions(
    records: list[dict[str, str]],
) -> dict[str, list[MentionRecord]]:
    """Parse a list of raw CSV row dicts into MentionRecords grouped by event_id.

    Groups records by ``event_id`` for O(1) lookup by downstream consumers
    (e.g. ``mentions_by_event.get(event_id, [])``).

    Args:
        records: List of raw row dicts from ``fetch_mentions_csv()``.

    Returns:
        A dict mapping ``event_id`` (str) to a list of ``MentionRecord``
        instances.  Rows that fail to parse (e.g. empty event_id) are
        skipped with a warning.
    """
    result: dict[str, list[MentionRecord]] = defaultdict(list)
    errors: int = 0

    for row in records:
        try:
            record = _parse_mention_record(row)
            if not record.event_id:
                logger.warning(
                    "Skipping row with empty event_id",
                )
                errors += 1
                continue
            result[record.event_id].append(record)
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
            "Parsed %d event groups with %d mention(s) total (errors: %d)",
            len(result),
            sum(len(v) for v in result.values()),
            errors,
        )
    else:
        logger.info(
            "Parsed %d event groups with %d mention(s) total",
            len(result),
            sum(len(v) for v in result.values()),
        )

    return dict(result)
