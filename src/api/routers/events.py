"""Event query endpoints — /api/events/*

Implements N4-2 through N4-5:
- N4-2: GET /api/events/active — 当前活跃事件
- N4-3: GET /api/events/entity/:ticker — 某股票相关事件
- N4-4: GET /api/events/sector/:name — 行业事件聚合
- N4-5: GET /api/events/risk-summary — 风险摘要（mock — LLM 聚合待 L-5 实现）

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
from src.graphiti.translation import (
    SEVERITY_WEIGHT,
    severity_sort_weight,
    translate_episode_to_event,
    translate_entities_to_items,
)
from src.utils.time_utils import now_hkt, to_iso8601
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/events",
    tags=["Events"],
)

# Helper — severity sort weight (delegated to translation.py)


def _severity_sort_weight(severity: str) -> int:
    """Map severity string to numeric weight for sorting."""
    return severity_sort_weight(severity)


# ---------------------------------------------------------------------------
# L-5: Risk-summary LLM prompts & helpers
# ---------------------------------------------------------------------------

SYSTEM_RISK_PROMPT = """你是一个金融风险分析师。严格按用户要求的 JSON 格式输出。
只输出 JSON，不要额外说明。"""

RISK_SUMMARY_PROMPT = """基于以下活跃事件数据，生成：
1. 整体风险摘要（2-3 句，中文）
2. 每条 top risk 的潜在影响分析（1-2 句，中文）

事件数据：
{events_json}

行业风险分布：
{sector_risk_json}

输出格式（JSON）：
{{
  "summary": "整体风险摘要...",
  "potential_impacts": ["影响分析1", "影响分析2", ...]
}}
"""


def _build_risk_events_json(top_risks_raw: list[dict]) -> str:
    """Build JSON string of risk events for LLM prompt."""
    import json

    simplified = []
    for r in top_risks_raw:
        simplified.append({
            "title": r["title"],
            "severity": r["severity"],
            "affected_sectors": r["affected_sectors"],
        })
    return json.dumps(simplified, ensure_ascii=False, indent=2)


def _format_sector_risk_json(sector_risk_levels: dict[str, str]) -> str:
    """Format sector risk levels as JSON string for LLM prompt."""
    import json

    return json.dumps(sector_risk_levels, ensure_ascii=False, indent=2)


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
        event = EventItem(**translate_episode_to_event(rec, entity_records))
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
        WHERE e.created_at > datetime() - duration({days: 7})
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
          AND ep.created_at > datetime() - duration({days: 3})
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
        event = EventItem(**translate_episode_to_event({"e": ep}, entity_records))
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
          AND ep.created_at > datetime() - duration({days: 7})
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
        WHERE e.episode_metadata CONTAINS 'MACRO'
          AND e.created_at > datetime() - duration({days: 14})
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

    sector_briefing returns None (SectorBriefingAggregator 提供缓存，见 src/ingestion/briefing_aggregator.py).
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
        event = EventItem(**translate_episode_to_event({"e": ep}, entity_records))
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
        sector_briefing=None,  # briefing 由 SectorBriefingAggregator 在调度器 cycle 内异步更新
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

    TODO(L-5): LLM 聚合 → risk-summary 真实文本.
    Uses LLM (qwen-plus) to generate summary and potential_impact text.
    Falls back to mock template on LLM failure.
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
    top_risks_raw: list[dict[str, Any]] = []  # raw data before LLM enrichment
    severity_counts: dict[str, int] = {}
    total_events = 0

    for rec in high_records:
        ep = rec.get("e")
        if ep is None:
            continue

        total_events += 1
        sev = (ep.get("severity") or "medium").lower()
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

        event_item = EventItem(**translate_episode_to_event(rec, rec.get("entities", [])))
        affected_sectors = [
            ent.name
            for ent in event_item.entities
            if ent.type == "sector"
        ]
        if not affected_sectors:
            affected_sectors = ["综合"]

        top_risks_raw.append({
            "event_id": event_item.event_id,
            "title": event_item.title,
            "severity": event_item.severity,
            "affected_sectors": affected_sectors,
        })

        for sec in affected_sectors:
            if sec not in sector_risk_levels:
                sector_risk_levels[sec] = sev.upper()

    # When no events, use empty dict + annotation (avoids hardcoded defaults)
    if not top_risks_raw:
        sector_risk_levels = {}

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

    top_risks = top_risks_raw[:5]

    # ---- LLM enrichment (L-5) ----
    llm_summary: str | None = None
    llm_impacts: list[str] | None = None

    if top_risks_raw:
        try:
            from openai import AsyncOpenAI

            llm_client = AsyncOpenAI(
                api_key=settings.bailian_api_key,
                base_url=settings.openai_base_url,
            )

            events_json = _build_risk_events_json(top_risks_raw)
            sector_risk_json = _format_sector_risk_json(sector_risk_levels)

            prompt = RISK_SUMMARY_PROMPT.format(
                events_json=events_json,
                sector_risk_json=sector_risk_json,
            )

            response = await llm_client.chat.completions.create(
                model="qwen-plus",
                messages=[
                    {
                        "role": "system",
                        "content": SYSTEM_RISK_PROMPT,
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=800,
                temperature=0.3,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            if content:
                import json

                parsed = json.loads(content)
                llm_summary = parsed.get("summary", "")
                llm_impacts = parsed.get("potential_impacts", [])

                if not isinstance(llm_impacts, list):
                    llm_impacts = None
        except Exception as exc:
            logger.warning(
                "Risk-summary LLM enrichment failed, using fallback: %s",
                exc,
            )

    # ---- Build final TopRiskItem list ----
    final_top_risks: list[TopRiskItem] = []
    for i, raw in enumerate(top_risks_raw[:5]):
        if llm_impacts and i < len(llm_impacts):
            impact = llm_impacts[i]
        else:
            impact = (
                f"{raw['title']} 可能对 "
                f"{', '.join(raw['affected_sectors'])} 板块产生影响。"
            )
        final_top_risks.append(
            TopRiskItem(
                event_id=raw["event_id"],
                title=raw["title"],
                severity=raw["severity"],
                affected_sectors=raw["affected_sectors"],
                potential_impact=impact,
            )
        )

    # ---- Build summary ----
    if llm_summary:
        summary_text = llm_summary
    else:
        summary_text = (
            f"当前整体风险等级 {overall_risk} (risk_score={risk_score:.2f})。"
            f"监测到 {crit_count} 条 critical 级别事件、{high_count} 条 high 级别事件。"
            f"建议关注高风险板块并调整防御仓位比例。"
        )

    now_str = to_iso8601(now_hkt())

    logger.info(
        "GET /api/events/risk-summary — overall_risk=%s, risk_score=%.2f, top_risks=%d",
        overall_risk,
        risk_score,
        len(final_top_risks),
    )
    return RiskSummaryResponse(
        overall_risk=overall_risk,
        risk_score=round(risk_score, 2),
        top_risks=final_top_risks,
        sector_risk_levels=sector_risk_levels,
        summary=summary_text,
        generated_at=now_str,
    )
