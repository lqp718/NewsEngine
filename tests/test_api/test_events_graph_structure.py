"""P2-1 图结构 API 单元测试 — GET /api/events/entity/:ticker 的 graph + episodes.

验证:
    1. 向后兼容: EntityEventsResponse 新增 graph/episodes 为可选字段，
       默认 None，既有 ticker/events/summary 契约不变
    2. Cypher 构造: hops 内插 1..3、越界防御（ValueError）、参数化
       $ticker/$edge_limit/$window_days/$episode_row_limit
    3. _build_graph_structure: 节点按 name 去重、边按 (source,target,type)
       去重、孤立起点行（rel=NULL）仍产出 start 节点、缺失 edge_type
       兜底 RELATED_TO
    4. _build_episode_items: 按 ep.uuid 归组、实体去重、title 取 content
       首行、valid_at 升序
    5. 端点: include_graph=True → graph/episodes 填充；include_graph=False
       → 不发图查询且字段为 None；图查询异常 → 降级 None，events 不受影响

不需要真实 Neo4j — 使用按 Cypher 特征分发的 fake driver。

运行方式:
    .venv/bin/python -m pytest tests/test_api/test_events_graph_structure.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.models import (
    EntityEventsResponse,
    EpisodeItem,
    GraphEdge,
    GraphNode,
    GraphStructure,
)
from src.api.routers.events import (
    _build_entity_episodes_query,
    _build_entity_graph_query,
    _build_episode_items,
    _build_graph_structure,
    get_entity_events,
)
from src.utils.time_utils import to_iso8601


# ==============================================================================
# Fake driver — 按 Cypher 特征分发三套记录
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
        self._driver.queries.append((cypher, params))
        if self._driver.graph_error and "$edge_limit" in cypher:
            raise self._driver.graph_error
        return iter(_FakeRecord(r) for r in self._driver.records_for(cypher))


class _FakeDriver:
    """records_for 按 Cypher 特征分发:
    - $edge_limit          → graph 查询
    - $episode_row_limit   → episodes 查询
    - $min_severity_weight → 既有 events 查询
    """

    def __init__(self, events=None, graph=None, episodes=None, graph_error=None):
        self._events = events or []
        self._graph = graph or []
        self._episodes = episodes or []
        self.graph_error = graph_error
        self.queries: list[tuple[str, dict]] = []

    def records_for(self, cypher):
        if "$edge_limit" in cypher:
            return self._graph
        if "$episode_row_limit" in cypher:
            return self._episodes
        return self._events

    def verify_connectivity(self):
        return None

    def session(self):
        return _FakeSession(self)


def _fake_settings(window_days: int = 7):
    return SimpleNamespace(entity_events_window_days=window_days)


def _event_record():
    now = datetime.now(timezone.utc)
    return {
        "ep": {
            "uuid": "ep-graph-001",
            "name": "graph event",
            "content": "图结构测试事件标题\n摘要正文内容足够长。",
            "severity": "high",
            "created_at": now,
            "valid_at": now,
            "source_count": 1,
        },
        "entities": [],
    }


def _graph_records():
    """五粮液 → 白酒 → 中国 两跳样例 + 一条重复边 + 一行孤立起点。"""
    return [
        {
            "start_name": "五粮液",
            "start_labels": ["Entity", "Stock"],
            "start_ticker": "000858.SZ",
            "source_name": "五粮液",
            "source_labels": ["Entity", "Stock"],
            "source_ticker": "000858.SZ",
            "target_name": "白酒",
            "target_labels": ["Entity", "Sector"],
            "target_ticker": None,
            "edge_type": "BELONGS_TO",
            "fact": "五粮液属于白酒行业",
        },
        {
            # 重复边（变长路径产生的多行）→ 必须去重
            "start_name": "五粮液",
            "start_labels": ["Entity", "Stock"],
            "start_ticker": "000858.SZ",
            "source_name": "五粮液",
            "source_labels": ["Entity", "Stock"],
            "source_ticker": "000858.SZ",
            "target_name": "白酒",
            "target_labels": ["Entity", "Sector"],
            "target_ticker": None,
            "edge_type": "BELONGS_TO",
            "fact": "五粮液属于白酒行业",
        },
        {
            "start_name": "五粮液",
            "start_labels": ["Entity", "Stock"],
            "start_ticker": "000858.SZ",
            "source_name": "白酒",
            "source_labels": ["Entity", "Sector"],
            "source_ticker": None,
            "target_name": "中国",
            "target_labels": ["Entity", "Country"],
            "target_ticker": None,
            "edge_type": "LOCATED_IN",
            "fact": "白酒行业位于中国",
        },
    ]


def _episode_records():
    t1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 2, tzinfo=timezone.utc)
    return [
        {
            "id": "ep-2",
            "name": "ep2-name",
            "content": "鲁泰A涨停\n第二行正文。",
            "valid_at": t2,
            "entity_name": "鲁泰A",
            "peer_name": "纺织行业",
        },
        {
            "id": "ep-1",
            "name": "ep1-name",
            "content": "关税政策利好纺织行业\n正文。",
            "valid_at": t1,
            "entity_name": "纺织行业",
            "peer_name": "中国",
        },
        {
            # 同一 episode 的另一行（多实体参与）→ 归组合并
            "id": "ep-1",
            "name": "ep1-name",
            "content": "关税政策利好纺织行业\n正文。",
            "valid_at": t1,
            "entity_name": "五粮液",
            "peer_name": "纺织行业",
        },
    ]


# ==============================================================================
# 1. 向后兼容 — 响应模型契约
# ==============================================================================


class TestBackwardCompatibility:

    def test_graph_and_episodes_are_optional(self):
        assert EntityEventsResponse.model_fields["graph"].default is None
        assert EntityEventsResponse.model_fields["episodes"].default is None

    def test_legacy_payload_still_valid(self):
        # 不带 graph/episodes 构造 → 既有契约不变
        resp = EntityEventsResponse(
            ticker="000858.SZ",
            events=[],
            summary={
                "total_events": 0,
                "avg_severity": "medium",
                "risk_level": "LOW",
                "news_sentiment_score": 0.5,
            },
        )
        dumped = resp.model_dump()
        assert dumped["graph"] is None
        assert dumped["episodes"] is None
        assert set(dumped) >= {"ticker", "events", "summary"}


# ==============================================================================
# 2. Cypher 构造器
# ==============================================================================


class TestGraphQueryBuilders:

    @pytest.mark.parametrize("hops", [1, 2, 3])
    def test_hops_interpolated(self, hops):
        q = _build_entity_graph_query(hops)
        assert f"*1..{hops}" in q

    @pytest.mark.parametrize("hops", [0, 4, -1])
    def test_hops_out_of_range_rejected(self, hops):
        # 防御: hops 以字面量内插，越界必须抛 ValueError（杜绝注入面）
        with pytest.raises(ValueError):
            _build_entity_graph_query(hops)
        with pytest.raises(ValueError):
            _build_entity_episodes_query(hops)

    def test_graph_query_parameterized(self):
        q = _build_entity_graph_query(3)
        assert "$ticker" in q
        assert "LIMIT $edge_limit" in q
        assert "start.ticker = $ticker" in q

    def test_episodes_query_parameterized(self):
        q = _build_entity_episodes_query(2)
        assert "$ticker" in q
        assert "$window_days" in q
        assert "LIMIT $episode_row_limit" in q
        assert "rel.uuid IN ep.entity_edges" in q

    def test_graph_query_keeps_isolated_start(self):
        # 起点无邻居时仍需返回一行（rels + [null] 哨兵），
        # 保证 nodes 至少含目标股票自身
        q = _build_entity_graph_query(3)
        assert "rels + [null]" in q


# ==============================================================================
# 3. _build_graph_structure
# ==============================================================================


class TestBuildGraphStructure:

    def test_nodes_and_edges_assembled(self):
        g = _build_graph_structure(_graph_records())
        assert isinstance(g, GraphStructure)
        assert {n.id for n in g.nodes} == {"五粮液", "白酒", "中国"}
        assert len(g.edges) == 2  # 重复 BELONGS_TO 已去重

    def test_node_types_from_labels(self):
        g = _build_graph_structure(_graph_records())
        by_id = {n.id: n for n in g.nodes}
        assert by_id["五粮液"].type == "stock"
        assert by_id["五粮液"].ticker == "000858.SZ"
        assert by_id["白酒"].type == "sector"
        assert by_id["中国"].type == "country"

    def test_edge_fields(self):
        g = _build_graph_structure(_graph_records())
        belongs = next(e for e in g.edges if e.type == "BELONGS_TO")
        assert belongs.source == "五粮液"
        assert belongs.target == "白酒"
        assert belongs.fact == "五粮液属于白酒行业"
        assert isinstance(belongs, GraphEdge)

    def test_isolated_start_row_yields_node_without_edges(self):
        records = [
            {
                "start_name": "五粮液",
                "start_labels": ["Entity", "Stock"],
                "start_ticker": "000858.SZ",
                "source_name": None,
                "source_labels": None,
                "source_ticker": None,
                "target_name": None,
                "target_labels": None,
                "target_ticker": None,
                "edge_type": None,
                "fact": None,
            }
        ]
        g = _build_graph_structure(records)
        assert len(g.nodes) == 1
        assert g.nodes[0].id == "五粮液"
        assert g.edges == []

    def test_missing_edge_type_falls_back(self):
        records = [
            {
                "start_name": "A",
                "start_labels": ["Entity"],
                "start_ticker": None,
                "source_name": "A",
                "source_labels": ["Entity"],
                "source_ticker": None,
                "target_name": "B",
                "target_labels": ["Entity"],
                "target_ticker": None,
                "edge_type": None,
                "fact": None,
            }
        ]
        g = _build_graph_structure(records)
        assert g.edges[0].type == "RELATED_TO"

    def test_empty_records(self):
        g = _build_graph_structure([])
        assert isinstance(g, GraphStructure)
        assert g.nodes == [] and g.edges == []


# ==============================================================================
# 4. _build_episode_items
# ==============================================================================


class TestBuildEpisodeItems:

    def test_grouped_and_sorted_by_valid_at_asc(self):
        items = _build_episode_items(_episode_records())
        assert [it.id for it in items] == ["ep-1", "ep-2"]  # 9/1 在 9/2 前
        assert all(isinstance(it, EpisodeItem) for it in items)

    def test_entities_merged_and_deduped(self):
        items = _build_episode_items(_episode_records())
        ep1 = next(it for it in items if it.id == "ep-1")
        assert ep1.entities == ["纺织行业", "中国", "五粮液"]

    def test_title_from_content_first_line(self):
        items = _build_episode_items(_episode_records())
        ep2 = next(it for it in items if it.id == "ep-2")
        assert ep2.title == "鲁泰A涨停"

    def test_title_falls_back_to_name(self):
        items = _build_episode_items(
            [{"id": "x", "name": "fallback-name", "content": "", "valid_at": None,
              "entity_name": "A", "peer_name": None}]
        )
        assert items[0].title == "fallback-name"
        assert items[0].valid_at is None

    def test_limit_respected(self):
        records = [
            {"id": f"ep-{i}", "name": f"n{i}", "content": f"t{i}",
             "valid_at": None, "entity_name": "A", "peer_name": None}
            for i in range(60)
        ]
        assert len(_build_episode_items(records, limit=50)) == 50

    def test_valid_at_accepts_neo4j_datetime(self):
        """P1 修复回归: neo4j.time.DateTime 不是 datetime.datetime 子类，
        isinstance() 检查失败曾导致 valid_at 永远为 None。"""
        from datetime import timedelta

        from neo4j.time import DateTime as Neo4jDateTime
        from neo4j.time import timezone as neo4j_timezone

        hkt = neo4j_timezone(timedelta(hours=8))
        records = [
            {
                "id": "ep-n4j",
                "name": "neo4j-ep",
                "content": "Neo4j DateTime episode",
                # 驱动真实返回类型: neo4j.time.DateTime（非 datetime 子类）
                "valid_at": Neo4jDateTime(2026, 9, 5, 20, 30, 0, tzinfo=hkt),
                "entity_name": "A",
                "peer_name": None,
            }
        ]
        items = _build_episode_items(records)
        # 与原生 datetime 走同一 to_iso8601 格式（系统既有格式为 ...+00:00Z）
        assert items[0].valid_at == to_iso8601(
            datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)
        )  # HKT 20:30 → UTC 12:30

    def test_neo4j_datetime_sorted_with_native_datetime(self):
        """混合 neo4j.time.DateTime 与原生 datetime 时仍按 valid_at 升序。"""
        from datetime import timedelta

        from neo4j.time import DateTime as Neo4jDateTime
        from neo4j.time import timezone as neo4j_timezone

        hkt = neo4j_timezone(timedelta(hours=8))
        records = [
            {
                "id": "ep-late",
                "name": "late",
                "content": "late",
                "valid_at": datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
                "entity_name": "A",
                "peer_name": None,
            },
            {
                "id": "ep-early",
                "name": "early",
                "content": "early",
                "valid_at": Neo4jDateTime(2026, 9, 2, 8, 0, 0, tzinfo=hkt),  # UTC 9/2 00:00
                "entity_name": "B",
                "peer_name": None,
            },
        ]
        items = _build_episode_items(records)
        assert [it.id for it in items] == ["ep-early", "ep-late"]
        # sort(key=valid_at or "") 对统一格式的 to_iso8601 字符串字典序即时间序，无需修复
        assert items[0].valid_at == to_iso8601(
            datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)
        )
        assert items[1].valid_at == to_iso8601(
            datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
        )


# ==============================================================================
# 5. 端点集成（fake driver）
# ==============================================================================


class TestGetEntityEventsGraph:

    @pytest.mark.asyncio
    async def test_include_graph_populates_graph_and_episodes(self):
        driver = _FakeDriver(
            events=[_event_record()],
            graph=_graph_records(),
            episodes=_episode_records(),
        )
        resp = await get_entity_events(
            ticker="000858.SZ",
            limit=30,
            min_severity="medium",
            include_graph=True,
            graph_depth=3,
            neo4j_driver=driver,
            settings=_fake_settings(),
        )

        assert resp.graph is not None
        assert {n.id for n in resp.graph.nodes} == {"五粮液", "白酒", "中国"}
        assert len(resp.graph.edges) == 2
        assert resp.episodes is not None
        assert [e.id for e in resp.episodes] == ["ep-1", "ep-2"]
        # 既有字段不受影响
        assert resp.ticker == "000858.SZ"
        assert len(resp.events) == 1

        # 三条查询都发出: events → graph → episodes
        cyphers = [q[0] for q in driver.queries]
        assert any("$min_severity_weight" in c for c in cyphers)
        assert any("$edge_limit" in c for c in cyphers)
        assert any("$episode_row_limit" in c for c in cyphers)

        # graph/episodes 查询参数正确
        graph_params = next(p for c, p in driver.queries if "$edge_limit" in c)
        assert graph_params["ticker"] == "000858.SZ"
        ep_params = next(p for c, p in driver.queries if "$episode_row_limit" in c)
        assert ep_params["window_days"] == 7

    @pytest.mark.asyncio
    async def test_include_graph_false_skips_graph_queries(self):
        driver = _FakeDriver(events=[_event_record()])
        resp = await get_entity_events(
            ticker="000858.SZ",
            limit=30,
            min_severity="medium",
            include_graph=False,
            graph_depth=3,
            neo4j_driver=driver,
            settings=_fake_settings(),
        )
        assert resp.graph is None
        assert resp.episodes is None
        # 只发 events 查询
        assert len(driver.queries) == 1
        assert "$min_severity_weight" in driver.queries[0][0]

    @pytest.mark.asyncio
    async def test_graph_depth_passed_to_cypher(self):
        driver = _FakeDriver(events=[_event_record()])
        await get_entity_events(
            ticker="000858.SZ",
            limit=30,
            min_severity="medium",
            include_graph=True,
            graph_depth=2,
            neo4j_driver=driver,
            settings=_fake_settings(),
        )
        graph_cypher = next(c for c, _ in driver.queries if "$edge_limit" in c)
        assert "*1..2" in graph_cypher

    @pytest.mark.asyncio
    async def test_graph_failure_degrades_but_events_survive(self):
        driver = _FakeDriver(
            events=[_event_record()],
            graph_error=RuntimeError("neo4j exploded"),
        )
        resp = await get_entity_events(
            ticker="000858.SZ",
            limit=30,
            min_severity="medium",
            include_graph=True,
            graph_depth=3,
            neo4j_driver=driver,
            settings=_fake_settings(),
        )
        assert resp.graph is None
        assert resp.episodes is None
        assert len(resp.events) == 1  # 主链路不受降级影响

    @pytest.mark.asyncio
    async def test_no_events_still_404(self):
        # 既有 404 契约不因 include_graph 改变
        driver = _FakeDriver(events=[])
        with pytest.raises(HTTPException) as exc_info:
            await get_entity_events(
                ticker="999999.SZ",
                limit=30,
                min_severity="medium",
                include_graph=True,
                graph_depth=3,
                neo4j_driver=driver,
                settings=_fake_settings(),
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_graph_node_model_serialization(self):
        # GraphNode/GraphEdge JSON 序列化形状与诊断报告 §3.8 样例一致
        node = GraphNode(id="五粮液", type="stock", ticker="000858.SZ")
        edge = GraphEdge(
            source="五粮液", target="白酒", type="BELONGS_TO", fact="五粮液属于白酒行业"
        )
        assert node.model_dump() == {
            "id": "五粮液",
            "type": "stock",
            "ticker": "000858.SZ",
        }
        assert edge.model_dump() == {
            "source": "五粮液",
            "target": "白酒",
            "type": "BELONGS_TO",
            "fact": "五粮液属于白酒行业",
        }


# ==============================================================================
# 6. time_utils — Neo4j DateTime 兼容（P1 修复）
# ==============================================================================


class TestCoerceDatetimeNeo4j:
    """coerce_datetime / to_iso8601 对 neo4j.time 类型的处理。

    根因: Neo4j 驱动返回 neo4j.time.DateTime，它不是 datetime.datetime
    子类，isinstance(x, datetime) 为 False → valid_at 曾永远为 None。
    """

    def _neo4j_dt(self):
        from datetime import timedelta

        from neo4j.time import DateTime as Neo4jDateTime
        from neo4j.time import timezone as neo4j_timezone

        return Neo4jDateTime(
            2026, 9, 5, 20, 30, 0, tzinfo=neo4j_timezone(timedelta(hours=8))
        )

    def test_native_datetime_passthrough(self):
        from src.utils.time_utils import coerce_datetime

        dt = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        assert coerce_datetime(dt) is dt

    def test_neo4j_datetime_coerced_to_native(self):
        from src.utils.time_utils import coerce_datetime

        native = coerce_datetime(self._neo4j_dt())
        assert isinstance(native, datetime)
        assert native.utcoffset().total_seconds() == 8 * 3600

    def test_none_and_unsupported_return_none(self):
        from src.utils.time_utils import coerce_datetime

        assert coerce_datetime(None) is None
        assert coerce_datetime("2026-09-05T12:00:00Z") is None
        assert coerce_datetime(12345) is None

    def test_to_iso8601_accepts_neo4j_datetime(self):
        from src.utils.time_utils import to_iso8601

        # HKT 20:30 → UTC 12:30；与原生 datetime 产出格式一致
        assert to_iso8601(self._neo4j_dt()) == to_iso8601(
            datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)
        )

    def test_to_iso8601_rejects_unsupported_type(self):
        from src.utils.time_utils import to_iso8601

        with pytest.raises(TypeError):
            to_iso8601(None)
