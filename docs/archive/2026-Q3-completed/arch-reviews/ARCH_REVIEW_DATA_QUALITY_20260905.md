# ARCH Review：《数据质量与架构诊断报告》代码验证结论

**Reviewer**: Architect
**日期**: 2026-09-05
**被审文档**: `docs/DATA_QUALITY_AND_ARCHITECTURE_REVIEW_20260905.md`
**验证方法**: 源码逐行核查（episode_writer.py / relation_types.py / entity_types.py / translation.py / api/routers/events.py / graphiti-core 0.29.3 内部实现）+ Neo4j 实库查询复核全部数值 + SynapseEngine 消费端交叉验证 + 9 天日志（8/27–9/5）证据检索

---

## 〇、总体结论

**数值层面：全部属实。逻辑层面：两处关键论断不成立，一处自相矛盾。完整性层面：遗漏了 10+ 个问题，其中 3 个比文档已列问题更致命。**

- ✅ 文档中所有 Neo4j 统计数字经实库复核**逐项吻合**（关系分布 10 项、46 对双向边、42 个空实体 episode、桥接实体数 = 0、白酒Ⅱ/非银金融/保险Ⅱ 命名断裂、INVESTS_IN ~60% 错误率）。
- ❌ **§2 "normalize_edge_type 从未触发、Pydantic schema 已约束 LLM 输出" 论断不成立**（详见二）。该函数每次写入都在触发；graphiti-core 的内置 prompt **明确指示 LLM 在无匹配类型时自行发明 SCREAMING_SNAKE_CASE 类型**，写后兜底是唯一防线。
- ❌ **§1.4 结论与自己的表格自相矛盾**：表格说 INVOLVES/HAPPENED_IN/AFFECTS "保留但简化"，结论却说"保留 4 种"。若真删这 3 种，将把 1642 条边（占全图 54%）压扁回 RELATES_TO，与 8 月底刚完成的重归一工程（投入 44 个单测 + 迁移脚本）方向完全相反。
- ⚠️ 文档遗漏的最致命问题：**SYMBOL 管线只有 22 个 episode（vs MACRO 573）**、**全库只有 4 个节点带 ticker（白名单仅 10 只）**、**SynapseEngine 端 ticker 转换代码对 A 股是坏的**。即使桥接修好，"查自选股查到相关事件"的业务目标依然达不成。

---

## 一、重点 1：关系类型（删 INVESTS_IN / TRIGGERS / EXPOSED_TO）

### 验证结论：类型确系 prompt + schema 显式定义，不是 LLM 自行发挥。删除建议方向成立，但文档漏掉了删除的完整成本。

**代码证据**：

1. 三种类型全部在 `CORE_EDGE_TYPES`（`src/graphiti/episode_writer.py` L121-132）中注册。
2. **TRIGGERS**：extraction instructions 明文定义 —— `'"X caused / led to / triggered Y" -> TRIGGERS'`（`_build_extraction_instructions`，episode_writer.py）。
3. **INVESTS_IN**：RELATION TYPE REFINEMENT 明文定义 —— `'INVESTS_IN: for investment relationships. Examples: "X invested $N in Y", "X acquired stake in Y"'`，且有专属 Pydantic 模型 `InvestsInEdge`。
4. **EXPOSED_TO**：**未出现在 instruction 文本中**，但通过 `ExposedToEdge` schema + `_normalized_edge_type_map` 注入 `(Entity, Entity)` 通用对，进入 graphiti 的 `<FACT TYPES>` prompt 区块，LLM 可见其名称与 docstring（"风险暴露"）。属于"半显式定义"。

**INVESTS_IN ~60% 错误率：实库抽样验证属实**。抽 15 条，约 10 条错误：捐赠（Lineage→加州社区基金会 ×5 条）、**学历**（"孙山山拥有深圳大学经济学硕士学位"→ INVESTS_IN 深圳大学）、收购（应归 PART_OF）、勘探作业、政府拨款。正确的仅五粮液 501 项目投资主体等 3-4 条。错误率 ~67%，与文档一致。

**文档遗漏的删除成本**（必须补进方案）：

| 遗漏项 | 说明 |
|--------|------|
| 存量 265 条边的迁移 | INVESTS_IN 103 + TRIGGERS 88 + EXPOSED_TO 74。删类型定义不删数据，库内会留下孤儿类型名；需迁移策略（正确子集改 PART_OF/AFFECTS，错误子集降级 RELATES_TO 或删除） |
| `normalize_edge_type` 前缀规则联动 | "TRIGGER"/"INVESTS"/"EXPOSED" 前缀分支删除后，这些输入会兜底成 RELATES_TO —— 行为变化需要过一遍 44 个单测（`tests/test_episode_writer.py`） |
| `_normalized_edge_type_map` 的 `(Entity, Entity)` 全核心集注入 | 删 CORE_EDGE_TYPES 成员即可自动生效，但需确认 graphiti 缓存/历史 edge_type_map 无残留 |
| EXPOSED_TO 的抽取入口特殊 | 它不在 instruction 文本里，只靠 schema 注入。若暂不删而是先"停供"，只需从 `_normalized_edge_types` 的 setdefault 列表移除即可，成本极低 |

**Architect 意见**：同意删 INVESTS_IN（错误率实证充分）和 EXPOSED_TO（无 prompt 引导、语义模糊、零消费）。**TRIGGERS 建议缓删**：它有明确 prompt 定义、88 条边、且是因果脉络（文档 §3.1 "先看宏观政策，再看个股反应"）最贴合的类型——业务目标恰恰需要因果边。若因当前零消费而删，将来做事件脉络时还得加回来。建议保留 TRIGGERS 观察一个迭代。

---

## 二、重点 2：normalize_edge_type 删除建议 —— **论断不成立，此节需要重写**

### 验证结论：❌ "从未触发"错误；❌ "Pydantic schema 已约束 LLM 输出"错误；⚠️ 文档引用的代码片段与真实实现不符。

**1. 文档引用的代码是错的。** 文档 §2.1 展示的是 `mapping = {...}; return mapping.get(raw_type, raw_type)`（字典精确映射、默认透传）。真实实现（episode_writer.py L177-232）是**前缀匹配、默认归 RELATES_TO**（不透传）。默认语义完全相反：真实版本会把任何未知类型**压扁**成 RELATES_TO——这正是历史上 ~150 条边丢失语义类型的根因（`scripts/renormalize_edge_types.py` 文档字符串自证："LLM 产出的非核心关系类型被旧版 normalize_edge_type() 兜底归一成通用 RELATES_TO（约 150 条）"）。基于错误代码片段得出的删除结论不可信。

**2. 该函数每次写入都在触发，共 3 个调用点，文档只看到 1 个：**

| 调用点 | 时机 | 是否触发 |
|--------|------|---------|
| `_normalized_edge_types()`（write_one 步骤 2b） | **每次写入前**，把 `EDGE_TYPES` 注册表的遗留键收敛为核心类型：CAUSED_BY→TRIGGERS、MITIGATES→AFFECTS、LOCATED_IN→PART_OF、RELATED_TO→RELATES_TO | **每次必触发**。`relation_types.py` 的 `EDGE_TYPES` 至今仍是 6 个遗留类型，没有这个函数，LLM 拿到的就是遗留类型集 |
| `_normalized_edge_type_map()` | 每次写入前，收敛 `DEFAULT_EDGE_TYPE_MAP` 中所有实体对的允许类型 | 每次必触发 |
| `_normalize_written_edges()` | 写入后兜底，矫正 LLM 偶发产出的 schema 外类型名 | 日志证据：**8/27–9/5 九天全部日志中 "edge type normalized" 出现 0 次**——文档"从未触发"仅对这一个兜底点近似成立 |

**3. "Pydantic schema 约束 LLM 输出范围"是对 graphiti-core 机制的误解。** 实查 graphiti-core 0.29.3 源码：

- `prompts/extract_nodes_and_edges.py` L76-90，内置 prompt 原文：**"If no FACT TYPE fits, derive a relation_type in SCREAMING_SNAKE_CASE (e.g., WORKS_AT, LIVES_IN, IS_FRIENDS_WITH)."** —— 框架**明文鼓励** LLM 在无匹配时发明新类型名。
- `utils/maintenance/edge_operations.py` L655/L780：`edge_model = edge_type_candidates.get(extracted_edge.name)` —— 名称对不上时只是**跳过自定义属性抽取，类型名原样入库**。Pydantic schema 约束的是已知类型的属性字段，**不约束类型名本身**。
- 历史证据：库里曾出现 118 种碎片化类型名（episode_writer.py 注释自证）。

**结论**：当前兜底 0 触发是因为 prompt 引导 + `(Entity, Entity)` 全核心集注入让 LLM 大多数时候能命中类型，**不是因为 schema 强约束**。删掉写后兜底 = 拆掉对抗 graphiti 文档化自由发挥行为的唯一防线，且历史上已经翻过车（118 种碎片名、150 条被压扁）。

**Architect 修正建议**（替代文档 §2.3 的"删除"）：

1. **保留 `normalize_edge_type` 与写后兜底**（成本 ≈ 0，九天 0 触发意味着无性能负担）。
2. 真正的债务在源头：**把 `relation_types.py` 的 `EDGE_TYPES`/`DEFAULT_EDGE_TYPE_MAP` 直接重写为核心类型集**，让 `_normalized_edge_types/_normalized_edge_type_map` 退化为恒等变换后再考虑移除这两层收敛。当前还有一个隐性 bug：收敛后 **PART_OF 复用的 schema 是 `LocatedInEdge`**（fact 描述写着"股票与国家的归属关系"），BELONGS_TO 复用 `relation_types.BelongsToEdge`（无 valid_at 字段）——模型与语义错配，重写注册表时一并修复。
3. 文档指出的 **LOCATED_IN→PART_OF 语义错误成立**（"腾讯位于中国"≠"腾讯是中国的一部分"）。修法不是删函数，而是把该映射改为 LOCATED_IN→HAPPENED_IN（或新增 HEADQUARTERED_IN 需谨慎，会扩大类型集）。注意此映射当前实际生效于 `(Stock, Country)` 实体对，库内 PART_OF 149 条中混有地点语义边，改映射时需配套存量修正。
4. 删除动作牵连 `scripts/renormalize_edge_types.py`（import 该函数）与 `tests/test_episode_writer.py`（44 个测试中大量直接断言映射行为），文档工作量估算 0.5 天严重偏低。

---

## 三、重点 3：Sector 命名统一方案 —— 方向对，但根因定位不准，且漏了比语言更严重的类型污染

### 3.1 "prompt 是否已要求中文但没生效"：答案是**分管线、分字段，且宏观英文是刻意设计**

| 位置 | 现状 | 生效情况 |
|------|------|---------|
| `entity_types.py` StockEntity.sector 字段描述 | 明文要求中文行业名（例：'互联网平台'、'半导体'） | **基本生效**：实库 stock.sector 属性值为 互联网平台×14、金融×9、军工×7 等中文；但混有 **'Unknown'×10** 和英文粗分类（见下） |
| `entity_types.py` SectorEntity | **空模型，零字段、零描述、零语言约束** | Sector 节点名完全由 graphiti 通用实体抽取 + custom instructions 决定 |
| `episode_writer.py` `_build_extraction_instructions` | **宏观管线明文强制英文**："Always extract entity names in English. Translate non-English names to their standard English equivalents."（P1-5.6 注释："宏观管线保持英文"）；个股管线仅要求**白名单公司**用中文标准名，Sector 不在白名单机制内 | **过度生效**：宏观 Sector 英文名（Textiles Sector、Aviation、Chemicals）不是 LLM 失控，是 prompt 明令的结果 |

**文档 §3.4 根因表把责任归给"LLM 按原文语言提取名称"——不准确。** 真实根因是 P1-5.6 的**管线级语言分叉是刻意写进 prompt 的**。因此"Sector 命名统一"不是调 prompt 措辞的小事，而是要**推翻/修正 P1-5.6 的语言规则设计**：至少对 Sector（及桥接关键的 Country）类型改为"无论原文语言，一律输出规范中文名"，并把行业概念映射表（文档 P1 项）的产出注入 `CANONICAL ENTITY NAMES` 区块——当前该区块只注入 `episode.entities` 的 name+ticker（`_build_extraction_instructions` 尾部循环），** Sector 规范名没有任何注入通道**。`data/canonical_entities.yaml` 实测无任何行业别名条目；而 `entity_canonical.py` 的 ALIAS_MAP + `canonical_name(name, entity_type)` 基础设施已存在且支持按类型扩展——**文档完全没有提到这个现成机制，方案应直接复用它**（新增 sector 类别 + 宏观写入前对 Sector 名做 canonical 化），而不是另起炉灶。

### 3.2 文档遗漏：Sector 类型污染比语言分裂更严重（实库取证）

全库 145 个 Sector 节点（中文 46 / 英文及其他 99）。英文侧大量**根本不是行业**的实体被归为 Sector：

> Adivasis（印度族群）、Dalit、OBCs（种姓群体）、American pop、Bollywood、Gen Z cohort、FDNY firefighters（消防队）、Critical Philosophy of Race（哲学流派）、EUR swap curve（利率曲线）、Brent crude futures（期货合约）、Electronic Benefit Transfer（美国福利计划）、InvestingPro GBP/USD（媒体栏目名）

中文侧同样有：**恒生指数、道琼斯指数、三板成指、三板做市指数**（指数应为独立类型或排除，StockEntity prompt 已说"指数不是股票"但 Sector 无对应约束）、`非银金融/保险Ⅱ`（**斜杠拼接的垃圾节点**）、殡葬、垃圾清运（粒度存疑）。另有 HTML 实体泄漏：`China&rsquo;s trucking sector`（内容清洗只做了控制字符，未做 HTML unescape）。

**含义**：即使中英文对齐了，"Textiles Sector↔纺织行业"这类真桥接也只是 145 个节点里的少数；把族群、哲学、期货合约统一成中文名的桥接毫无价值。**Sector 统一方案必须先加语义准入（exclusion rule：仅股票市场可交易的行业/板块/概念），再做语言归一，最后合并存量**——文档的 P0 估算（2-3 天）未包含存量 99 个英文节点的合并/清理，且若不清理，历史数据桥接依然是 0。

### 3.3 文档遗漏：个股管线内部也有 sector 语言分裂

- `data/ticker_whitelist.json` 实测：sector 字段是英文粗分类 **'Tech' / 'Consumer' / 'Finance'**（仅 3 类），而 `StockEntity.sector` 的 prompt 要求中文细分类（'互联网平台'）。`eastmoney_adapter.py` L284/L402 把白名单的英文 sector 原样塞进 pre-extracted entities，`cls_adapter.py` L266 硬编码英文 `"STAR Market"`。
- 也就是说：**"宏观英文 vs 个股中文"的二分法不完整**，个股管线自身同时存在英文粗类（来自白名单/CLS 硬编码）与中文细类（来自 LLM 推断与研报原文）两套 sector 词汇。统一方案必须同时覆盖白名单数据源和 adapter 硬编码。

---

## 四、重点 4：API 返回图结构（方案 A）可行性与改造成本

### 验证结论：可行，成本比文档暗示的更低——数据已在查询路径上，且模型层已有半成品；但文档漏了消费端契约的三处 drift。

**1. Cypher 现状（`src/api/routers/events.py`）**：三个查询都已经 `MATCH ... [rel:RELATES_TO] ...` 并把 rel 用于 `rel.uuid IN ep.entity_edges` 关联，**只是 RETURN 时丢弃了关系对象**。改造为方案 A 只需：

- RETURN 增加 `collect(DISTINCT {src: startNode(rel).name, tgt: endNode(rel).name, type: rel.name, fact: rel.fact})`（rel.name 即语义类型，rel.fact 即事实文本，库内 100% 有值——renormalize 脚本文档字符串自证"fact 描述 100% 完整"）；
- `translation.py` 增加一个 relations 翻译函数；
- 新增响应模型。**注意**：现有 `EventItem.relations` 字段（`api/models.py` L102）是**事件↔事件**关系（`target_event_id`），不是实体图边，且 `translate_episode_to_event` 恒置 `relations=None`——**这是个从未接线的死字段**，其 type 描述还在引用遗留类型名 "CAUSED_BY / MITIGATES / RELATED_TO"。方案 A 需要新增 `graph: {nodes, edges}` 结构（文档示例正确），顺手清掉死字段或明确其不同用途，避免语义混淆。

**成本评估**：NewsEngine 侧 1-1.5 天（查询扩列 + 模型 + 翻译层 + 测试），低于文档未明说的隐含预期；真正的工作量在 SynapseEngine 侧消费改造（文档已列 P2）。

**2. 文档遗漏的三处消费端契约 drift**（比"改造成图结构"更急，因为它们让**现有扁平接口也在失效**）：

| # | 问题 | 证据 |
|---|------|------|
| a | **entity 端点静默忽略 limit/min_severity**：SynapseEngine 客户端发送 `params={limit, min_severity}`（news_engine_client.py L262-268），但 `get_entity_events` 签名只有 ticker——FastAPI 丢弃未知 query 参数，客户端以为限流了其实没有；且服务端 Cypher 硬编码 **3 天窗口**（`duration({days: 3})`），做"事件脉络"太短，文档对此无一字 | events.py `_build_entity_events_query` |
| b | **sector 契约中英颠倒**：SynapseEngine `fetch_sector_briefing` docstring 写明 *"sector_name: 行业英文名（如 'Auto'、'Semiconductor'）"*，NewsEngine 端点是 *"Sector name in Chinese, e.g. 互联网平台"*。两仓库对同一接口的书面契约相反，这不是数据问题，是**接口契约管理缺失** | news_engine_client.py vs events.py |
| c | **ticker 查询是全等匹配无兜底**：`WHERE ent.ticker = $ticker`。格式归一化（000858 ↔ 000858.SZ）应该在服务端做（正则规范化后双格式匹配），而不是指望每个调用方传对——文档把 P0 定为"修 API 格式 bug"但没定位到该改哪一侧 | events.py `_build_entity_events_query` |

---

## 五、重点 5：文档遗漏的问题（按严重度排序）

### 🔴 致命级（直接否决业务目标达成）

**L-1. SYMBOL 管线只有 22 个 episode，MACRO 有 573 个（26:1）。**
实库复核：595 个带 scope 元数据的 episode 中 MACRO=573、SYMBOL=22。文档全部篇幅在修"桥接"，但桥的两端一端是 573 条宏观、另一端只有 22 条个股——**即使语言对齐、映射表建好，查自选股能查到的个股事件依然接近空**。文档 §3.1 的业务目标（"查自选股时相关宏观事件和个股事件都能查到"）的真正瓶颈首先是 SYMBOL 采集量，这一项在优先级表中完全缺席。建议列为 P0 并列项：先回答"为什么个股管线产出这么少"（白名单只有 10 只 × 源覆盖 × 调度频率）。

**L-2. 全库只有 4 个节点带 ticker，88 个 Stock 节点中 84 个 ticker=NULL。**
实库：带 ticker 的节点仅 000858.SZ / 601318.SH / 600519.SH / 000001.SZ 各 1。机制：`_ground_tickers`（episode_writer.py）对**白名单外节点主动 REMOVE ticker**（防幻觉，设计正确），而白名单实测只有 **10 只**（且 6 只是港股，A 股仅 4 只——恰好就是库里有 ticker 的 4 只）。后果：`/api/events/entity/{ticker}` 对全市场实际只能服务 4 个代码。**文档把 ticker 问题定性为"格式不匹配"（§4.3），漏了更根本的"覆盖率≈0"**。修格式之前先扩白名单/改 grounding 策略（如：白名单外但格式合法的 ticker 保留并标记 unverified，而非删除）。

**L-3. SynapseEngine 端 ticker 转换代码对 A 股是坏的（跨仓库 bug）。**
`main_dispatcher.py` L113-117：
```python
ticker = symbol.replace(".", "").lstrip("HK").lstrip("0") or symbol.replace(".", "")
if len(ticker) < 4: ticker = ticker.zfill(4)
ticker = f"{ticker}.HK"   # ← 无条件加 .HK 后缀
```
- 对 `SZ.000858`：产出 **`SZ000858.HK`**（lstrip("HK") 剥不掉 SZ 前缀，最后强行 .HK）——纯垃圾；
- `lstrip("HK")`/`lstrip("0")` 是字符集剥除不是前缀剥除，`HK.00066` 之类边界会多剥；
- 文档 §4.3 说"API 查询格式 000858"，实测真实调用方传的是 `0700.HK` 或垃圾串，**文档连调用方实际传什么都没核实**。P0 "修 API 格式 bug 1-2 天"必须改为跨仓库任务：NewsEngine 服务端归一化 + SynapseEngine 客户端重写转换（按交易所后缀正确映射），否则单修任何一侧都无效。

### 🟠 严重级

**L-4. §1.4 自相矛盾（见〇节）。** 表格保留 INVOLVES(700)/HAPPENED_IN(623)/AFFECTS(319)，结论只留 4 种。若按结论执行，1642 条边（54%）压扁回 RELATES_TO，直接摧毁 8/31 重归一工程的成果，且 `DEFAULT_EDGE_TYPE_MAP` 中 `(Entity,Stock):[AFFECTS]`、`(Event,Country/Organization/Sector):[AFFECTS]` 等实体对约束会失去允许类型。**必须澄清：到底是 7 种还是 4 种。** Architect 意见：保 7 种（AFFECTS/INVOLVES/HAPPENED_IN/PART_OF/BELONGS_TO/TRADED_ON/TRIGGERS 或 RELATES_TO 兜底），删 2 种（INVESTS_IN/EXPOSED_TO）。

**L-5. Sector 类型污染**（详见 3.2）：族群/哲学/指数/期货曲线混入 Sector，145 节点中真行业占比目测 <50%。任何"统一命名"方案不含语义准入与存量清理都会白费。

**L-6. 个股管线 sector 词汇自身分裂**（详见 3.3）：白名单英文粗类（Tech/Consumer/Finance）+ CLS 硬编码 "STAR Market" + LLM 中文细类 + 申万"白酒Ⅱ"风格并存。文档的二分法（宏观英/个股中）掩盖了这一点。

### 🟡 一般级

**L-7. `EventItem.relations` 死字段**：定义存在、翻译层恒 None、类型描述引用已废弃的 CAUSED_BY/MITIGATES 命名（详见四.1）。方案 A 落地前先决定去留。
**L-8. entity 端点忽略 limit/min_severity + 3 天硬窗口**（详见四.2a）。
**L-9. stock.sector='Unknown'×10**：StockEntity.sector 是必填字段，LLM 推断不出就填 Unknown（prompt 明文允许），这些节点在 sector 查询（`stock.sector = sector_ent.name`）中永久失联。文档未提。
**L-10. 指数被归类为 Stock**：`政府债券指数`、`恒生指数`（Stock/Sector 两侧都有指数）。prompt 写了"指数不是股票"但无 schema/后处理强制——又一个"prompt-only 约束不生效"的实证，与 §二 的结论同构。
**L-11. HTML 实体泄漏**：`China&rsquo;s trucking sector` 等。`_clean_text` 只清控制字符，缺 `html.unescape`。一行修复。
**L-12. 双向边占比数字不准**：文档"46 对 = 0.66% 噪声"；实测 46 对确认，但总边数 3029，46/3029≈1.5%（按边算 92/3029≈3%）。结论（噪声可接受）不变，数字应修正。
**L-13. sector 查询 Cypher 无标签过滤**：`MATCH (sector_ent:Entity) WHERE sector_ent.name = $sector_name` 匹配任意同名实体（Organization"恒生指数"等也会命中），应加 `'Sector' IN labels(sector_ent)`。
**L-14. 修复优先级顺序问题**：文档 P0 = "修 API 格式 + 统一 Sector 命名"。结合 L-1/L-2，正确的 P0 应该是：**① SYMBOL 管线供给量与 ticker 覆盖率（否则一切查询无米下锅）→ ② 跨仓库 ticker 契约 → ③ Sector 语义准入 + 语言统一 → ④ API 图结构**。

---

## 六、数值主张核对表（全部经实库复核，2026-09-05）

| 文档主张 | 实测 | 判定 |
|---------|------|------|
| 关系分布 840/700/623/319/149/112/103-88-74/21 | 逐项一致，合计 3029 = 全库边数 | ✅ |
| INVESTS_IN ~60% 错误 | 抽样 15 条 ~10 错（捐赠/学历/收购/勘探），~67% | ✅ |
| 桥接数量 = 0 | 跨 MACRO+SYMBOL 实体数 = **0**（精确复核） | ✅ |
| 42 个 episode 无实体（7%） | 42 / 595 = 7.06% | ✅ |
| 46 对双向边（0.66%） | 46 对 ✅，占比应为 ~1.5%（对/总边） | ⚠️ 数字修正 |
| 白酒 vs 白酒Ⅱ 不匹配 | 两节点均存在，sector 查询为全等匹配 → 确实查不到 | ✅ |
| ticker 000858.SZ vs 000858 | 库内确为 000858.SZ；但真实调用方传的是 0700.HK / 垃圾串（L-3），文档场景描述失真 | ⚠️ 部分 |
| "normalize_edge_type 从未触发" | 写后兜底 9 天日志 0 触发 ✅；但 schema 收敛层**每次写入必触发**，且函数曾被引用代码片段描述错误 | ❌ 论断不成立 |
| "Pydantic schema 已约束 LLM 输出范围" | graphiti-core 0.29.3 prompt 明文允许发明类型；schema 只约束属性不约束类型名 | ❌ |
| "宏观 Sector 英文是 LLM 按原文语言提取" | 是 `_build_extraction_instructions` 宏观分支**明文强制英文**（P1-5.6 刻意设计） | ⚠️ 根因错位 |

---

## 七、对文档的修订要求（发回灵汐）

1. **重写 §2**：撤回"删除 normalize_edge_type"结论；改为"重写 EDGE_TYPES 注册表为核心类型集 + 修正 LOCATED_IN→HAPPENED_IN 映射 + 保留写后兜底"；修正引用的代码片段；补充 44 个单测与 renormalize 脚本的联动成本。
2. **澄清 §1.4**：明确保留 7 种还是 4 种；若坚持 4 种，必须给出 1642 条存量边的处置方案与对重归一工程成果的解释。
3. **§3 补充**：SYMBOL 供给量（22 条）与 ticker 覆盖率（4/88）列为桥接的前置阻塞项；Sector 方案增加语义准入 + 存量 99 个英文节点清理 + 复用 `entity_canonical.py`/`canonical_entities.yaml` 基础设施 + CANONICAL ENTITY NAMES 注入通道改造。
4. **§4 补充**：跨仓库契约清单（ticker 转换 bug、sector 中英契约相反、limit/min_severity 被忽略、3 天窗口），P0 工作量按两个仓库重估。
5. **修正 §5.1** 双向边占比数字；**新增** L-7 ~ L-13 各项。

---

## 附：本次验证的证据来源

- 源码：`src/graphiti/episode_writer.py`、`src/graphiti/relation_types.py`、`src/graphiti/entity_types.py`、`src/graphiti/translation.py`、`src/api/routers/events.py`、`src/api/models.py`、`src/utils/entity_canonical.py`、`src/adapters/eastmoney_adapter.py`、`src/adapters/cls_adapter.py`、`scripts/renormalize_edge_types.py`、`tests/test_episode_writer.py`
- graphiti-core 0.29.3：`prompts/extract_nodes_and_edges.py`（L76-90 自由类型条款）、`utils/maintenance/edge_operations.py`（L655/L780 类型名不校验）
- SynapseEngine：`src/clients/news_engine_client.py`、`src/dispatcher/main_dispatcher.py`（L113-117）
- 数据：`data/ticker_whitelist.json`（10 只）、Neo4j 实库（newsengine-neo4j，3029 边 / 595 episode / 145 Sector / 88 Stock）
- 日志：`logs/news_engine.log*`（2026-08-27 ~ 09-05，"edge type normalized" 0 次命中）
- 历史报告：`reports/neo4j-data-audit-2026-09-05.md`、`reports/data-quality-root-cause-2026-09-04.md`
