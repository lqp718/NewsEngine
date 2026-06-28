"""FastAPI dependency injection for NewsEngine REST API.

Provides reusable FastAPI Depends functions that encapsulate the creation
and lifecycle management of shared resources (settings, Neo4j driver,
Graphiti SDK, EpisodeWriter, SectorBriefingAggregator).

All dependency functions reuse N4-0 infrastructure modules and SHALL NOT
reimplement equivalent logic.

Usage in endpoint signatures:
    from typing import Annotated
    from fastapi import Depends
    from src.api.deps import get_settings
    from src.core.config import Settings

    @router.get("/example")
    async def example_endpoint(
        settings: Annotated[Settings, Depends(get_settings)],
    ):
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends

if TYPE_CHECKING:
    from graphiti_core import Graphiti
    from neo4j import Driver

    from src.core.config import Settings
    from src.graphiti.episode_writer import EpisodeWriter

# ---------------------------------------------------------------------------
# Process-level singleton holder for SectorBriefingAggregator
# ---------------------------------------------------------------------------
_aggregator: Any | None = None


# ---------------------------------------------------------------------------
# Dependency functions (thin proxies to N4-0 modules)
# ---------------------------------------------------------------------------


def get_settings() -> Settings:
    """Provide the global Settings instance.

    Thin proxy — delegates to ``core/config.py::get_settings()``.
    Returns the singleton ``Settings`` instance with all configuration fields.
    """
    from src.core.config import get_settings as _get_settings

    return _get_settings()


def get_neo4j_driver() -> Driver:
    """Provide the global Neo4j Driver instance.

    Thin proxy — delegates to ``core/neo4j_client.py::get_neo4j_driver()``.
    Returns the singleton ``Driver`` with a verified connection pool.
    """
    from src.core.neo4j_client import get_neo4j_driver as _get_neo4j_driver

    return _get_neo4j_driver()


def get_graphiti() -> Graphiti:
    """Provide a new Graphiti instance per-call (factory pattern).

    Delegates to ``core/graphiti_client.py::create_graphiti()``.
    Passes ``graph_driver=None`` so Graphiti SDK creates its own internal
    Neo4j driver from configured credentials — see design.md AD-2.

    .. note::
        We intentionally do NOT share the N4-0 Neo4j driver with Graphiti:

        1. **Thread-safety**: Graphiti SDK's ``add_episode()`` is not
           thread-safe (design.md §3.5), and ``graphiti_core.driver.driver.GraphDriver``
           has a different interface than ``neo4j.Driver``. Passing a
           ``neo4j.Driver`` would risk ``AttributeError`` at runtime.

        2. **Isolation**: ``create_graphiti()`` receives ``uri``/``user``/
           ``password`` kwargs and creates its own driver internally when
           ``graph_driver=None``. This follows the Risk Mitigation strategy
           from design.md §1.6.

        3. **Per-call lifecycle**: Each ``get_graphiti()`` call creates a
           fresh ``Graphiti`` instance (factory pattern per AD-2). The
           SDK manages its own driver lifecycle internally.

    .. warning::
        Each call creates a new instance because Graphiti's ``add_episode()``
        is not thread-safe. Callers own the lifecycle.
    """
    from src.core.graphiti_client import create_graphiti

    return create_graphiti(graph_driver=None)


def get_episode_writer(
    graphiti: Graphiti = Depends(get_graphiti),
) -> EpisodeWriter:
    """Provide an EpisodeWriter configured with the shared Graphiti instance.

    Uses default entity/edge types from ``src/graphiti/entity_types.py`` and
    ``src/graphiti/relation_types.py``.
    """
    from src.graphiti.episode_writer import EpisodeWriter
    from src.graphiti.entity_types import SYMBOL_ENTITY_TYPES
    from src.graphiti.relation_types import DEFAULT_EDGE_TYPE_MAP, EDGE_TYPES

    return EpisodeWriter(
        graphiti=graphiti,
        entity_types=SYMBOL_ENTITY_TYPES,
        edge_types=EDGE_TYPES,
        edge_type_map=DEFAULT_EDGE_TYPE_MAP,
    )


def get_aggregator() -> Any:
    """Provide the process-level singleton SectorBriefingAggregator.

    The aggregator is lazily initialized on first call using a lazy import.
    It holds an in-memory cache shared across all API requests.

    .. note::
        ``SectorBriefingAggregator`` lives in ``src/ingestion/briefing_aggregator.py``,
        which is created in a subsequent N4 task. If the module does not exist yet,
        calling this function will raise a clear ``ImportError``.
    """
    global _aggregator

    if _aggregator is None:
        try:
            from src.ingestion.briefing_aggregator import (  # noqa: F811
                SectorBriefingAggregator,
            )
        except ImportError:
            raise ImportError(
                "SectorBriefingAggregator not available. "
                "Ensure src/ingestion/briefing_aggregator.py exists with "
                "a SectorBriefingAggregator class. "
                "This module is part of a subsequent N4 task (build order: "
                "deps.py first, then briefing_aggregator.py)."
            ) from None

        _aggregator = SectorBriefingAggregator()

    return _aggregator
