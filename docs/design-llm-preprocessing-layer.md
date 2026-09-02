# Design: LLM Preprocessing Layer for GDELT Adapter

**Status**: Draft  
**Date**: 2026-09-02  
**Author**: Ling Xi  

---

## 1. Problem Statement

### 1.1 Context Overflow in Graphiti

Graphiti 的 entity resolution 步骤（`_resolve_with_llm`）会将当前 episode + 最近 10 条 previous episodes 拼接成 prompt。当 episode 内容过长时，prompt 超过本地 LLM 的 32K token 限制，导致 ingestion 失败。

**数据支撑**：
- 523 条 episodes，平均 3,411 chars，最大 23,202 chars
- 最大 10 条 episodes 合计 140,885 chars (~47K tokens) → 超限
- 失败的 episode prompt 大小：33K-39K tokens

### 1.2 Fetch 失败时 episode 质量低

当 source URL fetch 失败时，GDELT adapter fallback 到 codebook 模板拼接：

```markdown
## GDELT Events Report
**Date**: 2026-08-25
**Event**: MAKE PUBLIC STATEMENT (CAMEO 01)
**Actors**: China → United States
**Goldstein Score**: +4.0 (Cooperative)
**Tone**: +2.3
**Sources**: 1. https://...
```

问题：
- 模板拼接缺乏语义关联（"因为 X 所以 Y"）
- 主题翻译质量差（"Taxonomy - FncAct" 对 graphiti 提取无帮助）
- Graphiti LLM 从这种结构化模板中提取的实体/关系质量有限

### 1.3 当前流程

#### GDELT Adapter

```
┌─────────────────────────────────────────────────────────────────┐
│ GDELT Adapter                                                   │
│                                                                 │
│  source_url → fetch ─┬─ 成功 → 原文直接写入 JSON                │
│                      │                                          │
│                      └─ 失败 → codebook 模板拼接写入 JSON        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    Graphiti LLM 提取实体/关系
```

**问题**：
- Fetch 成功：原文可能 20K+ chars，直接塞给 graphiti → context overflow
- Fetch 失败：codebook 模板信息密度低 → 提取质量差

#### RSS Adapter

```
┌─────────────────────────────────────────────────────────────────┐
│ RSS Adapter                                                     │
│                                                                 │
│  feedparser → {title, link, summary}                            │
│                                                                 │
│  link → ContentFetcher ─┬─ 成功 → 原文直接写入 JSON             │
│                         │                                       │
│                         └─ 失败 → RSS feed 自带 summary 写入    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    Graphiti LLM 提取实体/关系
```

**问题**：
- Fetch 成功：原文可能 5K-20K chars → context overflow（同 GDELT）
- Fetch 失败：fallback 是出版商写的自然语言摘要（100-500 chars），**质量尚可，不需要 LLM 整合**

#### GDELT vs RSS Fetch 失败对比

| | GDELT | RSS |
|---|---|---|
| **Fallback 内容** | Codebook 模板拼接（CAMEO code + actors + themes + Goldstein） | RSS feed 自带的 summary（出版商写的自然语言） |
| **质量** | 低 — "Taxonomy - FncAct" 这种结构化标签 | 还行 — 已经是自然语言 |
| **长度** | ~300-500 chars | ~100-500 chars |
| **需要 LLM 整合？** | **是** — 结构化 → 自然语言 | **不需要** — 已经是自然语言 |
| **有 Codebook？** | 有（CAMEO/actor/theme 翻译） | **没有** |

---

## 2. Proposed Solution

### 2.1 核心思路

在 adapter 写入 JSON 之前，增加一层 LLM 预处理：

```
┌─────────────────────────────────────────────────────────────────┐
│ GDELT Adapter                                                   │
│                                                                 │
│  source_url → fetch ─┬─ 成功 → 原文                             │
│                      │         ↓                                │
│                      │    LLM 压缩（如果 >5K chars）             │
│                      │         ↓                                │
│                      │    压缩后写入 JSON                        │
│                      │                                          │
│                      └─ 失败 → codebook 模板                    │
│                                ↓                               │
│                           LLM 整合成自然语言摘要                 │
│                                ↓                               │
│                           摘要写入 JSON                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                    Graphiti LLM 提取实体/关系
```

### 2.2 两种处理模式

| 模式 | 触发条件 | 输入 | 输出 | 适用 Adapter | 目标 |
|------|---------|------|------|-------------|------|
| **压缩模式** | Fetch 成功 & len > 5K | 原文 | 2-3K 压缩摘要 | GDELT + RSS | 解决 context overflow |
| **整合模式** | Fetch 失败 & 内容是结构化模板 | codebook 模板 + metadata | 1-2K 自然语言摘要 | **仅 GDELT** | 提升提取质量 |

> **注意**：RSS fetch 失败的 fallback 是出版商写的自然语言摘要，不是结构化模板，因此不需要整合模式。RSS 只需要压缩模式。

### 2.3 阈值设计

- **压缩阈值**: 5,000 chars (~1,700 tokens)
  - 低于此值：原文直接写入，不压缩
  - 高于此值：LLM 压缩到 2,000-3,000 chars
  - 理由：10 条 episodes × 3K chars = 30K chars ≈ 10K tokens，加上 system prompt + entities，不会超 32K

- **不压缩下限**: 500 chars
  - 低于此值：内容太短，压缩无意义

---

## 3. Design Details

### 3.1 组件位置

新增模块：`src/adapters/llm_preprocessor.py`

```
src/adapters/
├── llm_preprocessor.py      # NEW: LLM 预处理层
├── gdelt_adapter.py         # 调用 llm_preprocessor
├── gdelt_codebook.py        # 不变
└── ...
```

### 3.2 接口设计

```python
class LLMPreprocessor:
    """LLM-based episode content preprocessor."""
    
    def __init__(self, llm_client: LLMClient, config: PreprocessorConfig):
        self.llm = llm_client
        self.config = config
    
    async def preprocess(
        self,
        content: str,
        metadata: dict,
        mode: Literal["compress", "synthesize"]
    ) -> str:
        """
        Preprocess episode content before writing to JSON.
        
        Args:
            content: Original content (full text or codebook template)
            metadata: Episode metadata (date, actors, CAMEO code, etc.)
            mode: 
                - "compress": Compress long article text
                - "synthesize": Synthesize natural language from structured data
        
        Returns:
            Preprocessed content (2-3K chars for compress, 1-2K for synthesize)
        """
        ...
```

### 3.3 Prompt 设计

#### 压缩模式 Prompt

```python
COMPRESS_PROMPT = """You are a news content compressor. Given the following article, 
produce a concise summary that preserves:

1. All entity names (people, organizations, countries, companies)
2. All numbers, dates, and quantitative data
3. Key factual claims and causal relationships
4. The core event/action described

Output requirements:
- Maximum 2000 characters
- English language
- Maintain factual accuracy — do not infer or hallucinate
- Preserve direct quotes when they contain critical information

---
ARTICLE:
{content}
---

Compressed summary:"""
```

#### 整合模式 Prompt

```python
SYNTHESIZE_PROMPT = """You are a news analyst. Given the following structured metadata 
about a geopolitical/economic event, produce a natural language summary.

Metadata:
{metadata}

Structured content:
{content}

Requirements:
- Write 1-2 paragraphs (800-1500 characters)
- English language
- Describe WHAT happened, WHO is involved, WHEN and WHERE it occurred
- Explain the significance or potential impact if apparent
- Use natural flowing prose, not bullet points
- Do not invent facts not present in the metadata

Natural language summary:"""
```

### 3.4 调用点

#### GDELT Adapter（压缩 + 整合）

修改 `gdelt_adapter.py` 的 `_normalize_event_record` 和 `normalize` 方法：

```python
async def _normalize_event_record(
    self,
    record: dict,
    fetch_results: dict[str, ContentResult] | None = None,
) -> NormalizedEpisode:
    # ... existing fetch logic ...
    
    if full_text:
        pure_text, yaml_meta = strip_yaml_front_matter(full_text)
        
        # NEW: LLM compression for long content
        if self._llm_preprocessor and len(pure_text) > self._compress_threshold:
            body = await self._llm_preprocessor.preprocess(
                content=pure_text,
                metadata={"source_url": source_url, "event_date": event_record.event_date},
                mode="compress"
            )
        else:
            body = pure_text
            
    else:
        # Fetch failed: codebook template
        template_body = _build_event_episode_body(event_record, resolved_urls)
        
        # NEW: LLM synthesis from structured data
        if self._llm_preprocessor:
            metadata = {
                "event_date": event_record.event_date,
                "cameo_code": event_record.cameo_code,
                "cameo_description": translate_cameo(event_record.cameo_code),
                "actor1": event_record.actor1_name or event_record.actor1_code,
                "actor2": event_record.actor2_name or event_record.actor2_code,
                "goldstein": event_record.goldstein_scale,
                "tone": event_record.avg_tone,
            }
            body = await self._llm_preprocessor.preprocess(
                content=template_body,
                metadata=metadata,
                mode="synthesize"
            )
        else:
            body = template_body
    
    # ... rest of the method ...
```

#### RSS Adapter（仅压缩）

修改 `rss_adapter.py` 的 `normalize` 方法：

```python
async def normalize(self, record, fetch_results=None):
    # ... existing fetch logic ...
    
    if full_text:
        pure_text, yaml_meta = strip_yaml_front_matter(full_text)
        
        # NEW: LLM compression for long content (same as GDELT)
        if self._llm_preprocessor and len(pure_text) > self._compress_threshold:
            episode_body = await self._llm_preprocessor.preprocess(
                content=pure_text,
                metadata={"title": title, "source_url": link},
                mode="compress"
            )
        else:
            episode_body = _build_episode_body(title, pure_text)
            
    else:
        # Fetch failed: RSS feed summary — already natural language, no synthesis needed
        episode_body = _build_episode_body(title, summary)
    
    # ... rest of the method ...
```

### 3.5 配置

```python
@dataclass
class PreprocessorConfig:
    compress_threshold: int = 5000  # chars
    compress_target: int = 2500     # chars
    synthesize_target: int = 1500   # chars
    enable_compression: bool = True
    enable_synthesis: bool = True
    timeout_seconds: float = 30.0
    max_retries: int = 2
```

通过环境变量控制（复用现有 `.env` 机制），**默认关闭**，需显式开启：

```bash
# ── LLM Preprocessor ──
# 功能开关（默认关闭）
LLM_PREPROCESSOR_ENABLED=false

# 以下配置仅在 ENABLED=true 时生效
LLM_PREPROCESSOR_ENDPOINT=http://192.168.0.27:8080/v1
LLM_PREPROCESSOR_MODEL=gemma-4-12b
LLM_PREPROCESSOR_COMPRESS_THRESHOLD=5000
LLM_PREPROCESSOR_COMPRESS_TARGET=2500
LLM_PREPROCESSOR_SYNTHESIZE_TARGET=1500
LLM_PREPROCESSOR_TIMEOUT=30
```

在 `src/core/config.py` 的 `Settings` 中添加：

```python
# LLM Preprocessor (opt-in, default off)
llm_preprocessor_enabled: bool = False
llm_preprocessor_endpoint: str = "http://192.168.0.27:8080/v1"
llm_preprocessor_model: str = "gemma-4-12b"
llm_preprocessor_compress_threshold: int = 5000
llm_preprocessor_compress_target: int = 2500
llm_preprocessor_synthesize_target: int = 1500
llm_preprocessor_timeout: float = 30.0
```

Adapter 初始化时检查开关：

```python
# gdelt_adapter.py / rss_adapter.py
if settings.llm_preprocessor_enabled:
    self._llm_preprocessor = LLMPreprocessor(
        endpoint=settings.llm_preprocessor_endpoint,
        model=settings.llm_preprocessor_model,
        ...
    )
else:
    self._llm_preprocessor = None  # 走原有逻辑，零影响
```

---

## 4. Implementation Plan

### Phase 1: Core Infrastructure

1. 创建 `src/adapters/llm_preprocessor.py`
2. 实现 `LLMPreprocessor` 类
3. 实现压缩和整合两个 prompt
4. 添加错误处理（LLM 失败时 fallback 到原始行为）

### Phase 2: GDELT Integration

5. 修改 `GdeltAdapter.__init__` 接受 `llm_preprocessor` 参数
6. 修改 `_normalize_event_record` 调用预处理（压缩 + 整合）
7. 修改 `normalize` (GKG path) 调用预处理（压缩 + 整合）
8. 添加配置加载

### Phase 2b: RSS Integration

9. 修改 `RssAdapter.__init__` 接受 `llm_preprocessor` 参数
10. 修改 `normalize` 调用预处理（仅压缩模式）
11. RSS fetch 失败时不做整合（fallback 已是自然语言）

### Phase 3: Testing

12. 单元测试：压缩模式、整合模式、fallback 逻辑
13. RSS 单元测试：仅压缩模式、RSS summary 不触发整合
14. 集成测试：端到端 pipeline with LLM preprocessing
15. 性能测试：延迟增加评估

### Phase 4: Validation

16. 对比实验：
    - 处理同一批 GDELT + RSS episodes
    - 对比 graphiti 提取的实体/关系质量
    - 对比 context overflow 发生率

---

## 5. Risks and Mitigations

### 5.1 延迟增加

| 场景 | 预估延迟 | 影响 |
|------|---------|------|
| LLM 压缩 | 5-15s / episode | 可接受（batch 处理） |
| LLM 整合 | 3-8s / episode | 可接受 |
| LLM 超时/失败 | fallback 到原始行为 | 无影响 |

**缓解措施**：
- 只对 >5K chars 的内容做压缩（大部分 episodes <5K）
- 异步批量处理
- LLM 失败时 fallback 到原始行为（graceful degradation）

### 5.2 信息损失

压缩可能丢失 graphiti 本来能提取的细节。

**缓解措施**：
- Prompt 明确要求保留实体、数字、日期
- 压缩目标 2-3K chars，不是极端压缩
- 保留原文在 metadata 字段（可选，用于追溯）

### 5.3 LLM 幻觉

整合模式可能引入不存在的 facts。

**缓解措施**：
- Prompt 明确禁止 invent facts
- 整合输入是 codebook 模板（已有事实），LLM 只是改写格式
- 对比实验验证幻觉率

### 5.4 成本

本地 LLM 推理消耗算力。

**缓解措施**：
- 复用现有 llama-server（Gemma 4 12B）
- 只对 GDELT 管线启用（其他 adapter 暂不需要）
- 压缩阈值可调（提高阈值 = 更少调用）

---

## 6. Success Criteria

1. **Context overflow 消除**: ingestion 失败率从 ~15% 降到 <1%
2. **提取质量不降**: 对比实验显示实体/关系提取数量 ≥ 原始方案
3. **延迟可接受**: 平均每条 episode 处理时间增加 <10s
4. **Graceful degradation**: LLM 失败时自动 fallback，不影响 pipeline 稳定性

---

## 7. Future Extensions

1. **扩展到其他 adapter**: CLS、EastMoney 等有 content fetch 的 adapter
2. **自适应阈值**: 根据 graphiti 反馈动态调整压缩阈值
3. **增量压缩**: 对 previous_episodes 也做压缩，进一步减少 context
4. **缓存**: 相同内容的压缩结果缓存复用

---

## 8. Open Questions

1. **是否需要保留原文?** 压缩后是否把原文存到 metadata 字段用于追溯？
2. **阈值调优**: 5K 是否最优？需要实验验证
3. **Batch vs Single**: 批量调用 LLM 还是逐条调用？（取决于 llama-server 的 batch 能力）

---

## Appendix A: 数据流对比

### GDELT — Before

```
GDELT CSV → parse → fetch ─┬─ success → raw article (20K chars) → JSON → Graphiti
                           └─ fail → codebook template (500 chars) → JSON → Graphiti
```

### GDELT — After

```
GDELT CSV → parse → fetch ─┬─ success → raw article ─┬─ >5K → LLM compress (2.5K) → JSON → Graphiti
                           │                        └─ ≤5K → as-is → JSON → Graphiti
                           │
                           └─ fail → codebook template → LLM synthesize (1.5K) → JSON → Graphiti
```

### RSS — Before

```
RSS Feed → feedparser → fetch ─┬─ success → raw article (10K chars) → JSON → Graphiti
                               └─ fail → feed summary (200 chars) → JSON → Graphiti
```

### RSS — After

```
RSS Feed → feedparser → fetch ─┬─ success → raw article ─┬─ >5K → LLM compress (2.5K) → JSON → Graphiti
                               │                        └─ ≤5K → as-is → JSON → Graphiti
                               │
                               └─ fail → feed summary (200 chars) → as-is → JSON → Graphiti
                                          （已是自然语言，不需要 LLM 整合）
```

## Appendix B: 预估影响

| 指标 | Before | After (预估) |
|------|--------|-------------|
| 平均 episode 大小 | 3,411 chars | ~2,500 chars |
| 最大 episode 大小 | 23,202 chars | ~3,000 chars |
| 10 条 episodes 合计 | 25K-140K chars | ~25K chars |
| Context overflow 率 | ~15% | <1% |
| GDELT fetch 失败 episode 质量 | 结构化模板 | 自然语言摘要 |
| RSS fetch 失败 episode 质量 | 出版商摘要（已可用） | 不变（不需要 LLM） |
