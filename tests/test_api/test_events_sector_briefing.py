"""L2 断路修复单元测试 — sector_briefing 缓存链路打通.

验证:
    1. get_shared_aggregator() 返回进程级共享单例（两次调用同一实例）
    2. api/deps.py::get_aggregator() 委托共享单例（不再自建实例 ——
       此前 scheduler 与 API 各持一个 SectorBriefingAggregator，
       scheduler 写入的缓存 API 永远读不到）
    3. IngestionScheduler(dry_run=False)._aggregator 即共享单例；
       dry_run=True 时为 None（保持原语义）
    4. GET /api/events/sector/:name 从缓存读取 sector_briefing：
       命中 → Markdown 文本；未命中 → None（消费方降级自行聚合）；
       缓存读取异常 → None 且不影响 events 主链路

不需要真实 Neo4j — 使用 fake driver / fake aggregator。

运行方式:
    .venv/bin/python -m pytest tests/test_api/test_events_sector_briefing.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.api.deps as api_deps
import src.ingestion.briefing_aggregator as ba
from src.api.routers.events import get_sector_events


# ==============================================================================
# Fake Neo4j driver（沿用 tests/test_api 既有模式）
# ==============================================================================


class _FakeRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _FakeSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, params):
        self._driver.captured = (cypher, params)
        return iter(_FakeRecord(r) for r in self._driver.records)


class _FakeDriver:
    def __init__(self, records=None):
        self.records = records or []
        self.captured = None

    def verify_connectivity(self):
        return None

    def session(self):
        return _FakeSession(self)


def _sector_ep_record():
    """一条最小可用的 sector 事件记录（ep + entities + ticker_count）。"""
    now = datetime.now(timezone.utc)
    return {
        "ep": {
            "uuid": "ep-sector-001",
            "name": "sector event",
            "content": "行业事件标题\n行业事件摘要正文内容。",
            "severity": "high",
            "created_at": now,
            "valid_at": now,
            "source_count": 2,
        },
        "entities": [
            {
                "name": "某股票",
                "labels": ["Entity", "Stock"],
                "ticker": "000858.SZ",
            }
        ],
        "ticker_count": 1,
    }


# ==============================================================================
# 1-3. 共享单例链路: briefing_aggregator / deps / scheduler 同源
# ==============================================================================


class TestSharedAggregatorSingleton:
    """写入方（scheduler）与读取方（API）必须共享同一缓存实例。"""

    def test_get_shared_aggregator_is_idempotent(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(ba, "_shared_aggregator", sentinel)
        assert ba.get_shared_aggregator() is sentinel
        assert ba.get_shared_aggregator() is sentinel

    def test_deps_get_aggregator_delegates_to_shared_singleton(self, monkeypatch):
        # L2 断路根因回归: deps 不得再自建独立实例
        sentinel = object()
        monkeypatch.setattr(ba, "_shared_aggregator", sentinel)
        assert api_deps.get_aggregator() is sentinel

    def test_scheduler_uses_shared_aggregator_when_not_dry_run(self, monkeypatch):
        from src.ingestion import scheduler as scheduler_mod

        sentinel = object()
        monkeypatch.setattr(
            scheduler_mod, "get_shared_aggregator", lambda: sentinel
        )
        monkeypatch.setattr(
            scheduler_mod,
            "get_settings",
            lambda: SimpleNamespace(
                ingestion_interval_sec=900,
                min_cycle_gap_sec=0,
                ticker_whitelist_file="data/ticker_whitelist.json",
                landing_enabled=False,
                sweep_interval_sec=300.0,
            ),
        )
        sched = scheduler_mod.IngestionScheduler(dry_run=False)
        assert sched._aggregator is sentinel

    def test_scheduler_dry_run_has_no_aggregator(self, monkeypatch):
        from src.ingestion import scheduler as scheduler_mod

        monkeypatch.setattr(
            scheduler_mod, "get_shared_aggregator", lambda: object()
        )
        monkeypatch.setattr(
            scheduler_mod,
            "get_settings",
            lambda: SimpleNamespace(
                ingestion_interval_sec=900,
                min_cycle_gap_sec=0,
                ticker_whitelist_file="data/ticker_whitelist.json",
                landing_enabled=False,
                sweep_interval_sec=300.0,
            ),
        )
        sched = scheduler_mod.IngestionScheduler(dry_run=True)
        assert sched._aggregator is None


# ==============================================================================
# 4. GET /api/events/sector/:name — sector_briefing 读缓存
# ==============================================================================


class _FakeAggregator:
    """只实现 API 读取面 get_cached 的最小 fake。"""

    def __init__(self, cached=None, raise_on_get=False):
        self._cached = cached
        self._raise = raise_on_get
        self.requested: list[str] = []

    def get_cached(self, sector_name):
        self.requested.append(sector_name)
        if self._raise:
            raise RuntimeError("cache exploded")
        return self._cached


class TestSectorEventsBriefingCache:

    @pytest.mark.asyncio
    async def test_cache_hit_returns_briefing(self):
        driver = _FakeDriver(records=[_sector_ep_record()])
        aggregator = _FakeAggregator(cached="# 白酒行业简报\n核心摘要……")

        resp = await get_sector_events(
            sector_name="白酒",
            neo4j_driver=driver,
            aggregator=aggregator,
        )

        assert resp.sector_briefing == "# 白酒行业简报\n核心摘要……"
        assert aggregator.requested == ["白酒"]
        # events 主链路不受影响
        assert resp.sector == "白酒"
        assert resp.statistics.total_events == 1

    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        # 降级契约: 未命中 → sector_briefing=None，消费方自行聚合
        driver = _FakeDriver(records=[_sector_ep_record()])
        aggregator = _FakeAggregator(cached=None)

        resp = await get_sector_events(
            sector_name="白酒",
            neo4j_driver=driver,
            aggregator=aggregator,
        )

        assert resp.sector_briefing is None
        assert aggregator.requested == ["白酒"]
        assert resp.statistics.total_events == 1

    @pytest.mark.asyncio
    async def test_cache_error_degrades_to_none(self):
        # 缓存读取异常不阻断 events 主链路
        driver = _FakeDriver(records=[_sector_ep_record()])
        aggregator = _FakeAggregator(raise_on_get=True)

        resp = await get_sector_events(
            sector_name="白酒",
            neo4j_driver=driver,
            aggregator=aggregator,
        )

        assert resp.sector_briefing is None
        assert resp.statistics.total_events == 1

    @pytest.mark.asyncio
    async def test_no_hardcoded_none_in_response_path(self):
        # L2 断路回归: 端点不再硬编码 sector_briefing=None ——
        # 只要缓存命中，响应必须携带真实简报
        driver = _FakeDriver(records=[_sector_ep_record()])
        aggregator = _FakeAggregator(cached="briefing-text")

        resp = await get_sector_events(
            sector_name="互联网平台",
            neo4j_driver=driver,
            aggregator=aggregator,
        )
        dumped = resp.model_dump()
        assert dumped["sector_briefing"] == "briefing-text"
