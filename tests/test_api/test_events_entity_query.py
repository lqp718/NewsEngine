"""P0-3 单元测试 — GET /api/events/entity/{ticker} 查询契约

验证:
    1. _build_entity_events_query() 已参数化（$window_days / $min_severity_weight /
       $limit），不再硬编码 duration({days: 3})
    2. get_entity_events 把 limit / min_severity / 配置窗口正确传入 Cypher params
       （此前 SynapseEngine 客户端发送的 limit / min_severity 被丢弃）
    3. 默认值: limit=30, min_severity="medium"(weight=2)

不需要真实 Neo4j — 使用 fake driver 捕获 Cypher 与参数。

运行方式:
    .venv/bin/python -m pytest tests/test_api/test_events_entity_query.py -v
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.routers.events import _build_entity_events_query, get_entity_events
from src.graphiti.translation import SEVERITY_WEIGHT


# ==============================================================================
# Fake Neo4j driver — 捕获 cypher + params
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


def _fake_settings(window_days: int = 11):
    """Endpoint 只读取 settings.entity_events_window_days。"""
    return SimpleNamespace(entity_events_window_days=window_days)


def _fake_ep_record(severity: str = "high"):
    now = datetime.now(timezone.utc)
    return {
        "ep": {
            "uuid": "ep-test-001",
            "name": "test event",
            "content": "测试事件标题\n这是测试事件的摘要正文内容。",
            "severity": severity,
            "created_at": now,
            "valid_at": now,
            "source_count": 1,
        },
        "entities": [],
    }


# ==============================================================================
# 查询构造器 — 参数化契约
# ==============================================================================


class TestBuildEntityEventsQuery:

    def test_window_is_parameterized(self):
        query = _build_entity_events_query()
        assert "$window_days" in query
        # P0-3 回归: 不再硬编码 3 天窗口
        assert "days: 3" not in query

    def test_limit_is_parameterized(self):
        query = _build_entity_events_query()
        assert "LIMIT $limit" in query

    def test_severity_filter_present(self):
        query = _build_entity_events_query()
        assert "$min_severity_weight" in query
        # severity 缺省按 medium 处理（与 translation.SEVERITY_DEFAULT 一致）
        assert "coalesce(ep.severity, 'medium')" in query

    def test_ordered_by_recency(self):
        query = _build_entity_events_query()
        assert "ORDER BY ep.valid_at DESC, ep.created_at DESC" in query


# ==============================================================================
# Endpoint — 参数透传
# ==============================================================================


class TestGetEntityEventsParams:

    @pytest.mark.asyncio
    async def test_explicit_params_passed_to_cypher(self):
        driver = _FakeDriver(records=[_fake_ep_record()])
        resp = await get_entity_events(
            ticker="000858.SZ",
            limit=5,
            min_severity="high",
            include_graph=False,
            neo4j_driver=driver,
            settings=_fake_settings(window_days=11),
        )

        assert driver.captured is not None
        _, params = driver.captured
        assert params["ticker"] == "000858.SZ"
        assert params["limit"] == 5
        assert params["window_days"] == 11
        assert params["min_severity_weight"] == SEVERITY_WEIGHT["high"]
        assert resp.ticker == "000858.SZ"
        assert len(resp.events) == 1

    @pytest.mark.asyncio
    async def test_default_params(self):
        driver = _FakeDriver(records=[_fake_ep_record(severity="medium")])
        await get_entity_events(
            ticker="0700.HK",
            limit=30,
            min_severity="medium",
            include_graph=False,
            neo4j_driver=driver,
            settings=_fake_settings(window_days=7),
        )

        _, params = driver.captured
        assert params["limit"] == 30
        assert params["window_days"] == 7
        assert params["min_severity_weight"] == SEVERITY_WEIGHT["medium"]

    @pytest.mark.asyncio
    async def test_critical_weight(self):
        driver = _FakeDriver(records=[])
        with pytest.raises(HTTPException) as exc_info:
            await get_entity_events(
                ticker="600519.SH",
                limit=10,
                min_severity="critical",
                include_graph=False,
                neo4j_driver=driver,
                settings=_fake_settings(),
            )
        # 无事件 → 404，但参数已正确捕获
        assert exc_info.value.status_code == 404
        _, params = driver.captured
        assert params["min_severity_weight"] == SEVERITY_WEIGHT["critical"]
        assert params["ticker"] == "600519.SH"

    @pytest.mark.asyncio
    async def test_a_share_ticker_not_mangled(self):
        # P0-3 回归: A 股 ticker 原样传给 Cypher（不带 .HK 后缀）
        driver = _FakeDriver(records=[_fake_ep_record()])
        await get_entity_events(
            ticker="000858.SZ",
            limit=30,
            min_severity="medium",
            include_graph=False,
            neo4j_driver=driver,
            settings=_fake_settings(),
        )
        _, params = driver.captured
        assert params["ticker"] == "000858.SZ"
        assert ".HK" not in params["ticker"]
