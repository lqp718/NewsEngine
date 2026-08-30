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
import random
import re
import time
from datetime import timedelta
from typing import Any

from neo4j import Driver
from pydantic import BaseModel, Field

from graphiti_core.nodes import EpisodeType

from src.adapters.models import NormalizedEpisode
from src.graphiti.entity_types import SYMBOL_ENTITY_TYPES
from src.graphiti.relation_types import EDGE_TYPES, DEFAULT_EDGE_TYPE_MAP
from src.utils.entity_canonical import canonical_name
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── 控制字符过滤钩子 ────────────────────────────────────────────────────
# 写入前移除字符串中的 C0 控制字符（\x00-\x08, \x0B, \x0C, \x0E-\x1F），
# 保留 \n(0x0A) \r(0x0D) \t(0x09)。防止污染 Neo4j 属性（与
# scripts/clean_control_chars.py 共用同一规则）。
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _clean_text(text: str) -> str:
    """移除控制字符（保留 \n \r \t）。非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    return _CONTROL_CHAR_RE.sub("", text)


# ── 全局并发限制 / 429 退避 / 熔断（跨所有 EpisodeWriter 实例与 Tier 共享） ──
#
# 背景: 多个 Tier 共享同一 Gemini 4M tokens/min 配额。并发请求过多会触发
#       429，API 要求 37-44s 后退避；若退避过短（2s/4s）则重试必败、吞吐归零。
# 方案: 1) 模块级 Semaphore 限制并发 LLM 调用；
#       2) 429 时尊重 API 返回的 retryDelay（下限 37s + jitter）；
#       3) 连续 N 次 429 触发熔断，冷却整个写入队列。

try:  # graphiti-core 的 LLM 客户端统一抛出该异常（errors 模块为纯 Python，无可选依赖）
    from graphiti_core.llm_client.errors import RateLimitError  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - graphiti 不可用时退化为按类名识别
    RateLimitError = None  # type: ignore[assignment,misc]

# 全局并发信号量: 所有 EpisodeWriter 实例 / Tier 共享，限制同时进行的 LLM 调用数
# 并发参数配置化: 由 settings（.env）提供，避免硬编码
from src.core.config import get_settings

_settings = get_settings()
_LLM_SEMAPHORE = asyncio.Semaphore(_settings.episode_semaphore)

# 429 退避参数: 尊重 API retryDelay，但不得低于 _MIN_429_BACKOFF_SEC（配额恢复所需）
_MIN_429_BACKOFF_SEC = _settings.min_429_backoff_sec
_MAX_429_JITTER_SEC = 2.0

# 熔断参数: 连续 _CIRCUIT_MAX_CONSECUTIVE_429 次 429 后，冷却整个队列
_CIRCUIT_MAX_CONSECUTIVE_429 = _settings.circuit_max_consecutive_429
_CIRCUIT_COOLDOWN_SEC = _settings.circuit_cooldown_sec
_CIRCUIT_CONSECUTIVE_429 = 0  # 全局连续 429 计数（单线程事件循环，读写原子）
_CIRCUIT_OPEN_UNTIL = 0.0  # 熔断打开截止时刻（time.monotonic() 基准）

__all__ = [
    "WriteResult",
    "BatchWriteResult",
    "EpisodeWriter",
    "CORE_EDGE_TYPES",
    "normalize_edge_type",
    "ExposedToEdge",
    "InvolvesEdge",
    "HappenedInEdge",
]


# ── 核心关系类型 / 边类型归一 ────────────────────────────────────────────
#
# 背景: 历史数据中边 name 属性碎片化（118 种），应在边生成阶段就收敛到
#       核心关系类型集。graphiti-core 落库时关系类型统一为 :RELATES_TO，
#       语义类型存于 name 属性（如 "AFFECTS"）；这里做两层收敛:
#       1) 边生成前置: 归一 edge_types / edge_type_map，LLM 只产出核心类型
#       2) 写入后兜底: 本次写入边中偏离核心集的 name 就地归一（uuid 不变）


CORE_EDGE_TYPES = {
    "AFFECTS",      # 事件影响股票/行业
    "RELATES_TO",   # 通用关联
    "PART_OF",      # 行业/地区归属
    "EXPOSED_TO",   # 风险暴露
    "TRIGGERS",     # 因果触发
    "HAPPENED_IN",  # 地区关联
    "INVOLVES",     # 主体参与
}
"""核心关系类型集 — 所有边语义类型收敛到该集合。"""


class ExposedToEdge(BaseModel):
    """EXPOSED_TO 关系: 风险暴露 — 主体/事件暴露于某项风险。"""

    fact: str = Field(..., description="描述风险暴露关系的事实，使用中文")
    valid_at: str | None = Field(
        default=None, description="关系成立日期 (YYYY-MM-DD)"
    )


class InvolvesEdge(BaseModel):
    """INVOLVES 关系: 主体参与 — 事件/行为与主体的参与关系。"""

    fact: str = Field(..., description="描述参与关系的事实，使用中文")
    valid_at: str | None = Field(
        default=None, description="关系成立日期 (YYYY-MM-DD)"
    )


class HappenedInEdge(BaseModel):
    """HAPPENED_IN 关系: 地区关联 — 事件发生或关联于某地区。"""

    fact: str = Field(..., description="描述事件与地区关联的事实，使用中文")
    valid_at: str | None = Field(
        default=None, description="关系成立日期 (YYYY-MM-DD)"
    )


def normalize_edge_type(edge_type: str | None) -> str:
    """把任意边类型名称归一到核心关系类型集。

    按前缀匹配（大小写不敏感），顺序即优先级。要点:
    - "INVOLVES" 必须在 "IN" 前缀规则之前匹配，否则会被误归为 HAPPENED_IN
    - "IN" 前缀规则已收窄为精确 "IN" / "IN_" 前缀 + "HAPPENED"：原规则
      startswith("IN") 过于宽泛（"INVOLVES" 虽被前置的 INVOLV 规则拦住，
      但 "INFLUENCES" 等其他 IN* 名称仍会被误归为 HAPPENED_IN）
    - 补齐了任务给定规则的几个盲区: "CAUSED_BY" / "TRIGGERED_BY"
      （startswith("CAUSES") / startswith("TRIGGERS") 匹配不到）→ 用前缀
      "CAUSED" / "TRIGGER" 覆盖；MITIGATES（缓解本质是影响的一种）→ AFFECTS；
      LOCATED_IN（地区归属）→ PART_OF
    - 新增归属/任职映射: SUBSIDIARY/OWNED_BY/PARENT_OF → PART_OF；
      CEO_OF/EMPLOYED_BY/WORKS_FOR/CHAIR*/PRESIDENT* → INVOLVES
      （"CHAIR" 前缀同时覆盖 CHAIRMAN_OF / CHAIRPERSON / CHAIR_OF，与写时/
      迁移规则对称）
    - TRACKS/REPORTS/REPORTED_BY/STATES 属通用关联，由默认兜底保持
      RELATES_TO（不单设规则，显式注释以免后续误改）
    """
    if not edge_type:
        return "RELATES_TO"
    edge_upper = edge_type.upper().strip()
    if edge_upper.startswith("RELATES_TO"):
        return "RELATES_TO"
    # "TRIGGER" 前缀同时覆盖 TRIGGERS / TRIGGERED_BY / TRIGGER（盲区修复）
    if edge_upper.startswith(("CAUSES", "CAUSED", "TRIGGER")):
        return "TRIGGERS"
    if edge_upper.startswith(("AFFECTS", "IMPACTS", "INFLUENC", "MITIGATES")):
        return "AFFECTS"
    # 主体参与/任职: 除 INVOLV/ACTOR 外，CEO/受雇/任职/董事长/总裁等任职类
    # 关系本质是主体参与，归入 INVOLVES（必须先于 HAPPENED_IN 规则匹配）。
    # "CHAIR" 覆盖 CHAIRMAN_OF/CHAIRPERSON/CHAIR_OF；"PRESIDENT" 覆盖
    # PRESIDENT_OF（盲区修复，消除写时/迁移不对称）
    if edge_upper.startswith(
        ("INVOLV", "ACTOR", "CEO", "EMPLOYED", "WORKS_FOR", "CHAIR", "PRESIDENT")
    ):
        return "INVOLVES"
    if edge_upper.startswith("EXPOSED"):
        return "EXPOSED_TO"
    # 归属关系: 子公司/被持有/母子公司 → PART_OF
    if edge_upper.startswith(("SUBSIDIARY", "OWNED_BY", "PARENT_OF")):
        return "PART_OF"
    # 地区关联: 仅精确 "IN" / "IN_*" / "HAPPENED*"（原 startswith("IN") 过宽）
    if edge_upper == "IN" or edge_upper.startswith(("IN_", "HAPPENED")):
        return "HAPPENED_IN"
    if edge_upper.startswith(("PART", "BELONGS", "LOCATED")):
        return "PART_OF"
    # TRACKS / REPORTS / REPORTED_BY / STATES 等通用关联 → 保持默认兜底
    return "RELATES_TO"  # 默认兜底


def _normalized_edge_types(
    edge_types: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """把 edge_types schema 的键收敛到核心集（边生成阶段的类型约束）。

    同一核心类型对应多个遗留类型时复用第一个 model（字段兼容）。
    核心集中缺少 schema 的类型（EXPOSED_TO / INVOLVES / HAPPENED_IN）
    由本模块定义的最小 model 补齐，保证 7 种核心类型都可被 LLM 产出。
    """
    normalized: dict[str, type[BaseModel]] = {}
    for name, model in edge_types.items():
        normalized.setdefault(normalize_edge_type(name), model)
    normalized.setdefault("EXPOSED_TO", ExposedToEdge)
    normalized.setdefault("INVOLVES", InvolvesEdge)
    normalized.setdefault("HAPPENED_IN", HappenedInEdge)
    return normalized


def _normalized_edge_type_map(
    edge_type_map: dict[tuple[str, str], list[str]],
) -> dict[tuple[str, str], list[str]]:
    """把 edge_type_map 中允许的关系类型收敛到核心集。

    通用 (Entity, Entity) 对补充全部核心类型，保证核心类型在通用场景可用；
    其余实体对保持原有约束（名称归一）。
    """
    normalized: dict[tuple[str, str], list[str]] = {}
    for pair, names in edge_type_map.items():
        normalized[pair] = list(dict.fromkeys(normalize_edge_type(n) for n in names))
    entity_pair = ("Entity", "Entity")
    normalized.setdefault(entity_pair, [])
    for core in CORE_EDGE_TYPES:
        if core not in normalized[entity_pair]:
            normalized[entity_pair].append(core)
    return normalized


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
        whitelist: list[dict] | None = None,
        max_retries: int = 5,
        retry_base_sec: float = 2.0,
    ) -> None:
        self._graphiti = graphiti
        self._neo4j_driver = neo4j_driver
        self._entity_types = entity_types or SYMBOL_ENTITY_TYPES
        self._edge_types = edge_types or EDGE_TYPES
        self._edge_type_map = edge_type_map or DEFAULT_EDGE_TYPE_MAP
        self._whitelist = whitelist or []
        self._max_retries = max_retries
        self._retry_base_sec = retry_base_sec

        # 实例级别去重缓存（不跨运行周期持久化）
        self._seen_hashes: set[str] = set()
        self._seen_urls: set[str] = set()

    def set_whitelist(self, whitelist: list[dict] | None = None) -> None:
        """更新 ticker 白名单（每个 ingestion cycle 刷新）。

        SynapseEngine 可随时通过 POST /api/tickers/whitelist 推送新白名单，
        scheduler 每个 cycle 重新加载缓存并调用本方法同步到 writer。
        """
        self._whitelist = whitelist or []

        # 实例级别去重缓存（不跨运行周期持久化）
        self._seen_hashes: set[str] = set()
        self._seen_urls: set[str] = set()

    # ── 公开接口 ────────────────────────────────────────────────────

    async def write_one(self, episode: NormalizedEpisode) -> WriteResult:
        """写入单个 NormalizedEpisode 到 graphiti-core。

        返回 WriteResult，不会抛出未捕获异常。
        """
        start = time.monotonic()

        # -1. 写入前清洗控制字符（保留 \n \r \t），防止污染 Neo4j
        #     注意：graphiti-core 的 EpisodicNode.summary / Relationship.fact 由
        #     episode_body 派生，因此在源头清洗即可覆盖所有下游属性。
        episode.episode_body = _clean_text(episode.episode_body)
        episode.name = _clean_text(episode.name)
        episode.source_description = _clean_text(episode.source_description)

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

        # 2b. 边类型收敛（边生成阶段）: 归一 edge_types / edge_type_map，
        #     让 LLM 只产出核心关系类型（~7 种），从源头消除类型碎片化
        edge_types = _normalized_edge_types(self._edge_types)
        edge_type_map = _normalized_edge_type_map(self._edge_type_map)

        # 3. 尝试写入（含重试）
        #    429 限流: 尊重 API 返回的 retryDelay（下限 37s + jitter）；
        #    连续 429 触发全局熔断 → 冷却整个队列。
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            # 熔断打开时，整个队列在此冷却等待，直到熔断恢复
            await _wait_breaker()

            try:
                # 全局 Semaphore 限制并发 LLM 调用（所有实例 / Tier 共享）
                async with _LLM_SEMAPHORE:
                    result = await self._graphiti.add_episode(
                        name=episode.name,
                        episode_body=extended_body,
                        source_description=episode.source_description,
                        reference_time=episode.valid_at,
                        source=EpisodeType.text,
                        entity_types=self._entity_types,
                        edge_types=edge_types,
                        edge_type_map=edge_type_map,
                        custom_extraction_instructions=(
                            "ENTITY NAME LANGUAGE RULE:\n"
                            "- Always extract entity names in English.\n"
                            "- Translate non-English names to their standard English equivalents.\n"
                            "- If uncertain about translation, keep the original name."
                        ),
                    )
                _record_success()

                # 写入成功
                self._seen_hashes.add(episode.content_hash)
                if episode.source_url:
                    self._seen_urls.add(episode.source_url)

                # 4. ticker 接地：白名单内的 SET，白名单外的 REMOVE
                #    在 add_episode() 返回后、content_scope 透传之前执行。
                if self._neo4j_driver and self._whitelist:
                    try:
                        self._ground_tickers(result)
                    except Exception as exc:
                        logger.warning(
                            "ticker grounding failed for '%s': %s",
                            episode.name,
                            exc,
                            exc_info=True,
                        )

                # 4b. severity 补写：add_episode() 不支持 severity 参数，
                #    写入后通过 Cypher 设置 EpisodicNode.severity。
                if episode.severity and self._neo4j_driver:
                    try:
                        self._neo4j_driver.execute_query(
                            "MATCH (e:Episodic {uuid: $uuid}) SET e.severity = $severity",
                            uuid=result.episode.uuid,
                            severity=episode.severity,
                        )
                    except Exception as exc:
                        logger.warning(
                            "severity write failed for '%s': %s", episode.name, exc
                        )

                # 4c. 边类型归一兜底：本次写入边中偏离核心集的 name → 核心类型
                if self._neo4j_driver:
                    try:
                        self._normalize_written_edges(result)
                    except Exception as exc:
                        logger.warning(
                            "edge type normalization failed for '%s': %s",
                            episode.name,
                            exc,
                            exc_info=True,
                        )

                # 5. content_scope 透传：通过 Cypher 设置 episode_metadata
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
                if _is_rate_limit(exc):
                    # 429: 尊重 API retryDelay（>= 37s + jitter），并计入熔断
                    _record_429()
                    retry_delay = _extract_retry_delay(exc)
                    if attempt < self._max_retries:
                        delay = _backoff_for_429(retry_delay)
                        logger.warning(
                            "write_one attempt %d/%d rate-limited for '%s': %s. "
                            "RetryDelay=%s, backing off %.1fs...",
                            attempt,
                            self._max_retries,
                            episode.name,
                            exc,
                            f"{retry_delay:.0f}s" if retry_delay else "unknown",
                            delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "write_one rate-limited after %d attempts for '%s': %s",
                            self._max_retries,
                            episode.name,
                            exc,
                            exc_info=True,
                        )
                else:
                    # 其他错误: 指数退避
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

    def _ground_tickers(self, result: Any) -> None:
        """写入后 ticker 接地：白名单内的 SET，白名单外的 REMOVE。

        不信任 LLM 填写的 ticker，写入后基于 whitelist 做确定性校正：
        1. 命中白名单（节点名/biz_code/别名/canonical 归一）→ SET 正确 ticker
        2. 未命中白名单但节点上有 ticker → REMOVE（指数/ETF/幻觉代码）

        这同时修复三个问题：
        - 张冠李戴：指数/ETF 被错误填上白名单股票的 ticker → 被 REMOVE
        - dedup 丢失：MACRO 先建节点无 ticker（属性合并时丢失）→ 重新 SET
        - 分类歧义：上市公司被 LLM 分类为 Organization → 按名称匹配仍能 SET
        """
        if not hasattr(result, "nodes") or not result.nodes:
            return

        # 构建 name -> ticker 映射（支持 canonical_name 归一 + biz_code + 别名）
        name_to_ticker: dict[str, str] = {}
        for entry in self._whitelist:
            name = entry.get("name", "")
            ticker = entry.get("ticker", "")
            biz_code = entry.get("biz_code", "")
            if name and ticker:
                name_to_ticker[name] = ticker
                canonical = canonical_name(name, "stock")
                if canonical != name:
                    name_to_ticker[canonical] = ticker
            if biz_code and ticker:
                name_to_ticker[biz_code] = ticker

        if not name_to_ticker:
            return

        with self._neo4j_driver.session() as session:
            for node in result.nodes:
                node_uuid = self._node_attr(node, "uuid")
                node_name = self._node_attr(node, "name")
                if not node_uuid or not node_name:
                    continue

                # 检查是否命中白名单（名称/biz_code/别名/canonical）
                matched_ticker: str | None = None
                for check_name in (node_name, canonical_name(node_name, "stock")):
                    if check_name in name_to_ticker:
                        matched_ticker = name_to_ticker[check_name]
                        break

                if matched_ticker:
                    # 命中白名单 → SET ticker（确定性覆盖 LLM 填写的值）
                    session.run(
                        "MATCH (n) WHERE n.uuid = $uuid SET n.ticker = $ticker",
                        uuid=node_uuid,
                        ticker=matched_ticker,
                    )
                    logger.debug(
                        "ticker grounding: SET %s = %s (node=%s)",
                        node_name,
                        matched_ticker,
                        node_uuid[:12],
                    )
                else:
                    # 未命中白名单 → 若有 ticker 则 REMOVE（指数/ETF/幻觉代码）
                    session.run(
                        "MATCH (n) WHERE n.uuid = $uuid "
                        "AND n.ticker IS NOT NULL REMOVE n.ticker",
                        uuid=node_uuid,
                    )

    def _normalize_written_edges(self, result: Any) -> None:
        """写入后边类型归一兜底：矫正本次写入边中偏离核心集的 name。

        graphiti-core 在 add_episode() 内部落库（关系类型统一为 :RELATES_TO，
        语义类型存于 name 属性）。此处只修正 name 属性（uuid 不变），
        保证即便 LLM 偶发产出 schema 外名称，库内语义类型也收敛到核心集。
        """
        if self._neo4j_driver is None:
            return
        if not hasattr(result, "edges") or not result.edges:
            return

        for edge in result.edges:
            edge_uuid = self._node_attr(edge, "uuid")
            raw_name = self._node_attr(edge, "name")
            if not edge_uuid or not raw_name:
                continue
            core_name = normalize_edge_type(raw_name)
            if core_name == raw_name:
                continue
            try:
                with self._neo4j_driver.session() as session:
                    session.run(
                        "MATCH (n:Entity)-[r:RELATES_TO {uuid: $uuid}]->(m:Entity) "
                        "SET r.name = $name",
                        uuid=edge_uuid,
                        name=core_name,
                    )
                logger.info(
                    "edge type normalized: %s -> %s (edge %s)",
                    raw_name,
                    core_name,
                    edge_uuid[:12],
                )
            except Exception as exc:
                logger.warning(
                    "edge type normalization failed for %s (%s -> %s): %s",
                    edge_uuid[:12],
                    raw_name,
                    core_name,
                    exc,
                )

    @staticmethod
    def _node_attr(node: Any, key: str) -> Any:
        """从实体节点读取属性，兼容 Pydantic 模型与 dict 两种形态。

        graphiti-core 的 add_episode() 返回 EntityNode（Pydantic 模型，
        属性用 .uuid/.name 访问）；为防御后续版本返回 dict 的情况，
        这里两种形态都支持。
        """
        if isinstance(node, dict):
            return node.get(key)
        return getattr(node, key, None)


def _build_extended_body(episode: NormalizedEpisode) -> str:
    """构建 episode body，附加实体解析约束。

    这帮助 LLM 更准确地提取实体（已有提示），减少错误分类。
    同时注入 ENTITY RESOLUTION RULES 约束，强制 LLM 使用规范化后的实体名称，
    避免因名称变体（如 "Tencent" vs "Tencent Holdings Ltd."）导致的重复实体。
    """
    if not episode.entities:
        return episode.episode_body

    lines = [
        "\n[END OF CONTENT]",
        "",
        "ENTITY RESOLUTION RULES:",
        "1. Use the canonical names listed below as the preferred forms for entity resolution",
        "2. Do NOT add or remove suffixes (Ltd, Inc, Corp, 控股, etc.)",
        "3. If the same entity appears with different names, use the FIRST name listed",
        "",
        "CANONICAL ENTITY NAMES:",
    ]
    for ent in episode.entities:
        if ent.ticker:
            lines.append(f"- {ent.name} ({ent.ticker})")
        else:
            lines.append(f"- {ent.name}")

    return episode.episode_body + "\n".join(lines)


# ── 429 识别 / retryDelay 提取 / 退避 / 熔断 ────────────────────────────


def _is_rate_limit(exc: Exception) -> bool:
    """判断异常是否为 429 限流。

    命中 graphiti-core 包装的 RateLimitError，或任意 SDK 原生 RateLimitError
    （openai / anthropic / google.genai 等，按类名识别，避免硬依赖）。
    """
    if RateLimitError is not None and isinstance(exc, RateLimitError):
        return True
    name = type(exc).__name__.lower()
    return "ratelimit" in name or "rate_limit" in name


def _parse_delay_seconds(value: Any) -> float | None:
    """把 retryDelay 值解析为秒。

    支持: 数字 / "44s" / "1.5m" / "500ms" / timedelta / {"seconds": N, "nanos": N}
    （Google rpc Duration 形态）。解析失败返回 None。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*", value.strip(), re.IGNORECASE
        )
        if not match:
            return None
        num = float(match.group(1))
        unit = (match.group(2) or "s").lower()
        return num * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    if isinstance(value, dict):
        seconds = value.get("seconds")
        if seconds is None:
            return None
        nanos = value.get("nanos", 0) or 0
        return float(seconds) + float(nanos) / 1e9
    if isinstance(value, timedelta):
        return value.total_seconds()
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        try:
            return float(total_seconds())
        except Exception:
            return None
    return None


def _extract_retry_delay_from_body(body: Any) -> float | None:
    """在响应体 / 详情 dict|list 中递归查找 retryDelay（Google rpc RetryInfo 形态）。"""
    if body is None:
        return None
    if isinstance(body, dict):
        for key in ("retryDelay", "retry_delay", "retryDelaySeconds"):
            if key in body:
                parsed = _parse_delay_seconds(body[key])
                if parsed is not None:
                    return parsed
        for value in body.values():
            parsed = _extract_retry_delay_from_body(value)
            if parsed is not None:
                return parsed
    elif isinstance(body, (list, tuple)):
        for item in body:
            parsed = _extract_retry_delay_from_body(item)
            if parsed is not None:
                return parsed
    return None


_RETRY_DELAY_TEXT_RE = re.compile(
    r"retry_?delay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)\s*(ms|s|m|h)?",
    re.IGNORECASE,
)


def _extract_retry_delay(exc: BaseException) -> float | None:
    """从 429 异常链中提取 API 返回的 retryDelay（秒），找不到返回 None。

    沿 __cause__ / __context__ 遍历整条异常链（graphiti 用 `raise RateLimitError from e`
    链住原始 SDK 异常），依次尝试:
    1. 异常对象直接属性 retryDelay / retry_delay / Retry-After
    2. .response 的 headers（Retry-After）与 JSON body（Google rpc RetryInfo.retryDelay）
    3. .details 中的 retryDelay
    4. 异常文本中的 `retryDelay: "Ns"`
    """
    seen: set[int] = set()
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__

    # 1) 直接属性
    for cand in chain:
        for attr in ("retryDelay", "retry_delay", "retryDelaySeconds", "Retry-After"):
            parsed = _parse_delay_seconds(getattr(cand, attr, None))
            if parsed is not None:
                return parsed

    # 2) response headers + body
    for cand in chain:
        resp = getattr(cand, "response", None)
        if resp is None:
            continue
        headers = getattr(resp, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            for header_name in ("Retry-After", "retry-after", "x-retry-after"):
                parsed = _parse_delay_seconds(headers.get(header_name))
                if parsed is not None:
                    return parsed
        body = None
        json_fn = getattr(resp, "json", None)
        if callable(json_fn):
            try:
                body = json_fn()
            except Exception:
                body = None
        if body is None:
            body = getattr(resp, "text", None)
        parsed = _extract_retry_delay_from_body(body)
        if parsed is not None:
            return parsed

    # 3) details 字段
    for cand in chain:
        parsed = _extract_retry_delay_from_body(getattr(cand, "details", None))
        if parsed is not None:
            return parsed

    # 4) 文本扫描
    for cand in chain:
        match = _RETRY_DELAY_TEXT_RE.search(str(cand))
        if match:
            parsed = _parse_delay_seconds(f"{match.group(1)}{match.group(2) or 's'}")
            if parsed is not None:
                return parsed
    return None


def _backoff_for_429(retry_delay: float | None) -> float:
    """429 退避: 尊重 API 返回的 retryDelay，但不得低于 37s，并加 jitter 防共振。"""
    base = max(retry_delay or 0.0, _MIN_429_BACKOFF_SEC)
    return base + random.uniform(0.0, _MAX_429_JITTER_SEC)


async def _wait_breaker() -> None:
    """熔断冷却: 熔断打开期间，整个写入队列在此等待直至冷却结束。

    分段 sleep（最长 5s/段），保证可及时响应取消信号。
    """
    while True:
        now = time.monotonic()
        remaining = _CIRCUIT_OPEN_UNTIL - now
        if remaining <= 0:
            return
        logger.warning(
            "Circuit breaker OPEN: all writers cooling down %.1fs",
            remaining,
        )
        await asyncio.sleep(min(remaining, 5.0))


def _record_429() -> None:
    """记录一次全局 429。连续达到阈值 → 打开熔断，冷却整个队列。

    单线程事件循环下计数读写原子，无需加锁。
    """
    global _CIRCUIT_CONSECUTIVE_429, _CIRCUIT_OPEN_UNTIL
    _CIRCUIT_CONSECUTIVE_429 += 1
    if _CIRCUIT_CONSECUTIVE_429 >= _CIRCUIT_MAX_CONSECUTIVE_429:
        _CIRCUIT_OPEN_UNTIL = time.monotonic() + _CIRCUIT_COOLDOWN_SEC
        _CIRCUIT_CONSECUTIVE_429 = 0  # 已触发熔断，冷却结束后重新累计
        logger.warning(
            "Circuit breaker OPEN: %d consecutive 429s. "
            "Cooling down %.0fs (all writers pause).",
            _CIRCUIT_MAX_CONSECUTIVE_429,
            _CIRCUIT_COOLDOWN_SEC,
        )


def _record_success() -> None:
    """写入成功 → 重置全局连续 429 计数。"""
    global _CIRCUIT_CONSECUTIVE_429
    if _CIRCUIT_CONSECUTIVE_429:
        _CIRCUIT_CONSECUTIVE_429 = 0
        logger.info("Circuit breaker: consecutive 429 counter reset")
