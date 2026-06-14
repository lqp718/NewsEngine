"""Severity enrichment for Episodic nodes — rule-based classifier.

L-4: Uses keyword matching to classify Episodic severity when
Graphiti EpisodicNode has no native severity property.

Rule engine is preferred over LLM for Phase 1 (zero latency, zero API cost,
predictable output). Phase 2 may replace with LLM-based classifier.

Integration point: `IngestionScheduler._run_cycle()` in scheduler.py,
called after pipeline completion and before briefing aggregation.
"""

from __future__ import annotations

import logging

from neo4j import Driver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword lists (priority order: high → low)
# ---------------------------------------------------------------------------

CRITICAL_KEYWORDS: list[str] = [
    "暴跌",
    "崩盘",
    "停牌",
    "退市",
    "破产",
    "熔断",
]
"""Keywords that indicate critical severity (systemic risk / market crash)."""

HIGH_KEYWORDS: list[str] = [
    "大跌",
    "跌停",
    "利空",
    "罚款",
    "调查",
    "诉讼",
    "违约",
]
"""Keywords that indicate high severity (sector-level negative / major policy)."""

MEDIUM_KEYWORDS: list[str] = [
    "利好",
    "大涨",
    "涨停",
    "突破",
    "新高",
]
"""Keywords that indicate medium severity (individual stock / general industry)."""

LOW_KEYWORDS: list[str] = [
    "回购",
    "增持",
    "重组",
    "分红",
]
"""Keywords that indicate low severity (routine announcements)."""


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


def rule_based_severity(episode_body: str, source_count: int = 0) -> str:
    """Rule-driven severity classification for an Episodic node.

    Rules (checked in priority order, first match wins):
    1. source_count >= 5 → 'high'
    2. Body contains critical keyword → 'critical'
    3. Body contains high keyword → 'high'
    4. Body contains medium keyword → 'medium'
    5. Body contains low keyword → 'low'
    6. Default → 'medium'

    Args:
        episode_body: The body/content text of the Episodic node.
        source_count: Number of information sources.

    Returns:
        One of 'critical', 'high', 'medium', 'low'.
    """
    if source_count >= 5:
        return "high"

    for kw in CRITICAL_KEYWORDS:
        if kw in episode_body:
            return "critical"

    for kw in HIGH_KEYWORDS:
        if kw in episode_body:
            return "high"

    for kw in MEDIUM_KEYWORDS:
        if kw in episode_body:
            return "medium"

    for kw in LOW_KEYWORDS:
        if kw in episode_body:
            return "low"

    return "medium"


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------


def _build_unclassified_query() -> str:
    """Build Cypher to find Episodic nodes without severity."""
    return """
        MATCH (e:Episodic)
        WHERE e.severity IS NULL
           OR e.severity = ''
        RETURN e.uuid AS uuid, e.content AS body,
               0 AS source_count
        LIMIT 50
    """


def _build_update_query() -> str:
    """Build Cypher to set severity on an Episodic node."""
    return """
        MATCH (e:Episodic {uuid: $uuid})
        SET e.severity = $severity
        RETURN e.uuid AS uuid
    """


async def enrich_severity_batch(neo4j_driver: Driver) -> int:
    """Batch-enrich unclassified Episodic nodes with rule-based severity.

    Queries all Episodic nodes where severity is NULL or empty,
    applies rule_based_severity(), and writes the result to Neo4j.

    This is a non-critical path: failures are logged but do not
    block the pipeline. Unclassified nodes default to "medium".

    Args:
        neo4j_driver: Neo4j driver instance.

    Returns:
        Number of nodes enriched in this batch.
    """
    import asyncio

    try:
        neo4j_driver.verify_connectivity()
    except Exception as exc:
        logger.warning("severity_enricher: Neo4j unavailable, skipping: %s", exc)
        return 0

    try:
        # Query unclassified nodes
        records, _, _ = await asyncio.to_thread(
            neo4j_driver.execute_query,
            _build_unclassified_query(),
        )
    except Exception as exc:
        logger.warning("severity_enricher: query failed: %s", exc)
        return 0

    if not records:
        logger.debug("severity_enricher: no unclassified nodes found")
        return 0

    enriched = 0
    for record in records:
        uuid = record.get("uuid")
        body = record.get("body") or ""
        source_count = int(record.get("source_count", 0))

        severity = rule_based_severity(body, source_count)

        try:
            await asyncio.to_thread(
                neo4j_driver.execute_query,
                _build_update_query(),
                uuid=uuid,
                severity=severity,
            )
            enriched += 1
        except Exception as exc:
            logger.warning(
                "severity_enricher: failed to update %s: %s", uuid, exc
            )

    if enriched:
        logger.info(
            "severity_enricher: enriched %d/%d nodes",
            enriched,
            len(records),
        )

    return enriched


__all__ = [
    "rule_based_severity",
    "enrich_severity_batch",
    "CRITICAL_KEYWORDS",
    "HIGH_KEYWORDS",
    "MEDIUM_KEYWORDS",
    "LOW_KEYWORDS",
]
