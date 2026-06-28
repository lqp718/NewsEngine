"""百炼 Embedding 客户端 — 兼容 text-embedding-v4 单次 10 条上限。

Graphiti SDK 的 OpenAIEmbedder.create_batch() 没有分片逻辑，
百炼 v4 API 单次请求最多 10 条 input，超限返回 400。
本客户端继承 EmbedderClient，在 create_batch 中按 batch_size=10 分片。
"""

from __future__ import annotations

from collections.abc import Iterable

from graphiti_core.embedder.client import EmbedderClient, EmbedderConfig
from openai import AsyncOpenAI


class BailianEmbedder(EmbedderClient):
    """百炼 embedding 客户端，兼容 batch_size <= 10 的限制。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        embedding_dim: int = 1024,
    ):
        self.config = EmbedderConfig(embedding_dim=embedding_dim)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.batch_size = 10  # 百炼 v4 限制

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        result = await self.client.embeddings.create(
            input=input_data, model=self.model
        )
        return result.data[0].embedding[: self.config.embedding_dim]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        all_embeddings: list[list[float]] = []
        for i in range(0, len(input_data_list), self.batch_size):
            chunk = input_data_list[i : i + self.batch_size]
            result = await self.client.embeddings.create(
                input=chunk, model=self.model
            )
            all_embeddings.extend(
                e.embedding[: self.config.embedding_dim] for e in result.data
            )
        return all_embeddings
