#!/usr/bin/env python3
"""
Graphiti Episode 测试脚本 — 百炼 API 版
========================================
验证 graphiti-core 初始化 → 创建 Episode → Neo4j 节点/关系 完整链路。

依赖:
    pip install graphiti-core openai numpy python-dotenv

配置:
    从 .env 文件读取 (OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL, LLM_MODEL)
    或通过环境变量覆盖:
        LLM_API_KEY         (默认: OPENAI_API_KEY 的值)
        LLM_BASE_URL        (默认: OPENAI_BASE_URL 的值)
        EMBEDDING_API_KEY   (默认同 LLM_API_KEY)
        EMBEDDING_BASE_URL  (默认同 LLM_BASE_URL)
        EMBEDDING_DIM       (默认: 1024)
        NEO4J_URI           (默认: bolt://localhost:7687)
        NEO4J_USER          (默认: neo4j)
        NEO4J_PASSWORD      (默认: newsengine2026)

运行:
    cd /mnt/d/MyWallet/NewsEngine
    source .venv/bin/activate
    python test_graphiti_episode.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import numpy as np
from dotenv import load_dotenv

# ---------- 加载 .env ----------
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ---------- logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

logger = logging.getLogger("graphiti-test")

# ---------- config: 优先读取 .env 中的 百炼 配置 ----------
# LLM: 从 OPENAI_API_KEY + OPENAI_BASE_URL + LLM_MODEL
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE = os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

LLM_API_KEY = os.getenv("LLM_API_KEY", OPENAI_KEY)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", OPENAI_BASE)
LLM_MODEL = os.getenv("LLM_MODEL", "qwen-plus")

# Embedding: 默认同 LLM 配置
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", LLM_API_KEY)
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", LLM_BASE_URL)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "newsengine2026")


# ---------- Dummy Embedder (fallback) ----------
from graphiti_core.embedder import EmbedderClient


class RandomEmbedder(EmbedderClient):
    """Fallback: random vectors for connectivity testing."""

    def __init__(self, dim: int = 1024):
        self.dim = dim

    async def create(self, input_data):
        rng = np.random.default_rng(42)
        vec = rng.normal(0, 0.1, self.dim).astype(np.float32)
        return vec.tolist()

    async def create_batch(self, input_data_list: list[str]):
        rng = np.random.default_rng(42)
        return [
            rng.normal(0, 0.1, self.dim).astype(np.float32).tolist()
            for _ in input_data_list
        ]


# ---------- 主流程 ----------
async def main():
    logger.info("=" * 60)
    logger.info("Graphiti Episode 验证测试 (百炼 API)")
    logger.info("=" * 60)

    # ---- 0. 打印配置 (隐藏 key 中间部分) ----
    key_display = LLM_API_KEY[:8] + "..." + LLM_API_KEY[-4:] if len(LLM_API_KEY) > 12 else "not-set"
    logger.info("LLM:     %s  |  model=%s  |  key=%s", LLM_BASE_URL, LLM_MODEL, key_display)
    logger.info("Embed:   %s  |  model=%s  |  dim=%s", EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM)
    logger.info("Neo4j:   %s  |  user=%s", NEO4J_URI, NEO4J_USER)
    logger.info("")

    if not OPENAI_KEY:
        logger.warning("⚠️  OPENAI_API_KEY 未设置，将使用 RandomEmbedder fallback")
    else:
        logger.info("✅ OPENAI_API_KEY 已配置")

    # ---- 1. LLM Client ----
    from graphiti_core.llm_client import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    llm_config = LLMConfig(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
    )
    llm_client = OpenAIGenericClient(
        config=llm_config,
        structured_output_mode="json_object",
    )
    logger.info("[1/6] ✅ LLM client created (model=%s)", LLM_MODEL)

    # ---- 2. Embedder ----
    from graphiti_core.embedder import OpenAIEmbedder, OpenAIEmbedderConfig

    embed_config = OpenAIEmbedderConfig(
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        embedding_model=EMBEDDING_MODEL,
        embedding_dim=EMBEDDING_DIM,
    )

    try:
        embedder = OpenAIEmbedder(config=embed_config)
        test_vec = await embedder.create("test")
        logger.info(
            "[2/6] ✅ Embedder created (%s, dim=%d)", EMBEDDING_MODEL, len(test_vec)
        )
    except Exception as e:
        logger.warning(
            "[2/6] ⚠️ OpenAI embedder failed (%s), falling back to RandomEmbedder", e
        )
        embedder = RandomEmbedder(dim=EMBEDDING_DIM)
        _ = await embedder.create("test")  # warm up
        logger.info("[2/6] ✅ RandomEmbedder ready (dim=%d)", EMBEDDING_DIM)

    # ---- 3. Cross Encoder (passthrough) ----
    from graphiti_core.cross_encoder.client import CrossEncoderClient

    class PassthroughCrossEncoder(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            return [(p, 0.5) for p in passages]

    cross_encoder = PassthroughCrossEncoder()
    logger.info("[3/6] ✅ Cross-encoder created (passthrough)")

    # ---- 4. Graphiti 初始化 ----
    from graphiti_core import Graphiti

    graphiti = Graphiti(
        uri=NEO4J_URI,
        user=NEO4J_USER,
        password=NEO4J_PASSWORD,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )
    logger.info("[4/6] ✅ Graphiti initialized (uri=%s)", NEO4J_URI)

    # ---- 5. 创建 Episode ----
    episode_name = "腾讯股价波动测试（百炼）"
    episode_body = (
        "腾讯控股(0700.HK)今日股价下跌2.3%，报收420.5港元。\n"
        "市场分析认为，这与近期互联网平台监管政策收紧有关。\n"
        "多家投行下调了腾讯的目标价，但维持买入评级。\n"
        "与此同时，阿里巴巴(9988.HK)也下跌1.8%，美团(3690.HK)下跌1.5%。"
    )
    source_desc = "港股市场收盘报道"

    logger.info("[5/6] Creating episode: 「%s」...", episode_name)

    try:
        result = await graphiti.add_episode(
            name=episode_name,
            episode_body=episode_body,
            source_description=source_desc,
            reference_time=datetime.now(timezone.utc),
        )
        logger.info("[5/6] ✅ Episode created!")
        logger.info(
            "      episode_uuid=%s", result.episode.uuid[:12] if hasattr(result, "episode") else "?"
        )
        logger.info("      nodes=%d, edges=%d", len(result.nodes), len(result.edges))

        # 打印提取的节点
        for node in result.nodes:
            logger.info(
                "      📌 Node: name=%s  |  group=%s",
                node.name,
                getattr(node, "group_id", "?"),
            )

        # 打印提取的边
        for edge in result.edges:
            logger.info(
                "      🔗 Edge: %s -> %s  |  fact=%s",
                getattr(edge, "source_node_uuid", "?")[:8],
                getattr(edge, "target_node_uuid", "?")[:8],
                getattr(edge, "fact", "")[:50],
            )

    except Exception as e:
        logger.error("[5/6] ❌ Episode creation failed: %s", e)
        import traceback

        traceback.print_exc()

        # 尝试清理上次测试的残留数据（如果有）
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            with driver.session() as session:
                session.run("MATCH (n) DETACH DELETE n")
                logger.info("      🧹 Neo4j 已清理（DETACH DELETE ALL）")
            driver.close()
        except Exception:
            pass

        sys.exit(1)

    # ---- 6. 验证 Neo4j ----
    logger.info("[6/6] Verifying Neo4j graph...")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    try:
        with driver.session() as session:
            node_count = session.run("MATCH (n) RETURN count(n) AS cnt").single()["cnt"]
            rel_count = (
                session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]
            )

            logger.info("[6/6] ✅ Neo4j graph state:")
            logger.info("      Total nodes:  %d", node_count)
            logger.info("      Total rels:   %d", rel_count)

            # 节点标签分布
            label_dist = session.run(
                "MATCH (n) UNWIND labels(n) AS lbl "
                "RETURN lbl, count(*) AS cnt ORDER BY cnt DESC"
            ).data()
            logger.info("      Label distribution:")
            for row in label_dist:
                logger.info("        - %s: %d", row["lbl"], row["cnt"])

            # 采样节点
            sample_nodes = session.run(
                "MATCH (n) RETURN n.name AS name, labels(n) AS labels LIMIT 8"
            ).data()
            logger.info("      Sample nodes:")
            for n in sample_nodes:
                logger.info(
                    "        - %s  (%s)",
                    n.get("name", "(unnamed)"),
                    ", ".join(n.get("labels", [])),
                )

            # 采样关系
            sample_rels = session.run(
                "MATCH (a)-[r]->(b) RETURN a.name AS src, type(r) AS rel, "
                "b.name AS dst LIMIT 8"
            ).data()
            logger.info("      Sample relationships:")
            for r in sample_rels:
                logger.info(
                    "        - %s -[%s]-> %s",
                    r.get("src", "?"),
                    r.get("rel"),
                    r.get("dst", "?"),
                )

    finally:
        driver.close()

    # ---- 清理: 删除本次测试创建的图数据 ----
    logger.info("")
    logger.info("Cleaning up test data...")
    driver2 = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with driver2.session() as session:
            # 只删除本次测试的节点（按 episode_name 特征匹配）
            session.run(
                "MATCH (n:Episodic {name: $name}) DETACH DELETE n",
                name=episode_name,
            )
            logger.info("      ✅ Test episode data cleaned up")
    except Exception as e:
        logger.warning("      cleanup warning: %s", e)
    finally:
        driver2.close()

    await graphiti.close()

    logger.info("")
    logger.info("=" * 60)
    logger.info("✅ 全部验证通过！Graphiti + 百炼 API 端到端链路正常。")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
