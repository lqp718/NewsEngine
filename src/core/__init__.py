"""Core infrastructure modules for NewsEngine.

This package contains essential infrastructure components:
- Configuration management (config.py)
- Database connectivity (neo4j_client.py)  
- Graphiti SDK integration (graphiti_client.py)
"""

from .config import (
    Settings,
    get_settings,
    reload_settings,
    PROJECT_ROOT,
)
from .neo4j_client import (
    get_neo4j_driver,
    close_neo4j_driver,
)

__all__ = [
    # Config exports
    "Settings",
    "get_settings", 
    "reload_settings",
    "PROJECT_ROOT",
    # Neo4j client exports
    "get_neo4j_driver",
    "close_neo4j_driver",
]