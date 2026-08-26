"""集成测试: EpisodeWriter + 真实 Neo4j + 真实 graphiti-core。

验收要求：
- 真实 Neo4j 连接
- 真实 graphiti-core add_episode 调用
- 不使用 mock
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv

# 加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

from neo4j import GraphDatabase

# 配置
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "newsengine2026")

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")

# 导入模块
from src.graphiti.entity_types import MACRO_ENTITY_TYPES, SYMBOL_ENTITY_TYPES
from src.graphiti.relation_types import EDGE_TYPES, DEFAULT_EDGE_TYPE_MAP
from src.graphiti.episode_writer import EpisodeWriter, WriteResult, BatchWriteResult
from src.adapters.models import NormalizedEpisode, EntityItem

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qa-integration")


@pytest.fixture(scope="module")
def neo4j_driver():
    """Neo4j driver fixture."""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    yield driver
    driver.close()


@pytest.fixture(scope="module")
async def graphiti_client():
    """Graphiti client fixture with real LLM + Embedder."""
    from graphiti_core import Graphiti
    from graphiti_core.llm_client import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig

    llm_config = LLMConfig(
        api_key=OPENAI_KEY,
        base_url=OPENAI_BASE,
        model=LLM_MODEL,
    )
    llm_client = OpenAIGenericClient(
        config=llm_config,
        structured_output_mode="json_object",
    )

    embed_config = OpenAIEmbedderConfig(
        api_key=OPENAI_KEY,
        base_url=OPENAI_BASE,
        embedding_model=EMBEDDING_MODEL,
        embedding_dim=1024,
    )
    embedder = OpenAIEmbedder(config=embed_config)

    # Cross encoder passthrough
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    class PassthroughCrossEncoder(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            return [(p, 0.5) for p in passages]

    cross_encoder = PassthroughCrossEncoder()

    graphiti = Graphiti(
        uri=NEO4J_URI,
        user=NEO4J_USER,
        password=NEO4J_PASSWORD,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )

    yield graphiti

    await graphiti.close()


class TestEntityTypesIntegration:
    """验收标准 1: entity_types.py 实现双注册表"""

    def test_import_entity_types(self):
        """验证 MACRO_ENTITY_TYPES 和 SYMBOL_ENTITY_TYPES 可导入"""
        assert len(MACRO_ENTITY_TYPES) == 6
        assert len(SYMBOL_ENTITY_TYPES) == 6
        assert "Stock" in SYMBOL_ENTITY_TYPES
        assert "Event" in MACRO_ENTITY_TYPES

    def test_stock_entity_pydantic_validation(self):
        """验证 StockEntity Pydantic 校验"""
        from src.graphiti.entity_types import StockEntity
        from pydantic import ValidationError

        # 合法实例化
        s = StockEntity(ticker="0700.HK", entity_name="腾讯控股", sector="互联网平台", exchange="HKEX")
        assert s.ticker == "0700.HK"

        # 缺少必填字段
        try:
            StockEntity(entity_name="测试", sector="互联网平台", exchange="HKEX")
            assert False, "应抛出 ValidationError"
        except ValidationError:
            pass

    def test_sector_entity_validation(self):
        """验证 SectorEntity Pydantic 校验"""
        from src.graphiti.entity_types import SectorEntity

        s = SectorEntity(entity_name="互联网平台")
        assert s.entity_name == "互联网平台"

    def test_country_entity_validation(self):
        """验证 CountryEntity Pydantic 校验"""
        from src.graphiti.entity_types import CountryEntity

        c = CountryEntity(entity_name="中国")
        assert c.entity_name == "中国"

    def test_policy_entity_validation(self):
        """验证 PolicyEntity Pydantic 校验"""
        from src.graphiti.entity_types import PolicyEntity

        p = PolicyEntity(entity_name="降息", type="monetary", status="rumor")
        assert p.status == "rumor"


class TestRelationTypesIntegration:
    """验收标准 2: relation_types.py 实现 6 种关系类型"""

    def test_import_edge_types(self):
        """验证 EDGE_TYPES 可导入且包含 6 个键"""
        assert len(EDGE_TYPES) == 6
        expected = {"AFFECTS", "CAUSED_BY", "MITIGATES", "BELONGS_TO", "LOCATED_IN", "RELATED_TO"}
        assert set(EDGE_TYPES.keys()) == expected

    def test_edge_type_map_correct(self):
        """验证 DEFAULT_EDGE_TYPE_MAP 正确"""
        assert len(DEFAULT_EDGE_TYPE_MAP) == 9
        assert ("Entity", "Stock") in DEFAULT_EDGE_TYPE_MAP
        assert ("Stock", "Sector") in DEFAULT_EDGE_TYPE_MAP
        assert ("Stock", "Country") in DEFAULT_EDGE_TYPE_MAP
        assert ("Entity", "Policy") in DEFAULT_EDGE_TYPE_MAP

    def test_affects_edge_validation(self):
        """验证 AffectsEdge Pydantic 校验"""
        from src.graphiti.relation_types import AffectsEdge

        e = AffectsEdge(fact="监管传闻影响股价", valid_at="2026-06-09")
        assert e.severity == "medium"  # 默认值


class TestEpisodeWriterIntegration:
    """验收标准 3: EpisodeWriter 类实现"""

    @pytest.mark.asyncio
    async def test_episode_writer_init(self, graphiti_client):
        """验证 EpisodeWriter 初始化"""
        writer = EpisodeWriter(
            graphiti=graphiti_client,
            entity_types=MACRO_ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=DEFAULT_EDGE_TYPE_MAP,
        )

        assert writer._entity_types == MACRO_ENTITY_TYPES
        assert writer._edge_types == EDGE_TYPES
        assert writer._seen_hashes == set()
        await writer.close()

    @pytest.mark.asyncio
    async def test_write_one_real_neo4j(self, graphiti_client, neo4j_driver):
        """验收标准 5: 完整集成测试 — 真实 Neo4j + graphiti-core"""
        writer = EpisodeWriter(
            graphiti=graphiti_client,
            entity_types=MACRO_ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=DEFAULT_EDGE_TYPE_MAP,
        )

        # 构造 NormalizedEpisode
        episode = NormalizedEpisode(
            name="QA验收测试-腾讯新闻",
            episode_body="腾讯控股(0700.HK)今日股价下跌2.3%，报收420.5港元。市场分析认为，这与近期互联网平台监管政策收紧有关。",
            content_hash="qa-test-hash-001",
            source_url="https://qa-test.example.com/news/001",
            source_description="港股市场收盘报道",
            source_type="gdelt_csv",
            valid_at=datetime.now(timezone.utc),
            severity="medium",
            entities=[
                EntityItem(type="stock", name="腾讯控股", ticker="0700.HK"),
                EntityItem(type="sector", name="互联网平台"),
            ],
        )

        # 真实写入
        result = await writer.write_one(episode)

        # 验证结果
        assert result.status == "ok"
        assert result.nodes_count >= 1  # 至少提取 1 个实体
        logger.info(f"✅ write_one 成功: nodes={result.nodes_count}, edges={result.edges_count}")

        # Neo4j 查询验证
        with neo4j_driver.session() as session:
            # 检查 Episodic 节点
            episodic_count = session.run("MATCH (e:Episodic) RETURN count(e) AS cnt").single()["cnt"]
            assert episodic_count >= 1

            # 检查是否有 Stock 类型实体（或 Entity）
            entity_count = session.run("MATCH (e:Entity) RETURN count(e) AS cnt").single()["cnt"]
            assert entity_count >= 1

            # 检查关系
            rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]
            assert rel_count >= 1

        logger.info(f"✅ Neo4j 验证: episodic={episodic_count}, entities={entity_count}, rels={rel_count}")

        # 清理测试数据
        with neo4j_driver.session() as session:
            session.run("MATCH (n:Episodic {name: 'QA验收测试-腾讯新闻'}) DETACH DELETE n")

        await writer.close()

    @pytest.mark.asyncio
    async def test_dedup_same_url(self, graphiti_client, neo4j_driver):
        """验收标准 6: 去重验证 — 相同 URL 不重复写入"""
        writer = EpisodeWriter(
            graphiti=graphiti_client,
            entity_types=MACRO_ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=DEFAULT_EDGE_TYPE_MAP,
        )

        episode1 = NormalizedEpisode(
            name="去重测试-001",
            episode_body="测试新闻内容",
            content_hash="dedup-test-hash-001",
            source_url="https://dedup.example.com/news/001",
            source_description="测试",
            source_type="gdelt_csv",
            valid_at=datetime.now(timezone.utc),
            severity="low",
            entities=[],
        )

        # 第一次写入
        result1 = await writer.write_one(episode1)
        assert result1.status == "ok"

        # 第二次相同 URL（但不同 content_hash）
        episode2 = NormalizedEpisode(
            name="去重测试-002",
            episode_body="测试新闻内容",
            content_hash="dedup-test-hash-002",  # 不同 hash
            source_url="https://dedup.example.com/news/001",  # 相同 URL
            source_description="测试",
            source_type="gdelt_csv",
            valid_at=datetime.now(timezone.utc),
            severity="low",
            entities=[],
        )

        result2 = await writer.write_one(episode2)
        assert result2.status == "skipped_duplicate"
        logger.info("✅ URL 去重验证通过")

        # 第三次相同 content_hash
        episode3 = NormalizedEpisode(
            name="去重测试-003",
            episode_body="测试新闻内容",
            content_hash="dedup-test-hash-001",  # 相同 hash
            source_url="https://dedup.example.com/news/002",  # 不同 URL
            source_description="测试",
            source_type="gdelt_csv",
            valid_at=datetime.now(timezone.utc),
            severity="low",
            entities=[],
        )

        result3 = await writer.write_one(episode3)
        assert result3.status == "skipped_duplicate"
        logger.info("✅ content_hash 去重验证通过")

        # 清理
        with neo4j_driver.session() as session:
            session.run("MATCH (n:Episodic) WHERE n.name STARTS WITH '去重测试' DETACH DELETE n")

        await writer.close()


class TestTickerSyncIntegration:
    """验收标准 4: TickerSync 类实现"""

    def test_import_ticker_sync(self):
        """验证 TickerSync 可导入"""
        from src.sync.ticker_sync import TickerSync
        assert TickerSync is not None

    def test_ticker_sync_init(self):
        """验证 TickerSync 初始化"""
        from src.sync.ticker_sync import TickerSync

        sync = TickerSync(
            synapse_url="http://localhost:8000",
            cache_path="data/ticker_cache.json",
        )

        assert sync._synapse_url == "http://localhost:8000"
        assert sync._cache_path == "data/ticker_cache.json"
        assert sync.get_whitelist() == []

    @pytest.mark.asyncio
    async def test_ticker_sync_fallback_no_cache(self):
        """验证降级: 无缓存 + SynapseEngine 不可达 → 空列表"""
        from src.sync.ticker_sync import TickerSync
        import tempfile
        import os

        # 使用临时缓存路径（不存在）
        temp_cache = tempfile.mktemp(suffix=".json")

        sync = TickerSync(
            synapse_url="http://localhost:9999",  # 不存在的端口
            cache_path=temp_cache,
            timeout_sec=2.0,
        )

        whitelist = await sync.refresh()

        # 应返回空列表
        assert whitelist == []
        assert sync.get_whitelist() == []
        logger.info("✅ TickerSync 降级验证通过（返回空列表）")

        # 清理临时文件
        if os.path.exists(temp_cache):
            os.remove(temp_cache)


class TestFullIntegration:
    """端到端集成测试"""

    @pytest.mark.asyncio
    async def test_gdelt_adapter_to_episode_writer(self, graphiti_client, neo4j_driver):
        """验收范围第 5 条: GDELT CSV → 适配器解析 → Episode 写入 → Neo4j 可见"""
        # 模拟 GDELT 适配器输出（直接构造 NormalizedEpisode）
        episode = NormalizedEpisode(
            name="GDELT模拟-港股新闻",
            episode_body="腾讯控股(0700.HK)股价下跌，市场担忧监管政策收紧。阿里巴巴(9988.HK)同步下跌。",
            content_hash="gdelt-test-hash-001",
            source_url="https://gdelt.example.com/event/001",
            source_description="GDELT GKG 事件",
            source_type="gdelt_csv",
            valid_at=datetime.now(timezone.utc),
            severity="high",
            entities=[
                EntityItem(type="stock", name="腾讯控股", ticker="0700.HK"),
                EntityItem(type="stock", name="阿里巴巴", ticker="9988.HK"),
            ],
        )

        writer = EpisodeWriter(
            graphiti=graphiti_client,
            entity_types=MACRO_ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=DEFAULT_EDGE_TYPE_MAP,
        )

        result = await writer.write_one(episode)

        assert result.status == "ok"
        assert result.nodes_count >= 1
        logger.info(f"✅ GDELT 模拟写入成功: nodes={result.nodes_count}, edges={result.edges_count}")

        # Neo4j Cypher 查询验证
        with neo4j_driver.session() as session:
            # 查询节点
            nodes = session.run("MATCH (e:Entity) RETURN e.name AS name, labels(e) AS labels LIMIT 10").data()
            logger.info(f"Neo4j 实体节点: {nodes}")

            # 查询关系
            rels = session.run(
                "MATCH ()-[r:RELATES_TO]->() WHERE r.name IS NOT NULL "
                "RETURN DISTINCT r.name AS edge_type LIMIT 10"
            ).data()
            logger.info(f"Neo4j 关系类型: {rels}")

            assert len(nodes) >= 1

        # 清理
        with neo4j_driver.session() as session:
            session.run("MATCH (n:Episodic {name: 'GDELT模拟-港股新闻'}) DETACH DELETE n")

        await writer.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])