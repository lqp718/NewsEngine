"""IngestionScheduler — multi-source ingestion scheduler.

Orchestrates all data source adapters inside 4 independent tier loops
(multi-tier-cycle): Tier 1 real-time sources run every 15 minutes, low-
frequency tiers (daily/weekly/monthly) run on their own longer intervals.
Tier 1 is followed by SectorBriefingAggregator.aggregate_all().

V2.2: Macro/stock pipeline split:
- Macro pipeline (GDELT): uses 19 core theme OR matching, not ticker whitelist
- Macro pipeline (RSS): zero pre-ingestion filtering
- Stock pipeline (AkShare): ticker whitelist filtering only
- TTL cleanup: runs daily via DETACH DELETE (Layer 2)

Lifecycle:
    scheduler = IngestionScheduler(...)
    await scheduler.start()   # starts all tier loops
    await scheduler.stop()    # graceful shutdown (cancels all tier loops)
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
from src.utils.entity_canonical import canonical_name
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


# ── Multi-tier scheduling (multi-tier-cycle) ──────────────────────────
# Static adapter → tier mapping. Every adapter belongs to exactly one
# tier; each tier runs on its own configurable cycle interval (see
# config.py). Tier 1 keeps the legacy `ingestion_interval_sec` semantics.
TIER_MAP: dict[int, tuple[str, ...]] = {
    1: ("gdelt", "rss", "cls", "eastmoney", "akshare"),
    2: ("cninfo", "treasury", "fred"),
    3: ("eia", "acled", "sanctions"),
    4: ("bls", "china_macro", "eastmoney_research"),
}

# Unit name -> (scheduler adapter attr, writer attr).
# The CLS→EastMoney→AkShare fallback chain is handled as a composite
# unit ("stock") and therefore not listed here.
_ADAPTER_ATTRS: dict[str, tuple[str, str]] = {
    "gdelt": ("_gdelt_adapter", "_macro_writer"),
    "rss": ("_rss_adapter", "_macro_writer"),
    "fred": ("_fred_adapter", "_macro_writer"),
    "sanctions": ("_sanctions_adapter", "_macro_writer"),
    "acled": ("_acled_adapter", "_macro_writer"),
    "eia": ("_eia_adapter", "_macro_writer"),
    "bls": ("_bls_adapter", "_macro_writer"),
    "treasury": ("_treasury_adapter", "_macro_writer"),
    "china_macro": ("_china_macro_adapter", "_macro_writer"),
    "cninfo": ("_cninfo_adapter", "_symbol_writer"),
    "eastmoney_research": ("_eastmoney_research_adapter", "_symbol_writer"),
}

# Units that need the ticker whitelist before running (stock family).
_TICKER_AWARE_SOURCES: frozenset[str] = frozenset(
    {"cls", "eastmoney", "akshare", "cninfo", "eastmoney_research"}
)


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

    Runs every adapter inside 4 independent tier loops (one asyncio task
    per tier), each on its own configurable interval:
      - Tier 1 (real-time/high-frequency, default 15 min): GDELT, RSS,
        CLS→EastMoney→AkShare fallback chain
      - Tier 2 (daily, default 4 h): CNInfo, Treasury, FRED
      - Tier 3 (weekly, default 12 h): EIA, ACLED, Sanctions
      - Tier 4 (monthly/quarterly, default 24 h): BLS, China Macro,
        EastMoney Research

    Tier 1 also hosts TTL cleanup (daily guard), severity enrichment
    and briefing aggregation after each cycle. Long-cycle tiers never
    block short-cycle tiers: each tier gets its own task.

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
        landing_enabled: bool | None = None,
        capture_only: bool = False,
        ingest_only: bool = False,
        ingest_watch: bool = False,
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
            source_filter: Filter adapters ("gdelt", "rss", "akshare", "fred", "sanctions", "acled", "eia", "bls", None for all).
            fetch_content: When True, enable ContentFetcher for RSS (dry-run mode).
            landing_enabled: JSON 持久化层开关（设计 json-persistence-layer.md）。
                None 时取 settings.landing_enabled；capture_only/ingest_only 模式强制 True。
            capture_only: --fetch-only 模式 — 只跑 Stage A（capture），不创建
                graphiti/EpisodeWriter/IngestWorker（dry_run=False）。
            ingest_only: --ingest-only 模式 — 只跑 Stage B（IngestWorker），
                不创建 capture 适配器、不启动 Tier 循环。
            ingest_watch: --ingest-only --watch — 只入库 + 常驻监听；False 时
                drain 所有 pending 后退出（配合 drain_ingest()）。
        """
        self._dry_run = dry_run
        self._source_filter = source_filter
        self._fetch_content = fetch_content
        self._capture_only = capture_only
        self._ingest_only = ingest_only
        self._ingest_watch = ingest_watch

        settings = get_settings()

        # ── JSON 持久化层（landing zone）开关 ─────────────────────
        if landing_enabled is not None:
            self._landing_enabled = landing_enabled
        elif capture_only or ingest_only:
            self._landing_enabled = True
        else:
            self._landing_enabled = bool(getattr(settings, "landing_enabled", False))
        self._landing_store: Any = None
        self._ingest_worker: Any = None
        self._ingest_task: asyncio.Task[None] | None = None
        self._last_retention_date: str | None = None

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
        self._cninfo_adapter: Any = None  # V6.2: CNInfo announcements (Phase 2)
        self._eastmoney_research_adapter: Any = None  # V6.3: EastMoney research reports (Phase 3)

        # ── Phase 1 macro adapters (add-phase1-macro-adapters) ────
        self._fred_adapter: Any = None
        self._sanctions_adapter: Any = None
        self._acled_adapter: Any = None
        self._eia_adapter: Any = None
        self._bls_adapter: Any = None
        self._treasury_adapter: Any = None
        self._china_macro_adapter: Any = None

        # ── Briefing Aggregator ───────────────────────────────────
        self._aggregator = SectorBriefingAggregator() if not dry_run else None

        # ── TTL cleanup state (V2.2) ──────────────────────────────
        self._last_ttl_cleanup_date: str | None = None

        # ── Lazy-init guard ───────────────────────────────────────
        self._components_initialized: bool = False

        # ── Lifecycle state (multi-tier: one task per tier) ────────
        self._running = False
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._stop_event: asyncio.Event | None = None
        self._tier_groups: dict[int, list[tuple[str, Any, Any]]] = {}

        logger.info(
            "IngestionScheduler initialized (interval=%ds, whitelist=%s, dry_run=%s, source_filter=%s)",
            self._interval_sec,
            self._whitelist_path,
            dry_run,
            source_filter or "all",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the tiered ingestion cycle loops.

        One independent asyncio task is created per non-empty tier;
        each tier's first cycle runs immediately (no initial wait).

        landing 开启时额外启动: 启动恢复（lease 复位 + 孤儿文件扫描，在
        _lazy_init_components 内完成）+ 常驻 IngestWorker 任务（Stage B）。
        ingest_only 模式不启动 Tier 循环（capture 禁用）。
        """
        if self._running:
            logger.warning("IngestionScheduler already running")
            return

        self._running = True
        self._stop_event = asyncio.Event()
        self._lazy_init_components()
        if self._ingest_worker is not None:
            self._ingest_worker.stop_event = self._stop_event

        if self._ingest_only:
            # ── Stage B only: 不建 Tier 循环 ────────────────────
            if self._ingest_watch and self._ingest_worker is not None:
                self._ingest_task = asyncio.create_task(self._ingest_worker.run())
                logger.info("IngestWorker resident task started (--ingest-only --watch)")
            else:
                logger.info("Ingest-only mode: drain-once (call drain_ingest())")
            logger.info("IngestionScheduler started: ingest-only mode (capture disabled)")
            return

        self._tier_groups = self._build_tier_groups()
        self._tasks = {
            tier: asyncio.create_task(self._tier_loop(tier))
            for tier in sorted(self._tier_groups)
        }

        # ── Stage B 常驻 ingest 任务（landing 开启时，设计 §2.1）──
        if (
            not self._dry_run
            and not self._capture_only
            and self._ingest_worker is not None
        ):
            self._ingest_task = asyncio.create_task(self._ingest_worker.run())
            logger.info("IngestWorker resident task started (Stage B)")

        topology = ", ".join(
            "tier%d=[%s]@%ds"
            % (
                tier,
                ",".join(name for name, _, _ in units),
                self._tier_interval(tier),
            )
            for tier, units in self._tier_groups.items()
        )
        logger.info("IngestionScheduler started: %s", topology)

    async def stop(self) -> None:
        """Graceful shutdown.

        Cancels all tier loop tasks and waits for them to finish — no
        leftover scheduled coroutines. 常驻 IngestWorker 任务先给优雅退出
        机会（stop_event 置位，完成当前批），超时后强制取消 — 未完成的
        行保持 processing，靠 lease 超时在下次启动恢复（设计 §4.4）。
        """
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()

        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # ── IngestWorker 常驻任务 ────────────────────────────────
        ingest = self._ingest_task
        self._ingest_task = None
        if ingest is not None and not ingest.done():
            try:
                await asyncio.wait_for(asyncio.shield(ingest), timeout=5)
            except asyncio.TimeoutError:
                ingest.cancel()
                try:
                    await ingest
                except asyncio.CancelledError:
                    pass

        logger.info("IngestionScheduler stopped")

    async def drain_ingest(self) -> None:
        """--ingest-only（非 watch）: drain 所有 pending 后返回。

        每轮循环体与常驻 run() 相同（含 lease 恢复 + failed 到期重试），
        队列清空（无 pending、无到期可重试 failed）后退出。
        """
        if self._ingest_worker is None:
            logger.warning("drain_ingest: IngestWorker not initialized")
            return
        await self._ingest_worker.drain()

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
            Set of source names ("gdelt", "rss", "stock", "fred",
            "sanctions", "acled", "eia", "bls", "treasury", "china_macro").
        """
        source_filter = self._source_filter
        if source_filter is None or source_filter == "all":
            return {
                "gdelt",
                "rss",
                "stock",
                # ── Phase 1 macro adapters (add-phase1-macro-adapters) ──
                "fred",
                "sanctions",
                "acled",
                "eia",
                "bls",
                "treasury",
                "china_macro",
            }
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
        if self._ingest_only:
            # ingest-only: 不创建任何 capture 适配器，只建 writer + landing
            sources: set[str] = set()
        else:
            sources = self._resolve_sources()

        # ── In ingest-only mode, create Graphiti + EpisodeWriter ──
        # (fetch-only 模式不建 writer — capture 不需要 graphiti)
        if not self._dry_run and not self._capture_only:
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

            # 个股管线的 ticker 白名单：用于写入后确定性 ticker 接地
            # （不信任 LLM 填写的 ticker，白名单内的 SET，白名单外的 REMOVE）
            symbol_tickers = get_ticker_whitelist(self._whitelist_path)

            self._symbol_writer = EpisodeWriter(
                graphiti=self._graphiti,
                neo4j_driver=self._neo4j_driver,
                entity_types=SYMBOL_ENTITY_TYPES,
                whitelist=symbol_tickers,
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

        # ── JSON 持久化层（landing zone）: LandingStore + IngestWorker ──
        if self._landing_enabled and not self._dry_run and not self._capture_only:
            from src.persistence.ingest_worker import IngestWorker
            from src.persistence.landing_store import LandingStore

            lsettings = get_settings()
            self._landing_store = LandingStore(
                landing_dir=getattr(lsettings, "landing_dir", "data/landing"),
            )
            # ── 启动恢复（设计 §8.2/§8.3）: lease 复位 + 孤儿文件补登记 + .tmp 清理 ──
            try:
                recovered = self._landing_store.recover_leases()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("landing recover_leases failed: %s", exc)
                recovered = 0
            try:
                orphans = self._landing_store.scan_orphan_files()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("landing scan_orphan_files failed: %s", exc)
                orphans = 0
            try:
                tmp_count = self._landing_store.cleanup_tmp_files()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("landing cleanup_tmp_files failed: %s", exc)
                tmp_count = 0
            if recovered or orphans or tmp_count:
                logger.warning(
                    "landing recovery: leases_reset=%d orphan_rows=%d tmp_files=%d",
                    recovered,
                    orphans,
                    tmp_count,
                )

            self._ingest_worker = IngestWorker(
                store=self._landing_store,
                writer_resolver=self._resolve_writer,
                batch_size=getattr(lsettings, "ingest_batch_size", 20),
                poll_interval_sec=getattr(lsettings, "ingest_poll_interval_sec", 30),
                lease_sec=getattr(lsettings, "ingest_lease_sec", 900),
                max_attempts=getattr(lsettings, "ingest_max_attempts", 3),
                pending_high_water=getattr(lsettings, "ingest_pending_high_water", 3000),
            )
            logger.info(
                "Landing zone enabled: dir=%s store=%s worker=%s",
                getattr(lsettings, "landing_dir", "data/landing"),
                type(self._landing_store).__name__,
                type(self._ingest_worker).__name__,
            )
        else:
            logger.debug(
                "Landing zone disabled (landing_enabled=%s, dry_run=%s, capture_only=%s)",
                self._landing_enabled,
                self._dry_run,
                self._capture_only,
            )

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

        # ── Macro pipeline: Phase 1 macro data adapters (add-phase1-macro-adapters) ──
        # FRED / ACLED / EIA are key-gated (degrade gracefully when unconfigured);
        # OFAC/OpenSanctions (sanctions) and BLS need no key.
        if "fred" in sources:
            from src.adapters.fred_adapter import FredAdapter

            self._fred_adapter = FredAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: FRED adapter skipped (source_filter=%s)", self._source_filter)

        if "sanctions" in sources:
            from src.adapters.sanctions_adapter import SanctionsAdapter

            self._sanctions_adapter = SanctionsAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: Sanctions adapter skipped (source_filter=%s)", self._source_filter)

        if "acled" in sources:
            from src.adapters.acled_adapter import AcledAdapter

            self._acled_adapter = AcledAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: ACLED adapter skipped (source_filter=%s)", self._source_filter)

        if "eia" in sources:
            from src.adapters.eia_adapter import EiaAdapter

            self._eia_adapter = EiaAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: EIA adapter skipped (source_filter=%s)", self._source_filter)

        if "bls" in sources:
            from src.adapters.bls_adapter import BlsAdapter

            self._bls_adapter = BlsAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: BLS adapter skipped (source_filter=%s)", self._source_filter)

        # ── Treasury yield curve (US Treasury, no API key needed) ──
        if "treasury" in sources:
            from src.adapters.treasury_adapter import TreasuryAdapter

            self._treasury_adapter = TreasuryAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: Treasury adapter skipped (source_filter=%s)", self._source_filter)

        # ── China macro data (AKShare, no API key needed) ──
        if "china_macro" in sources:
            from src.adapters.china_macro_adapter import ChinaMacroAdapter

            self._china_macro_adapter = ChinaMacroAdapter(dedup_cache=self._dedup_cache)
        else:
            logger.info("Dry-run: China macro adapter skipped (source_filter=%s)", self._source_filter)

        # ── Stock pipeline: CLS (primary) → EastMoney (fallback) → AkShare (fallback) ──
        # V6.1: CLS telegraph replaces EastMoney as primary stock news source
        # V6.2: CNInfo announcements (Phase 2)
        # V6.3: EastMoney research reports (Phase 3)
        if "stock" in sources:
            from src.adapters.cls_adapter import CLSAdapter
            from src.adapters.eastmoney_adapter import EastMoneyAdapter
            from src.adapters.cninfo_adapter import CNInfoAdapter
            from src.adapters.eastmoney_research_adapter import EastMoneyResearchAdapter

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
            
            # V6.2: CNInfo announcements (Phase 2)
            self._cninfo_adapter = CNInfoAdapter(
                max_announcements=getattr(settings, 'cninfo_max_announcements', 5),
                max_pdf_pages=getattr(settings, 'cninfo_max_pdf_pages', 10),
                dedup_cache=self._dedup_cache,
            )
            
            # V6.3: EastMoney research reports (Phase 3)
            self._eastmoney_research_adapter = EastMoneyResearchAdapter(
                max_reports=getattr(settings, 'eastmoney_research_max_reports', 3),
                max_pdf_pages=getattr(settings, 'eastmoney_research_max_pdf_pages', 15),
                dedup_cache=self._dedup_cache,
            )
        else:
            logger.info("Dry-run: CLS/EastMoney/AkShare/CNInfo/EastMoneyResearch adapters skipped (source_filter=%s)", self._source_filter)

        logger.info(
            "Scheduler components initialized: "
            "GDELT=%s, RSS=%s feeds, CLS=%s, EastMoney=%s, AkShare=%s, CNInfo=%s, EastMoneyResearch=%s"
            "%s",
            "yes" if self._gdelt_adapter else "no",
            len(self._rss_adapter.feed_urls) if self._rss_adapter else "no",
            "yes" if self._cls_adapter else "no",
            "yes" if self._eastmoney_adapter else "no",
            "yes" if self._akshare_adapter else "no",
            "yes" if self._cninfo_adapter else "no",
            "yes" if self._eastmoney_research_adapter else "no",
            " [dry-run mode]" if self._dry_run else ""
        )
        logger.info(
            "Phase 1 macro adapters: FRED=%s, Sanctions=%s, ACLED=%s, EIA=%s, BLS=%s, Treasury=%s, ChinaMacro=%s",
            "yes" if self._fred_adapter else "no",
            "yes" if self._sanctions_adapter else "no",
            "yes" if self._acled_adapter else "no",
            "yes" if self._eia_adapter else "no",
            "yes" if self._bls_adapter else "no",
            "yes" if self._treasury_adapter else "no",
            "yes" if self._china_macro_adapter else "no",
        )

    # ── Internal: tiered cycle loops (multi-tier-cycle) ──────────────────

    async def _tier_loop(self, tier: int) -> None:
        """Independent cycle loop for one tier.

        Runs the tier's adapters every ``_tier_interval(tier)`` seconds.
        Each tier runs in its own asyncio task, so a long-cycle tier
        never blocks a short-cycle tier. Unexpected errors inside the
        loop body are logged as critical and the loop continues to the
        next cycle (crash self-healing).
        """
        interval = self._tier_interval(tier)
        adapter_names = [name for name, _, _ in self._tier_groups.get(tier, [])]
        logger.info(
            "Tier %d loop started (interval=%ds, adapters=%s)",
            tier,
            interval,
            ",".join(adapter_names),
        )

        while self._running:
            cycle_start = time.monotonic()
            # Cycle boundary logs use WARNING: with LOG_LEVEL=WARNING in .env,
            # INFO logs are filtered out — cycle start/end/summary must stay visible.
            logger.warning(
                "=== Tier %d cycle starting (interval=%ds, adapters=%s) ===",
                tier,
                interval,
                ",".join(adapter_names),
            )

            try:
                if tier == 1:
                    # V2.2: TTL cleanup at start of each cycle (daily guard inside)
                    await self._ttl_cleanup()
                elif tier == 4:
                    # json-persistence-layer §5.1: landing 保留期清理（每日一次，内部 date guard）
                    await self._retention_sweep()

                results = await self._run_tier_cycle(tier)
                await self._log_cycle_summary(tier, results, cycle_start)
            except asyncio.CancelledError:
                logger.info("Tier %d cycle cancelled", tier)
                raise
            except Exception as exc:
                logger.critical(
                    "Tier %d cycle crashed: %s",
                    tier,
                    exc,
                    exc_info=True,
                )

            # Sleep until next cycle (wakes early on stop())
            elapsed = time.monotonic() - cycle_start
            remaining = max(float(self._min_cycle_gap_sec), interval - elapsed)
            logger.warning(
                "=== Tier %d cycle complete (%.1fs, guard=%.1fs, next in %.1fs) ===",
                tier,
                elapsed,
                self._min_cycle_gap_sec,
                remaining,
            )
            await self._sleep_cancel_aware(remaining)

    async def _sleep_cancel_aware(self, seconds: float) -> None:
        """Sleep for ``seconds``, waking up early when ``stop()`` is called."""
        event = self._stop_event
        if event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _tier_interval(self, tier: int) -> int:
        """Return the configured cycle interval (seconds) for a tier."""
        settings = get_settings()
        if tier == 1:
            return self._interval_sec
        if tier == 2:
            return settings.ingestion_tier2_interval_sec
        if tier == 3:
            return settings.ingestion_tier3_interval_sec
        if tier == 4:
            return settings.ingestion_tier4_interval_sec
        raise ValueError(f"Unknown tier: {tier}")

    def _adapters_for_tier(
        self, tier: int
    ) -> list[tuple[str, Any, Any]]:
        """Resolve initialized adapter instances belonging to a tier.

        Returns a list of ``(unit_name, adapter, writer)`` tuples; units
        whose adapter instance is None (filtered out / not initialized)
        are skipped. The CLS→EastMoney→AkShare fallback chain is exposed
        as a single composite unit named ``"stock"`` when at least one of
        the three stock adapters is initialized.
        """
        units: list[tuple[str, Any, Any]] = []
        for name in TIER_MAP.get(tier, ()):
            if name in ("cls", "eastmoney", "akshare"):
                if name == "cls" and any(
                    getattr(self, attr) is not None
                    for attr in ("_cls_adapter", "_eastmoney_adapter", "_akshare_adapter")
                ):
                    units.append(("stock", None, self._symbol_writer))
                continue

            attr, writer_attr = _ADAPTER_ATTRS[name]
            adapter = getattr(self, attr)
            if adapter is not None:
                units.append((name, adapter, getattr(self, writer_attr)))
        return units

    def _build_tier_groups(self) -> dict[int, list[tuple[str, Any, Any]]]:
        """Group initialized adapters by tier.

        Returns:
            Mapping of tier → list of ``(unit_name, adapter, writer)``
            tuples, containing only tiers with at least one initialized
            adapter (empty tiers get no loop).
        """
        groups: dict[int, list[tuple[str, Any, Any]]] = {}
        for tier in sorted(TIER_MAP):
            units = self._adapters_for_tier(tier)
            if units:
                groups[tier] = units
        return groups

    async def _run_tier_cycle(self, tier: int) -> list[PipelineResult]:
        """Execute one cycle for a single tier's adapters.

        Tier-internal adapters run concurrently with error isolation
        (one failing adapter does not affect its tier-mates, and the
        tier cycle itself never raises). Ticker whitelist IO happens
        only for tiers containing stock-family adapters (Tier 1 stock
        chain, CNInfo, EastMoney Research).

        Tier 1 additionally runs severity enrichment and briefing
        aggregation at the tail (equivalent of the legacy per-cycle
        aggregation point).
        """
        units = self._tier_groups.get(tier, [])
        if not units:
            logger.warning("Tier %d: no adapters to run", tier)
            return []

        # ── Step 1: ticker whitelist — only for stock-family tiers ──
        # Tier 1 exposes the CLS→EastMoney→AkShare chain as the composite
        # unit "stock", which is ticker-aware too (design §1.4).
        tickers: list[dict[str, str]] = []
        sector_names: list[str] = []
        if any(
            name == "stock" or name in _TICKER_AWARE_SOURCES
            for name, _, _ in units
        ):
            tickers = get_ticker_whitelist(self._whitelist_path)
            sector_names = _extract_sector_names(tickers)
            self._update_adapter_tickers(tickers)
            # 同步白名单到 symbol writer（写入后 ticker 接地）
            if self._symbol_writer is not None:
                self._symbol_writer.set_whitelist(tickers)
            logger.info(
                "Tier %d: %d tickers, %d sectors",
                tier,
                len(tickers),
                len(sector_names),
            )

        # ── Step 2: run tier's units concurrently (error-isolated) ──
        named_coros: list[tuple[str, Any]] = []
        for name, adapter, writer in units:
            if name == "stock":
                named_coros.append(("stock", self._stock_pipeline(tickers)))
            else:
                named_coros.append(
                    (name, self._run_adapter_pipeline(adapter, writer, tickers))
                )

        completed = await asyncio.gather(
            *[coro for _, coro in named_coros],
            return_exceptions=True,
        )

        pipeline_results: list[PipelineResult] = []
        for (name, _), result in zip(named_coros, completed):
            if result is None:
                continue
            if isinstance(result, Exception):
                logger.error(
                    "Adapter pipeline [%s] threw unhandled exception: %s",
                    name,
                    result,
                    exc_info=True,
                )
                pipeline_results.append(
                    PipelineResult(
                        source_type=name,
                        success=False,
                        error=result,
                    )
                )
            else:
                pipeline_results.append(result)

        # ── Step 3 (Tier 1 only): severity enrichment + briefing ──
        if tier == 1:
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

        # ── Step 4 (Tier 1 only): 存量节点 ticker 扫除（cycle 尾部）──────
        # 保证最终状态不变量：ticker ⟺ 白名单名称。写入时接地负责新写入，
        # cycle 尾部扫除负责清理 LLM 重新引入的错填（如合并节点的英文名变体）。
        if tier == 1:
            try:
                self._sweep_ticker_grounding(tickers)
            except Exception as exc:
                logger.warning(
                    "Ticker grounding sweep failed (non-critical): %s",
                    exc,
                    exc_info=True,
                )

        return pipeline_results

    async def _stock_pipeline(self, tickers: list[dict[str, str]]) -> PipelineResult:
        """Stock pipeline: CLS (primary) → EastMoney (fallback) → AkShare (fallback).

        V6.1: CLS telegraph replaces EastMoney as primary stock news source.
        Only initialized adapters participate; each step guards None.
        """
        if self._cls_adapter is not None:
            cls_result = await self._run_adapter_pipeline(
                self._cls_adapter, self._symbol_writer, tickers
            )
            if cls_result.success and cls_result.episode_count > 0:
                logger.info(
                    "Stock pipeline: CLS returned %d episodes",
                    cls_result.episode_count,
                )
                return cls_result
            logger.info("CLS returned 0 episodes, falling back to EastMoney")

        if self._eastmoney_adapter is not None:
            em_result = await self._run_adapter_pipeline(
                self._eastmoney_adapter, self._symbol_writer, tickers
            )
            if em_result.success and em_result.episode_count > 0:
                logger.info(
                    "Stock pipeline: EastMoney returned %d episodes",
                    em_result.episode_count,
                )
                return em_result
            logger.info("EastMoney returned 0 episodes, falling back to AkShare")

        if self._akshare_adapter is not None:
            return await self._run_adapter_pipeline(
                self._akshare_adapter, self._symbol_writer, tickers
            )

        return PipelineResult(
            source_type="stock",
            success=False,
            error=RuntimeError("no stock adapter initialized"),
        )

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
        V6.2: CNInfo announcements (Phase 2)
        V6.3: EastMoney research reports (Phase 3)
        - CLSAdapter: name-based entity extraction (uses stock Chinese name)
        - EastMoneyAdapter: name-based search (uses stock Chinese name)
        - AkShareAdapter: symbol-based search (uses biz_code)
        - CNInfoAdapter: symbol-based search (uses symbol)
        - EastMoneyResearchAdapter: symbol-based search (uses symbol)

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
        
        # V6.2: CNInfoAdapter: index by symbol (support both 'ticker' and 'symbol' fields)
        if self._cninfo_adapter is not None:
            self._cninfo_adapter.ticker_whitelist = tickers
            self._cninfo_adapter._symbol_map = {}
            self._cninfo_adapter._name_map = {}
            for entry in tickers:
                symbol = entry.get("ticker", "") or entry.get("symbol", "")
                name = entry.get("name", "")
                if symbol:
                    self._cninfo_adapter._symbol_map[symbol] = entry
                if name:
                    self._cninfo_adapter._name_map[name] = entry
        
        # V6.3: EastMoneyResearchAdapter: index by symbol (support both 'ticker' and 'symbol' fields)
        if self._eastmoney_research_adapter is not None:
            self._eastmoney_research_adapter.ticker_whitelist = tickers
            self._eastmoney_research_adapter._symbol_map = {}
            self._eastmoney_research_adapter._name_map = {}
            for entry in tickers:
                symbol = entry.get("ticker", "") or entry.get("symbol", "")
                name = entry.get("name", "")
                if symbol:
                    self._eastmoney_research_adapter._symbol_map[symbol] = entry
                if name:
                    self._eastmoney_research_adapter._name_map[name] = entry

    def _sweep_ticker_grounding(self, tickers: list[dict[str, str]]) -> None:
        """存量节点确定性 ticker 接地扫除（幂等，每个 cycle 尾部执行）。

        与写入时接地同规则：仅白名单名称（或其 canonical/biz_code/别名）
        对应的 Entity 节点允许持有 ticker，否则 REMOVE。

        解决历史遗留数据问题：指数/ETF 节点被错误填上白名单股票的
        ticker（张冠李戴），以及合并/分类导致的 ticker 丢失/错填。

        每 cycle 都执行以保证最终状态不变量（ticker ⟺ 白名单名称），
        即使 LLM 在写入时重新引入了错填值，下个 cycle 尾部也会被清理。
        白名单为空时跳过（避免误删全部 ticker）。
        """
        if not tickers or self._neo4j_driver is None:
            return

        # 构建 name -> ticker 映射（含 canonical/biz_code/别名），与写入时接地一致
        name_to_ticker: dict[str, str] = {}
        for entry in tickers:
            name = entry.get("name", "").strip()
            ticker = entry.get("ticker", "").strip()
            biz_code = entry.get("biz_code", "").strip()
            if name and ticker:
                name_to_ticker[name] = ticker
                canonical = canonical_name(name, "stock")
                if canonical != name:
                    name_to_ticker[canonical] = ticker
            if biz_code and ticker:
                name_to_ticker[biz_code] = ticker

        if not name_to_ticker:
            return

        try:
            with self._neo4j_driver.session() as session:
                wl_names = list(name_to_ticker.keys())

                # SET pass: 白名单名称节点 → 确保持有正确 ticker
                for name, ticker in name_to_ticker.items():
                    session.run(
                        "MATCH (n:Entity) WHERE n.name = $name "
                        "AND (n.ticker IS NULL OR n.ticker <> $ticker) "
                        "SET n.ticker = $ticker",
                        name=name,
                        ticker=ticker,
                    )

                # REMOVE pass: 非白名单名称节点 → 清理遗留错填 ticker
                removed = session.run(
                    "MATCH (n:Entity) WHERE n.ticker IS NOT NULL "
                    "AND NOT n.name IN $wl_names REMOVE n.ticker "
                    "RETURN count(n) AS cleaned",
                    wl_names=wl_names,
                ).single()
                cleaned = removed["cleaned"] if removed else 0
                if cleaned > 0:
                    logger.info(
                        "Ticker sweep: removed ticker from %d non-whitelist entity node(s)",
                        cleaned,
                    )
        except Exception as exc:
            logger.warning("Ticker sweep failed (non-critical): %s", exc, exc_info=True)

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

    # ── Landing zone: writer resolver / retention (json-persistence-layer) ──

    # 个股管线（SYMBOL_ENTITY_TYPES）源类型集合；其余（宏/宏观管线）走 macro writer。
    _SYMBOL_WRITER_SOURCES: frozenset[str] = frozenset(
        {
            "akshare",
            "eastmoney",
            "cls_telegraph",
            "cninfo_announcement",
            "eastmoney_research",
        }
    )

    def _resolve_writer(self, source_type: str) -> Any:
        """按 source_type 返回 EpisodeWriter（宏/个股管线使用不同 entity_types）。

        IngestWorker 的 writer_resolver：landed_episodes.source_type 来自
        NormalizedEpisode.source_type（如 gdelt_csv / rss / cls_telegraph），
        个体股链路的 episode 走 symbol writer，其余走 macro writer。
        """
        if source_type in self._SYMBOL_WRITER_SOURCES:
            return self._symbol_writer
        return self._macro_writer

    async def _retention_sweep(self) -> int:
        """Landing 保留期清理（每日一次，挂靠 Tier 4 周期循环，设计 §5.1）。

        只删除过期 done/skipped/dead 行 + 对应 JSONL；pending/processing/failed
        永不自动清理（未入库数据不丢）。返回删除的行数。
        """
        if self._landing_store is None:
            return 0
        settings = get_settings()
        today = now_hkt().strftime("%Y-%m-%d")
        if self._last_retention_date == today:
            return 0
        self._last_retention_date = today
        retention_days = getattr(settings, "landing_retention_days", 14)
        try:
            deleted = self._landing_store.retention_sweep(retention_days=retention_days)
            if deleted:
                logger.info(
                    "Retention sweep: removed %d done/skipped/dead row(s) older than %d days",
                    deleted,
                    retention_days,
                )
            return deleted
        except Exception as exc:
            logger.warning("Retention sweep failed (non-critical): %s", exc, exc_info=True)
            return 0

    # ── Dry-run / fetch-only cycle (one-shot) ──────────────────────────

    def _collect_run_units(self) -> list[tuple[Any, str]]:
        """收集一次性 cycle（dry-run / fetch-only capture）要跑的 adapter 单元。

        返回 ``(adapter, source_name)`` 列表；source_name 用于 ticker 门控与
        CLS→EastMoney→AkShare 回退链决策（与 run_dry_cycle 原有逻辑一致）。
        """
        adapters_to_run: list[tuple[Any, str]] = []

        if self._gdelt_adapter is not None:
            adapters_to_run.append((self._gdelt_adapter, "gdelt_csv"))
        if self._rss_adapter is not None:
            adapters_to_run.append((self._rss_adapter, "rss"))

        # ── Phase 1 macro adapters (add-phase1-macro-adapters) ──
        if self._fred_adapter is not None:
            adapters_to_run.append((self._fred_adapter, "fred"))
        if self._sanctions_adapter is not None:
            adapters_to_run.append((self._sanctions_adapter, "sanctions"))
        if self._acled_adapter is not None:
            adapters_to_run.append((self._acled_adapter, "acled"))
        if self._eia_adapter is not None:
            adapters_to_run.append((self._eia_adapter, "eia"))
        if self._bls_adapter is not None:
            adapters_to_run.append((self._bls_adapter, "bls"))
        if self._treasury_adapter is not None:
            adapters_to_run.append((self._treasury_adapter, "treasury"))
        if self._china_macro_adapter is not None:
            adapters_to_run.append((self._china_macro_adapter, "china_macro"))

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

        # V6.2: CNInfo announcements (Phase 2) - supplementary source
        if self._cninfo_adapter is not None:
            adapters_to_run.append((self._cninfo_adapter, "cninfo"))

        # V6.3: EastMoney research reports (Phase 3) - supplementary source
        if self._eastmoney_research_adapter is not None:
            adapters_to_run.append((self._eastmoney_research_adapter, "eastmoney_research"))

        return adapters_to_run

    async def _run_units_sequential(
        self,
        units: list[tuple[Any, str]],
        tickers: list[dict[str, str]],
        *,
        mode: str,
    ) -> list[PipelineResult]:
        """串行跑一次性 cycle 的 adapter 列表（dry-run / fetch-only 共用）。

        包含原有 CLS→EastMoney→AkShare 动态回退逻辑：CLS 失败时追加 EastMoney，
        EastMoney 失败时追加 AkShare；CLS/EastMoney 成功时跳过回退链。

        Args:
            units: ``(adapter, source_name)`` 列表。
            tickers: ticker 白名单（个股链路传入）。
            mode: "dry_run" 或 "capture"（fetch-only，Stage A 落盘 landing）。
        """
        dry_run = mode == "dry_run"
        results: list[PipelineResult] = []

        if not units:
            logger.warning("%s: no adapters to run (all skipped by source_filter)", mode)
            return results

        logger.warning(
            "=== %s cycle starting: %d adapter(s) ===",
            mode,
            len(units),
        )

        # Use index-based loop to support dynamic fallback (appending AkShare if EastMoney fails)
        idx = 0
        while idx < len(units):
            adapter, source_name = units[idx]
            idx += 1
            source_type = getattr(adapter, "SOURCE_TYPE", source_name)
            logger.info("%s: running %s...", mode, source_type)
            try:
                result = await run_pipeline(
                    adapter=adapter,
                    writer=None,
                    tickers=(
                        tickers
                        if source_name in ("akshare", "eastmoney", "cls", "cninfo", "eastmoney_research")
                        else None
                    ),
                    dry_run=dry_run,
                    landing_store=None if dry_run else self._landing_store,
                )
                results.append(result)
                if result.success:
                    logger.info(
                        "%s [%s]: %d episodes in %.1fs",
                        mode,
                        source_type,
                        result.episode_count,
                        result.elapsed_seconds,
                    )
                    # Stock pipeline fallback: if CLS returned episodes, skip EastMoney/AkShare
                    # But CNInfo and EastMoney Research should still run (they are supplementary)
                    if source_name == "cls" and result.episode_count > 0:
                        logger.info("CLS returned %d episodes, skipping EastMoney/AkShare fallback", result.episode_count)
                        # Skip only the fallback chain, not supplementary sources
                        while idx < len(units) and units[idx][1] in ("eastmoney", "akshare"):
                            idx += 1
                        continue  # Continue to run CNInfo and EastMoney Research
                    if source_name == "eastmoney" and result.episode_count > 0:
                        logger.info("EastMoney returned %d episodes, skipping AkShare fallback", result.episode_count)
                        # Skip only AkShare fallback
                        while idx < len(units) and units[idx][1] == "akshare":
                            idx += 1
                        continue
                else:
                    logger.error(
                        "%s [%s]: FAILED after %.1fs: %s",
                        mode,
                        source_type,
                        result.elapsed_seconds,
                        result.error,
                    )
                    # CLS failed, try EastMoney fallback
                    if source_name == "cls" and self._eastmoney_adapter is not None:
                        logger.info("CLS failed, falling back to EastMoney")
                        units.append((self._eastmoney_adapter, "eastmoney"))
                    # EastMoney failed, try AkShare fallback
                    elif source_name == "eastmoney" and self._akshare_adapter is not None:
                        logger.info("EastMoney failed, falling back to AkShare")
                        units.append((self._akshare_adapter, "akshare"))
            except Exception as exc:
                logger.error(
                    "%s [%s] threw unhandled exception: %s",
                    mode,
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
                    units.append((self._eastmoney_adapter, "eastmoney"))
                # EastMoney threw exception, try AkShare fallback
                elif source_name == "eastmoney" and self._akshare_adapter is not None:
                    logger.info("EastMoney threw exception, falling back to AkShare")
                    units.append((self._akshare_adapter, "akshare"))

        logger.warning(
            "=== %s cycle complete: %d/%d adapters successful ===",
            mode,
            sum(1 for r in results if r.success),
            len(results),
        )

        return results

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

        return await self._run_units_sequential(
            self._collect_run_units(),
            tickers,
            mode="dry_run",
        )

    async def run_capture_cycle(self) -> list[PipelineResult]:
        """Execute one-shot Stage A capture (--fetch-only).

        与 run_dry_cycle 相同的 adapter 编排（含 CLS→EastMoney→AkShare 回退
        链），但 Stage 2 走 ``landing_store.capture_batch``（写 JSONL + 登记
        pending）。不初始化 graphiti/Neo4j、不启动 IngestWorker（scheduler
        以 capture_only 模式构造，_landing_store 已就绪）。

        Returns:
            List of PipelineResult（episode_count = 落盘 episode 数）。
        """
        self._lazy_init_components()

        if self._landing_store is None:
            logger.error("capture cycle: landing store not initialized (mode error)")
            return []

        # Load ticker whitelist for EastMoney & AkShare (same as normal cycle)
        tickers = get_ticker_whitelist(self._whitelist_path)
        if tickers:
            self._update_adapter_tickers(tickers)
            logger.info("Fetch-only: loaded %d tickers", len(tickers))

        return await self._run_units_sequential(
            self._collect_run_units(),
            tickers,
            mode="capture",
        )

    # ── Logging ─────────────────────────────────────────────────────────

    async def _log_cycle_summary(
        self,
        tier: int,
        results: list[PipelineResult],
        cycle_start: float,
    ) -> None:
        """Log a summary of one tier's cycle results."""
        if not results:
            logger.warning("Tier %d cycle: no adapter results", tier)
            return
        total_time = time.monotonic() - cycle_start
        total_episodes = sum(r.episode_count for r in results)
        source_stats = ", ".join(
            f"{r.source_type}={r.episode_count}ep{'!' if not r.success else ''}"
            for r in results
        )

        all_failed = all(not r.success for r in results)
        if all_failed:
            logger.critical(
                "Tier %d cycle: ALL sources failed [%.1fs] %s",
                tier,
                total_time,
                source_stats,
            )
        else:
            failed = [r.source_type for r in results if not r.success]
            if failed:
                logger.warning(
                    "Tier %d cycle: partial failures [%.1fs] total=%dep, failed=[%s] %s",
                    tier,
                    total_time,
                    total_episodes,
                    ", ".join(failed),
                    source_stats,
                )
            else:
                # Cycle summary at WARNING level so it is visible under LOG_LEVEL=WARNING
                logger.warning(
                    "Tier %d cycle: all sources OK [%.1fs] total=%dep %s",
                    tier,
                    total_time,
                    total_episodes,
                    source_stats,
                )


__all__ = [
    "IngestionScheduler",
    "get_ticker_whitelist",
]
