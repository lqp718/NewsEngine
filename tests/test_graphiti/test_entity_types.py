"""单元测试: entity_types — 4 种金融实体类型定义。"""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from src.graphiti.entity_types import (
    StockEntity,
    SectorEntity,
    CountryEntity,
    PolicyEntity,
    ENTITY_TYPES,
)


class TestStockEntity:
    def test_valid(self):
        s = StockEntity(ticker="0700.HK", entity_name="腾讯控股")
        assert s.ticker == "0700.HK"
        assert s.entity_name == "腾讯控股"
        assert s.sector is None
        assert s.exchange is None

    def test_all_fields(self):
        s = StockEntity(
            ticker="BABA.US",
            entity_name="阿里巴巴",
            sector="互联网平台",
            exchange="NYSE",
        )
        assert s.ticker == "BABA.US"
        assert s.sector == "互联网平台"
        assert s.exchange == "NYSE"

    def test_missing_ticker_raises(self):
        with pytest.raises(ValidationError):
            StockEntity(name="腾讯控股")

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            StockEntity(ticker="0700.HK")


class TestSectorEntity:
    def test_with_code(self):
        s = SectorEntity(entity_name="互联网平台", code="GICS_50")
        assert s.entity_name == "互联网平台"
        assert s.code == "GICS_50"

    def test_minimal(self):
        s = SectorEntity(entity_name="新能源")
        assert s.entity_name == "新能源"
        assert s.code is None

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            SectorEntity()


class TestCountryEntity:
    def test_with_code(self):
        c = CountryEntity(entity_name="中国", code="CN")
        assert c.entity_name == "中国"
        assert c.code == "CN"

    def test_eu_no_code(self):
        c = CountryEntity(entity_name="欧盟")
        assert c.entity_name == "欧盟"
        assert c.code is None

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            CountryEntity()


class TestPolicyEntity:
    def test_default_status(self):
        p = PolicyEntity(entity_name="降息", type="monetary")
        assert p.entity_name == "降息"
        assert p.type == "monetary"
        assert p.status == "rumor"

    def test_all_fields(self):
        p = PolicyEntity(
            entity_name="反垄断调查",
            type="regulatory",
            status="confirmed",
        )
        assert p.status == "confirmed"

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            PolicyEntity(type="regulatory")

    def test_missing_type_raises(self):
        with pytest.raises(ValidationError):
            PolicyEntity(entity_name="测试")


class TestEntityTypesDict:
    def test_has_four_keys(self):
        assert len(ENTITY_TYPES) == 4

    def test_keys_match(self):
        assert set(ENTITY_TYPES.keys()) == {"Stock", "Sector", "Country", "Policy"}

    def test_values_are_base_model_subclasses(self):
        from pydantic import BaseModel

        for name, model in ENTITY_TYPES.items():
            assert issubclass(model, BaseModel), f"{name} is not a BaseModel subclass"
