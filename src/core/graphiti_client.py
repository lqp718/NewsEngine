"""Graphiti SDK client management - provides a factory function for Graphiti instances.

Since Graphiti SDK's add_episode() method is not thread-safe, we use a factory function
rather than a singleton to allow each caller to manage their own instance lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from graphiti_core import Graphiti
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.llm_client.client import LLMConfig

if TYPE_CHECKING:
    from graphiti_core.driver.driver import GraphDriver

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

    # Configure LLM client with 百炼 API
    llm_config = LLMConfig(
        api_key=settings.bailian_api_key,
        base_url=settings.openai_base_url,
        model=settings.llm_model,
    )
    llm_client = OpenAIGenericClient(
        config=llm_config,
        structured_output_mode='json_object',  # Dashscope 不支持 json_schema constrained decoding
    )

    # Configure Embedder client with 百炼 API
    embedder_config = OpenAIEmbedderConfig(
        api_key=settings.bailian_api_key,
        base_url=settings.openai_base_url,
        embedding_model=settings.embedding_model,
    )
    embedder_client = OpenAIEmbedder(config=embedder_config)

    # Create and return Graphiti instance
    return Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        llm_client=llm_client,
        embedder=embedder_client,
        graph_driver=graph_driver,
    )


__all__ = ["create_graphiti"]
