"""Event query endpoints — /api/events/*

Implements N4-2 through N4-5:
- N4-2: GET /api/events/active — 当前活跃事件
- N4-3: GET /api/events/entity/:ticker — 某股票相关事件
- N4-4: GET /api/events/sector/:name — 行业事件聚合
- N4-5: GET /api/events/risk-summary — 风险摘要（mock, 待 N4-9 LLM 聚合）

All endpoints query the Neo4j knowledge graph (graphiti Episodic/Entity nodes)
and return responses conforming to the Pydantic models defined in models.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Path
from neo4j import Driver

from src.api.models import (
    ActiveEventsResponse,
    EntityEventsResponse,
    EntityEventSummary,
    EventItem,
    EventEntityItem,
    EventRelationItem,
    FreshnessInfo,
    RiskSummaryResponse,
    SectorEventsResponse,
    SectorStatistics,
    TopRiskItem,
)
from src.api.deps import get_neo4j_driver, get_settings
from src.core.config import Settings
from src.utils.time_utils import now_hkt, to_iso8601
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/events",
    tags=["Events"],
)

# ---------------------------------------------------------------------------
# Helper — severity sort weight
# ---------------------------------------------------------------------------

_SEVERITY_WEIGHT: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}


def _severity_sort_weight(severity: str) -> int:
    """Map severity string to numeric weight for sorting."""
    return _SEVERITY_WEIGHT.get(severity.lower(), 0)


# ---------------------------------------------------------------------------
# Helper — map EpisodicNode + related EntityNode data to EventItem
# ---------------------------------------------------------------------------


def _episode_to_event_item(
    episode_record: dict[str, Any],
    entity_records: list[dict[str, Any]] | None = None,
) -> EventItem:
    """Convert a Neo4j Episodic node + related entities to an EventItem.

    The graphiti EpisodicNode stores:
      - uuid, name, body, source, reference_time, created_at, group_id
    EntityNode stores:
      - uuid, name, entity_type, attributes (JSON)
    EntityEdge (RELATES_TO) stores:
      - name (AFFECTS / BELONGS_TO / …), fact, attributes (JSON)
    """

    e = episode_record.get("e", episode_record)

    # ---- event_id ----
    event_id = e.get("event_id") or e.get("uuid") or e.get("name", "unknown")
    if str(event_id).startswith("evt-"):
        pass  # already in correct format
    else:
        # Derive from name or uuid — use first chars as identifier
        short_id = str(event_id).replace("-", "")[:6]
        now = now_hkt()
        event_id = f"evt-{now.strftime('%Y%m%d')}-{short_id[:3].upper()}"

    # ---- title & summary ----
    body: str = e.get("content") or e.get("body") or e.get("title", "")
    # Find first non-empty line for title
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
    # Default to "medium". Proper severity will be available after N4-9
    # LLM enrichment which writes severity to episode metadata.
    severity_raw: str = "medium"
    if severity_raw.lower() not in _SEVERITY_WEIGHT:
        severity_raw = "medium"

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
    entities: list[EventEntityItem] = []
    if entity_records:
        for rec in entity_records:
            en = rec.get("ent", rec)

            # Entity labels: graphiti EntityNode has 'labels' property (list)
            # e.g. ['Entity', 'Stock'] or ['Entity', 'Sector']
            # Also check for entity_name, name, ticker, sector, type properties
            en_labels: list = en.get("labels") or []
            en_name: str = en.get("entity_name") or en.get("name", "")
            ticker: str | None = en.get("ticker") or None
            ent_type: str = "stock"  # default

            # Determine entity type from labels array
            label_set = {l.upper() for l in en_labels}
            if "SECTOR" in label_set:
                ent_type = "sector"
            elif "COUNTRY" in label_set:
                ent_type = "country"
            elif "POLICY" in label_set:
                ent_type = "policy"
            # Stock is the default — also check if ticker is present
            elif ticker:
                ent_type = "stock"
            # Additional fallback: check type property
            elif en.get("type"):
                type_val = str(en.get("type", "")).lower()
                if type_val in ("stock", "sector", "country", "policy"):
                    ent_type = type_val

            # Policy status
            status: str | None = None
            if ent_type == "policy":
                status = en.get("status", "rumor")

            entities.append(
                EventEntityItem(
                    type=ent_type,
                    ticker=ticker,
                    name=en_name,
                    status=status,
                )
            )

    # ---- relations ----
    relations: list[EventRelationItem] | None = None

    return EventItem(
        event_id=event_id,
        title=title,
        summary=summary,
        severity=severity_raw.lower(),
        first_seen=first_seen,
        last_updated=last_updated,
        source_count=source_count,
        source_urls=source_urls,
        keywords=keywords,
        entities=entities,
        relations=relations,
    )


# ---------------------------------------------------------------------------
# Helper — query Neo4j with fallback for connectivity
# ---------------------------------------------------------------------------


def _query_neo4j(
    driver: Driver,
    cypher: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a Cypher query against Neo4j and return records.

    Raises HTTPException(503) on connection errors.
    Raises HTTPException(500) on query execution errors.
    """
    try:
        # Verify connectivity first
        driver.verify_connectivity()
    except Exception as exc:
        logger.error("Neo4j unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={"error": "Neo4j unavailable", "detail": str(exc)},
        )

    try:
        records: list[dict[str, Any]] = []
        with driver.session() as session:
            result = session.run(cypher, params or {})
            for record in result:
                records.append(record.data())
        return records
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Neo4j query error: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal error", "detail": str(exc)},
        )


# ---------------------------------------------------------------------------
# N4-2: GET /api/events/active
# ---------------------------------------------------------------------------


@router.get("/active", response_model=ActiveEventsResponse)
async def get_active_events(
    limit: int = Query(default=50, ge=1, le=200, description="Max results"),
    min_severity: str = Query(
        default="medium",
        description="Minimum severity filter: low / medium / high / critical",
    ),
    sector: str | None = Query(
        default=None,
        description="Optional sector filter (Chinese name)",
    ),
    neo4j_driver: Driver = Depends(get_neo4j_driver),
) -> ActiveEventsResponse:
    """Return currently active events sorted by severity desc + last_updated desc.

    Queries Neo4j Episodic nodes (graphiti episodes) and their linked Entity
    nodes via the entity_edges array. Events are ordered by valid_at DESC
    and created_at DESC. Severity is derived from episode content analysis.

    Neo4j 不可用返回 503, 内部错误返回 500.
    """
    logger.info(
        "GET /api/events/active — limit=%d, min_severity=%s, sector=%s",
        limit,
        min_severity,
        sector,
    )

    try:
        records = _query_neo4j(
            neo4j_driver,
            _build_active_events_query(),
            params={
                "limit": limit,
                "sector": sector,
            },
        )
    except HTTPException:
        raise

    events: list[EventItem] = []
    for rec in records:
        entity_records = rec.get("entities", [])
        event = _episode_to_event_item(rec, entity_records)
        events.append(event)

    # Sort by severity desc + last_updated desc (in-memory safety net)
    now = now_hkt()
    events.sort(
        key=lambda ev: (
            _severity_sort_weight(ev.severity),
            ev.last_updated or to_iso8601(now),
        ),
        reverse=True,
    )

    # Build FreshnessInfo from data source timestamps (or placeholder)
    now_str = to_iso8601(now)
    freshness = FreshnessInfo(
        gdelt_last_update=now_str,
        rss_last_update=now_str,
        akshare_last_update=now_str,
    )

    logger.info("GET /api/events/active — returned %d events", len(events))
    return ActiveEventsResponse(
        events=events[:limit],
        total=len(events),
        freshness=freshness,
    )


# ---------------------------------------------------------------------------
# N4-3: GET /api/events/entity/:ticker
# ---------------------------------------------------------------------------


def _build_active_events_query() -> str:
    """Build Cypher query for active events.

    Graphiti entity_edges on EpisodicNode stores RELATES_TO relationship UUIDs.
    Each RELATES_TO connects two Entity nodes. We collect both sides.
    Filters by optional sector. Sorts by valid_at + created_at descending.
    """
    return """
        MATCH (e:Episodic)
        OPTIONAL MATCH (src:Entity)-[rel:RELATES_TO]-(tgt:Entity)
        WHERE rel.uuid IN e.entity_edges
        WITH e, collect(DISTINCT src) + collect(DISTINCT tgt) AS entities
        WHERE ($sector IS NULL
               OR ANY(ent IN entities
                      WHERE (ent.sector IS NOT NULL AND ent.sector CONTAINS $sector)
                         OR (ent.name CONTAINS $sector)
                         OR (ent.entity_name CONTAINS $sector)))
        RETURN e, entities
        ORDER BY e.valid_at DESC, e.created_at DESC
        LIMIT $limit
    """


def _build_entity_events_query() -> str:
    """Build Cypher to find events associated with a stock ticker.

    Finds Entity by ticker → RELATES_TO → Episodic via entity_edges.
    """
    return """
        MATCH (ent:Entity)
        WHERE ent.ticker = $ticker
        OPTIONAL MATCH (ent)-[rel:RELATES_TO]-(other_entity:Entity)
        OPTIONAL MATCH (ep:Episodic)
        WHERE rel.uuid IN ep.entity_edges
        OPTIONAL MATCH (ep)-[other_rel:RELATES_TO]-(other_entity2:Entity)
        WHERE ep IS NOT NULL AND other_rel.uuid IN ep.entity_edges
        RETURN ep, collect(DISTINCT other_entity) + collect(DISTINCT other_entity2) AS entities
    """


@router.get("/entity/{ticker}", response_model=EntityEventsResponse)
async def get_entity_events(
    ticker: str = Path(..., description="Stock ticker, e.g. 0700.HK"),
    neo4j_driver: Driver = Depends(get_neo4j_driver),
) -> EntityEventsResponse:
    """Return events associated with a specific stock ticker.

    ticker 格式: 0700.HK（不是 HK.00700）。
    Queries Neo4j via Entity ticker → EntityEdge → Episodic.

    ticker not found 返回 404, Neo4j 不可用返回 503, 内部错误返回 500。
    """
    logger.info("GET /api/events/entity/%s", ticker)

    try:
        records = _query_neo4j(
            neo4j_driver,
            _build_entity_events_query(),
            params={"ticker": ticker},
        )
    except HTTPException:
        raise

    events: list[EventItem] = []
    for rec in records:
        ep = rec.get("ep")
        if ep is None:
            continue
        entity_records = rec.get("entities", [])
        event = _episode_to_event_item({"e": ep}, entity_records)
        events.append(event)

    if not events:
        logger.warning("Ticker not found: %s", ticker)
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Ticker not found",
                "detail": f"No events for {ticker}",
            },
        )

    total_events = len(events)
    sev_sum = sum(_severity_sort_weight(ev.severity) for ev in events)
    avg_severity_num = sev_sum / total_events if total_events > 0 else 2

    if avg_severity_num >= 3.5:
        avg_severity_label = "critical"
        risk_level = "CRITICAL"
    elif avg_severity_num >= 2.5:
        avg_severity_label = "high"
        risk_level = "HIGH"
    elif avg_severity_num >= 1.5:
        avg_severity_label = "medium"
        risk_level = "MEDIUM"
    else:
        avg_severity_label = "low"
        risk_level = "LOW"

    news_sentiment_score = max(0.0, min(1.0, 1.0 - (avg_severity_num - 1.0) / 3.0))

    logger.info(
        "GET /api/events/entity/%s — %d events, risk=%s",
        ticker,
        total_events,
        risk_level,
    )
    return EntityEventsResponse(
        ticker=ticker,
        events=events,
        summary=EntityEventSummary(
            total_events=total_events,
            avg_severity=avg_severity_label,
            risk_level=risk_level,
            news_sentiment_score=round(news_sentiment_score, 2),
        ),
    )


def _build_sector_events_query() -> str:
    """Build Cypher to find events in a specific sector.

    Finds sector by Entity name, traces Stock entities in that sector,
    then finds RELATES_TO relationships and linked Episodic nodes.
    """
    return """
        MATCH (sector_ent:Entity)
        WHERE sector_ent.name = $sector_name
           OR sector_ent.entity_name = $sector_name
        OPTIONAL MATCH (stock:Entity)
        WHERE stock.sector = sector_ent.name
           OR stock.sector = sector_ent.entity_name
        OPTIONAL MATCH (stock)-[rel:RELATES_TO]-(other:Entity)
        OPTIONAL MATCH (ep:Episodic)
        WHERE rel.uuid IN ep.entity_edges
        OPTIONAL MATCH (ep)-[other_rel:RELATES_TO]-(all_ents:Entity)
        WHERE ep IS NOT NULL AND other_rel.uuid IN ep.entity_edges
        WITH ep, collect(DISTINCT all_ents) AS entities,
             count(DISTINCT stock) AS ticker_count
        WHERE ep IS NOT NULL
        RETURN ep, entities, ticker_count
        ORDER BY ep.valid_at DESC, ep.created_at DESC
    """


def _build_high_risk_query() -> str:
    """Build Cypher for recent episodes (risk summary)."""
    return """
        MATCH (e:Episodic)
        OPTIONAL MATCH (src:Entity)-[rel:RELATES_TO]-(tgt:Entity)
        WHERE rel.uuid IN e.entity_edges
        RETURN e, collect(DISTINCT src) + collect(DISTINCT tgt) AS entities
        ORDER BY e.created_at DESC
        LIMIT 10
    """


@router.get("/sector/{sector_name}", response_model=SectorEventsResponse)
async def get_sector_events(
    sector_name: str = Path(..., description="Sector name in Chinese, e.g. 互联网平台"),
    neo4j_driver: Driver = Depends(get_neo4j_driver),
) -> SectorEventsResponse:
    """Return events aggregated by sector (Chinese name).

    sector_briefing returns None (SectorBriefingAggregator TBD in N4-9).
    无该 sector 事件返回 404, Neo4j 不可用返回 503。
    """
    logger.info("GET /api/events/sector/%s", sector_name)

    try:
        records = _query_neo4j(
            neo4j_driver,
            _build_sector_events_query(),
            params={"sector_name": sector_name},
        )
    except HTTPException:
        raise

    events: list[EventItem] = []
    unique_tickers: set[str] = set()
    sev_counts: dict[str, int] = {}

    for rec in records:
        ep = rec.get("ep")
        if ep is None:
            continue
        entity_records = rec.get("entities", [])
        event = _episode_to_event_item({"e": ep}, entity_records)
        events.append(event)

        # Collect tickers and severity counts for statistics
        for ent_item in event.entities:
            if ent_item.ticker:
                unique_tickers.add(ent_item.ticker)

        sev = event.severity
        sev_counts[sev] = sev_counts.get(sev, 0) + 1

    if not events:
        logger.warning("Sector not found or no events: %s", sector_name)
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Sector not found",
                "detail": f"No events for sector '{sector_name}'",
            },
        )

    # Determine dominant severity
    dominant_severity = "medium"
    max_count = 0
    for sev, count in sev_counts.items():
        if count > max_count:
            max_count = count
            dominant_severity = sev

    logger.info(
        "GET /api/events/sector/%s — %d events, %d tickers",
        sector_name,
        len(events),
        len(unique_tickers),
    )
    return SectorEventsResponse(
        sector=sector_name,
        events=events,
        statistics=SectorStatistics(
            total_events=len(events),
            affected_tickers=len(unique_tickers),
            dominant_severity=dominant_severity,
        ),
        sector_briefing=None,  # SectorBriefingAggregator TBD in N4-9
    )


# ---------------------------------------------------------------------------
# N4-5: GET /api/events/risk-summary
# ---------------------------------------------------------------------------


@router.get("/risk-summary", response_model=RiskSummaryResponse)
async def get_risk_summary(
    neo4j_driver: Driver = Depends(get_neo4j_driver),
    settings: Settings = Depends(get_settings),
) -> RiskSummaryResponse:
    """Return a risk summary across all sectors.

    TODO: LLM 聚合逻辑待 N4-9 实现.
    Current implementation returns structured mock data derived from Neo4j query.
    """
    logger.info("GET /api/events/risk-summary")

    # Query Neo4j for recent episodes and sector distribution
    try:
        high_records = _query_neo4j(
            neo4j_driver,
            _build_high_risk_query(),
        )
    except HTTPException:
        raise

    sector_risk_levels: dict[str, str] = {}
    top_risks: list[TopRiskItem] = []
    severity_counts: dict[str, int] = {}
    total_events = 0

    for rec in high_records:
        ep = rec.get("e")
        if ep is None:
            continue

        total_events += 1
        sev = (ep.get("severity") or "medium").lower()
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

        # Build TopRiskItem
        event_item = _episode_to_event_item(rec, rec.get("entities", []))
        affected_sectors = [
            ent.name
            for ent in event_item.entities
            if ent.type == "sector"
        ]
        if not affected_sectors:
            affected_sectors = ["综合"]

        top_risks.append(
            TopRiskItem(
                event_id=event_item.event_id,
                title=event_item.title,
                severity=event_item.severity,
                affected_sectors=affected_sectors,
                potential_impact=f"{event_item.title} 可能对 {', '.join(affected_sectors)} 板块产生影响。"
                # TODO: LLM 聚合逻辑待 N4-9 实现 — use LLM to generate real potential_impact
            )
        )

        for sec in affected_sectors:
            if sec not in sector_risk_levels:
                sector_risk_levels[sec] = sev.upper()

    # If no high-severity events in Neo4j, provide reasonable defaults
    if not top_risks:
        sector_risk_levels = {
            "互联网平台": "LOW",
            "新能源汽车": "LOW",
            "消费": "LOW",
        }
        # TODO: LLM 聚合逻辑待 N4-9 实现 — query actual event data

    # Calculate overall risk and score
    crit_count = severity_counts.get("critical", 0)
    high_count = severity_counts.get("high", 0)
    medium_count = severity_counts.get("medium", 0)

    if total_events > 0:
        risk_score = min(
            1.0,
            (crit_count * 1.0 + high_count * 0.7 + medium_count * 0.3)
            / max(total_events, 1),
        )
    else:
        risk_score = 0.1

    if risk_score >= 0.7:
        overall_risk = "HIGH"
    elif risk_score >= 0.4:
        overall_risk = "MEDIUM"
    elif risk_score >= 0.2:
        overall_risk = "LOW"
    else:
        overall_risk = "LOW"

    top_risks = top_risks[:5]  # Top-5

    now_str = to_iso8601(now_hkt())

    logger.info(
        "GET /api/events/risk-summary — overall_risk=%s, risk_score=%.2f, top_risks=%d",
        overall_risk,
        risk_score,
        len(top_risks),
    )
    return RiskSummaryResponse(
        overall_risk=overall_risk,
        risk_score=round(risk_score, 2),
        top_risks=top_risks,
        sector_risk_levels=sector_risk_levels,
        summary=(
            f"当前整体风险等级 {overall_risk} (risk_score={risk_score:.2f})。"
            f"监测到 {crit_count} 条 critical 级别事件、{high_count} 条 high 级别事件。"
            f"建议关注高风险板块并调整防御仓位比例。"
        ),
        # TODO: LLM 聚合逻辑待 N4-9 实现 — use LLM to generate real summary text
        generated_at=now_str,
    )
