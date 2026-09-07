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

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Path
from neo4j import Driver

from src.api.models import (
    ActiveEventsResponse,
    EntityEventsResponse,
    EntityEventSummary,
    EpisodeItem,
    EventItem,
    EventEntityItem,
    FreshnessInfo,
    GraphEdge,
    GraphNode,
    GraphStructure,
    RiskSummaryResponse,
    SectorEventsResponse,
    SectorStatistics,
    TopRiskItem,
)
from src.api.deps import get_aggregator, get_neo4j_driver, get_settings
from src.core.config import Settings
from src.graphiti.translation import (
    SEVERITY_WEIGHT,
    entity_type_from_labels,
    severity_sort_weight,
    translate_episode_to_event,
    translate_entities_to_items,
)
from src.utils.time_utils import coerce_datetime, now_hkt, to_iso8601
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
        WITH e, [n IN entities | n{.*, labels: labels(n)}] AS entities
        WHERE ($sector IS NULL
               OR ANY(ent IN entities
                      WHERE (ent.sector IS NOT NULL AND ent.sector CONTAINS $sector)
                         OR (ent.name CONTAINS $sector)))
        RETURN e, entities
        ORDER BY e.valid_at DESC, e.created_at DESC
        LIMIT $limit
    """


def _build_entity_events_query() -> str:
    """Build Cypher to find events associated with a stock ticker.

    Finds Entity by ticker → RELATES_TO → Episodic via entity_edges.

    查询参数 (P0-3 跨仓库 ticker 契约修复):
      - ``$ticker``:              NewsEngine 格式 ticker（如 "0700.HK" / "000858.SZ"）
      - ``$window_days``:         事件时间窗口（天），来自 settings.entity_events_window_days，
                                  取代原硬编码 ``duration({days: 3})``
      - ``$min_severity_weight``: 最低 severity 权重（SEVERITY_WEIGHT 映射：
                                  low=1 / medium=2 / high=3 / critical=4；
                                  Episodic 无 severity 属性时按 medium 处理）
      - ``$limit``:               最大返回事件数（按 valid_at/created_at 降序取前 N）
    """
    return """
        MATCH (ent:Entity)
        WHERE ent.ticker = $ticker
        OPTIONAL MATCH (ent)-[rel:RELATES_TO]-(other_entity:Entity)
        OPTIONAL MATCH (ep:Episodic)
        WHERE rel.uuid IN ep.entity_edges
          AND ep.created_at > datetime() - duration({days: $window_days})
        OPTIONAL MATCH (ep)-[other_rel:RELATES_TO]-(other_entity2:Entity)
        WHERE ep IS NOT NULL AND other_rel.uuid IN ep.entity_edges
        WITH ep,
             [n IN (collect(DISTINCT other_entity) + collect(DISTINCT other_entity2))
              | n{.*, labels: labels(n)}] AS entities,
             CASE lower(toString(coalesce(ep.severity, 'medium')))
               WHEN 'critical' THEN 4
               WHEN 'high'     THEN 3
               WHEN 'medium'   THEN 2
               WHEN 'low'      THEN 1
               ELSE 0
             END AS sev_weight
        WHERE ep IS NULL OR sev_weight >= $min_severity_weight
        RETURN ep, entities
        ORDER BY ep.valid_at DESC, ep.created_at DESC
        LIMIT $limit
    """


def _build_entity_graph_query(hops: int) -> str:
    """Build Cypher for the P2-1 entity subgraph around a ticker.

    从 target ticker 的 Entity 出发，沿 RELATES_TO 无向遍历 1..hops 跳，
    返回每条边（含两端节点投影）+ 起点节点列。起点无邻居时仍返回
    一行（边列为 NULL），保证 nodes 至少含目标股票自身。

    .. warning::
        ``hops`` 以字面量内插（Cypher 不支持变长路径参数绑定），
        调用方必须传入经 1..3 范围校验的 int（本函数内再次防御校验，
        杜绝注入面）。
    """
    hops = int(hops)
    if not 1 <= hops <= 3:
        raise ValueError(f"graph hops must be within [1, 3], got {hops}")
    return f"""
        MATCH (start:Entity)
        WHERE start.ticker = $ticker
        OPTIONAL MATCH path = (start)-[:RELATES_TO*1..{hops}]-(other:Entity)
        WITH start, [p IN collect(path) WHERE p IS NOT NULL | relationships(p)] AS nested_rels
        WITH start, reduce(acc = [], rs IN nested_rels | acc + rs) AS rels
        UNWIND (rels + [null]) AS rel
        WITH DISTINCT start, rel
        RETURN start.name AS start_name,
               [l IN labels(start) | l] AS start_labels,
               start.ticker AS start_ticker,
               CASE WHEN rel IS NULL THEN null ELSE startNode(rel).name END AS source_name,
               CASE WHEN rel IS NULL THEN null ELSE [l IN labels(startNode(rel)) | l] END AS source_labels,
               CASE WHEN rel IS NULL THEN null ELSE startNode(rel).ticker END AS source_ticker,
               CASE WHEN rel IS NULL THEN null ELSE endNode(rel).name END AS target_name,
               CASE WHEN rel IS NULL THEN null ELSE [l IN labels(endNode(rel)) | l] END AS target_labels,
               CASE WHEN rel IS NULL THEN null ELSE endNode(rel).ticker END AS target_ticker,
               CASE WHEN rel IS NULL THEN null ELSE rel.name END AS edge_type,
               CASE WHEN rel IS NULL THEN null ELSE rel.fact END AS fact
        LIMIT $edge_limit
    """


def _build_entity_episodes_query(hops: int) -> str:
    """Build Cypher for the P2-1 episode timeline around a ticker.

    收集 1..hops 跳内的实体集合（含起点），再匹配这些实体参与的
    RELATES_TO 边，通过 ep.entity_edges 关联 Episodic 节点（事件脉络）。
    时间窗口与 events 查询一致（$window_days）。
    """
    hops = int(hops)
    if not 1 <= hops <= 3:
        raise ValueError(f"graph hops must be within [1, 3], got {hops}")
    return f"""
        MATCH (start:Entity)
        WHERE start.ticker = $ticker
        OPTIONAL MATCH (start)-[:RELATES_TO*1..{hops}]-(ent:Entity)
        WITH start, collect(DISTINCT ent) + [start] AS ents
        UNWIND ents AS ent
        MATCH (ent)-[rel:RELATES_TO]-(peer:Entity)
        MATCH (ep:Episodic)
        WHERE rel.uuid IN ep.entity_edges
          AND ep.created_at > datetime() - duration({{days: $window_days}})
        RETURN DISTINCT ep.uuid AS id,
               ep.name AS name,
               ep.content AS content,
               ep.valid_at AS valid_at,
               ent.name AS entity_name,
               peer.name AS peer_name
        LIMIT $episode_row_limit
    """


def _build_graph_structure(records: list[dict[str, Any]]) -> GraphStructure:
    """Assemble GraphStructure from _build_entity_graph_query() rows.

    节点按 name 去重；边按 (source, target, type) 去重。边 type 取
    RELATES_TO.name（graphiti 写入的关系类型，如 BELONGS_TO/AFFECTS），
    缺失时兜底 RELATED_TO。

    .. note::
        P2（CR 评审已接受，注明即可）: 去重键不含 ``fact`` —— 同型同两端
        但 fact 不同的边仅保留首条。当前数据无此类多 fact 边；若未来
        出现需区分，应将 fact 纳入去重键或改为聚合多 fact。
    """
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def _add_node(
        name: str | None,
        labels: list[str] | None,
        ticker: str | None,
    ) -> None:
        if not name or name in nodes:
            return
        nodes[name] = GraphNode(
            id=name,
            type=entity_type_from_labels(labels or [], ticker),
            ticker=ticker,
        )

    for rec in records:
        _add_node(
            rec.get("start_name"),
            rec.get("start_labels"),
            rec.get("start_ticker"),
        )
        src = rec.get("source_name")
        tgt = rec.get("target_name")
        if not src or not tgt:
            continue  # 起点孤立行（无边）
        _add_node(src, rec.get("source_labels"), rec.get("source_ticker"))
        _add_node(tgt, rec.get("target_labels"), rec.get("target_ticker"))
        edge_type = str(rec.get("edge_type") or "RELATED_TO").upper()
        key = (src, tgt, edge_type)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(
            GraphEdge(source=src, target=tgt, type=edge_type, fact=rec.get("fact"))
        )

    return GraphStructure(nodes=list(nodes.values()), edges=edges)


def _build_episode_items(
    records: list[dict[str, Any]], limit: int = 50
) -> list[EpisodeItem]:
    """Group _build_entity_episodes_query() rows into EpisodeItem list.

    同一 episode 因多实体参与会出多行 → 按 ep.uuid 归组，实体名去重；
    title 取 content 首个非空行（与 translation 层一致），兜底 ep.name；
    输出按 valid_at 升序（事件脉络时间线）。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for rec in records:
        ep_id = rec.get("id")
        if not ep_id:
            continue
        entry = grouped.get(ep_id)
        if entry is None:
            content: str = rec.get("content") or ""
            first_line = next(
                (ln.strip() for ln in content.split("\n") if ln.strip()), ""
            )
            title = first_line or rec.get("name") or "Untitled Episode"
            valid_at_dt = coerce_datetime(rec.get("valid_at"))
            entry = {
                "title": title[:200],
                "valid_at": to_iso8601(valid_at_dt)
                if valid_at_dt is not None
                else None,
                "entities": [],
            }
            grouped[ep_id] = entry
        for key in ("entity_name", "peer_name"):
            ent_name = rec.get(key)
            if ent_name and ent_name not in entry["entities"]:
                entry["entities"].append(ent_name)

    items = [EpisodeItem(id=ep_id, **entry) for ep_id, entry in grouped.items()]
    items.sort(key=lambda it: it.valid_at or "")
    return items[:limit]


# P2-1 图查询上限 — 防止 hub 实体变长遍历爆炸
_GRAPH_EDGE_LIMIT: int = 200
_EPISODE_ROW_LIMIT: int = 500


@router.get("/entity/{ticker}", response_model=EntityEventsResponse)
async def get_entity_events(
    ticker: str = Path(..., description="Stock ticker, e.g. 0700.HK / 000858.SZ"),
    limit: int = Query(default=30, ge=1, le=200, description="Max events to return"),
    min_severity: str = Query(
        default="medium",
        pattern="^(low|medium|high|critical)$",
        description="Minimum severity filter: low / medium / high / critical",
    ),
    include_graph: bool = Query(
        default=True,
        description=(
            "P2-1: include graph (nodes+edges within graph_depth hops) and "
            "episodes (事件脉络) in the response. false = legacy flat payload."
        ),
    ),
    graph_depth: int = Query(
        default=3,
        ge=1,
        le=3,
        description="P2-1: hop range for graph traversal (1-3, spec §3.8)",
    ),
    neo4j_driver: Driver = Depends(get_neo4j_driver),
    settings: Settings = Depends(get_settings),
) -> EntityEventsResponse:
    """Return events associated with a specific stock ticker.

    ticker 格式: 0700.HK / 000858.SZ / 600519.SH（代码.交易所，不是 HK.00700）。
    Queries Neo4j via Entity ticker → EntityEdge → Episodic.

    P0-3: 新增 limit / min_severity 查询参数（此前 SynapseEngine 客户端发送的
    同名参数被丢弃）；时间窗口从 settings.entity_events_window_days 读取
    （默认 7 天，原硬编码 3 天）。severity 过滤与 limit 截断在 Cypher 内完成，
    summary 统计基于过滤后的事件集合。

    P2-1: include_graph=true（默认）时额外返回 graph（ticker 出发
    graph_depth 跳内的 nodes+edges）与 episodes（时间线）。向后兼容：
    既有 ticker/events/summary 字段不变；图查询失败降级为 graph=None，
    不影响事件主链路。

    ticker not found 返回 404, Neo4j 不可用返回 503, 内部错误返回 500。
    """
    logger.info(
        "GET /api/events/entity/%s — limit=%d, min_severity=%s, window_days=%d, include_graph=%s, graph_depth=%s",
        ticker,
        limit,
        min_severity,
        settings.entity_events_window_days,
        include_graph,
        graph_depth,
    )

    try:
        records = _query_neo4j(
            neo4j_driver,
            _build_entity_events_query(),
            params={
                "ticker": ticker,
                "window_days": settings.entity_events_window_days,
                "min_severity_weight": SEVERITY_WEIGHT.get(
                    min_severity.lower(),
                    SEVERITY_WEIGHT["medium"],
                ),
                "limit": limit,
            },
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

    # ---- P2-1: graph structure + episodes (降级不阻断主链路) ----
    graph: GraphStructure | None = None
    episodes: list[EpisodeItem] | None = None
    if include_graph:
        try:
            graph_records = _query_neo4j(
                neo4j_driver,
                _build_entity_graph_query(graph_depth),
                params={"ticker": ticker, "edge_limit": _GRAPH_EDGE_LIMIT},
            )
            graph = _build_graph_structure(graph_records)

            episode_records = _query_neo4j(
                neo4j_driver,
                _build_entity_episodes_query(graph_depth),
                params={
                    "ticker": ticker,
                    "window_days": settings.entity_events_window_days,
                    "episode_row_limit": _EPISODE_ROW_LIMIT,
                },
            )
            episodes = _build_episode_items(episode_records)
        except Exception as exc:
            logger.warning(
                "Graph structure query degraded for %s (events still served): %s",
                ticker,
                exc,
            )
            graph, episodes = None, None

    logger.info(
        "GET /api/events/entity/%s — %d events, risk=%s, graph_nodes=%d, graph_edges=%d, episodes=%d",
        ticker,
        total_events,
        risk_level,
        len(graph.nodes) if graph else 0,
        len(graph.edges) if graph else 0,
        len(episodes) if episodes else 0,
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
        graph=graph,
        episodes=episodes,
    )


def _build_sector_events_query() -> str:
    """Build Cypher to find events in a specific sector.

    Finds sector by Entity name (label-filtered to :Sector to avoid
    same-name cross-type entity collisions, P2-2), traces Stock entities
    in that sector, then finds RELATES_TO relationships and linked
    Episodic nodes.
    """
    return """
        MATCH (sector_ent:Entity)
        WHERE 'Sector' IN labels(sector_ent)
          AND sector_ent.name = $sector_name
        OPTIONAL MATCH (stock:Entity)
        WHERE stock.sector = sector_ent.name
        OPTIONAL MATCH (stock)-[rel:RELATES_TO]-(other:Entity)
        OPTIONAL MATCH (ep:Episodic)
        WHERE rel.uuid IN ep.entity_edges
          AND ep.created_at > datetime() - duration({days: 7})
        OPTIONAL MATCH (ep)-[other_rel:RELATES_TO]-(all_ents:Entity)
        WHERE ep IS NOT NULL AND other_rel.uuid IN ep.entity_edges
        WITH ep,
             [n IN collect(DISTINCT all_ents) | n{.*, labels: labels(n)}] AS entities,
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
        WITH e, collect(DISTINCT src) + collect(DISTINCT tgt) AS entities
        WITH e, [n IN entities | n{.*, labels: labels(n)}] AS entities
        RETURN e, entities
        ORDER BY e.created_at DESC
        LIMIT 10
    """


@router.get("/sector/{sector_name}", response_model=SectorEventsResponse)
async def get_sector_events(
    sector_name: str = Path(..., description="Sector name in Chinese, e.g. 互联网平台"),
    neo4j_driver: Driver = Depends(get_neo4j_driver),
    aggregator: Any = Depends(get_aggregator),
) -> SectorEventsResponse:
    """Return events aggregated by sector (Chinese name).

    sector_briefing 从 SectorBriefingAggregator 的进程级共享缓存读取
    （L2 断路修复）：scheduler 每 15 分钟周期尾部调用 aggregate_all() 写入，
    此处 get_cached() O(1) 读取，不触发 LLM。缓存未命中返回 None，
    消费方（MiroFish/SynapseEngine）降级为基于原始 events 自行聚合。

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

    # L2 断路修复: 读共享缓存（命中 → Markdown 简报; 未命中 → None 降级）
    sector_briefing: str | None = None
    try:
        sector_briefing = aggregator.get_cached(sector_name)
    except Exception as exc:  # 缓存读取失败不影响事件列表主链路
        logger.warning(
            "sector_briefing cache read failed for %s: %s", sector_name, exc
        )

    logger.info(
        "GET /api/events/sector/%s — %d events, %d tickers, briefing_cached=%s",
        sector_name,
        len(events),
        len(unique_tickers),
        sector_briefing is not None,
    )
    return SectorEventsResponse(
        sector=sector_name,
        events=events,
        statistics=SectorStatistics(
            total_events=len(events),
            affected_tickers=len(unique_tickers),
            dominant_severity=dominant_severity,
        ),
        sector_briefing=sector_briefing,
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
                api_key=settings.openai_api_key,
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
