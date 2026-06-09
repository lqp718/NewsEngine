"""NewsEngine - Financial News Knowledge Graph Engine.

A system for ingesting financial news, extracting entities and relations,
and building a knowledge graph for analysis and insights.
"""

__version__ = "1.0.0"

# Export core modules for easy access
from . import core, utils

__all__ = ["core", "utils", "__version__"]