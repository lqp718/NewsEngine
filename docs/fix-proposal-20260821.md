# NewsEngine 修复方案（2026-08-21）

**日期**: 2026-08-21  
**状态**: 待审批  
**范围**: P0 + P1 + P2（P3 暂缓）

---

## 修复清单

### P0 — 日志语义修正

**问题**: LOG_LEVEL=WARNING 设计意图正确（一眼看到问题），但 WARNING 被滥用成垃圾桶 — 513 条 WARNING 中只有 ~50 条（9.7%）是真正需要关注的问题，其余 90% 是噪音。

**修复内容**:

| 文件 | 修改 | 效果 |
|------|------|------|
| `src/utils/news_spider.py` | 降级中间步骤 WARNING → INFO/DEBUG：<br>- Tier 1 blocked（L452, L923）→ DEBUG<br>- Tier 1.5 blocked（L678, L694）→ DEBUG<br>- CloakBrowser 尝试（L549, L582, L586）→ INFO<br>- Camoufox 成功/恢复（L852, L868, L1199）→ INFO<br>- Tier 3 fallback 开始/结果（L1188, L1205）→ INFO | 消除 ~280 条噪音 WARNING |
| `src/utils/content_fetcher.py` | "precision mode failed" fallback（L662）→ DEBUG | 消除 ~20 条噪音 WARNING |
| `src/utils/logging_config.py` | 添加第三方 logger 抑制：<br>`logging.getLogger("scrapling").setLevel(WARNING)`<br>`logging.getLogger("neo4j.notifications").setLevel(ERROR)`<br>`logging.getLogger("trafilatura").setLevel(ERROR)`<br>`logging.getLogger("cloakbrowser").setLevel(ERROR)` | 消除 ~250 条第三方噪音（scrapling INFO 90 条 + neo4j WARNING 139 条 + trafilatura/cloakbrowser） |

**保留的 WARNING**（真正的问题）：
- RSS feed parse error
- ACLED/FRED/Sanctions API 失败
- Pipeline 异常
- Scheduler 无 adapter 可运行

**预期效果**: WARNING 从 513 条降到 ~50 条，LOG_LEVEL=WARNING 时只看到真正需要关注的问题。

**工作量**: ~1 小时

---

### P1 — content_scope 缺失

**问题**: 7 个 adapter 未在 metadata 中设置 `content_scope`，导致：
1. TTL 清理失效（`scheduler.py:1032` 用 `WHERE ep.episode_metadata CONTAINS $scope` 匹配，缺失的 episode 永远不会被清理 → 数据无限增长）
2. API 查询遗漏（`events.py:382` 用 `WHERE e.episode_metadata CONTAINS 'MACRO'` 过滤，缺失的 episode 查不到）

**修复内容**:

| 文件 | 修改 | content_scope 值 |
|------|------|------------------|
| `src/adapters/acled_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |
| `src/adapters/bls_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |
| `src/adapters/china_macro_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |
| `src/adapters/eia_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |
| `src/adapters/fred_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |
| `src/adapters/sanctions_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |
| `src/adapters/treasury_adapter.py` | metadata dict 加 `"content_scope": "MACRO"` | MACRO |

**示例**（`fred_adapter.py`）：
```python
# 修改前
metadata={
    "_structured": True,
    "series_id": series_id,
    ...
}

# 修改后
metadata={
    "_structured": True,
    "content_scope": "MACRO",  # ← 新增
    "series_id": series_id,
    ...
}
```

**预期效果**: 
- TTL 清理正常工作（MACRO 14 天自动清理）
- API 按 scope 查询完整

**工作量**: ~30 分钟

---

### P1 — MENTIONS 边 fact

**问题**: 1309 条 MENTIONS 边的 fact 属性全空。

**调查结论**: **无需修复，设计正确。**

- MENTIONS 边是 `EpisodicEdge` 类，设计上没有 fact 字段，只是结构索引（Episode → Entity，"谁被提到了"）
- RELATES_TO 边是 `EntityEdge` 类，携带 fact/fact_embedding（Entity → Entity，"实体间有什么关系"）
- "为什么提到腾讯" → RELATES_TO 边的 fact 已回答
- 下游消费者（MiroFish、PM Agent、Kronos、SynapseEngine）通过 REST API 获取数据，API 查询路径不经过 MENTIONS 边属性
- 信息链路完整，无缺失

**工作量**: 0

---

### P2 — Gemini JSON 解析失败（杂音来源）

**问题**: Gemini 结构化输出失败 4 次，根因是喂给 LLM 的数据包含 HTML 残留和控制字符（`\t` tab 缩进、HTML 实体 `&nbsp;` 等）。

**杂音进入路径**:
```
网页 HTML → NewsSpider 提取（保留 \t）→ adapter 构建 episode_body 
→ graphiti EpisodicNode.content → Gemini 输入
```

**唯一清洗点** `client.py:98-113` 的行为：
- `\b`（backspace）会被过滤 ✅
- `\t`（tab）被**显式保留** ❌
- HTML 实体（`&nbsp;`、`&#9;`）不会被清理 ❌

**修复内容**:

| 文件 | 修改 |
|------|------|
| `src/utils/content_fetcher.py` | 在 `_spider_result_to_content()` 或 `_extract_content()` 中添加 HTML/控制字符清洗函数 |

**清洗逻辑**:
```python
import re

def _clean_extracted_text(text: str) -> str:
    """Clean HTML artifacts and control characters from extracted text."""
    # Remove HTML entity remnants
    text = re.sub(r'&(nbsp|amp|lt|gt|quot|#\d+);', ' ', text)
    # Remove HTML tag remnants
    text = re.sub(r'<[^>]+>', '', text)
    # Replace tabs and multiple spaces with single space
    text = re.sub(r'[\t]+', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    # Remove other control characters (keep \n for paragraph breaks)
    text = ''.join(c for c in text if ord(c) >= 32 or c == '\n')
    return text.strip()
```

**调用位置**: 在 adapter 构建 `episode_body` 后、创建 `NormalizedEpisode` 前，统一调用清洗函数。

**预期效果**: 
- Gemini 结构化输出成功率提升
- 实体/关系提取更完整
- 减少 token 浪费（不再发送无用控制字符）

**工作量**: ~1 小时

---

### P2 — 实体重复

**问题**: 966 个实体中有大量重复（如 "Bougainville Copper" / "Bougainville Copper Ltd." / "Bougainville Minerals" 等 5 个节点），342/966 实体缺 entity_name。

**根因**: graphiti 去重只看 `EntityNode.name`（LLM 提取的原始显示名），阈值硬编码（Jaccard ≥ 0.9），且短名字跳过模糊匹配。重复模式：
- 公司后缀差异（~30%）
- 中英文差异（~20%）
- 缩写 vs 全称（~15%）
- LLM 提取不一致（~25%）

**修复方案**: **Adapter 层规范化 + Episode Body 约束注入**（源头解决，不依赖 graphiti 内部机制）

| 文件 | 修改 |
|------|------|
| `src/utils/entity_canonical.py`（新建） | 公司后缀白名单去除 + 中英文映射表 + `canonical_name()` 函数 |
| `data/canonical_entities.yaml`（新建） | 高频实体 canonical name 映射表（从 Neo4j 现有实体中提取） |
| `src/adapters/models.py` | EntityItem 构造时调用 `canonical_name()` 统一名称 |
| `src/graphiti/episode_writer.py` | `_build_extended_body()` 注入约束指令 |
| 各 adapter 的 normalize 方法 | 确保 EntityItem 使用 canonical name |

**canonical_name 逻辑**:
```python
# 公司后缀白名单去除
CORPORATE_SUFFIXES = {
    'ltd', 'ltd.', 'limited', 'inc', 'inc.', 'corp', 'corp.',
    'co', 'co.', 'plc', 'llc', 'lp', 'sa', 'ag', 'se',
    '控股', '有限公司', '股份有限公司', '集团', '公司',
}

# 中英文映射表（高频实体）
EN_ZH_MAP = {
    'tencent': '腾讯控股',
    'alibaba': '阿里巴巴',
    'ping an': '中国平安',
    'china mobile': '中国移动',
    # ... 可扩展
}

def canonical_name(name: str, entity_type: str) -> str:
    cleaned = name.strip()
    lower = cleaned.lower()
    
    # 1. 中英文映射
    if lower in EN_ZH_MAP:
        return EN_ZH_MAP[lower]
    
    # 2. 去除公司后缀
    tokens = cleaned.split()
    while tokens and tokens[-1].lower().rstrip('.') in CORPORATE_SUFFIXES:
        tokens.pop()
    if tokens:
        cleaned = ' '.join(tokens)
    
    # 3. 空白标准化
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned
```

**Episode Body 约束注入**:
```python
def _build_extended_body(episode: NormalizedEpisode) -> str:
    if not episode.entities:
        return episode.episode_body
    
    lines = [
        "\n[END OF CONTENT]",
        "",
        "ENTITY RESOLUTION RULES:",
        "1. Use EXACTLY the names listed below, character for character",
        "2. Do NOT add or remove suffixes (Ltd, Inc, Corp, 控股, etc.)",
        "3. Always use Chinese names when provided",
        "4. If the same entity appears with different names, use the FIRST name listed",
        "",
        "CANONICAL ENTITY NAMES:",
    ]
    for ent in episode.entities:
        if ent.ticker:
            lines.append(f"- {ent.name} ({ent.ticker})")
        else:
            lines.append(f"- {ent.name}")
    
    return episode.episode_body + "\n".join(lines)
```

**预期效果**: 
- 在数据进入 graphiti 之前就统一名字，减少重复
- entity_name 缺失率降低
- 不依赖 graphiti 内部机制，不需要 Cypher 后处理

**工作量**: ~2 天（含测试）

---

## 修复优先级与时间线

| 优先级 | 问题 | 工作量 | 依赖 | 计划 |
|--------|------|--------|------|------|
| **P0** | 日志语义修正 | ~1h | 无 | Day 1 上午 |
| **P1** | content_scope 缺失 | ~30min | 无 | Day 1 上午 |
| **P1** | MENTIONS fact | 0 | 无需修复 | — |
| **P2** | Gemini 解析失败 | ~1h | 无 | Day 1 下午 |
| **P2** | 实体重复 | ~2 天 | 无 | Day 2-3 |

**总工作量**: ~3 天

---

## 验收标准

### P0 — 日志语义修正
- [ ] LOG_LEVEL=WARNING 时，WARNING 数量从 513 降到 ~50
- [ ] 真正的问题（API 失败、parse error、pipeline 异常）仍然可见
- [ ] 第三方噪音（neo4j property warnings、scrapling INFO）被抑制

### P1 — content_scope 缺失
- [ ] 7 个 adapter 的 metadata 中包含 `"content_scope": "MACRO"`
- [ ] TTL 清理正常工作（MACRO 14 天自动清理）
- [ ] API 按 scope 查询完整（`/api/events?scope=MACRO` 返回所有宏观事件）

### P2 — Gemini 解析失败
- [ ] ContentFetcher 层添加 HTML/控制字符清洗
- [ ] Gemini 结构化输出成功率提升（从 96% → 99%+）
- [ ] 日志中不再出现 `Failed to parse structured response: Unterminated string` 错误

### P2 — 实体重复
- [ ] 新建 `entity_canonical.py` 和 `canonical_entities.yaml`
- [ ] 各 adapter 使用 canonical name
- [ ] Episode body 注入约束指令
- [ ] 重跑一批 episode 后，重复实体数量下降 50%+
- [ ] entity_name 缺失率从 35% 降到 10% 以下

---

## 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 日志级别调整可能遗漏真正的问题 | 先在测试环境跑一个完整 cycle，人工审查 WARNING 列表 |
| content_scope 修改可能影响现有数据 | 修改后重跑一次全量 ingestion，刷新现有 episode 的 metadata |
| 实体 canonical name 映射表需要维护 | 初期只覆盖高频实体（Top 100），后续逐步扩展 |
| Episode body 约束注入可能影响 LLM 提取质量 | 小批量测试（10 episodes），对比修改前后的实体提取质量 |

---

## 不在本次修复范围

- **P3 — 抓取降级耗时优化**: 方案待讨论（Camoufox 单 session 复用 vs 多 session 并行）
- **MENTIONS 边 fact**: 设计正确，无需修复
- **Tier 2 (CloakBrowser) 评估**: 成功率 30-40%，耗时 5-15s，表现不佳，待评估是否移除

---

**审批**: Boss 确认后开始执行
