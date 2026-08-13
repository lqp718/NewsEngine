"""单元测试: entity_types — 金融实体类型定义（双注册表）。"""

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
        s = StockEntity(ticker="0700.HK", entity_name="腾讯控股", sector="互联网平台", exchange="HKEX")
        assert s.ticker == "0700.HK"
        assert s.entity_name == "腾讯控股"
        assert s.sector == "互联网平台"
        assert s.exchange == "HKEX"

    def test_missing_ticker_raises(self):
        with pytest.raises(ValidationError):
            StockEntity(entity_name="腾讯控股", sector="互联网平台", exchange="HKEX")

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            StockEntity(ticker="0700.HK", sector="互联网平台", exchange="HKEX")


class TestSectorEntity:
    def test_valid(self):
        s = SectorEntity(entity_name="互联网平台")
        assert s.entity_name == "互联网平台"

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            SectorEntity()


class TestCountryEntity:
    def test_valid(self):
        c = CountryEntity(entity_name="中国")
        assert c.entity_name == "中国"

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            CountryEntity()


class TestPolicyEntity:
    def test_valid(self):
        p = PolicyEntity(entity_name="降息", type="monetary", status="rumor")
        assert p.entity_name == "降息"
        assert p.type == "monetary"
        assert p.status == "rumor"

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            PolicyEntity(type="monetary", status="rumor")

    def test_missing_type_raises(self):
        with pytest.raises(ValidationError):
            PolicyEntity(entity_name="测试", status="rumor")


class TestMacroEntityTypes:
    def test_has_six_keys(self):
        assert len(MACRO_ENTITY_TYPES) == 6

    def test_keys_match(self):
        expected = {"Organization", "Country", "Topic", "Policy", "Sector", "Event"}
        assert set(MACRO_ENTITY_TYPES.keys()) == expected

    def test_values_are_base_model_subclasses(self):
        for name, model in MACRO_ENTITY_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"


class TestSymbolEntityTypes:
    def test_has_six_keys(self):
        assert len(SYMBOL_ENTITY_TYPES) == 6

    def test_keys_match(self):
        expected = {"Stock", "Sector", "Organization", "Country", "Policy", "Event"}
        assert set(SYMBOL_ENTITY_TYPES.keys()) == expected

    def test_values_are_base_model_subclasses(self):
        for name, model in SYMBOL_ENTITY_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"

    def test_stock_entity_in_symbol_types(self):
        assert SYMBOL_ENTITY_TYPES["Stock"] is StockEntity

    def test_event_entity_is_symbol_event(self):
        assert SYMBOL_ENTITY_TYPES["Event"] is SymbolEventEntity
