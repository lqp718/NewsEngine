"""SectorBriefingAggregator — sector_briefing 的完整生成链路.

Query Neo4j → LLM 聚合 → 写入内存缓存.
调用方: ingestion/scheduler.py（每 15 分钟调度一次）
消费者: api/routers/events.py（GET /api/events/sector/:name → 从缓存读取）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from openai import AsyncOpenAI
from neo4j import Driver

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个专业的金融情报分析师。你的任务是将一个行业（sector）内的多个离散新闻事件，
聚合为一份 300-500 字的中文 Markdown 行业情报简报。

要求：
1. 按 severity 降序排列事件（critical > high > medium > low）
2. 先给出"核心摘要"（2-3 句话概括该行业当前状态）
3. 然后逐条简述高风险事件（severity=high/critical），每条包含：事件标题、影响标的、潜在影响
4. 最后给出"行业展望"（1-2 句话总结趋势方向）
5. 语言简洁，适合量化交易系统的下游 Agent（MiroFish、PM Agent）直接消费
6. 不要在输出中包含"根据提供的数据"等元描述语
7. 输出纯 Markdown 文本，不要用 JSON 包裹"""

SEVERITY_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}


@dataclass
class BriefingCacheEntry:
    """单个 sector 的简报缓存."""

    briefing: str  # LLM 输出的 Markdown 文本
    generated_at: datetime  # 生成时间
    event_fingerprint: str  # 用于变化检测的事件哈希


class SectorBriefingAggregator:
    """行业简报聚合器.

    生命周期:
    - 由 ingestion/scheduler.py 在进程启动时初始化
    - 每个 15 分钟轮询周期结束时调用 aggregate_all()
    - API 层通过 get_cached(sector_name) 读取缓存
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._cache: dict[str, BriefingCacheEntry] = {}
        self._llm_client = AsyncOpenAI(
            api_key=settings.bailian_api_key,
            base_url=settings.openai_base_url,
        )
        self._llm_model = "qwen-plus"

    # ── 公开接口 ────────────────────────────────────────────────────

    def get_cached(self, sector_name: str) -> str | None:
        """获取 sector 的缓存简报. 无缓存则返回 None.

        API 层直接调用此方法 (O(1)), 不触发 LLM 调用.
        """
        entry = self._cache.get(sector_name)
        return entry.briefing if entry else None

    async def aggregate_all(
        self, sector_names: list[str]
    ) -> dict[str, str | None]:
        """对所有 sector 执行聚合 (增量).

        仅对事件指纹变化的 sector 重新生成简报.
        由 ingestion/scheduler.py 在每个轮询周期结束时调用.

        Returns:
            {sector_name: briefing_text | None}
        """
        results: dict[str, str | None] = {}
        for sector_name in sector_names:
            try:
                new_briefing = await self._aggregate_one(sector_name)
                results[sector_name] = new_briefing
            except Exception as exc:
                logger.error(
                    "sector_briefing 聚合失败 [%s]: %s", sector_name, exc
                )
                results[sector_name] = None
        return results

    # ── 内部方法 ────────────────────────────────────────────────────

    async def _aggregate_one(self, sector_name: str) -> str | None:
        """对单个 sector 执行聚合.

        流程:
        1. 从 Neo4j 查询该 sector 的事件
        2. 计算事件指纹
        3. 若指纹未变, 返回已有缓存
        4. 若指纹变化, 调用 LLM 生成新简报
        5. 更新缓存
        """
        driver = get_neo4j_driver()

        # Step 1: 查询事件
        events = await self._query_sector_events(driver, sector_name)
        if not events:
            logger.info("sector=%s 无活跃事件, 跳过聚合", sector_name)
            return None

        # Step 2: 事件指纹变化检测
        fingerprint = self._compute_fingerprint(events)
        cached = self._cache.get(sector_name)
        if cached and cached.event_fingerprint == fingerprint:
            logger.debug("sector=%s 事件无变化, 跳过 LLM 聚合", sector_name)
            return cached.briefing

        # Step 3: 调用 LLM 生成简报
        user_prompt = self._build_user_prompt(sector_name, events)
        briefing = await self._call_llm(user_prompt)

        # Step 4: 更新缓存
        self._cache[sector_name] = BriefingCacheEntry(
            briefing=briefing,
            generated_at=datetime.now(timezone.utc),
            event_fingerprint=fingerprint,
        )
        logger.info(
            "sector=%s 简报已更新 (%d 事件, %d 字)",
            sector_name,
            len(events),
            len(briefing),
        )
        return briefing

    async def _query_sector_events(
        self, driver: Driver, sector_name: str
    ) -> list[dict]:
        """执行 Neo4j Cypher 查询."""
        query = """
        MATCH (s:Sector {name: $sector_name})
              <-[:BELONGS_TO]-(stock:Stock)
              <-[:AFFECTS]-(event:Event)
        WHERE event.valid_at IS NOT NULL AND event.invalid_at IS NULL
        RETURN
          event.event_id AS event_id,
          event.title AS title,
          event.severity AS severity,
          event.first_seen AS first_seen,
          event.last_updated AS last_updated,
          event.source_count AS source_count,
          event.summary AS summary,
          event.keywords AS keywords,
          collect(DISTINCT stock.ticker) AS affected_tickers,
          collect(DISTINCT stock.name) AS affected_stocks
        ORDER BY
          CASE event.severity
            WHEN 'critical' THEN 4 WHEN 'high' THEN 3
            WHEN 'medium' THEN 2 WHEN 'low' THEN 1
          END DESC,
          event.last_updated DESC
        LIMIT 20
        """
        records, _, _ = await asyncio.to_thread(
            driver.execute_query, query, sector_name=sector_name
        )
        return [dict(r) for r in records]

    def _compute_fingerprint(self, events: list[dict]) -> str:
        """计算事件指纹 (用于增量检测).

        指纹 = SHA256(event_id + last_updated 的排序拼接)
        任何事件新增/修改/删除 -> 指纹变化 -> 触发重新聚合
        """
        key = "|".join(
            f"{e['event_id']}:{e['last_updated']}"
            for e in sorted(events, key=lambda x: str(x.get("event_id", "")))
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _build_user_prompt(
        self, sector_name: str, events: list[dict]
    ) -> str:
        """构建 LLM User Prompt."""
        tickers_set: set[str] = set()
        for e in events:
            for t in e.get("affected_tickers", []):
                tickers_set.add(str(t))

        events_text_parts: list[str] = []
        for e in events:
            icon = SEVERITY_ICON.get(str(e.get("severity", "")), "⚪")
            tickers = ", ".join(str(t) for t in e.get("affected_tickers", []))
            keywords = ", ".join(str(k) for k in e.get("keywords", [])[:5])
            events_text_parts.append(
                f"### [{icon}] {e.get('title', '无标题')}\n"
                f"- 严重级别: {e.get('severity', 'unknown')}\n"
                f"- 时间: {e.get('first_seen', '?')} ~ "
                f"{e.get('last_updated', '?')}\n"
                f"- 信源数: {e.get('source_count', 0)}\n"
                f"- 摘要: {e.get('summary', '无')}\n"
                f"- 受影响标的: {tickers}\n"
                f"- 关键词: {keywords}\n"
            )

        return (
            f"## 行业: {sector_name}\n"
            f"## 统计: 总事件 {len(events)} 个, 涉及 {len(tickers_set)} 只标的\n\n"
            f"## 事件列表:\n\n"
            f"{chr(10).join(events_text_parts)}\n\n"
            f"---\n"
            f"请基于以上事件数据, 生成该行业的 300-500 字中文 Markdown 情报简报。"
        )

    async def _call_llm(self, user_prompt: str) -> str:
        """调用百炼 qwen-plus 生成简报."""
        response = await self._llm_client.chat.completions.create(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("LLM 返回空内容")
        return content[:2000]  # 截断超长内容


__all__ = [
    "SectorBriefingAggregator",
    "BriefingCacheEntry",
    "SYSTEM_PROMPT",
]
