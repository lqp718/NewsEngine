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
# Path to RSS feed configuration JSON file
_RSS_FEEDS_JSON_PATH: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "rss_feeds.json",
)

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
        min_cycle_gap_sec: int | None = None,
        dry_run: bool = False,
        source_filter: str | None = None,
        fetch_content: bool = False,
    ) -> None:
        """Initialize scheduler and create all sub-components.

        Args:
            neo4j_driver: Shared Neo4j driver. If None, uses global driver.
            graphiti: Graphiti instance for EpisodeWriter.
            feed_urls: RSS feed URLs. If None, uses defaults.
            whitelist_path: Path to ticker whitelist cache file.
            interval_sec: Ingestion cycle interval.
            min_cycle_gap_sec: Minimum gap between cycles.
            dry_run: When True, skip Graphiti/EpisodeWriter/Neo4j initialization.
            source_filter: Filter adapters ("gdelt", "rss", "akshare", None for all).
            fetch_content: When True, enable ContentFetcher for RSS (dry-run mode).
        """
        self._dry_run = dry_run
        self._source_filter = source_filter
        self._fetch_content = fetch_content

        settings = get_settings()

        self._neo4j_driver = neo4j_driver
        self._graphiti = graphiti
        self._feed_urls = feed_urls or _DEFAULT_RSS_FEEDS
        self._feed_urls_explicit = feed_urls is not None
        self._whitelist_path = whitelist_path or str(
            os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ),
                settings.ticker_whitelist_file,
            )
        )
        self._interval_sec = interval_sec or settings.ingestion_interval_sec
        self._min_cycle_gap_sec = min_cycle_gap_sec or settings.min_cycle_gap_sec

        # ── Shared dedup cache (across all adapters) ──────────────
        self._dedup_cache: set[str] = set()

        # ── EpisodeWriter instances (lazy init) ────────────────────
        self._macro_writer: Any = None
        self._symbol_writer: Any = None

        # ── Adapter instances (lazy init) ─────────────────────────
        self._gdelt_adapter: Any = None
        self._rss_adapter: Any = None
        self._cls_adapter: Any = None  # V6.1: CLS telegraph (primary stock news)
        self._eastmoney_adapter: Any = None  # V6.1: demoted to fallback
        self._akshare_adapter: Any = None

        # ── Briefing Aggregator ───────────────────────────────────
        self._aggregator = SectorBriefingAggregator() if not dry_run else None

        # ── TTL cleanup state (V2.2) ──────────────────────────────
        self._last_ttl_cleanup_date: str | None = None

        # ── Lazy-init guard ───────────────────────────────────────
        self._components_initialized: bool = False

        # ── Lifecycle state ───────────────────────────────────────
        self._running = False
        self._task: asyncio.Task[None] | None = None

        logger.info(
            "IngestionScheduler initialized (interval=%ds, whitelist=%s, dry_run=%s, source_filter=%s)",
            self._interval_sec,
            self._whitelist_path,
            dry_run,
            source_filter or "all",
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

    # ── Internal: RSS feed loading ─────────────────────────────────────

    def _load_rss_feeds(self) -> list[str]:
        """Load RSS feed URLs from ``data/rss_feeds.json``.

        Reads the JSON config file, filters to ``enabled: true`` feeds,
        and returns the list of feed URLs. Falls back to ``_DEFAULT_RSS_FEEDS``
        if the file is missing, invalid, or all feeds are disabled.

        Returns:
            List of RSS feed URL strings.
        """
        rss_json_path = getattr(self, '_rss_json_path', _RSS_FEEDS_JSON_PATH)
        if not os.path.exists(rss_json_path):
            logger.warning(
                "rss_feeds.json not found at %s, using default feeds",
                rss_json_path,
            )
            return list(_DEFAULT_RSS_FEEDS)

        try:
            with open(rss_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "rss_feeds.json parse error: %s, using default feeds",
                exc,
            )
            return list(_DEFAULT_RSS_FEEDS)

        feeds = data.get("feeds", [])
        if not isinstance(feeds, list) or not feeds:
            logger.warning(
                "rss_feeds.json has no 'feeds' array, using default feeds"
            )
            return list(_DEFAULT_RSS_FEEDS)

        enabled_urls: list[str] = []
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            if feed.get("enabled", True) and feed.get("url"):
                enabled_urls.append(feed["url"])

        if not enabled_urls:
            logger.warning(
                "rss_feeds.json has no enabled feeds, using default feeds"
            )
            return list(_DEFAULT_RSS_FEEDS)

        logger.info(
            "Loaded %d RSS feeds from %s",
            len(enabled_urls),
            rss_json_path,
        )
        return enabled_urls

    # ── Internal: component lazy init ─────────────────────────────────

    def _resolve_sources(self) -> set[str]:
        """Resolve which data sources to run based on source_filter.

        Returns:
            Set of source names ("gdelt", "rss", "akshare").
        """
        source_filter = self._source_filter
        if source_filter is None or source_filter == "all":
            return {"gdelt", "rss", "akshare"}
        return {source_filter}

    def _lazy_init_components(self) -> None:
        """Create adapter and writer instances on first cycle.

        Delayed from __init__ to allow caller to set up dependencies
        (Neo4j, Graphiti) before the scheduler wires them.

        V2.2: Macro/Stock pipeline split — each adapter receives its
        own filtering configuration.

        In dry-run mode: skips Graphiti, EpisodeWriter, Neo4j connections
        and only creates the requested adapters.
        """
        if self._components_initialized:
            return
        self._components_initialized = True

        # ── Import adapter classes (lazy, to avoid circular deps) ───
        from src.adapters.gdelt_adapter import GdeltAdapter
        from src.adapters.rss_adapter import RssAdapter
        from src.adapters.akshare_adapter import AkShareAdapter

        # ── Resolve which sources to instantiate ───────────────────
        sources = self._resolve_sources()

        # ── In normal mode, create Graphiti + EpisodeWriter ────────
        if not self._dry_run:
            from src.graphiti.episode_writer import EpisodeWriter
            from src.graphiti.entity_types import MACRO_ENTITY_TYPES, SYMBOL_ENTITY_TYPES
            from src.core.graphiti_client import create_graphiti

            if self._neo4j_driver is None:
                from src.core.neo4j_client import get_neo4j_driver
                self._neo4j_driver = get_neo4j_driver()

            if self._graphiti is None:
                self._graphiti = create_graphiti()

            self._macro_writer = EpisodeWriter(
                graphiti=self._graphiti,
                neo4j_driver=self._neo4j_driver,
                entity_types=MACRO_ENTITY_TYPES,
            )

            self._symbol_writer = EpisodeWriter(
                graphiti=self._graphiti,
                neo4j_driver=self._neo4j_driver,
                entity_types=SYMBOL_ENTITY_TYPES,
            )
        else:
            logger.info("Dry-run mode: skipping Graphiti/EpisodeWriter/Neo4j initialization")

        # ── Initialize ContentFetcher conditionally ────────────────
        # ContentFetcher is shared between GDELT and RSS adapters
        content_fetcher = None
        if self._fetch_content:
            from src.utils.content_fetcher import ContentFetcher
            content_fetcher = ContentFetcher(
                timeout=30,
                max_concurrent=5,
                batch_size=50,
                batch_cooldown=5.0,
            )
            logger.info("Dry-run: ContentFetcher enabled (V2.4 batch scheduling, network_idle=False)")
        elif not self._dry_run:
            # Normal mode: always enable ContentFetcher
            from src.utils.content_fetcher import ContentFetcher
            content_fetcher = ContentFetcher(
                timeout=30,
                max_concurrent=5,
                batch_size=50,
                batch_cooldown=5.0,
            )
            logger.info("ContentFetcher enabled for GDELT and RSS (network_idle=False)")

        # ── Macro pipeline: GDELT uses macro theme keywords (not tickers) ──
        if "gdelt" in sources:
            from src.adapters.macro_themes import MACRO_THEME_KEYWORDS

            self._gdelt_adapter = GdeltAdapter(
                macro_theme_keywords=MACRO_THEME_KEYWORDS,
                dedup_cache=self._dedup_cache,
                content_fetcher=content_fetcher,
            )
            logger.debug("GdeltAdapter initialized (Plan D filter, content_fetcher=%s)", "yes" if content_fetcher else "no")
        else:
            logger.info("Dry-run: GDELT adapter skipped (source_filter=%s)", self._source_filter)

        # ── Macro pipeline: RSS uses zero filtering (no tickers) ───
        if "rss" in sources:
            # Load RSS feeds from JSON config, with explicit feed_urls override
            rss_urls = self._feed_urls
            # Only load from JSON if feed_urls was not explicitly provided
            if rss_urls == _DEFAULT_RSS_FEEDS and not getattr(self, '_feed_urls_explicit', False):
                rss_urls = self._load_rss_feeds()

            self._rss_adapter = RssAdapter(
                feed_urls=rss_urls,
                dedup_cache=self._dedup_cache,
                content_fetcher=content_fetcher,
            )
        else:
            logger.info("Dry-run: RSS adapter skipped (source_filter=%s)", self._source_filter)

        # ── Stock pipeline: CLS (primary) → EastMoney (fallback) → AkShare (fallback) ──
        # V6.1: CLS telegraph replaces EastMoney as primary stock news source
        if "akshare" in sources:
            from src.adapters.cls_adapter import CLSAdapter
            from src.adapters.eastmoney_adapter import EastMoneyAdapter

            settings = get_settings()
            
            # V6.1: CLS telegraph as primary stock news source
            self._cls_adapter = CLSAdapter(
                page_size=getattr(settings, 'cls_page_size', 50),
                dedup_cache=self._dedup_cache,
            )
            
            # V6.1: EastMoney demoted to fallback
            self._eastmoney_adapter = EastMoneyAdapter(
                page_size=settings.eastmoney_page_size,
                dedup_cache=self._dedup_cache,
                content_fetcher=content_fetcher,
            )

            self._akshare_adapter = AkShareAdapter(
                dedup_cache=self._dedup_cache,
                content_fetcher=content_fetcher,
            )
        else:
            logger.info("Dry-run: CLS/EastMoney/AkShare adapters skipped (source_filter=%s)", self._source_filter)

        logger.info(
            "Scheduler components initialized: "
            "GDELT=%s, RSS=%s feeds, CLS=%s, EastMoney=%s, AkShare=%s"
            "%s",
            "yes" if self._gdelt_adapter else "no",
            len(self._rss_adapter.feed_urls) if self._rss_adapter else "no",
            "yes" if self._cls_adapter else "no",
            "yes" if self._eastmoney_adapter else "no",
            "yes" if self._akshare_adapter else "no",
            " [dry-run mode]" if self._dry_run else ""
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
            remaining = max(float(self._min_cycle_gap_sec), self._interval_sec - elapsed)
            logger.info(
                "=== Ingestion cycle complete (%.1fs, guard=%.1fs, next in %.1fs) ===",
                elapsed,
                self._min_cycle_gap_sec,
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
        # Macro pipelines (GDELT, RSS) use MACRO_ENTITY_TYPES
        # Stock pipeline (AkShare) uses SYMBOL_ENTITY_TYPES
        pipeline_results: list[PipelineResult] = []

        # ── Step 2a: Macro pipelines run concurrently ──────────────
        async def _stock_pipeline() -> PipelineResult:
            """Stock pipeline: CLS (primary) → EastMoney (fallback) → AkShare (fallback).
            
            V6.1: CLS telegraph replaces EastMoney as primary stock news source.
            """
            # V6.1: CLS telegraph as primary
            cls_result = await self._run_adapter_pipeline(
                self._cls_adapter, self._symbol_writer, tickers
            )
            if cls_result.success and cls_result.episode_count > 0:
                logger.info(
                    "Stock pipeline: CLS returned %d episodes",
                    cls_result.episode_count,
                )
                return cls_result

            # V6.1: EastMoney as fallback
            logger.info(
                "CLS returned 0 episodes, falling back to EastMoney"
            )
            em_result = await self._run_adapter_pipeline(
                self._eastmoney_adapter, self._symbol_writer, tickers
            )
            if em_result.success and em_result.episode_count > 0:
                logger.info(
                    "Stock pipeline: EastMoney returned %d episodes",
                    em_result.episode_count,
                )
                return em_result

            # Fallback to AkShare
            logger.info(
                "EastMoney returned 0 episodes, falling back to AkShare"
            )
            return await self._run_adapter_pipeline(
                self._akshare_adapter, self._symbol_writer, tickers
            )

        macro_coros = [
            self._run_adapter_pipeline(self._gdelt_adapter, self._macro_writer, tickers),
            self._run_adapter_pipeline(self._rss_adapter, self._macro_writer, tickers),
        ]

        # Run macro pipelines + stock pipeline concurrently
        all_coros = [*macro_coros, _stock_pipeline()]

        completed = await asyncio.gather(*all_coros, return_exceptions=True)

        source_names = ["gdelt_csv", "rss", "stock"]
        for i, result in enumerate(completed):
            if isinstance(result, Exception):
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
        writer: Any,
        tickers: list[dict[str, str]],
    ) -> PipelineResult:
        """Run a single adapter pipeline with error isolation."""
        try:
            return await run_pipeline(
                adapter=adapter,
                writer=writer,
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
        """Update ticker whitelists for CLS, EastMoney and AkShare stock pipelines.

        V6.1: CLS adapter now primary, EastMoney demoted to fallback.
        - CLSAdapter: name-based entity extraction (uses stock Chinese name)
        - EastMoneyAdapter: name-based search (uses stock Chinese name)
        - AkShareAdapter: symbol-based search (uses biz_code)

        GDELT/RSS macro pipeline no longer uses ticker whitelist.
        """
        # GDELT: no longer receives ticker whitelist (uses macro theme filter)
        # RSS: no longer receives ticker whitelist (zero filter)

        # V6.1: CLSAdapter: index by stock name for entity extraction
        if self._cls_adapter is not None:
            self._cls_adapter.ticker_whitelist = tickers
            self._cls_adapter._name_map = {}
            for entry in tickers:
                name = entry.get("name", "")
                if name:
                    self._cls_adapter._name_map[name] = entry

        # EastMoneyAdapter: index by stock name (fallback)
        if self._eastmoney_adapter is not None:
            self._eastmoney_adapter.ticker_whitelist = tickers
            self._eastmoney_adapter._name_map = {}
            for entry in tickers:
                name = entry.get("name", "")
                if name:
                    self._eastmoney_adapter._name_map[name] = entry

        # AkShareAdapter: index by biz_code (fallback)
        if self._akshare_adapter is not None:
            self._akshare_adapter.ticker_whitelist = tickers
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
            DETACH DELETE e
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

    # ── Dry-run cycle (one-shot) ──────────────────────────────────────────

    async def run_dry_cycle(self) -> list[PipelineResult]:
        """Execute one-shot dry-run pipeline.

        Runs all instantiated adapters sequentially (not concurrently for
        simplicity), skips TTL cleanup, severity enrichment, and briefing
        aggregation.

        Returns:
            List of PipelineResult with episodes populated.
        """
        self._lazy_init_components()

        # Load ticker whitelist for EastMoney & AkShare (same as normal cycle)
        tickers = get_ticker_whitelist(self._whitelist_path)
        if tickers:
            self._update_adapter_tickers(tickers)
            logger.info("Dry-run: loaded %d tickers", len(tickers))

        results: list[PipelineResult] = []
        adapters_to_run: list[tuple[Any, str]] = []

        if self._gdelt_adapter is not None:
            adapters_to_run.append((self._gdelt_adapter, "gdelt_csv"))
        if self._rss_adapter is not None:
            adapters_to_run.append((self._rss_adapter, "rss"))

        # Stock pipeline: CLS (primary) → EastMoney (fallback) → AkShare (fallback)
        # V6.1: CLS telegraph replaces EastMoney as primary stock news source
        if self._cls_adapter is not None:
            adapters_to_run.append((self._cls_adapter, "cls"))
        elif self._eastmoney_adapter is not None:
            # CLS not configured, use EastMoney
            adapters_to_run.append((self._eastmoney_adapter, "eastmoney"))
        elif self._akshare_adapter is not None:
            # Neither CLS nor EastMoney configured, use AkShare
            adapters_to_run.append((self._akshare_adapter, "akshare"))

        if not adapters_to_run:
            logger.warning("Dry-run: no adapters to run (all skipped by source_filter)")
            return results

        logger.info(
            "=== Dry-run cycle starting: %d adapter(s) ===",
            len(adapters_to_run),
        )

        # Use index-based loop to support dynamic fallback (appending AkShare if EastMoney fails)
        idx = 0
        while idx < len(adapters_to_run):
            adapter, source_name = adapters_to_run[idx]
            idx += 1
            source_type = getattr(adapter, "SOURCE_TYPE", source_name)
            logger.info("Dry-run: running %s...", source_type)
            try:
                result = await run_pipeline(
                    adapter=adapter,
                    writer=None,
                    tickers=tickers if source_name in ("akshare", "eastmoney", "cls") else None,
                    dry_run=True,
                )
                results.append(result)
                if result.success:
                    logger.info(
                        "Dry-run [%s]: %d episodes in %.1fs",
                        source_type,
                        result.episode_count,
                        result.elapsed_seconds,
                    )
                    # Stock pipeline: if CLS/EastMoney returned episodes, skip fallback
                    if source_name == "cls" and result.episode_count > 0:
                        logger.info("CLS returned %d episodes, skipping EastMoney/AkShare fallback", result.episode_count)
                        break
                    if source_name == "eastmoney" and result.episode_count > 0:
                        logger.info("EastMoney returned %d episodes, skipping AkShare fallback", result.episode_count)
                        break
                else:
                    logger.error(
                        "Dry-run [%s]: FAILED after %.1fs: %s",
                        source_type,
                        result.elapsed_seconds,
                        result.error,
                    )
                    # CLS failed, try EastMoney fallback
                    if source_name == "cls" and self._eastmoney_adapter is not None:
                        logger.info("CLS failed, falling back to EastMoney")
                        adapters_to_run.append((self._eastmoney_adapter, "eastmoney"))
                    # EastMoney failed, try AkShare fallback
                    elif source_name == "eastmoney" and self._akshare_adapter is not None:
                        logger.info("EastMoney failed, falling back to AkShare")
                        adapters_to_run.append((self._akshare_adapter, "akshare"))
            except Exception as exc:
                logger.error(
                    "Dry-run [%s] threw unhandled exception: %s",
                    source_type,
                    exc,
                    exc_info=True,
                )
                results.append(
                    PipelineResult(
                        source_type=source_type,
                        success=False,
                        error=exc,
                    )
                )
                # CLS threw exception, try EastMoney fallback
                if source_name == "cls" and self._eastmoney_adapter is not None:
                    logger.info("CLS threw exception, falling back to EastMoney")
                    adapters_to_run.append((self._eastmoney_adapter, "eastmoney"))
                # EastMoney threw exception, try AkShare fallback
                elif source_name == "eastmoney" and self._akshare_adapter is not None:
                    logger.info("EastMoney threw exception, falling back to AkShare")
                    adapters_to_run.append((self._akshare_adapter, "akshare"))

        logger.info(
            "=== Dry-run cycle complete: %d/%d adapters successful ===",
            sum(1 for r in results if r.success),
            len(results),
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
