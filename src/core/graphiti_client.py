"""Graphiti SDK client management - provides a factory function for Graphiti instances.

Since Graphiti SDK's add_episode() method is not thread-safe, we use a factory function
rather than a singleton to allow each caller to manage their own instance lifecycle.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from graphiti_core import Graphiti
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.cross_encoder.bge_reranker_client import BGERerankerClient

if TYPE_CHECKING:
    from graphiti_core.driver.driver import GraphDriver

from .bailian_embedder import BailianEmbedder
from .config import get_settings
from .neo4j_client import get_neo4j_driver


def create_graphiti(graph_driver: GraphDriver | None = None) -> Graphiti:
    """Create a new Graphiti instance with the configured LLM and Embedder clients.

    Args:
        graph_driver: Optional GraphDriver instance. If provided, Graphiti will use this
            driver instead of creating its own from uri/user/password credentials.
            This allows callers to share a driver with existing connection pools.
            When None, Graphiti creates an internal driver using configured credentials.

    Returns:
        A new Graphiti instance configured with the current settings.
    """
    settings = get_settings()

    # LLM: 根据 graphiti_llm_provider 配置选择
    # - "gemini": 原生 Gemini API（免费但不稳定，偶发 503/JSON 解析失败）
    # - "openai": 百炼 OpenAI 兼容接口（稳定，收费）
    if settings.graphiti_llm_provider == "gemini":
        from graphiti_core.llm_client.gemini_client import GeminiClient
        llm_client = GeminiClient(
            config=LLMConfig(
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            ),
        )
    else:  # openai (百炼)
        from .bailian_llm_client import BailianOpenAIClient
        llm_client = BailianOpenAIClient(
            config=LLMConfig(
                api_key=settings.bailian_api_key,
                model=settings.llm_model,
                base_url=settings.openai_base_url,
            ),
            structured_output_mode='json_object',  # 百炼不支持 json_schema
        )

    # Embedder: 百炼 text-embedding-v4 (keep existing Bailian Embedder with 10-item batch limit)
    embedder_client = BailianEmbedder(
        api_key=settings.bailian_api_key,
        base_url=settings.openai_base_url,
        model=settings.embedding_model,
        embedding_dim=1024,
    )

    # Reranker: BGE local model (GPU available → sub-100ms)
    # No API key needed, no per-call cost
    reranker_client = BGERerankerClient()

    # Create and return Graphiti instance
    return Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        llm_client=llm_client,
        embedder=embedder_client,
        cross_encoder=reranker_client,
        graph_driver=graph_driver,
    )


__all__ = ["create_graphiti"]
