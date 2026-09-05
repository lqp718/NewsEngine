# NewsEngine GDELT 接入方案重构评审

**评审人**: Architect (Chief Architect)  
**日期**: 2026-06-28  
**版本**: V1.1 (最终评审)  
**项目**: NewsEngine (`D:\MyWallet\NewsEngine`)  
**项目目标**: 独立本地新闻情报引擎，编织"宏观↔宏观、宏观↔个股、个股↔个股"的事件网络，作为 MiroFish（行业推演）和 PM Agent（决策）的输入原材料，用于港股投资决策。  
**消费者**: MiroFish / PM Agent / Kronos  
**参考文档**: `NEWSENGINE-DESIGN-DOC.md` V2.2  
**评审结论**: ✅ **通过（条件性通过）**

---

## 评审结论

| 维度 | 结论 | 条件 |
|------|------|------|
| 方案正确性 | ✅ 通过 | — |
| 与原始需求对齐 | ✅ 通过 | — |
| 架构一致性 | ✅ 通过 | 微调 1 处（§3.6.2 过滤源声明） |
| 实施可行性 | ✅ 通过 | 见改造清单 |
| 风险可控性 | ✅ 通过 | 见降级策略 |

**最终裁决：通过，有 3 个必须修复的修改点 + 1 个强烈建议。**

---

## 一、现状诊断（确认）

### 1.1 五个核心问题全部确认存在

| # | 问题 | 严重程度 | 确认 |
|---|------|---------|------|
| 1 | 只用 GKG CSV，Events 和 Mentions 被忽略 | CRITICAL | ✅ |
| 2 | Themes 分类码被当作 Entity 喂给 Graphiti | CRITICAL | ✅ |
| 3 | episode_body 含未翻译 Themes | HIGH | ✅ |
| 4 | 缺少 Codebook 翻译层 | CRITICAL | ✅ |
| 5 | Events CSV 完全未使用 | CRITICAL | ✅ |

**证据**（源码 `gdelt_adapter.py`）：
- `fetch_lastupdate()` 只取 `lines[2]`（GKG 行），`lines[0]`（Events）和 `lines[1]`（Mentions）完全忽略
- `_parse_entities_from_record()` 将 Themes（如 `WB_698_TRADE`）作为 `EntityItem(type="theme", name=...)` 喂给 Graphiti
- `_build_episode_body()` 直接拼接原始 Themes 码
- 没有任何 Codebook 翻译逻辑

---

## 二、与原始设计文档的 Gap 分析

### 2.1 全面对齐检查表

| 原始需求（V2.2） | 新方案是否满足 | 说明 |
|-----------------|--------------|------|
| §1.2 架构图："GDELT CSV 每 15 分钟 GKG+Events" | ✅ | 三 CSV 全用 |
| §1.4.2 双管线架构："GDELT CSV → theme 过滤" | ⚠️ | 见修改点 #1 |
| §2.9 content_scope："GDELT 标记为 MACRO" | ✅ | 不变 |
| §3.6.2 19 核心宏观主题白名单 | ⚠️ | 见修改点 #1 |
| §3.2 模块职责矩阵 GdeltAdapter | ✅ | 职责扩展（新方案覆盖） |
| §3.1 文件架构 | ✅ | 需新增 3 个文件 |
| §2.8 Schema 映射表 | ✅ | 不变 |
| §3.6.5 content_scope 写入链路 | ✅ | 不变 |
| §6.6 TTL 分级淘汰 | ✅ | 不变 |
| "编织宏观↔宏观、宏观↔个股、个股↔个股的事件网络" | ✅ | Events CSV 提供事件骨架 |
| "作为 MiroFish 和 PM Agent 的输入原材料" | ✅ | REST API 输出格式不变 |
| "进行行业热点及个股走势的预测" | ✅ | 事件网络+情感评分+传播追踪 |

### 2.2 发现 1 个 Gap：过滤源声明与实际方案不完全一致

**原始设计 §3.6.2：**
> "匹配对象为 GKG V2.8 Themes 列"

**新方案：**
> "过滤的应该是 Events CSV 的 CAMEO 事件码 + 翻译后的主题名"

这个差异是**合理的演进**，但需要明确记录下来。GKG Themes 过滤存在两个问题：
1. CAMEO 事件码（如 `012`=外交磋商）不经过 GKG Themes 过滤——这些是结构化事件，由 Events CSV 直接提供，无需额外过滤
2. GKG Themes 过滤（如 `WB_698_TRADE`）是当前已工作的逻辑，保留它作为三层过滤的一环

**建议的修正方案**：三层过滤，而非单层

```
Layer A: Events CSV CAMEO 码白名单过滤（新增）
  → 只保留 CAMEO 中包含金融相关事件码的记录（如 012/057/064/082...）
  → 对应原始设计 §1.4.2 "theme 过滤"

Layer B: GKG Themes 子串匹配过滤（保留现有）
  → 19 核心金融主题 OR 匹配
  → 对应原始设计 §3.6.2

Layer C: Graphiti 语义去重（不变）
  → 同一事件多信源合并
```

**结论：这不构成阻塞 gap，只需在原始设计文档中更新 §3.6.2 的描述。**

---

## 三、新方案逐项评审

### 3.1 三 CSV 策略：✅ 正确

| CSV | 角色 | 评审意见 |
|-----|------|---------|
| **Events** | 事件骨架（Actor1→Actor2→CAMEO→Goldstein→地理） | ✅ 正确。这正是"谁对谁做了什么"的结构化事实 |
| **Mentions** | 传播覆盖层（Event→报道URL→置信度） | ✅ 正确。提供了"这个事件被多少媒体报道了"的量化指标 |
| **GKG** | NLP 语义元数据（主题/实体/情感） | ✅ 正确。但不是事件骨架，而是事件的元数据增强 |

### 3.2 Codebook 翻译策略：✅ 正确

| Codebook | 需要性 | 评审 |
|----------|--------|------|
| CAMEO 事件码 | 最高 | ✅ 必须。Events CSV 的列 26-28 是 EventBaseCode/EventRootCode/CAMEOEventCode |
| Theme 分类码 | 高 | ✅ 已有资源（59K 行 LOOKUP-GKGTHEMES.TXT） |
| Actor 代码 | 高 | 需要。Actor1Code/Actor2Code 如 `DEU`、`UKR` 需翻译 |

**关键决策确认：翻译层放在 adapter 层。** 这完全正确——这是数据规范化问题，不是语义理解问题，不应留给 Graphiti 的 LLM 处理。

### 3.3 CAMEO → 关系映射策略：✅ 正确

**不预映射，让 LLM 做语义判断。** 理由充分：
- CAMEO 有 ~300 个码，硬编码维护成本高
- LLM 看到翻译后的人类可读文本后能更好判断关系类型
- 设计文档定义的关系类型（AFFECTS/CAUSED_BY/MITIGATES）是语义层面，不是 CAMEO 码层面

### 3.4 架构分层设计：✅ 正确

六步数据流（下载→解析→Codebook翻译→合并→过滤→归一化）清晰、职责分明。

### 3.5 文件组织：✅ 正确

新增文件放在 `src/adapters/` 内，符合依赖铁律：
- 不污染 Graphiti 层
- adapter 不依赖 graphiti
- 共享类型通过 `adapters/models.py` 桥接

---

## 四、必须修改点（3 个）

### 修改点 #1：GKG Themes 过滤逻辑需要保留并扩展

**当前状态**（评审报告建议）：将过滤从 GKG Themes 改为 Events CAMEO 码
**正确做法**：两者都做，分层过滤

在 `filter_relevant()` 中增加 CAMEO 码过滤（新增），同时保留 GKG Themes 过滤（现有）：

```python
def filter_relevant(self, records: list[dict]) -> list[dict]:
    """两层宏观过滤。"""
    if not self._macro_theme_keywords:
        return records
    
    matched = []
    for rec in records:
        # Layer A: GKG Themes 子串匹配（现有逻辑，保留）
        themes_text = (rec.get("themes") or "").lower()
        theme_match = any(kw.lower() in themes_text for kw in self._macro_theme_keywords)
        
        # Layer B: CAMEO 码匹配（新增，基于 codebook 翻译后的中文标签）
        cameo_code = rec.get("event_code", "")
        cameo_match = cameo_code in self._macro_cameo_codes
        
        if theme_match or cameo_match:
            matched.append(rec)
    
    return matched
```

**同时更新原始设计文档 §3.6.2**：明确标注过滤源为两者（GKG Themes + Events CAMEO）。

### 修改点 #2：Themes 不再作为 Entity，但需要桥梁字段

**当前问题**：`_parse_entities_from_record()` 将 Themes 作为 `EntityItem(type="theme")` 写入 Graphiti。

**修改方案**：
```python
# gdelt_adapter.py 中的 _parse_entities_from_record()

# 不再这样做：
# entities.append(EntityItem(type="theme", name=theme_name))  # ❌ 删除

# 改为在 episode_body 中以翻译后的文本描述：
# "涉及主题: 国际贸易, 货币政策, 关税壁垒"
# 以及在 keywords 中保留（已翻译）：
keywords.append(translate_theme(theme_name))
```

**理由**：Themes 不是实体（Entity），而是分类标签。keywords 字段已经是设计文档定义的接口字段，用于存储分类标签是合理的。

### 修改点 #3：Episode body 中的 Events/Mentions 信息必须结构化

**当前**：`episode_body` 只有 GKG Themes/Source
**需要**：加入 Events CSV 的事件骨架 + Mentions 的传播信息 + GKG 的情感/主题元数据

**目标格式**（评审报告 §4.4 的建议完全正确，无需改动）：
```
事件: 德国(DEU) → 乌克兰(UKR) → 提供物资援助(CAMEO:057)
日期: 2026-06-28 | 情感(Goldstein): -0.4
地点: 基辅 (50.43, 30.52)
传播: 被5篇文章报道 (mirror.co.uk, bbc.com, ...)
涉及主题: 武装冲突, 国际援助
新闻情感(Tone): -2.3
来源: https://...
```

---

## 五、强烈建议（1 个）

### 建议 #1：保留 GKG-only 作为降级路径

当 Events/Mentions CSV 下载失败时，不应让整个 GDELT pipeline 宕机。保留当前 GKG-only 逻辑作为降级：

```python
async def fetch(self, **kwargs):
    try:
        # 优先：三 CSV 全量
        events_url, mentions_url, gkg_url = self.fetch_lastupdate_full()
        events_records = self.fetch_parse_events(events_url)
        mentions_records = self.fetch_parse_mentions(mentions_url)
        gkg_records = self.fetch_parse_gkg(gkg_url)
        merged = self.merge_event_data(events_records, mentions_records, gkg_records)
    except Exception as e:
        logger.warning("三CSV全量获取失败: %s，降级为GKG-only", e)
        gkg_url = self.fetch_lastupdate_gkg_only()
        gkg_records = self.fetch_parse_gkg(gkg_url)
        merged = self.gkg_to_normalized(gkg_records)  # 当前逻辑作为降级
    return merged
```

**理由**：GDELT 的 Events CSV 数据量大（每 15 分钟 590 条），下载超时风险高于 GKG。降级路径保证在极端情况下至少 GKG 管线不受影响。

---

## 六、设计文档修改清单

### 6.1 需要修改的原始文档部分

| 位置 | 修改内容 | 修改类型 |
|------|---------|---------|
| §1.4.2 双管线架构图 | "GDELT CSV → theme 过滤" 改为 "GDELT CSV → CAMEO 码+Theme 双重过滤" | 修改 |
| §3.6.2 GDELT 宏观主题白名单 | 匹配对象从仅 "GKG V2.8 Themes 列" 改为 "GKG Themes 列 + Events CAMEO 码（翻译后）" | 修改 |
| §3.2 模块职责矩阵 | GdeltAdapter 职责增加 "Events/Mentions/GKG 三 CSV 整合 + Codebook 翻译" | 修改 |
| §3.1 文件架构 | 新增 `gdelt_codebook.py`, `gdelt_events_parser.py`, `gdelt_mentions_parser.py` | 新增 |
| §3.6.2 `filter_relevant()` 代码 | 增加 CAMEO 码过滤分支 | 修改 |
| §8.3 main.py 编排逻辑 | GDELT fetch 增加降级路径 | 修改 |

### 6.2 不需要修改的部分

- §1 架构变更说明：不变
- §2 REST API 契约（全部）：不变
- §2.8 Schema 映射表：不变
- §2.9 content_scope：不变（GDELT 仍标记为 MACRO）
- §3.3 模块依赖图：不变（新增文件都在 adapter 层，符合铁律）
- §3.4 生命周期管理：不变
- §3.5 core/graphiti_client.py：不变
- §3.6.3 RSS 零过滤：不变
- §3.6.4 数据量三层防御：不变（需增加三 CSV 数据量评估）
- §3.6.5 content_scope 写入链路：不变
- §4 配置与测试：不变（需新增 CAMEO Codebook 路径配置）
- §5 sector_briefing：不变
- §6 部署要求：不变
- §7 MongoDB Schema：不变
- §8 N4 实施与验收：不变

**总计修改量**：6 处修改/新增，改动范围集中在 adapter 层 + §1.4.2 架构图 + §3.6.2 过滤说明。

---

## 七、实施改造清单

### 7.1 新增文件（3 个）

| 文件 | 职责 | 预估代码量 |
|------|------|-----------|
| `src/adapters/gdelt_codebook.py` | Codebook 加载与翻译（Cameo/Actor/Theme） | ~150 行 |
| `src/adapters/gdelt_events_parser.py` | Events CSV 下载/解析/结构化 | ~100 行 |
| `src/adapters/gdelt_mentions_parser.py` | Mentions CSV 下载/解析/关联 | ~80 行 |

### 7.2 改造文件（1 个）

| 文件 | 改造范围 |
|------|---------|
| `src/adapters/gdelt_adapter.py` | 重构 fetch 流程 + 三 CSV 合并 + Codebook 翻译 + episode_body/entities 重构 |

### 7.3 保留不变文件

| 文件 | 说明 |
|------|------|
| `src/adapters/macro_themes.py` | 19 核心主题白名单保留，扩展为同时用于 CAMEO 码映射 |
| `src/adapters/base.py` | 不变 |
| `src/adapters/models.py` | 不变（NormalizedEpisode 字段足够） |
| `src/graphiti/*` | 全部不变 |
| `src/api/*` | 全部不变 |

### 7.4 工作量估计

| 任务 | 预估时间 |
|------|---------|
| Codebook 文件准备（Actor 码表 + CAMEO 码表下载） | 1 小时 |
| `gdelt_codebook.py` 实现 | 2 小时 |
| `gdelt_events_parser.py` 实现 | 1.5 小时 |
| `gdelt_mentions_parser.py` 实现 | 1 小时 |
| `gdelt_adapter.py` 核心重构 | 3 小时 |
| 集成测试 + 清库重跑验证 | 1.5 小时 |
| 设计文档更新 | 0.5 小时 |
| **总计** | **~10.5 小时** |

可一次性完成（老公要求），但建议先做 Codebook → Events 解析 → Adapter 重构 → 测试的顺序推进。

---

## 八、风险与缓解

| 风险 | 严重程度 | 缓解措施 | 状态 |
|------|---------|---------|------|
| CAMEO Codebook 不完整 | MEDIUM | 先覆盖高频码（前 50 个覆盖 80% 事件），低频码 fallback 到原始码 + LLM 推理 | 需执行 |
| Events CSV 单次 590 条过大 | MEDIUM | MACRO 白名单 + CAMEO 码双重过滤，预计 80-180 条/周期 | 已有 |
| 三 CSV EventID 不完全关联 | LOW | LEFT JOIN 策略：Events 为主键，Mentions/GKG 缺失时允许 null | 已有 |
| Codebook 翻译质量 | LOW | 保留原始码 + 翻译文本并存，LLM 在 context 中可同时参考 | 已有 |
| GDELT HTTP 下载不稳定 | MEDIUM | 保留 GKG-only 降级路径（建议 #1） | 需执行 |

---

## 九、附录：Codebook 资源获取

| Codebook | 来源 | 获取方式 | 状态 |
|----------|------|---------|------|
| CAMEO 事件码 | GDELT 官网 | `http://data.gdeltproject.org/documentation/CAMEO.Manual.1.1b3.pdf` 或 `https://www.gdeltproject.org/data/documentation/CAMEO.Manual.1.1b3.pdf` | 🔴 需获取 |
| GKG Theme 码表 | GDELT 官方 | `http://data.gdeltproject.org/api/v2/guides/LOOKUP-GKGTHEMES.TXT` | ✅ 已下载 59K 行 |
| CAMEO Actor 码表 | GDELT 官网 | `https://www.gdeltproject.org/data/lookups/CAMEO_type.txt` 或 `http://data.gdeltproject.org/documentation/` | 🔴 需获取 |

**备选方案**：如果 GDELT 官方 Codebook 因 GFW 无法下载，可使用以下 fallback：
- CAMEO 码表可以从 Wiki 或学术论文附录获取
- Actor 码表（如 DEU=德国、CHN=中国）可以通过 ISO 3166-1 国家代码映射 + 手动补充组织代码

---

## 十、评审签字

| 角色 | 签字 | 日期 |
|------|------|------|
| Architect (评审) | ✅ 条件性通过 | 2026-06-28 |
| 老公 (审批) | 待审批 | — |

**下一步行动**：
1. 老公确认方案后
2. 按实施改造清单（§七）更新原始设计文档（修改清单 §六）
3. 实施改造
4. 清库重跑，验证数据质量改善

---

---

## 十一、补充评审：EventEntity 设计（方案 B）

**评审人**: Architect (Chief Architect)  
**日期**: 2026-06-28  
**版本**: V1.2 (补充评审追加)  
**触发背景**: GDELT Events CSV 的情感分数（Tone、GoldsteinScale）当前未被结构化存储，仅作为 episode_body 的文本描述喂给 Graphiti LLM。老公倾向方案 B（新建 EventEntity type）。

---

### 11.1 问题定义

**当前状态**：GDELT Events CSV 提供三条关键信息：

| 信息 | 当前处理 | 问题 |
|------|---------|------|
| CAMEO 事件骨架（Actor1→Actor2→事件码） | ❌ 未使用 Events CSV | Events CSV 的 57 列数据完全丢弃 |
| Goldstein Scale（合作/冲突指数，-10~+10） | ❌ 未使用 | 可以量化"德国援乌"（+8）vs"俄乌冲突"（-10） |
| Avg Tone（新闻情感，-100~+100） | ❌ 仅作为 GKG tone 的原始数字出现 | GKG tone 与 Events tone 来源不同（前者来自 GKG 文章情感，后者来自事件编码本身） |

**核心矛盾**：方案 B 提议 `EventEntity` 作为新 entity type 加入 MACRO_ENTITY_TYPES。但它与现有的 Country/Organization/Topic/Policy/Sector 的关系需要明确裁决。

---

### 11.2 方案对比

| 维度 | 方案 A（加到 metadata） | 方案 B（新建 EventEntity） |
|------|------------------------|--------------------------|
| 存储位置 | `EpisodicNode.episode_metadata` JSON 字符串 | `EntityNode` (label: `Entity:Event`) |
| tone/goldstein 可查询 | ⚠️ 需要 Cypher 解析 JSON | ✅ 直接作为属性查询 |
| 实体间关系 | ❌ 无法建立 EventEntity ↔ Country 的 RELATES_TO | ✅ 可建立 RELATES_TO 关系 |
| 与现有 entity type 协作 | 隔离：metadata 只属于 Episode | 关联：EventEntity 可被所有查询复用 |
| Graphiti LLM 提取 | 无需提取（数据直接写入 metadata） | 需要 LLM 从 episode_body 文本中提取 |
| 实现复杂度 | 低（adapter 层改一处） | 中（新增 Pydantic 模型 + 多个文件协调修改） |
| 查询能力 | 弱：`WHERE ep.episode_metadata CONTAINS 'goldstein_scale'`（无索引/无法范围查询） | 强：`MATCH (e:Event) WHERE e.goldstein_scale > 5 RETURN e` |

---

### 11.3 裁决：✅ 方案 B 通过，但需要修正

**结论：方案 B 正确，但设计需要微调。**

#### 11.3.1 方案 B 为什么正确

1. **信息密度高**：GDELT Events 的 (Actor1 + Actor2 + CAMEO + Goldstein + Tone) 本身就是结构化事实，比 `episode_body` 文本更精准
2. **查询需求硬**：用户需要"Goldstein > 5 的正面合作事件"、"Tone < -5 的恐慌事件"——这在 metadata JSON 中无法高效实现（Neo4j 不支持 JSON 属性索引）
3. **与现有架构一致**：Policy 也是从文本中提取的 entity type，EventEntity 遵循相同模式
4. **关系建模自然**：`EventEntity: "德国援乌" → AFFECTS → Country: "乌克兰"` — 比 episode_body 文本描述更可计算

#### 11.3.2 方案 A 为什么不可行（硬伤）

`episode_metadata` 存储的是 JSON 字符串。Neo4j 对 JSON 字符串属性：
- 不支持属性索引（无法高效 `WHERE goldstein > 5`）
- 不支持 Cypher 原生属性过滤（需要 `apoc.convert.fromJsonMap` 或手动解析，性能极差）
- 不支持图遍历（无法建立 Entity 间关系）

**方案 A 仅适用于不需要查询/遍历的简单标记（如 `content_scope: MACRO`）。情感分数是需要图查询的结构化数据。**

---

### 11.4 EventEntity 修正设计

老公的原始设计是正确的方向，但需要补充两个关键字段以最大化查询价值：

```python
class EventEntity(BaseModel):
    """事件实体 — 从 GDELT Events CSV 提取的结构化事件。

    用于建模"谁对谁做了什么"的结构化事实。
    与 PolicyEntity 的区别：
    - PolicyEntity 建模政策声明/监管行为（状态机：rumor→announced→…）
    - EventEntity 建模已发生的结构化事件（定量：Goldstein/Tone 评分）

    Neo4j 节点标签: Entity:Event
    """

    entity_name: str = Field(
        ...,
        description=(
            "事件描述，一句话，使用中文。"
            "例如: '德国向乌克兰提供物资援助', '美联储加息25个基点', "
            "'中美贸易谈判取得进展'"
        ),
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），翻译后的中文名。"
                    "例如: '德国', '美联储', '中国'。"
                    "如果无法确定 Actor1，省略。",
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），翻译后的中文名。"
                    "例如: '乌克兰', '市场', '美国'。"
                    "如果无法确定 Actor2，省略。",
    )
    cameo_code: str | None = Field(
        default=None,
        description="CAMEO 事件代码。"
                    "例如: '057' (提供援助), '173' (逮捕/拘留), '012' (外交磋商)。"
                    "如果无法确定 CAMEO 码，省略。",
    )
    goldstein_scale: float | None = Field(
        default=None,
        description=(
            "Goldstein 合作/冲突评分 (-10 ~ +10)。"
            "+10 = 最高合作 (如和平条约)，-10 = 最高冲突 (如宣战)。"
            "来源: GDELT Events CSV GoldsteinScale 列。"
        ),
    )
    tone: float | None = Field(
        default=None,
        description=(
            "新闻语调评分 (-100 ~ +100，已归一化)。"
            "+100 = 完全正面，-100 = 完全负面。"
            "来源: GDELT Events CSV AvgTone 列（已除 100 归一化）。"
            "注意: GDELT 原始值为 -10000~+10000，需要 /100 归一化。"
        ),
    )
    event_date: str | None = Field(
        default=None,
        description="事件日期 (YYYY-MM-DD)，从 GDELT EventDate 推导",
    )
```

**设计要点**：

1. **归一化 tone**：GDELT Events CSV 的 AvgTone 是 -10000~+10000 的整数。除以 100 归一化到 -100~+100，与 Goldstein Scale（-10~+10）在视觉上区分。
2. **所有字段都设为 Optional**：LLM 提取时可能无法精确还原所有字段。字段缺失不影响 EventEntity 节点创建。
3. **entity_name 保持自然语言**：让 LLM 从 episode_body 文本中提取描述，而非从代码拼接。LLM 更擅长从文本中提取，不擅长做代码拼接。
4. **与 PolicyEntity 区分**：
   - `PolicyEntity` 是"政策声明/监管行为"（有状态机：rumor→announced→confirmed→implemented），重点在"说了什么"
   - `EventEntity` 是"结构化事件"（有定量评分：Goldstein/Tone），重点在"做了什么，造成了多大的正面/负面影响"

---

### 11.5 与现有 entity type 的协作关系

```
┌─────────────────────────────────────────────────────────────────┐
│              宏观实体协作关系 — GDELT Events Pipeline              │
│                                                                  │
│  EventEntity (事件骨架)                                           │
│  ├─  event_description: "德国向乌克兰提供物资援助"                   │
│  ├─  goldstein_scale: 8.0                                        │
│  ├─  tone: -4.87                                                 │
│  │                                                               │
│  │ RELATES_TO (AFFECTS)                                          │
│  ├──► Country: "德国"        (Actor1 → 发起方)                    │
│  ├──► Country: "乌克兰"      (Actor2 → 接收方)                    │
│  ├──► Topic: "国际援助"      (CAMEO 057 → LLM 推断主题)           │
│  ├──► Topic: "俄乌冲突"      (事件背景推断)                        │
│  │                                                               │
│  │ RELATES_TO (CAUSED_BY)                                        │
│  └──► EventEntity: "俄乌冲突"  (因果链)                           │
│                                                                  │
│  Country: "乌克兰"                                                │
│  │ RELATES_TO (LOCATED_IN)                                       │
│  └──► Country: "欧洲"                                             │
│                                                                  │
│  Topic: "国际援助"                                                 │
│  │ RELATES_TO (BELONGS_TO)                                       │
│  └──► Sector: "军工"           (宏观主题→行业映射)                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**关键关系**：

| 关系 | 方向 | 示例 |
|------|------|------|
| EventEntity → AFFECTS → Country | 事件对国家的影响 | "物资援助" → AFFECTS → "乌克兰" |
| EventEntity → AFFECTS → Organization | 事件对组织的影响 | "制裁" → AFFECTS → "华为" |
| EventEntity → CAUSED_BY → EventEntity | 事件因果链 | "制裁俄罗斯" → CAUSED_BY → "俄入侵乌" |
| EventEntity → RELATED_TO → Topic | 事件属于什么话题 | "加息" → RELATED_TO → "货币政策" |
| EventEntity → RELATED_TO → Sector | 事件影响什么行业 | "芯片出口管制" → RELATED_TO → "半导体" |

**与现有 5 个 MACRO_ENTITY_TYPES 的关系**：

```
MACRO_ENTITY_TYPES (current):     MACRO_ENTITY_TYPES (proposed):
┌──────────────────────────┐      ┌──────────────────────────┐
│ Country                  │      │ Country                  │ (不变)
│ Organization             │      │ Organization             │ (不变)
│ Topic                    │      │ Topic                    │ (不变)
│ Policy                   │      │ Policy                   │ (不变)
│ Sector                   │      │ Sector                   │ (不变)
└──────────────────────────┘      │ Event                    │ (新增)
                                   └──────────────────────────┘
```

EventEntity 不替代任何现有 type，而是**补充事件维度的定量信息**。现有的 Topic/Policy 是定性，EventEntity 是定量。

---

### 11.6 EventEntity 的 Graphiti 提取逻辑

#### 11.6.1 提取链路

```
GDELT Events CSV 原始数据
  │
  ├── Actor1Code, Actor1Name → Codebook 翻译为中文名
  ├── Actor2Code, Actor2Name → Codebook 翻译为中文名
  ├── EventBaseCode → CAMEO Codebook → 中文事件描述
  ├── GoldsteinScale → 直接存储 (float)
  ├── AvgTone → /100 归一化后存储 (float)
  │
  ▼
adapter 层: _build_episode_body() 重构
  │  将 Events CSV 提取的信息以结构化文本写入 episode_body：
  │
  │  "事件: 德国(DEU) → 乌克兰(UKR) → 提供物资援助(CAMEO:057)
  │   日期: 2026-06-28
  │   Goldstein: 8.0 | Tone: -4.87"
  │
  ▼
Graphiti LLM: entity_types 中包含 EventEntity
  │  LLM 看到 entity_name="Event" 的 JSON Schema，
  │  从 episode_body 文本中提取 event_description/actor1/actor2/cameo_code/goldstein_scale/tone
  │
  ▼
Neo4j: (ent:Entity:Event {
    entity_name: "德国向乌克兰提供物资援助",
    actor1: "德国", actor2: "乌克兰",
    cameo_code: "057",
    goldstein_scale: 8.0, tone: -4.87
  })
```

#### 11.6.2 episode_body 重构（关键修改）

当前 `_build_episode_body()` 只输出 GKG 的 Themes/Persons/Organizations/Locations。需要重构成包含 Events CSV 信息：

```python
def _build_episode_body(
    gkg_record: dict,
    events_record: dict | None = None,
    mentions_record: dict | None = None,
) -> str:
    """Build a structured episode body combining all three CSV sources."""
    parts: list[str] = []

    # ── Events CSV (事件骨架) ──────────────────────────────
    if events_record:
        actor1 = events_record.get("actor1_name", "") or events_record.get("actor1_code", "")
        actor2 = events_record.get("actor2_name", "") or events_record.get("actor2_code", "")
        event_desc = events_record.get("event_description", "")
        cameo = events_record.get("cameo_code", "")
        goldstein = events_record.get("goldstein_scale", "")
        tone = events_record.get("tone", "")
        event_date = events_record.get("event_date", "")

        parts.append(
            f"事件: {actor1} → {actor2} → {event_desc}"
            f"(CAMEO:{cameo})"
        )
        parts.append(f"日期: {event_date}")
        if goldstein:
            parts.append(f"Goldstein合作/冲突评分: {goldstein} (-10~+10)")
        if tone:
            parts.append(f"事件语调评分(Tone): {tone} (-100~+100)")

    # ── Mentions CSV (传播覆盖) ─────────────────────────────
    if mentions_record:
        mention_count = mentions_record.get("mention_count", 0)
        mention_sources = mentions_record.get("source_urls", [])
        parts.append(f"传播覆盖: {mention_count} 篇报道")
        if mention_sources:
            parts.append(f"来源: {', '.join(mention_sources[:5])}")

    # ── GKG (语义元数据 — 现有逻辑保留) ────────────────────
    themes = gkg_record.get("themes", "")
    if themes:
        parts.append(f"主题标签: {themes}")

    persons = gkg_record.get("persons", "")
    if persons:
        parts.append(f"人物: {persons}")

    organizations = gkg_record.get("organizations", "")
    if organizations:
        parts.append(f"组织: {organizations}")

    locations = gkg_record.get("locations", "")
    if locations:
        parts.append(f"地点: {locations}")

    source_url = gkg_record.get("source_url", "")
    if source_url:
        parts.append(f"原文链接: {source_url}")

    return "\n".join(parts)
```

#### 11.6.3 LLM 提取可行性分析

**能提取**：LLM 从自然描述中提取结构化字段的能力已被验证（PolicyEntity 的 type/status 也是从文本推断）。

**潜在问题**：
- LLM 可能"编造" tone/goldstein（如果 episode_body 中没有显式数值）
- **解决**：在 episode_body 中显式写死数值，不给 LLM 猜测空间
- **策略**：tone/goldstein 在 episode_body 中直接以 `Tone: -4.87` 出现，LLM 读到后直接复制，不需要"推断"

**置信度**：高。只要 episode_body 包含数字，LLM 提取的准确性很高（字段复制而非推断）。

---

### 11.7 查询场景

#### 场景 1：查询高度正面的事件

```cypher
// 所有 Goldstein > 5 的合作事件（如和平条约、经济援助）
MATCH (e:Event)
WHERE e.goldstein_scale > 5
RETURN e.entity_name, e.actor1, e.actor2, e.goldstein_scale
ORDER BY e.goldstein_scale DESC
LIMIT 20
```

#### 场景 2：查询高度负面的事件

```cypher
// 所有 Tone < -5 的恐慌/冲突事件
MATCH (e:Event)
WHERE e.tone < -5
RETURN e.entity_name, e.actor1, e.actor2, e.tone
ORDER BY e.tone ASC
LIMIT 20
```

#### 场景 3：查询某个国家的所有事件（按合作/冲突排序）

```cypher
// 影响中国的所有事件，按冲突程度排序
MATCH (e:Event)-[r:RELATES_TO]->(c:Country {entity_name: '中国'})
WHERE r.name = 'AFFECTS'
RETURN e.entity_name, e.goldstein_scale, e.tone
ORDER BY e.goldstein_scale ASC
LIMIT 20
```

#### 场景 4：查询某行业的高风险事件

```cypher
// 半导体行业相关、且 Goldstein < -3 的负面事件
MATCH (e:Event)-[r1:RELATES_TO]->(s:Sector {entity_name: '半导体'})
MATCH (e:Event)-[r2:RELATES_TO]->(c:Country)
WHERE e.goldstein_scale < -3
  AND r1.name IN ['RELATED_TO', 'AFFECTS']
  AND r2.name = 'AFFECTS'
RETURN e.entity_name, e.goldstein_scale, c.entity_name AS 受影响国家
ORDER BY e.goldstein_scale ASC
```

#### 场景 5：Risk Summary 增强

```python
# API /api/events/risk-summary 现在可以整合 EventEntity 的定量评分

# 伪代码
async def compute_risk_score():
    goldstein_avg = await neo4j.run("""
        MATCH (e:Event)
        WHERE e.goldstein_scale IS NOT NULL
          AND e.event_date > date() - duration({days: 7})
        RETURN avg(e.goldstein_scale) AS avg_goldstein
    """)
    # avg_goldstein < 0 → 风险评分上升
    risk_score = map_goldstein_to_risk(avg_goldstein)
```

#### 场景 6：MiroFish 行业推演输入

```cypher
// 为 MiroFish 提供"有哪些正面事件可以助推某行业"
MATCH (e:Event)-[r:RELATES_TO]->(s:Sector {entity_name: '新能源'})
WHERE e.goldstein_scale > 3
  AND r.name IN ['RELATED_TO', 'AFFECTS']
RETURN e.entity_name, e.goldstein_scale, e.event_date
ORDER BY e.goldstein_scale DESC
```

---

### 11.8 对设计文档的影响

#### 11.8.1 ARCH_REVIEW_GDELT_REFACTOR.md 修改

| 位置 | 修改内容 | 修改类型 |
|------|---------|---------|
| §七 实施改造清单 | 新增 `EventEntity` 的 Pydantic 模型定义 | 新增 |
| §七.1 新增文件 | 可能不需要新文件，直接在 `entity_types.py` 中新增 | — |
| §七.2 改造文件 | `gdelt_adapter.py` 的 `_build_episode_body()` 重构 | 新增任务 |
| §九 Codebook 资源 | 已覆盖（Actor/CAMEO 码表已在计划内） | 不变 |

#### 11.8.2 NEWSENGINE-DESIGN-DOC.md 修改

| 位置 | 修改内容 | 修改类型 |
|------|---------|---------|
| §1.2 架构图 | EventEntity 作为新的实体类型标注在 Graphiti 引擎 | 新增 |
| §2.3 API 响应 | `entities[]` 中增加 `type: "event"` 示例 | 新增 |
| §2.5 sector 响应 | entities 中可能出现 `event` 类型 | 新增 |
| §2.8 Schema 映射表 | 新增 `EventEntity → Entity:Event` 映射行 | 新增 |
| §3.1 文件架构 | `entity_types.py` 新增 `EventEntity` 类 | 修改 |
| §3.2 模块职责矩阵 | `graphiti/entity_types.py` 的 MACRO_ENTITY_TYPES 增加 Event | 修改 |
| §3.6.2 macro_themes | 不变（过滤逻辑不受影响） | 不变 |
| §5 sector_briefing 生成 | EventEntity 可作为行业简报的输入素材 | 补充 |

#### 11.8.3 代码修改清单

| 文件 | 修改内容 | 代码量 |
|------|---------|--------|
| `src/graphiti/entity_types.py` | 新增 `EventEntity` Pydantic 模型；`MACRO_ENTITY_TYPES` 增加 `"Event": EventEntity` | ~30 行 |
| `src/graphiti/relation_types.py` | `DEFAULT_EDGE_TYPE_MAP` 增加 `("Event", "Entity")` 关系映射 | ~2 行 |
| `src/graphiti/translation.py` | `LABEL_TYPE_MAP` 增加 `"EVENT": "event"`；`entity_type_from_labels()` 处理 Event 标签 | ~3 行 |
| `src/graphiti/episode_writer.py` | 如果使用 `MACRO_ENTITY_TYPES`（目前用 `SYMBOL_ENTITY_TYPES`），需切换或整合 | ~5 行 |
| `src/adapters/gdelt_adapter.py` | `_build_episode_body()` 重构，整合 Events CSV 信息 | ~40 行 |
| `src/adapters/models.py` | `build_entity_suffix()` 增加 Event 类型的格式化分支 | ~3 行 |
| **总计** | | **~83 行** |

---

### 11.9 风险评估

| 风险 | 严重程度 | 缓解措施 |
|------|---------|---------|
| EventEntity 与 PolicyEntity 边界模糊 | LOW | episode_body 中显式区分：结构化事件（有 Goldstein）→ EventEntity；政策声明 → PolicyEntity |
| LLM 可能"编造"事件描述 | MEDIUM | episode_body 中写死结构化事实文本，不让 LLM 猜测；cameo_code 作为 hard hint |
| MACRO_ENTITY_TYPES 增长导致 LLM token 消耗增加 | LOW | 6 个 entity type 仍远低于 Graphiti 推荐上限（~10 个），token 增量可忽略 |
| SYMBOL_ENTITY_TYPES 不需要 EventEntity | NONE | EventEntity 仅加入 MACRO_ENTITY_TYPES。个股新闻（AkShare）中的"事件"应保持用 Topic 或 Policy 建模 |
| Actor 码表不完整 | MEDIUM | 已包含在 GDELT 重构风险池中（§八 "CAMEO Codebook 不完整"），使用原始码 + LLM 推理作为降级 |

---

### 11.10 最终裁决

| 维度 | 结论 |
|------|------|
| 方案 B vs 方案 A | ✅ 方案 B 正确。方案 A 的 metadata JSON 不支持属性索引和范围查询 |
| EventEntity 设计 | ✅ 通过。老公的原始设计正确，需补充 event_date、字段 Optional 化、tone 归一化说明 |
| 与现有 entity types 冲突 | ✅ 不冲突。EventEntity 是定量补充，Policy/Topic 是定性分类 |
| LLM 提取可行性 | ✅ 可行。episode_body 中写死数值后 LLM 复制即可 |
| 查询能力验证 | ✅ 通过。支持按 Goldstein/Tone 排序、按 Actor 关联、按 Sector 交叉查询 |
| 对架构的影响 | ✅ 控制在内。改动 ~83 行代码，集中在 adapter + graphiti 层 |
| 实施风险 | ✅ 可控。属于 GDELT 重构 V1.1 的自然延伸，不引入新依赖 |

**建议实施顺序**：
1. `entity_types.py` 新增 `EventEntity` → 2. `gdelt_adapter.py` 重构 `_build_episode_body()` → 3. `translation.py` / `models.py` 补充事件类型 → 4. 集成测试验证 Neo4j Entity:Event 标签正确写入。

---

### 11.11 更新后的实施改造清单

在第 §七节基础上，新增 EventEntity 相关任务：

| 任务 | 预估时间 | 依赖 |
|------|---------|------|
| EventEntity Pydantic 模型定义 | 0.5h | — |
| `_build_episode_body()` 重构（整合 Events CSV） | 1h | EventEntity 模型 |
| `translation.py` + `models.py` 补充事件类型 | 0.5h | EventEntity 模型 |
| MACRO_ENTITY_TYPES 注册 | 0.25h | EventEntity 模型 |
| 集成测试 | 0.5h | 以上全部 |
| **EventEntity 小计** | **~2.75h** | |

**原改造预估（§七）：~10.5 小时 → 更新后总计：~13.25 小时**

---

### 11.12 字段冗余评审：EventEntity vs EpisodicNode + NormalizedEpisode

**评审触发**：老公指出 Graphiti 的 EpisodicNode 自带 `valid_at` 时间属性，EventEntity 作为 Entity 节点通过 MENTIONS 关系连接到 Episodic 节点，查询时可直接用 Episodic 的 `valid_at`。因此需评审 §11.4 中为 EventEntity 新增的 `event_date` 是否冗余。

#### 11.12.1 Graphiti 时间属性分析

**EpisodicNode 的 `valid_at`**（源码：`graphiti_core/nodes.py:322`）：
```python
class EpisodicNode(Node):
    valid_at: datetime = Field(
        description='datetime of when the original document was created',
    )
```
- `valid_at` 表示**文章/报道的发布时间**
- 在 NewsEngine 中，NormalizedEpisode.valid_at → EpisodicNode.valid_at（通过 EpisodeWriter 的 `reference_time` 参数）
- 当前 GKG adapter 取的是 GKG CSV 第 2 列的日期（文章收录日期）

**EntityNode 的时间属性**：
```python
class EntityNode(Node):
    attributes: dict[str, Any] = Field(
        default={}, description='Additional attributes of the node. Dependent on node labels'
    )
```
- EntityNode 没有自带的 `valid_at` 字段
- Entity 节点通过 `attributes` dict 存储自定义属性（如 EventEntity 的 `goldstein_scale`, `tone`, `actor1` 等）
- EventEntity 的 `event_date` 也会落在 `attributes` dict 中

**图拓扑中的时间关系**：
```
EpisodicNode (valid_at = 文章发布时间) ──MENTIONS──► EventEntity (event_date = 事件发生日期)
```

#### 11.12.2 `event_date` 是否冗余：❌ 不冗余，必须保留

**结论：`event_date` 不冗余，且必须保留在 EventEntity 上。** 理由如下：

| # | 理由 | 详细说明 |
|---|------|---------|
| 1 | **语义不同** | `valid_at` = 文章发布时间；`event_date` = 事件发生日期。二者可能差数天（事件发生后多日才有报道） |
| 2 | **多对一关系** | 一个 EventEntity 可能被多个 EpisodicNode MENTIONS（不同媒体在不同时间报道同一事件）。若从 MENTIONS 推导时间，取哪一个 Episode 的 `valid_at`？取最早？最新？取决于查询场景，不是固定的。 |
| 3 | **查询性能** | 若事件时间从 Episodic 推导，每次按事件时间排序/过滤都需要 JOIN Episodic 节点，多一跳 JOIN 且需要聚合（MIN/MAX）。`event_date` 作为 EventEntity 的直接属性，查询效率远高于从 Episodic 推导。 |
| 4 | **GDELT 数据源自主提供** | GDELT Events CSV 的 EventDate（列 3-4，格式 YYYYMMDD）是**事件实际发生日期**，这是 GDELT 提供的核心字段，不应丢弃。 |
| 5 | **Entity 层面查询需求** | §11.7 中的查询场景（如场景 5/6）需要在 Entity 层面按时间范围过滤，这是 Entity 本身的属性，不应依赖 Episodic 节点。 |

**唯一可通过 Episodic 推导的情况**：当且仅当查询场景是"某篇报道中提到了这个事件"时，Episodic.valid_at 反应的是报道时间。但 NewsEngine 的核心查询场景（§11.7）按**事件维度**查询（"最近 7 天发生了哪些负面事件"），不是按报道维度。

**建议**：保留 `event_date` 在 EventEntity 上，不做删除。

#### 11.12.3 NormalizedEpisode 字段对照：哪些该加到 EventEntity

逐一对照 `NormalizedEpisode` 的 12 个字段，判断是否应该加到 `EventEntity`：

| NormalizedEpisode 字段 | 是否加到 EventEntity | 理由 |
|------------------------|-------------------|------|
| `episode_body` | ❌ 不加 | 这是 LLM 提取实体/关系的**输入文本**，不是实体属性。事件描述已在 `entity_name` |
| `name` | ❌ 不加 | Episode 的唯一标识（`"gdelt_csv-20260628-gkg-abc123"`），与 EventEntity 无关 |
| `source_description` | ❌ 不加 | Episode 的来源描述（如 `"GDELT GKG V2"`），EventEntity 不需要 |
| `source_type` | ❌ 不加 | 数据源枚举（`"gdelt_csv"`），EventEntity 始终来自 GDELT Events CSV，无需存储 |
| `source_url` | ❌ 不加 | 文章原文链接，属于 Episode 维度，不属于事件维度。多个文章可共享同一事件 |
| `valid_at` | ⚠️ **不直接加** | 这是**文章发布时间**，不是事件发生日期。EventEntity 应使用 `event_date`（来自 Events CSV EventDate 列），它代表的是不同概念 |
| `content_hash` | ❌ 不加 | Episode 内容去重 hash，与 EventEntity 无关 |
| `entities` | ❌ 不加 | 这是 Episode 预提取的实体列表（Country/Organization/Topic 等），不是 EventEntity 的字段 |
| `is_plain_text` | ❌ 不加 | Episode 的文本格式标记，EventEntity 不需要 |
| `severity` | ❌ 不加 | Episode 级别的严重程度（从 GKG Tone 推导），不是事件级别的。事件级别已有 `goldstein_scale` 和 `tone` |
| `keywords` | ❌ 不加 | GKG Themes 关键词（如 `WB_698_TRADE`），属于 Episode 维度。事件有关主题通过 RELATES_TO → Topic 关系表达 |
| `metadata` | ❌ 不加 | Episode 的自定义元数据（如 `content_scope: MACRO`），与 EventEntity 无关 |

**结论：NormalizedEpisode 的 12 个字段中，0 个需要加到 EventEntity。** EventEntity 的字段来源是 GDELT Events CSV 的结构化数据（Actor1/Actor2/CAMEO/Goldstein/Tone/EventDate），而不是 NormalizedEpisode。

#### 11.12.4 Graphiti 自带字段分析：哪些不需要在 EventEntity 中重复

Graphiti 的 EntityNode 自带以下系统字段（源码：`graphiti_core/nodes.py:499`）：

| Graphiti 自带字段 | 说明 | EventEntity 是否需要重复 |
|-----------------|------|----------------------|
| `uuid` | 节点唯一标识 | ❌ 系统自动生成，不需要定义 |
| `name` | 节点名称 | ✅ 通过 `entity_name` 映射，已在 §11.4 定义中 |
| `group_id` | 分组标识 | ❌ 系统自动管理 |
| `summary` | 节点摘要 | ❌ Graphiti 自动生成，不需要定义 |
| `name_embedding` | 名称向量 | ❌ Graphiti 自动生成 |
| `attributes` | 自定义属性 dict | ✅ EventEntity 所有自定义字段（`actor1`, `actor2`, `cameo_code`, `goldstein_scale`, `tone`, `event_date`）都存入此 dict |
| `created_at` | 节点创建时间 | ❌ 系统自动管理 |
| `labels` | Neo4j 标签列表 | ❌ 由 `MACRO_ENTITY_TYPES` 注册时自动生成 `["Entity", "Event"]` |

**结论：EventEntity 不需要重复任何 Graphiti 自带字段。** 所有自定义字段通过 Pydantic model → attributes dict 映射，这是 Graphiti 的标准机制。

#### 11.12.5 最终 EventEntity 字段列表（修正后）

经过以上三个维度的评审（`event_date` 必要性、NormalizedEpisode 对照、Graphiti 自带字段排除），**最终建议的 EventEntity 字段列表与 §11.4 原始定义完全一致，无字段需要删除。**

```python
class EventEntity(BaseModel):
    """事件实体 — 从 GDELT Events CSV 提取的结构化事件。

    Neo4j 节点标签: Entity:Event
    所有字段通过 Graphiti 的 model_json_schema() 自动映射到 EntityNode.attributes dict。
    """

    entity_name: str = Field(
        ...,
        description="事件描述，一句话，使用中文。例如: '德国向乌克兰提供物资援助'",
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），翻译后的中文名。",
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），翻译后的中文名。",
    )
    cameo_code: str | None = Field(
        default=None,
        description="CAMEO 事件代码。例如: '057' (提供援助), '173' (逮捕/拘留)",
    )
    goldstein_scale: float | None = Field(
        default=None,
        description="Goldstein 合作/冲突评分 (-10 ~ +10)。来源: GDELT Events CSV",
    )
    tone: float | None = Field(
        default=None,
        description="新闻语调评分 (-100 ~ +100，已归一化)。来源: GDELT Events CSV AvgTone / 100",
    )
    event_date: str | None = Field(
        default=None,
        description="事件发生日期 (YYYY-MM-DD)。来源: GDELT Events CSV EventDate 列。"
                    "与 EpisodicNode.valid_at (文章发布时间) 是不同的概念。",
    )
```

**字段来源映射**：

| EventEntity 字段 | GDELT Events CSV 列 | 说明 |
|-----------------|-------------------|------|
| `entity_name` | Actor1Name + Actor2Name + CAMEO 翻译 → LLM 合成 | 自然语言描述 |
| `actor1` | Actor1Name（列 7）或 Actor1Code（列 6） | Codebook 翻译后 |
| `actor2` | Actor2Name（列 17）或 Actor2Code（列 16） | Codebook 翻译后 |
| `cameo_code` | EventBaseCode（列 27）或 EventRootCode（列 28） | 直接取值 |
| `goldstein_scale` | GoldsteinScale（列 31） | 直接取值，float |
| `tone` | AvgTone（列 35）÷ 100 | 归一化到 -100~+100 |
| `event_date` | EventDate / SQLDATE（列 3-4） | YYYYMMDD → YYYY-MM-DD |

**字段设计原则**：
1. **所有字段 Optional**：LLM 提取时可能信息不全，允许字段缺失
2. **不重复 Graphiti 自带字段**：uuid、name、group_id、labels 等由系统管理
3. **不重复 Episode 维度信息**：source_url、keywords、severity 等属于 Episode，不属于事件实体
4. **`event_date` 必须保留**：语义与 EpisodicNode.valid_at 不同，且支持 Entity 层面的时间查询

#### 11.12.6 评审裁决

| 评审项 | 结论 |
|-------|------|
| `event_date` 是否冗余 | ❌ **不冗余，必须保留**。它与 EpisodicNode.valid_at 语义不同（事件日期 vs 报道日期），且支持 Entity 层面直接查询 |
| NormalizedEpisode 字段是否需加到 EventEntity | ❌ **不需要**。两者维度不同（Episode vs Entity），无共享字段 |
| 是否有 Graphiti 自带字段被重复定义 | ❌ **没有**。EventEntity 的所有字段都是业务自定义字段，通过 attributes dict 存储 |
| 最终字段列表是否需要调整 | ✅ **不需要调整**。§11.4 的原始定义完全正确 |
| 是否有字段需要删除 | ❌ **零字段删除**。所有 7 个字段均有独立业务价值 |

---

---

## 十二、跨管线 Entity Type 关联评审

### 12.1 核心原则

老公的核心观点：

> "不管是 GDELT 还是 RSS 都应该是宏观数据，再加上个股数据，这些数据的 entity type 应该是可以建立关联的，这样才能组成宏观↔宏观、宏观↔个股、个股↔个股之间的关系网络，并且看到事件的全貌。"

**裁决：完全正确。** Entity types 必须能跨管线建立关联，否则就不是"网络"而是"孤岛"。

### 12.2 当前 MACRO 与 SYMBOL 的 Entity Type 对照

| Entity Type | MACRO (GDELT/RSS) | SYMBOL (AkShare) | 关联点 |
|------------|-------------------|-------------------|--------|
| Organization | ✅ | ✅ | 公司/机构跨管线复用 |
| Country | ✅ | ✅ | 国家维度 |
| Policy | ✅ | ✅ | 政策影响个股 |
| Sector | ✅ | ✅ | 行业板块是天然的连接桥梁 |
| Topic | ✅ | ❌ | 只出现在宏观管线 |
| Stock | ❌ | ✅ | 只出现在个股管线 |
| **Event** | ✅ (新增) | ✅ (新增) | **跨管线关联的核心** |

**共享 entity type**（可跨管线关联）: Sector, Organization, Country, Policy, Event

### 12.3 EventEntity 同时存在于 MACRO 和 SYMBOL

**裁决：✅ EventEntity 加入 SYMBOL_ENTITY_TYPES（简化版）**

| 管线 | EventEntity 字段 | 说明 |
|------|-----------------|------|
| **MACRO** (GDELT/RSS) | entity_name, actor1, actor2, cameo_code, goldstein_scale, tone, event_date | 完整结构化事件 |
| **SYMBOL** (AkShare) | entity_name, actor1, actor2, event_date | 简化版，没有 CAMEO/Goldstein/Tone |

**SYMBOL 版 EventEntity 定义**：

```python
class SymbolEventEntity(BaseModel):
    """事件实体 — 从 AkShare 个股新闻提取的事件。

    与 MACRO 版的 EventEntity 区别：没有 cameo_code/goldstein_scale/tone，
    因为 AkShare 数据源不提供这些结构化字段。

    Neo4j 节点标签: Entity:Event
    """

    entity_name: str = Field(
        ...,
        description="事件描述，一句话，使用中文。例如: '腾讯回购10亿港元'",
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），例如: '腾讯控股'",
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），例如: '香港交易所'",
    )
    event_date: str | None = Field(
        default=None,
        description="事件发生日期 (YYYY-MM-DD)。",
    )
```

**注意**：MACRO 版和 SYMBOL 版的 EventEntity 都映射到同一个 Neo4j 标签 `Entity:Event`，但字段集合不同。Graphiti 的 Pydantic schema 机制支持这种差异——不同管线使用不同的 entity_types 注册表。

### 12.4 跨管线关联机制

**裁决：Graphiti 自动完成，不需要额外去重/合并逻辑。**

Graphiti 的实体消歧（entity resolution）使用 LLM 判断新提及是否与已有实体节点相同。当 GDELT 和 AkShare 的 episode 提到"腾讯控股"时：

1. GDELT episode: "美联储加息影响全球市场，腾讯控股股价下跌..." → 提取 Country: "美国", Organization: "腾讯控股", Event: "美联储加息"
2. AkShare episode: "腾讯控股今日回购10亿港元" → 提取 Stock: "0700.HK", Organization: "腾讯控股", Event: "腾讯回购10亿港元"
3. Graphiti LLM 判断两个 episode 提到的"腾讯控股"是同一个实体 → 合并为同一个 Entity 节点
4. 两个 EventEntity 通过 RELATES_TO 连接到同一个 Organization 节点

**最终 Neo4j 图结构**：

```
(:Event {entity_name: "美联储加息"}) -[:AFFECTS]-> (:Organization {entity_name: "腾讯控股"})
(:Event {entity_name: "腾讯回购10亿港元"}) -[:AFFECTS]-> (:Stock {ticker: "0700.HK"})
(:Organization {entity_name: "腾讯控股"}) <-[:MENTIONS]- (:Stock {ticker: "0700.HK"})
```

### 12.5 "事件全貌"查询场景

**裁决：✅ 支持跨管线查询**

```cypher
-- 查询"腾讯控股"相关的所有事件（宏观 + 个股）
MATCH (org:Entity:Organization {entity_name: "腾讯控股"})-[:RELATES_TO]-(event:Entity:Event)
RETURN event.entity_name AS event_name, event.event_date AS date
ORDER BY event.event_date DESC
```

这个查询能**同时返回**：
- GDELT 的宏观事件："美联储加息"、"中美贸易摩擦升级"
- AkShare 的个股事件："腾讯回购10亿港元"、"腾讯发布财报"

因为它们都通过 RELATES_TO 连接到同一个 Organization 节点。

**更精确的查询（通过 Stock 关联）**：

```cypher
-- 查询与"0700.HK"相关的所有事件（通过 Stock 节点）
MATCH (stock:Entity:Stock {ticker: "0700.HK"})-[:RELATES_TO]-(event:Entity:Event)
RETURN event.entity_name AS event_name, event.event_date AS date
```

### 12.6 Topic vs Event 的边界

**裁决：两者不合并，语义不同。**

| 类型 | 语义 | 示例 | 来源 |
|------|------|------|------|
| **Topic** | 抽象概念/主题 | "加息"、"贸易战"、"芯片出口管制" | GDELT Themes / RSS 关键词 |
| **Event** | 具体发生的事件 | "美联储宣布加息25个基点"、"德国向乌克兰提供物资援助" | GDELT Events / RSS 新闻 / AkShare 新闻 |

**判断标准**：
- Topic 是"讨论什么"（what is being discussed）
- Event 是"发生了什么"（what happened）

**示例对比**：

| 新闻内容 | 提取为 Topic | 提取为 Event |
|---------|-------------|-------------|
| "美联储加息对全球经济的影响" | Topic: "加息" | ❌ 没有具体事件 |
| "美联储宣布加息25个基点" | ❌ 不是抽象概念 | Event: "美联储宣布加息25个基点" |
| "芯片出口管制政策分析" | Topic: "芯片出口管制" | ❌ 没有具体事件 |
| "美国商务部将华为列入实体清单" | ❌ 不是抽象概念 | Event: "美国商务部将华为列入实体清单" |

**两者可以关联**：

```
(:Event {entity_name: "美联储宣布加息25个基点"}) -[:RELATED_TO]-> (:Topic {entity_name: "加息"})
```

### 12.7 最终 Entity Type 定义

**MACRO_ENTITY_TYPES**（GDELT + RSS）：

```python
MACRO_ENTITY_TYPES = {
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Topic": TopicEntity,
    "Policy": PolicyEntity,
    "Sector": SectorEntity,
    "Event": EventEntity,  # 完整版：含 cameo_code/goldstein_scale/tone
}
```

**SYMBOL_ENTITY_TYPES**（AkShare）：

```python
SYMBOL_ENTITY_TYPES = {
    "Stock": StockEntity,
    "Sector": SectorEntity,
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Policy": PolicyEntity,
    "Event": SymbolEventEntity,  # 简化版：无 cameo_code/goldstein_scale/tone
}
```

**跨管线关联的 Entity Types**（共享）：

| Entity Type | 说明 | 关联场景 |
|------------|------|---------|
| Organization | 公司/机构 | 宏观事件影响公司，个股新闻提到公司 |
| Country | 国家/地区 | 宏观政策影响国家，个股业务涉及国家 |
| Policy | 政策/监管 | 宏观政策影响个股，公司应对政策 |
| Sector | 行业/板块 | 宏观趋势影响行业，个股属于行业 |
| **Event** | 事件 | 宏观事件影响个股，个股事件关联宏观 |

### 12.8 设计文档修改清单

| 位置 | 修改内容 | 影响程度 |
|------|---------|---------|
| §3.4 Entity Types 定义 | 新增 EventEntity 和 SymbolEventEntity | 中 |
| §3.4 跨管线关联说明 | 新增 §3.4.3 说明 Graphiti 自动完成实体消歧 | 小 |
| §5.2 查询示例 | 新增跨管线查询示例 | 小 |
| §8.2 数据流图 | 更新 entity_types 注册表 | 小 |

---

**文档版本**: V1.5 (补充实施计划)  
**最后更新**: 2026-06-28  
**变更**: V1.4 → V1.5 增加 §十三 实施计划（Implement Plan）

---

## 十三、实施计划（Implement Plan）

### 13.1 任务总览

| # | 任务 | 前置依赖 | 预估 |
|---|------|---------|------|
| **G1** | Codebook 资源获取与整理 | 无 | 1h |
| **G2** | `gdelt_codebook.py` 实现 | G1 | 2h |
| **G3** | `gdelt_events_parser.py` 实现 | 无 | 1.5h |
| **G4** | `gdelt_mentions_parser.py` 实现 | 无 | 1h |
| **G5** | `gdelt_adapter.py` 核心重构 | G2, G3, G4 | 3h |
| **G6** | `entity_types.py` 新增 EventEntity + SymbolEventEntity | 无 | 1h |
| **G7** | `relation_types.py` 更新（如需要） | G6 | 0.5h |
| **G8** | 清库 + 集成测试 + 验证 | G5, G6, G7 | 1.5h |
| **G9** | 设计文档同步更新 | G8 | 0.5h |
| | **总计** | | **~12h** |

### 13.2 任务详情

#### G1: Codebook 资源获取与整理

**目标**：下载并整理三份 Codebook 为 JSON/Python dict 格式

| Codebook | 来源 | 状态 |
|----------|------|------|
| CAMEO 事件码 | GDELT 官网 | 需要找 |
| Theme 分类码 | `LOOKUP-GKGTHEMES.TXT` | ✅ 已下载 (59K行) |
| Actor 代码 | GDELT 官网 | 需要找 |

**产出**：`data/codebooks/` 目录下的三个 JSON 文件

#### G2: `gdelt_codebook.py` 实现

**目标**：Codebook 加载与翻译接口

```python
# src/adapters/gdelt_codebook.py

def translate_cameo(code: str) -> str: ...      # "057" → "提供物资援助"
def translate_actor(code: str) -> str: ...      # "DEU" → "德国"
def translate_theme(code: str) -> str: ...      # "WB_698_TRADE" → "国际贸易"
def translate_event_record(event: dict) -> dict: ...  # 批量翻译
```

**关键设计**：
- 低频码 fallback 到原始码 + LLM 推理
- 翻译结果缓存（避免重复 IO）

#### G3: `gdelt_events_parser.py` 实现

**目标**：Events CSV 下载/解析/结构化

```python
# src/adapters/gdelt_events_parser.py

def fetch_events_csv(url: str) -> list[dict]: ...
def parse_events(records: list[dict]) -> list[EventRecord]: ...

# EventRecord 结构：
# {
#   "event_id": "1311126111",
#   "event_date": "2026-06-21",
#   "actor1_code": "DEU", "actor1_name": "GERMANY",
#   "actor2_code": "UKR", "actor2_name": "UKRAINE",
#   "cameo_code": "057",
#   "goldstein_scale": 8.0,
#   "avg_tone": -0.4,
#   "lat": 50.43, "lon": 30.52,
#   "source_url": "http://..."
# }
```

#### G4: `gdelt_mentions_parser.py` 实现

**目标**：Mentions CSV 下载/解析/关联

```python
# src/adapters/gdelt_mentions_parser.py

def fetch_mentions_csv(url: str) -> list[dict]: ...
def parse_mentions(records: list[dict]) -> dict[str, list[MentionRecord]]: ...

# 返回: {event_id: [MentionRecord, ...]}
# MentionRecord: {domain, url, confidence, mention_date}
```

#### G5: `gdelt_adapter.py` 核心重构

**目标**：三 CSV 整合 + Codebook 翻译 + episode_body/entities 重构

**改造点**：
1. `fetch_lastupdate()` → 解析三行 URL（Events/Mentions/GKG）
2. 新增 `fetch_lastupdate_full()` → 返回三个 URL
3. 新增 `merge_event_data()` → 以 EventID 为主键 LEFT JOIN
4. 重构 `_build_episode_body()` → 人类可读格式（§4.4 目标格式）
5. 重构 `_parse_entities_from_record()` → Themes 退出 Entity，改为 keywords
6. 新增降级路径：Events/Mentions 失败 → 回退 GKG-only

**必须修改点（3 个）**：
- ✅ Layer A: GKG Themes 子串匹配（保留）+ Layer B: CAMEO 码匹配（新增）
- ✅ `EntityItem(type="theme")` 删除，Themes 改为 keywords + episode_body 文本
- ✅ episode_body 结构化（事件骨架 + 传播 + 主题 + 情感）

#### G6: `entity_types.py` 新增 EventEntity + SymbolEventEntity

**目标**：两个新 entity type

```python
# MACRO 版（完整版）
class EventEntity(BaseModel):
    entity_name: str           # 事件描述
    actor1: str | None = None
    actor2: str | None = None
    cameo_code: str | None = None
    goldstein_scale: float | None = None
    tone: float | None = None
    event_date: str | None = None

# SYMBOL 版（简化版）
class SymbolEventEntity(BaseModel):
    entity_name: str           # 事件描述
    actor1: str | None = None
    actor2: str | None = None
    event_date: str | None = None

# 更新注册表
MACRO_ENTITY_TYPES["Event"] = EventEntity
SYMBOL_ENTITY_TYPES["Event"] = SymbolEventEntity
```

#### G7: `relation_types.py` 更新

**目标**：确认是否需要新增关系类型

当前已定义：`AFFECTS`, `CAUSED_BY`, `MITIGATES`, `BELONGS_TO`, `LOCATED_IN`, `RELATED_TO`

**评估**：EventEntity 的关系（如 Event → AFFECTS → Country）可以用现有的 `AFFECTS` 和 `RELATED_TO`，**可能不需要新增**。

#### G8: 清库 + 集成测试 + 验证

**目标**：验证数据质量

1. 清空 Neo4j（`MATCH (n) DETACH DELETE n`）
2. 运行 `python main.py` 触发 GDELT 采集
3. 验证：
   - Entity 不再是 `mirror.co.uk` 等垃圾
   - Summary 不再是 `WB_621_HEALTH_NUTRITION_AND_POPULATION`
   - EventEntity 有 cameo_code/goldstein_scale/tone
   - 跨管线关联正常（Stock ↔ Organization ↔ Event）

#### G9: 设计文档同步更新

**目标**：更新 `NEWSENGINE-DESIGN-DOC.md`

| 位置 | 修改内容 |
|------|----------|
| §1.4.2 双管线架构 | GDELT 描述改为"Events+Mentions+GKG CSV" |
| §3.2 模块职责矩阵 | gdelt_adapter 职责增加 Events/Mentions 解析 |
| §3.4 Entity Types | 新增 EventEntity + SymbolEventEntity |
| §3.6.2 GDELT 宏观主题白名单 | 过滤逻辑改为双层（GKG Themes + CAMEO 码） |
| §3.1 文件架构 | 新增 gdelt_codebook.py, gdelt_events_parser.py 等 |

### 13.3 执行顺序

```
G1 (Codebook 获取)
    ↓
G2 (codebook.py) ←── 并行 ──→ G3 (events_parser.py) + G4 (mentions_parser.py) + G6 (entity_types.py)
    ↓                              ↓
G5 (gdelt_adapter.py 核心重构) ←───┘
    ↓
G7 (relation_types.py 评估)
    ↓
G8 (清库 + 集成测试)
    ↓
G9 (设计文档更新)
```
