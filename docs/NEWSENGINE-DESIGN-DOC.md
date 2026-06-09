# NewsEngine 设计文档

**版本:** V2.0（合并 Redesign Doc V1.2 + Internal Spec V1.1）  
**日期:** 2026-06-09  
**作者:** Chief Architect  
**依据文档:**
- `NewsEngine Proposal V1.0` (2026-06-08)
- `SynapseEngine LOW_LEVEL_DESIGN.md` V1.6

**状态:** Draft → 待审批  
**审批人:** 老公  
**适用项目:** `D:\MyWallet\NewsEngine`  
**关联文档:** `NEWSENGINE-IMPLEMENT-PLAN.md`（实施计划，位于 `D:\MyWallet\SynapseEngine\docs\` 软链接）

---

## 版本说明

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
| Part 8 | N4 实施与验收（补完清单 + API 指南 + main.py） | Internal Spec §8~§10 | NewsEngine 实现 |
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
│  │  • 实体提取: Stock / Sector / Country / Policy            │       │
│  │  • 关系提取: AFFECTS / CAUSED_BY / MITIGATES              │       │
│  │  • 时间窗口: valid_at / invalid_at（事实生命周期）          │       │
│  │  • 增量写入: 新 Episode 自动关联已有实体                    │       │
│  │  • 混合检索: 语义 + BM25 + 图遍历                         │       │
│  │  • 事件溯源: 每个事实可追溯到原始 Episode                  │       │
│  │  后端: Neo4j (Docker, WSL2)                                │       │
│  │  Embedding: 阿里百炼 text-embedding-v3                     │       │
│  │  LLM 提取: Qwen3.6-plus (百炼)                             │       │
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
| `tickers[].biz_code` | string | 是 | 纯数字代码（供 GDELT/AkShare 使用） |
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
    """获取当前白名单（供 GDELT 过滤器使用）。
    
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
        {"type": "policy", "name": "反垄断调查", "status": "rumor"}
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
| `events[].entities[].type` | string | 是 | 实体类型: `stock` / `sector` / `country` / `policy` |
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
        {"type": "stock", "ticker": "9988.HK", "name": "阿里巴巴-W"}
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
│   │   ├── rss_adapter.py        # RSS 抓取适配器【已实现】
│   │   ├── akshare_adapter.py    # AkShare 个股新闻适配器【已实现】
│   │   └── treasury_adapter.py   # Treasury API 适配器【已实现】
│   │
│   ├── graphiti/                 # Graphiti 知识图集成层【N3 ✅ 已完成】
│   │   ├── __init__.py
│   │   ├── entity_types.py       # 实体类型 Pydantic 定义【已实现】
│   │   ├── relation_types.py     # 关系类型 + edge_type_map【已实现】
│   │   └── episode_writer.py     # EpisodeWriter (去重 + 写入)【已实现】
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
| `adapters/` | 原始数据 → `NormalizedEpisode` 转换 | `core/config.py`, `adapters/models.py` | `BaseAdapter` 子类 |
| `graphiti/` | `NormalizedEpisode` → Neo4j 知识图写入 | `graphiti-core`, `adapters/models.py`, `core/neo4j_client.py` | `EpisodeWriter` |
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
   │routers ││entity  ││graphiti│   │/       ││/episode││ticker  │
   │        ││/relation││_client │   │        ││_writer ││_sync   │
   └───┬────┘└────────┘└───┬────┘   └───┬────┘└───┬────┘└───┬────┘
       │                    │            │         │          │
       └────────────────────┼────────────┘         │          │
                            ▼                      │          │
                    ┌──────────────┐               │          │
                    │ core/        │               │          │
                    │ neo4j_client │◄──────────────┘          │
                    └──────┬───────┘                          │
                           │                                  │
                    ┌──────▼───────┐                          │
                    │ core/config  │◄─────────────────────────┘
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

**依赖铁律（9 条，违反即 BUG）:**

1. **`core/config.py`** — 零业务依赖，仅依赖 `python-dotenv` + `pydantic-settings`。所有模块通过依赖注入获取配置，不直接 import。
2. **`core/neo4j_client.py`** — 仅依赖 `core/config.py` + `neo4j` 驱动。不依赖任何适配器或 graphiti 模块。
3. **`core/graphiti_client.py`** — 依赖 `core/config.py` + `core/neo4j_client.py` + `graphiti/` 类型定义。封装 Graphiti SDK 实例化。
4. **`adapters/`** — 仅依赖 `adapters/models.py` + `core/config.py`（通过依赖注入）。不依赖 `graphiti/` 或 `api/`。
5. **`graphiti/`** — 依赖 `graphiti-core` + `adapters/models.py`（NormalizedEpisode）。不依赖任何具体适配器。
6. **`ingestion/`** — 编排层，依赖 `adapters/` + `graphiti/` + `sync/` + `core/`。不依赖 `api/`。
7. **`api/`** — 依赖 `core/` + `graphiti/`。不直接依赖 `adapters/` 或 `sync/`。
8. **`utils/`** — 零业务依赖，可被所有模块 import。
9. **循环依赖零容忍** — `adapters/` ↔ `graphiti/` 之间的桥接通过 `adapters/models.py` 共享类型实现，不互相 import。

**共享类型（避免循环依赖的关键）:**

`src/adapters/models.py` 中的 `NormalizedEpisode` 是适配器层和 graphiti 层的**共享数据契约**：
- 适配器层**产出** `NormalizedEpisode`（`fetch → normalize → dedup`）
- Graphiti 层**消费** `NormalizedEpisode`（`EpisodeWriter.write_one`）

两边都依赖同一个 models 文件，避免了互相 import。

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

### 3.4.3 依赖就绪检查

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

**查询路径:** Sector → (BELONGS_TO) → Stock → (AFFECTS) → Event

```cypher
// 对给定 sector_name，获取该行业所有活跃事件
MATCH (s:Sector {name: $sector_name})
      <-[:BELONGS_TO]-(stock:Stock)
      <-[:AFFECTS]-(event:Event)
WHERE event.valid_at IS NOT NULL
  AND event.invalid_at IS NULL  // 仅活跃事件
RETURN
  event.event_id,
  event.title,
  event.severity,        // low | medium | high | critical
  event.first_seen,
  event.last_updated,
  event.source_count,
  event.summary,         // Graphiti 已提取的事件摘要
  event.keywords,
  collect(DISTINCT stock.ticker) AS affected_tickers,
  collect(DISTINCT stock.name) AS affected_stocks
ORDER BY
  CASE event.severity
    WHEN 'critical' THEN 4
    WHEN 'high' THEN 3
    WHEN 'medium' THEN 2
    WHEN 'low' THEN 1
  END DESC,
  event.last_updated DESC
LIMIT 20  // 受 LLM context window 约束
```

**提取字段（每条事件）:**

| 字段 | 用途 |
|------|------|
| `event.title` | 给 LLM 的标题输入 |
| `event.severity` | 控制摘要中事件的优先级排序和措辞 |
| `event.first_seen` / `last_updated` | 时间上下文 |
| `event.source_count` | 事件可信度指标（信源越多越可靠） |
| `event.summary` | 给 LLM 的正文输入 |
| `event.keywords` | 帮助 LLM 理解事件核心主题 |
| `affected_tickers` / `affected_stocks` | 受影响标的列表 |

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

**文件:** `src/ingestion/briefing_aggregator.py`（新建）  
**类:** `SectorBriefingAggregator`

```
ingestion/scheduler.py  ──每 15 分钟调用──▶  SectorBriefingAggregator.aggregate_all()
                                                  │
                                                  ├── _query_sector_events() → Neo4j (Cypher)
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

| 端点 | 数据来源 | 核心查询方法 |
|------|---------|------------|
| `/api/events/active` | Neo4j Event 节点 | Cypher: `MATCH (e:Event) WHERE ... RETURN e ORDER BY severity DESC, last_updated DESC` |
| `/api/events/entity/:ticker` | Neo4j Stock + Event 节点 | 按 ticker 属性查找 Stock 节点，沿 AFFECTS 追溯关联 Event |
| `/api/events/sector/:name` | Neo4j Sector + Stock + Event | Sector → BELONGS_TO 反查 Stock → AFFECTS 找 Event |
| `/api/events/risk-summary` | Neo4j 聚合 + LLM | 查询高 severity 事件 → 按行业分组 → LLM 生成 summary |
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

# Part 9: 闭环检查清单

（来源: Redesign Doc §G + Internal Spec §11，合并去重）

## 9.1 NewsEngine 自身完整性检查

- [x] 5 个 NewsEngine REST 端点完整契约（请求格式 + JSON Schema + 字段定义表 + 错误码表 + 代码示例）
- [x] 1 个 SynapseEngine → NewsEngine 端点完整契约（`POST /api/tickers/whitelist`，含请求体定义、响应格式、错误码、处理代码）
- [x] NewsEngine 文件架构完整定义（目录树 + 变更标记 + 模块职责矩阵）
- [x] 模块依赖图（ASCII 有向图 + 9 条铁律 + 共享类型契约）
- [x] 生命周期管理（启动 8 步 FIFO + 关闭 4 步 LIFO + 依赖就绪检查 + 运行时并发模型）
- [x] 配置管理规范完整（20 个 .env 字段 + Pydantic Settings 完整实现 + 当前 .env 差异分析）
- [x] `sector_briefing` 完整生成链路（Cypher 查询 → LLM Prompt 设计 → 完整 Aggregator 代码 → 缓存策略 7 维表 → 降级方案 6 场景表 → Pydantic 模型）
- [x] N4 基础设施补完清单（9 个文件 + neo4j_client.py 完整代码 + 责任判定表）
- [x] N4 REST API 实施指南（FastAPI 工厂 + 端点实现准则 + Neo4j 查询映射）
- [x] main.py 入口完整代码定义

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

---

*NewsEngine 设计文档 V2.0 — 单一真相源（Single Source of Truth）。定义 NewsEngine 项目从架构到实施的全部设计规格。审批通过后作为 Tech Lead 的唯一实施蓝图。2026-06-09*

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

