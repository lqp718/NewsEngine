"""CR 第二轮残留修复回归测试 — neo4j.time.DateTime 全端点覆盖

验证:
    1. P1 残留: translation.py 两处 isinstance(x, datetime) 改为
       coerce_datetime() 后，neo4j.time.DateTime 形态的 valid_at/created_at
       不再静默回退 now_hkt()（translate_episode_to_event /
       translate_episode_to_briefing_input）
    2. 端点级: /events/active、/events/entity/{ticker}、/events/sector/{name}
       喂入 Neo4j DateTime 记录时，EventItem.first_seen/last_updated
       反映真实事件时间（非当前时间）
    3. P2-1: neo4j_client.ensure_indexes() 幂等创建 entity_ticker 索引，
       失败不抛异常（best-effort）

不需要真实 Neo4j — fake driver + neo4j.time.DateTime 值对象。

运行方式:
    .venv/bin/python -m pytest tests/test_api/test_events_datetime_coercion.py -v
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from neo4j.time import DateTime as Neo4jDateTime
from neo4j.time import timezone as neo4j_timezone

from src.api.routers.events import (
    get_active_events,
    get_entity_events,
    get_sector_events,
)
from src.graphiti.translation import (
    translate_episode_to_briefing_input,
    translate_episode_to_event,
)
from src.utils.time_utils import to_iso8601

HKT = neo4j_timezone(timedelta(hours=8))

# 真实事件时间: HKT 2026-09-01 20:30 == UTC 2026-09-01 12:30
_VALID_AT_N4J = Neo4jDateTime(2026, 9, 1, 20, 30, 0, tzinfo=HKT)
_CREATED_AT_N4J = Neo4jDateTime(2026, 9, 1, 21, 0, 0, tzinfo=HKT)
_VALID_AT_ISO = to_iso8601(datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc))
_CREATED_AT_ISO = to_iso8601(datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc))


def _fake_episode(**overrides):
    ep = {
        "uuid": "ep-n4j-001",
        "name": "neo4j datetime episode",
        "content": "测试事件标题\n这是摘要正文内容，长度超过十个字符。",
        "severity": "high",
        "source_count": 2,
        "valid_at": _VALID_AT_N4J,
        "created_at": _CREATED_AT_N4J,
    }
    ep.update(overrides)
    return ep


# ==============================================================================
# 1. translation 层 — neo4j.time.DateTime 归一化
# ==============================================================================


class TestTranslateEpisodeToEventDateTime:
    """translate_episode_to_event: isinstance → coerce_datetime 修复。"""

    def test_neo4j_datetime_valid_at_not_fallback_to_now(self):
        result = translate_episode_to_event({"e": _fake_episode()}, [])
        # 修复前: isinstance(Neo4jDateTime, datetime) == False →
        # first_seen 静默回退 to_iso8601(now_hkt())
        assert result["first_seen"] == _VALID_AT_ISO
        assert result["last_updated"] == _CREATED_AT_ISO

    def test_neo4j_datetime_valid_at_only(self):
        # created_at 缺失 → last_updated 跟随 first_seen（原语义保持）
        ep = _fake_episode()
        del ep["created_at"]
        result = translate_episode_to_event({"e": ep}, [])
        assert result["first_seen"] == _VALID_AT_ISO
        assert result["last_updated"] == _VALID_AT_ISO

    def test_native_datetime_still_works(self):
        # 回归保护: 原生 datetime 路径行为不变
        native = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
        result = translate_episode_to_event(
            {"e": _fake_episode(valid_at=native, created_at=native)}, []
        )
        assert result["first_seen"] == to_iso8601(native)
        assert result["last_updated"] == to_iso8601(native)

    def test_uncoercible_falls_back_to_now(self):
        # 无法归一化的值（如字符串）→ first_seen 回退 now（原语义保持）
        before = to_iso8601(datetime.now(timezone.utc).replace(microsecond=0))
        result = translate_episode_to_event(
            {"e": _fake_episode(valid_at="not-a-datetime", created_at=None)}, []
        )
        after = to_iso8601(datetime.now(timezone.utc).replace(microsecond=0))
        assert before <= result["first_seen"] <= after


class TestTranslateEpisodeToBriefingInputDateTime:
    """translate_episode_to_briefing_input: 同一 P1 残留模式修复。"""

    def test_neo4j_datetime_coerced_to_native(self):
        result = translate_episode_to_briefing_input({"e": _fake_episode()}, [])
        assert isinstance(result["first_seen"], datetime)
        assert isinstance(result["last_updated"], datetime)
        # HKT 20:30 → UTC 12:30（coerce 后为等价时刻的原生 datetime）
        assert result["first_seen"] == datetime(
            2026, 9, 1, 12, 30, tzinfo=timezone.utc
        )
        assert result["last_updated"] == datetime(
            2026, 9, 1, 13, 0, tzinfo=timezone.utc
        )

    def test_missing_timestamps_fall_back_to_now(self):
        ep = _fake_episode()
        del ep["valid_at"]
        del ep["created_at"]
        result = translate_episode_to_briefing_input({"e": ep}, [])
        assert isinstance(result["first_seen"], datetime)
        assert result["last_updated"] == result["first_seen"]


# ==============================================================================
# 2. 端点级 — fake driver 喂 Neo4j DateTime 记录
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

    def run(self, cypher, params=None):
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


def _fake_settings():
    return SimpleNamespace(entity_events_window_days=7)


def _fake_aggregator(briefing=None):
    return SimpleNamespace(get_cached=lambda sector: briefing)


class TestEndpointValidAtWithNeo4jDateTime:
    """三个事件端点的 EventItem 时间戳均反映 Neo4j DateTime 真实值。"""

    @pytest.mark.asyncio
    async def test_active_events_first_seen_from_neo4j_datetime(self):
        driver = _FakeDriver(records=[{"e": _fake_episode(), "entities": []}])
        resp = await get_active_events(
            limit=10, min_severity="medium", sector=None, neo4j_driver=driver
        )
        assert len(resp.events) == 1
        assert resp.events[0].first_seen == _VALID_AT_ISO
        assert resp.events[0].last_updated == _CREATED_AT_ISO

    @pytest.mark.asyncio
    async def test_entity_events_first_seen_from_neo4j_datetime(self):
        driver = _FakeDriver(records=[{"ep": _fake_episode(), "entities": []}])
        resp = await get_entity_events(
            ticker="0700.HK",
            limit=30,
            min_severity="medium",
            include_graph=False,
            neo4j_driver=driver,
            settings=_fake_settings(),
        )
        assert len(resp.events) == 1
        assert resp.events[0].first_seen == _VALID_AT_ISO
        assert resp.events[0].last_updated == _CREATED_AT_ISO

    @pytest.mark.asyncio
    async def test_sector_events_first_seen_from_neo4j_datetime(self):
        driver = _FakeDriver(
            records=[{"ep": _fake_episode(), "entities": [], "ticker_count": 1}]
        )
        resp = await get_sector_events(
            sector_name="互联网平台",
            neo4j_driver=driver,
            aggregator=_fake_aggregator(),
        )
        assert len(resp.events) == 1
        assert resp.events[0].first_seen == _VALID_AT_ISO
        assert resp.events[0].last_updated == _CREATED_AT_ISO

    @pytest.mark.asyncio
    async def test_active_events_sorted_by_real_valid_at(self):
        """两条 Neo4j DateTime 记录按 last_updated 降序（非插入序）。"""
        late = _fake_episode(uuid="ep-late")  # HKT 9/1 21:00
        early = _fake_episode(
            uuid="ep-early",
            valid_at=Neo4jDateTime(2026, 8, 30, 8, 0, 0, tzinfo=HKT),
            created_at=Neo4jDateTime(2026, 8, 30, 9, 0, 0, tzinfo=HKT),
        )
        driver = _FakeDriver(
            records=[
                {"e": early, "entities": []},
                {"e": late, "entities": []},
            ]
        )
        resp = await get_active_events(
            limit=10, min_severity="medium", sector=None, neo4j_driver=driver
        )
        assert len(resp.events) == 2
        # late (HKT 9/1 21:00) 排在 early (HKT 8/30 09:00) 之前 — 非插入序
        assert resp.events[0].first_seen == _VALID_AT_ISO
        first_seen_values = [ev.first_seen for ev in resp.events]
        assert first_seen_values == sorted(first_seen_values, reverse=True)


# ==============================================================================
# 3. P2-1 — entity_ticker 索引（best-effort，幂等）
# ==============================================================================


class _IndexFakeSession:
    def __init__(self, driver):
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, params=None):
        self._driver.ddl.append(cypher)
        if self._driver.fail:
            raise RuntimeError("index creation denied")
        return SimpleNamespace(consume=lambda: None)


class _IndexFakeDriver:
    def __init__(self, fail=False):
        self.ddl: list[str] = []
        self.fail = fail

    def session(self):
        return _IndexFakeSession(self)


class TestEnsureIndexes:
    def test_creates_entity_ticker_index(self):
        from src.core.neo4j_client import ensure_indexes

        driver = _IndexFakeDriver()
        ensure_indexes(driver)
        assert any(
            "entity_ticker" in ddl and "e.ticker" in ddl and "IF NOT EXISTS" in ddl
            for ddl in driver.ddl
        )

    def test_failure_is_swallowed(self):
        # best-effort: 索引创建失败仅记 WARNING，不阻断驱动初始化
        from src.core.neo4j_client import ensure_indexes

        driver = _IndexFakeDriver(fail=True)
        ensure_indexes(driver)  # 不抛异常即通过
