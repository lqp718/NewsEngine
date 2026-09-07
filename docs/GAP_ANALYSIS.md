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
> 所有"待验证"项都需要一次全量 replay（`--replay-all`）或重新运行后再审计。

| # | Gap | 状态 | 影响场景 | 优先级 |
|---|-----|------|---------|--------|
| G1 | 宏观-个股桥接断裂 | 🟡 代码已修，数据 0 桥接，待重跑验证 | L1 L2 L3 | **P0** |
| G2 | Ticker 覆盖率低 + **格式碎片化** | 🔴 数据未改善；新发现三种格式并存 | L2 | **P0** |
| G3 | SYMBOL 管线供给不足 | 🟡 CLS 动态 scope 已修，供给结构仍偏 | L1 L2 | **P0→P1** |
| G4 | Sector 类型污染 | 🟡 prompt 准入已加，无写后强制、存量 147 节点未清 | L1 | **P1** |
| G5 | API 图结构未被 SynapseEngine 消费 | 🔴 服务端已提供，消费端 0 使用 | L2 L3 | **P2**（依赖 G1/G2） |
| G6 | 跨仓库 ticker/sector 契约 | 🟢 查询侧已修；**存储侧格式收敛缺失**、无契约测试 | L2 | **P1** |
| G7 | 补充发现（Stock 侧准入缺失 / sector=Unknown / 实体碎片化 / MACRO 供给结构） | 🔴 | L1 L2 | P1-P2 |

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
- 立即执行一次全量 replay（landing JSONL 还在）或新跑一轮 ingestion，
  用附录 A1 的桥接查询复测：目标 = 跨 scope 共享 Sector > 0 且持续增长。
- 建立**词表运营闭环**：定期（如每周）跑 sector 审计脚本，把高频新行业词
  补进 canonical_entities.yaml，而不是等桥接失败才发现。
- 中期评估：Sector 归一是否需要写后强制层（对 LLM 输出做 canonical 匹配 +
  近似匹配兜底），不能永远只靠 prompt。

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

## 优先级路线图（建议）

```
第一步（本周）：重跑验证 —— replay-all 或新一轮 ingestion
  └─ 复测：G1 桥接数、G3 scope 分布、G4 新增 Sector 垃圾率
     （修复代码全部已合入，这一步不做，后面全是盲修）

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
