"""Translation layer — translate MACRO-source episodes to Chinese.

Sits between the adapter output (``adapter.run()``) and persistence
(``landing_store.capture_batch()`` / ``writer.write_batch()``) so that both
the landing JSONL and the graphiti knowledge graph see Chinese content for
English-dominated sources (GDELT, RSS, treasury, sanctions, ACLED, EIA,
BLS, FRED).

Design notes:
    - Uses ``QwenNoThinkingClient`` (structured JSON output, thinking mode
      disabled) via the graphiti ``LLMClient.generate_response`` interface.
    - Batched: several episodes per LLM call, greedily packed by both item
      count (default 8, within the recommended 5–10 range) and a character
      budget, to bound output size and avoid timeouts.
    - Degrade-safe: any chunk-level failure logs a warning and keeps the
      original (untranslated) episodes — translation must never block
      ingestion.
    - Dedup safety: ``NormalizedEpisode.model_post_init`` recomputes
      ``content_hash`` from ``episode_body``. Since both the adapter
      ``dedup_cache`` and the landing store PK (INSERT OR IGNORE on
      content_hash) key on that hash, the ORIGINAL content_hash (and
      ``name``, whose suffix embeds hash[:12]) is restored after
      translation. Cross-cycle dedup therefore stays keyed to the source
      content, and LLM non-determinism cannot produce duplicate rows.
    - Entity names: translated by the LLM, then normalized through
      ``canonical_entities.yaml`` (via ``src.utils.entity_canonical``) so
      high-frequency entities land on their Chinese canonical names
      (e.g. "Tencent Holdings" → "腾讯控股"). Known mappings are also
      injected into the prompt as hints.
    - Translated episodes are NEW objects; inputs are never mutated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from graphiti_core.prompts.models import Message
from pydantic import BaseModel, Field

from src.adapters.models import EntityItem, NormalizedEpisode
from src.utils.entity_canonical import ALIAS_MAP, canonical_name
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# ── Structured-output models ───────────────────────────────────────────


class _TranslatedItem(BaseModel):
    """One translated episode inside a batch response."""

    index: int = Field(..., description="0-based index matching the input item order.")
    episode_body: str = Field(..., description="Full Chinese translation of episode_body.")
    source_description: str | None = Field(
        default=None, description="Chinese translation of source_description (optional)."
    )
    entities: list[str] = Field(
        default_factory=list,
        description=(
            "Chinese entity names, same order and length as the input "
            "entities list. Keep names that have no common Chinese form as-is."
        ),
    )


class _TranslationBatchResult(BaseModel):
    """Batch translation response envelope."""

    translations: list[_TranslatedItem]


# ── Prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "你是专业的财经与宏观新闻翻译引擎，负责把英文新闻/数据摘要翻译成简体中文。\n"
    "规则：\n"
    "1. 忠实翻译，不增删信息，不添加评论。\n"
    "2. 数字、日期、百分比、金额、货币单位、代码（如 ticker、事件码）保持原样，"
    "单位可转换为中文习惯表达（如 billion → 亿/十亿需保持数值等价）。\n"
    "3. 专有名词优先使用通用中文译名；如果输入提供了 entity_hints（英文→中文对照），"
    "必须采用对照表中的中文名。\n"
    "4. 保留原文的结构（段落、列表、标记行如 [END OF CONTENT]）。\n"
    "5. entities 列表中的每个名字翻译为中文，顺序和数量必须与输入一致；"
    "无通用中文译名的保留原文。\n"
    "6. 只输出符合 JSON schema 的结果，不要输出任何解释。"
)


def _build_user_payload(items: list[dict[str, Any]]) -> str:
    return (
        "请把下面 JSON 数组中的每一条翻译成简体中文，"
        "按相同顺序返回 translations 数组（index 与输入一致）：\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


# ── Translator ─────────────────────────────────────────────────────────


class EpisodeTranslator:
    """翻译 MACRO 源 episode 为中文。

    只对 ``TRANSLATABLE_SOURCES`` 中的 source_type 生效；其余（已是中文的
    akshare/eastmoney/cls 等源）原样透传。返回新对象列表，不修改输入。
    """

    # 需要翻译的 source_type（英文为主）
    TRANSLATABLE_SOURCES = {
        "gdelt_csv",
        "gdelt_events",
        "rss",
        "treasury",
        "sanctions",
        "acled",
        "eia",
        "bls",
        "fred",
    }

    # 单次 LLM 调用的批大小（建议 5–10 条/批，避免超时）
    DEFAULT_BATCH_SIZE = 8
    # 单批输入字符预算（贪心装箱，长文章自动单独成批）
    MAX_BATCH_CHARS = 24_000

    def __init__(self, llm_client: Any, batch_size: int | None = None):
        """
        Args:
            llm_client: QwenNoThinkingClient（或任何实现 graphiti
                ``LLMClient.generate_response`` 接口的客户端）。
            batch_size: 每批 episode 数，默认 8，钳制在 5–10。
        """
        self._client = llm_client
        if batch_size is None:
            batch_size = self.DEFAULT_BATCH_SIZE
        self._batch_size = max(5, min(10, int(batch_size)))
        self._canonical_map = self._load_canonical_map()

    @staticmethod
    def _load_canonical_map() -> dict[str, str]:
        """Load alias → canonical-name map from data/canonical_entities.yaml.

        Reuses the module-level map already parsed by
        ``src.utils.entity_canonical`` (lowercased alias → canonical name).
        """
        return dict(ALIAS_MAP)

    # ── Public API ─────────────────────────────────────────────────

    async def translate_batch(
        self, episodes: list[NormalizedEpisode]
    ) -> list[NormalizedEpisode]:
        """翻译一批 episode，返回翻译后的新列表（顺序与输入一致）。

        - 只翻译 TRANSLATABLE_SOURCES 中的 source_type，其余原样透传。
        - 分块批量调用 LLM；单块失败降级为保留原文并记录 warning。
        """
        if not episodes:
            return []

        results: list[NormalizedEpisode] = list(episodes)
        pending: list[tuple[int, NormalizedEpisode]] = [
            (i, ep)
            for i, ep in enumerate(episodes)
            if ep.source_type in self.TRANSLATABLE_SOURCES
        ]
        if not pending:
            logger.debug(
                "translator: no translatable episodes in batch of %d", len(episodes)
            )
            return results

        chunks = self._pack_chunks(pending)
        translated_count = 0
        failed_count = 0

        for chunk in chunks:
            # 顺序执行（不 gather）：避免并发触发 LLM 限流；单块失败不影响其他块。
            try:
                translated = await self._translate_chunk([ep for _, ep in chunk])
            except Exception as exc:
                failed_count += len(chunk)
                logger.warning(
                    "translator: chunk of %d episodes failed (%s) — "
                    "keeping original text",
                    len(chunk),
                    exc,
                )
                continue
            for (idx, _orig_ep), new_ep in zip(chunk, translated):
                results[idx] = new_ep
                translated_count += 1

        logger.info(
            "translator: %d translated, %d failed (kept original), "
            "%d passthrough (non-translatable sources)",
            translated_count,
            failed_count,
            len(episodes) - len(pending),
        )
        return results

    # ── Chunking ───────────────────────────────────────────────────

    def _pack_chunks(
        self, episodes: list[tuple[int, NormalizedEpisode]]
    ) -> list[list[tuple[int, NormalizedEpisode]]]:
        """Greedily pack (index, episode) pairs into chunks by count and char budget."""
        chunks: list[list[tuple[int, NormalizedEpisode]]] = []
        current: list[tuple[int, NormalizedEpisode]] = []
        current_chars = 0
        for entry in episodes:
            ep = entry[1]
            size = len(ep.episode_body) + len(ep.source_description or "")
            if current and (
                len(current) >= self._batch_size
                or current_chars + size > self.MAX_BATCH_CHARS
            ):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(entry)
            current_chars += size
        if current:
            chunks.append(current)
        return chunks

    # ── LLM call ───────────────────────────────────────────────────

    async def _translate_chunk(
        self, chunk: list[NormalizedEpisode]
    ) -> list[NormalizedEpisode]:
        """Translate one chunk via a single LLM call; returns new episodes."""
        items: list[dict[str, Any]] = []
        for i, ep in enumerate(chunk):
            item: dict[str, Any] = {
                "index": i,
                "episode_body": ep.episode_body,
                "source_description": ep.source_description,
                "entities": [ent.name for ent in ep.entities],
            }
            hints = self._entity_hints(ep)
            if hints:
                item["entity_hints"] = hints
            items.append(item)

        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=_build_user_payload(items)),
        ]
        raw = await self._client.generate_response(
            messages=messages,
            response_model=_TranslationBatchResult,
        )
        parsed = _TranslationBatchResult.model_validate(raw)

        by_index = {t.index: t for t in parsed.translations}
        out: list[NormalizedEpisode] = []
        for i, ep in enumerate(chunk):
            t = by_index.get(i)
            if t is None or not (t.episode_body or "").strip():
                logger.warning(
                    "translator: missing/empty translation for item %d "
                    "(episode %s) — keeping original",
                    i,
                    ep.name,
                )
                out.append(ep)
                continue
            out.append(self._rebuild_episode(ep, t))
        return out

    # ── Entity mapping ─────────────────────────────────────────────

    def _entity_hints(self, ep: NormalizedEpisode) -> dict[str, str]:
        """Build English → Chinese hints for entities known in canonical map."""
        hints: dict[str, str] = {}
        for ent in ep.entities:
            canonical = self._canonical_map.get(ent.name.strip().lower())
            if canonical and canonical != ent.name:
                hints[ent.name] = canonical
        return hints

    def _map_entity_name(self, translated: str, fallback: str, entity_type: str) -> str:
        """Map a translated entity name through the canonical map.

        Prefers exact canonical-map hits (covers both the translated name
        and any residual English alias), then falls back to
        ``canonical_name`` (alias lookup + corporate-suffix stripping).
        """
        name = (translated or fallback).strip()
        if not name:
            name = fallback
        hit = self._canonical_map.get(name.lower())
        if hit:
            return hit
        return canonical_name(name, entity_type)

    # ── Episode rebuild ────────────────────────────────────────────

    def _rebuild_episode(
        self, ep: NormalizedEpisode, t: _TranslatedItem
    ) -> NormalizedEpisode:
        """Build a NEW translated episode; never mutates the input."""
        new_entities: list[EntityItem] = []
        for i, ent in enumerate(ep.entities):
            translated_name = t.entities[i] if i < len(t.entities) else ""
            name = self._map_entity_name(translated_name, ent.name, ent.type)
            new_entities.append(
                EntityItem(
                    type=ent.type,
                    name=name,
                    ticker=ent.ticker,
                    sector=ent.sector,
                    exchange=ent.exchange,
                )
            )

        new_ep = NormalizedEpisode(
            episode_body=t.episode_body,
            name=ep.name,
            source_description=(t.source_description or ep.source_description),
            source_type=ep.source_type,
            source_url=ep.source_url,
            valid_at=ep.valid_at,
            content_hash=ep.content_hash,
            entities=new_entities,
            is_plain_text=ep.is_plain_text,
            severity=ep.severity,
            keywords=ep.keywords,
            metadata={
                **ep.metadata,
                "translated": True,
                "translated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        # model_post_init recomputed content_hash from the TRANSLATED body.
        # Restore the original hash so cross-cycle dedup stays keyed to the
        # source content: adapter.dedup_cache and the landing-store PK
        # (INSERT OR IGNORE on content_hash) would otherwise miss on the
        # next cycle (LLM output is non-deterministic) and write duplicate
        # rows. The `name` suffix (hash[:12]) stays consistent for the same
        # reason — keep the original name.
        new_ep.content_hash = ep.content_hash
        return new_ep


__all__ = ["EpisodeTranslator"]

# TRANSLATION_LAYER_COMPLETE
