"""Unit tests: P1-1 Sector 语义准入 + P1-2 Sector 语言统一.

背景（诊断报告）:
    145 个 Sector 节点中真正是"行业"的不到一半 —— 指数（恒生指数）、
    政策概念（中国式现代化）、族群（Dalit/Adivasis）、哲学（Critical
    Philosophy of Race）、应急机构（FDNY firefighters）、HTML 泄漏
    （China&rsquo;s）均被误标为 Sector；同时宏观管线强制英文、个股管线
    中文，sector 语言分裂导致实体无法合并。

覆盖:
- P1-1: SECTOR ADMISSION RULES 注入宏观/个股两条管线，含 negative examples
- P1-2: 宏观管线移除"强制英文"（sector 例外），CANONICAL SECTOR NAMES 注入
- P1-2: EntityItem.sector 经 canonical_name 归一（Tech→科技, STAR Market→科创板）
- P1-2: cls_adapter is_stib 硬编码改为 "科创板"
- P1-2: canonical_entities.yaml sectors 区块加载（45+ 条目，覆盖白名单粗类）
- token 控制: sector 注入块长度受限
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.adapters.cls_adapter import CLSAdapter
from src.adapters.models import EntityItem, NormalizedEpisode
from src.graphiti.episode_writer import (
    _SECTOR_NAMES_BLOCK,
    _build_extraction_instructions,
)
from src.utils.entity_canonical import (
    SECTORS,
    canonical_name,
    canonical_sector_names,
)

_WHITELIST_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "ticker_whitelist.json"
)


def _make_episode(source_type: str = "rss") -> NormalizedEpisode:
    return NormalizedEpisode(
        episode_body="Textile exports declined while the chip sector rallied.",
        name=f"{source_type}-20260906-deadbeef0001",
        source_description="test feed",
        source_type=source_type,  # type: ignore[arg-type]
        valid_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        content_hash="",  # model_post_init 会重算
        entities=[],
    )


class TestSectorAdmissionRules:
    """P1-1: 非行业实体不得被标为 Sector（准入规则 + negative examples）。"""

    @pytest.mark.parametrize("source_type", ["rss", "gdelt_csv", "cls_telegraph", "akshare"])
    def test_admission_rules_injected_for_both_pipelines(self, source_type: str):
        instructions = _build_extraction_instructions(_make_episode(source_type))

        assert "SECTOR ADMISSION RULES" in instructions
        # 正面定义：股票市场可交易的行业/板块/概念
        assert "tradable in the stock market" in instructions

    def test_negative_examples_cover_all_diagnosed_junk_classes(self):
        instructions = _build_extraction_instructions(_make_episode("rss"))

        # 指数 / 政策概念 / 族群 / 哲学 / 应急机构 / HTML 泄漏
        assert "恒生指数" in instructions
        assert "中国式现代化" in instructions
        assert "Dalit" in instructions
        assert "Critical Philosophy of Race" in instructions
        assert "FDNY firefighters" in instructions
        assert "China&rsquo;s" in instructions

    def test_admission_rules_present_without_entities(self):
        instructions = _build_extraction_instructions(_make_episode("rss"))
        assert "SECTOR ADMISSION RULES" in instructions
        assert "ENTITY RESOLUTION RULES" not in instructions


class TestSectorLanguageUnification:
    """P1-2: 宏观/个股管线 sector 统一使用中文 canonical 名。"""

    def test_macro_no_longer_forces_english_for_sectors(self):
        instructions = _build_extraction_instructions(_make_episode("rss"))

        assert "Always extract entity names in English" not in instructions
        assert "EXCEPT sector/industry entities" in instructions

    @pytest.mark.parametrize("source_type", ["rss", "cls_telegraph"])
    def test_canonical_sector_names_injected(self, source_type: str):
        instructions = _build_extraction_instructions(_make_episode(source_type))

        assert "SECTOR LANGUAGE RULES" in instructions
        assert "CANONICAL SECTOR NAMES" in instructions
        # 白名单粗类 + 高频宏观行业词均在词表内
        for term in ("科技", "消费", "金融", "纺织", "有色金属", "人工智能", "科创板"):
            assert term in instructions

    def test_english_to_chinese_mapping_examples_in_prompt(self):
        instructions = _build_extraction_instructions(_make_episode("rss"))
        assert "Textiles→纺织" in instructions
        assert "Mining→有色金属" in instructions
        assert "Tech→科技" in instructions

    def test_sector_block_token_controlled(self):
        # 紧凑单行格式，每个 canonical 最多带 1 个 ASCII 别名
        assert "\n" not in _SECTOR_NAMES_BLOCK
        assert len(_SECTOR_NAMES_BLOCK) < 1200


class TestSectorCanonicalization:
    """P1-2: sector 字段经 canonical_name 归一（EntityItem / entity_canonical）。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Tech", "科技"),
            ("Consumer", "消费"),
            ("Finance", "金融"),
            ("STAR Market", "科创板"),
            ("Textiles", "纺织"),
            ("Mining", "有色金属"),
            ("AI", "人工智能"),
            ("Semiconductors", "半导体"),
            ("光伏", "光伏"),          # 中文 canonical 幂等
            ("消费电子", "消费电子"),   # 白名单中文 sector 原样保留
        ],
    )
    def test_entity_item_sector_normalized(self, raw: str, expected: str):
        item = EntityItem(type="stock", name="测试股票", ticker="SH600000", sector=raw)
        assert item.sector == expected

    def test_entity_item_sector_none_safe(self):
        item = EntityItem(type="stock", name="测试股票", ticker="SH600000")
        assert item.sector is None

    def test_canonical_name_sector_lookup(self):
        assert canonical_name("Insurance", "sector") == "保险"
        assert canonical_name("Liquor II", "sector") == "白酒"
        assert canonical_name("非银金融", "sector") == "金融"

    def test_sectors_block_loaded_from_yaml(self):
        names = canonical_sector_names()
        assert len(names) >= 40
        assert len(names) == len(SECTORS)
        # 每个 sector canonical 必须是中文（P1-2 语言统一前提）
        for name in names:
            assert not name.isascii(), f"sector canonical 必须是中文: {name}"


class TestClsAdapterStarMarket:
    """P1-2: cls_adapter is_stib 硬编码 'STAR Market' → '科创板'。"""

    def test_stib_stock_gets_chinese_sector(self):
        adapter = CLSAdapter()
        entities = adapter._extract_entities_from_stock_list(
            [{"name": "中芯国际", "StockID": "sh688981", "is_stib": True}]
        )
        assert entities[0].sector == "科创板"

    def test_non_stib_stock_has_no_sector(self):
        adapter = CLSAdapter()
        entities = adapter._extract_entities_from_stock_list(
            [{"name": "兆易创新", "StockID": "sh603986", "is_stib": False}]
        )
        assert entities[0].sector is None

    def test_source_has_no_english_hardcode(self):
        src_path = Path(__file__).resolve().parents[1] / "src" / "adapters" / "cls_adapter.py"
        text = src_path.read_text(encoding="utf-8")
        assert 'kwargs["sector"] = "STAR Market"' not in text
        assert 'kwargs["sector"] = "科创板"' in text


@pytest.mark.skipif(not _WHITELIST_PATH.exists(), reason="whitelist 为运行时缓存，可能不存在")
class TestWhitelistSectorChinese:
    """P1-2: 白名单 sector 字段全部为中文 canonical。"""

    def test_all_whitelist_sectors_are_chinese(self):
        data = json.loads(_WHITELIST_PATH.read_text(encoding="utf-8"))
        entries = data["tickers"] if isinstance(data, dict) else data
        sectors = {
            e.get("sector", "").strip() for e in entries if e.get("sector", "").strip()
        }
        assert sectors, "whitelist 应包含 sector 字段"
        for sector in sectors:
            assert not sector.isascii(), f"白名单 sector 必须为中文: {sector}"

    def test_whitelist_sectors_canonical_idempotent(self):
        data = json.loads(_WHITELIST_PATH.read_text(encoding="utf-8"))
        entries = data["tickers"] if isinstance(data, dict) else data
        for entry in entries:
            sector = entry.get("sector", "").strip()
            if sector:
                assert canonical_name(sector, "sector") == sector, (
                    f"白名单 sector 归一后应幂等: {sector}"
                )
