"""单元测试: prompt leakage 修复（_build_extended_body → custom_extraction_instructions）。

背景:
    旧版 _build_extended_body() 把 "ENTITY RESOLUTION RULES" / "CANONICAL ENTITY
    NAMES" 等指令文本追加到 episode_body 末尾，graphiti-core 的 LLM 把指令文本
    当作实体数据提取，写入节点属性（如 contact 混入 "Please wait..." /
    "ENTITY RESOLUTION RULES..."）。

覆盖:
- _build_extraction_instructions 包含 canonical names / ticker / 解析规则
- 无实体时仅返回基础语言规则
- write_one 传给 add_episode 的 episode_body 不含任何指令标记
- write_one 的 custom_extraction_instructions 携带 canonical names
- 清理脚本 truncate_leaked 的截断 / 移除 / 幂等语义
"""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.adapters.models import EntityItem, NormalizedEpisode
from src.graphiti.episode_writer import (
    EpisodeWriter,
    _build_extraction_instructions,
)

# 泄漏标记（与旧版指令追加内容一致），用于断言 body 中不得出现
_LEAK_MARKER_RE = re.compile(
    r"\[END OF CONTENT\]"
    r"|ENTITY RESOLUTION RULES"
    r"|CANONICAL ENTITY NAMES"
    r"|ENTITY NAME LANGUAGE RULE"
    r"|Please wait,?\s*We are optimizing your request",
    re.IGNORECASE,
)


def _load_clean_script_module():
    """按路径加载 scripts/clean_prompt_leakage.py（scripts 非包）。"""
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "clean_prompt_leakage.py"
    )
    spec = importlib.util.spec_from_file_location(
        "clean_prompt_leakage", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_episode(
    body: str = "Tencent Holdings reported strong quarterly earnings.",
    entities: list[EntityItem] | None = None,
) -> NormalizedEpisode:
    return NormalizedEpisode(
        episode_body=body,
        name="rss-20260831-deadbeef0001",
        source_description="rss test feed",
        source_type="rss",
        valid_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        content_hash="",  # model_post_init 会重算
        entities=entities or [],
    )


class TestBuildExtractionInstructions:
    """canonical entity names 通过 custom_extraction_instructions 传递。"""

    def test_contains_canonical_names_with_ticker(self):
        ent_stock = EntityItem(type="stock", name="Tencent Holdings", ticker="0700.HK")
        ent_org = EntityItem(type="organization", name="Alibaba Group")
        episode = _make_episode(entities=[ent_stock, ent_org])
        instructions = _build_extraction_instructions(episode)

        assert "CANONICAL ENTITY NAMES" in instructions
        # EntityItem 可能在构造时做 canonical 归一，断言构造后的实际名称
        assert f"- {ent_stock.name} ({ent_stock.ticker})" in instructions
        assert f"- {ent_org.name}" in instructions
        assert "ENTITY RESOLUTION RULES" in instructions
        assert "ENTITY NAME LANGUAGE RULE" in instructions

    def test_no_entities_returns_base_rules_only(self):
        instructions = _build_extraction_instructions(_make_episode(entities=[]))

        assert "ENTITY NAME LANGUAGE RULE" in instructions
        assert "CANONICAL ENTITY NAMES" not in instructions
        assert "ENTITY RESOLUTION RULES" not in instructions


class TestWriteOneNoLeakage:
    """write_one 传给 add_episode 的参数不得携带指令文本。"""

    @pytest.mark.asyncio
    async def test_body_clean_and_instructions_carry_canonical_names(self):
        body = "Tencent Holdings reported strong quarterly earnings."
        ent = EntityItem(type="stock", name="Tencent Holdings", ticker="0700.HK")
        episode = _make_episode(body=body, entities=[ent])

        fake_graphiti = SimpleNamespace(
            add_episode=AsyncMock(
                return_value=SimpleNamespace(
                    episode=SimpleNamespace(uuid="ep-uuid-0001"),
                    nodes=[],
                    edges=[],
                )
            )
        )
        writer = EpisodeWriter(graphiti=fake_graphiti, neo4j_driver=None)

        result = await writer.write_one(episode)

        assert result.status == "ok"
        kwargs = fake_graphiti.add_episode.call_args.kwargs

        # 1) episode_body 只含正文，不含任何指令标记
        assert kwargs["episode_body"] == body
        assert not _LEAK_MARKER_RE.search(kwargs["episode_body"])

        # 2) canonical names 走 custom_extraction_instructions 通道
        instructions = kwargs["custom_extraction_instructions"]
        assert "CANONICAL ENTITY NAMES" in instructions
        assert f"- {ent.name} ({ent.ticker})" in instructions
        assert "ENTITY RESOLUTION RULES" in instructions


class TestCleanScriptTruncate:
    """清理脚本的截断语义与幂等性。"""

    def setup_method(self):
        self.module = _load_clean_script_module()

    def test_truncate_removes_leak_suffix(self):
        polluted = (
            "86-755-86013388\n"
            "[END OF CONTENT]\n\n"
            "ENTITY RESOLUTION RULES:\n1. Use the canonical names..."
        )
        assert self.module.truncate_leaked(polluted) == "86-755-86013388"

    def test_truncate_pure_pollution_yields_empty(self):
        polluted = "Please wait We are optimizing your request..."
        assert self.module.truncate_leaked(polluted) == ""

    def test_truncate_idempotent_and_preserves_clean_values(self):
        clean = "Tencent Holdings investor relations contact"
        assert self.module.truncate_leaked(clean) == clean

        polluted = "ENTITY RESOLUTION RULES:\nCANONICAL ENTITY NAMES:\n- Foo"
        once = self.module.truncate_leaked(polluted)
        assert once == ""
        # 幂等: 已清理的值再处理不变
        assert self.module.truncate_leaked(once) == once

    def test_marker_pattern_matches_all_known_signatures(self):
        for text in (
            "[END OF CONTENT]",
            "ENTITY RESOLUTION RULES",
            "CANONICAL ENTITY NAMES",
            "Please wait, We are optimizing your request",
            "Please wait We are optimizing your request",
            "1. Use the canonical names listed below as the preferred forms",
            "Do NOT add or remove suffixes (Ltd, Inc, Corp)",
            "(Wait, let's refine based on the prompt text)",
            "Policy Check - Updated from Messages",
            "'Policy/Act\u201d, 1. Use the canonical names",
        ):
            assert re.search(
                self.module.LEAK_MARKERS_PATTERN, text, re.IGNORECASE
            ), f"marker not covered: {text!r}"

    def test_truncate_entity_status_blob_removed(self):
        # 实际污染样本: Entity.status 整段为泄漏的指令/消息 JSON 垃圾
        blob = (
            "'Policy/Act\u201d, 1. Use the canonical names listed below "
            "as the preferred forms for entity resolution 2. Do NOT add "
            "or remove suffixes CANONICAL ENTITY NAMES: - India - \u9ec4\u91d1"
        )
        assert self.module.truncate_leaked(blob) == ""

    def test_truncate_claims_bill_status_removed(self):
        # 实际污染样本: 截断后仅剩前导引号碎片 → 归一为空串 → REMOVE
        polluted = (
            "\"Policy Check - Updated from Messages: [END OF CONTENT] "
            "(Wait, let's refine based on the prompt text)\""
        )
        assert self.module.truncate_leaked(polluted) == ""
