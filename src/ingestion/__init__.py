"""NewsEngine Ingestion Module.

Orchestration layer that wires N2 adapters, N3 graphiti, N3 sync,
and N4-0 core infrastructure into a running ingestion service.

Modules:
    scheduler.py        — multi-source ingestion orchestration
    pipeline.py         — fetch → normalize → dedup → write → health
    briefing_aggregator — sector briefing generation via Neo4j + LLM
"""

from .briefing_aggregator import BriefingCacheEntry, SectorBriefingAggregator
from .pipeline import PipelineResult, SourceHealth, run_pipeline, run_pipeline_single
from .scheduler import IngestionScheduler, get_ticker_whitelist

__all__ = [
    # scheduler
    "IngestionScheduler",
    "get_ticker_whitelist",
    # pipeline
    "PipelineResult",
    "SourceHealth",
    "run_pipeline",
    "run_pipeline_single",
    # briefing_aggregator
    "SectorBriefingAggregator",
    "BriefingCacheEntry",
]
