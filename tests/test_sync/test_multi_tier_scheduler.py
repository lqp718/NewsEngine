"""Unit tests for multi-tier cycle scheduling (multi-tier-cycle).

Covers: TIER_MAP completeness (3.1), config defaults/overrides/validation
(3.2), per-tier scheduling rhythm, empty-tier skipping (3.3), error
isolation and loop survival (3.4), graceful shutdown and the shared
dedup cache (3.5).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.core.config import Settings
from src.ingestion.pipeline import PipelineResult
from src.ingestion.scheduler import IngestionScheduler, TIER_MAP
from src.utils.time_utils import now_hkt

# ── Test data ──────────────────────────────────────────────────────────

ALL_ADAPTERS: set[str] = {
    "gdelt", "rss", "cls", "eastmoney", "akshare",
    "cninfo", "treasury", "fred",
    "eia", "acled", "sanctions",
    "bls", "china_macro", "eastmoney_research",
}

# Logical adapter name -> scheduler attribute holding the instance.
_ADAPTER_ATTRS: dict[str, str] = {
    "gdelt": "_gdelt_adapter",
    "rss": "_rss_adapter",
    "cls": "_cls_adapter",
    "eastmoney": "_eastmoney_adapter",
    "akshare": "_akshare_adapter",
    "cninfo": "_cninfo_adapter",
    "fred": "_fred_adapter",
    "sanctions": "_sanctions_adapter",
    "acled": "_acled_adapter",
    "eia": "_eia_adapter",
    "bls": "_bls_adapter",
    "treasury": "_treasury_adapter",
    "china_macro": "_china_macro_adapter",
    "eastmoney_research": "_eastmoney_research_adapter",
}


def _fake_settings(**overrides: object) -> SimpleNamespace:
    """Settings stub with every field the scheduler touches."""
    values: dict[str, object] = dict(
        ingestion_interval_sec=900,
        ingestion_tier2_interval_sec=14400,
        ingestion_tier3_interval_sec=43200,
        ingestion_tier4_interval_sec=86400,
        min_cycle_gap_sec=0,
        ticker_whitelist_file="data/ticker_whitelist.json",
        eastmoney_page_size=20,
        cls_page_size=50,
        cninfo_max_announcements=5,
        cninfo_max_pdf_pages=10,
        eastmoney_research_max_reports=3,
        eastmoney_research_max_pdf_pages=15,
        episode_ttl_symbol_days=3,
        episode_ttl_sector_days=7,
        episode_ttl_macro_days=14,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    interval_sec: float = 60,
    source_filter: str | None = None,
    **settings_overrides: object,
) -> IngestionScheduler:
    """Build a scheduler with a stubbed settings object and no real network.

    Component lazy init is skipped; adapters are installed manually by
    the caller with ``_install_adapters``.
    """
    monkeypatch.setattr(
        "src.ingestion.scheduler.get_settings",
        lambda: _fake_settings(**settings_overrides),
    )
    scheduler = IngestionScheduler(
        dry_run=True,
        source_filter=source_filter,
        interval_sec=interval_sec,
        min_cycle_gap_sec=None,
    )
    scheduler._components_initialized = True
    # Short-circuit the daily TTL guard so no Neo4j session is attempted.
    scheduler._last_ttl_cleanup_date = now_hkt().strftime("%Y-%m-%d")
    return scheduler


def _install_adapters(scheduler: IngestionScheduler, names: set[str]) -> None:
    """Install lightweight mock adapters on the scheduler instance."""
    for name in names:
        attr = _ADAPTER_ATTRS[name]
        setattr(
            scheduler,
            attr,
            SimpleNamespace(
                SOURCE_TYPE=name,
                dedup_cache=scheduler._dedup_cache,
                run=AsyncMock(return_value=[]),
            ),
        )


def _install_all_adapters(scheduler: IngestionScheduler) -> None:
    """Install every adapter except EastMoney/AkShare (covered by CLS)."""
    _install_adapters(scheduler, ALL_ADAPTERS - {"eastmoney", "akshare"})


# ── 3.1: TIER_MAP completeness ─────────────────────────────────────────


class TestTierMap:
    def test_tier_map_covers_exactly_all_14_adapters(self) -> None:
        assert set(TIER_MAP) == {1, 2, 3, 4}
        mapped: set[str] = set()
        for tier, names in TIER_MAP.items():
            assert isinstance(names, tuple)
            for name in names:
                assert name not in mapped, f"adapter {name!r} assigned twice"
                mapped.add(name)
        assert mapped == ALL_ADAPTERS

    def test_all_installed_adapters_resolve_to_exactly_one_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(monkeypatch, interval_sec=60)
        _install_all_adapters(scheduler)

        groups = scheduler._build_tier_groups()
        assert set(groups) == {1, 2, 3, 4}

        resolved: set[str] = set()
        for tier, units in groups.items():
            for name, _adapter, _writer in units:
                if name == "stock":
                    # Composite unit stands for the whole CLS→EM→AkShare chain.
                    assert tier == 1
                    resolved.update({"cls", "eastmoney", "akshare"})
                else:
                    assert name in TIER_MAP[tier]
                    resolved.add(name)

        assert resolved == ALL_ADAPTERS


# ── 3.2: config defaults / overrides / validation ──────────────────────


class TestTierConfig:
    def test_default_intervals(self) -> None:
        s = Settings(openai_api_key="test-key", deepseek_api_key="test-key")
        assert s.ingestion_interval_sec == 900
        assert s.ingestion_tier2_interval_sec == 14400
        assert s.ingestion_tier3_interval_sec == 43200
        assert s.ingestion_tier4_interval_sec == 86400

    def test_env_override_tier2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGESTION_TIER2_INTERVAL_SEC", "21600")
        s = Settings(openai_api_key="test-key", deepseek_api_key="test-key")
        assert s.ingestion_tier2_interval_sec == 21600
        # Other tiers unaffected.
        assert s.ingestion_tier3_interval_sec == 43200
        assert s.ingestion_tier4_interval_sec == 86400

    def test_backward_compat_interval_sec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INGESTION_INTERVAL_SEC", "600")
        s = Settings(openai_api_key="test-key", deepseek_api_key="test-key")
        assert s.ingestion_interval_sec == 600
        # Tier 2-4 fall back to their own defaults.
        assert s.ingestion_tier2_interval_sec == 14400
        assert s.ingestion_tier3_interval_sec == 43200
        assert s.ingestion_tier4_interval_sec == 86400

    @pytest.mark.parametrize(
        "bad",
        [
            {"ingestion_interval_sec": 0},
            {"ingestion_interval_sec": -1},
            {"ingestion_tier2_interval_sec": 0},
            {"ingestion_tier2_interval_sec": -5},
            {"ingestion_tier3_interval_sec": 0},
            {"ingestion_tier4_interval_sec": -100},
        ],
    )
    def test_non_positive_interval_rejected(self, bad: dict[str, int]) -> None:
        with pytest.raises(ValidationError):
            Settings(openai_api_key="test-key", deepseek_api_key="test-key", **bad)


# ── 3.3: scheduling rhythm / tier independence / empty tiers ───────────


class TestTierRhythm:
    async def test_tiers_run_on_own_intervals_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        async def fake_run_pipeline(
            adapter, writer, tickers=None, dry_run=False
        ) -> PipelineResult:
            calls.append(getattr(adapter, "SOURCE_TYPE", "mock"))
            return PipelineResult(
                source_type=getattr(adapter, "SOURCE_TYPE", "mock"),
                success=True,
                episode_count=1,
            )

        scheduler = _make_scheduler(
            monkeypatch,
            interval_sec=0.05,  # Tier 1
            ingestion_tier2_interval_sec=0.1,
            ingestion_tier3_interval_sec=0.15,
            ingestion_tier4_interval_sec=0.2,
        )
        _install_all_adapters(scheduler)
        monkeypatch.setattr("src.ingestion.scheduler.run_pipeline", fake_run_pipeline)

        await scheduler.start()
        assert set(scheduler._tasks) == {1, 2, 3, 4}

        await asyncio.sleep(0.5)
        await scheduler.stop()

        tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
        for source in calls:
            for tier, names in TIER_MAP.items():
                if source in names:
                    tier_counts[tier] += 1
                    break
            else:
                tier_counts[1] += 1  # composite "stock" unit

        # Every tier fired at least once…
        assert tier_counts[1] >= 2
        assert tier_counts[2] >= 1
        assert tier_counts[3] >= 1
        assert tier_counts[4] >= 1
        # …and the short-cycle tier fired substantially more often than
        # the long-cycle tier (long tier never blocked the short one).
        assert tier_counts[1] > tier_counts[4]

    async def test_empty_tiers_get_no_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(
            monkeypatch, interval_sec=60, source_filter="gdelt"
        )
        _install_adapters(scheduler, {"gdelt"})

        await scheduler.start()
        try:
            # Only Tier 1 (gdelt) is non-empty; tiers 2-4 have no loop.
            assert set(scheduler._tasks) == {1}
        finally:
            await scheduler.stop()

    async def test_stop_event_wakes_idle_loops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(monkeypatch, interval_sec=3600)
        _install_all_adapters(scheduler)

        await scheduler.start()
        tasks = list(scheduler._tasks.values())
        await scheduler.stop()

        assert scheduler._running is False
        assert scheduler._tasks == {}
        assert all(t.done() for t in tasks)


# ── 3.4: error isolation ───────────────────────────────────────────────


class TestTierWhitelistIo:
    """Ticker whitelist IO only happens for stock-family tiers.

    Tier 1 exposes the CLS→EastMoney→AkShare chain as the composite
    unit "stock" (design §1.4): the whitelist MUST still be loaded or
    the stock pipeline would ingest zero episodes.
    """

    async def test_tier1_composite_stock_loads_whitelist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict[str, int] = {"get": 0, "update": 0}
        whitelist = [{"name": "平安银行", "biz_code": "000001", "ticker": "000001"}]

        async def fake_run_pipeline(
            adapter, writer, tickers=None, dry_run=False
        ) -> PipelineResult:
            return PipelineResult(
                source_type=getattr(adapter, "SOURCE_TYPE", "mock"),
                success=True,
                episode_count=1,
            )

        def fake_get_whitelist(_path: str) -> list[dict[str, str]]:
            calls["get"] += 1
            return whitelist

        scheduler = _make_scheduler(monkeypatch, interval_sec=60)
        _install_all_adapters(scheduler)
        scheduler._tier_groups = scheduler._build_tier_groups()

        monkeypatch.setattr("src.ingestion.scheduler.run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(
            "src.ingestion.scheduler.get_ticker_whitelist", fake_get_whitelist
        )
        monkeypatch.setattr(
            "src.ingestion.scheduler._extract_sector_names",
            lambda tickers: [t["name"] for t in tickers],
        )

        # Track that the stock adapters actually receive the whitelist.
        received: dict[str, list] = {}

        def fake_update(tickers: list[dict[str, str]]) -> None:
            calls["update"] += 1
            for name in ("_cls_adapter", "_eastmoney_adapter", "_akshare_adapter"):
                received[name] = tickers

        scheduler._update_adapter_tickers = fake_update  # type: ignore[method-assign]

        # Tier 1: composite "stock" unit present → whitelist must load.
        results = await scheduler._run_tier_cycle(1)
        assert results
        assert calls["get"] == 1
        assert calls["update"] == 1
        assert received["_cls_adapter"] == whitelist

        # Tier 3: no stock-family unit → no whitelist IO at all.
        await scheduler._run_tier_cycle(3)
        assert calls["get"] == 1  # unchanged
        assert calls["update"] == 1  # unchanged

    async def test_tier4_research_loads_whitelist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict[str, int] = {"get": 0}

        async def fake_run_pipeline(
            adapter, writer, tickers=None, dry_run=False
        ) -> PipelineResult:
            return PipelineResult(
                source_type=getattr(adapter, "SOURCE_TYPE", "mock"),
                success=True,
                episode_count=1,
            )

        monkeypatch.setattr("src.ingestion.scheduler.run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(
            "src.ingestion.scheduler.get_ticker_whitelist",
            lambda _path: calls.__setitem__("get", calls["get"] + 1) or [
                {"name": "贵州茅台", "symbol": "600519"}
            ],
        )

        scheduler = _make_scheduler(monkeypatch, interval_sec=60)
        _install_all_adapters(scheduler)
        scheduler._tier_groups = scheduler._build_tier_groups()

        # Tier 4 contains eastmoney_research → whitelist loads once.
        await scheduler._run_tier_cycle(4)
        assert calls["get"] == 1


class TestTierErrorIsolation:
    async def test_single_adapter_failure_isolated_within_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(monkeypatch, interval_sec=60)
        _install_all_adapters(scheduler)
        scheduler._tier_groups = scheduler._build_tier_groups()

        async def fake_run_pipeline(
            adapter, writer, tickers=None, dry_run=False
        ) -> PipelineResult:
            if adapter is scheduler._eia_adapter:
                raise RuntimeError("EIA boom")
            return PipelineResult(
                source_type=getattr(adapter, "SOURCE_TYPE", "mock"),
                success=True,
                episode_count=1,
            )

        monkeypatch.setattr("src.ingestion.scheduler.run_pipeline", fake_run_pipeline)

        # Tier 3: EIA fails, ACLED + Sanctions must still succeed.
        results = await scheduler._run_tier_cycle(3)
        by_type = {r.source_type: r for r in results}
        assert by_type["eia"].success is False
        assert by_type["eia"].error is not None
        assert by_type["acled"].success is True
        assert by_type["sanctions"].success is True

        # Cross-tier: Tier 1/2/4 unaffected.
        for tier in (1, 2, 4):
            results = await scheduler._run_tier_cycle(tier)
            assert results
            assert all(r.success for r in results), f"tier {tier} affected by EIA failure"

    async def test_unhandled_coroutine_exception_collected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(monkeypatch, interval_sec=60)
        _install_all_adapters(scheduler)
        scheduler._tier_groups = scheduler._build_tier_groups()

        real = scheduler._run_adapter_pipeline

        async def raising(adapter, writer, tickers):
            if adapter is scheduler._china_macro_adapter:
                raise RuntimeError("china_macro coroutine boom")
            return await real(adapter, writer, tickers)

        scheduler._run_adapter_pipeline = raising  # type: ignore[method-assign]

        results = await scheduler._run_tier_cycle(4)
        by_type = {r.source_type: r for r in results}
        # Exception is collected as a failed result, not propagated.
        assert by_type["china_macro"].success is False
        assert by_type["bls"].success is True
        assert by_type["eastmoney_research"].success is True

    async def test_tier_loop_survives_recurring_adapter_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        eia_calls = {"n": 0}
        acled_calls = {"n": 0}

        async def fake_run_pipeline(
            adapter, writer, tickers=None, dry_run=False
        ) -> PipelineResult:
            if adapter is scheduler._eia_adapter:
                eia_calls["n"] += 1
                raise RuntimeError("EIA keeps failing")
            if adapter is scheduler._acled_adapter:
                acled_calls["n"] += 1
            return PipelineResult(
                source_type=getattr(adapter, "SOURCE_TYPE", "mock"),
                success=True,
                episode_count=0,
            )

        scheduler = _make_scheduler(
            monkeypatch,
            interval_sec=0.05,
            ingestion_tier2_interval_sec=0.1,
            ingestion_tier3_interval_sec=0.1,
            ingestion_tier4_interval_sec=0.1,
        )
        _install_all_adapters(scheduler)
        monkeypatch.setattr("src.ingestion.scheduler.run_pipeline", fake_run_pipeline)

        await scheduler.start()
        try:
            await asyncio.sleep(0.15)
            # Loop still alive after EIA failed on its first cycle…
            assert not scheduler._tasks[3].done()
            assert eia_calls["n"] >= 1
            assert acled_calls["n"] >= 1

            await asyncio.sleep(0.15)
            # …and keeps retrying across windows (no loop exit).
            assert eia_calls["n"] >= 2
        finally:
            await scheduler.stop()


# ── 3.5: lifecycle + shared dedup cache ────────────────────────────────


class TestLifecycle:
    async def test_stop_cancels_all_tier_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(
            monkeypatch,
            interval_sec=60,
            ingestion_tier2_interval_sec=60,
            ingestion_tier3_interval_sec=60,
            ingestion_tier4_interval_sec=60,
        )
        _install_all_adapters(scheduler)

        await scheduler.start()
        tasks = list(scheduler._tasks.values())
        assert len(tasks) == 4

        await scheduler.stop()

        assert scheduler._running is False
        assert scheduler._tasks == {}
        assert all(t.done() for t in tasks)

    def test_shared_dedup_cache_across_tiers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _make_scheduler(monkeypatch, interval_sec=60)
        _install_all_adapters(scheduler)
        scheduler._tier_groups = scheduler._build_tier_groups()

        cache_ids: set[int] = set()
        for tier, units in scheduler._tier_groups.items():
            assert tier in TIER_MAP
            for name, adapter, _writer in units:
                if name == "stock":
                    continue
                cache_ids.add(id(adapter.dedup_cache))

        # Exactly one shared cache instance across every tier.
        assert len(cache_ids) == 1
        assert id(scheduler._dedup_cache) in cache_ids