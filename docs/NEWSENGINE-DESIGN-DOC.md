# NewsEngine 设计文档

**版本**: V3.0（依据 develop 分支 `407bc91` 反向重写）
**日期**: 2026-09-07
**说明**: 本文档描述 NewsEngine 的当前实现，面向开源社区。旧版（V2.x）以变更史形式组织，
其中大量章节（Crucix 替换、N4 实施验收、MongoDB Schema 迁移、SynapseEngine LLD 替换清单等）
属于一次性迁移记录，与当前代码不符，已删除；历史细节见 git 提交记录与 `docs/archive/`。

---

## 1. 核心业务场景

### 1.1 产品定位

NewsEngine 是一个**金融事件情报系统**：从全球多个数据源实时采集新闻，经 LLM 抽取实体与关系，
构建知识图谱（Graphiti + Neo4j），向下游决策系统提供**事件上下文**。

与传统新闻聚合器的本质区别在于**关系网络**——系统的核心价值是构建**宏观-行业-个股**三层
贯通的事件脉络，让用户看到完整的事件链路，而非扁平的新闻列表。

三个核心场景（下文以 L1/L2/L3 指代）：

| 编号 | 场景 | 一句话目标 |
|------|------|-----------|
| **L1** | 行业事件推演 | 查行业时，看到宏观事件 + 个股反应形成的完整链路 |
| **L2** | 个股事件上下文 | 查股票时，看到关联的宏观/行业事件，而不仅是个股公告 |
| **L3** | 事件脉络追踪 | 追踪一个事件的传播链路和连锁反应 |

### 1.2 场景 L1：行业事件推演

查询某个行业时，系统应返回该行业相关的宏观事件与个股反应，并按时间线组织：

```
查询：纺织行业

├── 宏观事件：India's textiles sector gains tariff advantage
├── 行业影响：纺织出口企业受益于关税优势
├── 个股反应：鲁泰A 涨停
└── 时间线：宏观政策 → 行业影响 → 个股表现
```

**数据流示例**（桥接机制）：不同来源的事件通过共享的 `Sector` 实体连接——

```
宏观新闻（GDELT/RSS，英文语料）
    → LLM 抽取 Sector "纺织"（强制中文 canonical 名）
                      ↓ 同一实体
个股新闻（CLS/EastMoney，中文语料）
    → LLM 抽取 Sector "纺织"
                      ↓
Stock "鲁泰A" ──BELONGS_TO──▶ Sector "纺织"
```

查询时从 `Sector "纺织"` 出发遍历：既命中宏观 episode，也命中个股 episode，
再按 `valid_at` 排成时间线。**Sector 是宏观层与个股层之间唯一的桥接实体**，
其命名一致性（见 §6.3 SECTOR LANGUAGE RULES）直接决定 L1 是否成立。

### 1.3 场景 L2：个股事件上下文

查询某只股票（以 ticker 为索引）时，返回该股票的事件及其关联的宏观/行业上下文，
API 响应为**图结构**（nodes + edges）而非扁平列表：

```
查询：000858.SZ（五粮液）

├── 个股事件：五粮液财报发布、涨停
├── 行业动态：白酒行业政策、消费趋势
├── 宏观背景：中国消费刺激政策、贸易关税
└── 关联图：五粮液 → 白酒 → 中国
```

```json
{
  "ticker": "000858.SZ",
  "events": [ { "event_id": "ep_123", "title": "五粮液发布年报", "severity": "medium", "...": "..." } ],
  "graph": {
    "nodes": [
      {"id": "五粮液", "type": "Stock", "ticker": "000858.SZ"},
      {"id": "白酒", "type": "Sector"},
      {"id": "中国", "type": "Country"}
    ],
    "edges": [
      {"source": "五粮液", "target": "白酒", "type": "BELONGS_TO", "fact": "五粮液属于白酒行业"},
      {"source": "白酒", "target": "中国", "type": "HAPPENED_IN", "fact": "..."}
    ]
  },
  "episodes": [ "...事件脉络时间线..." ]
}
```

L2 的关键依赖是 **ticker 覆盖率**：图中 `Stock` 节点必须携带与查询格式一致的 ticker，
否则 `WHERE start.ticker = $ticker` 无法命中（见 §7.2、§8.2）。

### 1.4 场景 L3：事件脉络追踪

追踪一个事件的传播链路和连锁反应，依赖图谱中的因果/影响关系：

```
查询：关税政策

事件链：
  政策发布 (Country: USA)
       ↓ TRIGGERS
  行业影响 (Sector: 半导体)
       ↓ AFFECTS
  个股反应 (Stock: 中芯国际)
       ↓ RELATES_TO
  供应链调整 (Organization: TSMC)
```

L3 由 `TRIGGERS`（因果）与 `AFFECTS`（影响）关系承载，并通过 episode 时间线
（`episodes` 字段）还原传播顺序。关系类型体系见 §6.2。

### 1.5 设计原则

1. **桥接优先** — 宏观、行业、个股事件必须通过共享实体（尤其是 Sector）关联。
   任何破坏实体命名一致性的设计（如管线间语言不一致）都视为缺陷。
2. **图结构优先** — API 返回节点 + 关系的图结构，而非扁平列表；下游据此构建事件脉络。
3. **Ticker 为核心索引** — 个股查询依赖 ticker；ticker 覆盖率与格式一致性是 L2 的生命线。
4. **数据质量优先** — 宁可少做功能，也要保证图谱质量：语义准入、实体归一、
   关系类型收敛、prompt 注入约束，均服务于此。

---

## 2. 系统架构总览

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          DATA SOURCES                                │
│  🌍 GDELT   📰 RSS   📱 CLS   📋 CNInfo   💹 EastMoney   📈 AkShare    │
│  💵 Treasury  📊 FRED  🛢️ EIA  🌐 ACLED  🚫 Sanctions  📉 BLS  🇨🇳 ChinaMacro │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ADAPTER LAYER (src/adapters/)                     │
│  多源适配 → 标准化输出 NormalizedEpisode                              │
│  • content_scope 标记（MACRO / SYMBOL）                              │
│  • 预抽取实体（白名单 grounding：name + ticker + sector）             │
│  • rule_based_severity 初评分                                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              CONTENT FETCHER (src/utils/news_spider.py,              │
│                              content_fetcher.py)                     │
│  渐进式抓取漏斗：curl_cffi(chrome146) → 备用 TLS 指纹 →               │
│  CloakBrowser(Chromium) → Camoufox(Firefox)                          │
│  静态提取：__NEXT_DATA__ / JSON-LD / Trafilatura；Cookie 池复用       │
│  LLM 预处理：长文 compress（>20K chars → ~5K，熔断保护）              │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│            PERSISTENCE / LANDING ZONE (src/persistence/)             │
│  LandingStore(SQLite) → IngestWorker                                 │
│  • content_hash 去重      • 失败重试 / dead 队列                      │
│  • replay 回放            • retention 定期清理                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│           KNOWLEDGE GRAPH (src/graphiti/ + Graphiti + Neo4j)         │
│  双 Writer（macro / symbol）→ LLM 实体关系抽取                        │
│  • 8 种核心关系类型 + normalize_edge_type 收敛                        │
│  • 实体归一化（canonical_entities.yaml）                             │
│  • Sector 语义准入 + 语言统一（prompt 注入）                          │
│  • severity enrichment + 分级 TTL 淘汰                               │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     API LAYER (src/api/, FastAPI)                    │
│  GET  /api/events/active           — 活跃事件（扁平）                 │
│  GET  /api/events/entity/{ticker}  — 个股事件 + 图结构（P2-1）        │
│  GET  /api/events/sector/{name}    — 行业事件 + sector_briefing      │
│  GET  /api/events/risk-summary     — LLM 风险摘要（缓存）             │
│  GET  /api/events/health           — 健康检查                        │
│  POST /api/tickers/whitelist       — 接收 SynapseEngine 白名单推送   │
└─────────────────────────────────────────────────────────────────────┘
```

调度入口是 `main.py`（FIFO 8 步启动，见 §9.1）；`IngestionScheduler`
（`src/ingestion/scheduler.py`）以 4 个独立 Tier 循环驱动全部适配器（见 §3.2）。

### 2.2 宏观/个股双管线

系统内部始终存在两条并行管线，差异贯穿实体类型、抽取指令与语言策略：

| 维度 | MACRO 管线 | SYMBOL 管线 |
|------|-----------|------------|
| 数据源 | GDELT、RSS、FRED、Treasury、EIA、ACLED、Sanctions、BLS、ChinaMacro | CLS（带 stock_list 的快讯）、EastMoney、EastMoney Research、CNInfo、AkShare |
| content_scope | `MACRO` | `SYMBOL` |
| EpisodeWriter | `_macro_writer` | `_symbol_writer` |
| 实体类型 | EventEntity（Actor1/Actor2/CAMEO/Goldstein）等 | SymbolEventEntity + StockEntity（ticker/sector/exchange）等 |
| 实体名语言 | 英文（**Sector 例外：强制中文 canonical**） | 白名单/规范名内标的用中文标准名 |
| 语料语言 | 以英文为主（国际新闻） | 以中文为主（A 股/港股公告、快讯、研报） |

两条管线在图谱中汇合的唯一通道是**共享实体**：Sector（行业桥）、Stock（ticker 桥）、
Country/Organization（背景桥）。管线判定以 `source_type` 为确定性信号
（`_SYMBOL_WRITER_SOURCES` / `_SYMBOL_SOURCE_TYPES`），不依赖预抽取实体是否带 ticker。

CLS 特殊处理（P0-1）：`content_scope` 按 API 返回的 `stock_list` 动态判定——
有编辑标注个股 → SYMBOL，否则 → MACRO；不再整源硬编码。

### 2.3 目录结构（当前实现）

```
NewsEngine/
├── main.py                     # 入口：FIFO 启动 + CLI 子命令
├── src/
│   ├── adapters/               # 14 个数据源适配器 + 公共模型
│   │   ├── models.py           #   NormalizedEpisode / EntityItem 契约
│   │   ├── base.py             #   BaseAdapter（dedup cache、fetch/normalize 协议）
│   │   ├── gdelt_adapter.py    #   GDELT Events V2 (+ events_pipeline_filter 三段过滤)
│   │   ├── rss_adapter.py      #   RSS（多 feed）
│   │   ├── cls_adapter.py      #   财联社电报（stock_list → SYMBOL/MACRO 动态判定）
│   │   ├── eastmoney_adapter.py        # 东方财富个股新闻（白名单驱动）
│   │   ├── eastmoney_research_adapter.py  # 东财研报（白名单驱动）
│   │   ├── cninfo_adapter.py   #   巨潮公告（PDF 抽取）
│   │   ├── akshare_adapter.py  #   AkShare 行情/财务
│   │   ├── fred_adapter.py / treasury_adapter.py / eia_adapter.py
│   │   ├── bls_adapter.py / acled_adapter.py / sanctions_adapter.py
│   │   ├── china_macro_adapter.py      # 中国宏观指标（PMI/GDP/CPI）
│   │   ├── llm_preprocessor.py #   长文 compress（本地 LLM，熔断降级）
│   │   └── macro_themes.py     #   GDELT 宏观主题白名单（19 主题）
│   ├── ingestion/
│   │   ├── scheduler.py        # 4-Tier 调度 + TTL 清理 + writer 路由
│   │   ├── pipeline.py         # fetch → landing → ingest 单源管线
│   │   ├── severity_enricher.py# Episodic severity 规则回填（Tier 1 周期后）
│   │   ├── briefing_aggregator.py # sector_briefing LLM 聚合（内存缓存）
│   │   └── events_pipeline_filter.py # GDELT CAMEO/Goldstein/Mentions 三段过滤
│   ├── persistence/
│   │   ├── landing_store.py    # SQLite Landing Zone（pending/processing/done/failed/dead）
│   │   ├── ingest_worker.py    # 消费 landing → EpisodeWriter → 标记
│   │   └── models.py           # EpisodeEnvelope / CaptureRunRecord
│   ├── graphiti/
│   │   ├── episode_writer.py   # Graphiti 写入 + 抽取指令注入 + 边类型收敛 + 429 熔断
│   │   ├── entity_types.py     # 实体 Pydantic 模型（Stock/Sector/Event/SymbolEvent…）
│   │   ├── relation_types.py   # 边 Pydantic 模型与实体对映射
│   │   └── translation.py      # 图数据 → API 模型翻译层
│   ├── api/
│   │   ├── server.py           # FastAPI 应用工厂
│   │   ├── deps.py             # Neo4j driver / settings / aggregator 依赖注入
│   │   ├── models.py           # EventItem / GraphStructure 等响应模型
│   │   └── routers/            # events.py / health.py / whitelist.py
│   ├── sync/
│   │   └── ticker_sync.py      # 白名单拉取（SynapseEngine）+ 本地缓存降级
│   ├── core/
│   │   ├── config.py           # Pydantic Settings（.env）
│   │   ├── neo4j_client.py     # driver 单例与生命周期
│   │   ├── graphiti_client.py  # Graphiti SDK 实例（LLM/Embedder 配置）
│   │   └── bailian_llm_client.py / bailian_embedder.py / qwen_no_thinking_client.py
│   └── utils/
│       ├── content_fetcher.py  # 抓取漏斗 + Tier 0 静态提取
│       ├── news_spider.py      # Scrapling Spider（TLS 指纹 / CloakBrowser / Camoufox）
│       ├── entity_canonical.py # ALIAS_MAP + canonical_name() 实体/行业归一
│       ├── time_utils.py / yaml_parser.py / logging_config.py
├── data/
│   ├── ticker_whitelist.json   # 白名单缓存（git 跟踪，SynapseEngine push 可覆盖）
│   ├── canonical_entities.yaml # 实体/行业规范名映射
│   ├── gdelt_events_filter.json# GDELT 三段过滤配置
│   └── landing/ + state.db     # Landing Zone 数据
└── scripts/                    # 运维/审计脚本（audit_neo4j_data.py 等）
```

---

## 3. 数据接入层

### 3.1 数据源清单

| 来源 | 覆盖 | 内容类型 | 管线 | Tier |
|------|------|---------|------|------|
| GDELT Events V2 | 全球 | 地缘政治、冲突、外交 | MACRO | 1 |
| RSS（investing/mining/BBC/ECB/Fed/oilprice…） | 全球 | 金融新闻 | MACRO | 1 |
| CLS 财联社电报 | 中国 | 实时快讯（stock_list 动态分流） | MACRO/SYMBOL | 1 |
| EastMoney 个股新闻 | 中国 A 股 | 白名单标的新闻 | SYMBOL | 1 |
| AkShare | 中国 | 行情、财务指标 | SYMBOL | 1 |
| CNInfo 巨潮公告 | 中国/港交所 | 官方公告（PDF） | SYMBOL | 2 |
| US Treasury | 美国 | 国债收益率曲线 | MACRO | 2 |
| FRED | 美国 | 宏观经济数据 | MACRO | 2 |
| EIA | 美国 | 能源库存/产量 | MACRO | 3 |
| ACLED | 全球 | 冲突事件 | MACRO | 3 |
| Sanctions（OFAC SDN / OpenSanctions） | 全球 | 制裁名单 | MACRO | 3 |
| BLS | 美国 | 劳工统计 | MACRO | 4 |
| China Macro（PMI/GDP/CPI） | 中国 | 宏观指标 | MACRO | 4 |
| EastMoney Research 研报 | 中国 A 股 | 白名单标的券商研报 | SYMBOL | 4 |

### 3.2 多 Tier 调度

`IngestionScheduler` 将全部适配器静态映射到 4 个 Tier（`TIER_MAP`），
每个 Tier 一个独立 asyncio 任务、独立周期，长周期 Tier 不阻塞短周期 Tier：

| Tier | 语义 | 默认间隔 | 适配器 |
|------|------|---------|--------|
| 1 | 实时/高频 | 900s（15 min） | gdelt, rss, cls, eastmoney, akshare |
| 2 | 日频 | 14400s（4 h） | cninfo, treasury, fred |
| 3 | 周频 | 43200s（12 h） | eia, acled, sanctions |
| 4 | 月/季频 | 86400s（24 h） | bls, china_macro, eastmoney_research |

Tier 1 每个周期结束后依次执行：severity enrichment（规则回填 Episodic severity）
→ `SectorBriefingAggregator.aggregate_all()`（行业简报 LLM 聚合，见 §7.3）
→ TTL 清理（每日一次守卫，见 §6.5）。

`CLS → EastMoney → AkShare` 构成个股复合源（CLI `--source stock`），
个股族适配器（`_TICKER_AWARE_SOURCES`）运行前需加载 ticker 白名单（§8.1）。

### 3.3 NormalizedEpisode 契约

所有适配器输出统一契约 `NormalizedEpisode`（`src/adapters/models.py`）：

- `episode_body` — 标题 + 正文拼装（Graphiti episode 内容）
- `source_type` — 管线判定的确定性信号（如 `cls_telegraph`、`gdelt_csv`）
- `content_scope` — `MACRO` / `SYMBOL`
- `content_hash` — SHA-256 去重键（CLS 用 article_id 精化）
- `valid_at` — 事件时间（HKT 解析）
- `severity` — `rule_based_severity()` 关键词初评分
- `entities: list[EntityItem]` — **预抽取实体**（白名单 grounding）：
  `name`（经 `canonical_name()` 归一）、`type`、`ticker`、`sector`（归一为中文 canonical）、`exchange`
- `episode_metadata` — JSON：`content_scope`、`content_fetched`、`extracted_metadata`
  （title/author/url/hostname/date/tags…，供 API 与审计还原来源）
- `content_fetched` — 是否成功抓到全文（GDELT 为 false 的 episode 直接跳过写入，
  防止摘要级噪声入图）

预抽取实体有两个用途：① 作为 CANONICAL ENTITY NAMES 注入抽取 prompt（§6.3），
把白名单标的的 name+ticker 钉进图谱；② `sector`/`exchange` 经 `build_entity_suffix()`
拼入 episode body 尾部，为 LLM 提供行业归属提示。

### 3.4 GDELT 三段过滤

`events_pipeline_filter.py` 对 GDELT Events + Mentions 做三段结构化过滤
（配置 `data/gdelt_events_filter.json`）：

1. **CAMEO 过滤** — 事件码前缀/精确/包含匹配白名单（19 个核心金融主题，`macro_themes.py`）
2. **Goldstein 过滤** — `|goldstein| ≥ min_abs_value`（过滤弱信号事件）
3. **Mentions 过滤** — `len(mentions) ≥ min_count`（过滤孤证事件）

URL 解析策略 `mentions_first`：按置信度排序 + 去重后选取 article URL。

### 3.5 LLM 预处理层（compress）

`llm_preprocessor.py` 在内容抓取与落盘之间提供可选的长文压缩：

- **触发**：抓取的正文超过 `LLM_PREPROCESSOR_COMPRESS_THRESHOLD`（默认 20000 chars）
- **目标**：压缩到 ~`COMPRESS_TARGET`（5000 chars），保留实体、数字、日期、因果陈述
- **使用方**：GDELT 与 RSS（长文占比最高的两个源）
- **确定性**：`temperature=0` + JSON Schema 结构化输出 → 同一内容压缩结果稳定
  （保证 content_hash 去重有效）
- **降级**：任何 LLM 失败（超时/HTTP 错误/解析失败/空输出）返回原文，不阻塞管线；
  连续失败触发熔断（冷却期内跳过 LLM）

> 背景：Graphiti 实体消解会把当前 episode 与最近 10 个 episode 拼接进同一 prompt，
> 超长 episode 会溢出 LLM 上下文。compress 是入库前的长度保险丝。
> （synthesize 模式已于 `407bc91` 移除。）

---

## 4. 内容抓取层（渐进式漏斗）

`utils/news_spider.py`（基于 Scrapling Spider）+ `utils/content_fetcher.py`，
按成本递增逐级降级：

| 层级 | 引擎 | 特点 | 速度 |
|------|------|------|------|
| Tier 1 | `FetcherSession`（curl_cffi，chrome146 TLS 指纹） | 快速 HTTP | ~1s/页 |
| Tier 1.5 | 备用 TLS 指纹（firefox135 → safari15_5） | 绕过指纹封锁 | ~1s/页 |
| Tier 2 | CloakBrowser（补丁 Chromium，C++ stealth） | 绕过 Cloudflare 等挑战 | ~5-15s/页 |
| Tier 3 | Camoufox（Firefox + Juggler） | 终极反检测兜底 | ~10-15s/页 |

- **触发降级**：Tier 1 返回封锁状态码（403/429/503）且检测到 CF 挑战 → 逐级升级
- **Cookie 池**：Tier 2/3 浏览器拿到的 `cf_clearance` 等 cookie 回灌 Tier 1/1.5 复用
- **静态提取（Tier 0，与抓取层级正交）**：任何层级拿到的 HTML 先尝试
  `__NEXT_DATA__` → JSON-LD → Trafilatura 提取正文，提升结构化站点的抽取质量
- **批量接口**：`fetch_batch()` 并发抓取，供适配器在 normalize 前预取全文

---

## 5. 持久化与摄取管线（Landing Zone）

抓取与图谱写入解耦，中间以 SQLite Landing Zone 缓冲（`src/persistence/`）：

```
Adapter.fetch/normalize → LandingStore（pending）→ IngestWorker → EpisodeWriter → Neo4j
                                │ 状态机: pending → processing → done / failed → dead
                                └─ JSONL 原文留存（data/landing/），支持 replay
```

- **EpisodeEnvelope**：landing 行模型（cycle_id、source_type、content_hash、状态、重试计数）
- **去重**：两级——适配器内 dedup cache（TTL 24h，FRED/Treasury/EIA/央行类 7d）
  + landing 层 content_hash 唯一约束
- **失败处理**：写图失败 → `failed`（可重试）→ 超过阈值 → `dead`；
  CLI `--retry-dead` 重新入队
- **replay**：`--replay` / `--replay-all` 从 JSONL 重建图谱（不重新抓取），
  用于 schema/指令变更后的存量重放
- **retention sweep**：每日清理过期（默认 14 天）的 done/skipped/dead 行与 JSONL；
  pending/processing/failed 永不自动清理（未入库数据不丢）
- **两阶段运行**：`--fetch-only`（只抓取落 landing）与 `--ingest-only`
  （只消费 landing 写图）可分离部署，互不阻塞

---

## 6. 知识图谱层（Graphiti + Neo4j）

### 6.1 双 Writer 与实体类型

`EpisodeWriter`（`src/graphiti/episode_writer.py`）封装 Graphiti `add_episode()`，
实例化两个 writer，由 scheduler 按 `source_type` 路由（`_resolve_writer`）：

- **macro writer** — entity_types 含 `EventEntity`（actor1/actor2/cameo_code/
  goldstein_score/tone/event_date，面向 GDELT 结构化字段）等
- **symbol writer** — entity_types 含 `SymbolEventEntity` 与 `StockEntity`
  （ticker/sector/exchange 必填）等

实体类型体系（Neo4j 标签，`Entity` 为公共标签）：

| 类型 | 语义 | 关键属性 |
|------|------|---------|
| Stock | 股票 | ticker、sector、exchange |
| Sector | 行业/板块（可交易口径） | — |
| Organization | 公司/机构 | — |
| Person | 人物（Graphiti 内建） | — |
| Country | 国家/地区 | — |
| Policy | 政策/法规 | — |
| Event | 事件（宏观，GDELT 结构化） | cameo_code、goldstein_score、tone |
| Topic | 主题/概念 | 货币政策/贸易/科技/地缘政治… |

写入侧防泄漏约束：提示词只允许走 Graphiti `custom_extraction_instructions` 通道，
**严禁追加到 episode_body**（历史 prompt leakage 教训：指令文本被当作实体数据入图）。

### 6.2 关系类型（8 种核心）

所有实体关系统一以 `RELATES_TO` 物理边存储，语义类型在边的 `name` 属性上：

| 关系 | 语义 | 示例 |
|------|------|------|
| RELATES_TO | 通用关联（兜底） | 事件 A 与事件 B 相关 |
| INVOLVES | 主体参与/任职 | 人物 → 组织 |
| HAPPENED_IN | 地点归属 | 事件 → 国家 |
| AFFECTS | 影响关系 | 政策 → 行业 |
| PART_OF | 结构组成（所有权/持股/组织包含） | 子公司 → 母公司 |
| BELONGS_TO | 行业/概念归属（**桥接关键**） | 股票 → 行业 |
| TRADED_ON | 上市地点 | 股票 → 交易所 |
| TRIGGERS | 因果关系（**事件脉络关键**） | 事件 A → 事件 B |

已废弃：`INVESTS_IN`（实测 ~60% 错误率，投资/持股事实由 PART_OF/RELATES_TO 承接）、
`EXPOSED_TO`（语义模糊、零消费）。

**边类型收敛**：Graphiti prompt 允许 LLM 在无匹配时发明新类型名（SCREAMING_SNAKE_CASE），
Pydantic schema 不约束类型名。因此 `normalize_edge_type()` 在三处兜底：
① 写入前收敛 EDGE_TYPES 注册表遗留键；② 写入前收敛实体对映射表
（如 LOCATED_IN → PART_OF）；③ 写后扫描归一（对抗 LLM 自由发挥的最后防线）。

### 6.3 抽取指令注入（Prompt 契约）

`_build_extraction_instructions()` 为每个 episode 构建 `custom_extraction_instructions`，
注入以下规则块（宏观/个股管线共用 Sector 规则）：

1. **ENTITY NAME LANGUAGE RULE** — 个股管线：白名单/规范名内标的用中文标准名
   （腾讯控股，非 Tencent Holdings）；宏观管线：实体名用英文，
   **Sector 例外——必须用中文 canonical 名**（P1-2，桥接的语言前提）
2. **SECTOR ADMISSION RULES**（P1-1，语义准入）— Sector 必须是股票市场可交易的
   行业/板块/概念；明确禁止：指数（恒生指数）、政策口号（中国式现代化）、
   族群（Dalit）、学术/哲学领域、媒体栏目、应急服务机构、HTML 碎片
3. **SECTOR LANGUAGE RULES + CANONICAL SECTOR NAMES** — 注入
   `canonical_entities.yaml` sectors 区块的中文 canonical 词表
   （每个 canonical 附至多 1 个 ASCII 别名，如 `纺织 (Textiles)`），
   引导 LLM 把英文行业词映射到中文 canonical
4. **EXCLUSION RULES** — 禁止把新闻机构/数据源（CLS、Reuters、Bloomberg…）抽为实体
5. **RELATION TYPE RULES / REFINEMENT** — 优先具体类型、收窄 RELATES_TO 占比；
   PART_OF 仅限结构性所有权；BELONGS_TO 用于行业归类；TRADED_ON 用于上市地
6. **ENTITY RESOLUTION RULES + CANONICAL ENTITY NAMES** — 当 episode 携带预抽取实体时，
   注入 `name (ticker)` 清单，强制 LLM 使用规范名并钉住 ticker（白名单 grounding）

### 6.4 实体归一化

`utils/entity_canonical.py` + `data/canonical_entities.yaml`：

- **ALIAS_MAP**：平面小写 alias → canonical 字典（如 `tencent holdings ltd.` → 腾讯控股）
- **canonical_name(name, entity_type)**：适配器预抽取实体（EntityItem 构造时）
  与 sector 字段的归一入口
- **sectors 区块**：机器可读的行业映射（`Tech → 科技`、`STAR Market → 科创板`、
  `Textiles → 纺织`…），双重用途——① 归一化适配器 sector 字段；
  ② 生成 CANONICAL SECTOR NAMES 注入 prompt
- **维护规则**：canonical 必须是可交易行业中文名；新增前对照
  `ticker_whitelist.json` 的 sector 取值，保持两侧一致

文本清洗：入库前 `_clean_text()` 处理控制字符与 HTML 实体（`html.unescape`，P3-1）。

### 6.5 Severity 与 TTL

- **severity 链路**：适配器 `rule_based_severity()` 初评（关键词规则，零成本零延迟）
  → Tier 1 周期后 `severity_enricher.py` 对 Episodic 节点批量回填
  （Graphiti EpisodicNode 无原生 severity 属性）
- **分级 TTL 淘汰**（两层）：
  - Layer 1（查询层）：API 查询按 `window_days` 过滤（entity 端点默认 7 天，可配置）
  - Layer 2（存储层）：每日一次 `DETACH DELETE` 过期 Episodic 节点——
    MACRO 保留 14 天、SECTOR 7 天、SYMBOL 3 天（个股消息时效最短）

---

## 7. API 层（FastAPI，默认端口 8100）

### 7.1 端点总览

| 端点 | 参数 | 返回 |
|------|------|------|
| `GET /api/events/active` | limit(≤200)、min_severity、sector | 活跃事件扁平列表 + freshness |
| `GET /api/events/entity/{ticker}` | limit、min_severity、**include_graph**(默认 true)、**graph_depth**(1-3) | 个股事件 + 图结构 + 事件脉络 |
| `GET /api/events/sector/{sector_name}` | — | 行业事件 + statistics + sector_briefing |
| `GET /api/events/risk-summary` | — | LLM 宏观风险摘要（缓存 300s） |
| `GET /api/events/health` | — | 组件健康状态 |
| `POST /api/tickers/whitelist` | body: tickers[] | 接收 SynapseEngine 推送，写本地缓存 |

错误约定：ticker 未命中 → 404；Neo4j 不可用 → 503；内部错误 → 500。

### 7.2 图结构响应（P2-1）

`GET /api/events/entity/{ticker}` 在 `include_graph=true` 时额外返回：

- **graph** — 以 `start.ticker = $ticker` 命中起点，`RELATES_TO*1..graph_depth`
  遍历子图；nodes 含 id/type/ticker，edges 含 source/target/type（取
  `RELATES_TO.name`，如 BELONGS_TO/AFFECTS）与 fact
- **episodes** — 子图内实体参与的边通过 `ep.entity_edges` 反查 Episodic 节点，
  按时间窗组织的事件脉络时间线

向后兼容：既有 `ticker/events/summary` 字段不变；图查询失败降级 `graph=None`，
不影响事件主链路。severity 过滤与 limit 截断在 Cypher 内完成
（SEVERITY_WEIGHT 参数化），summary 统计基于过滤后集合。

sector 端点查询带标签过滤（`'Sector' IN labels(...)`），避免同名跨类型实体误命中。

### 7.3 sector_briefing 生成链路

`ingestion/briefing_aggregator.py`：

```
Tier 1 周期结束 → aggregate_all()
  → Query Neo4j（sector 维度事件聚合，含受影响 ticker 统计）
  → LLM 聚合（行业叙事：总事件数、涉及标的、按 severity 分组的事件列表）
  → 写入进程内存缓存
GET /api/events/sector/{name} → 直接读缓存（零 LLM 延迟）
```

降级：缓存未命中或读取失败 → `sector_briefing=None` + 原始事件列表照常返回，
消费方（SynapseEngine/MiroFish）可基于 events 自行聚合，不阻塞 API。

---

## 8. 与 SynapseEngine 的集成

SynapseEngine（组合决策系统）是 NewsEngine 当前的主要下游消费者。

### 8.1 白名单同步（Push + Pull 双通道）

- **Push（主）**：SynapseEngine `POST /api/tickers/whitelist` 推送持仓/自选清单，
  NewsEngine 持久化到 `data/ticker_whitelist.json`
- **Pull（兜底）**：`sync/ticker_sync.py` 定期（默认 6h）从 SynapseEngine
  `GET /api/portfolio/tickers` 拉取；SynapseEngine 不可达 → 使用本地缓存文件
- 白名单条目：`ticker`（`0700.HK` / `000858.SZ` 格式）、`name`（中文）、
  `sector`（中文 canonical，申万风格）、`exchange`、`biz_code`
- 加载时对同名多条目显式告警（后写覆盖风险）；白名单同时供给
  个股族适配器（抓取范围）与抽取 prompt（CANONICAL ENTITY NAMES grounding）

### 8.2 接口契约要点

- **ticker 格式**：`代码.交易所`（`000858.SZ`、`0700.HK`、`600519.SH`），
  不是 `SZ.000858`；SynapseEngine 侧经 `ticker_utils.symbol_to_ticker()`
  统一转换（P0-3）
- **sector 语言**：两端契约统一为**中文行业名**（如 "互联网平台"、"半导体"）
- **entity 端点参数**：`limit` / `min_severity` 由服务端 Cypher 内消费（P0-3，
  此前被静默丢弃）；时间窗口 `entity_events_window_days`（默认 7 天，原硬编码 3 天）
- **消费现状**：SynapseEngine 目前消费 `summary/severity/entities` 字段；
  `graph/episodes` 图结构已提供但尚未被消费（演进方向见 `docs/GAP_ANALYSIS.md`）

---

## 9. 运维与运行模式

### 9.1 启动序列（FIFO 8 步）

`main.py` 单事件循环内按序启动，任一步失败即终止（fast-fail）：

1. 加载并校验 `.env`（Pydantic Settings）
2. 初始化结构化 JSON 日志
3. 连接 Neo4j（硬阻塞）
4. 创建 FastAPI app + 注册 whitelist 路由
5. 初始化 Graphiti SDK（LLM/Embedder 配置）
6. 创建 EpisodeWriter（macro + symbol 双实例）
7. 创建并启动 IngestionScheduler（4 个 Tier 循环任务）
8. 启动 uvicorn（programmatic Server API，与调度共享事件循环）

关闭按 LIFO 逆序：uvicorn → scheduler → writer → Graphiti → Neo4j。

### 9.2 CLI 运行模式

| 命令 | 用途 |
|------|------|
| `python main.py` | 常驻模式（抓取 + 写图 + API） |
| `--dry-run` | 验证模式：走完抓取/normalize，不写 Neo4j |
| `--source gdelt,rss,stock` | 指定数据源（stock = CLS→EastMoney→AkShare 复合链 + CNInfo + 研报） |
| `--fetch-only [--watch]` | 只抓取落 Landing Zone（可循环） |
| `--ingest-only` | 只消费 Landing Zone 写图 |
| `--replay [--since/--until]` / `--replay-all` | 从 landing JSONL 重放写图 |
| `--retry-dead` | dead 队列重新入队 |
| `--stats` | Landing Zone 状态统计 |
| `--rebuild-index` | 重建搜索索引 |
| `--fetch-content` | 抓取阶段启用全文抓取 |

### 9.3 配置要点（.env）

| 分组 | 关键项 | 默认 |
|------|--------|------|
| LLM/Embedding | `OPENAI_BASE_URL`（百炼兼容）、`LLM_MODEL`、`EMBEDDING_MODEL=text-embedding-v4` | — |
| Neo4j | `NEO4J_URI=bolt://localhost:7687` | — |
| API | `api_port` | 8100 |
| 调度 | `TIER1..4_INTERVAL` | 900 / 14400 / 43200 / 86400 秒 |
| 去重 | `DEDUP_TTL_SECONDS`（低频源单独 7d） | 86400 |
| TTL | `episode_ttl_macro/sector/symbol_days` | 14 / 7 / 3 天 |
| 查询窗口 | `entity_events_window_days` | 7 天 |
| 并发/熔断 | `EPISODE_SEMAPHORE`、`CIRCUIT_MAX_CONSECUTIVE_429`、`MIN_429_BACKOFF_SEC` | 20 / 3 / 37s |
| LLM 预处理 | `LLM_PREPROCESSOR_ENABLED/ENDPOINT/COMPRESS_THRESHOLD/COMPRESS_TARGET` | true / 本地端点 / 20000 / 5000 |
| SynapseEngine | `synapse_base_url`、白名单缓存路径 | http://localhost:8000 |

---

## 10. 测试策略

```
tests/
├── test_adapters/          # 适配器 normalize 契约（Mock 抓取）
├── test_graphiti/          # episode_writer 指令注入、边类型收敛
├── test_sync/              # 多 Tier 调度、白名单同步
├── test_api/               # 端点行为（Mock Neo4j）
├── test_episode_writer.py  # 写入管线单测（含 prompt leakage 回归）
├── test_sector_rules.py    # Sector 准入/语言统一/归一化回归
├── test_prompt_leakage.py  # 抽取指令通道回归（严禁 body 追加）
└── conftest.py             # 公共 fixture
```

- **单元测试**：Mock 外部依赖（HTTP/Neo4j/LLM），验证 normalize 契约与规则注入
- **回归重点**：prompt leakage、废弃关系类型不再出现、Sector 规则注入双管线、
  CLS 动态 scope、边类型收敛行为
- **集成验证**：`--dry-run` 全链路 + `scripts/audit_neo4j_data.py` 图数据审计
- 运行：`pytest`（覆盖率 `pytest --cov=src`）

---

## 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| V3.0 | 2026-09-07 | 依据 develop `407bc91` 全量重写：第一章改为核心业务场景（L1/L2/L3）；删除一次性迁移章节（Crucix 替换、N4 验收、MongoDB Schema、SynapseEngine LLD 附录）；同步双管线、4-Tier 调度、Landing Zone、P0/P1/P2 数据质量修复、图结构 API 等当前实现 |
| V2.x | 2026-06 | 变更史形式（架构变更/接口契约/内部架构/配置测试/briefing/部署/MongoDB/N4 实施），详见 git 历史 |
