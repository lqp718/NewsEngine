# NewsEngine 设计文档

**版本:** V2.3（GDELT 三 CSV 整合 + EventEntity 新增）  
**日期:** 2026-06-29  
**作者:** Chief Architect  
**依据文档:**
- `NewsEngine Proposal V1.0` (2026-06-08)
- `SynapseEngine LOW_LEVEL_DESIGN.md` V1.8
- `Graphiti SDK v0.29.2` 实际 Neo4j Schema（Episodic/Entity/RELATES_TO）
- 灵汐 + Architect 联合审查（宏观管线瘫痪分析，2026-06-14）

**状态:** Draft → 待审批  
**审批人:** 老公  
**适用项目:** `D:\MyWallet\NewsEngine`  
**关联文档:** `NEWSENGINE-IMPLEMENT-PLAN.md`（实施计划，位于 `D:\MyWallet\SynapseEngine\docs\` 软链接）

---

## 版本说明

### V2.3 变更摘要 (2026-06-29)

1. **GDELT 三 CSV 整合** (§1.4.2, §3.2, §3.6.2): GDELT 从单一 GKG CSV 改为 Events + Mentions + GKG 三 CSV 整合，提供结构化事件骨架、传播追踪和语义元数据三层信息。
2. **EventEntity 新增** (§2.8, §3.4.3, §3.2): 新增 EventEntity（MACRO 管线）和 SymbolEventEntity（SYMBOL 管线）作为第 6 个实体类型，支持结构化事件建模和跨管线关联。
3. **Codebook 翻译层** (§3.1, §3.2): 新增 `gdelt_codebook.py` 模块，负责 CAMEO 事件码、Actor 代码和 Theme 代码的人类可读翻译，将 GDELT 原始代码转换为中文文本。
4. **双层过滤策略** (§3.6.2): GDELT 过滤从单一 GKG Themes 白名单扩展为 GKG Themes + Events CAMEO 码双重过滤，提升金融相关事件命中率。
5. **数据流更新** (§1.4.2): Events CSV → parse_events() → EventRecord，Mentions CSV → parse_mentions() → MentionRecord，GKG CSV → parse_gkg() → GKGRecord，三源合并后通过 Graphiti add_episode() 写入 Neo4j。
6. **Episode body 结构化** (§3.2): `_build_episode_body()` 重构为包含事件骨架（Actor1→Actor2→CAMEO）、传播覆盖（Mentions count）、情感评分（Goldstein/Tone）的人类可读格式。
7. **跨管线 Entity Type 关联** (§3.4.3): EventEntity 同时存在于 MACRO 和 SYMBOL 管线（字段集合不同），通过 Graphiti 实体消歧机制自动建立跨管线关联。

### V2.2 变更摘要 (2026-06-14)

1. **宏观/个股管线拆分** (§1.4 新增, §2.2, §3.2, §3.6 新增): 发现 V2.1 实现中 GDELT 和 RSS 适配器误用 `ticker_whitelist` 过滤，导致 824 条宏观新闻全部丢弃。将数据接入层拆分为宏观管线（GDELT/RSS）和个股管线（AkShare），各管线使用独立的过滤策略。
2. **GDELT 宏观主题白名单** (§3.6.2 新增): 定义 19 个核心金融主题作为 GDELT 的 OR 匹配过滤器，替代原来的 ticker 关键词匹配。不包含地理标签（避免 CHINA/USA 等标签引入体育/娱乐/社会新闻噪音）。
3. **RSS 零过滤** (§3.6.3): RSS 每天仅 10-30 条，源头（MarketWatch + FT）已是精选财经内容，无需 pre-ingestion 过滤。
4. **content_scope 写入时标记** (§2.9 新增, §3.6.5): 在 Episode 写入 Neo4j 时标记 `content_scope`（MACRO/SECTOR/SYMBOL），利用 Graphiti `EpisodicNode.episode_metadata` 字段透传。替代原来 SynapseEngine 消费端推断的模式。
5. **TTL 分级淘汰** (§6.6 新增): SYMBOL 3 天 / SECTOR 7 天 / MACRO 14 天，应用层 Cypher DETACH DELETE + 查询 WHERE 双重保障，每天执行 1 次。
6. **19 个核心宏观主题定义** (§3.6.2): 货币政策(3) + 宏观指标(3) + 贸易制裁(3) + 地缘政治(2) + 监管(2) + 市场(2) + 科技(1) + 能源(1) + 债务风险(1) + 汇率(1)。
7. **ticker whitelist 职责限定** (§2.2): 明确 `POST /api/tickers/whitelist` 仅服务于 AkShare 个股管线，不再传递给 GDELT/RSS 适配器。

### V2.1 变更摘要 (2026-06-14)

1. **Neo4j Schema 对齐**: Graphiti SDK 硬编码产生 `Episodic`/`Entity`/`RELATES_TO` label，与 V2.0 假设的 `Event`/`Stock`/`Sector`/`AFFECTS`/`BELONGS_TO` 不一致。本文档新增 §2.8「物理存储模型 vs 逻辑业务模型」对两套 schema 做完整映射。
2. **sector_briefing Cypher 更新** (§5.2): 从假设的 `MATCH (s:Sector)<-[:BELONGS_TO]-(stock:Stock)<-[:AFFECTS]-(event:Event)` 更新为实际 Graphiti schema `MATCH (sector:Entity)...(stock:Entity)...(ep:Episodic)`。
3. **新增共享翻译层** (§3.1, §3.2, §3.3): `src/graphiti/translation.py` 将 Episodic/Entity → EventItem 的内存翻译逻辑提取为公共函数，API 路由层和简报聚合层复用同一翻译入口，避免 DRY 漂移。
4. **端点→Neo4j 映射表修正** (§8.2.3): 反映实际查询的 `Episodic`/`Entity`/`RELATES_TO` label，不再引用不存在的 `Event`/`Stock`/`Sector`/`AFFECTS`/`BELONGS_TO`。
5. **MongoDB 同步链路澄清**: SynapseEngine `news_events` Collection 通过 NewsEngine REST API 填充（`_episode_to_event_item()` 在 API 响应层完成 Episodic→EventItem 翻译），不直接依赖 Neo4j label，无需额外同步层。

### V2.0 合并说明

V2.0 合并了 NewsEngine 的两份设计文档：

- **NEWSENGINE-REDESIGN-DOC.md V1.2**（原 SynapseEngine 视角的架构变更说明）→ 纳入 Part 1/2/6/7 及附录
- **NEWSENGINE-INTERNAL-SPEC.md V1.1**（原 NewsEngine 提供者视角的内部架构规格）→ 纳入 Part 3/4/5/8

合并后 V2.0 是本项目的**单一真相源（Single Source of Truth）**。原始文件 `NEWSENGINE-REDESIGN-DOC.md` 和 `NEWSENGINE-INTERNAL-SPEC.md` 保留不动。

**文档导航:**

| Part | 内容 | 来源 | 实施方 |
|------|------|------|--------|
| Part 1 | 架构变更动机 | Redesign Doc §A | — (背景说明) |
| Part 2 | 接口契约（完整 JSON Schema） | Redesign Doc §C | NewsEngine 实现 |
| Part 3 | 内部架构（文件/依赖/生命周期） | Internal Spec §2~§4 | NewsEngine 实现 |
| Part 4 | 配置与测试 | Internal Spec §5~§6 | NewsEngine 实现 |
| Part 5 | sector_briefing 生成链路 | Internal Spec §7 | NewsEngine 实现 |
| Part 6 | 部署要求 | Redesign Doc §F | NewsEngine + SynapseEngine |
| Part 7 | MongoDB Schema 变更 | Redesign Doc §D | SynapseEngine 实施 |
| Part 8 | N4 实施与验收（补完清单 + API 指南 + main.py + N4-L 收尾） | Internal Spec §8~§10 + V2.1 新增 | NewsEngine 实现 |
| Part 9 | 闭环检查 | 两份 spec 的闭环检查合并去重 | — |
| 附录 A | SynapseEngine LLD 替换清单 | Redesign Doc §B | **不在 NewsEngine 实施** |
| 附录 B | SynapseEngine IMPLEMENT_PLAN 变更影响 | Redesign Doc §E | **不在 NewsEngine 实施** |

---

# Part 1: 架构变更说明

（来源: Redesign Doc §A，原文保留）

## 1.1 为什么去掉 Crucix

| 痛点 | 详情 |
|------|------|
| **GDELT HTTPS API 不可靠** | `api.gdeltproject.org` 被 GFW 拦截/SSL 层阻断，直连超时。代理虽可建立 CONNECT tunnel 但 TLS 握手 `SSL_ERROR_SYSCALL` 被拒（P2-3 验证结论，2026-06-04） |
| **Crucix adapter 漏 RSS 产出** | P3-C2 验收发现 Crucix 的 29 源聚合只输出了 GDELT GKG 解析结果，RSS feed / Telegram 情报帖未正确映射到 MongoDB |
| **原始新闻是"数据湖"而非"情报"** | Crucix 仅做聚合+关键词匹配，无实体提取、无事件去重、无因果关系建模 |
| **GDELT 数据文件通路可用** | `data.gdeltproject.org` (HTTP) 可正常下载 GKG/Events CSV，是可靠替代方案 |

## 1.2 NewsEngine 新架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                         NewsEngine                                    │
│                   D:\MyWallet\NewsEngine                              │
│                                                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐           │
│  │GDELT CSV │  │RSS 直连  │  │ AkShare  │  │Treasury   │           │
│  │每 15 分钟 │  │(原Crucix │  │个股新闻   │  │API        │           │
│  │GKG+Events│  │ 暴露端口)│  │          │  │(Phase 2+) │           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘            │
│       │             │             │             │                    │
│       ▼             ▼             ▼             ▼                    │
│  ┌──────────────────────────────────────────────────────────┐       │
│  │              适配器层 (Adapter Layer)                      │       │
│  │  GDELTAdapter / RSSAdapter / AkShareAdapter / Treasury    │       │
│  │  职责: 原始数据 → Graphiti Episode（统一格式）              │       │
│  └────────────────────────┬─────────────────────────────────┘       │
│                           ▼                                          │
│  ┌──────────────────────────────────────────────────────────┐       │
│  │               Graphiti 引擎（核心）                         │       │
│  │  • 实体提取: Stock / Sector / Country / Policy / Event     │       │
│  │  • 关系提取: AFFECTS / CAUSED_BY / MITIGATES / RELATED_TO │       │
│  │  • 时间窗口: valid_at / invalid_at（事实生命周期）          │       │
│  │  • 增量写入: 新 Episode 自动关联已有实体                    │       │
│  │  • 混合检索: 语义 + BM25 + 图遍历                         │       │
│  │  • 事件溯源: 每个事实可追溯到原始 Episode                  │       │
│  │  后端: Neo4j (Docker, WSL2)                                │       │
│  │  Embedding: 阿里百炼 text-embedding-v3                     │       │
│  │  LLM 提取: Qwen3.6-plus (百炼)                             │       │
│  │  V2.3: Event 实体新增，支持结构化事件建模                  │       │
│  └────────────────────────┬─────────────────────────────────┘       │
│                           ▼                                          │
│  ┌──────────────────────────────────────────────────────────┐       │
│  │               REST API 层（输出）                           │       │
│  │  GET /api/events/active                                   │       │
│  │  GET /api/events/entity/:ticker                           │       │
│  │  GET /api/events/sector/:name                             │       │
│  │  GET /api/events/risk-summary                             │       │
│  │  GET /api/events/health                                   │       │
│  │  框架: FastAPI, 端口: 8100                                 │       │
│  └──────────────────────────────────────────────────────────┘       │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

## 1.3 NewsEngine 与 SynapseEngine 的关系

```
┌──────────────────────────┐         ┌──────────────────────────┐
│      SynapseEngine        │         │       NewsEngine          │
│   D:\MyWallet\SynapseEngine│  REST   │  D:\MyWallet\NewsEngine  │
│                           │         │                           │
│  ┌─────────────────────┐ │  GET    │ ┌───────────────────────┐ │
│  │ news_engine_client  │ │────────►│ │ FastAPI REST API      │ │
│  │ (REST 调用层)        │◄─────────│ │ :8100                 │ │
│  └─────────────────────┘ │         │ └───────────────────────┘ │
│                           │         │                           │
│  ┌──────────────────────┐│  POST   │ ┌───────────────────────┐ │
│  │ push_ticker_whitelist││────────►│ │ POST /api/tickers/    │ │
│  │ (启动+变化时触发)    ││         │ │ whitelist (接收+缓存)  │ │
│  └──────────────────────┘│         │ └───────────────────────┘ │
│                           │         │                           │
│  消费者:                  │         │  数据源:                  │
│  • MiroFish (消费events)  │         │  • GDELT CSV (HTTP)      │
│  • PM Agent (消费events)  │         │  • RSS 直连 (原Crucix)   │
│  • Kronos (消费events)    │         │  • AkShare 个股新闻       │
│                           │         │  • Treasury API           │
└──────────────────────────┘         └──────────────────────────┘

两个项目**物理独立**（不同目录、不同进程、不同数据库），通过 HTTP REST API 通信。
NewsEngine 是 SynapseEngine 的**情报子系统**，提供结构化事件情报。
NewsEngine 可独立启动（不依赖 SynapseEngine 在线），ticker 白名单由 SynapseEngine 启动后 + 变化时主动 Push。
```

**关键特性对比:**

| 维度 | 旧方案 (Crucix) | 新方案 (NewsEngine) |
|------|----------------|---------------------|
| 定位 | OSINT 聚合器（数据湖） | 情报引擎（结构化事件） |
| 数据源接入 | 仅 Crucix API (localhost:3117) | GDELT CSV 直连 + RSS + AkShare (含个股新闻) + Treasury (Phase 2+) |
| 实体/关系 | ❌ 无（仅关键词匹配） | ✅ Graphiti 时序知识图 |
| 去重/聚类 | ❌ 无 | ✅ Graphiti 语义去重 + 实体消歧 |
| 因果关系 | ❌ 无 | ✅ CAUSED_BY / MITIGATES 关系链 |
| 时间模型 | ❌ 无（仅创建时间戳） | ✅ valid_at / invalid_at 双时间模型 |
| 输出 | 原始 JSON（需 Crucix adapter 转写） | 结构化 REST API（可直接消费） |
| SynapseEngine 耦合 | 强耦合（Crucix 直连、专用 adapter） | 松耦合（REST API，可独立演进） |

> **Treasury API 说明:** Proposal §3.4 定义的 Treasury API（美国国债收益率/利率决策）作为结构化数据源。Phase 1 (MVP) 暂不接入，推迟到 Phase 2+ 实施。理由: Phase 1 聚焦 GDELT CSV + RSS + AkShare 三条核心管线跑通，Treasury 为日级低频源，不影响主流程。

## 1.4 宏观/个股双管线架构（V2.2 新增）

### 1.4.1 问题发现

V2.1 实现中 GDELT 和 RSS 适配器复用了 AkShare 的 `ticker_whitelist` 过滤逻辑：

```
GDELT: Parsed 824 records → Filtered 824 → 0 records by ticker whitelist
RSS:   Parsed 10 entries  → Filtered 10  → 0 entries by ticker whitelist
AkShare: Fetched 20 news items → wrote 1 episode
```

**根因：** 宏观新闻（"美联储加息"、"中美贸易战"、"芯片出口管制"）不包含个股 ticker 代码或公司名称，ticker 关键词匹配（`biz_code` / `name_zh` / `name_en`）对宏观事件命中率为零。三条管线使用了相同的过滤策略，但只有 AkShare 应该按 ticker 过滤。

### 1.4.2 双管线架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                      NewsEngine 数据接入层                            │
│                                                                      │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐ │
│  │     宏观管线 (MACRO)          │  │     个股管线 (SYMBOL)         │ │
│  │                              │  │                              │ │
│  │  GDELT CSV ──► CAMEO码+Theme │  │  AkShare ──► ticker 过滤 ──┤ │
│  │  (Events+Mentions+GKG)       │  │  (按白名单查询) (biz_code)    │ │
│  │  双层过滤+三源合并            │  │                              │ │
│  │                              │  │                              │ │
│  │  RSS ────────► 零过滤 ──────┤  │                              │ │
│  │  (10条/周期)  (源头干净)      │  │                              │ │
│  │                              │  │                              │ │
│  │  V2.3: GDELT 三 CSV 整合     │  │                              │ │
│  └──────────────┬───────────────┘  └──────────────┬───────────────┘ │
│                 │                                  │                 │
│                 ▼                                  ▼                 │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                  Graphiti 引擎（统一写入）                      │   │
│  │  content_scope: MACRO / SECTOR / SYMBOL (episode_metadata)   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**过滤策略矩阵：**

| 管线 | 数据源 | 过滤维度 | 过滤来源 | 维护方 |
|------|--------|---------|---------|--------|
| **宏观** | GDELT | 19 核心宏观主题 OR 匹配 | 静态配置 `macro_themes.py` | NewsEngine（开发期静态） |
| **宏观** | RSS | 零过滤（源头精选） | — | — |
| **个股** | AkShare | ticker 白名单 | `POST /api/tickers/whitelist`（SynapseEngine push） | SynapseEngine（运行时动态） |

### 1.4.3 设计决策

**为什么不加地理标签？** GDELT GKG 的 locations 列包含国家代码（如 `CN`、`US`），若加入地理过滤，"中国女排 3:0 战胜巴西"（体育新闻，theme 不含金融标签）会因 `CN` 命中而漏入。内容主题天然排除非财经噪音：一条体育/娱乐新闻不会同时打上 `MONETARY_POLICY` 或 `REGULATION` 标签。

**为什么 RSS 零过滤？** MarketWatch Top Stories + FT 两个 RSS 源每天产出仅 10-30 条，且源头已是财经精选。量级不足以构成存储压力，加过滤反而可能丢失关键宏观评论。

**为什么不在 Neo4j 创建双 label？** Graphiti SDK 的 `Episodic`/`Entity`/`RELATES_TO` 是物理真实，不可更改。`content_scope` 作为 `episode_metadata` 自定义属性存储，消费时通过 Cypher WHERE 过滤。



---

# Part 2: 接口契约

（来源: Redesign Doc §C，原文完整保留 — 含全部 JSON Schema、字段定义表、错误码表、代码示例）

## 2.1 接口总览

```
┌──────────────────────────────────────────────────────────────────┐
│                    SynapseEngine  ←→  NewsEngine                  │
│                                                                   │
│  SynapseEngine 提供:                                              │
│  ┌──────────────────────────────────────────────────┐            │
│  │ GET /api/portfolio/tickers                       │            │
│  │ (运维查询)                                        │            │
│  │ POST /api/tickers/whitelist                      │            │
│  │ (推送白名单, 启动+变化时)                          │            │
│  └──────────────────────────────────────────────────┘            │
│                                                                   │
│  NewsEngine 提供:                                                 │
│  ┌──────────────────────────────────────────────────┐            │
│  │ GET /api/events/active         — 当前活跃事件     │            │
│  │ GET /api/events/entity/:ticker  — 某股票事件      │            │
│  │ GET /api/events/sector/:name    — 行业事件聚合     │            │
│  │ GET /api/events/risk-summary    — 风险摘要         │            │
│  │ GET /api/events/health          — 数据源健康       │            │
│  └──────────────────────────────────────────────────┘            │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2.2 SynapseEngine → NewsEngine: POST /api/tickers/whitelist

SynapseEngine 启动时 + ticker 变化时（持仓变动/watchlist 增删）调用此端点，push ticker 白名单到 NewsEngine。

> **V2.2 职责限定:** 此白名单**仅用于 AkShare 个股管线**。GDELT 和 RSS 宏观管线使用独立的 theme 过滤策略（见 §1.4 和 §3.6.2），不消费 ticker 白名单。

**请求:**
```
POST /api/tickers/whitelist HTTP/1.1
Host: localhost:8100
Content-Type: application/json

{
  "tickers": [
    {
      "symbol": "HK.00700",
      "biz_code": "0700",
      "name": "腾讯控股",
      "sector": "互联网平台",
      "source": "holding"
    },
    {
      "symbol": "HK.09988",
      "biz_code": "9988",
      "name": "阿里巴巴-W",
      "sector": "互联网平台",
      "source": "holding"
    },
    {
      "symbol": "HK.01211",
      "biz_code": "1211",
      "name": "比亚迪股份",
      "sector": "新能源汽车",
      "source": "watchlist"
    }
  ],
  "total": 25,
  "updated_at": "2026-06-08T08:00:00+08:00"
}
```

**请求体字段定义:**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tickers` | array | 是 | ticker 列表，包含持仓(5) + 自选股(20) |
| `tickers[].symbol` | string | 是 | SynapseEngine 内部格式 `HK.XXXXX` |
| `tickers[].biz_code` | string | 是 | 纯数字代码（供 AkShare 个股管线使用） |
| `tickers[].name` | string | 是 | 中文名称 |
| `tickers[].sector` | string | 是 | 行业分类 |
| `tickers[].source` | string | 是 | 枚举: `holding` (持仓) / `watchlist` (自选) |
| `total` | int | 是 | ticker 总数 |
| `updated_at` | string | 是 | 数据更新时间 (ISO 8601 +08:00) |

**成功响应 (200):**
```json
{
  "status": "ok",
  "received": 25,
  "cached_at": "2026-06-08T08:00:01+08:00"
}
```

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `400` | `{"error": "Invalid request body", "detail": "..."}` | 请求体格式错误 |
| `500` | `{"error": "Internal server error", "detail": "..."}` | 写入缓存失败 |

**NewsEngine 端处理逻辑:**
```python
# NewsEngine 侧: 接收 ticker 白名单 POST → 写入本地缓存
# 文件: src/api/routers/whitelist.py

from fastapi import APIRouter, Request
from pathlib import Path
import json

router = APIRouter()
_whitelist_cache: list[dict] = []
_cache_file = Path("data/ticker_whitelist.json")


@router.post("/api/tickers/whitelist")
async def receive_ticker_whitelist(request: Request):
    """接收 SynapseEngine 推送的 ticker 白名单。"""
    try:
        body = await request.json()
    except Exception:
        return {"error": "Invalid JSON"}, 400

    tickers = body.get("tickers", [])
    if not isinstance(tickers, list) or len(tickers) == 0:
        return {"error": "tickers must be a non-empty array"}, 400

    # 更新内存缓存
    global _whitelist_cache
    _whitelist_cache = tickers

    # 持久化到本地文件（兜底）
    _cache_file.parent.mkdir(parents=True, exist_ok=True)
    _cache_file.write_text(json.dumps(body, ensure_ascii=False, indent=2))

    return {
        "status": "ok",
        "received": len(tickers),
        "cached_at": datetime.utcnow().isoformat() + "Z",
    }


def get_ticker_whitelist() -> list[dict]:
    """获取当前白名单（供 AkShare Adapter 使用）。
    
    V2.2: ticker whitelist 仅用于个股管线（AkShare）。
    GDELT/RSS 宏观管线使用 macro_themes 白名单（见 §3.6.2）。
    优先使用内存缓存，降级到本地文件。
    """
    global _whitelist_cache
    if _whitelist_cache:
        return _whitelist_cache
    if _cache_file.exists():
        data = json.loads(_cache_file.read_text())
        _whitelist_cache = data.get("tickers", [])
        return _whitelist_cache
    return []
```

**数据来源（SynapseEngine 侧）:**
- 持仓 ticker: `portfolio_state_history` 最新 BOD 快照 → `slots[].symbol`
- 自选股 ticker: `watchlist_history` 最新快照 → `candidates[].symbol`

**Push 时序:**
```
1. NewsEngine 启动 → 监听 POST /api/tickers/whitelist
2. SynapseEngine 启动 → push ticker 白名单到 NewsEngine
3. ticker 变化时（watchlist 增删/持仓变动）→ SynapseEngine 再次 push
4. NewsEngine 本地缓存文件兜底（data/ticker_whitelist.json）
```

---

## 2.3 NewsEngine → SynapseEngine: GET /api/events/active

**请求:**
```
GET /api/events/active?limit=50&min_severity=medium HTTP/1.1
Host: localhost:8100
```

**查询参数:**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 50 | 最大返回条数 |
| `min_severity` | string | `medium` | 最低严重级别: `low` / `medium` / `high` / `critical` |
| `sector` | string | — | 可选: 按行业过滤 |

**成功响应 (200):**
```json
{
  "events": [
    {
      "event_id": "evt-20260608-001",
      "title": "腾讯股价午后跳水，市场担忧监管收紧",
      "summary": "受传闻影响，腾讯控股午后跌幅扩大至3.2%，成交额较前日放大180%。",
      "severity": "high",
      "first_seen": "2026-06-08T13:15:00+08:00",
      "last_updated": "2026-06-08T14:30:00+08:00",
      "source_count": 12,
      "source_urls": [
        "https://finance.sina.com.cn/...",
        "https://news.qq.com/..."
      ],
      "keywords": ["腾讯", "监管", "股价跳水"],
      "entities": [
        {"type": "stock", "ticker": "0700.HK", "name": "腾讯控股"},
        {"type": "policy", "name": "反垄断调查", "status": "rumor"},
        {"type": "event", "name": "监管收紧传闻", "goldstein_scale": -3.2, "tone": -4.87, "event_date": "2026-06-08"}
      ],
      "relations": [
        {"type": "CAUSED_BY", "target_event_id": "evt-20260607-003"}
      ]
    }
  ],
  "total": 23,
  "freshness": {
    "gdelt_last_update": "2026-06-08T14:15:00+08:00",
    "rss_last_update": "2026-06-08T14:20:00+08:00",
    "akshare_last_update": "2026-06-08T14:25:00+08:00"
  }
}
```

**字段定义:**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `events` | array | 是 | 事件列表，按 severity 降序 + last_updated 降序 |
| `events[].event_id` | string | 是 | 唯一事件 ID，格式 `evt-YYYYMMDD-NNN` |
| `events[].title` | string | 是 | 事件标题 |
| `events[].summary` | string | 否 | 事件摘要 (LLM 生成或原文首段) |
| `events[].severity` | string | 是 | 枚举: `low` / `medium` / `high` / `critical` |
| `events[].first_seen` | string | 是 | 首次发现时间 (ISO 8601 +08:00) |
| `events[].last_updated` | string | 是 | 最后更新时间 |
| `events[].source_count` | int | 是 | 信息来源数 |
| `events[].source_urls` | array | 否 | 来源链接列表 |
| `events[].keywords` | array | 是 | 关键词 |
| `events[].entities` | array | 是 | 关联实体 |
| `events[].entities[].type` | string | 是 | 实体类型: `stock` / `sector` / `country` / `policy` / `event` (V2.3) |
| `events[].entities[].ticker` | string | 否 | ticker (仅 stock 类型) |
| `events[].entities[].name` | string | 是 | 实体名称 |
| `events[].entities[].status` | string | 否 | 状态 (仅 policy 类型): `rumor` / `confirmed` / `resolved` |
| `events[].relations` | array | 否 | 事件间关系 |
| `events[].relations[].type` | string | 是 | 关系类型: `CAUSED_BY` / `MITIGATES` / `RELATED_TO` |
| `events[].relations[].target_event_id` | string | 是 | 目标事件 ID |
| `total` | int | 是 | 符合条件的事件总数 |
| `freshness` | object | 是 | 数据源新鲜度 |
| `freshness.gdelt_last_update` | string | 是 | GDELT 最后更新 |
| `freshness.rss_last_update` | string | 否 | RSS 最后更新 |
| `freshness.akshare_last_update` | string | 否 | AkShare 最后更新 |

**排序:** severity 降序（critical=4 > high=3 > medium=2 > low=1）+ last_updated 降序

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `503` | `{"error": "Neo4j unavailable", "detail": "..."}` | 图数据库不可用 |
| `500` | `{"error": "Internal error", "detail": "..."}` | 其他错误 |

---

## 2.4 NewsEngine → SynapseEngine: GET /api/events/entity/:ticker

**请求:**
```
GET /api/events/entity/0700.HK HTTP/1.1
Host: localhost:8100
```

> **ticker 格式:** NewsEngine 使用 `XXXX.HK` 格式（如 `0700.HK`），SynapseEngine 调用时需将内部 `HK.00700` 格式转换。

**成功响应 (200):**
```json
{
  "ticker": "0700.HK",
  "events": [
    {
      "event_id": "evt-20260608-001",
      "title": "腾讯午后跳水3.2%",
      "severity": "high",
      "first_seen": "2026-06-08T13:15:00+08:00",
      "last_updated": "2026-06-08T14:30:00+08:00",
      "source_count": 12,
      "keywords": ["腾讯", "跳水", "监管"],
      "entities": [
        {"type": "stock", "ticker": "0700.HK", "name": "腾讯控股"},
        {"type": "policy", "name": "反垄断调查", "status": "rumor"}
      ]
    }
  ],
  "summary": {
    "total_events": 3,
    "avg_severity": "high",
    "risk_level": "HIGH",
    "news_sentiment_score": 0.15
  }
}
```

**字段定义:**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `ticker` | string | 是 | 股票代码 |
| `events` | array | 是 | 关联事件列表（格式同 `/events/active`） |
| `summary.total_events` | int | 是 | 事件总数 |
| `summary.avg_severity` | string | 是 | 平均严重级别 |
| `summary.risk_level` | string | 是 | 风险等级 |
| `summary.news_sentiment_score` | number | 是 | 综合情绪分 (0~1, 低=负面) |

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `404` | `{"error": "Ticker not found", "detail": "No events for 0700.HK"}` | 无该 ticker 事件 |
| `503` | `{"error": "Neo4j unavailable"}` | 数据库不可用 |

---

## 2.5 NewsEngine → SynapseEngine: GET /api/events/sector/:name

**请求:**
```
GET /api/events/sector/互联网平台 HTTP/1.1
Host: localhost:8100
```

> **sector 名称使用中文**（与 SynapseEngine 内部 `sector` 字段一致）。

**成功响应 (200):**
```json
{
  "sector": "互联网平台",
  "events": [
    {
      "event_id": "evt-20260608-001",
      "title": "互联网平台监管收紧传闻",
      "severity": "high",
      "first_seen": "2026-06-08T10:00:00+08:00",
      "entities": [
        {"type": "stock", "ticker": "0700.HK", "name": "腾讯控股"},
        {"type": "stock", "ticker": "9988.HK", "name": "阿里巴巴-W"},
        {"type": "event", "name": "监管收紧传闻", "goldstein_scale": -3.2, "tone": -4.87}
      ]
    }
  ],
  "statistics": {
    "total_events": 5,
    "affected_tickers": 8,
    "dominant_severity": "high"
  },
  "sector_briefing": "## 互联网平台行业情报汇总 (2026-06-08)\n\n### 核心摘要\n当前互联网平台行业面临监管收紧传闻压力...\n\n### 高风险事件\n1. **互联网平台监管收紧传闻** (high)\n   影响标的: 0700.HK, 9988.HK...\n\n### 行业展望\n短期内需关注监管政策落地节奏..."
}
```

**字段定义:**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sector` | string | 是 | 行业名称 |
| `events` | array | 是 | 该行业的事件列表（格式同 `/events/active`） |
| `statistics.total_events` | int | 是 | 该行业事件总数 |
| `statistics.affected_tickers` | int | 是 | 受影响的股票数 |
| `statistics.dominant_severity` | string | 是 | 主要严重级别 |
| `sector_briefing` | string \| null | 否 | LLM 聚合的行业情报简报（Markdown 格式，300-500 字）。由 `SectorBriefingAggregator` 每 15 分钟异步预计算 + 内存缓存。为 null 时消费者应降级为自行聚合原始事件列表。完整生成链路见 Part 5。 |

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `404` | `{"error": "Sector not found"}` | 无该行业事件 |
| `503` | `{"error": "Neo4j unavailable"}` | 数据源不可用 |

---

## 2.6 NewsEngine → SynapseEngine: GET /api/events/risk-summary

**请求:**
```
GET /api/events/risk-summary HTTP/1.1
Host: localhost:8100
```

**成功响应 (200):**
```json
{
  "overall_risk": "HIGH",
  "risk_score": 0.75,
  "top_risks": [
    {
      "event_id": "evt-20260608-001",
      "title": "互联网平台监管传闻",
      "severity": "high",
      "affected_sectors": ["互联网平台"],
      "potential_impact": "可能在早盘引发港股科技板块集体低开"
    },
    {
      "event_id": "evt-20260608-005",
      "title": "美联储鹰派发言",
      "severity": "high",
      "affected_sectors": ["金融", "地产"],
      "potential_impact": "加息预期升温，资金可能流出港股"
    }
  ],
  "sector_risk_levels": {
    "互联网平台": "HIGH",
    "新能源汽车": "MEDIUM",
    "消费": "LOW"
  },
  "summary": "当前宏观风险偏高 (risk_score=0.75)。互联网平台与金融板块面临双重压力，建议 PM Agent 提高防御仓位比例。",
  "generated_at": "2026-06-08T08:05:00+08:00"
}
```

**字段定义:**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `overall_risk` | string | 是 | 综合风险等级: `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `risk_score` | number | 是 | 风险评分 0~1（越高越危险） |
| `top_risks` | array | 是 | Top-5 高风险事件 |
| `top_risks[].event_id` | string | 是 | 事件 ID |
| `top_risks[].title` | string | 是 | 事件标题 |
| `top_risks[].severity` | string | 是 | 严重级别 |
| `top_risks[].affected_sectors` | array | 是 | 受影响行业列表 |
| `top_risks[].potential_impact` | string | 是 | LLM 生成的潜在影响分析 |
| `sector_risk_levels` | object | 是 | 各行业风险等级映射 `{sector_name: risk_level}` |
| `summary` | string | 是 | LLM 生成的风险摘要文本 |
| `generated_at` | string | 是 | 摘要生成时间 (ISO 8601 +08:00)，反映缓存新鲜度 |

**缓存策略:** 结果缓存 5 分钟（`RISK_SUMMARY_CACHE_TTL_SEC=300`），降低 Neo4j 查询压力 + LLM 调用成本。

---

## 2.7 NewsEngine → SynapseEngine: GET /api/events/health

**请求:**
```
GET /api/events/health HTTP/1.1
Host: localhost:8100
```

**成功响应 (200):**
```json
{
  "status": "healthy",
  "uptime_seconds": 86400,
  "data_sources": {
    "gdelt_csv": {
      "status": "ok",
      "last_update": "2026-06-08T14:15:00+08:00",
      "latency_minutes": 5
    },
    "rss": {
      "status": "ok",
      "last_update": "2026-06-08T14:20:00+08:00",
      "latency_minutes": 0
    },
    "akshare": {
      "status": "ok",
      "last_update": "2026-06-08T14:25:00+08:00",
      "latency_minutes": 5
    },
    "treasury": {
      "status": "degraded",
      "last_update": "2026-06-08T08:00:00+08:00",
      "latency_minutes": 380,
      "error": "API rate limited"
    }
  },
  "neo4j": {
    "status": "ok",
    "node_count": 15420,
    "relation_count": 38500
  },
  "graphiti": {
    "status": "ok",
    "episode_count_today": 234
  }
}
```

**字段定义:**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `status` | string | 是 | 综合状态: `healthy` / `degraded` / `down` |
| `uptime_seconds` | int | 是 | 服务运行时间（秒） |
| `data_sources.*.status` | string | 是 | 各数据源状态: `ok` / `degraded` / `down` |
| `data_sources.*.last_update` | string | 是 | 最后数据更新时间 |
| `data_sources.*.latency_minutes` | int | 是 | 距上次更新的分钟数 |
| `data_sources.*.error` | string | 否 | 错误描述（仅 degraded/down 时） |
| `neo4j.status` | string | 是 | Neo4j 连接状态: `ok` / `down` |
| `neo4j.node_count` | int | 是 | 知识图谱节点数 |
| `neo4j.relation_count` | int | 是 | 关系边数量 |
| `graphiti.status` | string | 是 | Graphiti 状态 |
| `graphiti.episode_count_today` | int | 是 | 今日处理 Episode 数 |

**状态判定规则:** 最近数据源更新在 30 分钟内 = healthy；任一数据源超过 30 分钟 = degraded；Neo4j 不可达 = down。

---

## 2.8 物理存储模型 vs 逻辑业务模型

> **V2.1 新增（2026-06-14）。** Graphiti SDK v0.29.2 硬编码 Neo4j label，不可更改。V2.0 设计文档假设的 `Event`/`Stock`/`Sector`/`AFFECTS`/`BELONGS_TO` 是**逻辑业务模型**，不是物理存储 schema。

### 2.8.1 Schema 映射表

| 概念 | 物理存储 (Graphiti Neo4j) | 逻辑业务模型 (代码中使用) | 映射方式 |
|------|--------------------------|------------------------|---------|
| 新闻事件 | `Episodic` 节点 (label: `Episodic`) | `EventItem` (Pydantic model) | `translation.py: translate_episode_to_event()` |
| 股票 | `Entity` 节点 (label: `Entity:Stock`, 属性 `ticker`) | `EventEntityItem(type="stock")` | 同上 |
| 行业 | `Entity` 节点 (label: `Entity:Sector`, 属性 `name`) | `EventEntityItem(type="sector")` | 同上 |
| 国家 | `Entity` 节点 (label: `Entity:Country`) | `EventEntityItem(type="country")` | 同上 |
| 政策 | `Entity` 节点 (label: `Entity:Policy`, 属性 `status`) | `EventEntityItem(type="policy")` | 同上 |
| **事件实体 (V2.3)** | `Entity` 节点 (label: `Entity:Event`, 属性 `goldstein_scale/tone/cameo_code`) | `EventEntityItem(type="event")` | 同上 |
| 事件-实体关系 | `RELATES_TO` 边 (属性 `name`: `AFFECTS`/`BELONGS_TO`/`CAUSED_BY`/…) | `EventRelationItem` | 同上 |
| 社区/摘要 | `Community` / `Saga` 节点 | 无（当前未消费） | — |

### 2.8.2 核心设计决策

**不在 Neo4j 中创建双 label。** Graphiti SDK 的 `Episodic`/`Entity`/`RELATES_TO` 是物理真实。`Event`/`Stock`/`Sector`/`AFFECTS`/`BELONGS_TO` 是逻辑抽象，仅在应用代码中作为 Pydantic 模型存在。

**翻译层位置:** `src/graphiti/translation.py` 封装所有 Neo4j record → 业务模型的转换逻辑。API 路由层 (`events.py`) 和简报聚合层 (`briefing_aggregator.py`) 共用同一套翻译函数。

**数据流:**
```
Graphiti SDK 写入 → Neo4j (Episodic/Entity/RELATES_TO)
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   events.py             briefing_           SynapseEngine
   Cypher 查             aggregator.py       REST API 调用
   Episodic/Entity       Cypher 查           拿到 EventItem
        │                Episodic/Entity          JSON
        ▼                     │                    │
   translation.py         translation.py      MongoDB
   内存翻译 →              内存翻译 →          news_events
   EventItem JSON         聚合 dict              Collection
```

**每个消费者的 schema 依赖总结:**

| 消费者 | 读/写 | 访问路径 | 依赖 Graphiti schema? | 依赖逻辑模型? |
|--------|------|---------|---------------------|-------------|
| `api/routers/events.py` | 读 | 直连 Neo4j → 内存翻译 | ✅ | ✅ |
| `ingestion/briefing_aggregator.py` | 读 | 直连 Neo4j → 内存翻译 | ✅ | ✅ |
| SynapseEngine `news_events` | 写 | REST API → JSON 响应 → MongoDB | ❌ (透明) | ✅ |
| SynapseEngine 盘前消费 | 读 | MongoDB `news_events` 本地缓存 | ❌ (透明) | ✅ |

---

n

## 2.9 content_scope 定义与标记策略（V2.2 新增）

### 2.9.1 定义

`content_scope` 标记每条 Episode 的宏观/行业/个股归属，用于消费端按维度查询和 TTL 分级淘汰。

| 值 | 语义 | 典型来源 | 下游消费 |
|----|------|---------|---------|
| `MACRO` | 宏观事件（货币政策、地缘政治、贸易制裁等） | GDELT、RSS | PM Agent `macro_context`、risk-summary |
| `SECTOR` | 行业级事件（行业趋势、政策变化） | sector_briefing 聚合产出 | MiroFish 行业推演、`/api/events/sector/:name` |
| `SYMBOL` | 个股事件（财报、股价异动、公司新闻） | AkShare | Sentiment Analyst、`/api/events/entity/:ticker` |

### 2.9.2 标记时机与位置

**标记时机：写入时（NewsEngine 侧），不依赖消费端推断。**

- 适配器层（GDELT/RSS/AkShare）在 `normalize()` 阶段设置 `NormalizedEpisode.metadata["content_scope"]`
- `EpisodeWriter` 写入 Neo4j 时，Graphiti 自动将 `metadata` 透传到 `EpisodicNode.episode_metadata` 属性
- sector_briefing 聚合产出的事件标记为 `SECTOR`

**标记依据：**

| 适配器 | content_scope | 依据 |
|--------|--------------|------|
| `GdeltAdapter` | `MACRO` | GDELT GKG 是全球宏观事件数据源 |
| `RssAdapter` | `MACRO` | MarketWatch + FT 是宏观财经评论 |
| `AkShareAdapter` | `SYMBOL` | 东方财富个股新闻 API，按 ticker 查询 |
| `SectorBriefingAggregator` | `SECTOR` | 行业级聚合产出 |

**为什么不依赖消费端推断？** 源头即真相。GDELT adapter 知道自己在处理宏观数据，不需要消费者通过实体类型（country/policy/sector/stock）反向猜测 scope。写入时标记比读取时推断更准确、更高效。

### 2.9.3 episode_metadata 存储（Graphiti 透传）

```python
# 写入示例：GdeltAdapter.normalize()
episode = NormalizedEpisode(
    ...
    metadata={"content_scope": "MACRO"},
)
```

Graphiti SDK 的 `EpisodicNode` 模型支持 `episode_metadata` 字段，写入 Neo4j 时自动存储为节点属性。查询时：

```cypher
-- 查询所有宏观事件（14 天 TTL）
MATCH (ep:Episodic)
WHERE ep.episode_metadata CONTAINS 'MACRO'
  AND ep.created_at > datetime() - duration({days: 14})
RETURN ep
ORDER BY ep.created_at DESC
```

# Part 3: 内部架构

（来源: Internal Spec §2~§4，原文完整保留 — 含文件树、职责矩阵、依赖图、生命周期）

## 3.1 文件架构（完整目录树）

```
NewsEngine/
├── .env                          # 敏感配置（不提交 git）【已存在】
├── .env.example                  # 配置模板【需创建】
├── .gitignore
├── requirements.txt              # Python 依赖【已存在】
├── docker-compose.yml            # Neo4j 部署【已存在】
├── pyproject.toml                # 项目元数据【建议新增】
├── main.py                       # 应用入口（见 §8.3）
│
├── src/
│   ├── __init__.py               # 包版本号 __version__ = "1.0.0"
│   │
│   ├── core/                     # ★ 基础设施层（N1 应完成，N4 前必须补完）
│   │   ├── __init__.py
│   │   ├── config.py             # 配置加载 + 校验（Pydantic Settings）【0 字节 → 需补】
│   │   ├── neo4j_client.py       # Neo4j 连接管理（单例/生命周期）【0 字节 → 需补】
│   │   └── graphiti_client.py    # Graphiti 实例封装（N4 新增）
│   │
│   ├── adapters/                 # 数据源适配器层【N2 ✅ 已完成】
│   │   ├── __init__.py           # 适配器注册表【已实现】
│   │   ├── base.py               # BaseAdapter 抽象基类【已实现】
│   │   ├── models.py             # NormalizedEpisode / EntityItem【已实现】
│   │   ├── gdelt_adapter.py      # GDELT CSV 适配器【已实现】
│   │   ├── gdelt_codebook.py     # CAMEO/Actor/Theme 代码翻译【V2.3 新增】
│   │   ├── gdelt_events_parser.py # Events CSV 解析【V2.3 新增】
│   │   ├── gdelt_mentions_parser.py # Mentions CSV 解析【V2.3 新增】
│   │   ├── rss_adapter.py        # RSS 抓取适配器【已实现】
│   │   ├── akshare_adapter.py    # AkShare 个股新闻适配器【已实现】
│   │   └── macro_themes.py       # GDELT 宏观主题白名单（19 核心主题）【V2.2 新增】
│   │   └── treasury_adapter.py   # Treasury API 适配器【已实现】
│   │
│   ├── graphiti/                 # Graphiti 知识图集成层【N3 ✅ 已完成】
│   │   ├── __init__.py
│   │   ├── entity_types.py       # 实体类型 Pydantic 定义【已实现】
│   │   ├── relation_types.py     # 关系类型 + edge_type_map【已实现】
│   │   ├── episode_writer.py     # EpisodeWriter (去重 + 写入)【已实现】
│   │   └── translation.py        # 共享翻译层: Episodic/Entity → EventItem (V2.1 新增)【需新建】
│   │
│   ├── api/                      # REST API 层【N4 🔴 未开始 — 全部 0 字节】
│   │   ├── __init__.py
│   │   ├── server.py             # FastAPI 应用工厂 + uvicorn 入口【0 字节 → 本文件定义】
│   │   ├── deps.py               # FastAPI 依赖注入（get_config, get_graphiti）【新建】
│   │   ├── models.py             # API 响应 Pydantic 模型（EventResponse 等）【0 字节 → 本文件定义】
│   │   └── routers/
│   │       ├── __init__.py
│   │       ├── events.py         # /api/events/* 端点实现【0 字节 → 本文件定义】
│   │       └── health.py         # /api/events/health 端点【0 字节 → 本文件定义】
│   │
│   ├── sync/                     # SynapseEngine 同步【N3 ✅ 已完成】
│   │   ├── __init__.py
│   │   └── ticker_sync.py        # TickerSync 客户端【已实现】
│   │
│   ├── ingestion/                # ★ 数据摄取调度层（N4 阶段创建）
│   │   ├── __init__.py
│   │   ├── scheduler.py          # 多源调度编排（15 分钟 Cron 轮询）
│   │   ├── pipeline.py           # 完整管线: fetch → normalize → dedup → write → health check
│   │   └── briefing_aggregator.py # SectorBriefingAggregator（sector_briefing 生成）
│   │
│   └── utils/                    # 工具函数【全部 0 字节 → 需补】
│       ├── __init__.py
│       ├── logging_config.py     # 结构化日志配置【0 字节 → 需补】
│       └── time_utils.py         # 时间工具（HKT 转换、ISO 8601）【0 字节 → 需补】
│
├── tests/                        # 测试【需创建】
│   ├── __init__.py
│   ├── conftest.py               # 全局 fixture（mock Neo4j、mock 百炼、mock GDELT）
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_config.py
│   │   ├── test_adapters/
│   │   │   ├── __init__.py
│   │   │   ├── test_base.py
│   │   │   ├── test_gdelt.py
│   │   │   ├── test_rss.py
│   │   │   ├── test_akshare.py
│   │   │   └── test_treasury.py
│   │   ├── test_graphiti/
│   │   │   ├── __init__.py
│   │   │   ├── test_episode_writer.py
│   │   │   ├── test_entity_types.py
│   │   │   └── test_relation_types.py
│   │   └── test_api/
│   │       ├── __init__.py
│   │       ├── test_events.py
│   │       └── test_health.py
│   └── integration/
│       ├── __init__.py
│       ├── test_neo4j_connection.py
│       ├── test_graphiti_write.py
│       ├── test_gdelt_pipeline.py
│       └── test_api_endpoints.py
│
├── logs/                         # 日志目录【已存在】
│   └── news_engine.log
├── data/                         # 数据目录【已存在】
│   ├── neo4j/                    # Neo4j 数据卷
│   └── ticker_cache.json         # TickerSync 缓存文件
└── docs/                         # 文档【已存在】
    ├── NEWSENGINE-DESIGN-DOC.md       # 本文件（单一真相源）
    └── NEWSENGINE-IMPLEMENT-PLAN.md   # 实施计划（软链接 → SynapseEngine）
```

**变更标记说明:**
- **【已存在】** = N1/N2/N3 阶段已创建且实现（有实质代码）
- **【已实现】** = 文件已存在且有完整实现
- **【0 字节 → 需补】** = 空壳文件，N4 前必须补完
- **【需新建/创建】** = 文件尚不存在，N4 阶段需创建
- **【建议新增】** = 非阻塞，建议后续迭代添加

## 3.2 模块职责矩阵

| 模块 | 一级职责 | 依赖（入口方向） | 对外暴露 |
|------|---------|-----------------|---------|
| `core/config.py` | 配置加载、校验、环境变量解析 | `python-dotenv`, `pydantic-settings` | `Settings` 单例 |
| `core/neo4j_client.py` | Neo4j Driver 生命周期管理 | `core/config.py`, `neo4j` 驱动 | `get_neo4j_driver()`, `close_neo4j_driver()` |
| `core/graphiti_client.py` | Graphiti SDK 实例创建与配置 | `core/config.py`, `core/neo4j_client.py`, `graphiti/` | `create_graphiti()` |
| `adapters/` | 原始数据 → `NormalizedEpisode` 转换 + 管线过滤（V2.2）+ GDELT 三 CSV 整合 + Codebook 翻译（V2.3） | `core/config.py`, `adapters/models.py`, `adapters/macro_themes.py`, `adapters/gdelt_codebook.py` (V2.3) | `BaseAdapter` 子类 |
| `adapters/macro_themes.py` | GDELT 宏观主题白名单（19 核心金融主题）| 零依赖（纯常量） | `MACRO_THEME_KEYWORDS` |
| `adapters/gdelt_codebook.py` (V2.3) | CAMEO/Actor/Theme 代码的人类可读翻译 | 零依赖（加载 JSON 码表） | `translate_cameo()`, `translate_actor()`, `translate_theme()` |
| `adapters/gdelt_events_parser.py` (V2.3) | GDELT Events CSV 下载/解析/结构化 | `core/config.py`, `adapters/gdelt_codebook.py` | `parse_events()` |
| `adapters/gdelt_mentions_parser.py` (V2.3) | GDELT Mentions CSV 下载/解析/关联 | `core/config.py` | `parse_mentions()` |
| `graphiti/` | `NormalizedEpisode` → Neo4j 知识图写入 | `graphiti-core`, `adapters/models.py`, `core/neo4j_client.py` | `EpisodeWriter` |
| `graphiti/translation.py` | Neo4j Episodic/Entity record → 业务模型 (EventItem/EventEntityItem) 共享翻译 | `graphiti/entity_types.py` | `translate_episode_to_event()`, `translate_entity_to_item()`, `SEVERITY_WEIGHT` |
| `sync/` | SynapseEngine ticker 白名单管理 | `requests` | `get_ticker_whitelist()` |
| `ingestion/` | 多源调度编排（适配器 + Graphiti + ticker sync + briefing） | `adapters/`, `graphiti/`, `sync/`, `core/` | `run_ingestion_cycle()` |
| `api/` | REST API 端点实现 + FastAPI 应用 | `core/`, `graphiti/`, `ingestion/` | FastAPI `app` |
| `utils/` | 日志、时间工具（零业务依赖） | 无 | `get_logger()`, `now_hkt()` |
| `main.py` | 进程入口：初始化 → 启动 API + 调度器 | 所有模块 | 进程启动 |

## 3.3 模块依赖图

```
                         ┌─────────────────────┐
                         │     main.py          │
                         │   (进程入口)          │
                         └──────┬──────┬───────┘
                   ┌────────────┘      └────────────┐
                   ▼                                 ▼
        ┌─────────────────────┐           ┌─────────────────────┐
        │   api/server.py     │           │  ingestion/          │
        │   (FastAPI 应用)     │           │  (调度编排)           │
        └──────────┬──────────┘           └──────────┬──────────┘
                   │                                 │
        ┌──────────┼──────────┐           ┌──────────┼──────────┐
        ▼          ▼          ▼           ▼          ▼          ▼
   ┌────────┐┌────────┐┌────────┐   ┌────────┐┌────────┐┌────────┐
   │api/    ││graphiti││core/   │   │adapters││graphiti││sync/   │
   │routers ││translat││graphiti│   │/       ││/episode││ticker  │
   │        ││ion     ││_client │   │        ││_writer ││_sync   │
   └───┬────┘└───┬────┘└───┬────┘   └───┬────┘└───┬────┘└───┬────┘
       │         │         │            │         │          │
       └─────────┼─────────┼────────────┘         │          │
                 │         ▼                      │          │
                 │  ┌──────────────┐              │          │
                 │  │graphiti/     │              │          │
                 │  │entity_types  │◄─────────────┘          │
                 │  └──────┬───────┘                         │
                 │         │                                  │
                 └─────────┼──────────────────────────────────┘
                           │
                    ┌──────▼───────┐
                    │ core/        │
                    │ neo4j_client │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ core/config  │
                    │ (Pydantic    │
                    │  Settings)   │
                    └──────────────┘
                           ▲
                    ┌──────┴───────┐
                    │ utils/       │
                    │ (logging,    │
                    │  time_utils) │
                    └──────────────┘
```

**依赖铁律（10 条，违反即 BUG）:**

1. **`core/config.py`** — 零业务依赖，仅依赖 `python-dotenv` + `pydantic-settings`。所有模块通过依赖注入获取配置，不直接 import。
2. **`core/neo4j_client.py`** — 仅依赖 `core/config.py` + `neo4j` 驱动。不依赖任何适配器或 graphiti 模块。
3. **`core/graphiti_client.py`** — 依赖 `core/config.py` + `core/neo4j_client.py` + `graphiti/` 类型定义。封装 Graphiti SDK 实例化。
4. **`adapters/`** — 仅依赖 `adapters/models.py` + `core/config.py`（通过依赖注入）。不依赖 `graphiti/` 或 `api/`。
5. **`graphiti/entity_types, relation_types, episode_writer`** — 依赖 `graphiti-core` + `adapters/models.py`（NormalizedEpisode）。不依赖任何具体适配器。
6. **`graphiti/translation.py`** — 依赖 `graphiti/entity_types.py`（Entity 类型定义）。零 `api/` 依赖（可被任意模块 import）。被 `api/routers/events.py` 和 `ingestion/briefing_aggregator.py` 共同消费。
7. **`ingestion/`** — 编排层，依赖 `adapters/` + `graphiti/`（含 translation）+ `sync/` + `core/`。不依赖 `api/`。
8. **`api/`** — 依赖 `core/` + `graphiti/`（含 translation）。不直接依赖 `adapters/` 或 `sync/`。
9. **`utils/`** — 零业务依赖，可被所有模块 import。
10. **循环依赖零容忍** — `adapters/` ↔ `graphiti/` 之间的桥接通过 `adapters/models.py` 共享类型实现，不互相 import。`translation.py` 仅依赖 `graphiti/entity_types.py`，不导入 `api/` 或 `ingestion/`。

**共享类型（避免循环依赖的关键）:**

`src/adapters/models.py` 中的 `NormalizedEpisode` 是适配器层和 graphiti 层的**共享数据契约**：
- 适配器层**产出** `NormalizedEpisode`（`fetch → normalize → dedup`）
- Graphiti 层**消费** `NormalizedEpisode`（`EpisodeWriter.write_one`）

`src/graphiti/translation.py` 中的翻译函数是 Graphiti schema 和业务模型的**共享转换契约**：
- API 路由层**调用** `translate_episode_to_event()` 将 Neo4j record → `EventItem` JSON
- 简报聚合层**调用**同一函数做 Neo4j record → 聚合输入 dict

两边都依赖同一个 translation 文件，避免了翻译逻辑的 DRY 漂移。

## 3.4 生命周期管理

### 3.4.1 启动顺序（严格 FIFO）

| 步骤 | 操作 | 负责模块 | 失败处理 |
|------|------|---------|---------|
| 0 | `python main.py` 被调用 | `main.py` | 进程退出 |
| 1 | `load_settings()` — 加载 .env + 校验必需字段 | `core/config.py` | 抛出 `SettingsValidationError` → 进程退出 |
| 2 | `setup_logging()` — 结构化日志初始化 | `utils/logging_config.py` | 回退到标准 logging → WARNING |
| 3 | `Neo4jDriver.open()` — 建立 Bolt 连接 | `core/neo4j_client.py` | 重试 3 次 → 失败则退出（无 Neo4j 无法运行） |
| 4 | `setup_whitelist_route()` — 启动 whitelist POST 端点监听 | `api/routers/whitelist.py` | WARNING + 继续（等待 SynapseEngine push） |
| 5 | `create_graphiti()` — 初始化 Graphiti SDK | `core/graphiti_client.py` | 退出（百炼 API Key 无效无法运行） |
| 6 | `EpisodeWriter(graphiti)` — 创建写入器 | `graphiti/episode_writer.py` | 退出 |
| 7 | `start_ingestion_scheduler()` — 启动 Cron 调度器 | `ingestion/scheduler.py` | WARNING + 继续（不阻断 API） |
| 8 | `uvicorn.run(app)` — 启动 FastAPI | `api/server.py` | 进程退出 |

```
main.py 启动流程（伪代码）:

1. settings = load_settings()              # core/config.py
2. setup_logging(settings.log_level)       # utils/logging_config.py
3. driver = Neo4jDriver.open(settings)     # core/neo4j_client.py
4. setup_whitelist_route(app)              # api/routers/whitelist.py (监听 POST /api/tickers/whitelist)
5. graphiti = create_graphiti(driver)      # core/graphiti_client.py
6. writer = EpisodeWriter(graphiti)        # graphiti/episode_writer.py
7. scheduler = start_ingestion(writer, tickers)  # ingestion/scheduler.py
8. uvicorn.run(create_app(writer), port=8100)    # api/server.py
```

### 3.4.2 关闭顺序（LIFO）

| 步骤 | 操作 | 负责模块 |
|------|------|---------|
| 1 | `scheduler.stop()` — 停止 Cron 任务，等待当前轮完成 | `ingestion/scheduler.py` |
| 2 | `uvicorn` 优雅关闭 — 等待 pending requests 完成 | uvicorn 内置 |
| 3 | `writer.close()` — 关闭 EpisodeWriter 资源 | `graphiti/episode_writer.py` |
| 4 | `driver.close()` — 关闭 Neo4j 连接 | `core/neo4j_client.py` |

### 3.4.3 Entity Types 定义 — EventEntity 和 SymbolEventEntity（V2.3 新增）

**新增实体类型：Event**

V2.3 新增 EventEntity 作为第 6 个实体类型，用于建模"谁对谁做了什么"的结构化事件。与 PolicyEntity 的区别：
- **PolicyEntity**: 政策声明/监管行为（有状态机：rumor→announced→confirmed→implemented），重点在"说了什么"
- **EventEntity**: 结构化事件（有定量评分：Goldstein/Tone），重点在"做了什么，造成了多大的正面/负面影响"

**MACRO_ENTITY_TYPES（GDELT/RSS 管线）：**

```python
MACRO_ENTITY_TYPES = {
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Topic": TopicEntity,
    "Policy": PolicyEntity,
    "Sector": SectorEntity,
    "Event": EventEntity,  # V2.3 新增（完整版：含 cameo_code/goldstein_scale/tone）
}
```

**EventEntity Pydantic 模型（MACRO 版）：**

```python
class EventEntity(BaseModel):
    """事件实体 — 从 GDELT Events CSV 提取的结构化事件。
    
    Neo4j 节点标签: Entity:Event
    """
    entity_name: str = Field(
        ...,
        description="事件描述，一句话，使用中文。例如: '德国向乌克兰提供物资援助'"
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），翻译后的中文名。例如: '德国'"
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），翻译后的中文名。例如: '乌克兰'"
    )
    cameo_code: str | None = Field(
        default=None,
        description="CAMEO 事件代码。例如: '057' (提供援助), '173' (逮捕/拘留)"
    )
    goldstein_scale: float | None = Field(
        default=None,
        description="Goldstein 合作/冲突评分 (-10 ~ +10)。+10 = 最高合作，-10 = 最高冲突"
    )
    tone: float | None = Field(
        default=None,
        description="新闻语调评分 (-100 ~ +100，已归一化)。来源: GDELT AvgTone / 100"
    )
    event_date: str | None = Field(
        default=None,
        description="事件发生日期 (YYYY-MM-DD)。与 EpisodicNode.valid_at (文章发布时间) 是不同的概念"
    )
```

**SYMBOL_ENTITY_TYPES（AkShare 管线）：**

```python
SYMBOL_ENTITY_TYPES = {
    "Stock": StockEntity,
    "Sector": SectorEntity,
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Policy": PolicyEntity,
    "Event": SymbolEventEntity,  # V2.3 新增（简化版：无 cameo_code/goldstein_scale/tone）
}
```

**SymbolEventEntity Pydantic 模型（SYMBOL 版）：**

```python
class SymbolEventEntity(BaseModel):
    """事件实体 — 从 AkShare 个股新闻提取的事件。
    
    与 MACRO 版的 EventEntity 区别：没有 cameo_code/goldstein_scale/tone，
    因为 AkShare 数据源不提供这些结构化字段。
    
    Neo4j 节点标签: Entity:Event
    """
    entity_name: str = Field(
        ...,
        description="事件描述，一句话，使用中文。例如: '腾讯回购10亿港元'"
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），例如: '腾讯控股'"
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），例如: '香港交易所'"
    )
    event_date: str | None = Field(
        default=None,
        description="事件发生日期 (YYYY-MM-DD)"
    )
```

**跨管线关联机制：**

MACRO 版和 SYMBOL 版的 EventEntity 都映射到同一个 Neo4j 标签 `Entity:Event`，但字段集合不同。Graphiti 的 Pydantic schema 机制支持这种差异——不同管线使用不同的 entity_types 注册表。

**EventEntity 关系类型：**

| 关系 | 方向 | 示例 |
|------|------|------|
| Event → AFFECTS → Country | 事件对国家的影响 | "物资援助" → AFFECTS → "乌克兰" |
| Event → AFFECTS → Organization | 事件对组织的影响 | "制裁" → AFFECTS → "华为" |
| Event → CAUSED_BY → EventEntity | 事件因果链 | "制裁俄罗斯" → CAUSED_BY → "俄入侵乌" |
| Event → RELATED_TO → Topic | 事件属于什么话题 | "加息" → RELATED_TO → "货币政策" |
| Event → RELATED_TO → Sector | 事件影响什么行业 | "芯片出口管制" → RELATED_TO → "半导体" |

**查询场景示例：**

```cypher
-- 查询高度正面的事件（Goldstein > 5）
MATCH (e:Event)
WHERE e.goldstein_scale > 5
RETURN e.entity_name, e.actor1, e.actor2, e.goldstein_scale
ORDER BY e.goldstein_scale DESC
LIMIT 20

-- 查询与“腾讯控股”相关的所有事件（宏观 + 个股）
MATCH (org:Entity:Organization {entity_name: "腾讯控股"})-[:RELATES_TO]-(event:Entity:Event)
RETURN event.entity_name AS event_name, event.event_date AS date
ORDER BY event.event_date DESC
```

```python
# 文件: main.py — 启动时健康检查

async def check_dependencies(settings: Settings) -> bool:
    """验证所有必需依赖可用。返回 True 表示可以启动。"""
    all_ok = True

    # 1. Neo4j 连接（硬阻塞）
    try:
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=...)
        driver.verify_connectivity()
        logger.info("✅ Neo4j 连接正常 (%s)", settings.neo4j_uri)
    except Exception as exc:
        logger.critical("❌ Neo4j 连接失败: %s", exc)
        all_ok = False

    # 2. 百炼 API（可选验证，Graphiti 初始化时会检查）
    # 3. ticker 白名单（非阻塞，等待 SynapseEngine push，本地缓存兜底）
    try:
        whitelist = get_ticker_whitelist()
        if whitelist:
            logger.info("✅ ticker 白名单已加载 (%d 个)", len(whitelist))
        else:
            logger.warning("⚠️ ticker 白名单为空，等待 SynapseEngine push")
    except Exception:
        logger.warning("⚠️ 本地白名单缓存不可用")

    return all_ok  # 只有 Neo4j 是硬阻塞
```

### 3.4.4 运行时并发模型

NewsEngine 是一个**长期运行的服务进程**（不是一次性脚本），包含两个并发子系统：

```
┌──────────────────────────────────────────────────────────────┐
│                    NewsEngine 进程 (:8100)                     │
│                                                               │
│  ┌───────────────────────┐    ┌──────────────────────────┐   │
│  │ FastAPI REST API       │    │ Ingestion Scheduler      │   │
│  │ (asyncio event loop)   │    │ (asyncio background task)│   │
│  │                        │    │                           │   │
│  │ 处理 HTTP 请求          │    │ 每 15 分钟:                │   │
│  │ GET /api/events/*      │    │   GDELT CSV → Episode     │   │
│  │                        │    │   RSS Feed → Episode      │   │
│  │                        │    │   AkShare → Episode       │   │
│  │                        │    │                           │   │
│  │                        │    │ 接收 SynapseEngine push:   │   │
│  │                        │    │   POST /api/tickers/       │   │
│  │                        │    │   whitelist               │   │
│  │                        │    │   SectorBriefingAggregator│   │
│  │                        │    │   .aggregate_all()        │   │
│  └───────────────────────┘    └──────────────────────────┘   │
│                                                               │
│  共享资源: Neo4j Driver (线程安全), EpisodeWriter (串行)       │
└──────────────────────────────────────────────────────────────┘
```

**并发安全约束:** Graphiti SDK 的 `add_episode()` 必须串行（不是线程安全的）。`EpisodeWriter.write_batch()` 已确保串行调用。FastAPI 和 scheduler 共享同一个 asyncio event loop，天然协程级并发安全。

## 3.5 core/graphiti_client.py 职责

```python
# 文件: src/core/graphiti_client.py
# 职责: Graphiti SDK 实例创建与配置
# 不负责 Episode 写入（那是 graphiti/episode_writer.py 的职责）

from graphiti_core import Graphiti
from graphiti_core.utils.maintenance.graph_data_operations import clear_data
from neo4j import Driver

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver
from src.graphiti.entity_types import ENTITY_TYPES
from src.graphiti.relation_types import EDGE_TYPES, DEFAULT_EDGE_TYPE_MAP


def create_graphiti(driver: Driver | None = None) -> Graphiti:
    """创建 Graphiti 实例。每次调用创建新实例，由调用方管理生命周期。

    为何不设单例？Graphiti SDK 的 add_episode() 必须串行（不是线程安全的），
    单例会引入并发风险。由 EpisodeWriter 持有实例并在每次 write_batch 中串行使用。
    """
    settings = get_settings()
    _driver = driver or get_neo4j_driver()

    return Graphiti(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        llm_client={
            "api_key": settings.bailian_api_key,
            "base_url": settings.openai_base_url,
            "model": settings.llm_model,
        },
        embedder_client={
            "api_key": settings.bailian_api_key,
            "base_url": settings.openai_base_url,
            "model": settings.embedding_model,
        },
    )
```

---

n

## 3.6 数据接入层 — 宏观/个股双管线过滤策略（V2.2 新增）

### 3.6.1 架构概述

数据接入层拆分为**宏观管线**和**个股管线**，使用独立的 pre-ingestion 过滤策略：

```
IngestionScheduler._run_cycle()
        │
        ├── Step 1: 读取 ticker whitelist（仅用于 AkShare）
        │
        ├── Step 2: 并发运行 3 条管线
        │     ├── GDELT Pipeline: fetch → theme_filter → normalize → dedup → write
        │     ├── RSS Pipeline:   fetch → (no filter) → normalize → dedup → write
        │     └── AkShare Pipeline: fetch → (ticker_filter implicit) → normalize → dedup → write
        │
        ├── Step 3: Severity enrichment (L-4 rule engine)
        │
        └── Step 4: SectorBriefingAggregator.aggregate_all()
```

**适配器初始化差异：**

| 适配器 | 构造参数 | 过滤属性 |
|--------|---------|---------|
| `GdeltAdapter` | `macro_theme_keywords: set[str]` | `_macro_theme_keywords`（19 个主题，OR 匹配） |
| `RssAdapter` | `feed_urls: list[str]` | 无（零过滤） |
| `AkShareAdapter` | `ticker_whitelist: list[dict]` | `ticker_whitelist`（SynapseEngine push） |

**Scheduler._update_adapter_tickers() 拆分（V2.2）：**

```python
def _update_adapter_tickers(self, tickers):
    # ticker whitelist 仅用于 AkShare 个股管线
    # GDELT/RSS 不使用 ticker whitelist（使用 macro_theme 白名单 / 零过滤）
    if self._akshare_adapter is not None:
        self._akshare_adapter.ticker_whitelist = tickers
        self._akshare_adapter._symbol_map = {}
        for entry in tickers:
            biz_code = entry.get("biz_code", "")
            if biz_code:
                self._akshare_adapter._symbol_map[biz_code] = entry
```

### 3.6.2 GDELT 双层过滤策略（V2.3 更新）

**文件：** `src/adapters/macro_themes.py` + `src/adapters/gdelt_codebook.py`

**V2.3 变更：** 从单一 GKG Themes 白名单扩展为 GKG Themes + Events CAMEO 码双层过滤。

**匹配方式：**

**Layer A: GKG Themes 白名单过滤（V2.2 逻辑，保留）**
- 子串匹配（`keyword.lower() in themes_text.lower()`）
- 19 个关键词中**任一命中即保留**（OR 逻辑）
- 匹配对象为 GKG V2.8 Themes 列

**Layer B: Events CAMEO 码过滤（V2.3 新增）**
- CAMEO 事件码匹配（基于 Codebook 翻译后的中文标签）
- 匹配对象为 Events CSV 的 EventBaseCode 列
- 金融相关 CAMEO 码白名单（如 012/057/064/082 等）

**双层过滤逻辑：**
```python
def filter_relevant(self, records: list[dict]) -> list[dict]:
    """双层宏观过滤：GKG Themes + Events CAMEO 码。"""
    if not self._macro_theme_keywords and not self._macro_cameo_codes:
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

**19 个核心主题：**

| # | 类别 | 关键词 | 覆盖的投资场景 |
|---|------|--------|-------------|
| 1 | 货币政策 | `MONETARY_POLICY` | 货币政策转向、QE/QT |
| 2 | 货币政策 | `CENTRAL_BANK` | 央行政策声明、利率决议 |
| 3 | 货币政策 | `INTEREST_RATE` | 利率变动、收益率曲线 |
| 4 | 宏观指标 | `GDP` | 国内生产总值、经济增长 |
| 5 | 宏观指标 | `INFLATION` | CPI/PPI 超预期、通胀预期 |
| 6 | 宏观指标 | `RECESSION` | 经济衰退信号、PMI 下滑 |
| 7 | 贸易制裁 | `TRADE` | 贸易争端、301 关税、谈判 |
| 8 | 贸易制裁 | `TARIFF` | 关税政策、贸易壁垒 |
| 9 | 贸易制裁 | `SANCTION` | 经济制裁、金融制裁 |
| 10 | 地缘政治 | `GEOPOLITICAL` | 地缘冲突、台海、南海 |
| 11 | 地缘政治 | `WAR` | 军事冲突 |
| 12 | 监管 | `REGULATION` | 监管政策、法律变更 |
| 13 | 监管 | `ANTITRUST` | 反垄断调查、拆分 |
| 14 | 市场 | `STOCK_MARKET` | 全球股市波动、交易所事件 |
| 15 | 市场 | `CURRENCY` | 汇率波动、货币贬值、人民币 |
| 16 | 科技 | `SEMICONDUCTOR` | 芯片出口管制、供应链 |
| 17 | 能源 | `ENERGY` | 能源危机、OPEC+ 减产 |
| 18 | 债务风险 | `DEBT` | 主权债务、信用违约、恒大/碧桂园 |
| 19 | 汇率 | `EXCHANGE_RATE` | 人民币汇率、日元贬值 |

**排除的主题及理由：**

| 排除关键词 | 理由 |
|-----------|------|
| `ECONOMY` | 太泛，GDELT GKG 中 ~30% 记录带经济标签，等于不过滤 |
| `MARKETS` | 太泛，覆盖劳动力市场、商品市场等，金融信号密度低 |
| `TECHNOLOGY` | 太泛，AI 论文、App 更新都算，与投资无关；`SEMICONDUCTOR` 已覆盖核心科技供应链 |
| `CHINA` / `USA` / `ASIA` | 地理标签，会引入体育/娱乐/社会新闻噪音 |
| `DIPLOMACY` | 太泛，外交访问/协议等低频市场影响 |
| `QE` | 与 `MONETARY_POLICY` 重叠 |
| `EMPLOYMENT` | GDELT 中极少独立出现就业主题 |
| `COMMODITIES` | 与 `ENERGY` 重叠 |

**常量定义：**

```python
# src/adapters/macro_themes.py
"""GDELT GKG macro theme whitelist for financial event filtering.

19 core themes organized by category. Any theme keyword matching a GKG
record's V2.8 Themes field will cause the record to be retained (OR logic).
"""

MACRO_THEME_KEYWORDS: set[str] = {
    # 货币政策 (3)
    "MONETARY_POLICY",
    "CENTRAL_BANK",
    "INTEREST_RATE",
    # 宏观指标 (3)
    "GDP",
    "INFLATION",
    "RECESSION",
    # 贸易制裁 (3)
    "TRADE",
    "TARIFF",
    "SANCTION",
    # 地缘政治 (2)
    "GEOPOLITICAL",
    "WAR",
    # 监管 (2)
    "REGULATION",
    "ANTITRUST",
    # 市场 (2)
    "STOCK_MARKET",
    "CURRENCY",
    # 科技 (1)
    "SEMICONDUCTOR",
    # 能源 (1)
    "ENERGY",
    # 债务风险 (1)
    "DEBT",
    # 汇率 (1)
    "EXCHANGE_RATE",
}
```

**GDELT filter_relevant() 改写：**

```python
def filter_relevant(self, records: list[dict]) -> list[dict]:
    """Macro theme filter — 19 核心金融主题 OR 匹配。

    V2.2: 代替原来的 ticker_whitelist 关键词过滤。
    匹配 GKG V2.8 Themes 列中是否包含任一核心金融主题。
    """
    if not self._macro_theme_keywords:
        return records

    matched: list[dict] = []
    for rec in records:
        themes_text = (rec.get("themes") or "").lower()
        if any(kw.lower() in themes_text for kw in self._macro_theme_keywords):
            matched.append(rec)

    logger.info(
        "GDELT theme filter: %d → %d records (themes=%s)",
        len(records), len(matched),
        "OR".join(sorted(self._macro_theme_keywords))[:80]
    )
    return matched
```

**预期过滤效果：**

| 指标 | 当前 (ticker whitelist) | 方案后 (19 themes OR) |
|------|------------------------|---------------------|
| 单轮输入 | 824 条 | 824 条 |
| 单轮输出 | **0 条** | **80-180 条** |
| 保留比例 | 0% | 10-22% |
| 日写入 (4 轮) | 0 条 | 320-720 条（Graphiti 去重前） |
| 日有效 Episode | 0 条 | 80-200 条（Graphiti 去重后） |

### 3.6.3 RSS 零过滤

**文件：** `src/adapters/rss_adapter.py`

**方案：** 移除 `filter_relevant()` 调用，`fetch()` 直接返回全部 RSS entry。

**当前代码移除：**
```python
# 移除这一行
all_entries = self.filter_relevant(all_entries)
```

**适配器构造参数变更：**
```python
# 移除 ticker_whitelist 参数
class RssAdapter(BaseAdapter):
    def __init__(self, feed_urls=None, dedup_cache=None):
        self.feed_urls = feed_urls or []
        # 不再有 self.ticker_whitelist
```

**数据量评估：**
- MarketWatch Top Stories：~6-8 条/次
- FT：~4-6 条/次
- 每轮周期 ~10 条
- 每天 ~40 条（4 轮 15min）
- Graphiti 语义去重后 ~25-30 条/天有效 Episode

**零过滤的理由：**
1. 量级极低（~10 条/周期），不存在存储压力
2. MarketWatch + FT 源头已是财经精选内容
3. 任何 pre-ingestion 过滤都可能丢失关键宏观评论（如 FT 专栏对地缘政治的分析可能不提具体经济术语）

### 3.6.4 数据量三层防御

```
Layer 1: Pre-Ingestion 主题过滤（适配器层）
  GDELT: 824 → ~80-180 条/周期
  RSS:   10  → 10 条/周期（零过滤）
  降低 GDELT 75-90%

Layer 2: Graphiti 语义去重（SDK 层）
  ~80-180 → ~30-80 条/周期
  Graphiti 的 add_episode() 内置语义相似度判重，
  同一事件多信源报道合并为同一 Episode
  降低 40-60%

Layer 3: Neo4j TTL 淘汰（存储层）
  MACRO 14 天 / SECTOR 7 天 / SYMBOL 3 天
  每天 1 次 DETACH DELETE 清理
  查询层加 created_at 时间窗口 WHERE
  活跃事件集控制在 ~5K 以内
```

### 3.6.5 content_scope 写入链路

```
适配器 normalize()
        │
        ├── GdeltAdapter:  metadata["content_scope"] = "MACRO"
        ├── RssAdapter:    metadata["content_scope"] = "MACRO"
        └── AkShareAdapter: metadata["content_scope"] = "SYMBOL"
                │
                ▼
        NormalizedEpisode.metadata
                │
                ▼
        EpisodeWriter.write_one()
        → graphiti.add_episode(episode)
                │
                ▼
        EpisodicNode.episode_metadata  ←  Graphiti SDK 自动透传
                │
                ▼
        Neo4j: (ep:Episodic {episode_metadata: '{"content_scope": "MACRO"}'})
                │
                ▼
        查询: MATCH (ep:Episodic)
              WHERE ep.episode_metadata CONTAINS 'MACRO'
```


# Part 4: 配置与测试

（来源: Internal Spec §5~§6，原文完整保留）

## 4.1 配置管理规范

### 4.1.1 .env 完整字段定义

```env
# ============================================================
# NewsEngine 配置 V1.0
# ============================================================

# === 阿里百炼 API (LLM + Embedding) ===
BAILIAN_API_KEY=sk-***
# 敏感: 是 | 默认: — | 必填: 是
# 说明: 阿里百炼 API Key，用于 Qwen LLM 和 text-embedding-v4

OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# 敏感: 否 | 默认: https://dashscope.aliyuncs.com/compatible-mode/v1 | 必填: 否
# 说明: 百炼兼容 OpenAI 格式的 API Base URL

EMBEDDING_MODEL=text-embedding-v4
# 敏感: 否 | 默认: text-embedding-v4 | 必填: 否
# 说明: 百炼嵌入模型。可选: text-embedding-v3, text-embedding-v4

LLM_MODEL=qwen-plus
# 敏感: 否 | 默认: qwen-plus | 必填: 否
# 说明: 百炼 LLM 模型。可选: qwen-plus, qwen-max, qwen3.7-plus

# === Neo4j 连接 ===
NEO4J_URI=bolt://localhost:7687
# 敏感: 否 | 默认: bolt://localhost:7687 | 必填: 否
# 说明: Neo4j Bolt 协议连接地址

NEO4J_USER=neo4j
# 敏感: 否 | 默认: neo4j | 必填: 否

NEO4J_PASSWORD=newsengine2026
# 敏感: 是 | 默认: newsengine2026 | 必填: 否
# 说明: Neo4j 密码。生产环境必须修改

# === FastAPI 服务 ===
API_HOST=0.0.0.0
# 敏感: 否 | 默认: 0.0.0.0 | 必填: 否
# 说明: FastAPI 监听地址。0.0.0.0 允许外部访问

API_PORT=8100
# 敏感: 否 | 默认: 8100 | 必填: 否
# 说明: FastAPI 监听端口。需与 Design Doc §6.5 端口规划一致

# === SynapseEngine 连接 ===
SYNAPSE_BASE_URL=http://localhost:8000
# 敏感: 否 | 默认: http://localhost:8000 | 必填: 否
# 说明: SynapseEngine 的 REST API 地址（用于运维查询 /api/portfolio/tickers 等）

TICKER_WHITELIST_FILE=data/ticker_whitelist.json
# 敏感: 否 | 默认: data/ticker_whitelist.json | 必填: 否
# 说明: ticker 白名单本地缓存文件路径。NewsEngine 接收 SynapseEngine push 后持久化到此文件作为兜底

# === 日志 ===
LOG_LEVEL=INFO
# 敏感: 否 | 默认: INFO | 必填: 否
# 说明: Python logging level。可选: DEBUG, INFO, WARNING, ERROR, CRITICAL

LOG_FILE=logs/news_engine.log
# 敏感: 否 | 默认: logs/news_engine.log | 必填: 否
# 说明: 日志文件路径。空字符串表示仅输出到 stdout

# === 数据摄取 ===
INGESTION_INTERVAL_SEC=900
# 敏感: 否 | 默认: 900 (15 分钟) | 必填: 否
# 说明: 数据源轮询间隔（秒）

# === GDELT ===
GDELT_LASTUPDATE_URL=http://data.gdeltproject.org/gdeltv2/lastupdate.txt
# 敏感: 否 | 默认: http://data.gdeltproject.org/gdeltv2/lastupdate.txt | 必填: 否
# 说明: GDELT V2 lastupdate.txt 地址（HTTP，非 HTTPS）

GDELT_MAX_RETRIES=3
# 敏感: 否 | 默认: 3 | 必填: 否
# 说明: GDELT 下载失败时的最大重试次数

GDELT_TIMEOUT_SEC=60
# 敏感: 否 | 默认: 60 | 必填: 否
# 说明: GDELT HTTP 请求超时（秒）

# === RSS ===
RSS_TIMEOUT_SEC=30
# 敏感: 否 | 默认: 30 | 必填: 否
# 说明: RSS feed HTTP 请求超时（秒）

# === AkShare ===
AKSHARE_REQUEST_INTERVAL_SEC=0.5
# 敏感: 否 | 默认: 0.5 | 必填: 否
# 说明: AkShare 单只股票查询间隔（秒），限速保护

# === Risk Summary 缓存 ===
RISK_SUMMARY_CACHE_TTL_SEC=300
# 敏感: 否 | 默认: 300 (5 分钟) | 必填: 否
# 说明: /api/events/risk-summary 结果缓存时间（秒）。Design Doc §2.6 要求的缓存策略

# === Episode TTL 淘汰 (V2.2 新增) ===
EPISODE_TTL_MACRO_DAYS=14
# 敏感: 否 | 默认: 14 (2 周) | 必填: 否
# 说明: MACRO scope Episode 保留天数。宏观趋势变化慢，2 周内对 PM Agent 仍有价值

EPISODE_TTL_SECTOR_DAYS=7
# 敏感: 否 | 默认: 7 (1 周) | 必填: 否
# 说明: SECTOR scope Episode 保留天数

EPISODE_TTL_SYMBOL_DAYS=3
# 敏感: 否 | 默认: 3 (3 天) | 必填: 否
# 说明: SYMBOL scope Episode 保留天数。个股消息 3 天后已反映在股价里

TTL_CLEANUP_INTERVAL_HOURS=24
# 敏感: 否 | 默认: 24 (每天 1 次) | 必填: 否
# 说明: TTL DETACH DELETE 清理间隔（小时）。每天执行 1 次，在 ingestion cycle 之间执行

# === GDELT 宏观主题 (V2.2 新增) ===
GDELT_MACRO_THEMES_FILE=src/adapters/macro_themes.py
# 敏感: 否 | 默认: src/adapters/macro_themes.py | 必填: 否
# 说明: GDELT 宏观主题白名单常量文件路径（19 个核心金融主题）
```

### 4.1.2 Pydantic Settings 实现 (core/config.py)

当前 `core/config.py` 为 0 字节空壳。N4 实现前必须补完为：

```python
# 文件: src/core/config.py
"""配置加载 — 使用 Pydantic Settings 从 .env 和环境变量加载配置。

所有模块通过 get_settings() 获取全局单例，或通过依赖注入传递。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """NewsEngine 配置模型。所有字段从 .env 文件或环境变量加载。"""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 百炼 API ──
    bailian_api_key: str = Field(..., description="阿里百炼 API Key")
    openai_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="百炼 OpenAI 兼容 Base URL",
    )
    embedding_model: str = Field(
        default="text-embedding-v4",
        description="百炼 Embedding 模型名",
    )
    llm_model: str = Field(
        default="qwen3.7-plus",
        description="百炼 LLM 模型名",
    )

    # ── Neo4j ──
    neo4j_uri: str = Field(
        default="bolt://localhost:7687",
        description="Neo4j Bolt URI",
    )
    neo4j_user: str = Field(
        default="neo4j",
        description="Neo4j 用户名",
    )
    neo4j_password: str = Field(
        default="newsengine2026",
        description="Neo4j 密码",
    )

    # ── FastAPI ──
    api_host: str = Field(
        default="0.0.0.0",
        description="FastAPI 监听地址",
    )
    api_port: int = Field(
        default=8100,
        description="FastAPI 监听端口",
    )

    # ── SynapseEngine ──
    synapse_base_url: str = Field(
        default="http://localhost:8000",
        description="SynapseEngine REST API 地址",
    )

    # ── 日志 ──
    log_level: str = Field(
        default="INFO",
        description="日志级别",
    )
    log_file: str = Field(
        default="logs/news_engine.log",
        description="日志文件路径",
    )

    # ── 数据摄取 ──
    ingestion_interval_sec: int = Field(
        default=900,
        description="数据源轮询间隔（秒）",
    )
    # ── GDELT ──
    gdelt_lastupdate_url: str = Field(
        default="http://data.gdeltproject.org/gdeltv2/lastupdate.txt",
        description="GDELT V2 lastupdate.txt URL",
    )
    gdelt_max_retries: int = Field(
        default=3,
        description="GDELT 下载重试次数",
    )
    gdelt_timeout_sec: int = Field(
        default=60,
        description="GDELT HTTP 超时（秒）",
    )

    # ── RSS ──
    rss_timeout_sec: int = Field(
        default=30,
        description="RSS HTTP 超时（秒）",
    )

    # ── AkShare ──
    akshare_request_interval_sec: float = Field(
        default=0.5,
        description="AkShare 查询间隔（秒）",
    )

    # ── Risk Summary 缓存 ──
    risk_summary_cache_ttl_sec: int = Field(
        default=300,
        description="Risk Summary 结果缓存 TTL（秒）",
    )

    # ── Episode TTL 淘汰 (V2.2 新增) ──
    episode_ttl_macro_days: int = Field(
        default=14,
        description="MACRO scope Episode 保留天数",
    )
    episode_ttl_sector_days: int = Field(
        default=7,
        description="SECTOR scope Episode 保留天数",
    )
    episode_ttl_symbol_days: int = Field(
        default=3,
        description="SYMBOL scope Episode 保留天数",
    )
    ttl_cleanup_interval_hours: int = Field(
        default=24,
        description="TTL 清理间隔（小时）",
    )

    # ── GDELT 宏观主题 (V2.2 新增) ──
    gdelt_macro_themes_file: str = Field(
        default="src/adapters/macro_themes.py",
        description="GDELT 宏观主题白名单常量文件路径",
    )

    # ── 校验 ──

    @field_validator("bailian_api_key")
    @classmethod
    def api_key_must_not_be_placeholder(cls, v: str) -> str:
        if v in ("***", "sk-***", "your-api-key", ""):
            raise ValueError(
                "BAILIAN_API_KEY 必须设置为真实的百炼 API Key，不可使用占位符"
            )
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"LOG_LEVEL 必须为 {valid} 之一，收到: {v}")
        return upper


# ── 单例 ──

_settings: Settings | None = None


def get_settings() -> Settings:
    """获取全局配置单例。线程安全。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """重新加载配置（测试用）。"""
    global _settings
    _settings = Settings()
    return _settings
```

### 4.1.3 当前 .env 与完整 .env 的差异

| 当前 .env (6 行) | 完整 .env (16 行) | 差异 |
|-----------------|-----------------|------|
| `BAILIAN_API_KEY` | `BAILIAN_API_KEY` | 保留 |
| `OPENAI_BASE_URL` | `OPENAI_BASE_URL` | 保留 |
| `EMBEDDING_MODEL=text-embedding-v4` | `EMBEDDING_MODEL=text-embedding-v4` | 保留（已验证可行） |
| `LLM_MODEL=qwen3.7-plus` | `LLM_MODEL=qwen3.7-plus` | 保留 |
| `NEO4J_URI` | `NEO4J_URI` | 保留 |
| `NEO4J_USER` | `NEO4J_USER` | 保留 |
| `NEO4J_PASSWORD` | `NEO4J_PASSWORD` | 保留 |
| — | `API_HOST` | **缺失** |
| — | `API_PORT` | **缺失** |
| `SYNAPSE_BASE_URL` | — | `SYNAPSE_BASE_URL` | **保留**（运维查询用，不再用于拉取 ticker） |
| — | `TICKER_WHITELIST_FILE` | **新增** |
| — | `LOG_LEVEL` | **缺失** |
| — | `LOG_FILE` | **缺失** |
| — | `INGESTION_INTERVAL_SEC` | **缺失** |
| — | `GDELT_LASTUPDATE_URL` | **缺失**（当前硬编码在 gdelt_adapter 中） |
| — | `GDELT_MAX_RETRIES` | **缺失** |
| — | `GDELT_TIMEOUT_SEC` | **缺失** |
| — | `RSS_TIMEOUT_SEC` | **缺失** |
| — | `AKSHARE_REQUEST_INTERVAL_SEC` | **缺失** |
| — | `RISK_SUMMARY_CACHE_TTL_SEC` | **缺失** |

**结论:** 当前 `.env` 覆盖率仅 ~35%。N4 实施时必须补全。Pydantic Settings 的 `default` 值为所有非必需字段提供合理默认值。

---

## 4.2 测试策略

### 4.2.1 测试金字塔

```
          ┌─────────┐
          │  E2E    │  1-2 场景 (全链路: GDELT → Neo4j → API)
          │         │  N6-1
          └─────────┘
       ┌───────────────┐
       │  集成测试       │  Neo4j + 百炼 API 真实调用
       │                │  tests/integration/
       └───────────────┘
   ┌─────────────────────────┐
   │  单元测试                  │  纯 Python, mock 所有外部依赖
   │                          │  tests/unit/
   └─────────────────────────┘
```

### 4.2.2 单元测试（Mock 策略）

| 测试对象 | Mock 对象 | 验证内容 |
|---------|----------|---------|
| `adapters/base.py` | 子类的 `fetch()` 返回 mock 数据 | `dedup()` 正确去重、`run()` 流水线正确 |
| `adapters/gdelt_adapter.py` | `requests.get` → mock HTTP 响应 | CSV 解析正确、`normalize()` 字段映射正确、severity 计算正确 |
| `adapters/rss_adapter.py` | `requests.get` → mock RSS XML | RSS/Atom 解析正确 |
| `adapters/akshare_adapter.py` | `ak.stock_news_em()` → mock 返回 | 字段映射正确、时间解析正确 |
| `graphiti/episode_writer.py` | `graphiti.add_episode()` → mock | 去重逻辑正确、retry 逻辑正确、BatchWriteResult 汇总正确 |
| `graphiti/entity_types.py` | 无（纯 Pydantic） | schema 生成正确、字段验证正确 |
| `graphiti/relation_types.py` | 无（纯 Pydantic） | edge_type_map 正确 |
| `sync/ticker_sync.py` | `requests.get` → mock | 缓存写入/读取正确、降级逻辑正确 |
| `api/routers/events.py` | `EpisodeWriter` → mock | 响应格式符合 Design Doc §2.3~§2.7、错误码正确 |
| `api/routers/health.py` | Neo4j 连接 → mock | status 字段正确计算 |
| `core/config.py` | `.env` 文件（pytest monkeypatch） | 校验正确、默认值正确 |

### 4.2.3 集成测试（真实依赖）

| 测试场景 | 依赖 | 验证内容 |
|---------|------|---------|
| Neo4j 连接 | 本地 Neo4j Docker | `get_neo4j_driver()` 可连接 |
| Graphiti Episode 写入 | Neo4j + 百炼 API | 单条 Episode 写入成功、实体/关系可见 |
| GDELT 管线 | GDELT HTTP + Neo4j + 百炼 | 完整 fetch → normalize → write 链路 |
| API 端点 | 已填充的 Neo4j | `/api/events/active` 等 5 个端点返回正确格式 |
| Ticker 同步 | SynapseEngine (需先部署) | TickerSync 拉取成功 |

### 4.2.4 conftest.py 核心 fixture

```python
# 文件: tests/conftest.py
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.fixture
def mock_settings():
    """创建测试用 Settings 实例（假 API Key）。"""
    from src.core.config import Settings
    return Settings(
        bailian_api_key="sk-test",
        neo4j_password="test",
    )

@pytest.fixture
def mock_graphiti():
    """创建 mock Graphiti 实例。"""
    g = AsyncMock()
    g.add_episode = AsyncMock(return_value=MagicMock(nodes=[1,2,3], edges=[1,2]))
    return g

@pytest.fixture
def sample_normalized_episode():
    """创建测试用 NormalizedEpisode。"""
    from datetime import datetime, timezone
    from src.adapters.models import NormalizedEpisode
    ep = NormalizedEpisode(
        episode_body="测试新闻内容",
        name="gdelt_csv-20260609-001-abc123def456",
        source_description="GDELT GKG",
        source_type="gdelt_csv",
        valid_at=datetime(2026, 6, 9, 10, 0, 0, tzinfo=timezone.utc),
        content_hash="abc123def456",
    )
    return ep
```

### 4.2.5 测试覆盖率目标

| 模块 | 覆盖率目标 | 说明 |
|------|-----------|------|
| `core/` | ≥ 90% | config 校验、Neo4j 连接池是核心路径 |
| `adapters/` | ≥ 80% | 外部依赖多，通过 mock 覆盖 |
| `graphiti/` | ≥ 80% | EpisodeWriter 是核心写入链 |
| `api/` | ≥ 85% | 每个端点至少 happy path + 3 错误码测试 |
| `sync/` | ≥ 80% | TickerSync 的缓存降级逻辑 |
| `utils/` | ≥ 90% | 纯函数，高覆盖率 |
| `ingestion/` | ≥ 70% | 编排层，集成测试覆盖为主 |

---

# Part 5: sector_briefing 生成链路

（来源: Internal Spec §7，原文完整保留 — 含 Cypher 查询、LLM Prompt、Aggregator 完整代码、缓存策略、降级方案）

## 5.1 决策：直接改名（无向后兼容）

**老公决策 (2026-06-09):** 项目未上线，不存在向后兼容需求。`mirofish_seeds` → `sector_briefing` 直接改名，不使用双字段过渡。

**字段定义:**

| 属性 | 值 |
|------|-----|
| 字段路径 | `response.sector_briefing` |
| 类型 | `string \| null` |
| 必填 | 否 |
| 语义 | LLM 聚合的行业情报简报（Markdown 格式，300-500 字），供下游消费者直接使用 |
| 消费者 | MiroFish（当前唯一）、未来任何需要行业级情报摘要的系统 |

## 5.2 数据来源（从 Neo4j 取什么）

> **V2.1 更新 (2026-06-14):** Graphiti SDK 实际产生 `Episodic`/`Entity`/`RELATES_TO` label，不是 V2.0 假设的 `Sector`/`Stock`/`Event`/`AFFECTS`/`BELONGS_TO`。查询已改为实际物理 schema。

**查询路径:** 行业 Entity → 关联 Stock Entity（通过 sector 属性）→ RELATES_TO 边 → Episodic 节点

```cypher
// 对给定 sector_name，获取该行业所有活跃事件
// 物理 schema: Entity (行业/股票) + Episodic (事件) + RELATES_TO (关系边)
// entity_edges 是 Episodic 节点的数组属性，存储关联的 RELATES_TO 边 UUID

MATCH (sector_ent:Entity)
WHERE (sector_ent.name = $sector_name
       OR sector_ent.entity_name = $sector_name)
  AND 'Sector' IN sector_ent.labels
OPTIONAL MATCH (stock:Entity)
WHERE stock.ticker IS NOT NULL
  AND (stock.sector = sector_ent.name
       OR stock.sector = sector_ent.entity_name)
OPTIONAL MATCH (stock)-[rel:RELATES_TO]-(ep:Episodic)
WHERE rel.uuid IN ep.entity_edges
OPTIONAL MATCH (ep)-[other_rel:RELATES_TO]-(all_ents:Entity)
WHERE ep IS NOT NULL AND other_rel.uuid IN ep.entity_edges
RETURN
  ep.uuid AS event_id,
  ep.name AS title,
  ep.source AS source_description,
  ep.source_description AS summary,
  ep.created_at AS first_seen,
  ep.created_at AS last_updated,
  coalesce(ep.source_count, 1) AS source_count,
  ep.keywords AS keywords,
  'medium' AS severity,              // Graphiti EpisodicNode 无原生 severity，默认 medium
  collect(DISTINCT stock.ticker) AS affected_tickers,
  collect(DISTINCT stock.name) AS affected_stocks,
  collect(DISTINCT all_ents) AS entities
ORDER BY
  ep.created_at DESC
LIMIT 20  // 受 LLM context window 约束
```

> **注意:** Graphiti 的 `EpisodicNode` 没有原生 `severity` 字段。V2.1 默认使用 `'medium'` 填充。severity 的 LLM 富化待后续 Phase 单独设计实施（不在当前 scope 内）。`EpisodicNode.source_count` 仅在 multi-source 去重时设置，单源事件为 NULL 时默认 1。

**提取字段（每条事件）:**

| 字段 | Neo4j 来源 | 用途 |
|------|----------|------|
| `event_id` | `Episodic.uuid` | 唯一标识 |
| `title` | `Episodic.name` | 给 LLM 的标题输入 |
| `severity` | 默认 `'medium'` (LLM 富化待后续 Phase) | 控制摘要中事件的优先级排序和措辞 |
| `first_seen` / `last_updated` | `Episodic.created_at` | 时间上下文 |
| `source_count` | `Episodic.source_count` (默认 1) | 事件可信度指标 |
| `summary` | `Episodic.source_description` | 给 LLM 的正文输入 |
| `keywords` | `Episodic.keywords` | 帮助 LLM 理解事件核心主题 |
| `affected_tickers` / `affected_stocks` | `Entity.ticker` / `Entity.name` | 受影响标的列表 |
| `entities` | 关联的 Entity 节点（含 EventEntity） | 实体上下文（V2.3） |

**V2.3 新增：EventEntity 作为输入素材**

EventEntity 提供结构化事件信息，可显著增强行业简报的质量：

| EventEntity 字段 | 简报增强效果 |
|-----------------|-------------|
| `entity_name` | 事件描述直接入简报正文 |
| `actor1` / `actor2` | 明确事件参与者（"德国→乌克兰"） |
| `goldstein_scale` | 定量评分辅助 severity 判断（>0 = 合作，<0 = 冲突） |
| `tone` | 新闻语调评分辅助情绪判断 |
| `event_date` | 区分"事件发生时间"与"文章发布时间" |

**查询示例（含 EventEntity）:**

```cypher
// 查询行业相关的事件实体（V2.3 新增）
MATCH (sector_ent:Entity:Sector {entity_name: $sector_name})
OPTIONAL MATCH (sector_ent)-[:RELATES_TO]-(event_ent:Entity:Event)
WHERE event_ent.goldstein_scale < 0  // 仅冲突事件
RETURN
  event_ent.entity_name AS event_description,
  event_ent.actor1 AS actor1,
  event_ent.actor2 AS actor2,
  event_ent.goldstein_scale AS goldstein,
  event_ent.tone AS tone,
  event_ent.event_date AS event_date
ORDER BY event_ent.goldstein_scale ASC
LIMIT 10
```

## 5.3 LLM 配置与 Prompt 设计

**LLM 选型:** 阿里百炼 `qwen-plus`

| 配置项 | 值 | 说明 |
|--------|-----|------|
| 模型 | `qwen-plus` | 与 Graphiti 使用的 `qwen3.6-plus` 同系列，百炼 API 统一管理 |
| API Key | `BAILIAN_API_KEY`（复用 `.env`） | 与 Graphiti 共享同一 API Key |
| Base URL | `OPENAI_BASE_URL`（复用 `.env`） | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| max_tokens | 600 | 300-500 字输出，600 tokens 足够 |
| temperature | 0.3 | 低温度，控制输出稳定性和一致性 |

**System Prompt:**

```text
你是一个专业的金融情报分析师。你的任务是将一个行业（sector）内的多个离散新闻事件，
聚合为一份 300-500 字的中文 Markdown 行业情报简报。

要求：
1. 按 severity 降序排列事件（critical > high > medium > low）
2. 先给出"核心摘要"（2-3 句话概括该行业当前状态）
3. 然后逐条简述高风险事件（severity=high/critical），每条包含：事件标题、影响标的、潜在影响
4. 最后给出"行业展望"（1-2 句话总结趋势方向）
5. 语言简洁，适合量化交易系统的下游 Agent（MiroFish、PM Agent）直接消费
6. 不要在输出中包含"根据提供的数据"等元描述语
7. 输出纯 Markdown 文本，不要用 JSON 包裹
```

**User Prompt 模板:**

```text
## 行业: {sector_name}
## 统计: 总事件 {total_events} 个，涉及 {affected_ticker_count} 只标的

## 事件列表:

{events_text}

---
请基于以上事件数据，生成该行业的 300-500 字中文 Markdown 情报简报。
```

**`{events_text}` 格式化模板（每条事件）:**

```text
### [{severity_icon}] {title}
- 严重级别: {severity}
- 时间: {first_seen} ~ {last_updated}
- 信源数: {source_count}
- 摘要: {summary}
- 受影响标的: {affected_tickers}
- 关键词: {keywords}
```

其中 `severity_icon` 映射: `critical→🔴`, `high→🟠`, `medium→🟡`, `low→🟢`

## 5.4 聚合模块归属与完整实现

**文件:** `src/ingestion/briefing_aggregator.py`（已实现）  
**类:** `SectorBriefingAggregator`

> **V2.1 更新:** `_query_sector_events()` 查询 Graphiti 原生 schema（§5.2 更新后的 Cypher）。Neo4j record → 聚合 dict 的翻译复用 `src/graphiti/translation.py` 的共享函数，与 API 路由层的翻译逻辑保持一致。

```
ingestion/scheduler.py  ──每 15 分钟调用──▶  SectorBriefingAggregator.aggregate_all()
                                                  │
                                                  ├── _query_sector_events() → Neo4j (Cypher, Episodic/Entity/RELATES_TO)
                                                  ├── translation.py → Episodic/Entity record → 聚合 dict (共享翻译)
                                                  ├── _compute_fingerprint() → SHA256 变化检测
                                                  ├── _build_user_prompt()  → LLM Prompt 模板
                                                  └── _call_llm()           → 百炼 qwen-plus
                                                      │
                                                      ▼
                                            进程内存缓存 {sector → BriefingCacheEntry}
                                                      │
                            API 请求时 ──get_cached(name)──▶  O(1) 内存读取，< 1ms
```

```python
# 文件: src/ingestion/briefing_aggregator.py
# 职责: sector_briefing 的完整生成链路
#       Query Neo4j → LLM 聚合 → 写入内存缓存
# 调用方: ingestion/scheduler.py（每 15 分钟调度一次）
# 消费者: api/routers/events.py（GET /api/events/sector/:name → 从缓存读取）

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import logging

from openai import OpenAI
from neo4j import Driver

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个专业的金融情报分析师。你的任务是将一个行业（sector）内的多个离散新闻事件，
聚合为一份 300-500 字的中文 Markdown 行业情报简报。

要求：
1. 按 severity 降序排列事件（critical > high > medium > low）
2. 先给出"核心摘要"（2-3 句话概括该行业当前状态）
3. 然后逐条简述高风险事件（severity=high/critical），每条包含：事件标题、影响标的、潜在影响
4. 最后给出"行业展望"（1-2 句话总结趋势方向）
5. 语言简洁，适合量化交易系统的下游 Agent（MiroFish、PM Agent）直接消费
6. 不要在输出中包含"根据提供的数据"等元描述语
7. 输出纯 Markdown 文本，不要用 JSON 包裹"""

SEVERITY_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}


@dataclass
class BriefingCacheEntry:
    """单个 sector 的简报缓存"""
    briefing: str                     # LLM 输出的 Markdown 文本
    generated_at: datetime            # 生成时间
    event_fingerprint: str            # 用于变化检测的事件哈希


class SectorBriefingAggregator:
    """行业简报聚合器。

    生命周期:
    - 由 ingestion/scheduler.py 在进程启动时初始化
    - 每个 15 分钟轮询周期结束时调用 aggregate_all()
    - API 层通过 get_cached(sector_name) 读取缓存
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._cache: dict[str, BriefingCacheEntry] = {}
        self._llm_client = OpenAI(
            api_key=settings.bailian_api_key,
            base_url=settings.openai_base_url,
        )
        self._llm_model = "qwen-plus"

    def get_cached(self, sector_name: str) -> str | None:
        """获取 sector 的缓存简报。无缓存则返回 None。

        API 层直接调用此方法（O(1)），不触发 LLM 调用。
        """
        entry = self._cache.get(sector_name)
        return entry.briefing if entry else None

    async def aggregate_all(self, sector_names: list[str]) -> dict[str, str | None]:
        """对所有 sector 执行聚合（增量）。

        仅对事件指纹变化的 sector 重新生成简报。
        由 ingestion/scheduler.py 在每个轮询周期结束时调用。

        Returns:
            {sector_name: briefing_text | None}
        """
        results: dict[str, str | None] = {}
        for sector_name in sector_names:
            try:
                new_briefing = await self._aggregate_one(sector_name)
                results[sector_name] = new_briefing
            except Exception as exc:
                logger.error("sector_briefing 聚合失败 [%s]: %s", sector_name, exc)
                results[sector_name] = None
        return results

    async def _aggregate_one(self, sector_name: str) -> str | None:
        """对单个 sector 执行聚合。

        流程:
        1. 从 Neo4j 查询该 sector 的事件
        2. 计算事件指纹
        3. 若指纹未变，返回已有缓存
        4. 若指纹变化，调用 LLM 生成新简报
        5. 更新缓存
        """
        driver = get_neo4j_driver()

        # Step 1: 查询事件（§5.2 的 Cypher）
        events = await self._query_sector_events(driver, sector_name)
        if not events:
            logger.info("sector=%s 无活跃事件，跳过聚合", sector_name)
            return None

        # Step 2: 事件指纹变化检测
        fingerprint = self._compute_fingerprint(events)
        cached = self._cache.get(sector_name)
        if cached and cached.event_fingerprint == fingerprint:
            logger.debug("sector=%s 事件无变化，跳过 LLM 聚合", sector_name)
            return cached.briefing

        # Step 3: 调用 LLM 生成简报
        user_prompt = self._build_user_prompt(sector_name, events)
        briefing = await self._call_llm(user_prompt)

        # Step 4: 更新缓存
        self._cache[sector_name] = BriefingCacheEntry(
            briefing=briefing,
            generated_at=datetime.utcnow(),
            event_fingerprint=fingerprint,
        )
        logger.info("sector=%s 简报已更新 (%d 事件, %d 字)",
                     sector_name, len(events), len(briefing))
        return briefing

    async def _query_sector_events(self, driver: Driver, sector_name: str) -> list[dict]:
        """执行 Neo4j Cypher 查询（§5.2）。"""
        query = """
        MATCH (s:Sector {name: $sector_name})
              <-[:BELONGS_TO]-(stock:Stock)
              <-[:AFFECTS]-(event:Event)
        WHERE event.valid_at IS NOT NULL AND event.invalid_at IS NULL
        RETURN
          event.event_id AS event_id,
          event.title AS title,
          event.severity AS severity,
          event.first_seen AS first_seen,
          event.last_updated AS last_updated,
          event.source_count AS source_count,
          event.summary AS summary,
          event.keywords AS keywords,
          collect(DISTINCT stock.ticker) AS affected_tickers,
          collect(DISTINCT stock.name) AS affected_stocks
        ORDER BY
          CASE event.severity
            WHEN 'critical' THEN 4 WHEN 'high' THEN 3
            WHEN 'medium' THEN 2 WHEN 'low' THEN 1
          END DESC,
          event.last_updated DESC
        LIMIT 20
        """
        records, _, _ = driver.execute_query(query, {"sector_name": sector_name})
        return [dict(r) for r in records]

    def _compute_fingerprint(self, events: list[dict]) -> str:
        """计算事件指纹（用于增量检测）。

        指纹 = SHA256(event_id + last_updated 的排序拼接)
        任何事件新增/修改/删除 → 指纹变化 → 触发重新聚合
        """
        key = "|".join(
            f"{e['event_id']}:{e['last_updated']}"
            for e in sorted(events, key=lambda x: str(x.get('event_id', '')))
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _build_user_prompt(self, sector_name: str, events: list[dict]) -> str:
        """构建 LLM User Prompt（§5.3 的模板）。"""
        tickers_set: set[str] = set()
        for e in events:
            for t in e.get("affected_tickers", []):
                tickers_set.add(str(t))

        events_text_parts: list[str] = []
        for e in events:
            icon = SEVERITY_ICON.get(str(e.get("severity", "")), "⚪")
            tickers = ", ".join(str(t) for t in e.get("affected_tickers", []))
            keywords = ", ".join(str(k) for k in e.get("keywords", [])[:5])
            events_text_parts.append(
                f"### [{icon}] {e.get('title', '无标题')}\n"
                f"- 严重级别: {e.get('severity', 'unknown')}\n"
                f"- 时间: {e.get('first_seen', '?')} ~ {e.get('last_updated', '?')}\n"
                f"- 信源数: {e.get('source_count', 0)}\n"
                f"- 摘要: {e.get('summary', '无')}\n"
                f"- 受影响标的: {tickers}\n"
                f"- 关键词: {keywords}\n"
            )

        return (
            f"## 行业: {sector_name}\n"
            f"## 统计: 总事件 {len(events)} 个，涉及 {len(tickers_set)} 只标的\n\n"
            f"## 事件列表:\n\n"
            f"{chr(10).join(events_text_parts)}\n\n"
            f"---\n"
            f"请基于以上事件数据，生成该行业的 300-500 字中文 Markdown 情报简报。"
        )

    async def _call_llm(self, user_prompt: str) -> str:
        """调用百炼 qwen-plus 生成简报。"""
        response = self._llm_client.chat.completions.create(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("LLM 返回空内容")
        return content[:2000]  # 截断超长内容
```

## 5.5 缓存策略

| 维度 | 设计 |
|------|------|
| 存储介质 | 进程内存（Python dict） |
| 键 | sector 名称（中文，如 `"互联网平台"`） |
| 值 | `BriefingCacheEntry {briefing, generated_at, event_fingerprint}` |
| 更新时机 | 每 15 分钟（跟随 GDELT 轮询周期结束时调用 `aggregate_all()`） |
| 更新条件 | **增量更新**——仅当 `event_fingerprint` 变化时才调用 LLM |
| 冷启动 | 首次 `aggregate_all()` 对所有 sector 生成简报 |
| API 读取 | `get_cached()` → 直接返回内存值，< 1ms，不触发 LLM |
| 过期策略 | 无 TTL（事件驱动的增量更新；若 sector 事件全部过期，下一次聚合时会自然排除） |
| 内存占用 | ~20 sectors × ~2KB = ~40KB，可忽略 |

**增量触发条件:**
- ✅ sector 有新事件（event_id 不在指纹中）→ 指纹变化 → 重新聚合
- ✅ sector 事件被更新（last_updated 变化）→ 指纹变化 → 重新聚合
- ✅ sector 事件过期（invalid_at 置非 null，查询不再返回）→ 指纹变化 → 重新聚合
- ❌ sector 事件无变化 → 指纹相同 → 跳过 LLM 调用

## 5.6 降级方案

| 场景 | 行为 | 外部表现 |
|------|------|---------|
| **百炼 API 不可用** (429/5xx/超时) | `_call_llm()` 抛出异常 → `_aggregate_one()` 捕获 → 返回 None → 保留旧缓存（若有） | `sector_briefing` = null（若无旧缓存）或回退到旧缓存值 |
| **Neo4j 不可用** | `_query_sector_events()` 抛出异常 → 整个 `aggregate_all()` 跳过 | `sector_briefing` = null（上一次缓存仍可用） |
| **sector 无活跃事件** | `_query_sector_events()` 返回空列表 → 返回 None | `sector_briefing` = null |
| **LLM 返回空内容** | `_call_llm()` 结果为空字符串 → 抛出 RuntimeError → 返回 None | `sector_briefing` = null |
| **LLM 返回超长内容** | 截断到 2000 字符（post-processing） | 截断后的简报 |
| **首次启动，缓存全空** | `aggregate_all()` 对所有 sector 生成；若 LLM 不可用则全部为 None | API 返回不带 `sector_briefing` 的完整事件数据 |

**API 端降级行为（`api/routers/events.py`）:**

```python
# GET /api/events/sector/:name
briefing = aggregator.get_cached(sector_name)  # O(1) 内存读取

response = SectorEventsResponse(
    sector=sector_name,
    events=events,
    statistics=stats,
    sector_briefing=briefing,  # None 时 JSON 序列化为 null
)
```

**关键原则:** `sector_briefing` 缺失不阻断主流程。消费者应检查字段是否为 null，为 null 时降级为自行聚合原始事件列表。

## 5.7 API 响应模型（Pydantic）

```python
# 文件: src/api/models.py

from pydantic import BaseModel, Field


class SectorEventsResponse(BaseModel):
    """GET /api/events/sector/:name 响应模型"""

    sector: str = Field(..., description="行业名称")
    events: list[EventItem] = Field(default_factory=list)
    statistics: SectorStatistics

    sector_briefing: str | None = Field(
        default=None,
        description=(
            "LLM 聚合的行业情报简报（Markdown 格式，300-500 字）。"
            "由 SectorBriefingAggregator 每 15 分钟异步生成并缓存。"
            "为 null 时表示聚合未就绪或 LLM 不可用，消费者应降级处理。"
        ),
    )
```

## 5.8 生成链路总结

```
┌─────────────────────────────────────────────────────────────────────┐
│ sector_briefing 端到端生成链路                                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  [每 15 分钟 — ingestion/scheduler.py]                               │
│       │                                                              │
│       ▼                                                              │
│  ┌─────────────────────────────────────────┐                        │
│  │ SectorBriefingAggregator.aggregate_all() │                        │
│  │ 文件: src/ingestion/briefing_aggregator.py                       │
│  └────────────────┬────────────────────────┘                        │
│                   │                                                 │
│     ┌─────────────┼─────────────┐                                   │
│     ▼             ▼             ▼                                   │
│  ┌──────┐   ┌──────────┐   ┌──────────┐                            │
│  │Neo4j │   │ 事件指纹  │   │ 百炼LLM  │                            │
│  │Cypher│   │ 变化检测  │   │qwen-plus│                            │
│  │查询  │   │ (SHA256)  │   │ 聚合生成  │                            │
│  └──┬───┘   └────┬─────┘   └────┬─────┘                            │
│     │            │              │                                    │
│     │  top 20    │  指纹相同?    │  300-500 字                        │
│     │  events    │  → 跳过LLM   │  Markdown                          │
│     │            │              │                                    │
│     └────────────┼──────────────┘                                    │
│                  ▼                                                   │
│     ┌───────────────────────┐                                       │
│     │   内存缓存 (dict)      │                                       │
│     │   {sector → briefing} │                                       │
│     └───────────┬───────────┘                                       │
│                 │                                                    │
│  [API 请求时 — api/routers/events.py]                                │
│                 │                                                    │
│                 ▼                                                    │
│     ┌───────────────────────┐                                       │
│     │ GET /api/events/      │                                       │
│     │   sector/:name        │                                       │
│     │ → aggregator          │                                       │
│     │   .get_cached(name)   │  ← O(1) 内存读取                       │
│     └───────────────────────┘                                       │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

# Part 6: 部署要求

（来源: Redesign Doc §F，原文完整保留）

## 6.1 新增外部依赖

| 依赖 | 版本 | 部署方式 | 端口 | 说明 |
|------|------|---------|------|------|
| **Neo4j** | 5.x | Docker (WSL2) | 7687 (bolt), 7474 (browser) | Graphiti 后端存储 |
| **NewsEngine** | — | Python 进程 (Windows/WSL2) | 8100 | 情报引擎服务 |
| **graphiti-core** | 0.29.2 | pip | — | Python SDK |
| **gdeltPyR** | latest | pip | — | GDELT CSV 解析 |
| **Treasury API** | — | NewsEngine 内聚 (Phase 2+) | — | 美国国债收益率/利率决策，低频日级源，Phase 1 不接入 |

## 6.2 Neo4j Docker 部署（WSL2 侧）

```yaml
# D:\MyWallet\NewsEngine\docker-compose.yml
version: '3.8'
services:
  neo4j:
    image: neo4j:5-community
    container_name: newsengine-neo4j
    ports:
      - "7474:7474"   # HTTP (browser)
      - "7687:7687"   # Bolt (Graphiti)
    environment:
      - NEO4J_AUTH=neo4j/newsengine2026
      - NEO4J_server_memory_heap_initial__size=512m
      - NEO4J_server_memory_heap_max__size=2g
      - NEO4J_server_memory_pagecache_size=512m
    volumes:
      - ./data/neo4j:/data
      - ./data/logs:/logs
    restart: unless-stopped
```

```bash
# 一键启动
cd /mnt/d/MyWallet/NewsEngine
docker-compose up -d

# 验证
curl http://localhost:7474
```

## 6.3 端口规划

| 服务 | 端口 | 宿主机 | 容器内 | 冲突风险 |
|------|------|--------|--------|---------|
| MongoDB | 27017 | WSL2 | Docker | 无 |
| Neo4j Bolt | 7687 | WSL2 | Docker | 无 |
| Neo4j Browser | 7474 | WSL2 | Docker | 无 |
| NewsEngine API | 8100 | WSL2 | — | 无（不与 SynapseEngine 8000 冲突） |
| SynapseEngine UI | 8000 | WSL2 | — | 无 |
| SynapseEngine (其他) | — | — | — | 不变 |

## 6.4 内存预算（WSL2 总内存 16GB 假设）

| 进程 | 内存预算 | 说明 |
|------|---------|------|
| Neo4j JVM Heap | 2 GB | `NEO4J_server_memory_heap_max__size=2g` |
| Neo4j Page Cache | 512 MB | `NEO4J_server_memory_pagecache_size=512m` |
| Neo4j OS overhead | 500 MB | 容器开销 |
| NewsEngine Python | 1 GB | FastAPI + Graphiti + Embedding 推理 |
| MongoDB (Docker) | 2 GB | (现有) |
| SynapseEngine Python | 1 GB | (现有) |
| Kronos GPU 推理 | 2 GB | (现有，GPU 侧) |
| **合计** | **~9 GB** | 余量 ~7 GB |

## 6.5 启动顺序

```
1. docker-compose up -d          # MongoDB + Neo4j
2. python NewsEngine/main.py     # NewsEngine API (:8100), 监听 POST /api/tickers/whitelist
3. python SynapseEngine/...      # SynapseEngine 主进程 (:8000), 启动后 push ticker 白名单
```

**解耦说明:** NewsEngine 不再依赖 SynapseEngine 先启动，可独立部署。ticker 白名单通过 SynapseEngine push + 本地文件缓存兜底。若 SynapseEngine 未启动，NewsEngine 使用本地缓存文件 `data/ticker_whitelist.json` 兜底。

---

n

## 6.6 Episode TTL 分级淘汰策略（V2.2 新增）

### 6.6.1 设计决策

**应用层 TTL，非 Neo4j 原生 TTL。** Neo4j 社区版无文档级 TTL 支持，且 Graphiti SDK 内部索引依赖 Episodic UUID，不能依赖数据库层自动过期。

**两层保障：**
- **Layer 1（查询过滤）：** 所有面向消费者的 Cypher 查询加 `WHERE ep.created_at > datetime() - duration({days: N})`
- **Layer 2（定时清理）：** 每天 1 次 `DETACH DELETE` 删除过期节点

### 6.6.2 分级 TTL

| content_scope | TTL | 理由 |
|---------------|-----|------|
| `SYMBOL`（个股） | **3 天** | 个股消息已反映在股价里，3 天后无边际增量信息价值 |
| `SECTOR`（行业） | **7 天** | 行业趋势变化慢于个股，一周内的行业事件对 MiroFish 推演仍有参考价值 |
| `MACRO`（宏观） | **14 天** | 宏观信号（加息周期、贸易战、地缘冲突）是慢变量，两周前的数据对 PM Agent 决策仍有上下文价值 |

### 6.6.3 查询层 WHERE 子句（Layer 1）

所有面向消费者的 Cypher 查询必须加时间窗口限制：

```cypher
-- /api/events/active - 返回所有活跃事件（限制 7 天通用窗口）
MATCH (ep:Episodic)
WHERE ep.valid_at IS NOT NULL
  AND ep.created_at > datetime() - duration({days: 7})
RETURN ep ORDER BY ep.created_at DESC LIMIT 50

-- /api/events/entity/:ticker - 个股事件（3 天）
MATCH (stock:Entity {ticker: $ticker})-[rel:RELATES_TO]-(ep:Episodic)
WHERE rel.uuid IN ep.entity_edges
  AND ep.created_at > datetime() - duration({days: 3})
RETURN ep ORDER BY ep.created_at DESC

-- /api/events/sector/:name - 行业事件（7 天）
MATCH (sector:Entity)-[*2]-(ep:Episodic)
WHERE sector.name = $sector_name
  AND ep.created_at > datetime() - duration({days: 7})
RETURN ep ORDER BY ep.created_at DESC

-- /api/events/risk-summary - 宏观风险（14 天）
MATCH (ep:Episodic)
WHERE ep.episode_metadata CONTAINS 'MACRO'
  AND ep.created_at > datetime() - duration({days: 14})
RETURN ep ORDER BY ep.severity DESC LIMIT 20
```

### 6.6.4 定时清理作业（Layer 2）

**文件：** `src/ingestion/scheduler.py`

**执行频率：** 每天 1 次（默认 04:00 UTC / 12:00 HKT），在 ingestion cycle 之间执行。

```python
async def _ttl_cleanup(self) -> dict[str, int]:
    """分级 TTL 清理：DETACH DELETE 过期 Episodic 节点。

    每天执行 1 次，在 cycle 开始前检查 last_ttl_cleanup_date。
    """
    settings = get_settings()
    today = now_hkt().strftime("%Y-%m-%d")

    if self._last_ttl_cleanup_date == today:
        return {"skipped": 0}

    results: dict[str, int] = {}
    ttl_configs = [
        ("SYMBOL", settings.episode_ttl_symbol_days),
        ("SECTOR", settings.episode_ttl_sector_days),
        ("MACRO", settings.episode_ttl_macro_days),
    ]

    for scope, days in ttl_configs:
        query = """
        MATCH (ep:Episodic)
        WHERE ep.episode_metadata CONTAINS $scope
          AND ep.created_at < datetime() - duration({days: $days})
        DETACH DELETE ep
        RETURN count(ep) AS deleted
        """
        result = self._neo4j_driver.execute_query(
            query, {"scope": scope, "days": days}
        )
        deleted = result[0]["deleted"] if result else 0
        results[scope] = deleted
        if deleted > 0:
            logger.info(
                "TTL cleanup [%s]: deleted %d episodes (ttl=%dd)",
                scope, deleted, days,
            )

    # Optionally: clean orphan Entity nodes (no Episodic connected)
    orphan_query = """
    MATCH (e:Entity)
    WHERE NOT (e)-[:RELATES_TO]-()
    DELETE e
    RETURN count(e) AS deleted
    """
    orphan_result = self._neo4j_driver.execute_query(orphan_query)
    orphan_deleted = orphan_result[0]["deleted"] if orphan_result else 0
    if orphan_deleted > 0:
        results["orphan_entities"] = orphan_deleted
        logger.info("TTL cleanup: deleted %d orphan Entity nodes", orphan_deleted)

    self._last_ttl_cleanup_date = today
    return results
```

### 6.6.5 边界条件处理

| 场景 | 处理 |
|------|------|
| 7 天内无新 Episode | 不执行清理（所有 `deleted == 0`） |
| 清理中 ingestion 并发 | Neo4j 事务隔离，写入不受影响 |
| 旧 Episode 仍有活跃 RELATES_TO | `DETACH DELETE` 自动级联删除关系边 |
| Entity 变成孤儿 | 清理后执行 `MATCH (e:Entity) WHERE NOT (e)--() DELETE e` |
| 首次启动时 Neo4j 有大量旧数据 | 首次清理可能删除大量节点，之后稳态 |

### 6.6.6 预期效果

| 时间 | 活跃 Episode 存量 | 说明 |
|------|------------------|------|
| 首次运行后 | 0 | 全新 Neo4j |
| 第 1 天 | ~500-1200 | GDELT(200) + RSS(30) + AkShare(300-800) |
| 稳态（第 14 天后） | ~3K-6K | 前 14 天滚动窗口 |
| 每天变化 | ±200 | 日增量 ~500-1000，淘汰 ~500-1000，动态平衡 |


# Part 7: MongoDB Schema 变更

（来源: Redesign Doc §D，原文完整保留 — 含 DDL、索引、字段重命名、废弃说明）

> **本 Part 定义的是 SynapseEngine 侧 MongoDB 的变更，不在 NewsEngine 项目内实施。** 供 SynapseEngine 侧 Tech Lead 参考。

## 7.1 最终决策

**方案: 仅保留 `news_events` Collection。V1.6 完全移除 `sentiment_raw_data`。**

**决策理由 (2026-06-08 老公 + 灵汐 + Architect 共识):**
1. Neo4j (NewsEngine) 已经提供结构化事件（实体/关系/因果链/LLM 情感/severity），FinBERT 打分为冗余操作
2. 三个消费者均可直接消费 NewsEngine REST API 或 `news_events` Collection
3. MiroFish 种子材料由 NewsEngine `/api/events/sector/:name` 的 `sector_briefing` 字段提供
4. PM Agent `macro_context` 已改为 NewsEngine API (V1.5)
5. Sentiment Analyst 改为消费 `news_events` + NewsEngine API，质量更高
6. FinBERT (P3-C3) 废弃，替换为 NewsEngine LLM 打分，质量更好（中文原生 + 多粒度 + 因果上下文）

**架构简化:**
```
旧: GDELT/RSS/AkShare → NewsEngine → sentiment_raw_data (已废弃) → 消费者
                                   → news_events (MongoDB) → 消费者
                                   → Neo4j (完整知识图谱)

新: GDELT/RSS/AkShare → NewsEngine → Neo4j (完整知识图谱，权威)
                                   → news_events (MongoDB, 7天缓存) → 消费者
```

**风险缓解:**
| 风险 | 缓解 |
|------|------|
| NewsEngine 单点故障 | `news_events` 7 天 TTL 提供本地缓存。不可用时降级为缓存 + WARNING |
| 历史新闻追溯 | Neo4j Episode 全量保存 + 事实溯源。审计能力不降反升 |
| LLM 情感质量不如 FinBERT | Qwen3.6-plus 中文原生，FinBERT 英文为主。中文场景 LLM 胜出 |

## 7.2 news_events Collection（唯一新增）

```javascript
// 新增 Collection: news_events — NewsEngine 事件缓存
db.createCollection("news_events");

// 唯一索引: event_id
db.news_events.createIndex(
    { "event_id": 1 },
    { name: "idx_news_events_id_unique", unique: true }
);

// 按交易日查询
db.news_events.createIndex(
    { "trading_date": -1 },
    { name: "idx_news_events_date" }
);

// 按实体 ticker 查询
db.news_events.createIndex(
    { "entities.ticker": 1, "trading_date": -1 },
    { name: "idx_news_events_ticker_date", sparse: true }
);

// 按 severity 查询
db.news_events.createIndex(
    { "severity": 1, "trading_date": -1 },
    { name: "idx_news_events_severity_date" }
);

// 7 天 TTL 自动过期（事件历史不长留，完整数据在 Neo4j/NewsEngine 端）
db.news_events.createIndex(
    { "synced_at": 1 },
    { name: "idx_news_events_ttl", expireAfterSeconds: 604800 }
);
```

**字段定义:**

| 字段路径 | 类型 | 必填 | 说明 |
|---------|------|------|------|
| `_id` | ObjectId | 是 | MongoDB 自动生成 |
| `event_id` | string | 是 | NewsEngine 事件 ID `evt-YYYYMMDD-NNN` |
| `trading_date` | string | 是 | 交易日 `YYYY-MM-DD` |
| `title` | string | 是 | 事件标题 |
| `summary` | string | 否 | 事件摘要 |
| `severity` | string | 是 | 枚举: `low` / `medium` / `high` / `critical` |
| `first_seen` | string | 是 | 首次发现时间 ISO 8601 |
| `last_updated` | string | 是 | 最后更新时间 |
| `source_count` | int | 是 | 信源数 |
| `source_urls` | array | 否 | 来源链接 |
| `keywords` | array | 是 | 关键词 |
| `entities` | array | 是 | 关联实体 (同 API 格式) |
| `relations` | array | 否 | 事件间关系 |
| `synced_at` | ISODate | 是 | 同步时间 (TTL 索引键) |

## 7.3 跨 Collection 字段重命名

以下字段从 `crucix_*` 重命名为 `news_*`：

| Collection | 旧字段 | 新字段 | 说明 |
|-----------|--------|--------|------|
| `portfolio_state_history` | `slots[].crucix_sentiment_score` | `slots[].news_sentiment_score` | 个股事件情绪分 |
| `watchlist_history` | `candidates[].crucix_sentiment_score` | `candidates[].news_sentiment_score` | 候选股事件情绪分 |
| `feature_store` | `sentiment_features.crucix_sentiment_score` | `sentiment_features.news_sentiment_score` | 事件情绪分 |
| `feature_store` | `sentiment_features.crucix_news_count` | `sentiment_features.news_event_count` | 事件数 |
| `feature_store` | — (新增) | `sentiment_features.news_risk_level` | 风险等级 |

**存量兼容策略:**
- 写入端：统一使用新字段名
- 读取端：优先读取新字段，fallback 到旧字段（兼容存量数据）
- 示例代码: `sentiment_score = doc.get("news_sentiment_score") or doc.get("crucix_sentiment_score", 0.5)`

## 7.4 sentiment_raw_data 废弃说明

**决策 (2026-06-08 老公 + Architect):** `sentiment_raw_data` Collection 已删除。

**废弃原因:**
1. Neo4j (NewsEngine) 已提供完整的结构化事件情报（实体/关系/因果链/severity）
2. `news_events` Collection 提供 7 天 MongoDB 缓存，消费者可直接查询
3. FinBERT (P3-C3) 被 NewsEngine LLM 打分替代，质量更好
4. 三个消费者（MiroFish / Sentiment Analyst / PM Agent）均已切换为 NewsEngine API / news_events

**迁移路径:**
1. Phase 1: 仅写入 `news_events`
2. Phase 1: 旧 `sentiment_raw_data` Collection 的索引和 Collection 手动删除
3. Phase 1: 所有消费者从 `news_events` + NewsEngine API 读取

**不阻塞理由:** 消费者全部迁移完成，旧表可直接删除。

---

# Part 8: N4 实施与验收

（来源: Internal Spec §8~§10，原文完整保留）

## 8.1 N4 基础设施补完清单

### 8.1.1 必须在 N4-1 (FastAPI 骨架) 之前完成

| 文件 | 当前状态 | N4 前需完成 | 本文件参考章节 |
|------|---------|-----------|--------------|
| `src/core/config.py` | 0 字节 | 完整实现（Settings + get_settings() + 校验） | §4.1.2 |
| `src/core/neo4j_client.py` | 0 字节 | 完整实现（Driver 单例 + 生命周期） | 见下方 §8.1.2 |
| `src/utils/logging_config.py` | 0 字节 | 完整实现（结构化日志） | 常规 logging 配置 |
| `src/utils/time_utils.py` | 0 字节 | 完整实现（HKT 转换 + ISO 8601） | 常规时间工具 |
| `src/__init__.py` | 0 字节 | 添加 `__version__` | 项目元数据 |
| `src/core/__init__.py` | 0 字节 | 添加 re-export (`__all__`) | 模块导出 |
| `src/utils/__init__.py` | 0 字节 | 添加 re-export | 模块导出 |
| `src/graphiti/__init__.py` | 0 字节 | 添加 re-export | 模块导出 |
| `src/sync/__init__.py` | 0 字节 | 添加 re-export | 模块导出 |

### 8.1.2 Neo4jClient 生命周期规范

```python
# 文件: src/core/neo4j_client.py
"""Neo4j 驱动连接管理。

提供:
- get_neo4j_driver() → 全局单例 Driver
- 自动验证连通性
- 优雅关闭
"""

from __future__ import annotations

import logging

from neo4j import GraphDatabase, Driver

from src.core.config import get_settings

logger = logging.getLogger(__name__)

_driver: Driver | None = None


def get_neo4j_driver() -> Driver:
    """获取 Neo4j Driver 全局单例。

    首次调用时创建 Driver 并验证连通性。
    线程安全（neo4j Driver 自身就是线程安全的）。
    """
    global _driver
    if _driver is not None:
        return _driver

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        max_connection_lifetime=3600,
        max_connection_pool_size=50,
    )
    driver.verify_connectivity()
    logger.info("Neo4j 连接成功 (%s)", settings.neo4j_uri)
    _driver = driver
    return _driver


def close_neo4j_driver() -> None:
    """关闭 Neo4j Driver。应在进程 shutdown 时调用。"""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
        logger.info("Neo4j 连接已关闭")
```

### 8.1.3 责任判定

| 文件 | 应在 N1 完成？ | 实际状态 | 责任 |
|------|:---:|------|------|
| `config.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `neo4j_client.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `logging_config.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `time_utils.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `graphiti_client.py` | ❌ 否 (N4 阶段) | 不存在 | 新文件，在 §3.5 中定义 |

**Architect 的责任是定义 spec（本文件），Tech Lead 的责任是按 spec 实现。** N1 阶段 IMPLEMENT_PLAN 要求"完整目录树"，但只创建了空壳文件。本文件补定义后，Tech Lead 需在 N4 点火前补完。

---

## 8.2 N4 REST API 实施指南

### 8.2.1 FastAPI 应用工厂

```python
# 文件: src/api/server.py
"""FastAPI 应用工厂 — NewsEngine REST API (:8100)。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import get_settings
from src.api.routers.events import router as events_router
from src.api.routers.health import router as health_router


def create_app() -> FastAPI:
    """创建 FastAPI 应用（工厂模式，便于测试）。"""
    settings = get_settings()

    app = FastAPI(
        title="NewsEngine",
        version="1.0.0",
        description="AI-powered financial event intelligence engine",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — 允许 SynapseEngine 跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{settings.api_port}",
            "http://localhost:8000",   # SynapseEngine
            "http://localhost:3000",   # SynapseUI (Next.js)
        ],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(events_router)
    app.include_router(health_router)

    return app


app = create_app()
```

### 8.2.2 端点实现注意事项

| 端点 | 实现关注点 |
|------|-----------|
| `GET /api/events/active` | 参数: `limit`, `min_severity`, `sector`。Cypher 查询 Neo4j 按 severity + last_updated 排序。`freshness` 需查询各数据源的最后更新时间。 |
| `GET /api/events/entity/:ticker` | ticker 格式: `0700.HK`（不是 `HK.00700`）。`summary.news_sentiment_score` 由 severity 加权计算。 |
| `GET /api/events/sector/:name` | sector 名称使用中文。`sector_briefing` 从 `SectorBriefingAggregator.get_cached()` 读取（见 Part 5）。 |
| `GET /api/events/risk-summary` | 结果缓存 5 分钟。`overall_risk` 由各行业风险等级的加权计算得出。 |
| `GET /api/events/health` | 实时检查 Neo4j + 各数据源状态。状态判定: 任一数据源超过 30 分钟未更新 → degraded。Neo4j 不可达 → down。 |

### 8.2.3 端点 → Neo4j 查询映射

> **V2.1 更新 (2026-06-14):** 所有端点查询 Graphiti 原生 `Episodic`/`Entity`/`RELATES_TO` label，不是 V2.0 假设的 `Event`/`Stock`/`Sector`/`AFFECTS`/`BELONGS_TO`。业务模型字段通过 `translation.py` 共享翻译函数生成。

| 端点 | 数据来源 | 核心查询方法 |
|------|---------|------------|
| `/api/events/active` | Neo4j `Episodic` + `Entity` 节点 + `RELATES_TO` 边 | Cypher: `MATCH (e:Episodic) OPTIONAL MATCH ... RELATES_TO ... RETURN e, entities ORDER BY e.created_at DESC` |
| `/api/events/entity/:ticker` | Neo4j `Entity` (ticker 属性) + `Episodic` | 按 `Entity.ticker = $ticker` 查找→ `RELATES_TO` 反查 `Episodic` (通过 `entity_edges` 数组) |
| `/api/events/sector/:name` | Neo4j `Entity` (sector) + `Episodic` | 按 `Entity.sector = $sector_name` 查找 stock Entity → `RELATES_TO` → `Episodic` |
| `/api/events/risk-summary` | Neo4j 聚合 + LLM | 查询最近 `Episodic` → 按 Entity 分组 → `translation.py` 翻译 → LLM 生成 summary |
| `/api/events/health` | Neo4j 系统表 + 内部状态变量 | `CALL dbms.listConfig()` + 数据源最后更新时间变量 |

---

## 8.3 main.py 入口

```python
# 文件: main.py
"""NewsEngine 进程入口。

启动顺序:
1. 加载配置  2. 初始化日志  3. 连接 Neo4j
4. 启动 whitelist POST 端点  5. 启动数据摄取调度器  6. 启动 FastAPI
"""

from __future__ import annotations

import logging
import uvicorn

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver, close_neo4j_driver
from src.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """NewsEngine 主入口。"""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    logger.info("NewsEngine v%s 启动中...", "1.0.0")

    try:
        driver = get_neo4j_driver()
        logger.info("Neo4j 连接就绪")
    except Exception as exc:
        logger.critical("Neo4j 连接失败，进程退出: %s", exc)
        raise SystemExit(1)

    logger.info("启动 FastAPI 服务 (http://%s:%d)...", settings.api_host, settings.api_port)
    uvicorn.run(
        "src.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
```

> **注意:** TickSync 和数据摄取调度器的启动可放在 FastAPI 的 lifespan context manager 中（async startup event），与 API 共享同一个 asyncio event loop。具体实现由 Tech Lead 决策。

---

## 8.4 N4-L: 收尾修复与 V2.1 对齐

> **V2.1 新增 (2026-06-14).** N4-1~N4-10 已完成基础设施和骨架。N4-L 是最后一个 Phase，目标是修复运行时 Gap 并对齐 V2.1 新定义。所有任务应在一个窗口内完成（预估 2-3 天），无需分 phase。

### 8.4.1 背景

N4-1~N4-10 标记完成后，Architect 重新审计发现以下问题：

- **3 个功能 Gap**（Design Doc 定义了契约，代码只写了骨架/占位符）
- **2 个 V2.1 新增需求**（translation.py 共享翻译层，briefing_aggregator Cypher 更新）
- **8 处过时代码注释**（引用了不存在或已完成的 N4-9）

IMPLEMENT-PLAN 中 N4-1~N4-10 虽标 `[x] ✅`，但上述项目不应单独追踪 — 适合作为一个收尾任务包统一交付。

### 8.4.2 任务清单

#### L-1: 创建 `src/graphiti/translation.py`（共享翻译层）

**动机:** V2.1 设计决策 — 不在 Neo4j 建双 label，翻译逻辑集中在 graphiti 层。API 路由和简报聚合器复用同一套函数。

**文件:** `src/graphiti/translation.py`（新建）

**导出:**
```python
from src.graphiti.translation import (
    SEVERITY_WEIGHT,
    SEVERITY_DEFAULT,
    translate_episode_to_event,       # Episodic dict → EventItem (API 用)
    translate_entities_to_items,      # Entity dict list → list[EventEntityItem]
    translate_episode_to_briefing_input,  # Episodic dict → sector_briefing 聚合输入 dict
)
```

**实现要点:**
1. 从 `src/api/routers/events.py` 提取 `_episode_to_event_item()` 核心逻辑
2. 从 `src/api/routers/events.py` 提取 `_SEVERITY_WEIGHT` 常量
3. Graphiti `RELATES_TO` 边的 `name` 属性（`AFFECTS`/`BELONGS_TO`/`CAUSED_BY`）→ 转换为业务关系类型
4. `translate_episode_to_event()` 输入为 Neo4j `Record.data()` dict，输出为 `EventItem` 的构造函数参数 dict
5. 零依赖 `api/` 或 `ingestion/` 模块（仅依赖 `graphiti/entity_types.py` 的类型常量）

**类型判断逻辑:**
```python
# Entity label 检测（从 Neo4j labels 数组推断类型）
LABEL_TYPE_MAP = {
    "Sector": "sector",
    "Stock": "stock",
    "Country": "country",
    "Policy": "policy",
}

def _entity_type_from_labels(labels: list[str], ticker: str | None) -> str:
    """从 Neo4j Entity 节点的 labels 数组推断实体类型。"""
    label_set = {l.title() for l in labels}
    for label, entity_type in LABEL_TYPE_MAP.items():
        if label in label_set:
            return entity_type
    # Fallback: 有 ticker 则视为 stock
    if ticker:
        return "stock"
    return "unknown"
```

**验收标准:**
- [ ] `events.py` 中的 `_episode_to_event_item()` 改为 `from src.graphiti.translation import translate_episode_to_event`
- [ ] `events.py` 中的 `_SEVERITY_WEIGHT` 改为 `from src.graphiti.translation import SEVERITY_WEIGHT`
- [ ] 原 `_episode_to_event_item()` 函数体从 `events.py` 删除
- [ ] `briefing_aggregator.py` 的 `_query_sector_events()` 返回后调用 `translate_episode_to_briefing_input()`
- [ ] 运行 `python -c 'from src.graphiti.translation import *'` 无 import error

#### L-2: 更新 `briefing_aggregator.py` Cypher 查询

**动机:** V2.1 §5.2 将 Cypher 从假设的 `Sector`/`Stock`/`Event`/`AFFECTS`/`BELONGS_TO` 更新为实际 Graphiti schema `Entity`/`Episodic`/`RELATES_TO`。当前代码仍使用旧查询。

**文件:** `src/ingestion/briefing_aggregator.py`  
**方法:** `SectorBriefingAggregator._query_sector_events()`

**当前（错误的）查询:**
```cypher
MATCH (s:Sector {name: $sector_name})
      <-[:BELONGS_TO]-(stock:Stock)
      <-[:AFFECTS]-(event:Event)
```

**目标（见 Design Doc §5.2 完整 Cypher）:**
```cypher
MATCH (sector_ent:Entity)
WHERE (sector_ent.name = $sector_name
       OR sector_ent.entity_name = $sector_name)
  AND 'Sector' IN sector_ent.labels
OPTIONAL MATCH (stock:Entity)
WHERE stock.ticker IS NOT NULL
  AND (stock.sector = sector_ent.name
       OR stock.sector = sector_ent.entity_name)
OPTIONAL MATCH (stock)-[rel:RELATES_TO]-(ep:Episodic)
WHERE rel.uuid IN ep.entity_edges
...
```

**关键差异:**

| 项目 | 旧查询 | 新查询 |
|------|--------|--------|
| 行业定位 | `(s:Sector {name})` | `(sector_ent:Entity) WHERE name=$n AND 'Sector' IN labels` |
| 股票定位 | `(stock:Stock)` | `(stock:Entity) WHERE ticker IS NOT NULL` |
| 事件定位 | `(event:Event)` | `(ep:Episodic)` |
| 关系 | `:BELONGS_TO`, `:AFFECTS` | `[rel:RELATES_TO] WHERE rel.uuid IN ep.entity_edges` |
| severity | `event.severity` | `'medium'` 默认值（L-4 后 LLM 富化） |

**验收标准:**
- [ ] `_query_sector_events()` 使用新 Cypher 返回正确结果（至少 1 个 sector 有事件）
- [ ] 返回 dict 的字段名保持不变（`event_id`, `title`, `severity`, `affected_tickers`, `affected_stocks`） — 聚合器的下游代码不感知查询变更
- [ ] `_compute_fingerprint()` 正常工作（`event_id` 字段名不变）
- [ ] `_build_user_prompt()` 正常工作（格式化模板不变）

#### L-3: 更新过时代码注释

**文件 + 行号 + 旧注释 → 新注释:**

| 文件 | 旧注释 | 新注释 |
|------|--------|--------|
| `events.py:7` | `N4-5...（mock, 待 N4-9 LLM 聚合）` | `N4-5...（mock — LLM 聚合待 L-5 实现）` |
| `events.py:120` | `Proper severity will be available after N4-9` | `Severity defaults to medium; LLM enrichment deferred to L-4` |
| `events.py:497` | `SectorBriefingAggregator TBD in N4-9` | `SectorBriefingAggregator 提供缓存，见 src/ingestion/briefing_aggregator.py` |
| `events.py:563` | `# SectorBriefingAggregator TBD in N4-9` | `# briefing 由 SectorBriefingAggregator 在调度器 cycle 内异步更新` |
| `events.py:579,624,639,684` | `TODO: LLM 聚合逻辑待 N4-9 实现` | `TODO(L-5): LLM 聚合 → risk-summary 真实文本` |
| `health.py:7` | `N4-9 scheduler` | `L-6: ingestion scheduler health telemetry` |
| `health.py:136` | `will be filled by the N4-9 scheduler` | `will be filled by L-6: ingestion scheduler health telemetry` |
| `health.py:165` | `placeholder — 待 N4-9 调度器实现后填充` | `placeholder — 待 L-6 实现 ingestion scheduler health telemetry` |
| `health.py:187` | `TODO: data_sources 待 N4-9 调度器实现后填充真实状态` | `TODO(L-6): data_sources 待 ingestion scheduler health telemetry 实现后填充真实状态` |

**验收标准:**
- [ ] `grep -rn "N4-9" src/` 返回空（零残留）
- [ ] 所有 TODO 指向 L-4 / L-5 / L-6（可追踪）

#### L-4: LLM Severity 富化

**问题:** Graphiti `EpisodicNode` 没有原生 `severity` 字段。所有 API 响应默认返回 `severity="medium"`，导致 briefing_aggregator 和 risk-summary 无法区分事件严重性。

**设计:**
- 在 `episode_writer.py` 的 `write_one()` 成功后，异步调用 LLM 为 Episodic body 打分
- 或作为 ingestion scheduler 中每个 cycle 结束后的批处理任务

**实现位置:** `src/ingestion/scheduler.py` 的 `_run_cycle()` 中，在 pipeline 完成后、briefing aggregation 前

**LLM 调用:**
```python
SEVERITY_CLASSIFIER_PROMPT = """你是金融事件严重性分级器。
根据新闻内容，将事件严重性分为以下 4 级：

- critical: 系统性风险、市场崩盘、战争、重大监管打击
- high: 板块级负面事件、龙头股大跌、重大政策变化
- medium: 个股事件、行业动态、一般性政策调整
- low: 中性公告、常规经营动态、一般市场评论

仅输出一个单词: low / medium / high / critical
"""

async def enrich_severity_batch(
    neo4j_driver: Driver,
    llm_client: AsyncOpenAI,
    model: str = "qwen-plus",
) -> int:
    """批量富化 Episodic 节点的 severity。

    查询所有 severity 为 NULL 的 Episodic 节点，
    用 LLM 根据 body 内容打分，写入 Neo4j。
    """
```

**验收标准:**
- [ ] 新创建的 Episodic 节点在 15 分钟内获得 severity 评分
- [ ] API 响应 `severity` 字段不再固定为 `"medium"`
- [ ] briefing_aggregator 的 severity 排序生效（critical 事件排在前面）
- [ ] 非关键路径：LLM 失败时 severity 保持 `"medium"`，不阻断管道

**备选方案（更简单，推荐优先）:**

如果在 Phase 1 不想引入 LLM 调用，可以用**规则引擎**替代：

```python
def rule_based_severity(episode_body: str, source_count: int) -> str:
    """规则驱动的 severity 判定。

    规则（优先级从高到低）:
    1. source_count >= 5 → high
    2. 正文含 '暴跌/崩盘/停牌/退市/破产' → critical
    3. 正文含 '大跌/跌停/利空/罚款/调查/诉讼' → high
    4. 正文含 '利好/大涨/涨停/突破' → medium
    5. 正文含 '回购/增持/重组' → low
    6. 默认 → medium
    """
    CRITICAL_KEYWORDS = ["暴跌", "崩盘", "停牌", "退市", "破产", "熔断"]
    HIGH_KEYWORDS = ["大跌", "跌停", "利空", "罚款", "调查", "诉讼", "违约"]
    MEDIUM_KEYWORDS = ["利好", "大涨", "涨停", "突破", "新高"]
    LOW_KEYWORDS = ["回购", "增持", "重组", "分红"]

    if source_count >= 5:
        return "high"
    for kw in CRITICAL_KEYWORDS:
        if kw in episode_body:
            return "critical"
    for kw in HIGH_KEYWORDS:
        if kw in episode_body:
            return "high"
    for kw in MEDIUM_KEYWORDS:
        if kw in episode_body:
            return "medium"
    for kw in LOW_KEYWORDS:
        if kw in episode_body:
            return "low"
    return "medium"
```

> **Architect 建议:** 规则引擎方案优先。零 LLM 调用、零延迟、可预测。Phase 2 再考虑 LLM replacement。

#### L-5: Risk-summary LLM 聚合

**问题:** `GET /api/events/risk-summary` 返回 hardcoded mock 文本：
- `top_risks[i].potential_impact` 是模板拼接
- `summary` 是固定文本
- `sector_risk_levels` 在无事件时使用硬编码默认值

**目标:** 不改 JSON schema（契约不变），仅用 LLM 替换 mock 文本生成逻辑。

**实现位置:** `src/api/routers/events.py` → `get_risk_summary()`

**变更范围（仅替换字段生成逻辑）:**

| 字段 | 当前（mock） | 改为 |
|------|-------------|------|
| `summary` | 固定模板拼接 | LLM 根据 top_risks 和 sector_risk_levels 生成 2-3 句中文 summary |
| `top_risks[i].potential_impact` | `f"{title} 可能对 {sectors} 板块产生影响。"` | LLM 根据 title + entities 生成 1-2 句影响分析 |
| `sector_risk_levels` (无事件时) | `{"互联网平台":"LOW","新能源汽车":"LOW","消费":"LOW"}` | 返回空 dict `{}`，annotation 标注 `"no_active_events"` |

**LLM 调用模板:**
```python
RISK_SUMMARY_PROMPT = """你是一个金融风险分析师。基于以下活跃事件数据，生成：
1. 整体风险摘要（2-3 句，中文）
2. 每条 top risk 的潜在影响分析（1-2 句，中文）

事件数据：
{events_json}

行业风险分布：
{sector_risk_json}

输出格式（JSON）：
{{
  "summary": "整体风险摘要...",
  "potential_impacts": ["影响分析1", "影响分析2", ...]
}}
"""
```

**验收标准:**
- [ ] `summary` 字段内容为 LLM 生成的 2-3 句中文（非固定模板）
- [ ] `top_risks[i].potential_impact` 为 LLM 生成的 1-2 句中文（非固定模板）
- [ ] LLM 失败时降级为 mock 文本（已有逻辑，保留）
- [ ] 无事件时 `sector_risk_levels = {}`

#### L-6: Health endpoint — data_sources 真实数据接入

**问题:** `GET /api/events/health` 的 `data_sources` 返回空占位符。调度器已运行但未将数据源最后更新时间回传给 health 端点。

**实现:**

1. **调度器侧** — 在 `ingestion/pipeline.py` 的 `SourceHealth` dataclass 中已有的 `last_run_time` / `last_success_time` 字段暴露给全局 registry
2. **Health 端点侧** — `src/api/routers/health.py` 从 `ingestion/pipeline.get_health_registry()` 读取实际状态

**data_sources 字段映射:**

| health 响应字段 | 来源 |
|----------------|------|
| `data_sources.gdelt.status` | `health_registry["gdelt_csv"].consecutive_errors == 0 → ok, < 3 → degraded, >= 3 → down` |
| `data_sources.gdelt.last_update` | `health_registry["gdelt_csv"].last_success_time` |
| `data_sources.gdelt.latency_minutes` | `(now - last_success_time) / 60` |
| `data_sources.gdelt.error` | `None` (ok) / 最后一次异常信息 (degraded/down) |
| `data_sources.rss.*` | 同上，key=`"rss"` |
| `data_sources.akshare.*` | 同上，key=`"akshare"` |

**验收标准:**
- [ ] `GET /api/events/health` 返回真实的 `data_sources.gdelt.status`（非占位符）
- [ ] `data_sources.*.last_update` 为实际最后更新时间
- [ ] 调度器停止 30 分钟后，health 端点返回 `degraded`
- [ ] 不影响现有 health 检查的其他字段（`neo4j` / `graphiti` / `uptime_seconds`）

### 8.4.3 依赖关系

```
L-1 (translation.py) ── 无依赖，可最先做
    │
    ├──► L-2 (briefing Cypher) ── 依赖 L-1（复用翻译函数）
    │
    ├──► L-3 (过时注释) ── 无依赖，任意时间做
    │
    └──► L-4 (severity 富化) ── 依赖 L-1（写 severity 后翻译层读取）
             │
             └──► L-5 (risk-summary LLM) ── 依赖 L-4（有真实 severity 才值得 LLM 聚合）

L-6 (health data_sources) ── 无依赖，任意时间做
```

**推荐执行顺序:** L-1 → L-2 + L-3 + L-6 并行 → L-4 → L-5

### 8.4.4 N4-L 完工定义

- [ ] `src/graphiti/translation.py` 存在，`events.py` 和 `briefing_aggregator.py` 都 import 它
- [ ] `briefing_aggregator._query_sector_events()` 查询 Graphiti 原生 schema 并返回正确结果
- [ ] `grep -rn "N4-9" src/` 返回空
- [ ] API 响应 `severity` 字段不为固定 `"medium"`（规则引擎或 LLM 输出真实值）
- [ ] `GET /api/events/risk-summary` 返回 LLM 生成的 summary（非 mock 模板）
- [ ] `GET /api/events/health` 返回真实 `data_sources` 状态（非占位符）
- [ ] 全量集成测试：启动 NewsEngine → 等待 1 个 intake cycle → 5 个 API 端点均返回非 mock 数据

---

# Part 9: 闭环检查清单

（来源: Redesign Doc §G + Internal Spec §11，合并去重）

## 9.1 NewsEngine 自身完整性检查

- [x] 5 个 NewsEngine REST 端点完整契约（请求格式 + JSON Schema + 字段定义表 + 错误码表 + 代码示例）
- [x] 1 个 SynapseEngine → NewsEngine 端点完整契约（`POST /api/tickers/whitelist`，含请求体定义、响应格式、错误码、处理代码）
- [x] NewsEngine 文件架构完整定义（目录树 + 变更标记 + 模块职责矩阵 + V2.1 新增 `translation.py`）
- [x] 模块依赖图（ASCII 有向图 + 10 条铁律 + 共享类型契约 + translation.py 依赖链）
- [x] 生命周期管理（启动 8 步 FIFO + 关闭 4 步 LIFO + 依赖就绪检查 + 运行时并发模型）
- [x] 配置管理规范完整（20 个 .env 字段 + Pydantic Settings 完整实现 + 当前 .env 差异分析）
- [x] `sector_briefing` 完整生成链路（V2.1: Cypher 更新为 Graphiti 原生 Episodic/Entity/RELATES_TO schema → LLM Prompt 设计 → 完整 Aggregator 代码 → 缓存策略 7 维表 → 降级方案 6 场景表 → Pydantic 模型）
- [x] N4 基础设施补完清单（9 个文件 + neo4j_client.py 完整代码 + 责任判定表）
- [x] N4 REST API 实施指南（FastAPI 工厂 + 端点实现准则 + V2.1: Neo4j 查询映射更新为实际 schema）
- [x] main.py 入口完整代码定义
- [x] **V2.1 新增** §2.8 物理存储模型 vs 逻辑业务模型（schema 映射表 + 设计决策 + 数据流图 + 消费者依赖矩阵）
- [x] **V2.1 新增** `src/graphiti/translation.py` 共享翻译层设计
- [ ] **N4-L** Translation.py 创建（L-1）
- [ ] **N4-L** Briefing Cyber 更新为 Graphiti 原生 schema（L-2）
- [ ] **N4-L** 过时代码注释清理（L-3：8 处 N4-9 残留）
- [ ] **N4-L** LLM/Rule Severity 富化（L-4）
- [ ] **N4-L** Risk-summary LLM 聚合（L-5）
- [ ] **N4-L** Health data_sources 真实数据接入（L-6）
- [ ] **N4-L** 全量集成测试通过
- [ ] **V2.2** §1.4 宏观/个股双管线架构设计
- [ ] **V2.2** §2.2 ticker whitelist 职责限定（仅 AkShare）
- [ ] **V2.2** §2.9 content_scope 定义与标记策略
- [ ] **V2.2** §3.6.2 19 个核心宏观主题白名单定义
- [ ] **V2.2** §3.6.3 RSS 零过滤策略
- [ ] **V2.2** §3.6.5 content_scope 写入链路
- [ ] **V2.2** §4.1 配置项新增（EPISODE_TTL_*, GDELT_MACRO_THEMES_FILE）
- [ ] **V2.2** §6.6 TTL 分级淘汰策略（3/7/14 天）

## 9.2 SynapseEngine 侧影响检查

- [x] LLD §0.3 组件依赖图：Crucix → NewsEngine
- [x] LLD §1.2 数据流方向：Crucix 宏观情报 → NewsEngine 宏观事件
- [x] LLD §3 目录结构：consumer_adapters.py 注释 + 新增 clients/ 目录
- [x] LLD §4.2.1 portfolio_state_history：crucix_sentiment_score → news_sentiment_score
- [x] LLD §4.2.2 watchlist_history：crucix_sentiment_score → news_sentiment_score
- [x] LLD §4.2.3 feature_store：crucix_sentiment_score / crucix_news_count → news_* 字段
- [x] LLD §4.2.6 sentiment_raw_data：已删除，替换为 news_events Collection
- [x] LLD §6.0 架构图：Sentiment Analyst (Crucix数据) → (NewsEngine 事件数据)
- [x] LLD §6.1 macro_context：MongoDB 直读 → NewsEngine REST API
- [x] LLD §6.3 数据接入层：整节重写（crucix_adapter → news_engine_client）
- [x] LLD §7.3b news_ingestion_node.py：Crucix 调用 → NewsEngine REST API 调用
- [x] LLD §7.3c nlp_scoring_node.py：P3-C3 整个节点废弃
- [x] LLD §7.4 sentiment_analyst.py：crucix_sentiment_score → news_sentiment_score
- [x] LLD §7.6 dragon_catcher.py：DragonCandidate 字段重命名
- [x] LLD §7.x morning_rebalance.py：字段重命名
- [x] LLD §9 Cron 时序图：08:00 Crucix 宏观查询 → NewsEngine 宏观事件查询
- [x] LLD §9.3 时间点表：描述文本更新
- [x] LLD §10 异常处理矩阵：Crucix → NewsEngine + 新增异常行
- [x] LLD §11.1 .env：CRUCIX_URL → NEWSENGINE_BASE_URL
- [x] LLD §11.2 settings.yaml：crucix 配置段 → news_engine 配置段
- [x] IMPLEMENT_PLAN P2-3：Crucix 验证 → NewsEngine 情报管道验证
- [x] IMPLEMENT_PLAN P3-C2：新闻采集管线 → NewsEngine 客户端集成
- [x] IMPLEMENT_PLAN P3-C2.5（不再阻塞）：/api/portfolio/tickers GET 端点（运维查询）+ 启动时 push 白名单到 NewsEngine
- [x] IMPLEMENT_PLAN P3-C5/P3-D2：字段重命名
- [x] IMPLEMENT_PLAN models.py (P3-C0-3)：字段变更
- [x] MongoDB Schema 变更有 DDL 语句
- [x] 依赖部署方案完整（Neo4j Docker + 端口 + 内存预算）

## 9.3 不确定事项

| # | 事项 | 说明 |
|---|------|------|
| 1 | **RSS/TG 直连端口** | ✅ 已决策: 彻底废弃 Crucix，NewsEngine 自建 RSS 抓取 |
| 2 | **sector 命名对齐** | ✅ 已决策: Phase 1 统一中文名映射表 |
| 3 | **Event 写入时机** | ✅ 已决策: 15 分钟 Cron 轮询 |

---

# 变更记录

| 日期 | 变更内容 | 操作人 |
|------|----------|--------|
| 2026-06-08 | Redesign Doc V1.0: Crucix → NewsEngine 替代方案初始创建 | Chief Architect |
| 2026-06-08 | Redesign Doc V1.1: 灵汐 Review 修正（TODO 2 矛盾修正、Treasury API 补充、UI 组件替换清单、severity 映射修正、LLD 升级链标注） | Chief Architect |
| 2026-06-09 | Internal Spec V1.0: NewsEngine 内部架构规格说明书 | Chief Architect |
| 2026-06-09 | Redesign Doc V1.2: `mirofish_seeds` → `sector_briefing` 字段重命名（12 处同步修改） | Chief Architect |
| 2026-06-09 | Internal Spec V1.1: 新增 `sector_briefing` 完整生成链路（数据来源/LLM Prompt/模块归属/缓存策略/降级方案） | Chief Architect |
| 2026-06-09 | **V2.0 合并版**: Redesign Doc V1.2 + Internal Spec V1.1 合并为单一设计文档。NewsEngine 内部架构（文件树、依赖图、生命周期、配置、测试、sector_briefing 链路、N4 指南）纳入正文。SynapseEngine 侧变更移至附录 A/B 作为参考指引。原始文件保留不动。 | Chief Architect |
| 2026-06-14 | **V2.1 Graphiti Schema 对齐**: (1) 新增 §2.8 物理存储模型 vs 逻辑业务模型 (schema 映射表 + 设计决策 + 数据流图)。(2) §5.2 sector_briefing Cypher 从假设的 Event/Stock/Sector 更新为实际 Episodic/Entity/RELATES_TO。(3) 新增 `src/graphiti/translation.py` 共享翻译层 (§3.1/§3.2/§3.3)。(4) §8.2.3 端点→Neo4j 映射表修正为实际 label。(5) 依赖铁律 9→10 条。(6) MongoDB 同步链路澄清（REST API 响应层完成翻译）。(7) **新增 §8.4 N4-L 收尾任务** — 6 个任务修复 3 个 Gap + 实现 V2.1 新定义。 | Chief Architect |
| 2026-06-14 | **V2.2 宏观/个股管线拆分 + 数据质量修复**: (1) 新增 §1.4 宏观/个股双管线架构（问题发现 + 双管线图 + 过滤策略矩阵 + 设计决策）。(2) §2.2 ticker whitelist 职责限定为仅 AkShare。(3) 新增 §2.9 content_scope 定义与标记策略（3 种 scope + 写入时标记 + episode_metadata 透传）。(4) 新增 §3.6 数据接入层完整设计（GDELT 19 主题白名单 + RSS 零过滤 + 三层防御 + content_scope 写入链路）。(5) §3.1 新增 `macro_themes.py`。(6) §3.2 适配器模块职责更新。(7) §4.1 新增 TTL 和 GDELT 主题配置项。(8) 新增 §6.6 TTL 分级淘汰策略（3/7/14 天 + 两层保障 + 定时清理作业）。(9) 老公 + 灵汐 + Architect 三方审查确认。 | Chief Architect |

---

*NewsEngine 设计文档 V2.2 — 单一真相源（Single Source of Truth）。定义 NewsEngine 项目从架构到实施的全部设计规格。审批通过后作为 Tech Lead 的唯一实施蓝图。2026-06-14*

---

# 附录 A: SynapseEngine LLD 替换清单

> **本附录定义的变更不在 NewsEngine 项目内实施，供 SynapseEngine 侧 Tech Lead 参考。**

（来源: Redesign Doc §B，原文完整保留）

# 附录 A LLD 中所有 Crucix 引用的替换清单

以下按 LLD V1.4 章节顺序，逐处列出涉及 Crucix 的地方及替换方案。

## §0.3 核心组件依赖图

**原文:**
```
┌─────────────────┐
│  Crucix (宏观)   │
│  GDELT + 卫星    │
│  + 地缘冲突      │
└────────┬────────┘
```

**替换为:**
```
┌──────────────────┐
│  NewsEngine       │
│  (REST API :8100) │
│  GDELT CSV + RSS  │
│  + AkShare + Graph│
└────────┬─────────┘
```

**变更类型:** 修改 — 依赖图节点名称和描述

---

## §1.2 数据流方向

**原文:**
```
├──► [08:00] NewsEngine 宏观事件查询 ──► news_events (MACRO/SECTOR, 7d TTL)
```

**替换为:**
```
├──► [08:00] NewsEngine 宏观事件查询 ──► news_events (MACRO/SECTOR, 7d TTL)
```

**变更类型:** 修改 — 数据源名称 + 增加 news_events 作为中间存储

---

## §3 目录结构

**原文:**
```
│   │   └── consumer_adapters.py  # 数据源→内部格式转换（Crucix/AkShare 等适配器）
```

**替换为:**
```
│   │   └── consumer_adapters.py  # 数据源→内部格式转换（NewsEngine/AkShare 等适配器）
│   ├── clients/                  # ★ 新增: 外部服务客户端
│   │   ├── __init__.py
│   │   └── news_engine_client.py # NewsEngine REST API 客户端
```

**变更类型:** 修改注释 + 新增 `src/clients/` 目录

---

## §4.2.1 portfolio_state_history

**原文:**
```
| `slots[].crucix_sentiment_score` | number/null | 否 | 个股情绪分 (0~1) |
```

**替换为:**
```
| `slots[].news_sentiment_score` | number/null | 否 | 个股事件情绪分 (0~1)，来源: NewsEngine /api/events/entity/:ticker |
```

**变更类型:** 修改 — 字段重命名，来源变更

---

## §4.2.2 watchlist_history

**原文:**
```
| `candidates[].crucix_sentiment_score` | number | 是 | 情绪分 (0~1) |
```

**替换为:**
```
| `candidates[].news_sentiment_score` | number | 是 | 事件情绪分 (0~1)，来源: NewsEngine /api/events/entity/:ticker |
```

**变更类型:** 修改 — 字段重命名

---

## §4.2.3 feature_store

**原文:**
```
| `sentiment_features.crucix_sentiment_score` | number | 是 | Crucix 个股情绪分 |
| `sentiment_features.crucix_news_count` | int | 是 | 新闻条数 (V3.3) |
```

**替换为:**
```
| `sentiment_features.news_sentiment_score` | number | 是 | NewsEngine 事件情绪分 (0~1) |
| `sentiment_features.news_event_count` | int | 是 | 关联事件数 |
| `sentiment_features.news_risk_level` | string | 否 | NewsEngine 风险等级: LOW / MEDIUM / HIGH / CRITICAL |
```

**变更类型:** 修改 — 重命名 + 新增 `news_risk_level`

---

## §4.2.6 ~~sentiment_raw_data~~ → news_events

**原文:**
```
| `source` | string | 是 | 来源 (如 `Crucix_Aggregator`) |
```

**替换为:**
```
| `source` | string | 是 | 来源 (如 `NewsEngine` / `GDELT_CSV` / `AkShare` / `RSS`) |
```

**变更类型:** 修改 — 枚举值变更

---

## §6.0 架构概述（PM Agent 决策引擎架构图）

**原文:**
```
│   │ Sentiment    │
│   │ Analyst      │
│   │ (Crucix数据) │
```

**替换为:**
```
│   │ Sentiment    │
│   │ Analyst      │
│   │ (NewsEngine  │
│   │  事件数据)   │
```

**变更类型:** 修改 — 图上标注文本

---

## §6.1 PMEngineState 的 macro_context 字段

**原文 (LLD §6.1):**
```python
# StateMapper 注入 MACRO 级情绪数据
trading_date = state.get("_meta", {}).get("trading_date", "")
db = state.get("_meta", {}).get("db")
macro_news = db.news_events.find(
    {"content_scope": {"$in": ["MACRO", "SECTOR"]}, "trading_date": trading_date}
).sort("severity", 1).limit(10)

macro_context = {
    "events": [{"title": n.get("title", ""), "impact": n.get("severity", "medium")} for n in macro_news],
    "summary": f"{len(list(macro_news))} 条宏观事件"
}
```

**替换为:**
```python
# StateMapper 注入 MACRO 级事件数据（来源: NewsEngine REST API）
from src.clients.news_engine_client import NewsEngineClient

news_client = NewsEngineClient()

# 获取宏观风险摘要 + 行业事件
risk_summary = news_client.get_risk_summary()
sector_events = news_client.get_sector_events("all")

macro_context = {
    "source": "NewsEngine",
    "risk_level": risk_summary.get("overall_risk", "LOW"),
    "top_risks": risk_summary.get("top_risks", [])[:5],
    "sector_events": {
        sector: events[:3] for sector, events in sector_events.items()
    },
    "summary": risk_summary.get("summary", "无宏观风险事件")
}
```

**变更类型:** 修改 — 数据源从 MongoDB 直读 → NewsEngine REST API

---

## §6.3 数据接入层（整节重写）

**原文:** 完整描述 Crucix 外部依赖、Crucix 健康检查、crucix_adapter.py

**替换为以下内容:**

#### §6.3 数据接入层 (V1.0 重写)

**外部依赖:** NewsEngine 是独立 Python 进程（FastAPI，端口 8100），提供结构化事件情报 REST API。

**架构依赖图:**
```
┌──────────────────────┐     ┌──────────────────────┐
│   NewsEngine          │     │  AkShare (Python)    │
│   (REST API :8100)    │────▶│  consumer_adapters.py│
│   Graphiti + GDELT    │     │  (格式转换)           │
│   + RSS + AkShare     │     └──────────┬───────────┘
└──────────────────────┘                │
                           ▼           ▼
              ┌──────────────────────────────┐
              │  MongoDB news_events          │
              │  + news_events Collection     │
              └──────────────────────────────┘
```

**NewsEngine 健康检查:** 盘前 08:00 启动时必须验证 NewsEngine 存活且数据新鲜（`check_news_engine_health()`）。
不可用时降级为使用最近 7 天 `news_events` 缓存 + 标记 `WARNING_NEWSENGINE_UNAVAILABLE`。

PM Agent 决策引擎从 Chimera 数据生态（MongoDB + AkShare + NewsEngine + Kronos）直接读取，不依赖 ai-hedge-fund 的 `financialdatasets.ai`。

数据接入层按 **宏观管线** 和 **个股管线** 拆分，职责边界清晰：

```
┌──────────────────────────────────────────────────────────────────┐
│                     数据接入层 (Data Adapter Layer)                │
│                                                                   │
│                        ▼                                          │
│  ┌──────────────────────────────────────────┐                    │
│  │ nlp_scoring_node.py                      │                    │
│  │ (V1.6: LLM 端完成)                         │                    │
│  │                                          │                    │
│  │ (V1.6: 已废弃, NewsEngine LLM 替代)    │                    │
│  │ severity → sentiment_score                │                    │
│  │ (V1.6: severity 替代 nlp_analysis)       │                    │
│  └──────────────────┬───────────────────────┘                    │
│                     │                                             │
└─────────────────────┼─────────────────────────────────────────────┘
                      │
                      ▼
              PM Agent 消费层
```

| Chimera 数据需求 | 数据来源 | 接入方式 | 管线 |
|------------------|---------|---------|------|
| 实时行情 | uSmart WS | `broker_api.get_realtime_price()` | — |
| K 线数据 | MongoDB `market_kline_data` | `db.market_kline_data.find()` | — |
| 技术面特征 | MongoDB `feature_store.tech_features` | `db.feature_store.find_one()` | — |
| 宏观事件数据 | NewsEngine REST API | `news_client.get_risk_summary()` + `news_client.get_active_events()` | 宏观 |
| 个股事件数据 | NewsEngine REST API | `news_client.get_entity_events(ticker)` | 个股 |
| GPU 预测特征 | Kronos Predictor | `feature_store.kronos_features.bullish_prob_5d` | — |
| 持仓快照 | MongoDB `portfolio_state_history` | `db.portfolio_state_history.find_one()` | — |
| 候选池 | MongoDB `watchlist_history` | `db.watchlist_history.find_one()` | — |
| 补充 K 线 (盘前) | AkShare | `ak.stock_hk_daily()` | — |
| 行业推演 | MiroFish | 内存 `sector_trend dict` | 宏观 |

---

## §6.3 适配器: news_engine_client.py (替代原 crucix_adapter.py)

```python
# 文件: src/clients/news_engine_client.py
# 替代原 src/pipeline/crucix_adapter.py

import requests
from typing import Optional
from src.utils.logging_config import get_logger
from src.utils.time_utils import now_hkt
from src.core.config import get_config

logger = get_logger(__name__)


class NewsEngineClient:
    """NewsEngine REST API 客户端 — 替代原 Crucix HTTP 调用"""

    def __init__(self):
        config = get_config()
        self.base_url = config.news_engine_base_url  # 默认 http://localhost:8100
        self.timeout_sec = config.news_engine_timeout_sec  # 默认 30

    # ── 健康检查 ──

    def check_health(self) -> dict:
        """
        验证 NewsEngine 是否在运行且数据新鲜。

        Returns:
            dict: {
                "ok": bool,
                "gdelt_last_update": str | None,
                "rss_last_update": str | None,
                "akshare_last_update": str | None,
                "status": "healthy" | "stale" | "degraded"
            }
        """
        try:
            resp = requests.get(
                f"{self.base_url}/api/events/health",
                timeout=self.timeout_sec
            )
            if resp.status_code == 200:
                health = resp.json()
                # 判断新鲜度: 最近数据源更新在 30 分钟内
                return {
                    "ok": health.get("status") != "down",
                    "gdelt_last_update": health.get("gdelt_last_update"),
                    "rss_last_update": health.get("rss_last_update"),
                    "akshare_last_update": health.get("akshare_last_update"),
                    "status": health.get("status", "unknown")
                }
            return {"ok": False, "status": f"HTTP_{resp.status_code}"}
        except Exception as e:
            logger.warning(f"NewsEngine 健康检查失败: {e}")
            return {"ok": False, "status": str(e)}

    # ── 事件查询 ──

    def get_active_events(self, limit: int = 50) -> dict:
        """
        获取当前活跃事件列表。

        GET /api/events/active?limit=50

        Returns:
            dict: {"events": [...], "total": int, "freshness": {...}}
        """
        resp = requests.get(
            f"{self.base_url}/api/events/active",
            params={"limit": limit},
            timeout=self.timeout_sec
        )
        resp.raise_for_status()
        return resp.json()

    def get_entity_events(self, ticker: str) -> dict:
        """
        获取某股票关联的所有事件。

        GET /api/events/entity/:ticker

        Args:
            ticker: 股票代码 (如 "0700.HK" 或 "HK.00700")

        Returns:
            dict: {"ticker": str, "events": [...], "summary": {...}}
        """
        # NewsEngine 使用 "0700.HK" 格式
        news_ticker = ticker.replace("HK.", "") if ticker.startswith("HK.") else ticker
        resp = requests.get(
            f"{self.base_url}/api/events/entity/{news_ticker}",
            timeout=self.timeout_sec
        )
        resp.raise_for_status()
        return resp.json()

    def get_sector_events(self, sector_name: str) -> dict:
        """
        获取某行业的事件聚合。

        GET /api/events/sector/:name

        Args:
            sector_name: 行业名 (如 "互联网平台", "半导体")

        Returns:
            dict: {"sector": str, "events": [...]}
        """
        resp = requests.get(
            f"{self.base_url}/api/events/sector/{sector_name}",
            timeout=self.timeout_sec
        )
        resp.raise_for_status()
        return resp.json()

    def get_risk_summary(self) -> dict:
        """
        获取风险摘要。

        GET /api/events/risk-summary

        Returns:
            dict: {
                "overall_risk": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
                "top_risks": [...],
                "summary": str
            }
        """
        resp = requests.get(
            f"{self.base_url}/api/events/risk-summary",
            timeout=self.timeout_sec
        )
        resp.raise_for_status()
        return resp.json()

    # ── 便捷方法: 适配同步到 MongoDB ──

    def sync_events_to_cache(self, db) -> int:
        """
        从 NewsEngine 拉取活跃事件 → 写入 news_events。

        替代原 fetch_and_store_macro_sentiment()。

        Returns:
            int: 写入的文档数
        """
        try:
            active = self.get_active_events(limit=100)
        except Exception as e:
            logger.error(f"NewsEngine 事件同步失败: {e}")
            return 0

        count = 0
        today_str = now_hkt().strftime("%Y-%m-%d")

        for event in active.get("events", []):
            # 判断 content_scope
            entities = event.get("entities", [])
            entity_types = {e.get("type") for e in entities}

            if "country" in entity_types or "policy" in entity_types:
                content_scope = "MACRO"
            elif "sector" in entity_types:
                content_scope = "SECTOR"
            else:
                content_scope = "SYMBOL"

            doc = {
                "symbol": _derive_symbol(entities),
                "trading_date": today_str,
                "source": "NewsEngine",
                "content_scope": content_scope,
                "headline": event.get("title", ""),
                "summary": event.get("summary", event.get("title", "")),
                "url": event.get("source_urls", [None])[0] if event.get("source_urls") else None,
                "language": "zh",  # TODO: 从 NewsEngine 获取语言标记
                "raw_text": f"{event.get('title', '')}\n{event.get('summary', '')}",
                "source_url": event.get("source_urls", [None])[0] if event.get("source_urls") else None,
                "severity": event.get("severity", "medium"),
                "sentiment_score": _map_severity_to_score(event.get("severity", "medium")),
                "keywords": event.get("keywords", [])[:5],
                "created_at": now_hkt()
            }
            # 写入 news_events 缓存
            db.news_events.update_one(
                {"event_id": event["event_id"]},
                {"$set": {**event, "trading_date": today_str, "synced_at": now_hkt()}},
                upsert=True
            )
            count += 1

        return count


def _derive_symbol(entities: list) -> Optional[str]:
    """从实体列表提取首个股票 ticker"""
    for e in entities:
        if e.get("type") == "stock":
            return e.get("ticker")
    return None


def _map_severity_to_score(severity: str) -> float:
    """NewsEngine severity → 初始 sentiment_score（low/medium/high/critical → 0.50/0.40/0.20/0.10）。

    severity 仅表示事件影响程度，不表示正负面方向。
    正负面由 NewsEngine LLM 端完成（severity → score 映射）。
    此处提供保守初始值（偏中性偏负面），让 NLP 节点后续修正。
    """
    return {
        "low": 0.50,
        "medium": 0.40,
        "high": 0.20,
        "critical": 0.10
    }.get(severity, 0.5)


def check_news_engine_health() -> dict:
    """
    验证 NewsEngine 存活且数据新鲜（替代原 check_crucix_health）。

    Returns:
        dict: {"ok": bool, "status": str, ...}
    """
    client = NewsEngineClient()
    return client.check_health()
```

**变更类型:** 新增 — 替代原 `crucix_adapter.py`

---

## §7.3b news_ingestion_node.py

**原文:** 依赖 Crucix 外部进程，`check_crucix_health()`, `fetch_and_store_macro_sentiment()`

**替换为:**

```python
# 文件: src/pipeline/news_ingestion_node.py (V1.0 重写)
# 双管线新闻采集统一入口 — NewsEngine 版

from src.clients.news_engine_client import NewsEngineClient, check_news_engine_health
from src.utils.logging_config import get_logger

logger = get_logger(__name__)


def run_macro_ingestion(db) -> dict:
    """
    宏观管线: NewsEngine REST API → MongoDB

    替代原 Crucix 管线 (fetch_and_store_macro_sentiment)。
    """
    # 1. NewsEngine 健康检查
    health = check_news_engine_health()
    if not health["ok"]:
        logger.warning(f"NewsEngine 不可用 (status={health.get('status')}), 使用最近 24h 已有数据")
        return {"scope": "MACRO", "articles_fetched": 0, "status": "FALLBACK_CACHE"}

    # 2. 从 NewsEngine 同步事件到 news_events 缓存
    client = NewsEngineClient()
    count = client.sync_events_to_cache(db)

    # 3. 同时写入 news_events Collection（结构化事件）
    active = client.get_active_events(limit=100)
    today_str = now_hkt().strftime("%Y-%m-%d")
    for event in active.get("events", []):
        db.news_events.update_one(
            {"event_id": event["event_id"]},
            {"$set": {
                **event,
                "trading_date": today_str,
                "synced_at": now_hkt()
            }},
            upsert=True
        )

    return {"scope": "MACRO", "articles_fetched": count, "status": "OK"}


def run_stock_ingestion(top_n: int, db) -> dict:
    """
    个股管线: NewsEngine REST API → MongoDB

    决策: 采用方案 A — SynapseEngine 个股新闻统一走 NewsEngine /api/events/entity/:ticker。
    V1.6: 个股管线由宏观管线统一覆盖，不再独立运行。

    NewsEngine 内聚 GDELT + AkShare 个股新闻 + RSS，SynapseEngine 仅作为消费者。
    """
    # V1.6: 已废弃独立的个股管线，由 run_macro_ingestion() 统一覆盖
    return {"scope": "SYMBOL", "articles_fetched": 0, "status": "UNIFIED_WITH_MACRO"}
```

**变更类型:** 重写 — 宏观管线数据源从 Crucix → NewsEngine REST API

---

## §7.3c nlp_scoring_node.py

**原文:** 保持不变

**变更:** 整节点废弃。V1.6 起 FinBERT 不再使用，替换为 NewsEngine LLM 在实体提取时同步完成的情感打分（severity → 0~1 分数映射）。

**变更类型:** 删除 — P3-C3 整个节点废弃

---

## §7.4 sentiment_analyst.py

**原文:**
```python
position_data 必须包含:
    - crucix_sentiment_score (float)
```

**替换为:**
```python
position_data 必须包含:
    - news_sentiment_score (float)  # 来源: feature_store.sentiment_features.news_sentiment_score
```

**变更类型:** 修改 — 字段名

---

## §7.6 dragon_catcher.py

**原文:**
```python
class DragonCandidate(TypedDict):
    crucix_sentiment_score: float    # 初始情绪分 (0.5 默认)
```

**替换为:**
```python
class DragonCandidate(TypedDict):
    news_sentiment_score: float      # 初始事件情绪分 (0.5 默认)，来源: NewsEngine get_entity_events()
```

**变更类型:** 修改 — 字段名 + 注释

---

## §7.x morning_rebalance.py (LLD §7.x, 在 IMPLEMENT_PLAN P3-C5 中定义)

**原文 (IMPLEMENT_PLAN P3-C5):**
```
内存交汇: 将 sector_trend + crucix_sentiment_score 绑定到每个标的
防线触发: 持仓标的 crucix_sentiment_score < 0.20 → 强制覆写 sector_trend = "SECTOR_DECAY"
```

**替换为:**
```
内存交汇: 将 sector_trend + news_sentiment_score 绑定到每个标的
防线触发: 持仓标的 news_sentiment_score < 0.20 → 强制覆写 sector_trend = "SECTOR_DECAY"
```

**变更类型:** 修改 — 字段名

---

## §9 Cron 调度时序图

**原文:**
```
08:00 ───── NewsEngine 宏观事件查询 (REST API :8100) ► news_events (MACRO/SECTOR)
```

**替换为:**
```
08:00 ───── NewsEngine 宏观事件查询 (REST API :8100) ► news_events (MACRO/SECTOR, 7d TTL)
```

**ASCII 时序图中替换:**
```
原文: ├─ Crucix 宏观查询    │
替换: ├─ NewsEngine 宏观查询 │
```

**§9.3 时间点表替换:**
```
原文: BOD 继承 + Crucix 宏观查询 + AkShare 个股查询 + NLP 打分...
替换: BOD 继承 + NewsEngine 宏观事件查询 + AkShare 个股查询 + NLP 打分...
```

**变更类型:** 修改 — 时序图步骤名称

---

## §10 异常处理矩阵

**原文:**
```
| **Crucix (情报抓取)** | 抓取超时 > 3min | 停止抓取，使用最近 24h 数据 | 情绪分析降级为历史数据 | WARNING |
| **Crucix (情报抓取)** | 覆盖率 < 30% | 仅使用已有数据，不阻断 | 情绪分析置信度降低 | WARNING |
```

**替换为:**
```
| **NewsEngine (情报查询)** | 抓取超时 > 3min | 停止抓取，使用最近 24h 数据 | 情绪分析降级为历史数据 | WARNING |
| **NewsEngine (情报查询)** | 覆盖率 < 30% | 仅使用已有数据，不阻断 | 情绪分析置信度降低 | WARNING |
| **NewsEngine (情报查询)** | REST API 返回 5xx | 使用最近 24h 缓存 | 事件数据缺失，降级为纯技术分析 | CRITICAL |
| **NewsEngine (情报查询)** | Neo4j 宕机 | NewsEngine 返回 degraded 状态 | 仅返回已缓存事件，不阻断 | WARNING |
```

**变更类型:** 修改 — 组件名 + 新增异常行

---

## §11.1 .env 环境变量

**原文:**
```env
# === Crucix OSINT 聚合器 ===
CRUCIX_URL=http://localhost:3117
# 敏感: 否 | 默认: http://localhost:3117 | 必填: 否
# 说明: Crucix 是外部 Node.js 进程，通过 HTTP API 提供 29 源 OSINT 聚合数据
```

**替换为:**
```env
# === NewsEngine 情报引擎 ===
NEWSENGINE_BASE_URL=http://localhost:8100
# 敏感: 否 | 默认: http://localhost:8100 | 必填: 否
# 说明: NewsEngine 是独立 Python 进程（FastAPI），提供 Graphiti 驱动的结构化事件情报 REST API
```

**变更类型:** 修改 — 变量名 + URL + 说明

---

## §11.2 settings.yaml

**原文:**
```yaml
# --- Crucix OSINT 聚合器 (外部依赖) ---
crucix:
  base_url: "http://localhost:3117"
  timeout_sec: 30
  sweep_interval_min: 15
  fallback: "use_cached_24h"
```

**替换为:**
```yaml
# --- NewsEngine 情报引擎 (外部依赖) ---
news_engine:
  base_url: "http://localhost:8100"
  timeout_sec: 30
  max_retries: 3
  fallback: "use_cached_24h"
```

**变更类型:** 修改 — 配置段名和字段

---

## §12 UI API 设计 (LLD)

> **§12 无 Crucix 引用，无需变更。** 但 SynapseUI 前端组件中涉及 `crucix_*` 字段的显示需同步变更为 `news_*` 字段。以下是完整的 UI 组件替换清单：

#### UI 组件 crucix → news 字段替换清单

| 组件文件 (.tsx) | 旧字段引用 | 新字段引用 | 说明 |
|-----------------|-----------|-----------|------|
| `PositionCard.tsx` | `slot.crucix_sentiment_score` | `slot.news_sentiment_score` | 持仓卡片中的个股事件情绪分展示 |
| `MiroFishSector.tsx` | `sentiment_features.crucix_sentiment_score` | `sentiment_features.news_sentiment_score` | MiroFish 行业推演的 Crucix 情绪列 |
| `MiroFishSector.tsx` | `sentiment_features.crucix_news_count` | `sentiment_features.news_event_count` | 事件数展示 |
| `KronosCard.tsx` | `sentiment_features.crucix_sentiment_score` | `sentiment_features.news_sentiment_score` | Kronos 卡片中的情绪关联指标 |
| `DashboardPage.tsx` | `crucix_sentiment_score` 关键词匹配 | `news_sentiment_score` 关键词匹配 | 仪表盘中任何 sentiment_score 引用 |
| `DecisionCard.tsx` | PM Agent context 中 `crucix_*` 字段 | `news_*` 字段 | 决策卡片展示上下文 |
| `AnalystCard.tsx` | Sentiment Analyst 输出中 `crucix_sentiment_score` | `news_sentiment_score` | Analyst 信号展示 |
| `stores/agentStore.ts` | `crucix_sentiment_score` / `crucix_news_count` | `news_sentiment_score` / `news_event_count` | Zustand 状态管理中的字段路径 |
| `stores/dashboardStore.ts` | `crucix_sentiment_score` / `crucix_news_count` | `news_sentiment_score` / `news_event_count` | Dashboard 状态 |
| `api/agent.ts` | API 响应中 `crucix_*` | `news_*` | 后端 API 响应字段映射 |
| `api/dashboard.ts` | API 响应中 `crucix_*` | `news_*` | Dashboard API 字段映射 |

> **实施策略:** 建议一次性全局搜索 `crucix_` 替换为 `news_`，外加保留一个 fallback 读取函数（优先读新字段，找不到再读旧字段），避免逐文件手改遗漏。

---


---

# 附录 B: SynapseEngine IMPLEMENT_PLAN 变更影响

> **本附录定义的变更不在 NewsEngine 项目内实施，供 SynapseEngine 侧 Tech Lead 参考。**

（来源: Redesign Doc §E，原文完整保留）

# 附录 B IMPLEMENT_PLAN 变更影响

## E.1 P2-3 Crucix 宏观冲击感知验证 → 替换为 NewsEngine 验证

| 原任务 | 新任务 |
|--------|--------|
| **P2-3** Crucix 宏观冲击感知验证 | **P2-3** NewsEngine 情报管道验证 |

**新验证内容:**

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 |
|---|------|------|--------|------|--------|
| **P2-3a** | Neo4j Docker 部署 + Graphiti 接入 | [ ] | Tech Lead | 1 天 | Neo4j 运行 + Graphiti SDK 连通性 |
| **P2-3b** | GDELT CSV 下载链路验证 (HTTP) | [ ] | Tech Lead | 0.5 天 | GKG CSV 下载 + 解压 + 过滤脚本 |
| **P2-3c** | RSS/TG 直连验证 (原 Crucix 暴露端口) | [ ] | Tech Lead | 0.5 天 | RSS feed 拉取验证 |
| **P2-3d** | NewsEngine REST API 端到端测试 | [ ] | Tech Lead | 1 天 | 全事件生命周期走通 |

**通过标准:** GDELT CSV 下载成功率 > 95% + NewsEngine 活跃事件覆盖率 >= 80%
**失败应对:** 任一失败 → GDELT CSV 降级为仅 HTTP 文件路径；NewsEngine 不可用 → 降级为 AkShare-only 情绪分析

---

## E.2 P3-C2 新闻采集管线 → 重写为 NewsEngine 客户端集成

| 原任务 | 新任务 |
|--------|--------|
| **P3-C2** 双管线新闻采集（宏观 + 个股） | **P3-C2** NewsEngine 客户端集成 + 事件同步 |

**原 P3-C2 内容（废除）:**
- ~~Crucix 适配器: 调用 Crucix `/api/data` HTTP API~~
- ~~`check_crucix_health()`~~
- ~~`settings.yaml` 新增 `crucix` 配置段~~
- ~~`.env.example` 新增 `CRUCIX_URL`~~
- ~~宏观管线: `run_macro_ingestion(db)` → Crucix~~

**新 P3-C2 内容:**

| # | 子任务 | 产出物 |
|---|--------|--------|
| P3-C2-1 | `src/clients/news_engine_client.py` — NewsEngine REST API 客户端 | 替代原 `crucix_adapter.py` |
| P3-C2-2 | `check_news_engine_health()` + `sync_events_to_cache()` | 宏观事件同步 |
| P3-C2-3 | `settings.yaml` 新增 `news_engine` 配置段 | 配置变更 |
| P3-C2-4 | `.env.example` 新增 `NEWSENGINE_BASE_URL` | 环境变量 |
| P3-C2-5 | MongoDB 新增 `news_events` Collection + 4 索引 | Schema 变更 |
| P3-C2-6 | `consumer_adapters.py` 更新注释 + 移除 Crucix 引用 | 清理 |
| P3-C2-7 | 重写 `news_ingestion_node.py` 宏观管线（NewsEngine REST → MongoDB） | 核心管线 |

---

## E.3 新增 P3-C2.5: SynapseEngine `/api/portfolio/tickers` 端点实现

> **新增任务**: NewsEngine 依赖此端点获取 ticker 白名单。

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 |
|---|------|------|--------|------|--------|
| **P3-C2.5** | `/api/portfolio/tickers` 端点实现 | [ ] | Tech Lead | 0.5 天 | 新增 REST 端点 |

**实现内容:**
- [ ] 在 `src/dispatcher/ui_server.py`（FastAPI）新增路由:
  ```python
  @app.get("/api/portfolio/tickers")
  async def get_portfolio_tickers():
      """返回持仓 + 自选股的完整 ticker 列表，供 NewsEngine 白名单过滤"""
  ```
- [ ] 数据来源: `portfolio_state_history` 最新 BOD + `watchlist_history` 最新快照
- [ ] 返回格式: 见 §C.2
- [ ] 不含任何敏感信息（仅返回 symbol/name/sector）
- [ ] 测试: `curl http://localhost:8000/api/portfolio/tickers` 返回 JSON

---

## E.4 P3-C5 双池洗牌（字段重命名）

**P3-C5 原有描述:**
```
内存交汇: 将 sector_trend + crucix_sentiment_score 绑定到每个标的
防线触发: 持仓标的 crucix_sentiment_score < 0.20 → 强制覆写 sector_trend = "SECTOR_DECAY"
```

**变更为:**
```
内存交汇: 将 sector_trend + news_sentiment_score 绑定到每个标的
防线触发: 持仓标的 news_sentiment_score < 0.20 → 强制覆写 sector_trend = "SECTOR_DECAY"
```

> news_sentiment_score 来源: `feature_store.sentiment_features.news_sentiment_score`，由 P3-C2 NewsEngine 同步管线计算注入。

---

## E.5 P3-D2 早盘危机逃生（字段重命名）

**P3-D2 原有描述:**
```
触发条件: 持仓标的进入"瘟疫状态"（crucix_sentiment_score < 0.20 或 sector_trend == "SECTOR_DECAY"）
```

**变更为:**
```
触发条件: 持仓标的进入"瘟疫状态"（news_sentiment_score < 0.20 或 sector_trend == "SECTOR_DECAY"）
```

---

## E.6 P3-C3 FinBERT 情感打分 → 废弃

**原 P3-C3 (废除):**
- ~~nlp_scoring_node.py — FinBERT 情感分析~~
- ~~对 MongoDB 中未打分的新闻批量打分 (V1.6 废弃)~~

**替换方案:** NewsEngine LLM (Qwen3.6-plus) 在实体/关系提取时同步完成情感打分。输出的结构化事件已包含 `severity` 字段（low/medium/high/critical），映射关系见 `_map_severity_to_score()`。

**质量对比:**
| 维度 | FinBERT (废弃) | NewsEngine LLM (新) |
|------|---------------|---------------------|
| 中文支持 | 弱（英文语料训练） | 强（原生中文） |
| 情感粒度 | 3 元 (pos/neg/neutral) | 4 级 severity + 因果上下文 |
| 成本 | 本地 GPU | API 调用（已分摊到实体提取） |

---

## E.7 P3-C4 MiroFish 行业推演（输入源变更）

**原有:**
```
- 输入: NewsEngine `/api/events/sector/:name` 的 `sector_briefing` 字段
```

**变更为:**
```
- 输入: 直接使用 NewsEngine /api/events/sector/:name 的 sector_briefing 字段（300-500 字 Markdown 种子材料）
- 降级: NewsEngine 不可用时 → 从 news_events Collection 按行业聚合（保留本地降级能力）
```

---

## E.8 P4-2 Webhook 告警（组件名变更）

**原有:**
```
- Crucix 超时降级
```

**变更为:**
```
- NewsEngine 超时降级
- NewsEngine Neo4j 宕机降级
```

---

## E.7 models.py 字段变更（IMPLEMENT_PLAN P3-C0-3）

| 模型 | 旧字段 | 新字段 |
|------|--------|--------|
| `SlotState` | `crucix_sentiment_score` | `news_sentiment_score` |
| `WatchlistCandidate` | `crucix_sentiment_score` | `news_sentiment_score` |
| `SentimentFeatures` | `crucix_sentiment_score` | `news_sentiment_score` |
| `SentimentFeatures` | `crucix_news_count` | `news_event_count` |
| `SentimentFeatures` | — | `news_risk_level` (新增) |

---

