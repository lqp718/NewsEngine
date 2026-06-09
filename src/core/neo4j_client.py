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


__all__ = ["get_neo4j_driver", "close_neo4j_driver"]