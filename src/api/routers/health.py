"""Health check endpoint — GET /api/events/health

Implements N4-6:
- N4-6: GET /api/events/health — 系统健康检查

Reports Neo4j connectivity, node/relation counts, Graphiti episode count,
data source status (placeholder, to be filled by N4-9 scheduler),
and overall service uptime.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends
from neo4j import Driver

from src.api.deps import get_neo4j_driver, get_settings
from src.api.models import (
    HealthResponse,
    Neo4jHealth,
    GraphitiHealth,
    DataSourceHealth,
    DataSourceStatus,
)
from src.core.config import Settings
from src.utils.time_utils import now_hkt, to_iso8601
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/events",
    tags=["Health"],
)

# ---------------------------------------------------------------------------
# Module-level start time for uptime tracking
# ---------------------------------------------------------------------------
_START_TIME: float = time.time()

# ---------------------------------------------------------------------------
# Cache for Neo4j node/relation counts (refresh every health check call)
# ---------------------------------------------------------------------------

_LAST_NEO4J_COUNT: int = 0
_LAST_NEO4J_COUNT_TIME: float = 0.0


def _query_neo4j_counts(driver: Driver) -> tuple[int, int, str]:
    """Query Neo4j for node and relation counts.

    Returns (node_count, relation_count, status).
    On failure, returns previously cached counts (if any) and status 'down'.
    """
    global _LAST_NEO4J_COUNT, _LAST_NEO4J_COUNT_TIME

    try:
        driver.verify_connectivity()
    except Exception as exc:
        logger.error("Neo4j health check — connectivity failed: %s", exc)
        return _LAST_NEO4J_COUNT, _LAST_NEO4J_COUNT, "down"

    try:
        with driver.session() as session:
            # Count all nodes (including Episodic, Entity, and internal Neo4j nodes)
            node_result = session.run("MATCH (n) RETURN count(n) AS cnt")
            node_count = node_result.single()["cnt"]

            # Count all relationships
            rel_result = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt")
            rel_count = rel_result.single()["cnt"]

        _LAST_NEO4J_COUNT = node_count
        _LAST_NEO4J_COUNT_TIME = time.time()

        logger.info(
            "Neo4j health — %d nodes, %d relations",
            node_count,
            rel_count,
        )
        return node_count, rel_count, "ok"

    except Exception as exc:
        logger.error("Neo4j count query failed: %s", exc)
        return _LAST_NEO4J_COUNT, _LAST_NEO4J_COUNT, "down"


def _query_graphiti_episode_count(driver: Driver) -> tuple[int, str]:
    """Query Neo4j for today's episode count (Graphiti Episodic nodes)."""
    try:
        driver.verify_connectivity()
    except Exception:
        return 0, "down"

    try:
        from datetime import datetime, timezone, timedelta

        # Compute start of today in UTC
        utc_now = datetime.now(timezone.utc)
        today_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)

        with driver.session() as session:
            # Count Episodic nodes created today
            result = session.run(
                """
                MATCH (e:Episodic)
                WHERE e.created_at >= $today_start
                RETURN count(e) AS cnt
                """,
                {"today_start": today_start.isoformat()},
            )
            episode_count = result.single()["cnt"]

        return episode_count, "ok"

    except Exception as exc:
        logger.warning("Graphiti episode count query failed: %s", exc)
        return 0, "down"


# ---------------------------------------------------------------------------
# N4-6: GET /api/events/health
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def get_health(
    neo4j_driver: Driver = Depends(get_neo4j_driver),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Return system health status.

    Real-time Neo4j connectivity check via verify_connectivity().
    Data source status is a placeholder — will be filled by the N4-9 scheduler.

    Status determination:
      - Neo4j reachable + all checks ok  → status="healthy"
      - Neo4j unreachable                → status="down"
    """
    logger.info("GET /api/events/health")

    # ---- Uptime ----
    uptime_seconds = int(time.time() - _START_TIME)

    # ---- Neo4j health ----
    neo4j_node_count, neo4j_rel_count, neo4j_status = _query_neo4j_counts(
        neo4j_driver
    )

    # ---- Graphiti health ----
    episode_count, graphiti_status = _query_graphiti_episode_count(neo4j_driver)

    # ---- Overall status ----
    if neo4j_status == "down":
        overall_status = "down"
    elif neo4j_status == "ok":
        overall_status = "healthy"
    else:
        overall_status = "degraded"

    now_str = to_iso8601(now_hkt())

    # ---- Data sources (placeholder — 待 N4-9 调度器实现后填充) ----
    data_sources = DataSourceHealth(
        gdelt_csv=DataSourceStatus(
            status="ok",
            last_update=now_str,
            latency_minutes=0,
            error=None,
        ),
        rss=DataSourceStatus(
            status="ok",
            last_update=now_str,
            latency_minutes=0,
            error=None,
        ),
        akshare=DataSourceStatus(
            status="ok",
            last_update=now_str,
            latency_minutes=0,
            error=None,
        ),
        treasury=None,
    )
    # TODO: data_sources 待 N4-9 调度器实现后填充真实状态

    logger.info(
        "GET /api/events/health — status=%s, uptime=%ds, neo4j=%s, episodes_today=%d",
        overall_status,
        uptime_seconds,
        neo4j_status,
        episode_count,
    )
    return HealthResponse(
        status=overall_status,
        uptime_seconds=uptime_seconds,
        data_sources=data_sources,
        neo4j=Neo4jHealth(
            status=neo4j_status,
            node_count=neo4j_node_count,
            relation_count=neo4j_rel_count,
        ),
        graphiti=GraphitiHealth(
            status=graphiti_status,
            episode_count_today=episode_count,
        ),
    )
