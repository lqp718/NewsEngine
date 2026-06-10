"""IngestionScheduler — multi-source ingestion scheduler.

Orchestrates three adapter pipelines (GDELT, RSS, AkShare) every 15 minutes,
followed by SectorBriefingAggregator.aggregate_all().

Lifecycle:
    scheduler = IngestionScheduler(...)
    await scheduler.start()   # starts the cycle loop
    await scheduler.stop()    # graceful shutdown (waits for current cycle)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from neo4j import Driver

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver
from src.utils.logging_config import get_logger

from .briefing_aggregator import SectorBriefingAggregator
from .pipeline import PipelineResult, run_pipeline

logger = get_logger(__name__)

# ── Default RSS feed URLs (financial news feeds) ───────────────────────

_DEFAULT_RSS_FEEDS: list[str] = [
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://www.ft.com/content?format=rss",
]


def get_ticker_whitelist(cache_path: str) -> list[dict[str, str]]:
    """Read ticker whitelist from local cache file (Push mode).

    The cache file is written by the POST /api/tickers/whitelist endpoint
    when SynapseEngine pushes the whitelist.

    Supports two file formats:
        - {"tickers": [...]} (API push format)
        - [...] (direct list format)

    Returns:
        List of ticker dicts, or empty list if cache file missing/invalid.
    """
    if not os.path.exists(cache_path):
        logger.warning(
            "Ticker whitelist cache not found: %s. "
            "Waiting for SynapseEngine to push via POST /api/tickers/whitelist.",
            cache_path,
        )
        return []

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            tickers = data.get("tickers", [])
        elif isinstance(data, list):
            tickers = data
        else:
            logger.warning(
                "Unexpected ticker whitelist format (type=%s)", type(data).__name__
            )
            return []

        logger.info("Loaded %d tickers from whitelist cache: %s", len(tickers), cache_path)
        return tickers

    except json.JSONDecodeError as exc:
        logger.warning("Ticker whitelist cache corrupt: %s", exc)
        return []
    except OSError as exc:
        logger.warning("Failed to read ticker whitelist cache: %s", exc)
        return []


def _extract_sector_names(tickers: list[dict[str, str]]) -> list[str]:
    """Extract unique sector names from ticker whitelist.

    Returns deduplicated, non-empty sector names.
    """
    sectors: set[str] = set()
    for t in tickers:
        sector = t.get("sector", "").strip()
        if sector:
            sectors.add(sector)
    return sorted(sectors)


# ── IngestionScheduler ─────────────────────────────────────────────────


class IngestionScheduler:
    """Multi-source ingestion scheduler.

    Runs three adapter pipelines (GDELT, RSS, AkShare) concurrently every
    ``interval_sec`` seconds, then calls SectorBriefingAggregator.
    """

    def __init__(
        self,
        neo4j_driver: Driver | None = None,
        graphiti: Any | None = None,
        feed_urls: list[str] | None = None,
        whitelist_path: str | None = None,
        interval_sec: int | None = None,
    ) -> None:
        """Initialize scheduler and create all sub-components.

        Args:
            neo4j_driver: Shared Neo4j driver. If None, uses global driver.
            graphiti: Graphiti instance for EpisodeWriter.
            feed_urls: RSS feed URLs. If None, uses defaults.
            whitelist_path: Path to ticker whitelist cache file.
            interval_sec: Ingestion cycle interval.
        """
        settings = get_settings()

        self._neo4j_driver = neo4j_driver or get_neo4j_driver()
        self._graphiti = graphiti
        self._feed_urls = feed_urls or _DEFAULT_RSS_FEEDS
        self._whitelist_path = whitelist_path or str(
            os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ),
                settings.ticker_whitelist_file,
            )
        )
        self._interval_sec = interval_sec or settings.ingestion_interval_sec

        # ── Shared dedup cache (across all adapters) ──────────────
        self._dedup_cache: set[str] = set()

        # ── EpisodeWriter (lazy init) ─────────────────────────────
        self._writer: Any = None

        # ── Adapter instances (lazy init) ─────────────────────────
        self._gdelt_adapter: Any = None
        self._rss_adapter: Any = None
        self._akshare_adapter: Any = None

        # ── Briefing Aggregator ───────────────────────────────────
        self._aggregator = SectorBriefingAggregator()

        # ── Lifecycle state ───────────────────────────────────────
        self._running = False
        self._task: asyncio.Task[None] | None = None

        logger.info(
            "IngestionScheduler initialized (interval=%ds, whitelist=%s)",
            self._interval_sec,
            self._whitelist_path,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the ingestion cycle loop.

        The first cycle runs immediately (no initial 15-minute wait).
        """
        if self._running:
            logger.warning("IngestionScheduler already running")
            return

        self._running = True
        self._lazy_init_components()
        self._task = asyncio.create_task(self._cycle_loop())
        logger.info("IngestionScheduler started")

    async def stop(self) -> None:
        """Graceful shutdown.

        Cancels the next scheduled cycle and waits for the current
        cycle to complete if one is in progress.
        """
        self._running = False

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info("IngestionScheduler stopped")

    # ── Internal: component lazy init ─────────────────────────────────

    def _lazy_init_components(self) -> None:
        """Create adapter and writer instances on first cycle.

        Delayed from __init__ to allow caller to set up dependencies
        (Neo4j, Graphiti) before the scheduler wires them.
        """
        if self._writer is not None:
            return

        # ── Import adapter classes (lazy, to avoid circular deps) ───
        from src.adapters.gdelt_adapter import GdeltAdapter
        from src.adapters.rss_adapter import RssAdapter
        from src.adapters.akshare_adapter import AkShareAdapter
        from src.graphiti.episode_writer import EpisodeWriter
        from src.core.graphiti_client import create_graphiti

        if self._graphiti is None:
            self._graphiti = create_graphiti()

        self._writer = EpisodeWriter(graphiti=self._graphiti)

        self._gdelt_adapter = GdeltAdapter(
            dedup_cache=self._dedup_cache,
        )
        self._rss_adapter = RssAdapter(
            feed_urls=self._feed_urls,
            dedup_cache=self._dedup_cache,
        )
        self._akshare_adapter = AkShareAdapter(
            dedup_cache=self._dedup_cache,
        )

        logger.info(
            "Scheduler components initialized: GDELT+RSS+AkShare adapters, "
            "EpisodeWriter, SectorBriefingAggregator"
        )

    # ── Internal: cycle loop ──────────────────────────────────────────

    async def _cycle_loop(self) -> None:
        """Main cycle loop. Runs until ``self._running`` is False."""
        while self._running:
            cycle_start = time.monotonic()
            logger.info("=== Ingestion cycle starting ===")

            try:
                results = await self._run_cycle()
                await self._log_cycle_summary(results, cycle_start)
            except asyncio.CancelledError:
                logger.info("Ingestion cycle cancelled")
                raise
            except Exception as exc:
                logger.critical(
                    "Ingestion cycle crashed: %s",
                    exc,
                    exc_info=True,
                )

            # Sleep until next cycle (check running flag every second)
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, self._interval_sec - elapsed)
            logger.info(
                "=== Ingestion cycle complete (%.1fs, next in %.0fs) ===",
                elapsed,
                remaining,
            )

            # Graceful cancel-aware sleep
            for _ in range(int(remaining)):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _run_cycle(self) -> list[PipelineResult]:
        """Execute one full ingestion cycle.

        1. Read ticker whitelist
        2. Run 3 pipelines concurrently with error isolation
        3. Call SectorBriefingAggregator
        """
        # ── Step 1: Read ticker whitelist ───────────────────────────
        tickers = get_ticker_whitelist(self._whitelist_path)
        sector_names = _extract_sector_names(tickers)
        logger.info(
            "Cycle: %d tickers, %d sectors",
            len(tickers),
            len(sector_names),
        )

        # Update adapter ticker whitelists for this cycle
        self._update_adapter_tickers(tickers)

        # ── Step 2: Run 3 pipelines concurrently ───────────────────
        pipeline_results: list[PipelineResult] = []

        coros = [
            self._run_adapter_pipeline(self._gdelt_adapter, tickers),
            self._run_adapter_pipeline(self._rss_adapter, tickers),
            self._run_adapter_pipeline(self._akshare_adapter, tickers),
        ]

        completed = await asyncio.gather(*coros, return_exceptions=True)

        for i, result in enumerate(completed):
            if isinstance(result, Exception):
                source_names = ["gdelt_csv", "rss", "akshare"]
                logger.error(
                    "Adapter pipeline [%s] threw unhandled exception: %s",
                    source_names[i] if i < len(source_names) else f"adapter_{i}",
                    result,
                    exc_info=True,
                )
                pipeline_results.append(
                    PipelineResult(
                        source_type=source_names[i] if i < len(source_names) else f"adapter_{i}",
                        success=False,
                        error=result,
                    )
                )
            else:
                pipeline_results.append(result)

        # ── Step 3: Briefing aggregation ───────────────────────────
        if sector_names and self._aggregator:
            try:
                briefing_results = await self._aggregator.aggregate_all(sector_names)
                briefing_count = sum(
                    1 for v in briefing_results.values() if v is not None
                )
                logger.info(
                    "Briefing aggregation: %d/%d sectors updated",
                    briefing_count,
                    len(sector_names),
                )
            except Exception as exc:
                logger.error(
                    "Briefing aggregation failed: %s",
                    exc,
                    exc_info=True,
                )

        return pipeline_results

    async def _run_adapter_pipeline(
        self,
        adapter: Any,
        tickers: list[dict[str, str]],
    ) -> PipelineResult:
        """Run a single adapter pipeline with error isolation."""
        try:
            return await run_pipeline(
                adapter=adapter,
                writer=self._writer,
                tickers=tickers,
            )
        except Exception as exc:
            source_type = getattr(adapter, "SOURCE_TYPE", type(adapter).__name__)
            logger.error(
                "Pipeline [%s] failed: %s", source_type, exc, exc_info=True
            )
            return PipelineResult(
                source_type=source_type,
                success=False,
                error=exc,
            )

    def _update_adapter_tickers(
        self, tickers: list[dict[str, str]]
    ) -> None:
        """Update ticker whitelists on all adapters for this cycle.

        The adapters use the whitelist for filtering and symbol lookup.
        """
        if self._gdelt_adapter is not None:
            self._gdelt_adapter.ticker_whitelist = tickers
        if self._rss_adapter is not None:
            self._rss_adapter.ticker_whitelist = tickers
        if self._akshare_adapter is not None:
            self._akshare_adapter.ticker_whitelist = tickers
            # AkShareAdapter rebuilds symbol map from whitelist each cycle
            self._akshare_adapter._symbol_map = {}
            for entry in tickers:
                biz_code = entry.get("biz_code", "")
                if biz_code:
                    self._akshare_adapter._symbol_map[biz_code] = entry

    async def _log_cycle_summary(
        self,
        results: list[PipelineResult],
        cycle_start: float,
    ) -> None:
        """Log a summary of the cycle's results."""
        total_time = time.monotonic() - cycle_start
        total_episodes = sum(r.episode_count for r in results)
        source_stats = ", ".join(
            f"{r.source_type}={r.episode_count}ep{'!' if not r.success else ''}"
            for r in results
        )

        all_failed = all(not r.success for r in results)
        if all_failed:
            logger.critical(
                "Cycle: ALL sources failed [%.1fs] %s",
                total_time,
                source_stats,
            )
        else:
            failed = [r.source_type for r in results if not r.success]
            if failed:
                logger.warning(
                    "Cycle: partial failures [%.1fs] total=%dep, failed=[%s] %s",
                    total_time,
                    total_episodes,
                    ", ".join(failed),
                    source_stats,
                )
            else:
                logger.info(
                    "Cycle: all sources OK [%.1fs] total=%dep %s",
                    total_time,
                    total_episodes,
                    source_stats,
                )


__all__ = [
    "IngestionScheduler",
    "get_ticker_whitelist",
]
