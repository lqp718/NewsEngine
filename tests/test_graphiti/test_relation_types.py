"""单元测试: relation_types — 4 种关系类型定义（G8 修复后）。"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from src.graphiti.relation_types import (
    AffectsEdge,
    InvolvesEdge,
    BelongsToEdge,
    RelatedToEdge,
    EDGE_TYPES,
    DEFAULT_EDGE_TYPE_MAP,
)


class TestAffectsEdge:
    def test_valid(self):
        e = AffectsEdge(fact="监管传闻影响股价", valid_at="2026-06-09")
        assert e.fact == "监管传闻影响股价"
        assert e.valid_at == "2026-06-09"
        assert e.severity == "medium"

    def test_custom_severity(self):
        e = AffectsEdge(fact="重大影响", valid_at="2026-06-09", severity="critical")
        assert e.severity == "critical"

    def test_missing_fact_raises(self):
        with pytest.raises(ValidationError):
            AffectsEdge(valid_at="2026-06-09")

    def test_missing_valid_at_raises(self):
        with pytest.raises(ValidationError):
            AffectsEdge(fact="test")


class TestInvolvesEdge:
    def test_valid(self):
        e = InvolvesEdge(fact="鲍威尔任职美联储", valid_at="2026-06-09")
        assert e.fact == "鲍威尔任职美联储"
        assert e.valid_at == "2026-06-09"

    def test_missing_fact_raises(self):
        with pytest.raises(ValidationError):
            InvolvesEdge(valid_at="2026-06-09")


class TestBelongsToEdge:
    def test_valid(self):
        e = BelongsToEdge(fact="腾讯属于互联网平台")
        assert e.fact == "腾讯属于互联网平台"

    def test_missing_fact_raises(self):
        with pytest.raises(ValidationError):
            BelongsToEdge()


class TestRelatedToEdge:
    def test_valid(self):
        e = RelatedToEdge(fact="波动与政策关联", valid_at="2026-06-09")
        assert e.fact == "波动与政策关联"
        assert e.valid_at == "2026-06-09"


class TestEdgeTypesDict:
    def test_has_four_keys(self):
        assert len(EDGE_TYPES) == 4

    def test_keys_match(self):
        expected = {"AFFECTS", "INVOLVES", "BELONGS_TO", "RELATED_TO"}
        assert set(EDGE_TYPES.keys()) == expected

    def test_values_are_base_model_subclasses(self):
        from pydantic import BaseModel

        for name, model in EDGE_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"


class TestEdgeTypeMap:
    def test_has_eleven_groups(self):
        assert len(DEFAULT_EDGE_TYPE_MAP) == 11

    def test_entity_entity_has_all_types(self):
        types = DEFAULT_EDGE_TYPE_MAP[("Entity", "Entity")]
        assert "AFFECTS" in types
        assert "INVOLVES" in types
        assert "BELONGS_TO" in types
        assert "RELATED_TO" in types

    def test_entity_stock(self):
        assert "AFFECTS" in DEFAULT_EDGE_TYPE_MAP[("Entity", "Stock")]

    def test_stock_sector(self):
        assert "BELONGS_TO" in DEFAULT_EDGE_TYPE_MAP[("Stock", "Sector")]

    def test_entity_policy(self):
        assert "RELATED_TO" in DEFAULT_EDGE_TYPE_MAP[("Entity", "Policy")]

    def test_person_organization_involves(self):
        assert "INVOLVES" in DEFAULT_EDGE_TYPE_MAP[("Person", "Organization")]

    def test_event_person_involves(self):
        assert "INVOLVES" in DEFAULT_EDGE_TYPE_MAP[("Event", "Person")]

    def test_organization_organization_involves(self):
        assert "INVOLVES" in DEFAULT_EDGE_TYPE_MAP[("Organization", "Organization")]


class TestEventEntityMappings:
    """测试 Event 实体类型关系映射。"""

    def test_default_edge_type_map_has_event_mappings(self):
        """DEFAULT_EDGE_TYPE_MAP 应包含 Event 实体类型的 5 个映射。"""
        event_keys = [k for k in DEFAULT_EDGE_TYPE_MAP.keys() if k[0] == "Event"]
        assert len(event_keys) == 5, f"Expected 5 Event mappings, got {len(event_keys)}"

    def test_event_country_affects(self):
        """Event -> Country 应映射为 AFFECTS 关系。"""
        assert ("Event", "Country") in DEFAULT_EDGE_TYPE_MAP
        assert "AFFECTS" in DEFAULT_EDGE_TYPE_MAP[("Event", "Country")]

    def test_event_organization_affects(self):
        """Event -> Organization 应映射为 AFFECTS 关系。"""
        assert ("Event", "Organization") in DEFAULT_EDGE_TYPE_MAP
        assert "AFFECTS" in DEFAULT_EDGE_TYPE_MAP[("Event", "Organization")]

    def test_event_sector_affects(self):
        """Event -> Sector 应映射为 AFFECTS 关系。"""
        assert ("Event", "Sector") in DEFAULT_EDGE_TYPE_MAP
        assert "AFFECTS" in DEFAULT_EDGE_TYPE_MAP[("Event", "Sector")]

    def test_event_topic_related_to(self):
        """Event -> Topic 应映射为 RELATED_TO 关系。"""
        assert ("Event", "Topic") in DEFAULT_EDGE_TYPE_MAP
        assert "RELATED_TO" in DEFAULT_EDGE_TYPE_MAP[("Event", "Topic")]
