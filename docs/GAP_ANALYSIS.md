# NewsEngine Gap Analysis — 实现 vs 核心业务目标

**日期**: 2026-09-07
**基线**: develop `407bc91`（NewsEngine）+ SynapseEngine develop 当前 HEAD
**方法**: GitNexus 代码分析 + Neo4j 在线数据审计（查询脚本见附录）
**读者**: 内部开发。表述直白，不含对外修饰。

---

## 0. 结论速览

> ⚠️ **重要背景**：当前库内全部 595 个 episode 产自 **2026-09-05**，
> 而数据质量修复 commits（`3a85b62` 09-06、`cf8e2bc` 09-07）在其之后。
> 即：**代码修复已合入，但图谱数据是修复前的存量，修复效果一律未经真实数据验证。**
> 所有“待验证”项都需要一次全量 replay（`--replay-all`）或重新运行后再审计。
>
> ⚠️ **Replay 验证结果（2026-09-08）**：Replay 进行中（352/2665），发现 prompt 约束
> 效果有限——英文 Sector 仍有 20 个，跨 scope 桥接仍为 0，ticker 覆盖 4.8%。
> 根因：LLM 在抽取时同时做翻译，认知负荷大，遵循度低。
> 新增方案：统一语言翻译（在 adapter 写入 JSONL 前调用 LLM 翻译，统一翻译成中文）。

| # | Gap | 状态 | 影响场景 | 优先级 |
|---|-----|------|---------|--------|
| G1 | 宏观-个股桥接断裂 | 🔴 Replay 验证：prompt 约束效果有限，桥接仍为 0；新增统一语言翻译方案 | L1 L2 L3 | **P0** |
| G2 | Ticker 覆盖率低 + **格式碎片化** | 🔴 Replay 验证：覆盖率 4.8%，未改善 | L2 | **P0** |
| G3 | SYMBOL 管线供给不足 | 🟡 Replay 验证：6.25%，目标 15-20% | L1 L2 | **P0→P1** |
| G4 | Sector 类型污染 | 🟡 Replay 验证：英文 Sector 20 个，准入规则未完全生效 | L1 | **P1** |
| G5 | API 图结构未被 SynapseEngine 消费 | 🔴 服务端已提供，消费端 0 使用 | L2 L3 | **P2**（依赖 G1/G2） |
| G6 | 跨仓库 ticker/sector 契约 | 🟢 查询侧已修；**存储侧格式收敛缺失**、无契约测试 | L2 | **P1** |
| G7 | 补充发现（Stock 侧准入缺失 / sector=Unknown / 实体碎片化 / MACRO 供给结构） | 🔴 | L1 L2 | P1-P2 |
| G8 | 基础设施审查：Entity/Edge Type 定义缺陷 + 双向边 | 🔴 Entity Type 缺语言约束；Edge Type 定义与实际脱节；双向边是 Graphiti 架构问题 | G1 G4 L1-L3 | **P0** |

---

## G1. 宏观-个股桥接断裂（P0，🟡 代码已修/数据未验证）

**问题描述**
L1/L2/L3 全部依赖宏观 episode 与个股 episode 通过共享实体（核心是 Sector）汇合。
修复前：宏观管线刻意强制英文实体名（含 Sector），个股管线产中文 Sector，
两侧 Sector 永远是不同节点 → 图谱实际是两个孤立网络。

**影响场景**: L1（行业推演的桥就是 Sector）、L2（个股→行业→宏观的上溯路径）、L3（跨层传播链）

**当前状态（证据）**
- 数据（09-05 存量，实测）：`MACRO 573 / SYMBOL 22` 个 episode；
  跨 scope 共享 Sector 实体数 = **0**（Cypher 见附录 A1）。
  英文 Sector（`Textiles Sector`、`Aviation`、`Brent crude futures`）与
  中文 Sector（`白酒Ⅱ`、`旅游酒店`）各自孤立。
- 代码（已修，`cf8e2bc`/`3a85b62`）：
  - `episode_writer.py`：SECTOR LANGUAGE RULES（宏观管线 Sector 例外强制中文 canonical）
    + SECTOR ADMISSION RULES + CANONICAL SECTOR NAMES 词表注入（`_build_sector_names_block`）
  - `cls_adapter.py` L283：`"STAR Market"` 硬编码 → `"科创板"`
  - `adapters/models.py` L78：`EntityItem.sector` 经 `canonical_name()` 归一
  - `canonical_entities.yaml`：新增机器可读 `sectors:` 区块（~30 个 canonical 条目）
  - 回归测试：`tests/test_sector_rules.py`（含 `test_macro_no_longer_forces_english_for_sectors`）

**残余风险（修复覆盖不到的部分）**
1. **词表是封闭集，宏观语料是开放集**。canonical_entities.yaml 只有 ~30 个行业条目；
   GDELT/RSS 的行业词远不止这些（实测存量已出现 `Bakken`、`Permian`、`Potash`、
   `GLP-1 therapies`…）。词表外的英文行业词 LLM 会自行翻译，译名不收敛
   （`Mining→有色金属` 还是 `采矿业`？）——桥接质量取决于词表覆盖速度。
2. **非 Sector 实体仍跨语言分裂**：宏观侧 Organization/Stock 是英文
   （规则只给 Sector 开例外），个股侧是中文。跨语言实体合并（`copper`↔`铜`、
   `Tencent`↔`腾讯控股`）只有 ALIAS_MAP 平面映射一条路，覆盖率低（见 G7.3）。
3. **零验证**：修复后没有任何一次真实写入。prompt 约束是软约束，
   LLM 实际遵从率未知。

**建议修复方向**

### Replay 验证结果（2026-09-08，已停止）

进程启动时间：Sep 7 23:15（晚于代码修改时间 Sep 6 11:42，用的是新代码）
进程停止时间：Sep 8 00:37（Boss 确认后停止）

**最终数据**（394 episodes done / 2265 pending）：
- 跨 scope 桥接：**0**（仍为 0，未改善）
- Sector 节点：100 个（英文 Sector 仍存在）
- Ticker 覆盖：4/84（4.8%，未改善）
- SYMBOL 占比：22/394（5.6%，目标 15-20%）

**结论**：Prompt 约束效果有限，LLM 没有完全遵守 SECTOR LANGUAGE RULES。
英文新闻中的行业词被直接抽取为英文 Sector，导致跨 scope 桥接仍为 0。

**根因**：LLM 在抽取时同时做翻译，认知负荷大，遵循度低。
需要把翻译从抽取阶段剥离出来，交给确定性任务。

---

### 方案 A：Prompt 约束（已实施，效果有限）
- 立即执行一次全量 replay（landing JSONL 还在）或新跑一轮 ingestion，
  用附录 A1 的桥接查询复测：目标 = 跨 scope 共享 Sector > 0 且持续增长。
- 建立**词表运营闭环**：定期（如每周）跑 sector 审计脚本，把高频新行业词
  补进 canonical_entities.yaml，而不是等桥接失败才发现。
- 中期评估：Sector 归一是否需要写后强制层（对 LLM 输出做 canonical 匹配 +
  近似匹配兜底），不能永远只靠 prompt。

### 方案 B：统一语言翻译（推荐，2026-09-08 新增）

**背景**：Replay 验证发现 prompt 约束效果有限。RSS 源（英文）抽取的 Sector
仍有 20 个英文节点（如 "Energy"、"Mining"、"Artificial intelligence"），
跨 scope 桥接仍为 0。LLM 在抽取时同时做翻译，认知负荷大，遵循度低。

**方案**：在 adapter 写入 JSONL 之前调用 LLM 进行翻译，统一翻译成中文。

```
Adapter → NormalizedEpisode → [翻译层] → JSONL → IngestWorker → Graphiti
```

**实现要点**：
1. **翻译层位置**：`NormalizedEpisode` 创建后、写入 JSONL 前
2. **翻译策略**：
   - 宏观源（RSS/GDELT/CLS macro）→ 翻译成中文
   - 个股源（CLS stock/Eastmoney）→ 保持中文
   - 专有名词（公司名、人名）→ 保留原文或加括号注释
   - 关键数据（数字、日期、ticker）→ 不翻译
3. **参考词表**：复用 `canonical_entities.yaml` 作为翻译参考
4. **基础设施**：复用 `QwenNoThinkingClient`（已有 `enable_thinking: False`）

**优势**：
1. **源头统一**：后续所有环节（抽取、去重、桥接）都处理同一种语言
2. **白名单直接匹配**：canonical_entities.yaml 是中文，翻译后直接命中
3. **解耦翻译和抽取**：翻译是确定性任务（有参考词表），抽取是开放性任务
4. **成本可控**：翻译 prompt 比抽取简单（~2K tokens vs ~8K tokens），成本约 ¥0.5/天

**解决的 Gap**：
- **G1**：跨 scope 桥接断裂 → 都用中文 → canonical 词表直接匹配 → 自动桥接
- **G4**：Sector 垃圾率高 → 翻译成 canonical 中文名（"有色金属"）
- **G2**（间接）：公司名统一中文 → 白名单直接匹配 → ticker 覆盖提升

**需要修改的地方**：
1. 新增翻译模块：`src/translation/translator.py`
2. 修改 adapter 输出：在 `to_normalized_episode()` 后调用翻译
3. 配置项：`translate_enabled: bool`、`translate_target_language: str`

**为什么翻译成中文而非英文**：
- 现有基础设施全部是中文（canonical_entities.yaml、ticker_whitelist.json、SECTOR ADMISSION RULES）
- 下游 API 查询是中文（`sector_name='白酒'`）
- 最终用户是中文
- 翻译成中文 = 零改动现有基础设施；翻译成英文 = 全部推翻重写

**风险**：
- 翻译层增加延迟（约 1-2 秒/episode）
- 翻译层增加成本（约 ¥0.5/天）
- 翻译可能丢失部分语义（但行业/sector 有 canonical 词表兜底，准确率接近 100%）

---

## G2. Ticker 覆盖率低 + 格式碎片化（P0，🔴）

**问题描述**
L2 以 ticker 为核心索引（`WHERE start.ticker = $ticker` 精确匹配）。两个子问题：
① 覆盖率：绝大多数 Stock 节点无 ticker；② **格式：有 ticker 的节点格式不统一**
（本次审计新发现，比覆盖率更隐蔽——节点"看起来有 ticker"但查询命不中）。

**影响场景**: L2（直接失效）；L1（个股端点→行业上溯断链）

**当前状态（证据）**
- 覆盖率（实测）：88 个 Stock 节点，**仅 4 个带 ticker**
  （平安银行/五粮液/贵州茅台/中国平安，全部来自白名单 grounding 的研报/公告 episode）。
  其余 84 个（鲁泰A、兆易创新、ST章鼓…）是 LLM 从新闻文本抽的，无 ticker 来源。
- 格式碎片化（代码证据，三种写入格式并存）：
  | 写入方 | 格式 | 示例 | 代码位置 |
  |--------|------|------|---------|
  | 白名单/EastMoney 族 | `代码.交易所` | `000858.SZ` | whitelist json / eastmoney adapters |
  | CLS | `交易所+代码` 前缀式 | `SH603986` | `cls_adapter.py`：`stock_id.upper()` |
  | CNInfo | 裸代码 | `000858` | `cninfo_adapter.py` L373：`"ticker": sec_code`（exchange 单独字段） |
- API 侧无归一化：`events.py` entity 端点直接 `start.ticker = $ticker` 精确匹配。
  SynapseEngine 按契约发 `000858.SZ` → CLS 写入的 `SH603986` 节点、
  CNInfo 写入的 `000858` 节点**永远命不中**。
- 白名单已扩至 76 只（中文申万风格 sector）且已 git 跟踪
  （修复了"gitignored 运行时缓存被 push 覆盖回退"的问题）——但这只扩大
  grounding 范围，不解决 LLM 自由抽取的 84 个无 ticker 节点。

**建议修复方向**
- **先定 canonical 格式**：全库统一 `代码.交易所`（与对外契约一致），
  在适配器写入层收敛（CLS/CNInfo 归一后再进 EntityItem），存量数据跑一次迁移脚本。
- 服务端防御：entity 端点接收 ticker 后做格式归一再查询（容忍上游变体），
  或在图中为 Stock 节点维护 ticker 别名集。
- 覆盖率：白名单外个股的 ticker 回填是独立问题——方向是引入
  名称→ticker 的参照数据（AkShare 全市场股票列表），对 LLM 抽取的 Stock
  节点做写后 grounding；不要试图扩白名单解决（白名单语义是"关注列表"，不是全市场）。

---

## G3. SYMBOL 管线供给不足（P0→P1，🟡）

**问题描述**
桥的两端严重失衡：SYMBOL episode 只占 3.7%。个股侧供给不足时，
即使桥接修好，L1/L2 查出来的个股事件也寥寥无几。

**影响场景**: L1（个股反应端空缺）、L2（事件量少）

**当前状态（证据）**
- 数据（实测，09-05 存量）：MACRO 573 / SYMBOL 22。
  22 个 SYMBOL 全部来自 EastMoney Research（11）+ CNInfo（11）；
  **CLS 62 条全部进了 MACRO**——因为当时代码硬编码 `content_scope="MACRO"`。
- 代码（已修）：`cls_adapter.py` L202 动态判定
  （`stock_list` 非空 → SYMBOL），P0-1；白名单 76 只扩大 EastMoney/CNInfo/研报抓取面。
- 供给结构问题（未修）：
  - MACRO 侧 **OFAC Sanctions 200 条（占全部 episode 34%）**+ GDELT 183 条。
    制裁名单类 episode 事件密度低、与"中国股市宏观背景"相关度低，
    稀释 LLM 预算与图谱信噪比。
  - CLS 的 SYMBOL 产出率取决于财联社编辑的 stock_list 标注率，不可控。
  - AkShare 在 TIER_MAP 中属 SYMBOL 源，但其产出（行情/财务指标）
    是否形成有意义的 episode 供给，存量数据中不可见（0 条 akshare episode）。

**建议修复方向**
- 重跑后复测 scope 分布，目标：SYMBOL 占比 ≥ 15-20%（CLS 动态分流 + 白名单 76 只生效）。
- 审视 Sanctions 源的取舍：降频（Tier 3→更低）、收紧过滤，或明确其在
  L1-L3 中的消费场景；若无场景，考虑默认关闭。
- AkShare 管线实测验证：确认其 episode 是否真实产出并带 stock 实体。

---

## G4. Sector 类型污染（P1，🟡 准入已加/存量未清）

**问题描述**
Sector 是 L1 的桥接实体，但库内 Sector 标签被大量非行业实体污染：
指数、政策口号、族群、哲学领域、媒体栏目、金融工具、HTML 碎片。
污染直接后果：sector 查询误命中、桥接到垃圾节点、briefing 聚合失真。

**影响场景**: L1（核心）；L2（sector 上溯路径质量）

**当前状态（证据）**
- 数据（实测）：147 个 Sector 节点，抽样即见：
  `Adivasis`/`Dalit`/`OBCs`（族群）、`Critical Philosophy of Race`（哲学）、
  `Bollywood`/`American pop`（文化）、`FDNY firefighters`（应急机构）、
  `EUR swap curve`/`Brent crude futures`/`gilt markets`（金融工具）、
  `InvestingPro GBP/USD`（媒体栏目）、`China&rsquo;s trucking sector`（HTML 实体泄漏）、
  `中国式现代化`类政策概念、`恒生指数`类指数。真正"可交易行业"占比目测 < 50%。
- 代码：
  - 已修（增量防御）：SECTOR ADMISSION RULES prompt 注入（`episode_writer.py`
    `_SECTOR_ADMISSION_RULES`，负面例子覆盖上述全部垃圾类别）；
    HTML unescape（P3-1，`_clean_text`）；canonical_entities.yaml 收录规则明文禁止非行业实体。
  - 未修（存量）：**没有任何 sector 存量清理/迁移脚本**
    （`scripts/` 只有 edge 类型 renormalize 和 prompt leakage 清理）。
    147 个节点被 115 条 BELONGS_TO 边引用，直接删会断个股侧归属链。
  - 未修（强制层）：准入只是 prompt 软约束，无写后校验。LLM 违规输出照单全收。

**建议修复方向**
- 存量清理需要迁移方案而非一刀切：垃圾 Sector 上的 BELONGS_TO 边
  （个股→`AI应用概念` 这类合法概念 vs 个股→垃圾节点的误挂）先分类，
  合法边保留、误挂边降级 RELATES_TO 或删除，再删节点。
- 建立 sector 准入的**写后强制层**或定期审计脚本（audit_neo4j_data.py 扩展），
  把"新入库 Sector 是否命中负面类别"变成可度量指标。
- 与 G1 词表运营合并：canonical_entities.yaml 的 sectors 区块就是白名单语义，
  可考虑"词表外 Sector 打标待审"策略。

---

## G5. API 图结构未被 SynapseEngine 消费（P2，🔴 消费端）

**问题描述**
NewsEngine 已按图结构优先原则交付 P2-1（entity 端点返回 graph + episodes），
但唯一下游 SynapseEngine 完全不消费——图能力的价值闭环没有形成。

**影响场景**: L2（个股上下文只有扁平事件）、L3（脉络追踪无人使用）

**当前状态（证据）**
- NewsEngine 侧（已交付）：`events.py` `include_graph=true`（默认）、
  `graph_depth 1-3`、`_build_entity_graph_query`/`_build_graph_structure`、
  失败降级 `graph=None`；`EventItem.relations` 死字段已删除（M-2 解决）。
- SynapseEngine 侧（实测代码）：`src/clients/news_engine_client.py`
  全文无 `graph` 字段消费；`main_dispatcher.py` 只取
  `events[].severity/title/summary` 做负面新闻过滤；
  `news_ingestion_node.py` 只做健康检查 + 扁平事件注入。
- **依赖警示**：即使 SynapseEngine 今天接上 graph，拿到的也是 G1/G2/G4 状态下的图——
  子图多半是孤立节点（无 ticker 命不中、Sector 桥为 0）。**消费端改造应排在桥接验证之后**，
  否则会基于坏图构建错误的"事件脉络"。

**建议修复方向**
- 顺序：G1/G2 replay 验证通过 → SynapseEngine 客户端增加 graph/episodes 解析 →
  PM Agent 决策链消费图上下文（宏观事件如何进入个股判断，由 SynapseEngine 设计）。
- 接口层可以先加契约测试锁定 graph 响应 schema（nodes/edges 字段），
  避免消费端开发时服务端再次漂移。

---

## G6. 跨仓库 ticker/sector 契约（P1，🟢 查询侧已修/存储侧未收敛）

**问题描述**
历史上四个契约问题：ticker 转换 bug、sector 中英颠倒、limit/min_severity 被丢弃、
3 天硬编码窗口。**查询侧已全部修复**；残余问题在存储侧格式收敛（已并入 G2）
与契约的回归保护。

**影响场景**: L2

**当前状态（证据）**
- 已修（两端实测）：
  - SynapseEngine `main_dispatcher.py` L116：改用 `ticker_utils.symbol_to_ticker()`
    （支持 HK/SZ/SH，不再无条件加 `.HK`）——原 `lstrip("HK")` 垃圾转换已移除
  - sector 契约两端统一中文（客户端 docstring："行业中文名如 汽车/互联网平台"）
  - NewsEngine entity 端点消费 `limit`/`min_severity`（Cypher 内过滤，P0-3）
  - 窗口 `entity_events_window_days` 可配置（默认 7 天，原硬编码 3 天）
- 残余：
  1. **存储侧 ticker 格式碎片化**（CLS `SH603986` / CNInfo `000858` / 白名单 `000858.SZ`）
     ——契约只统一了"查询入口"，没统一"库里存什么"。详见 G2，修复归 G2。
  2. **无跨仓库契约测试**：两端契约靠 docstring 和人工对齐，
     任何一侧改格式/字段没有回归保护（历史上 sector 中英颠倒就是这么产生的）。

**建议修复方向**
- 契约测试：最小集是"SynapseEngine 发出的 ticker/sector 样本 → NewsEngine
  entity/sector 端点能命中"的端到端断言（可用固定 fixture 图数据）。
  放哪一侧都行，但必须存在且 CI 跑。
- 契约文档单一来源：ticker 格式、sector 语言、响应 schema 写进
  Design Doc §8.2（已做），两侧代码注释引用它而不是各自复述。

---

## G7. 补充发现（本次审计新增）

### G7.1 Stock 侧语义准入缺失（P1）
Sector 有准入规则，**Stock 没有**。实测：`政府债券指数`、`恒生指数` 被标为 Stock；
prompt 写了"指数不是股票"但无 schema/写后强制。指数节点混入 Stock 会污染
L2 的 ticker 查询空间与 BELONGS_TO 归属统计。方向：Stock 准入规则
（可交易证券口径）+ 与 G4 共用写后校验层。

### G7.2 Stock.sector='Unknown' 失联节点（P1）
实测 12/88 Stock 节点 sector 为 Unknown/null（LLM 推断不出时 prompt 明文允许填 Unknown）。
这些节点在"按 sector 反查个股"（`stock.sector = sector_ent.name`）中永久失联，
L1 行业聚合会漏掉它们。方向：白名单/参照数据回填 sector（与 G2 的
名称→ticker grounding 同一套基础设施），或 sector 查询改走 BELONGS_TO 边为主、
属性匹配为辅。

### G7.3 实体碎片化（P2）
`五粮液` 存在 10+ 别名节点（`39度五粮液`、`五粮液1618`、`五粮液集团公司`…）；
ALIAS_MAP 只覆盖 Top 50 高频实体。碎片化稀释事件聚合（同一标的的事件分散在多个节点），
L2 查询召回不完整。方向：白名单标的的别名自动扩展（子品牌/后缀变体规则），
跨语言别名依赖 G1 的词表运营。

### G7.4 MACRO 供给结构偏斜（P2，与 G3 合并处理）
OFAC 200 + GDELT 183 占 MACRO 供给 67%，其中大量与中国市场无关
（加拿大医疗政策、法国选举、印度纺织关税）。L1 的"宏观事件"应该是
**对用户持仓/自选有传导意义的宏观**，当前供给是"全球噪音平铺"。
方向：宏观源按 L1 消费场景重新定权（ChinaMacro/FRED/Treasury/RSS 财经源优先，
GDELT/Sanctions 收紧主题过滤），而非全量入库后指望 LLM 抽取。

---

## G8. 基础设施审查：Entity/Edge Type 定义缺陷 + 双向边（P0，2026-09-08 新增）

**问题描述**
Graphiti 的抽取质量取决于 Entity Type 和 Edge Type 的定义质量。审查发现：
1. **Entity Type doc 没有让 LLM 理解 type 代表什么**：doc 描述过于简单，LLM 不知道这个 type 的业务意义
2. **Edge Type 定义与实际严重脱节**：定义了 6 种，其中 3 种是死类型；LLM 自创了 5 种
3. **共同 Entity Type 存在但语言不统一**：Sector/Country/Organization 等 6 个类型在 MACRO 和 SYMBOL 管线都有定义，但抽取结果语言不一致导致无法匹配（翻译层解决）

**影响场景**: G1（桥接断裂的根因之一）、G4（Sector 污染）、所有依赖图谱质量的场景

**Graphiti 社区最佳实践**：
> "Custom entity types are Pydantic models with a docstring describing the entity category. **The LLM uses both the class name and docstring to classify extracted entities.**"
> 
> — DeepWiki: Graphiti Podcast Processing Example

即：`__doc__` 是 LLM 分类实体的核心依据，应该描述"这个 type 代表什么、业务意义是什么"。

**当前状态（证据）**

### Entity Type 审查

| Entity Type | Doc 完备度 | 问题 |
|-------------|-----------|------|
| StockEntity | ⚠️ 中等 | doc 说"可交易标的"，但没说业务意义（个股端的核心实体） |
| SectorEntity | ❌ 不足 | doc 只说"行业概念"，没说业务意义（桥接核心） |
| CountryEntity | ❌ 不足 | doc 只说"国家/地区"，没说业务意义（地缘政治参与者） |
| OrganizationEntity | ❌ 不足 | doc 只说"组织/机构"，没说与 Stock 的区别 |
| PolicyEntity | ⚠️ 中等 | 有 type/status 枚举，但 doc 没说业务意义 |
| EventEntity | ✅ 较好 | doc 说"CAMEO 事件"，有字段说明 |

**核心问题**：大部分 entity type 的 doc **没有让 LLM 理解这个 type 代表什么、业务意义是什么**。LLM 不知道 Sector 是桥接核心，不知道 Country 是地缘政治参与者，导致抽取质量低。

### Edge Type 审查

当前 `relation_types.py` 定义了 6 种：

| Edge Type | Doc 完备度 | 实际使用 | 问题 |
|-----------|-----------|---------|------|
| AFFECTS | ⚠️ 中等 | 92 条 | 方向描述模糊，说"任意→Stock"但实际是任意→任意 |
| CAUSED_BY | ⚠️ 中等 | 0 条 | 死类型，LLM 不用 |
| MITIGATES | ⚠️ 中等 | 0 条 | 死类型，LLM 不用 |
| BELONGS_TO | ✅ 较好 | 92 条 | 方向明确 Stock→Sector |
| LOCATED_IN | ⚠️ 中等 | 0 条 | 死类型，被 HAPPENED_IN 替代 |
| RELATED_TO | ⚠️ 中等 | 169 条 | 兜底，但描述说"事件→政策"太窄 |

**LLM 自创的类型**（不在定义中，但实际使用）：

| Edge Type | 数量 | 说明 |
|-----------|------|------|
| HAPPENED_IN | 273 | 最多！LLM 用来表示"发生在某地" |
| INVOLVES | 272 | 第二多！LLM 用来表示"参与关系" |
| PART_OF | 53 | 与 BELONGS_TO 语义重叠 |
| TRIGGERS | 25 | 因果链 |
| TRADED_ON | 6 | 股票→交易所 |

### 共同 Entity Type 分析

| Entity Type | MACRO | SYMBOL | 桥接作用 | 实际数据 |
|-------------|:-----:|:------:|---------|----------|
| **Sector** | ✅ | ✅ | **核心桥接**：宏观事件→行业←个股 | 中文 78 / 英文 22 |
| **Organization** | ✅ | ✅ | 机构关联（如"美联储"影响"银行"） | 中文 ~400 / 英文 ~128 |
| **Country** | ✅ | ✅ | 地域关联（如"中国"→"白酒"） | 中文 ~150 / 英文 ~53 |
| **Policy** | ✅ | ✅ | 政策关联（如"降息"→"房地产"） | - |
| **Event** | ✅ | ✅ | 事件关联 | - |
| **Person** | ✅ | ✅ | 人物关联 | - |
| Topic | ✅ | ❌ | MACRO 独有 | - |
| Stock | ❌ | ✅ | SYMBOL 独有 | - |

**结论**：共同 entity types 定义是有的（6 个），但语言不统一导致无法匹配。
**语言不统一问题由翻译层解决**（见 G1 方案 B），本 Gap 聚焦于 doc 质量问题。

**建议修复方向**

### 1. Entity Type Doc 改进

根据 Graphiti 社区最佳实践，Entity Type 的 `__doc__` 作用是：
> **描述这个 entity category 代表什么**，让 LLM 知道"这个实体应该归类为哪个 type"

LLM 使用 **class name + docstring** 来分类抽取的实体。

**Doc 应该包含**：
- **语义定义**：这个 entity type 代表什么概念
- **业务意义**：为什么需要这个 type，在业务场景中扮演什么角色
- **典型示例**：什么样的实体应该被归类为这个 type

**Doc 不应该包含**：
- ~~语言约束~~：翻译层会处理，不需要在 doc 中约束
- ~~canonical 参考~~：LLM 不知道 `canonical_entities.yaml` 的存在
- ~~负面示例~~：entry type 是 LLM 应该提取的东西，没有"不让提取"的概念

**示例（SectorEntity 改进后）**：
```python
class SectorEntity(BaseModel):
    """行业/板块实体 — 代表一个可交易或可投资的行业分类。

    业务意义：Sector 是宏观事件与个股之间的桥接实体。
    宏观事件影响某个行业，个股属于某个行业，通过 Sector 建立关联。

    典型示例：
    - 传统行业：白酒、房地产、有色金属、煤炭
    - 新兴行业：人工智能、新能源、半导体、生物医药
    - 概念板块：国企改革、一带一路、碳中和

    注意：Sector 应该是行业级别的概念，不是具体公司或产品。
    """
```

**示例（CountryEntity 改进后）**：
```python
class CountryEntity(BaseModel):
    """国家/地区实体 — 代表一个主权国家或重要经济体。

    业务意义：Country 是地缘政治、贸易政策、宏观经济事件的核心参与者。
    通过 Country 可以追踪"美国对中国加征关税"等跨国事件链。

    典型示例：
    - 主权国家：中国、美国、日本、德国
    - 经济体：欧盟、东盟
    - 地区：中东、东南亚（仅当作为整体参与事件时）

    注意：城市、省份不是 Country，除非它们作为独立参与者出现。
    """
```

### 2. Edge Type 精简方案

根据 GAP 文档和实际数据，精简到 4 种：

| Edge Type | 用途 | 实体对约束 | doc 改进 |
|-----------|------|-----------|----------|
| **BELONGS_TO** | 归属 | Stock→Sector, Subsidiary→Parent | 明确"结构性归属" |
| **INVOLVES** | 参与 | Person→Org, Event→Person | **新增**！LLM 已在用（272 条） |
| **AFFECTS** | 影响 | Event→Stock/Sector/Country | 放宽方向约束 |
| **RELATES_TO** | 兜底 | 任意→任意 | 明确"无法归类时使用" |

**删除**：
- CAUSED_BY（0 条，死类型）
- MITIGATES（0 条，死类型）
- LOCATED_IN（0 条，被 HAPPENED_IN 替代）
- PART_OF（与 BELONGS_TO 重叠）
- TRIGGERS（25 条，可合并到 AFFECTS）
- TRADED_ON（6 条，可合并到 BELONGS_TO）

**新增**：
- INVOLVES（LLM 已在用，272 条，是核心类型）

### 3. Edge Type Doc 改进

根据 Graphiti 社区最佳实践，Edge Type 的 `__doc__` 作用是：
> **描述这个 relationship 代表什么**，让 LLM 知道"这两个实体之间是什么关系"

**Doc 应该包含**：
- **语义定义**：这个 edge type 代表什么关系
- **方向约定**：A→B 还是 B→A（如 Stock→Sector 表示"股票属于行业"）
- **实体对约束**：哪些类型的实体之间可以用这个关系
- **典型示例**：什么样的关系应该被归类为这个 type

**Doc 不应该包含**：
- ~~语言约束~~：翻译层会处理，不需要在 doc 中约束

示例（BELONGS_TO 改进后）：
```python
class BelongsToEdge(BaseModel):
    """BELONGS_TO 关系: 结构性归属。

    语义定义：表示一个实体是另一个实体的组成部分或分类归属。
    方向约定：子 → 父（如 Stock → Sector 表示"股票属于行业"）

    实体对约束：
    - Stock → Sector: 股票属于某个行业
    - Subsidiary → Parent: 子公司属于母公司
    - Product → Category: 产品属于某个品类

    典型示例：
    - "0700.HK 属于 互联网平台 行业"
    - "Tencent 是一家 科技 公司"
    - "贵州茅台 属于 白酒 行业"

    不要用于：
    - 地理位置（用 HAPPENED_IN 或 LOCATED_IN）
    - 时间关系（用 valid_at 属性）
    - 因果关系（用 AFFECTS 或 TRIGGERS）
    """

    fact: str = Field(..., description="描述归属关系的事实陈述")
```

### 4. 双向边问题（Graphiti 架构缺陷）

Graphiti 的 `get_between_nodes` 只查单向 `(a)-[r]->(b)`。如果 LLM 对同一对实体抽了 A→B 和 B→A，系统不会去重。随着数据量增长，双向边会累积。

**影响场景**: L1/L2/L3（图查询时双向边导致重复遍历或矛盾信息）

**修复方向**：
- 短期：在 prompt 中明确边的方向约定（如"政策→行业"而非"行业→政策"）
- 中期：评估是否需要在 EpisodeWriter 层加写前去重（查询两个方向是否已存在）
- 长期：等 Graphiti 官方修复（Issue #1303）

### 5. 实施顺序

1. **先改 Entity Type doc**：让 LLM 知道每个 type 代表什么、业务意义是什么
2. **再精简 Edge Type**：删除死类型，新增 INVOLVES（4 种核心类型）
3. **最后改 Edge Type doc**：明确语义、方向、实体对约束

**注意**：
- Doc 改进解决的是"LLM 理解"问题：让 LLM 知道 type 代表什么
- 翻译层解决的是"语言统一"问题：让抽取结果语言一致
- 两者独立，可以并行实施

**社区最佳实践参考**：
- Graphiti 官方文档："Clear Descriptions: Always include detailed descriptions in docstrings and Field descriptions"
- DeepWiki："The LLM uses both the class name and docstring to classify extracted entities"
- 关键：docstring 是 LLM 分类实体的依据，应该描述"这个 type 代表什么"，而不是"怎么抽取"

---

## 优先级路线图（建议）

```
第一步（本周）：基础设施修复 + 统一语言翻译（G1/G4/G8 根源修复）
  ├─ 1a. Entity Type doc 改进：添加语言约束（配合翻译层）
  ├─ 1b. Edge Type 精简：删除死类型，新增 INVOLVES（4 种核心类型）
  ├─ 1c. 实现翻译层：src/translation/translator.py
  ├─ 1d. 修改 adapter 输出：NormalizedEpisode → 翻译 → JSONL
  └─ 复测：G1 桥接数、G4 新增 Sector 垃圾率、Edge Type 分布
  （翻译层是确定性任务，有 canonical 词表兜底，效果可预期）

第二步（P0）：ticker 格式收敛（G2/G6 残余）
  └─ canonical 格式定义 → 适配器写入层归一 → 存量迁移 → API 端防御性归一

第三步（P1）：存量治理
  └─ G4 Sector 清理迁移（含 BELONGS_TO 边分类）
  └─ G7.1 Stock 准入 + G7.2 sector=Unknown 回填（共用 grounding 基础设施）
  └─ G3 供给结构调整（Sanctions 降权、AkShare 验证）

第四步（P2）：价值闭环
  └─ G6 跨仓库契约测试
  └─ G5 SynapseEngine 消费 graph/episodes（前提：第一、二步验证通过）
  └─ G7.3 实体碎片化治理、G7.4 宏观源定权
```

---

## 附录：数据验证方法

审计时间 2026-09-07，Neo4j 在线库（595 episodes，时间范围 09-05 08:22–11:12 UTC）。

**A1. 跨 scope 桥接检查（G1）**
```cypher
MATCH (e:Episodic)-[r:RELATES_TO]->(n:Entity)
WHERE r.uuid IN e.entity_edges AND 'Sector' IN labels(n)
WITH n, collect(DISTINCT CASE WHEN e.episode_metadata CONTAINS 'MACRO' THEN 'MACRO'
     WHEN e.episode_metadata CONTAINS 'SYMBOL' THEN 'SYMBOL' ELSE 'OTHER' END) AS scopes
WHERE size(scopes) > 1
RETURN n.name, scopes
// 实测结果：0 行
```

**A2. scope 分布（G3）**：episode_metadata 无独立 content_scope 属性，
需从 JSON 字符串解析。实测：MACRO 573 / SYMBOL 22。

**A3. ticker 覆盖率（G2）**
```cypher
MATCH (n:Entity) WHERE 'Stock' IN labels(n)
RETURN count(n) AS total,
       count(CASE WHEN n.ticker IS NOT NULL AND n.ticker <> '' THEN 1 END) AS with_ticker
// 实测：88 / 4
```

**A4. Sector 污染抽样（G4）**
```cypher
MATCH (n:Entity) WHERE 'Sector' IN labels(n) RETURN n.name ORDER BY n.name
// 实测：147 个，抽样垃圾类别见 G4
```

**A5. 边类型分布**：语义类型在 `RELATES_TO.name` 属性（非 Neo4j 关系类型）。
实测仅 8 种核心类型（INVESTS_IN/EXPOSED_TO 已绝迹，P1-3 生效）：
RELATES_TO 894 / INVOLVES 803 / HAPPENED_IN 623 / AFFECTS 337 /
PART_OF 155 / BELONGS_TO 115 / TRIGGERS 89 / TRADED_ON 21。

---

**维护者**: Architect
**关联文档**: `docs/NEWSENGINE-DESIGN-DOC.md`（V3.0）、
`docs/DATA_QUALITY_AND_ARCHITECTURE_REVIEW_20260905.md`（历史诊断，本文档为其修复后的现状复核）
