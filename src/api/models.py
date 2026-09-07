"""Pydantic response models for NewsEngine REST API.

Defines the JSON schema for all API responses, ensuring type safety and
automatic OpenAPI documentation generation via FastAPI.

All field definitions, types, and optional/mandatory markers SHALL match
the API contracts defined in Design Doc Part 2 §2.3~§2.7.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Nested models — EventItem dependencies
# ---------------------------------------------------------------------------


class EventEntityItem(BaseModel):
    """A single entity associated with an event."""

    type: str = Field(
        ...,
        description="Entity type: stock / sector / country / policy",
    )
    ticker: str | None = Field(
        default=None,
        description="Stock ticker (only for stock type), e.g. 0700.HK",
    )
    name: str = Field(
        ...,
        description="Entity display name (Chinese)",
    )
    status: str | None = Field(
        default=None,
        description="Status (only for policy type): rumor / confirmed / resolved",
    )


# ---------------------------------------------------------------------------
# EventItem — core event model
# ---------------------------------------------------------------------------


class EventItem(BaseModel):
    """A single event as returned in the events array of every endpoint response."""

    event_id: str = Field(
        ...,
        description="Unique event ID, format evt-YYYYMMDD-NNN",
    )
    title: str = Field(
        ...,
        description="Event title",
    )
    summary: str | None = Field(
        default=None,
        description="Event summary (LLM generated or original first paragraph)",
    )
    severity: str = Field(
        ...,
        description="Enum: low / medium / high / critical",
    )
    first_seen: str = Field(
        ...,
        description="First discovery time (ISO 8601 +08:00)",
    )
    last_updated: str = Field(
        ...,
        description="Last update time (ISO 8601 +08:00)",
    )
    source_count: int = Field(
        ...,
        description="Number of information sources",
    )
    source_urls: list[str] | None = Field(
        default=None,
        description="Source link list",
    )
    keywords: list[str] = Field(
        ...,
        description="Keywords extracted from the event",
    )
    entities: list[EventEntityItem] = Field(
        ...,
        description="Associated entities",
    )


# ---------------------------------------------------------------------------
# FreshnessInfo — nested in ActiveEventsResponse
# ---------------------------------------------------------------------------


class FreshnessInfo(BaseModel):
    """Data source freshness timestamps."""

    gdelt_last_update: str = Field(
        ...,
        description="GDELT last update time (ISO 8601 +08:00)",
    )
    rss_last_update: str | None = Field(
        default=None,
        description="RSS last update time (ISO 8601 +08:00)",
    )
    akshare_last_update: str | None = Field(
        default=None,
        description="AkShare last update time (ISO 8601 +08:00)",
    )


# ---------------------------------------------------------------------------
# ActiveEventsResponse — GET /api/events/active
# ---------------------------------------------------------------------------


class ActiveEventsResponse(BaseModel):
    """Response model for GET /api/events/active."""

    events: list[EventItem] = Field(
        ...,
        description="Event list, sorted by severity desc + last_updated desc",
    )
    total: int = Field(
        ...,
        description="Total number of matching events",
    )
    freshness: FreshnessInfo = Field(
        ...,
        description="Data source freshness information",
    )


# ---------------------------------------------------------------------------
# Graph structure models — P2-1 (诊断报告 §3.8 方案 A)
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    """A node in the entity graph returned by GET /api/events/entity/:ticker.

    ``id`` is the entity's canonical name (中文主名，与 SynapseEngine 接地
    后的实体名一致)；``type`` 使用 translation.LABEL_TYPE_MAP 的小写业务
    类型（stock / sector / country / policy / …），与 EventEntityItem.type
    保持同一枚举。
    """

    id: str = Field(
        ...,
        description="Node id — canonical entity name (e.g. 五粮液 / 白酒 / 中国)",
    )
    type: str = Field(
        ...,
        description="Business entity type: stock / sector / country / policy / topic / organization / event / person / unknown",
    )
    ticker: str | None = Field(
        default=None,
        description="Stock ticker (only for stock type), e.g. 000858.SZ",
    )


class GraphEdge(BaseModel):
    """A directed edge (RELATES_TO relationship) in the entity graph."""

    source: str = Field(..., description="Source node id (entity name)")
    target: str = Field(..., description="Target node id (entity name)")
    type: str = Field(
        ...,
        description="Relation type from RELATES_TO.name, e.g. BELONGS_TO / AFFECTS / LOCATED_IN",
    )
    fact: str | None = Field(
        default=None,
        description="Natural-language fact extracted with the edge (Chinese)",
    )


class GraphStructure(BaseModel):
    """Entity subgraph within N hops of the queried ticker (P2-1).

    Nodes are deduplicated by name; edges are deduplicated by
    (source, target, type). Lets downstream consumers (SynapseEngine)
    reconstruct event context without querying Neo4j directly.
    """

    nodes: list[GraphNode] = Field(
        default_factory=list,
        description="Distinct entities within the hop range (including the queried stock)",
    )
    edges: list[GraphEdge] = Field(
        default_factory=list,
        description="Distinct RELATES_TO edges between the returned nodes",
    )


class EpisodeItem(BaseModel):
    """A compact episodic-memory item (事件脉络节点, P2-1)."""

    id: str = Field(..., description="Episodic node uuid")
    title: str = Field(..., description="Episode title (first content line or node name)")
    valid_at: str | None = Field(
        default=None,
        description="Event time (ISO 8601 +08:00)",
    )
    entities: list[str] = Field(
        default_factory=list,
        description="Names of entities linked to this episode",
    )


# ---------------------------------------------------------------------------
# EntityEventSummary & EntityEventsResponse — GET /api/events/entity/:ticker
# ---------------------------------------------------------------------------


class EntityEventSummary(BaseModel):
    """Aggregated summary for a single ticker."""

    total_events: int = Field(
        ...,
        description="Total event count for this ticker",
    )
    avg_severity: str = Field(
        ...,
        description="Average severity level across all events",
    )
    risk_level: str = Field(
        ...,
        description="Aggregated risk level, e.g. HIGH",
    )
    news_sentiment_score: float = Field(
        ...,
        description="Composite sentiment score (0~1, low = negative)",
    )


class EntityEventsResponse(BaseModel):
    """Response model for GET /api/events/entity/:ticker.

    P2-1: ``graph`` / ``episodes`` 为新增可选字段（向后兼容 —— 既有
    ``ticker`` / ``events`` / ``summary`` 契约不变）。include_graph=false
    或图查询降级时为 None。
    """

    ticker: str = Field(
        ...,
        description="Stock ticker, e.g. 0700.HK",
    )
    events: list[EventItem] = Field(
        ...,
        description="Associated event list for this ticker",
    )
    summary: EntityEventSummary = Field(
        ...,
        description="Aggregated summary for this ticker",
    )
    graph: GraphStructure | None = Field(
        default=None,
        description=(
            "Entity subgraph (nodes + edges) within graph_depth hops of the "
            "ticker (P2-1). None when include_graph=false or the graph "
            "query degrades; events/summary remain authoritative."
        ),
    )
    episodes: list[EpisodeItem] | None = Field(
        default=None,
        description=(
            "Compact episode timeline linked to entities in graph scope, "
            "sorted by valid_at asc (事件脉络). None under the same "
            "conditions as graph."
        ),
    )


# ---------------------------------------------------------------------------
# SectorStatistics & SectorEventsResponse — GET /api/events/sector/:name
# ---------------------------------------------------------------------------


class SectorStatistics(BaseModel):
    """Sector-level event statistics."""

    total_events: int = Field(
        ...,
        description="Total events in this sector",
    )
    affected_tickers: int = Field(
        ...,
        description="Number of affected stocks in this sector",
    )
    dominant_severity: str = Field(
        ...,
        description="Dominant severity level across sector events",
    )


class SectorEventsResponse(BaseModel):
    """Response model for GET /api/events/sector/:name."""

    sector: str = Field(
        ...,
        description="Sector name (Chinese)",
    )
    events: list[EventItem] = Field(
        ...,
        description="Events in this sector",
    )
    statistics: SectorStatistics = Field(
        ...,
        description="Sector-level statistics",
    )
    sector_briefing: str | None = Field(
        default=None,
        description=(
            "LLM aggregated sector intelligence briefing (Markdown, 300-500 chars). "
            "Generated asynchronously by SectorBriefingAggregator. "
            "When None, consumers should fall back to self-aggregation from raw events."
        ),
    )


# ---------------------------------------------------------------------------
# TopRiskItem & RiskSummaryResponse — GET /api/events/risk-summary
# ---------------------------------------------------------------------------


class TopRiskItem(BaseModel):
    """A single high-risk event in the risk summary."""

    event_id: str = Field(
        ...,
        description="Event ID",
    )
    title: str = Field(
        ...,
        description="Event title",
    )
    severity: str = Field(
        ...,
        description="Severity level: low / medium / high / critical",
    )
    affected_sectors: list[str] = Field(
        ...,
        description="List of affected sector names",
    )
    potential_impact: str = Field(
        ...,
        description="LLM-generated potential impact analysis",
    )


class RiskSummaryResponse(BaseModel):
    """Response model for GET /api/events/risk-summary."""

    overall_risk: str = Field(
        ...,
        description="Overall risk level: LOW / MEDIUM / HIGH / CRITICAL",
    )
    risk_score: float = Field(
        ...,
        description="Risk score 0~1 (higher = riskier)",
    )
    top_risks: list[TopRiskItem] = Field(
        ...,
        description="Top-5 high-risk events",
    )
    sector_risk_levels: dict[str, str] = Field(
        ...,
        description="Sector risk level mapping {sector_name: risk_level}",
    )
    summary: str = Field(
        ...,
        description="LLM-generated risk summary text",
    )
    generated_at: str = Field(
        ...,
        description="Summary generation time (ISO 8601 +08:00), reflects cache freshness",
    )


# ---------------------------------------------------------------------------
# Health models — GET /api/events/health
# ---------------------------------------------------------------------------


class DataSourceStatus(BaseModel):
    """Per-data-source health status."""

    status: str = Field(
        ...,
        description="Source status: ok / degraded / down",
    )
    last_update: str = Field(
        ...,
        description="Last data update time (ISO 8601 +08:00)",
    )
    latency_minutes: int = Field(
        ...,
        description="Minutes since last update",
    )
    error: str | None = Field(
        default=None,
        description="Error description (only when status is degraded or down)",
    )


class DataSourceHealth(BaseModel):
    """Aggregated data source health for all configured sources."""

    gdelt_csv: DataSourceStatus = Field(
        ...,
        description="GDELT CSV source health",
    )
    rss: DataSourceStatus = Field(
        ...,
        description="RSS source health",
    )
    akshare: DataSourceStatus = Field(
        ...,
        description="AkShare source health",
    )
    treasury: DataSourceStatus | None = Field(
        default=None,
        description="Treasury source health (optional, Phase 2+)",
    )


class Neo4jHealth(BaseModel):
    """Neo4j knowledge graph connection status."""

    status: str = Field(
        ...,
        description="Connection status: ok / down",
    )
    node_count: int = Field(
        ...,
        description="Knowledge graph node count",
    )
    relation_count: int = Field(
        ...,
        description="Relation edge count",
    )


class GraphitiHealth(BaseModel):
    """Graphiti SDK status."""

    status: str = Field(
        ...,
        description="Graphiti status",
    )
    episode_count_today: int = Field(
        ...,
        description="Episodes processed today",
    )


class HealthResponse(BaseModel):
    """Response model for GET /api/events/health."""

    status: str = Field(
        ...,
        description="Overall status: healthy / degraded / down",
    )
    uptime_seconds: int = Field(
        ...,
        description="Service uptime in seconds",
    )
    data_sources: DataSourceHealth = Field(
        ...,
        description="Per-source health status",
    )
    neo4j: Neo4jHealth = Field(
        ...,
        description="Neo4j connection status",
    )
    graphiti: GraphitiHealth = Field(
        ...,
        description="Graphiti status",
    )
