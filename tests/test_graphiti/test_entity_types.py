"""单元测试: entity_types — 金融实体类型定义（双注册表）。

注意: 实体 name 由 graphiti-core 原生提取（EntityNode.name），
entity_types 模型只承载补充属性字段，不包含名称字段。
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError
import pytest

from src.graphiti.entity_types import (
    StockEntity,
    SectorEntity,
    CountryEntity,
    PolicyEntity,
    OrganizationEntity,
    TopicEntity,
    EventEntity,
    SymbolEventEntity,
    MACRO_ENTITY_TYPES,
    SYMBOL_ENTITY_TYPES,
)


class TestStockEntity:
    def test_valid(self):
        s = StockEntity(ticker="0700.HK", sector="互联网平台", exchange="HKEX")
        assert s.ticker == "0700.HK"
        assert s.sector == "互联网平台"
        assert s.exchange == "HKEX"

    def test_missing_ticker_allowed(self):
        """ticker is optional (auto-grounded by post-write normalizer)."""
        s = StockEntity(sector="互联网平台", exchange="HKEX")
        assert s.ticker is None

    def test_missing_required_fields_raises(self):
        """sector 和 exchange 为必填字段（name 由 Graphiti 原生提取，无需提供）。"""
        with pytest.raises(ValidationError):
            StockEntity(ticker="0700.HK")


class TestSectorEntity:
    def test_empty_model_valid(self):
        """SectorEntity 为空模型 — name 由 Graphiti 原生提取，无需任何字段。"""
        s = SectorEntity()
        assert isinstance(s, SectorEntity)


class TestCountryEntity:
    def test_empty_model_valid(self):
        """CountryEntity 为空模型 — name 由 Graphiti 原生提取，无需任何字段。"""
        c = CountryEntity()
        assert isinstance(c, CountryEntity)


class TestPolicyEntity:
    def test_valid(self):
        p = PolicyEntity(type="monetary", status="rumor")
        assert p.type == "monetary"
        assert p.status == "rumor"

    def test_missing_type_raises(self):
        with pytest.raises(ValidationError):
            PolicyEntity(status="rumor")


class TestMacroEntityTypes:
    def test_has_seven_keys(self):
        assert len(MACRO_ENTITY_TYPES) == 7

    def test_keys_match(self):
        expected = {"Organization", "Country", "Topic", "Policy", "Sector", "Event", "Person"}
        assert set(MACRO_ENTITY_TYPES.keys()) == expected

    def test_values_are_base_model_subclasses(self):
        for name, model in MACRO_ENTITY_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"


class TestSymbolEntityTypes:
    def test_has_seven_keys(self):
        assert len(SYMBOL_ENTITY_TYPES) == 7

    def test_keys_match(self):
        expected = {"Stock", "Sector", "Organization", "Country", "Policy", "Event", "Person"}
        assert set(SYMBOL_ENTITY_TYPES.keys()) == expected

    def test_values_are_base_model_subclasses(self):
        for name, model in SYMBOL_ENTITY_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"

    def test_stock_entity_in_symbol_types(self):
        assert SYMBOL_ENTITY_TYPES["Stock"] is StockEntity

    def test_event_entity_is_symbol_event(self):
        assert SYMBOL_ENTITY_TYPES["Event"] is SymbolEventEntity