"""单元测试: relation_types — 6 种关系类型定义。"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from src.graphiti.relation_types import (
    AffectsEdge,
    CausedByEdge,
    MitigatesEdge,
    BelongsToEdge,
    LocatedInEdge,
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


class TestCausedByEdge:
    def test_valid(self):
        e = CausedByEdge(fact="A导致B", valid_at="2026-06-09")
        assert e.confidence == "medium"

    def test_high_confidence(self):
        e = CausedByEdge(fact="明确因果关系", valid_at="2026-06-09", confidence="high")
        assert e.confidence == "high"


class TestMitigatesEdge:
    def test_valid(self):
        e = MitigatesEdge(fact="公司回应缓解担忧", valid_at="2026-06-09")
        assert e.fact == "公司回应缓解担忧"


class TestBelongsToEdge:
    def test_valid(self):
        e = BelongsToEdge(fact="腾讯属于互联网平台")
        assert e.fact == "腾讯属于互联网平台"


class TestLocatedInEdge:
    def test_valid(self):
        e = LocatedInEdge(fact="腾讯注册在中国")
        assert e.fact == "腾讯注册在中国"


class TestRelatedToEdge:
    def test_valid(self):
        e = RelatedToEdge(fact="波动与政策关联", valid_at="2026-06-09")
        assert e.fact == "波动与政策关联"
        assert e.valid_at == "2026-06-09"


class TestEdgeTypesDict:
    def test_has_six_keys(self):
        assert len(EDGE_TYPES) == 6

    def test_keys_match(self):
        expected = {
            "AFFECTS", "CAUSED_BY", "MITIGATES",
            "BELONGS_TO", "LOCATED_IN", "RELATED_TO",
        }
        assert set(EDGE_TYPES.keys()) == expected

    def test_values_are_base_model_subclasses(self):
        from pydantic import BaseModel

        for name, model in EDGE_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"


class TestEdgeTypeMap:
    def test_has_five_groups(self):
        assert len(DEFAULT_EDGE_TYPE_MAP) == 5

    def test_entity_entity_has_all_types(self):
        types = DEFAULT_EDGE_TYPE_MAP[("Entity", "Entity")]
        assert "AFFECTS" in types
        assert "CAUSED_BY" in types
        assert "MITIGATES" in types
        assert "BELONGS_TO" in types
        assert "LOCATED_IN" in types
        assert "RELATED_TO" in types

    def test_entity_stock(self):
        assert "AFFECTS" in DEFAULT_EDGE_TYPE_MAP[("Entity", "Stock")]

    def test_stock_sector(self):
        assert "BELONGS_TO" in DEFAULT_EDGE_TYPE_MAP[("Stock", "Sector")]

    def test_stock_country(self):
        assert "LOCATED_IN" in DEFAULT_EDGE_TYPE_MAP[("Stock", "Country")]

    def test_entity_policy(self):
        assert "RELATED_TO" in DEFAULT_EDGE_TYPE_MAP[("Entity", "Policy")]
