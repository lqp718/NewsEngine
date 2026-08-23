# NewsEngine 重新调查报告：Ticker 来源与 edge_type_map 使用方式

> 日期: 2026-08-21
> 方法: 直接读源码（不重建 GitNexus 索引）+ 直连 Neo4j 查询实际数据（bolt://localhost:7687, 698 实体 / 589 边 / 214 episodes）
> 原则: 不预设结论，用数据说话

---

## 摘要（先说结论）

| 原结论 | 重新调查后的修正 |
|---|---|
| "Ticker 是 LLM 幻觉" | **部分错误**。白名单股票自身的 ticker 来自 SynapseEngine 推送 + adapter 注入，链路是确定的、无幻觉。真正的缺陷是：**SYMBOL 管线的 StockEntity schema 把 ticker 设为必填字段，LLM 被迫给所有被分类为 Stock 的实体（指数、ETF、其他公司）填一个 ticker**，导致 ticker 张冠李戴 + 非白名单 ticker 凭空生成。 |
| "edge_type_map 软约束失效是 P0 缺陷" | **前提错误**。Graphiti 官方 prompt 明确规定：无匹配 fact type 时 LLM 应自行派生 SCREAMING_SNAKE_CASE 类型名。32.6% "不合规"是**设计内的预期行为**，不是失效。且抽样显示自创关系的 fact 内容语义准确；唯一消费方 SynapseEngine 完全不按关系类型过滤。 |

**Boss 的两个质疑基本成立。** 但调查发现了一个比原结论更严重、且此前未被识别的真实缺陷：**4/10 白名单股票在图里根本没有正确的 Stock 节点**（腾讯 0700.HK / 阿里 9988.HK / 京东 9618.HK / 平安 601318.SH），导致 SynapseEngine 对这些标的的查询返回空或返回别人的事件。这是真正的消费者可感知缺陷。

---

## 1. Ticker 数据流完整链路

### 1.1 SYMBOL 管线（个股）— ticker 来源是确定的，无幻觉

```
SynapseEngine watchlist
  └─ push_whitelist() POST /api/tickers/whitelist        [src/api/routers/whitelist.py]
      └─ data/ticker_whitelist.json（原子写入，10 条，source=test_data）
          └─ AkShareAdapter.fetch() 按 biz_code 遍历白名单     [src/adapters/akshare_adapter.py]
              └─ 每条新闻 item 附加 _ticker_full/_ticker_name/_ticker_sector/_ticker_exchange
                  （全部来自白名单 entry，不是新闻文本）
                  └─ normalize() 构建 EntityItem(type="stock", ticker=ticker_full)
                      └─ EpisodeWriter._build_extended_body()    [src/graphiti/episode_writer.py]
                          └─ episode 正文末尾追加:
                             "CANONICAL ENTITY NAMES:\n- 宁德时代 (300750.SZ)"
                          └─ Graphiti.add_episode() → LLM 提取 → EntityNode.attributes.ticker → Neo4j
```

**关键代码证据**（akshare_adapter.py `normalize()`）：
```python
if ticker_full:
    kwargs = {"type": "stock", "name": ticker_name or symbol, "ticker": ticker_full}
```
ticker 100% 来自白名单，与新闻内容无关。**Boss 的判断在这条链路上是正确的：不存在幻觉。**

### 1.2 MACRO 管线 — 根本不产生 ticker

- `MACRO_ENTITY_TYPES`（entity_types.py）包含 Organization/Country/Topic/Policy/Sector/Event，**没有 Stock 类型**。
- GDELT adapter 源码中无任何 ticker 引用（grep 验证）。
- RSS adapter 有一个 `\$([A-Z]{2,5})` 正则预提取（如 $AAPL），但 MACRO writer 的 entity_types 里没有 Stock，LLM 无法把它们分类成带 ticker 的 Stock。
- **结论：MACRO 管线不是 ticker 幻觉的来源。** 此前"沪深300→300750.SZ 是 MACRO 幻觉"的归因是错的。

### 1.3 那 沪深300指数→300750.SZ 到底是谁干的？— SYMBOL 管线 + 必填字段逼迫

Neo4j 实查（`MATCH (ep)-[:MENTIONS]->(n {name:'沪深300指数'})`）：

| 实体 | ticker | 来源 episodes（全部 SYMBOL scope） |
|---|---|---|
| 沪深300指数 | 300750.SZ | EastMoney Research: 宁德时代/平安银行 研报 |
| 标普500指数 | 300750.SZ | EastMoney Research: 宁德时代/山西证券/平安银行 研报 |
| 三板成指 | 300750.SZ | EastMoney Research: 宁德时代/平安银行 研报 |
| 恒生指数 | **9618.HK**（京东的 ticker！） | AkShare Stock News |
| 恒生科技ETF | 00700.HK | AkShare Stock News: 03690 |
| 纳斯达克指数 | IXIC.GI | CLS 电报 + 东财研报 |
| 蔚来-SW | 9866.HK | AkShare Stock News: 09618 |
| 海特高新 | 002023.SZ | CLS 电报（走 _symbol_writer） |

**机制（根因）**：`StockEntity.ticker` 在 entity_types.py 中定义为 `Field(...)`（必填）。研报/新闻里提到"沪深300指数"时，LLM 在 SYMBOL schema 下把它归类为 Stock，然后**被必填字段逼着填一个 ticker** —— 要么抄最近的 canonical name（300750.SZ），要么抄研报主角的 ticker（恒生指数→9618.HK），要么动用自身知识编一个真实存在的代码（9866.HK、IXIC.GI、002023.SZ）。

**即：ticker 的"值"大多不是幻觉（多数是白名单值或真实代码），但"归属"是错的。原结论"ticker 幻觉"应改名为"ticker 错误归属 + schema 诱导填充"。**

---

## 2. 真实缺陷（新发现）：白名单股票自身的节点覆盖不全

对 10 个白名单 ticker 逐一查节点归属：

| ticker | 期望实体 | Neo4j 实际 | 判定 |
|---|---|---|---|
| 0700.HK | 腾讯控股 | **无任何节点**（只有 GDELT 建的无 ticker 的 "00700" 节点） | ❌ |
| 9988.HK | 阿里巴巴-W | 无（"阿里巴巴-W" 节点是 Organization，无 ticker） | ❌ |
| 3690.HK | 美团-W | 美团-W ✓（另有误挂的 159128） | ✅ |
| 1810.HK | 小米集团-W | 小米集团-W ✓ | ✅ |
| 9618.HK | 京东集团-SW | **只有恒生指数、恒生科技ETF华夏**（ticker 挂错对象） | ❌ |
| 000001.SZ | 平安银行 | 平安银行 ✓ | ✅ |
| 600519.SH | 贵州茅台 | 贵州茅台 ✓ | ✅ |
| 000858.SZ | 五粮液 | 五粮液 ✓ | ✅ |
| 601318.SH | 中国平安 | 无（只有"中国平安人寿…" Organization） | ❌ |
| 300750.SZ | 宁德时代 | 宁德时代 ✓（另有 3 个误挂的指数） | ✅（但有污染） |

**消费者影响（实锤，非理论）**——SynapseEngine `main_dispatcher._fetch_negative_news()` 调用 `fetch_entity_events(ticker)`，而 NewsEngine 的查询是 `MATCH (ent:Entity) WHERE ent.ticker = $ticker`：

- `fetch_entity_events('0700.HK')` / `('9988.HK')` / `('601318.SH')` → **404，腾讯/阿里/平安永远"没有新闻"**
- `fetch_entity_events('9618.HK')` → 返回**恒生指数**的事件，被当作京东的新闻
- `fetch_entity_events('300750.SZ')` → 混入沪深300/标普500/三板成指的 episodes（实查：3 个关联 episodes 中 2 个来自误挂节点）

这才是应该立项修复的问题。**根因有两个**：
1. Graphiti 实体消歧（dedup）把 "腾讯控股" 与先前 MACRO 管线建的 "00700"/"阿里巴巴" 等节点合并，合并时丢失了 ticker 属性（MACRO 节点无 ticker，先入为主）。
2. ticker 依赖 LLM 填充，无确定性保证。

---

## 3. edge_type_map 使用方式评估

### 3.1 Graphiti 官方设计意图（读 .venv 源码）

`graphiti_core/prompts/extract_edges.py`（第 165-166 行）对 LLM 的指令原文：

```
- If FACT_TYPES are provided and the relationship matches one of the types
  (considering the entity type signature), use that fact_type_name as the relation_type.
- Otherwise, derive a relation_type from the relationship predicate
  in SCREAMING_SNAKE_CASE (e.g., WORKS_AT, LIVES_IN, IS_FRIENDS_WITH).
```

**结论：edge_type_map 在 Graphiti 设计中就是"建议词表 + 属性 schema 选择器"，不是硬约束。** 无匹配时自行派生类型名是**官方规定的 fallback 路径**，示例注释里举的正是 WORKS_AT、LIVES_IN。

`edge_operations.resolve_extracted_edges()` 对 edge_type_map 的唯一机制性用法是：按边两端节点 label 查 map，决定给这条边**套用哪个 Pydantic 属性 schema**（如 AFFECTS 的 severity、CAUSED_BY 的 confidence）做二次属性提取。没有任何"丢弃不在 map 中的边"的逻辑 —— 自创类型的边照常入图，只是没有自定义属性。

### 3.2 我们的用法评估

| 检查项 | 评估 |
|---|---|
| 传参方式（edge_types + edge_type_map 一起传） | ✅ 符合官方用法 |
| 6 种类型是否"太少" | 类型数量不是问题本身；问题是把 32.6% 自创类型当作"缺陷"去治理，方向错了 |
| ("Entity","Entity") 全量放行 | ✅ 合理，等价于官方默认 |
| 自定义属性（severity/confidence）是否被消费 | ❌ events.py API 不暴露边属性；translation.py 中 `relations` 恒为 None —— 我们精心定义的 6 个 schema 的属性**实际上无人消费** |

### 3.3 LLM 自创关系的质量抽样（25+ 条，实查 Neo4j）

高频自创类型 fact 抽样：

| 类型（数量） | 示例边 | fact 内容 | 语义判定 |
|---|---|---|---|
| WORKS_AT (15) | 李彦宏→百度 | "李彦宏担任百度董事长兼CEO" | ✅ 准确 |
| WORKS_AT | 王军→平安银行 | "王军担任平安银行总行行长助理及副行长" | ✅ 准确 |
| OPERATES_IN (15) | 萝卜快跑→迪拜 | "萝卜快跑已在迪拜开启全无人商业化运营" | ✅ 准确 |
| OWNS (10) | 谷歌→Waymo | "谷歌旗下Waymo为其无人出租车自研定制芯片" | ✅ 准确 |
| OWNS | B2Gold→Fekola mine | "Fekola mine remained Mali's largest gold producer..." | ✅ 准确 |
| ACQUIRED (4) | OceanaGold→Ausgold | "buying Ausgold for A$776 million (almost $553 million)" | ✅ 准确且含细节 |
| ATTACKS (2) | 胡塞武装→沙特阿美公司 | "胡塞武装袭击了纳季兰沙特阿美公司的敏感目标" | ✅ 准确 |
| IMPORTS_FROM (7) | Japan→Middle East | "Japan imported 90% of its crude oil from the Middle East" | ✅ 准确 |
| LOCATED_NEAR (5) | Antino gold project→Paramaribo | "Antino lies about 275 km south of Paramaribo..." | ✅ 准确 |
| CEO_OF / CHAIR_OF / APPOINTED_AT | 高管任职系列 | 含继任者、委员会等细节 | ✅ 准确 |
| CONDEMNS (1) | 伊朗外交部→美国 | "2026年8月20日发表声明谴责美国对伊朗实施新一轮经济制裁" | ✅ 准确 |

**抽样结论：自创类型的边在事实层面质量很高，语义完全正确，只是类型名不在我们的 6 类词表里。这 192 条边不是"垃圾数据"，而是未被词表覆盖的有效知识。**

---

## 4. SynapseEngine 消费需求分析

读 `SynapseEngine/src/clients/news_engine_client.py` + 调用方：

| 客户端方法 | NewsEngine 端点 | 过滤维度 | 是否用到关系类型 |
|---|---|---|---|
| `check_health()` | GET /api/events/health | — | ❌ |
| `fetch_active_events()` | GET /api/events/active | severity + sector | ❌ |
| `fetch_entity_events()` | GET /api/events/entity/{ticker} | **ticker（实体属性）** | ❌ |
| `fetch_sector_briefing()` | GET /api/events/sector/{name} | sector 名 | ❌ |
| `push_whitelist()` | POST /api/tickers/whitelist | — | — |

消费方字段用途：
- `consumer_adapters.py`：EventItem → SentimentRawData（summary→raw_text、severity、source）写 MongoDB
- `main_dispatcher.py`：取 title/summary/severity/source，只保留 high/critical
- `mirofish_runner.py`：sector briefing 文本
- EventItem.relations 字段在 NewsEngine 侧恒为 None，SynapseEngine 侧零引用

**结论：SynapseEngine 只按「实体 ticker / sector 名 / severity」过滤，只消费「事件标题、摘要、严重度、来源、实体 ticker」。关系类型名和边属性在当前消费链路上零价值。32.6% 的"不合规"对消费者没有任何影响。**

---

## 5. 修正后的问题定义与推荐方案

### 问题 A（修正后）：ticker 错误归属 + 白名单节点覆盖不全 —— 真实优先级 P1

**定义**：`StockEntity.ticker` 必填 + ticker 依赖 LLM 填充，导致：
(a) 非白名单实体（指数/ETF/其他公司）被强填 ticker（抄白名单值或编真实代码），污染 `fetch_entity_events` 结果；
(b) 实体消歧合并时丢失 ticker，4/10 白名单标的查不到事件。

**推荐方案（按代价从低到高）**：
1. **确定性后补全（推荐首选）**：写入后不信任 LLM 填的 ticker。用白名单做 `name → ticker` 确定性 Cypher 补全：对 name 命中白名单（含 canonical_name 归一）的 Stock 节点强制 SET ticker；对 name 不在白名单的 Stock 节点 REMOVE ticker。这同时解决 (a)(b)，且不改变写入链路。
2. **改 schema**：把 `StockEntity.ticker` 改为 Optional，并在 StockEntity docstring 中注明"仅当实体在提供的 CANONICAL ENTITY NAMES 列表中时才分类为 Stock，且 ticker 必须逐字使用列表中的值；指数/ETF 不得分类为 Stock"。缓解但不能根除（LLM 仍可能违规），需配合方案 1。
3. **实体消歧防合并**：在 ENTITY RESOLUTION RULES 中补充"带 ticker 标注的名称不得与无标注的近似名合并"，或对 Stock 节点做 group 内 name 唯一性校验。

### 问题 B（修正后）：edge_type_map —— 降级为"无需修复"，可选 P3 增强

**定义修正**：不存在"软约束失效"问题。当前行为符合 Graphiti 设计意图；自创关系事实质量高；消费方不需要类型词表。

**可选增强（仅当未来消费方需要按关系类型过滤时再做）**：
- 把高频自创类型（WORKS_AT×15、OPERATES_IN×15、OWNS×10、IMPORTS_FROM×7、ACQUIRED×4、LOCATED_NEAR×5、MET_WITH×4、INVESTED_IN×3 等约 10 个）纳入 EDGE_TYPES，为它们补上 fact/valid_at 属性 schema，让 Graphiti 的属性二次提取覆盖这些边。
- **不建议**做写入后强制收敛/重命名到 6 类 —— 会损失语义（WORKS_AT 和 AFFECTS 语义完全不同），且无消费者收益。

### 遗留说明

- 此前报告引用的"32.6% 不合规"数据本身属实（192/589，本次实查复核一致），但定性错误。
- MACRO 管线 315 canonical / 186 invented vs SYMBOL 管线 77 / 14：自创类型主要来自 MACRO 管线（国际矿业并购、地缘冲突新闻），这些新闻里雇佣/所有权/地理关系天然不在我们面向"事件→股票影响"设计的 6 类词表内，属词表覆盖面问题而非质量问题。
- `data/ticker_whitelist.json` 当前 source=test_data（2026-07-25），SynapseEngine 推送链路代码存在但数据为测试数据，生产联调时需确认推送实际发生。

---

## 附录：关键证据索引

| 结论 | 证据位置 |
|---|---|
| ticker 来自白名单注入 | `src/adapters/akshare_adapter.py` normalize()；`src/graphiti/episode_writer.py` `_build_extended_body()` |
| ticker 必填导致强填 | `src/graphiti/entity_types.py` StockEntity.ticker = Field(...) |
| MACRO 无 Stock 类型 | `src/graphiti/entity_types.py` MACRO_ENTITY_TYPES |
| Graphiti 允许自创类型 | `.venv/.../graphiti_core/prompts/extract_edges.py` L165-166 |
| edge_type_map 只选属性 schema | `.venv/.../graphiti_core/utils/maintenance/edge_operations.py` resolve_extracted_edges |
| 误挂节点与泄漏 episodes | Neo4j 实查（见正文表格） |
| 消费方不用关系类型 | `SynapseEngine/src/clients/news_engine_client.py`；`src/api/routers/events.py` 全部 Cypher 无 r.name 过滤 |
| relations 恒为 None | `src/graphiti/translation.py` L167 |
