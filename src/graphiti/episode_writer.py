"""EpisodeWriter — 将 NormalizedEpisode 列表写入 graphiti-core 知识图。

职责:
1. 去重: 按 content_hash + source_url 跳过已处理的 Episode
2. 格式转换: NormalizedEpisode → graphiti-core EpisodicNode 参数
3. 写入: 调用 Graphiti.add_episode()
4. content_scope 透传: 写入后通过 Cypher 设置 episode_metadata
5. 错误处理: LLM/Neo4j 失败时的重试与降级
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from neo4j import Driver
from pydantic import BaseModel, Field

from graphiti_core.nodes import EpisodeType

from src.adapters.models import NormalizedEpisode, build_entity_suffix
from src.graphiti.entity_types import SYMBOL_ENTITY_TYPES
from src.graphiti.relation_types import EDGE_TYPES, DEFAULT_EDGE_TYPE_MAP
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "WriteResult",
    "BatchWriteResult",
    "EpisodeWriter",
]


# ── 结果数据结构 ────────────────────────────────────────────────────────


class WriteResult(BaseModel):
    """单个 Episode 写入结果。"""

    episode_name: str = Field(..., description="NormalizedEpisode.name")
    status: str = Field(
        ..., description="'ok' | 'skipped_duplicate' | 'error'"
    )
    nodes_count: int = Field(
        default=0, description="提取的实体节点数"
    )
    edges_count: int = Field(
        default=0, description="提取的关系边数"
    )
    error: str | None = Field(
        default=None, description="错误信息（仅 status='error' 时非空）"
    )
    duration_ms: float = Field(
        default=0.0, description="写入耗时（毫秒）"
    )


class BatchWriteResult(BaseModel):
    """批量写入结果汇总。"""

    total: int = Field(..., description="输入总数")
    ok: int = Field(..., description="成功写入数")
    skipped: int = Field(..., description="去重跳过数")
    error: int = Field(..., description="失败数")
    results: list[WriteResult] = Field(
        default_factory=list, description="详细结果列表"
    )
    duration_ms: float = Field(
        default=0.0, description="批量写入总耗时（毫秒）"
    )


# ── EpisodeWriter ──────────────────────────────────────────────────────


class EpisodeWriter:
    """将 NormalizedEpisode 列表写入 graphiti-core 知识图。"""

    def __init__(
        self,
        graphiti: Any,
        neo4j_driver: Driver | None = None,
        entity_types: dict[str, type[BaseModel]] | None = None,
        edge_types: dict[str, type[BaseModel]] | None = None,
        edge_type_map: dict[tuple[str, str], list[str]] | None = None,
        max_retries: int = 3,
        retry_base_sec: float = 2.0,
    ) -> None:
        self._graphiti = graphiti
        self._neo4j_driver = neo4j_driver
        self._entity_types = entity_types or SYMBOL_ENTITY_TYPES
        self._edge_types = edge_types or EDGE_TYPES
        self._edge_type_map = edge_type_map or DEFAULT_EDGE_TYPE_MAP
        self._max_retries = max_retries
        self._retry_base_sec = retry_base_sec

        # 实例级别去重缓存（不跨运行周期持久化）
        self._seen_hashes: set[str] = set()
        self._seen_urls: set[str] = set()

    # ── 公开接口 ────────────────────────────────────────────────────

    async def write_one(self, episode: NormalizedEpisode) -> WriteResult:
        """写入单个 NormalizedEpisode 到 graphiti-core。

        返回 WriteResult，不会抛出未捕获异常。
        """
        start = time.monotonic()

        # 0. 去重检查
        if episode.content_hash in self._seen_hashes:
            return WriteResult(
                episode_name=episode.name,
                status="skipped_duplicate",
                duration_ms=(time.monotonic() - start) * 1000,
            )
        if episode.source_url and episode.source_url in self._seen_urls:
            return WriteResult(
                episode_name=episode.name,
                status="skipped_duplicate",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        # 1. 构造扩展 episode_body
        extended_body = _build_extended_body(episode)

        # 2. 提取 content_scope 用于写入后透传
        content_scope = episode.metadata.get("content_scope") if episode.metadata else None

        # 3. 尝试写入（含重试）
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                result = await self._graphiti.add_episode(
                    name=episode.name,
                    episode_body=extended_body,
                    source_description=episode.source_description,
                    reference_time=episode.valid_at,
                    source=EpisodeType.text,
                    entity_types=self._entity_types,
                    edge_types=self._edge_types,
                    edge_type_map=self._edge_type_map,
                )

                # 写入成功
                self._seen_hashes.add(episode.content_hash)
                if episode.source_url:
                    self._seen_urls.add(episode.source_url)

                # 4. content_scope 透传：通过 Cypher 设置 episode_metadata
                if content_scope and self._neo4j_driver:
                    self._set_episode_metadata(
                        episode_uuid=result.episode.uuid,
                        content_scope=content_scope,
                        metadata=episode.metadata,
                    )

                nodes_count = len(result.nodes) if hasattr(result, "nodes") else 0
                edges_count = len(result.edges) if hasattr(result, "edges") else 0

                return WriteResult(
                    episode_name=episode.name,
                    status="ok",
                    nodes_count=nodes_count,
                    edges_count=edges_count,
                    duration_ms=(time.monotonic() - start) * 1000,
                )

            except Exception as exc:
                last_error = exc
                if attempt < self._max_retries:
                    delay = self._retry_base_sec * (2 ** (attempt - 1))
                    logger.warning(
                        "write_one attempt %d/%d failed for '%s': %s. "
                        "Retrying in %.1fs...",
                        attempt,
                        self._max_retries,
                        episode.name,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "write_one failed after %d attempts for '%s': %s",
                        self._max_retries,
                        episode.name,
                        exc,
                        exc_info=True,
                    )

        # 所有重试均失败
        return WriteResult(
            episode_name=episode.name,
            status="error",
            error=f"{type(last_error).__name__}: {last_error}",
            duration_ms=(time.monotonic() - start) * 1000,
        )

    async def write_batch(
        self, episodes: list[NormalizedEpisode]
    ) -> BatchWriteResult:
        """批量写入 NormalizedEpisode 列表。

        graphiti-core 要求串行调用 add_episode()，
        因此按顺序逐个写入，不并发。
        """
        start = time.monotonic()
        results: list[WriteResult] = []

        for episode in episodes:
            result = await self.write_one(episode)
            results.append(result)

        ok_count = sum(1 for r in results if r.status == "ok")
        skipped_count = sum(1 for r in results if r.status == "skipped_duplicate")
        error_count = sum(1 for r in results if r.status == "error")

        return BatchWriteResult(
            total=len(episodes),
            ok=ok_count,
            skipped=skipped_count,
            error=error_count,
            results=results,
            duration_ms=(time.monotonic() - start) * 1000,
        )

    async def close(self) -> None:
        """清理资源。

        目前 graphiti-core 没有需要手动关闭的资源，
        此方法预留用于后续扩展。
        """
        # 如果 graphiti 有 close 方法则调用
        close_method = getattr(self._graphiti, "close", None)
        if callable(close_method):
            await close_method() if asyncio.iscoroutinefunction(close_method) else close_method()

    # ── 内部方法 ────────────────────────────────────────────────────

    @property
    def seen_count(self) -> int:
        """当前实例已处理的唯一 Episode 数量。"""
        return len(self._seen_hashes)

    def _set_episode_metadata(
        self,
        episode_uuid: str,
        content_scope: str,
        metadata: dict[str, Any],
    ) -> None:
        """通过 Cypher 将 content_scope 写入 EpisodicNode.episode_metadata。

        Graphiti SDK 的 add_episode() 不直接支持 episode_metadata 参数，
        因此写入后通过 Cypher 更新节点属性。

        注意：Neo4j 不支持 dict 类型属性，metadata dict 会被序列化为 JSON 字符串。
        """
        try:
            # Neo4j 不支持 dict 类型属性，序列化为 JSON 字符串
            episode_metadata_json = json.dumps(dict(metadata), ensure_ascii=False)

            with self._neo4j_driver.session() as session:
                session.run(
                    """
                    MATCH (ep:Episodic {uuid: $uuid})
                    SET ep.episode_metadata = $metadata_json
                    """,
                    uuid=episode_uuid,
                    metadata_json=episode_metadata_json,
                )
            logger.debug(
                "Set episode_metadata for %s: content_scope=%s",
                episode_uuid[:12],
                content_scope,
            )
        except Exception as exc:
            logger.warning(
                "Failed to set episode_metadata for %s: %s",
                episode_uuid[:12],
                exc,
            )


def _build_extended_body(episode: NormalizedEpisode) -> str:
    """将 pre-extracted entities 附加到 episode_body 末尾。

    这帮助 LLM 更准确地提取实体（已有提示），减少错误分类。
    """
    suffix = build_entity_suffix(episode.entities)
    if suffix:
        return episode.episode_body + suffix
    return episode.episode_body
