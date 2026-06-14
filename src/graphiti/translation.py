"""Shared translation layer — Neo4j Episodic/Entity records → business models.

V2.1 design decision: translation logic lives in graphiti layer, not in
API routes or ingestion. Both `api/routers/events.py` and
`ingestion/briefing_aggregator.py` consume these shared functions.

Zero dependency on `api/` or `ingestion/` modules — only depends on
`graphiti/entity_types.py` type constants.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.utils.time_utils import now_hkt, to_iso8601

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_WEIGHT: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}
"""Sort weight for severity levels. Higher = more severe."""

SEVERITY_DEFAULT: str = "medium"
"""Default severity when none is set on the Episodic node."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def severity_sort_weight(severity: str) -> int:
    """Map severity string to numeric weight for sorting."""
    return SEVERITY_WEIGHT.get(severity.lower(), 0)


# ---------------------------------------------------------------------------
# Entity label detection
# ---------------------------------------------------------------------------

LABEL_TYPE_MAP: dict[str, str] = {
    "SECTOR": "sector",
    "STOCK": "stock",
    "COUNTRY": "country",
    "POLICY": "policy",
}
"""Mapping from uppercase Entity label → business entity type string."""


def entity_type_from_labels(labels: list[str], ticker: str | None) -> str:
    """Infer business entity type from Neo4j Entity node labels.

    Args:
        labels: Node labels from Neo4j (e.g. ['Entity', 'Stock'])
        ticker: Optional stock ticker (used as fallback hint)

    Returns:
        One of: 'stock', 'sector', 'country', 'policy', 'unknown'
    """
    label_set = {l.upper() for l in labels}
    for label, entity_type in LABEL_TYPE_MAP.items():
        if label in label_set:
            return entity_type
    # Fallback: if ticker is present, treat as stock
    if ticker:
        return "stock"
    return "unknown"


# ---------------------------------------------------------------------------
# EventItem translation — used by API routes (events.py)
# ---------------------------------------------------------------------------


def translate_episode_to_event(
    episode_record: dict[str, Any],
    entity_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a Neo4j Episodic node + related entities to an EventItem dict.

    The graphiti EpisodicNode stores:
      - uuid, name, body, source, reference_time, created_at, group_id
    EntityNode stores:
      - uuid, name, entity_type, attributes (JSON)
    EntityEdge (RELATES_TO) stores:
      - name (AFFECTS / BELONGS_TO / …), fact, attributes (JSON)

    Returns a dict compatible with ``EventItem(**result)`` constructor.
    """
    e = episode_record.get("e", episode_record)

    # ---- event_id ----
    event_id = e.get("event_id") or e.get("uuid") or e.get("name", "unknown")
    if str(event_id).startswith("evt-"):
        pass  # already in correct format
    else:
        _short_id = str(event_id).replace("-", "")[:6]
        _now = now_hkt()
        event_id = f"evt-{_now.strftime('%Y%m%d')}-{_short_id[:3].upper()}"

    # ---- title & summary ----
    body: str = e.get("content") or e.get("body") or e.get("title", "")
    lines = body.split("\n") if body else []
    title = ""
    rest_lines: list[str] = []
    found_title = False
    for line in lines:
        stripped = line.strip()
        if not found_title and stripped:
            title = stripped
            found_title = True
        elif found_title:
            rest_lines.append(line)
    if not title:
        title = e.get("name", str(e.get("entity_name", "Untitled Event")))
    if len(title) > 200:
        title = title[:200]

    # summary from remaining content
    summary: str | None = None
    body_summary = "\n".join(rest_lines).strip() if rest_lines else ""
    if len(body_summary) > 10:
        summary = body_summary[:500]

    # ---- severity ----
    # Graphiti EpisodicNode does not have a native severity property.
    # Default to "medium". Severity defaults to medium; LLM enrichment deferred to L-4
    severity_raw: str = e.get("severity", SEVERITY_DEFAULT)
    if not isinstance(severity_raw, str) or severity_raw.lower() not in SEVERITY_WEIGHT:
        severity_raw = SEVERITY_DEFAULT

    # ---- timestamps ----
    ref_time = e.get("valid_at") or e.get("reference_time") or e.get("first_seen") or now_hkt()
    created = e.get("created_at") or ref_time
    if isinstance(ref_time, datetime):
        first_seen = to_iso8601(ref_time)
        if isinstance(created, datetime):
            last_updated = to_iso8601(created)
        else:
            last_updated = first_seen
    else:
        first_seen = to_iso8601(now_hkt())
        last_updated = first_seen

    # ---- source_count, source_urls ----
    source_count: int = int(e.get("source_count", 0))
    source_urls: list[str] | None = e.get("source_urls") or None

    # ---- keywords ----
    keywords: list[str] = list(e.get("keywords", []))

    # ---- entities ----
    entities: list[dict[str, Any]] = []
    if entity_records:
        for rec in entity_records:
            en = rec.get("ent", rec)
            entities.append(_translate_entity_to_item_dict(en))

    # ---- relations ----
    relations: list[dict[str, str]] | None = None

    return {
        "event_id": event_id,
        "title": title,
        "summary": summary,
        "severity": severity_raw.lower(),
        "first_seen": first_seen,
        "last_updated": last_updated,
        "source_count": source_count,
        "source_urls": source_urls,
        "keywords": keywords,
        "entities": entities,
        "relations": relations,
    }


# ---------------------------------------------------------------------------
# Entity item translation
# ---------------------------------------------------------------------------


def _translate_entity_to_item_dict(entity_node: dict[str, Any]) -> dict[str, Any]:
    """Convert a single Neo4j Entity node dict to EventEntityItem-compatible dict.

    Returns a dict suitable for ``EventEntityItem(**result)``.
    """
    en_labels: list = entity_node.get("labels") or []
    en_name: str = entity_node.get("entity_name") or entity_node.get("name", "")
    ticker: str | None = entity_node.get("ticker") or None
    ent_type: str = entity_type_from_labels(en_labels, ticker)

    status: str | None = None
    if ent_type == "policy":
        status = entity_node.get("status", "rumor")

    return {
        "type": ent_type,
        "ticker": ticker,
        "name": en_name,
        "status": status,
    }


def translate_entities_to_items(
    entity_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert a list of Neo4j Entity record dicts to EventEntityItem-compatible dicts.

    Args:
        entity_records: Each dict should have key 'ent' or direct Entity node keys.

    Returns:
        List of dicts suitable for ``EventEntityItem(**item)``.
    """
    items: list[dict[str, Any]] = []
    for rec in entity_records:
        en = rec.get("ent", rec)
        items.append(_translate_entity_to_item_dict(en))
    return items


# ---------------------------------------------------------------------------
# Briefing input translation — used by briefing_aggregator.py
# ---------------------------------------------------------------------------


def translate_episode_to_briefing_input(
    episode_record: dict[str, Any],
    entity_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a Neo4j Episodic node to a sector_briefing aggregation input dict.

    The output dict matches the field names expected by
    ``SectorBriefingAggregator._build_user_prompt()``:
      event_id, title, severity, first_seen, last_updated, source_count,
      summary, keywords, affected_tickers, affected_stocks

    Args:
        episode_record: Episodic node data from Neo4j
        entity_records: Related Entity node data (for ticker/stock extraction)

    Returns:
        A flat dict suitable for briefing aggregation pipeline.
    """
    e = episode_record.get("e", episode_record)

    # ---- event_id ----
    event_id = e.get("event_id") or e.get("uuid") or e.get("name", "unknown")
    if str(event_id).startswith("evt-"):
        pass
    else:
        _short_id = str(event_id).replace("-", "")[:6]
        _now = now_hkt()
        event_id = f"evt-{_now.strftime('%Y%m%d')}-{_short_id[:3].upper()}"

    # ---- title ----
    body: str = e.get("content") or e.get("body") or e.get("title", "")
    lines = body.split("\n") if body else []
    title = ""
    found_title = False
    for line in lines:
        stripped = line.strip()
        if not found_title and stripped:
            title = stripped
            found_title = True
    if not title:
        title = e.get("name", str(e.get("entity_name", "Untitled Event")))
    if len(title) > 200:
        title = title[:200]

    # ---- severity ----
    severity_raw: str = e.get("severity", SEVERITY_DEFAULT)
    if not isinstance(severity_raw, str) or severity_raw.lower() not in SEVERITY_WEIGHT:
        severity_raw = SEVERITY_DEFAULT

    # ---- timestamps ----
    first_seen = e.get("valid_at") or e.get("reference_time") or e.get("first_seen")
    last_updated = e.get("created_at") or first_seen
    if not isinstance(first_seen, datetime):
        first_seen = now_hkt()
    if not isinstance(last_updated, datetime):
        last_updated = first_seen

    # ---- source_count, summary, keywords ----
    source_count: int = int(e.get("source_count", 0))
    summary: str | None = None
    if len(lines) > 1:
        summary = "\n".join(lines[1:]).strip()[:500]
    keywords: list[str] = list(e.get("keywords", []))

    # ---- affected_tickers, affected_stocks ----
    affected_tickers: list[str] = []
    affected_stocks: list[str] = []
    if entity_records:
        for rec in entity_records:
            en = rec.get("ent", rec)
            ticker = en.get("ticker")
            if ticker:
                affected_tickers.append(str(ticker))
            name = en.get("entity_name") or en.get("name", "")
            if name:
                affected_stocks.append(name)

    return {
        "event_id": event_id,
        "title": title,
        "severity": severity_raw.lower(),
        "first_seen": first_seen,
        "last_updated": last_updated,
        "source_count": source_count,
        "summary": summary,
        "keywords": keywords,
        "affected_tickers": list(dict.fromkeys(affected_tickers)),
        "affected_stocks": list(dict.fromkeys(affected_stocks)),
    }


__all__ = [
    "SEVERITY_WEIGHT",
    "SEVERITY_DEFAULT",
    "severity_sort_weight",
    "translate_episode_to_event",
    "translate_entities_to_items",
    "translate_episode_to_briefing_input",
    "entity_type_from_labels",
    "LABEL_TYPE_MAP",
]
