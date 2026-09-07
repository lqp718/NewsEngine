"""P2-2/P2-3 单元测试 — sector 查询标签过滤 + API 契约死字段清理

验证:
    1. P2-2: _build_sector_events_query() 的 sector_ent 匹配带
       'Sector' IN labels(sector_ent) 过滤 —— 同名跨类型实体
       （如 Organization 与 Sector 重名）不再误命中
    2. P2-3: EventItem.relations 死字段已删除（引用已废弃的
       CAUSED_BY/MITIGATES 关系语义，零消费方 —— 含 SynapseEngine
       跨仓核查），EventRelationItem 类已移除

不需要真实 Neo4j — 纯 Cypher 文本 / Pydantic schema 断言。

运行方式:
    .venv/bin/python -m pytest tests/test_api/test_events_sector_query.py -v
"""

import src.api.models as api_models
import src.api.routers.events as events_router
from src.api.models import EventItem
from src.api.routers.events import _build_sector_events_query


class TestSectorQueryLabelFilter:
    """P2-2: sector 查询按 :Sector 标签过滤。"""

    def test_sector_ent_has_label_filter(self):
        cypher = _build_sector_events_query()
        assert "'Sector' IN labels(sector_ent)" in cypher

    def test_label_filter_applies_to_sector_ent_match(self):
        # 过滤条件必须与 sector_ent.name 参数约束同在（同一 WHERE 语义），
        # 防止只加了过滤但仍按裸 name 全库匹配
        cypher = _build_sector_events_query()
        sector_match_idx = cypher.index("MATCH (sector_ent:Entity)")
        filter_idx = cypher.index("'Sector' IN labels(sector_ent)")
        name_idx = cypher.index("sector_ent.name = $sector_name")
        assert sector_match_idx < filter_idx < name_idx

    def test_query_still_parameterized(self):
        # 回归保护: sector 名仍走参数绑定，未被内联
        cypher = _build_sector_events_query()
        assert "$sector_name" in cypher


class TestEventItemRelationsRemoved:
    """P2-3: EventItem.relations 死字段与 EventRelationItem 已删除。"""

    def test_relations_field_removed(self):
        assert "relations" not in EventItem.model_fields

    def test_event_relation_item_class_removed(self):
        assert not hasattr(api_models, "EventRelationItem")

    def test_router_no_longer_imports_event_relation_item(self):
        assert not hasattr(events_router, "EventRelationItem")

    def test_event_item_still_serializes_core_fields(self):
        # 删除死字段不破坏既有响应契约（其余字段保持必填/可选语义）
        item = EventItem(
            event_id="evt-20260905-001",
            title="t",
            severity="high",
            first_seen="2026-09-05T00:00:00+08:00",
            last_updated="2026-09-05T00:00:00+08:00",
            source_count=1,
            keywords=["k"],
            entities=[],
        )
        dumped = item.model_dump()
        assert "relations" not in dumped
        assert dumped["event_id"] == "evt-20260905-001"

    def test_event_item_ignores_relations_kwarg(self):
        # Pydantic 默认 extra='ignore': 旧客户端若仍传 relations，
        # 构造不报错但字段不再进入响应契约
        item = EventItem(
            event_id="evt-20260905-002",
            title="t",
            severity="high",
            first_seen="2026-09-05T00:00:00+08:00",
            last_updated="2026-09-05T00:00:00+08:00",
            source_count=1,
            keywords=["k"],
            entities=[],
            relations=[{"type": "CAUSED_BY", "target_event_id": "x"}],
        )
        assert "relations" not in item.model_dump()
        assert getattr(item, "relations", None) is None
