# NewsEngine 数据质量与架构诊断报告

**日期**: 2026-09-05  
**版本**: v3.0（经 Architect Review 两轮修订）  
**诊断范围**: 关系类型设计、实体桥接、API 查询、数据质量

---

## 一、关系类型设计

### 1.1 现状

定义了 10 种关系类型：`RELATES_TO`, `INVOLVES`, `HAPPENED_IN`, `AFFECTS`, `PART_OF`, `BELONGS_TO`, `TRADED_ON`, `INVESTS_IN`, `TRIGGERS`, `EXPOSED_TO`

### 1.2 实际数据分布

| 关系类型 | 数量 | 下游是否使用 | 来源 |
|---------|------|------------|------|
| RELATES_TO | 840 | ❌ SynapseEngine 不查图 | prompt + schema 显式定义 |
| INVOLVES | 700 | ❌ | prompt 显式定义 |
| HAPPENED_IN | 623 | ❌ | prompt 显式定义 |
| AFFECTS | 319 | ❌ | prompt 显式定义 |
| PART_OF | 149 | ❌ | prompt 显式定义 |
| BELONGS_TO | 112 | ❌ | prompt 显式定义 |
| INVESTS_IN | 103 | ❌ 且 ~60% 错误 | prompt 显式定义 |
| TRIGGERS | 88 | ❌ | prompt 显式定义 |
| EXPOSED_TO | 74 | ❌ | 仅 schema 注入（半显式） |
| TRADED_ON | 21 | ❌ | prompt 显式定义 |

### 1.3 核心矛盾

**SynapseEngine（当前唯一消费者）只用这些字段**：
- `summary`（原文摘要）
- `severity`（情绪评分）
- `entities`（关键词/ticker）
- `sector`（行业范围）

**它不查图关系。** 10 种关系类型全部是"为未来准备"的设计，当前没有任何下游消费者使用图遍历能力。

### 1.4 建议（修订后）

| 关系类型 | 建议 | 理由 |
|---------|------|------|
| `RELATES_TO` | **保留** | Graphiti 默认的通用关系，兜底类型 |
| `INVOLVES` | **保留** | 主体参与/任职，700 条存量 |
| `HAPPENED_IN` | **保留** | 地点归属，623 条存量 |
| `AFFECTS` | **保留** | 影响关系，319 条存量 |
| `PART_OF` | **保留** | 结构性归属（子公司→母公司），149 条 |
| `BELONGS_TO` | **保留** | 股票→行业，桥接关键，112 条 |
| `TRADED_ON` | **保留** | 股票→交易所，21 条 |
| `TRIGGERS` | **保留** | 因果关系，88 条，事件脉络业务目标需要 |
| `INVESTS_IN` | **删除** | ~60% 错误率（捐赠/学历被标为投资），103 条需迁移 |
| `EXPOSED_TO` | **删除** | 无 prompt 引导、语义模糊、零消费，74 条需迁移 |

**保留 8 种，删除 2 种。**

### 1.5 删除成本（必须考虑）

| 遗漏项 | 说明 |
|--------|------|
| 存量 177 条边迁移 | INVESTS_IN 103 + EXPOSED_TO 74。正确子集改 PART_OF/AFFECTS，错误子集降级 RELATES_TO 或删除 |
| 44 个单测联动 | `tests/test_episode_writer.py` 大量直接断言映射行为 |
| renormalize 脚本联动 | `scripts/renormalize_edge_types.py` import normalize_edge_type |

---

## 二、normalize_edge_type：保留，但需修正

### 2.1 原诊断错误

**原诊断**："从未触发，Pydantic schema 已约束 LLM 输出范围，建议删除"

**实际情况（Architect Review 纠正）**：

1. **每次写入都触发**，有 3 个调用点：
   - `_normalized_edge_types()`：写入前，把 EDGE_TYPES 注册表遗留键收敛为核心类型
   - `_normalized_edge_type_map()`：写入前，收敛 DEFAULT_EDGE_TYPE_MAP 中所有实体对的允许类型
   - `_normalize_written_edges()`：写后兜底（这个确实 0 触发，9 天日志 0 次 "edge type normalized"）

2. **Pydantic schema 不约束类型名**：graphiti-core 0.29.3 prompt 明文允许 LLM 在无匹配时发明新类型名（`SCREAMING_SNAKE_CASE`）。schema 只约束已知类型的属性字段。

3. **代码片段引用错误**：真实实现是前缀匹配+默认归 RELATES_TO（不透传），不是我之前引用的字典透传。

### 2.2 修正建议

**保留 normalize_edge_type 与写后兜底**（成本 ≈ 0，无性能负担）。

真正的债务在源头：
1. 把 `relation_types.py` 的 `EDGE_TYPES`/`DEFAULT_EDGE_TYPE_MAP` 直接重写为核心类型集
2. **修复 schema 错配**：`LocatedInEdge`（docstring 写"股票上市地/注册地所在国家"）被映射到 PART_OF，语义不匹配；`BelongsToEdge` 无 valid_at 字段。重写注册表时一并修复模型与语义错配
3. 修正 LOCATED_IN→PART_OF 语义错误（"腾讯位于中国"≠"腾讯是中国的一部分"），改为 LOCATED_IN→HAPPENED_IN
4. **存量修正配套**：LOCATED_IN 映射变更会影响 `(Stock, Country)` 和 `(Organization, Country)` 实体对。实测库内 PART_OF 149 条中混有地点语义边（如 `FEILI CO LIMITED -> China`，fact: "is located in China"），需配套存量修正
5. 保留写后兜底作为对抗 graphiti 自由发挥行为的唯一防线

---

## 三、业务需求断层：宏观-行业-个股桥接完全缺失

### 3.1 业务目标

> 查自选股时，相关的宏观事件和个股事件都能查到，且关联有实际意义。
> 查行业时，相关的宏观事件和正面/负面消息能一起查到。

**核心需求**：通过 Graphiti 的关系网络（RELATES_TO + fact），构建事件脉络，让 SynapseEngine 拿到一张完整的图。

### 3.2 设计意图 vs 现实

#### 设计意图（正确）

```
宏观 episode → 提取 Sector "纺织行业"
                    ↓
个股 episode → 提取 Sector "纺织行业"  ← 同一个实体！
                    ↓
Stock "鲁泰A" → BELONGS_TO → Sector "纺织行业"
                    ↓
查询时：从 "纺织行业" 出发，遍历整个网络
  - 宏观事件：India's textiles sector gains tariff advantage
  - 个股事件：鲁泰A 涨停，受益于关税优势
  - 时间线：先看宏观政策，再看个股反应
```

**这正是 Graphiti 的核心价值：通过共享实体构建事件脉络。**

#### 现实（桥接断裂）

```
宏观 episode → 提取 Sector "Textiles Sector"（英文）
                    ↓
个股 episode → 提取 Sector "纺织行业"（中文）  ← 不同实体！
                    ↓
Stock "鲁泰A" → BELONGS_TO → Sector "纺织行业"
                    ↓
查询时：两个孤立网络，无法桥接
  - 查 "Textiles Sector" → 只看到宏观事件
  - 查 "纺织行业" → 只看到个股事件
```

### 3.3 当前状态

```
宏观 episode（GDELT/RSS）: 573 个
  ├── 实体：India, Canada, Textiles Sector, Aviation...（英文）
  └── 关联：Country↔Country, Country↔Organization

个股 episode（公告/研报）: 22 个
  ├── 实体：中国平安, 非银金融, 白酒Ⅱ...（中文）
  └── 关联：Stock↔Sector, Stock↔Organization

桥接数量：0
```

**没有任何一个实体同时出现在宏观和个股 episode 中。**

### 3.4 根因分析（修订后）

| 层级 | 问题 | 影响 |
|------|------|------|
| **数据源** | 宏观是国际新闻（英文），个股是中国公告（中文） | 内容本身交集少 |
| **管线设计** | 宏观管线**刻意强制英文**（`_build_extraction_instructions` 第 849-850 行） | 不是 LLM 失控，是设计如此 |
| **个股管线内部英文** | 白名单 sector 字段是英文粗类（Tech/Consumer/Finance），CLS 硬编码 "STAR Market" | 个股管线自身也产英文 sector |
| **实体抽取** | Sector 无语言约束（SectorEntity 空模型，零字段零描述） | 宏观英文、个股中文，无法合并 |
| **供给量** | SYMBOL 管线只有 22 个 episode（vs MACRO 573） | 桥的一端几乎为空 |
| **ticker 覆盖** | 全库只有 4 个节点带 ticker（白名单仅 10 只） | 即使桥接修好，查自选股也查不到多少 |
| **API 查询** | ticker/sector 格式不匹配 + 跨仓库契约颠倒 | 即使有数据也查不到 |

### 3.5 具体例子

```
宏观新闻："India's textiles sector gains tariff advantage"
  → 提取：Textiles Sector (Sector), India (Country)

个股研报："纺织行业出口企业受益于关税优势"
  → 提取：纺织行业 (Sector), 鲁泰A (Stock)

结果：Textiles Sector 和 纺织行业 是两个独立节点
      无法桥接，查"纺织"只能看到其中一个
```

### 3.6 Sector 类型污染（比语言分裂更严重）

145 个 Sector 节点中，真正是"行业"的可能不到一半：

**中文侧问题**：
- 指数混入：`三板做市指数`, `三板成指`, `恒生指数`, `道琼斯指数`
- 政策概念：`中国式现代化`, `公共租赁住房`, `个人养老金基金产品`
- 垃圾节点：`非银金融/保险Ⅱ`（斜杠拼接）

**英文侧问题**：
- 族群/种姓：`Adivasis`, `Dalit`, `OBCs`, `Gen Z cohort`
- 哲学/文化：`Critical Philosophy of Race`, `Critique`, `European sensibility`, `Bollywood`, `hip-hop`
- 金融工具/指数：`EUR swap curve`, `Brent crude futures`, `gilt markets`, `sovereign debt`
- 媒体/栏目：`InvestingPro GBP/USD`
- 消防队：`FDNY firefighters`
- HTML 实体泄漏：`China&rsquo;s trucking sector`

**结论**：Sector 统一方案必须先加语义准入（exclusion rule：仅股票市场可交易的行业/板块/概念），再做语言归一，最后合并存量。

### 3.6.1 个股管线 sector 词汇自身分裂

不仅宏观/个股之间存在语言分裂，**个股管线自身也产英文 sector**：

| 来源 | sector 值 | 问题 |
|------|----------|------|
| `data/ticker_whitelist.json` | `Tech`, `Consumer`, `Finance`（仅 3 类英文粗分类） | `eastmoney_adapter.py` 把白名单 sector 原样塞进 pre-extracted entities |
| `cls_adapter.py` L266 | `"STAR Market"`（硬编码） | 科创板标的统一标为 "STAR Market" |
| LLM 抽取 | `白酒Ⅱ`, `非银金融`（中文细分类） | 与白名单英文粗类并存 |

**后果**：即使统一了宏观管线的语言规则，白名单和 CLS 硬编码仍会持续污染 Sector 词汇。P1-2 必须覆盖这两个数据源。

### 3.7 实施路径：复用现有基础设施

**现有基础设施**（不要另起炉灶）：

| 组件 | 位置 | 功能 |
|------|------|------|
| `entity_canonical.py` | `src/utils/entity_canonical.py` | ALIAS_MAP（平面小写 alias→canonical 字典）+ `canonical_name(name, entity_type)`（entity_type 参数已预留但未启用） |
| `canonical_entities.yaml` | `data/canonical_entities.yaml` | 规范名映射，**已有 Sectors/Themes 区块**（6 个行业条目，英文→中文方向，如 `半导体: [Semiconductor, Semiconductors, Chip, Chips]`） |
| CANONICAL ENTITY NAMES 注入 | `episode_writer.py` L891 | 当前只注入 episode.entities 的 name+ticker，Sector 规范名无注入通道 |

**实施步骤**：
1. `canonical_entities.yaml` **扩充**现有 Sectors/Themes 区块，覆盖白名单粗类（Tech/Consumer/Finance）、STAR Market 及高频宏观行业词
2. 让 `sector` 字段也走 `canonical_name` 归一（`adapters/models.py` L74 EntityItem 归一当前只覆盖 `name`，`sector` 字段不经过 `canonical_name`——这才是白名单 "Tech"/"STAR Market" 能原样入库的原因）。**不要给 ALIAS_MAP 加类型维度**（会破坏现有 9 处调用点）
3. `_build_extraction_instructions` 改造 CANONICAL ENTITY NAMES 注入通道，把 Sector 规范名注入 prompt
4. 白名单 `ticker_whitelist.json` sector 字段中文化（或映射）
5. `cls_adapter.py` L266 硬编码 "STAR Market" 替换为 "科创板"

### 3.8 API 响应与下游消费

#### NewsEngine API 当前设计

| 端点 | 返回格式 | 说明 |
|------|---------|------|
| `GET /api/events/active` | `{events: [EventItem], total, freshness}` | 扁平事件列表 |
| `GET /api/events/entity/{ticker}` | `{ticker, events: [EventItem], summary}` | 该 ticker 相关事件 |
| `GET /api/events/sector/{sector_name}` | `{sector, events: [EventItem], statistics}` | 该行业相关事件 |

每个 `EventItem` 包含：
- `event_id`, `title`, `summary`, `severity`, `valid_at`
- `entities: [{name, type, ticker}]` — 扁平的实体列表

**关键问题**：API 返回的是扁平列表，不是图结构。没有 `nodes` + `edges`，没有关系信息。

#### SynapseEngine 当前消费方式

```python
# SynapseEngine 调用 NewsEngine
client = NewsEngineClient.from_config()
events = client.fetch_entity_events("000858.SZ")  # 五粮液
# 返回：{"ticker": "000858.SZ", "events": [...], "summary": {...}}

# SynapseEngine 只使用：
# - event.summary（原文摘要）
# - event.severity（情绪评分）
# - event.entities（关键词列表）
# 不查图关系，不构建事件脉络
```

#### 正确的做法（修复桥接后）

**方案 A：API 返回图结构（推荐）**

```json
{
  "ticker": "000858.SZ",
  "graph": {
    "nodes": [
      {"id": "五粮液", "type": "Stock", "ticker": "000858.SZ"},
      {"id": "白酒", "type": "Sector"},
      {"id": "中国", "type": "Country"}
    ],
    "edges": [
      {"source": "五粮液", "target": "白酒", "type": "BELONGS_TO", "fact": "五粮液属于白酒行业"},
      {"source": "白酒", "target": "中国", "type": "HAPPENED_IN", "fact": "白酒行业受中国政策影响"}
    ]
  },
  "episodes": [
    {"id": "ep1", "title": "关税政策利好纺织行业", "valid_at": "2026-09-01", "entities": ["纺织行业", "中国"]},
    {"id": "ep2", "title": "鲁泰A涨停", "valid_at": "2026-09-02", "entities": ["鲁泰A", "纺织行业"]}
  ]
}
```

**前置清理**：`EventItem.relations` 死字段（`api/models.py` L102，类型描述引用已废弃的 CAUSED_BY/MITIGATES/RELATED_TO，翻译层恒置 None）必须在方案 A 落地前决定去留——删除或明确区别于新增 `graph` 字段，否则会有两个语义混淆的"关系"字段。

**方案 B：API 保持扁平，SynapseEngine 自建图**

如果桥接修复了，每个 `EventItem.entities` 会包含更丰富的信息（宏观+个股），SynapseEngine 可以根据 `entities` 自己构建关联图。

**但核心前提是：桥接必须修复。** 否则宏观和个股的实体名称不一致，无法合并。

#### 结论

| 问题 | 解决方案 |
|------|----------|
| 桥接断裂 | 统一 Sector 命名规范（中英文对齐）+ 语义准入 + 存量清理 |
| API 不返回图 | 修改 API 响应格式，添加 `graph` 字段 |
| SynapseEngine 不查图 | 修改 SynapseEngine，使用图结构构建事件脉络 |

**优先级**：先修桥接（P0），再改 API（P1），最后改 SynapseEngine（P2）。

---

## 四、跨仓库契约问题

### 4.1 ticker 转换 bug（SynapseEngine 端）

`main_dispatcher.py` L113-117：
```python
ticker = symbol.replace(".", "").lstrip("HK").lstrip("0") or symbol.replace(".", "")
if len(ticker) < 4:
    ticker = ticker.zfill(4)
ticker = f"{ticker}.HK"  # ← 无条件加 .HK 后缀
```

**问题**：
- 对 A 股 `SZ.000858`：产出 `SZ000858.HK`（纯垃圾）
- `lstrip("HK")` 是字符集剥除不是前缀剥除，边界情况会多剥

### 4.2 sector 契约中英颠倒

| 端 | 文档 |
|----|------|
| NewsEngine 端点 | `"Sector name in Chinese, e.g. 互联网平台"` |
| SynapseEngine 客户端 | `"sector_name: 行业英文名（如 "Auto"、"Semiconductor"）"` |

同一个接口，两端文档说的语言相反。

### 4.3 entity 端点忽略 limit/min_severity

- SynapseEngine 客户端发送 `params={limit, min_severity}`
- NewsEngine `get_entity_events` 签名只有 `ticker`，没有这两个参数
- FastAPI 丢弃未知 query 参数，客户端以为限流了其实没有

### 4.4 3 天硬编码窗口

`_build_entity_events_query` 硬编码 `duration({days: 3})`，做事件脉络太短。

---

## 五、其他发现的问题

### 5.1 双向边问题（低优先级）

- 46 对双向边（~1.5% 噪声，按边算 ~3%）
- 84% 是不同事实（可接受）
- 8% 是同事实冗余
- 6% 是矛盾因果

**结论**：噪声水平可接受，Graphiti Issue #1303 正在修复，暂不需要人工干预。

### 5.2 42 个 episode 完全没有实体

- 占总 episode 的 7%
- 主要是 CLS Telegraph 短消息和 GDELT 低质量事件
- 这些 episode 在图查询中完全不可见

### 5.3 实体碎片化

- `五粮液` 有 10+ 个别名节点：`39度五粮液`, `五粮液1618`, `五粮液集团公司`...
- Graphiti 按名称精确匹配，无法自动合并这些别名
- 跨语言合并（`copper` ↔ `铜`）更不可能

### 5.4 宏观 episode 质量参差

- GDELT 事件很多是无意义的（加拿大医疗政策、印度纺织业关税）
- 与中国股市相关的宏观事件比例很低
- RSS 源质量较高但数量有限

### 5.5 stock.sector='Unknown'

StockEntity.sector 是必填字段，LLM 推断不出就填 Unknown（prompt 明文允许），这些节点在 sector 查询中永久失联。

### 5.6 指数被归类为 Stock

`政府债券指数`、`恒生指数`（Stock/Sector 两侧都有指数）。prompt 写了"指数不是股票"但无 schema/后处理强制。

### 5.7 HTML 实体泄漏

`China&rsquo;s trucking sector` 等。`_clean_text` 只清控制字符，缺 `html.unescape`。一行修复。

### 5.8 EventItem.relations 死字段

`api/models.py` L102 定义 `relations: list[EventRelationItem] | None`，L46 类型描述引用已废弃的 "CAUSED_BY / MITIGATES / RELATED_TO"，翻译层恒置 None。方案 A（API 返回图结构）落地前必须决定去留。

### 5.9 sector 查询无标签过滤

`events.py` L360-361：`MATCH (sector_ent:Entity) WHERE sector_ent.name = $sector_name`，无 `'Sector' IN labels(sector_ent)`。库内存在同名跨类型实体（如"恒生指数"Stock/Sector 两侧都有），会误命中。一行修复，并入 P2-3。

---

## 六、优先级排序（修订后）

| 优先级 | 问题 | 影响 | 工作量 |
|--------|------|------|--------|
| **P0-1** | SYMBOL 管线供给量（22 vs 573） | 桥的一端几乎为空 | 需分析原因 |
| **P0-2** | ticker 覆盖率（4/88）+ 白名单扩充 | 查自选股几乎查不到 | 2-3 天 |
| **P0-3** | 跨仓库 ticker 契约（NewsEngine 服务端归一化 + SynapseEngine 客户端重写） | A 股查询完全失效 | 2-3 天 |
| **P1-1** | Sector 语义准入 + 存量清理（145 个节点中垃圾占一半） | 桥接质量 | 2-3 天 |
| **P1-2** | Sector 语言统一（宏观英文 vs 个股中文 + 白名单/CLS 硬编码） | 桥接断裂 | 2-3 天 |
| **P1-3** | 删除 INVESTS_IN + EXPOSED_TO（含存量迁移） | 减少错误 | 1-2 天 |
| **P2-1** | API 返回图结构 | 事件脉络 | 1-2 天 |
| **P2-2** | sector 契约统一（中英） | 接口一致性 | 0.5 天 |
| **P2-3** | entity 端点接收 limit/min_severity + 窗口调整 + sector 查询加标签过滤 | 接口完整性 | 0.5 天 |
| **P3-1** | HTML unescape | 数据清洗 | 0.1 天 |
| **P3-2** | 双向边处理 | 低噪声 | 等 Graphiti 修复 |

---

## 七、一句话总结

**设计是对的，但实现有多个断层**：

1. **供给断层**：SYMBOL 管线只有 22 个 episode（vs MACRO 573），ticker 覆盖率 4/88
2. **语言断层**：宏观刻意强制英文，个股用中文，实体无法合并；个股管线自身也产英文 sector（白名单 Tech/Consumer/Finance + CLS "STAR Market"）
3. **类型污染**：Sector 节点混有族群/哲学/期货/指数，真正行业占比 <50%
4. **契约断裂**：跨仓库 ticker 转换 bug + sector 中英颠倒 + 参数被忽略
5. **代码冗余**：多余关系类型（INVESTS_IN/EXPOSED_TO）增加复杂度但不增加价值

**最紧急的是 P0**：先解决供给量和 ticker 覆盖率，否则一切查询无米下锅。

---

## 附录：数据验证方法

### Neo4j 查询示例

```cypher
// 检查 Sector 实体跨 scope 桥接
MATCH (sector:Entity)
WHERE 'Sector' IN labels(sector)
OPTIONAL MATCH (sector)<-[r1:RELATES_TO]-(ep_macro:Episodic)
WHERE r1.uuid IN ep_macro.entity_edges AND ep_macro.episode_metadata CONTAINS 'MACRO'
OPTIONAL MATCH (sector)<-[r2:RELATES_TO]-(ep_stock:Episodic)
WHERE r2.uuid IN ep_stock.entity_edges AND ep_stock.episode_metadata CONTAINS 'SYMBOL'
WITH sector, 
     count(DISTINCT ep_macro) as macro_count,
     count(DISTINCT ep_stock) as stock_count
RETURN sector.name as name, macro_count, stock_count
ORDER BY (macro_count + stock_count) DESC

// 检查 ticker 格式
MATCH (stock:Entity)
WHERE 'Stock' IN labels(stock)
RETURN stock.name, stock.ticker, stock.sector
LIMIT 20
```

### 审计脚本

`scripts/audit_neo4j_data.py` 可用于定期验证数据质量。

---

**文档维护者**: 灵汐 (Ling Xi)  
**最后更新**: 2026-09-05  
**版本**: v3.0（经 Architect Review 两轮修订）
