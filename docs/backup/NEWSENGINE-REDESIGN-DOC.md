# SynapseEngine ← NewsEngine 替代 Crucix 架构变更设计文档

**版本:** V1.2  
**日期:** 2026-06-08 (初版 V1.0), 2026-06-09 (修订 V1.2)  
**作者:** Chief Architect  
**依据文档:**
- `NewsEngine Proposal V1.0` (2026-06-08)
- `SynapseEngine LOW_LEVEL_DESIGN.md` V1.4 (2026-06-04)
- `SynapseEngine IMPLEMENT_PLAN.md` (2026-05-30, 持续更新)

**状态:** Draft → 待审批  
**审批人:** 老公  
**LLD 升级链:** 本 Redesign Doc 审批通过后，LLD 升级为 V1.5 → V1.6。当前 LLD 版本: **V1.6**（已实施 Redesign Doc + sentiment_raw_data 废弃 + FinBERT 替换）。

---

## V1.1 变更摘要 (灵汐 Review 修正)

| # | 变更内容 | 影响章节 |
|---|---------|---------|
| 1 | **TODO 2 矛盾修正:** 采用方案 A — `run_stock_ingestion()` 改为走 NewsEngine `/api/events/entity/:ticker`，移除 AkShare 直调冗余。TODO 2 删除并标注"已决策" | §7.3b, §H |
| 2 | **DDL 表格语法:** 确认 `news_events` 字段定义表连续无断裂（`entities` 行与上下文连续） | §D.3 |
| 3 | **Treasury API 补充:** 架构图标注 `(Phase 2+)`、对比表补充、F.1 依赖表新增行、异常矩阵新增行 | §A.2, §A.3, §F.1, §10 |
| 4 | **UI 组件替换清单:** 新增完整的 `.tsx` 文件级别 `crucix_*` → `news_*` 字段映射表（11 个文件） | §12 |
| 5 | **severity 映射修正:** `_map_severity_to_score()` low→0.50 / medium→0.40 / high→0.20 / critical→0.10 | §6.3 |
| 6 | **LLD 升级链标注:** 文档头明确"审批通过后 LLD 升级至 V1.5" | 文档头 |
| 7 | **TODO 重编号:** 移除已决策的 TODO 2，剩余 3 项重新编号并标注"需老公决策" | §H |

## V1.0 变更摘要

| # | 变更内容 | 影响范围 |
|---|---------|---------|
| 1 | Crucix 废弃，NewsEngine 接替为情报子系统 | SynapseEngine 数据接入层全量替换 |
| 2 | NewsEngine 数据源方案：GDELT CSV + RSS 直连 + AkShare + Graphiti 时序知识图 | §A, §B |
| 3 | SynapseEngine ↔ NewsEngine 双向 REST API 接口契约定义 | §C |
| 4 | MongoDB Schema 变更：`source` 字段值变更 + 新增 `news_events` Collection | §D |
| 5 | IMPLEMENT_PLAN 任务重写：P2-3 + P3-C2 + 新增 P3-C2.5 | §E |
| 6 | 新增外部依赖：Neo4j Docker + NewsEngine 服务 | §F |

---

## A. 架构变更说明

### A.1 为什么去掉 Crucix

| 痛点 | 详情 |
|------|------|
| **GDELT HTTPS API 不可靠** | `api.gdeltproject.org` 被 GFW 拦截/SSL 层阻断，直连超时。代理虽可建立 CONNECT tunnel 但 TLS 握手 `SSL_ERROR_SYSCALL` 被拒（P2-3 验证结论，2026-06-04） |
| **Crucix adapter 漏 RSS 产出** | P3-C2 验收发现 Crucix 的 29 源聚合只输出了 GDELT GKG 解析结果，RSS feed / Telegram 情报帖未正确映射到 MongoDB |
| **原始新闻是"数据湖"而非"情报"** | Crucix 仅做聚合+关键词匹配，无实体提取、无事件去重、无因果关系建模 |
| **GDELT 数据文件通路可用** | `data.gdeltproject.org` (HTTP) 可正常下载 GKG/Events CSV，是可靠替代方案 |

### A.2 NewsEngine 新架构

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

### A.3 NewsEngine 与 SynapseEngine 的关系

```
┌──────────────────────────┐         ┌──────────────────────────┐
│      SynapseEngine        │         │       NewsEngine          │
│   D:\MyWallet\SynapseEngine│  REST   │  D:\MyWallet\NewsEngine  │
│                           │◄────────│                           │
│  ┌─────────────────────┐ │         │ ┌───────────────────────┐ │
│  │ news_engine_client  │ │  GET    │ │ FastAPI REST API      │ │
│  │ (REST 调用层)        │─┼────────►│ │ :8100                 │ │
│  └─────────────────────┘ │         │ └───────────────────────┘ │
│                           │         │                           │
│  ┌─────────────────────┐ │         │ ┌───────────────────────┐ │
│  │ /api/portfolio/     │ │◄────────│ │ Ticker 同步 Client    │ │
│  │ tickers             │ │  GET    │ │ (启动+6h刷新)          │ │
│  └─────────────────────┘ │         │ └───────────────────────┘ │
│                           │         │                           │
│  消费者:                  │         │  数据源:                  │
│  • MiroFish (消费events)  │         │  • GDELT CSV (HTTP)      │
│  • PM Agent (消费events)  │         │  • RSS 直连 (原Crucix)   │
│  • Kronos (消费events)    │         │  • AkShare 个股新闻       │
│                           │         │  • Treasury API           │
└──────────────────────────┘         └──────────────────────────┘

两个项目**物理独立**（不同目录、不同进程、不同数据库），通过 HTTP REST API 通信。
NewsEngine 是 SynapseEngine 的**情报子系统**，提供结构化事件情报。
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

## B. LLD 中所有 Crucix 引用的替换清单

以下按 LLD V1.4 章节顺序，逐处列出涉及 Crucix 的地方及替换方案。

### §0.3 核心组件依赖图

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

### §1.2 数据流方向

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

### §3 目录结构

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

### §4.2.1 portfolio_state_history

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

### §4.2.2 watchlist_history

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

### §4.2.3 feature_store

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

### §4.2.6 ~~sentiment_raw_data~~ → news_events

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

### §6.0 架构概述（PM Agent 决策引擎架构图）

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

### §6.1 PMEngineState 的 macro_context 字段

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

### §6.3 数据接入层（整节重写）

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

### §6.3 适配器: news_engine_client.py (替代原 crucix_adapter.py)

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


def _map_severity_to_score(severity: str, sentiment_direction: str = "neutral") -> float:
    """
    NewsEngine severity → 初始 sentiment_score。

    severity 仅表示事件影响程度，不表示正负面方向。
    正负面由 NewsEngine LLM 端完成（severity → score 映射）。
    此处提供保守初始值（偏中性偏负面），让 NLP 节点后续修正。

    映射逻辑:
      low      → 0.50 (中性，低影响事件无需预判)
      medium   → 0.40 (轻微偏负面，因为新闻 medium 通常带负面倾向)
      high     → 0.20 (偏负面，高影响事件需要关注)
      critical → 0.10 (极负面，默认假设高风险)
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

### §7.3b news_ingestion_node.py

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

### §7.3c nlp_scoring_node.py

**原文:** 保持不变

**变更:** 整节点废弃。V1.6 起 FinBERT 不再使用，替换为 NewsEngine LLM 在实体提取时同步完成的情感打分（severity → 0~1 分数映射）。

**变更类型:** 删除 — P3-C3 整个节点废弃

---

### §7.4 sentiment_analyst.py

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

### §7.6 dragon_catcher.py

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

### §7.x morning_rebalance.py (LLD §7.x, 在 IMPLEMENT_PLAN P3-C5 中定义)

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

### §9 Cron 调度时序图

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

### §10 异常处理矩阵

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

### §11.1 .env 环境变量

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

### §11.2 settings.yaml

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

### §12 UI API 设计 (LLD)

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

## C. SynapseEngine ↔ NewsEngine 接口契约

### C.1 接口总览

```
┌──────────────────────────────────────────────────────────────────┐
│                    SynapseEngine  ←→  NewsEngine                  │
│                                                                   │
│  SynapseEngine 提供:                                              │
│  ┌──────────────────────────────────────────────────┐            │
│  │ GET /api/portfolio/tickers                       │            │
│  │ 返回: 持仓 + 自选股 ticker 列表                    │            │
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

### C.2 SynapseEngine → NewsEngine: GET /api/portfolio/tickers

NewsEngine 启动时 + 每 6 小时调用此端点，获取 ticker 白名单用于 GDELT CSV 过滤。

**请求:**
```
GET /api/portfolio/tickers HTTP/1.1
Host: localhost:8000        # SynapseEngine UI API 端口（或 Python 内嵌 HTTP）
```

**成功响应 (200):**
```json
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

**字段定义:**

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

**数据来源:**
- 持仓 ticker: `portfolio_state_history` 最新 BOD 快照 → `slots[].symbol`
- 自选股 ticker: `watchlist_history` 最新快照 → `candidates[].symbol`

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `503` | `{"error": "MongoDB unavailable", "detail": "..."}` | 数据库不可用 |
| `500` | `{"error": "Internal server error", "detail": "..."}` | 其他错误 |

**NewsEngine 端调用伪代码:**
```python
# NewsEngine 侧: 拉取 ticker 白名单
import requests

def fetch_ticker_whitelist():
    resp = requests.get("http://localhost:8000/api/portfolio/tickers", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # 返回 biz_code 列表供 GDELT 过滤
    return [t["biz_code"] for t in data["tickers"]]
```

---

### C.3 NewsEngine → SynapseEngine: GET /api/events/active

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

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `503` | `{"error": "Neo4j unavailable", "detail": "..."}` | 图数据库不可用 |
| `500` | `{"error": "Internal error", "detail": "..."}` | 其他错误 |

---

### C.4 NewsEngine → SynapseEngine: GET /api/events/entity/:ticker

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

### C.5 NewsEngine → SynapseEngine: GET /api/events/sector/:name

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
  "sector_briefing": "## 互联网平台行业情报汇总 (2026-06-08)\n\n### 事件 1: 互联网平台监管收紧传闻 (high severity)\n受传闻影响，腾讯控股午后跌幅扩大...\n\n### 事件 2: 阿里巴巴回购计划 (low severity)\n阿里巴巴宣布 50 亿美元回购计划..."
}
```

**新增字段 (V1.1 — 支持 MiroFish 行业推演直接消费):**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sector_briefing` | string | 否 | LLM 聚合的 300-500 字行业种子材料。NewsEngine 端将同行业事件摘要拼接为 Markdown 格式。MiroFish 直接使用此字段，无需再从 MongoDB 聚合新闻全文。 |

**错误响应:**

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `404` | `{"error": "Sector not found"}` | 无该行业事件 |

---

### C.6 NewsEngine → SynapseEngine: GET /api/events/risk-summary

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
| `overall_risk` | string | 是 | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `risk_score` | number | 是 | 风险评分 0~1 |
| `top_risks` | array | 是 | Top-5 高风险事件 |
| `top_risks[].potential_impact` | string | 是 | LLM 生成的潜在影响分析 |
| `sector_risk_levels` | object | 是 | 各行业风险等级 |
| `summary` | string | 是 | LLM 生成的风险摘要 |
| `generated_at` | string | 是 | 生成时间 |

---

### C.7 NewsEngine → SynapseEngine: GET /api/events/health

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
| `status` | string | 是 | `healthy` / `degraded` / `down` |
| `uptime_seconds` | int | 是 | 服务运行时间 |
| `data_sources.*.status` | string | 是 | 各数据源状态: `ok` / `degraded` / `down` |
| `data_sources.*.last_update` | string | 是 | 最后数据更新时间 |
| `data_sources.*.latency_minutes` | int | 是 | 距上次更新的分钟数 |
| `neo4j.node_count` | int | 是 | 知识图谱节点数 |
| `graphiti.episode_count_today` | int | 是 | 今日处理 Episode 数 |

---

## D. MongoDB Schema 变更

### D.1 最终决策 (V1.1 Review)

**方案: 仅保留 news_events Collection。V1.6 完全移除 sentiment_raw_data。**

**决策理由 (2026-06-08 老公 + 灵汐 + Architect 共识):**
1. Neo4j (NewsEngine) 已经提供结构化事件（实体/关系/因果链/LLM 情感/severity），FinBERT 打分为冗余操作
2. 三个消费者均可直接消费 NewsEngine REST API 或 `news_events` Collection
3. MiroFish 种子材料由 NewsEngine `/api/events/sector/:name` 的 `sector_briefing` 字段提供
4. PM Agent macro_context 已改为 NewsEngine API (V1.5)
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

### D.2 news_events Collection (唯一新增)

### D.2 news_events Collection (唯一新增)
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

### D.3 跨 Collection 字段重命名（存量兼容）

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

### D.4 sentiment_raw_data 废弃说明

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

## E. IMPLEMENT_PLAN 变更影响

### E.1 P2-3 Crucix 宏观冲击感知验证 → 替换为 NewsEngine 验证

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

### E.2 P3-C2 新闻采集管线 → 重写为 NewsEngine 客户端集成

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

### E.3 新增 P3-C2.5: SynapseEngine `/api/portfolio/tickers` 端点实现

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

### E.4 P3-C5 双池洗牌（字段重命名）

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

### E.5 P3-D2 早盘危机逃生（字段重命名）

**P3-D2 原有描述:**
```
触发条件: 持仓标的进入"瘟疫状态"（crucix_sentiment_score < 0.20 或 sector_trend == "SECTOR_DECAY"）
```

**变更为:**
```
触发条件: 持仓标的进入"瘟疫状态"（news_sentiment_score < 0.20 或 sector_trend == "SECTOR_DECAY"）
```

---

### E.6 P3-C3 FinBERT 情感打分 → 废弃

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

### E.7 P3-C4 MiroFish 行业推演（输入源变更）

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

### E.8 P4-2 Webhook 告警（组件名变更）

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

### E.7 models.py 字段变更（IMPLEMENT_PLAN P3-C0-3）

| 模型 | 旧字段 | 新字段 |
|------|--------|--------|
| `SlotState` | `crucix_sentiment_score` | `news_sentiment_score` |
| `WatchlistCandidate` | `crucix_sentiment_score` | `news_sentiment_score` |
| `SentimentFeatures` | `crucix_sentiment_score` | `news_sentiment_score` |
| `SentimentFeatures` | `crucix_news_count` | `news_event_count` |
| `SentimentFeatures` | — | `news_risk_level` (新增) |

---

## F. 依赖与部署要求

### F.1 新增外部依赖

| 依赖 | 版本 | 部署方式 | 端口 | 说明 |
|------|------|---------|------|------|
| **Neo4j** | 5.x | Docker (WSL2) | 7687 (bolt), 7474 (browser) | Graphiti 后端存储 |
| **NewsEngine** | — | Python 进程 (Windows/WSL2) | 8100 | 情报引擎服务 |
| **graphiti-sdk** | latest | pip | — | Python SDK |
| **gdeltPyR** | latest | pip | — | GDELT CSV 解析 |
| **Treasury API** | — | NewsEngine 内聚 (Phase 2+) | — | 美国国债收益率/利率决策，低频日级源，Phase 1 不接入 |

### F.2 Neo4j Docker 部署（WSL2 侧）

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

### F.3 端口规划

| 服务 | 端口 | 宿主机 | 容器内 | 冲突风险 |
|------|------|--------|--------|---------|
| MongoDB | 27017 | WSL2 | Docker | 无 |
| Neo4j Bolt | 7687 | WSL2 | Docker | 无 |
| Neo4j Browser | 7474 | WSL2 | Docker | 无 |
| NewsEngine API | 8100 | WSL2 | — | 无（不与 SynapseEngine 8000 冲突） |
| SynapseEngine UI | 8000 | WSL2 | — | 无 |
| SynapseEngine (其他) | — | — | — | 不变 |

### F.4 内存预算（WSL2 总内存 16GB 假设）

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

### F.5 启动顺序

```
1. docker-compose up -d          # MongoDB + Neo4j
2. python NewsEngine/main.py     # NewsEngine API (:8100)
3. python SynapseEngine/...      # SynapseEngine 主进程 (:8000)
```

NewsEngine 启动后自动调用 SynapseEngine `/api/portfolio/tickers` 拉取 ticker 白名单。

---

## G. 闭环检查清单

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
- [x] LLD §7.3c nlp_scoring_node.py：确认无代码变更（数据源变化不影响）
- [x] LLD §7.4 sentiment_analyst.py：crucix_sentiment_score → news_sentiment_score
- [x] LLD §7.6 dragon_catcher.py：DragonCandidate 字段重命名
- [x] LLD §7.x morning_rebalance.py：字段重命名
- [x] LLD §9 Cron 时序图：08:00 Crucix 宏观查询 → NewsEngine 宏观事件查询
- [x] LLD §9.3 时间点表：描述文本更新
- [x] LLD §10 异常处理矩阵：Crucix → NewsEngine + 新增异常行
- [x] LLD §11.1 .env：CRUCIX_URL → NEWSENGINE_BASE_URL
- [x] LLD §11.2 settings.yaml：crucix 配置段 → news_engine 配置段
- [x] IMPLEMENT_PLAN P2-3：Crucix 验证 → NewsEngine 验证方案
- [x] IMPLEMENT_PLAN P3-C2：新闻采集管线 → NewsEngine 客户端集成
- [x] IMPLEMENT_PLAN P3-C2.5（新增）：/api/portfolio/tickers 端点
- [x] IMPLEMENT_PLAN P3-C5/P3-D2：字段重命名
- [x] IMPLEMENT_PLAN models.py (P3-C0-3)：字段变更
- [x] 双向接口契约：5 个 NewsEngine 端点 + 1 个 SynapseEngine 端点完整定义
- [x] 每个接口有请求/响应格式 + JSON 示例 + 错误码
- [x] MongoDB Schema 变更有 DDL 语句
- [x] 依赖部署方案完整（Neo4j Docker + 端口 + 内存预算）

---

## H. 不确定事项 (TODO: 待确认)

| # | 事项 | 说明 |
|---|------|------|
| 1 | **RSS/TG 直连端口** | ✅ **已决策 (2026-06-08 老公):** 彻底废弃 Crucix，把里面的免费源（RSS/TG 等）迁移到 NewsEngine，NewsEngine 自建 RSS 抓取。 |
| 2 | **sector 命名对齐** | ✅ **已决策 (2026-06-08 老公):** 同意对齐。Phase 1 统一 NewsEngine ↔ SynapseEngine 的行业中文名映射表。 |
| 3 | **Event 写入时机** | ✅ **已决策 (2026-06-08 老公):** 15 分钟 Cron 轮询够用，不需要实时增量推送。Phase 1 采用启动批量 + Cron 每 15 分钟轮询方案。 |

---

## I. 变更记录

| 日期 | 变更内容 | 操作人 |
|------|----------|--------|
| 2026-06-08 | V1.0 初始创建：完整 Crucix → NewsEngine 替代方案 | Chief Architect |
| 2026-06-08 | V1.1 灵汐 Review 修正：TODO 2 矛盾修正 (方案A)、Treasury API 补充、UI 组件替换清单、severity 映射修正、LLD 升级链标注 | Chief Architect |

---

*Redesign Doc V1.1 — 灵汐 Review 修正版，待老公最终审批后进入 Phase 1 实施*
