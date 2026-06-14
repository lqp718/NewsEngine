"""IngestionScheduler — multi-source ingestion scheduler.

Orchestrates two pipelines (macro pipeline: GDELT & RSS, stock pipeline:
AkShare) every 15 minutes, followed by SectorBriefingAggregator.aggregate_all().

V2.2: Macro/stock pipeline split:
- Macro pipeline (GDELT): uses 19 core theme OR matching, not ticker whitelist
- Macro pipeline (RSS): zero pre-ingestion filtering
- Stock pipeline (AkShare): ticker whitelist filtering only
- TTL cleanup: runs daily via DETACH DELETE (Layer 2)

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
from src.utils.time_utils import now_hkt

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

    Runs two pipelines (macro & stock) concurrently every ``interval_sec``
    seconds, then calls SectorBriefingAggregator.

    V2.2:
    - Macro pipeline: GDELT (theme filter) + RSS (zero filter)
    - Stock pipeline: AkShare (ticker whitelist filter)
    - TTL cleanup on daily schedule
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

        # ── TTL cleanup state (V2.2) ──────────────────────────────
        self._last_ttl_cleanup_date: str | None = None

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

        V2.2: Macro/Stock pipeline split — each adapter receives its
        own filtering configuration.
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

        self._writer = EpisodeWriter(
            graphiti=self._graphiti,
            neo4j_driver=self._neo4j_driver,
        )

        # ── Macro pipeline: GDELT uses macro theme keywords (not tickers) ──
        from src.adapters.macro_themes import MACRO_THEME_KEYWORDS

        self._gdelt_adapter = GdeltAdapter(
            macro_theme_keywords=MACRO_THEME_KEYWORDS,
            dedup_cache=self._dedup_cache,
        )

        # ── Macro pipeline: RSS uses zero filtering (no tickers) ───
        self._rss_adapter = RssAdapter(
            feed_urls=self._feed_urls,
            dedup_cache=self._dedup_cache,
        )

        # ── Stock pipeline: AkShare uses ticker whitelist ──────────
        self._akshare_adapter = AkShareAdapter(
            dedup_cache=self._dedup_cache,
        )

        logger.info(
            "Scheduler components initialized: "
            "GDELT[theme-filter] + RSS[zero-filter](macro pipeline), "
            "AkShare[ticker-filter](stock pipeline), "
            "EpisodeWriter, SectorBriefingAggregator"
        )

    # ── Internal: cycle loop ──────────────────────────────────────────

    async def _cycle_loop(self) -> None:
        """Main cycle loop. Runs until ``self._running`` is False."""
        while self._running:
            cycle_start = time.monotonic()
            logger.info("=== Ingestion cycle starting ===")

            try:
                # V2.2: TTL cleanup at start of each cycle
                await self._ttl_cleanup()

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

        1. Read ticker whitelist (only for AkShare stock pipeline)
        2. Run pipelines concurrently with error isolation
        3. Call SectorBriefingAggregator
        """
        # ── Step 1: Read ticker whitelist — only for AkShare ────
        tickers = get_ticker_whitelist(self._whitelist_path)
        sector_names = _extract_sector_names(tickers)
        logger.info(
            "Cycle: %d tickers, %d sectors",
            len(tickers),
            len(sector_names),
        )

        # Update adapter ticker whitelists — only AkShare needs tickers
        self._update_adapter_tickers(tickers)

        # ── Step 2: Run pipelines concurrently ───────────────────
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

        # ── Step 3: Severity enrichment (L-4 rule engine) ────────
        try:
            from .severity_enricher import enrich_severity_batch

            enriched = await enrich_severity_batch(self._neo4j_driver)
            if enriched:
                logger.info(
                    "Severity enrichment: classified %d Episodic nodes",
                    enriched,
                )
        except Exception as exc:
            logger.warning(
                "Severity enrichment failed (non-critical): %s",
                exc,
                exc_info=True,
            )

        # ── Step 4: Briefing aggregation ─────────────────────────
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
        """Update ticker whitelists — only for AkShare stock pipeline.

        V2.2: GDELT/RSS macro pipeline no longer uses ticker whitelist.
        Only AkShare receives ticker updates.
        """
        # GDELT: no longer receives ticker whitelist (uses macro theme filter)
        # RSS: no longer receives ticker whitelist (zero filter)
        if self._akshare_adapter is not None:
            self._akshare_adapter.ticker_whitelist = tickers
            # AkShareAdapter rebuilds symbol map from whitelist each cycle
            self._akshare_adapter._symbol_map = {}
            for entry in tickers:
                biz_code = entry.get("biz_code", "")
                if biz_code:
                    self._akshare_adapter._symbol_map[biz_code] = entry

    # ── TTL Cleanup (V2.2 Layer 2) ─────────────────────────────────────

    async def _ttl_cleanup(self) -> dict[str, int]:
        """分级 TTL 清理：DETACH DELETE 过期 Episodic 节点。

        每天执行 1 次，在 cycle 开始前检查 last_ttl_cleanup_date。

        Layer 2（存储层）：DETACH DELETE 直接删除过期节点。
        Layer 1（查询层）在 API 端 Cypher WHERE 中实现。

        Returns:
            各 scope 的删除数量字典。
        """
        settings = get_settings()
        today = now_hkt().strftime("%Y-%m-%d")

        if self._last_ttl_cleanup_date == today:
            return {"skipped": 0}

        results: dict[str, int] = {}
        ttl_configs = [
            ("SYMBOL", settings.episode_ttl_symbol_days),
            ("SECTOR", settings.episode_ttl_sector_days),
            ("MACRO", settings.episode_ttl_macro_days),
        ]

        for scope, days in ttl_configs:
            try:
                with self._neo4j_driver.session() as session:
                    query = """
                    MATCH (ep:Episodic)
                    WHERE ep.episode_metadata CONTAINS $scope
                      AND ep.created_at < datetime() - duration({days: $days})
                    DETACH DELETE ep
                    RETURN count(ep) AS deleted
                    """
                    result = session.run(
                        query,
                        {"scope": scope, "days": days},
                    )
                    record = result.single()
                    deleted = record["deleted"] if record else 0
                    results[scope] = deleted
                    if deleted > 0:
                        logger.info(
                            "TTL cleanup [%s]: deleted %d episodes (ttl=%dd)",
                            scope,
                            deleted,
                            days,
                        )
            except Exception as exc:
                logger.warning(
                    "TTL cleanup [%s] failed: %s",
                    scope,
                    exc,
                    exc_info=True,
                )
                results[f"{scope}_error"] = 1

        # 清理孤儿 Entity 节点（无关联 RELATES_TO 的 Entity）
        try:
            orphan_query = """
            MATCH (e:Entity)
            WHERE NOT (e)-[:RELATES_TO]-()
            DELETE e
            RETURN count(e) AS deleted
            """
            with self._neo4j_driver.session() as session:
                orphan_result = session.run(orphan_query)
                orphan_record = orphan_result.single()
                orphan_deleted = orphan_record["deleted"] if orphan_record else 0
            if orphan_deleted > 0:
                results["orphan_entities"] = orphan_deleted
                logger.info(
                    "TTL cleanup: deleted %d orphan Entity nodes",
                    orphan_deleted,
                )
        except Exception as exc:
            logger.warning(
                "TTL cleanup [orphan] failed: %s",
                exc,
                exc_info=True,
            )

        self._last_ttl_cleanup_date = today
        logger.info(
            "TTL cleanup complete: %s",
            ", ".join(f"{k}={v}" for k, v in results.items()),
        )
        return results

    # ── Logging ─────────────────────────────────────────────────────────

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
