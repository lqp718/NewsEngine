"""Neo4j driver management - provides a global singleton for Neo4j connections.

This module manages a single Neo4j driver instance with lazy initialization,
connection pool configuration, and graceful shutdown capabilities.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from neo4j import GraphDatabase

if TYPE_CHECKING:
    from neo4j import Driver

from .config import get_settings

# Global driver instance - initialized lazily
_driver: Driver | None = None
_logger = logging.getLogger(__name__)


def get_neo4j_driver() -> Driver:
    """Get the global Neo4j driver instance (lazy initialization).
    
    Creates a new driver instance on first call, verifies connectivity,
    and returns the same instance on subsequent calls.
    """
    global _driver
    if _driver is None:
        settings = get_settings()
        
        # Create driver with connection pool configuration
        _driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_lifetime=3600,  # 1 hour
            max_connection_pool_size=50,
        )
        
        # Verify connectivity
        try:
            _driver.verify_connectivity()
            _logger.info(f"Neo4j connection successful ({settings.neo4j_uri})")
        except Exception as e:
            _logger.error(f"Failed to connect to Neo4j: {e}")
            raise
    
    return _driver


def close_neo4j_driver() -> None:
    """Close the global Neo4j driver connection (graceful shutdown).
    
    This function is idempotent - calling it multiple times is safe.
    """
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
        _logger.info("Neo4j connection closed")


# === Graphiti 共享 driver（P0-G2: 消除双驱动/双连接池） ===
# graphiti-core 的 ops 依赖其自身的 GraphDriver 接口（.provider / execute_query /
# .session()），与应用程序层同步 neo4j.Driver 不同。这里持有唯一的 Graphiti
# Neo4jDriver 实例，`create_graphiti(graph_driver=...)` 与 EpisodeWriter 的
# ticker 接地/severity Cypher 共用，避免连接数翻倍与配置漂移。
_graphiti_driver: Any | None = None


def get_graphiti_driver() -> Any:
    """Lazy singleton of graphiti_core's Neo4jDriver (async, shared).

    Uses the same uri/user/password as the app-layer driver but owns its
    own connection pool (graphiti ops run async). Callers must not close
    it individually; close via :func:`close_graphiti_driver` on shutdown.
    """
    global _graphiti_driver
    if _graphiti_driver is None:
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        settings = get_settings()
        _graphiti_driver = Neo4jDriver(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
        )
        _logger.info("Graphiti Neo4jDriver created (shared)")
    return _graphiti_driver


async def close_graphiti_driver() -> None:
    """Close the shared Graphiti driver (idempotent). 须在事件循环内调用。"""
    global _graphiti_driver
    if _graphiti_driver is not None:
        driver, _graphiti_driver = _graphiti_driver, None
        try:
            await driver.close()
        except Exception:
            _logger.warning("Graphiti Neo4jDriver close failed", exc_info=True)
        _logger.info("Graphiti Neo4jDriver closed")


__all__ = [
    "get_neo4j_driver",
    "close_neo4j_driver",
    "get_graphiti_driver",
    "close_graphiti_driver",
]