# NewsEngine — 实施计划

**创建日期**: 2026-06-08  
**依据**: `NEWSENGINE-DESIGN-DOC.md` V2.0 + `P2-3-NEWSENGINE-PIPELINE-VERIFICATION.md` + `NEWSENGINE-PROPOSAL-2026-06-08.md`  
**项目根目录**: `D:\MyWallet\NewsEngine`  
**更新纪律**: 每完成一项，将 `[ ]` 改为 `[x]`，填写完成日期。任何任务状态变更必须同步到本文件。

---

## 进度总览

| Phase | 阶段名 | 总项 | 完成 | 进行中 | 未开始 | 状态 |
|-------|--------|------|------|--------|--------|------|
| N0 | 文档与设计 | 1 | 1 | 0 | 0 | ✅ 已完成 |
| N1 | 项目骨架 + 基础设施 | 4 | 4 | 0 | 0 | ✅ 已完成 |
| N2 | 数据源适配器 | 4 | 4 | 0 | 0 | ✅ 已完成 |
| N3 | Graphiti 集成 | 4 | 4 | 0 | 0 | ✅ 已完成 |
| N4 | REST API 层 | 10 | 10 | 0 | 0 | ✅ 已完成 |
| N4.5 | 历史回补（7 天） | 1 | 0 | 0 | 1 | 🔴 未开始 |
| N5 | SynapseEngine 同步 | 1 | 0 | 0 | 1 | 🔴 未开始 |
| N6 | 端到端集成测试 | 1 | 0 | 0 | 1 | 🔴 未开始 |

---

## Phase N0: 文档与设计

> **目标**: 确认架构设计、接口契约、数据模型全部锁定
> **前置**: 无（已完成）

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N0-1** | 架构设计文档 + 接口契约 | [x] ✅ | Architect | — | `NEWSENGINE-DESIGN-DOC.md` V2.0 ✅ 已完成 | 2026-06-08 |

### N0-1 设计依据

以下文档是 NewsEngine 开发的完整依据，**已全部锁定**：

- ✅ `NEWSENGINE-DESIGN-DOC.md` V2.0 — 完整设计文档（架构变更 + 接口契约 + 内部架构 + 部署要求 + SynapseEngine 侧变更指引）
- ✅ `NEWSENGINE-PROPOSAL-2026-06-08.md` — 初始 Proposal（架构图 + 技术栈）
- ✅ `P2-3-NEWSENGINE-PIPELINE-VERIFICATION.md` — 管道验证报告（3/3 管线全通）
- ✅ `LOW_LEVEL_DESIGN.md` V1.6 — SynapseEngine 侧消费契约

**待确认事项**（来自 Design Doc §9.3）：

| # | 事项 | 状态 | 老公决策 |
|---|------|------|---------|
| 1 | RSS/TG 直连 | ✅ 已决策 | Crucix 完全废弃，NewsEngine 自建 RSS 抓取 |
| 2 | sector 命名对齐 | ✅ 已决策 | Phase 1 统一中文映射表对齐 |
| 3 | Event 写入时机 | ✅ 已决策 | 15 分钟 Cron 轮询够用 |

---

## Phase N1: 项目骨架 + 基础设施

> **目标**: 项目目录结构、依赖安装、Neo4j 部署完成
> **前置**: N0 完成

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N1-1** | 项目目录结构 + .env 配置 | [x] ✅ | Tech Lead | 0.5 天 | 完整目录树 + `.env.example` + `.gitignore` | 2026-06-09 |
| **N1-2** | Neo4j Docker 部署 | [x] ✅ | Tech Lead | 0.5 天 | `docker-compose.yml` + 端口验证 | 2026-06-09 |
| **N1-3** | graphiti-core 安装 + 验证 | [x] ✅ | Tech Lead | 0.5 天 | `pip install graphiti-core` + Episode 测试 | 2026-06-08 |
| **N1-4** | requirements.txt + 依赖锁定 | [x] ✅ | Tech Lead | 0.5 天 | `requirements.txt` + pip install 验证 | 2026-06-09 |

**N1-1 项目目录结构（按 LLD V1.6 + Design Doc §1.2 定义创建）**

> **状态**: [ ] 未完成 — P2-3 阶段仅创建了 `.env` 文件和 `test_graphiti_episode.py`，完整目录树尚未创建。当前仅有：`.env`、`test_graphiti_episode.py`、`docker-compose.yml`（不完整），其余目录和文件均为空。

- [ ] 按上述目录树创建完整项目骨架

```
NewsEngine/
├── .env                        # 敏感配置（不提交 git）
│   └── BAILIAN_API_KEY=***
├── .env.example                # 配置模板
├── .gitignore
├── requirements.txt            # Python 依赖
├── docker-compose.yml          # Neo4j 部署
├── src/
│   ├── __init__.py
│   ├── adapters/               # 数据源适配器
│   │   ├── __init__.py
│   │   ├── gdelt_adapter.py    # GDELT CSV 适配器
│   │   ├── rss_adapter.py      # RSS 抓取适配器
│   │   └── akshare_adapter.py  # AkShare 个股新闻适配器
│   ├── graphiti/               # Graphiti 集成
│   │   ├── __init__.py
│   │   ├── entity_types.py     # 实体类型定义 (Pydantic)
│   │   ├── relation_types.py   # 关系类型定义
│   │   └── episode_writer.py   # Episode 写入逻辑
│   ├── api/                    # REST API 层
│   │   ├── __init__.py
│   │   ├── server.py           # FastAPI 应用入口 (:8100)
│   │   ├── routers/            # API 路由
│   │   │   ├── __init__.py
│   │   │   ├── events.py       # /api/events/* 端点
│   │   │   └── health.py       # /api/events/health
│   │   └── models.py           # API 响应模型 (Pydantic)
│   ├── sync/                   # SynapseEngine 同步
│   │   ├── __init__.py
│   │   └── ticker_sync.py      # Ticker 白名单缓存管理（Push 模式）
│   ├── core/                   # 公共基础
│   │   ├── __init__.py
│   │   ├── config.py           # 配置加载
│   │   └── neo4j_client.py     # Neo4j 连接管理
│   └── utils/                  # 工具函数
│       ├── __init__.py
│       ├── logging_config.py   # 日志配置
│       └── time_utils.py       # 时间工具
├── tests/                      # 测试
│   ├── __init__.py
│   ├── test_adapters/
│   ├── test_graphiti/
│   ├── test_api/
│   └── test_integration/
├── logs/                       # 日志目录
│   └── news_engine.log
├── data/                       # 数据目录
│   └── neo4j/                  # Neo4j 数据卷 (docker-compose 挂载)
├── docs/                       # 文档
│   └── NEWSENGINE-IMPLEMENT-PLAN.md  # 本文件
└── test_graphiti_episode.py    # P2-3 验证脚本（已存在）
```

### N1-2 Neo4j Docker 部署

`docker-compose.yml` 已存在（P2-3 创建），需要确认并完善：

- [ ] 确认 `docker-compose.yml` 包含完整配置：
  ```yaml
  services:
    neo4j:
      image: neo4j:5-community
      container_name: newsengine-neo4j
      restart: unless-stopped
      ports:
        - "7474:7474"   # Neo4j Browser
        - "7687:7687"   # Bolt
      environment:
        - NEO4J_AUTH=neo4j/${NEO4J_PASSWORD:-newsengine2026}
        - NEO4J_server_memory_heap_initial__size=512m
        - NEO4J_server_memory_heap_max__size=2g
        - NEO4J_server_memory_pagecache_size=512m
      volumes:
        - ./data/neo4j:/data
        - ./data/logs:/logs
  ```
- [ ] `docker compose up -d` 启动成功
- [ ] `curl http://localhost:7474` 返回 200
- [ ] Python `neo4j` 驱动 `bolt://localhost:7687` 连接成功

> **注意**: P2-3 已通过 `docker run` 临时验证 Neo4j。`docker-compose.yml` 已创建，只需确认完整。

### N1-3 graphiti-core 安装 + 验证

- [x] ✅ `pip install graphiti-core` 成功（v0.29.2）
- [x] ✅ 百炼 API 配置：
  ```env
  OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
  LLM_MODEL=qwen-plus
  EMBEDDING_MODEL=text-embedding-v4
  BAILIAN_API_KEY=***
  ```
- [x] ✅ Episode 创建测试通过：
  - 输入: "腾讯控股午后股价跳水，跌幅超3%，市场担忧监管收紧"
  - 提取: 3 个实体 (腾讯控股/监管/港股) + 3 条关系
  - Neo4j 验证: 8 个节点 / 12 条关系
  - 耗时: 35.1 秒

### N1-4 requirements.txt

- [ ] 创建 `requirements.txt`，包含：
  ```
  graphiti-core>=0.29.2,<1.0
  neo4j>=5.26.0,<6.0
  openai>=1.91.0,<2.0
  fastapi>=0.115.0,<1.0
  uvicorn>=0.30.0,<1.0
  pydantic>=2.11.5,<3.0
  python-dotenv>=1.0.1,<2.0
  tenacity>=9.0.0,<10.0
  numpy>=1.24.0,<2.0
  requests>=2.31.0,<3.0
  httpx>=0.27.0,<1.0
  pytest>=8.0.0,<9.0
  pytest-asyncio>=0.24.0,<1.0
  ```
- [ ] `pip install -r requirements.txt` 确认全部安装成功
- [ ] 核心库通过 import 验证：
  ```python
  import graphiti_core
  import neo4j
  import openai
  import fastapi
  import uvicorn
  ```

---

## Phase N2: 数据源适配器

> **目标**: 4 条数据源管线全部跑通 → Graphiti Episode
> **前置**: N1 完成（Neo4j + graphiti-core + .env 就绪）

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N2-1** | GDELT CSV 适配器 | [x] ✅ | Tech Lead | 1 天 | `src/adapters/gdelt_adapter.py` | 2026-06-09 |
| **N2-2** | RSS 抓取适配器 | [x] ✅ | Tech Lead | 1 天 | `src/adapters/rss_adapter.py` | 2026-06-09 |
| **N2-3** | AkShare 个股新闻适配器 | [x] ✅ | Tech Lead | 0.5 天 | `src/adapters/akshare_adapter.py` | 2026-06-09 |
| **N2-4** | Treasury API 适配器 (Phase 2+) | [x] ✅ | Tech Lead | 0.5 天 | `src/adapters/treasury_adapter.py` | 2026-06-09 |

### N2-1 GDELT CSV 适配器（P0 首要）

> **依据**: P2-3 验证报告 + Design Doc §1.1（arch 动机）

- [ ] 实现 `GdeltAdapter` 类：
  - `fetch_lastupdate()` → 从 `http://data.gdeltproject.org/gdeltv2/lastupdate.txt` 获取最新 GKG CSV URL
  - `download_gkg(csv_url)` → 下载 zip 文件 → 解压 → 返回 CSV 路径
  - `parse_gkg(csv_path)` → 解析 GKG V2 格式（27 列，制表符分隔）→ 返回记录列表
  - `filter_relevant(records, ticker_whitelist)` → 按 ticker 白名单过滤 → 返回相关条目
  - `to_episode(record)` → 将单条 GKG 记录转为 Graphiti Episode 格式

- [ ] GKG V2 关键字段映射：
  | GKG 列 | Graphiti Episode 字段 | 说明 |
  |--------|---------------------|------|
  | V2.1 (日期时间) | `valid_at` | 事件时间 |
  | V2.3 (来源 URL) | `source` | 溯源 |
  | V2.5 (人物) | `entities[]` | 人名实体 |
  | V2.6 (组织) | `entities[]` | 公司/组织实体 |
  | V2.7 (地点) | `entities[]` | 地理实体 |
  | V2.8 (主题) | `entities[]` | 行业/主题实体 |
  | V2.14 (Tone) | `severity` | 情感强度 → severity 映射 |

- [ ] Tone → severity 映射：
  | Tone 范围 | severity | 说明 |
  |----------|----------|------|
  | > 5.0 | low | 正面新闻 |
  | -5.0 ~ 5.0 | medium | 中性新闻 |
  | -15.0 ~ -5.0 | high | 负面新闻 |
  | < -15.0 | critical | 极负面新闻 |

- [ ] 去重逻辑：
  - 相同 `V2.3 (SourceUrl)` 跳过
  - content_hash 相同跳过（SHA256）

- [ ] 调度：每 15 分钟轮询一次
- [ ] 限速保护：GDELT HTTP 失败 → 指数退避重试（3 次）
- [ ] 降级：连续 3 次失败 → 使用上一轮数据 + 日志 WARNING

**P2-3 已验证基础链路**，此任务是将验证代码重构为生产级适配器。

### N2-2 RSS 抓取适配器

> **依据**: Design Doc §1.1 (Crucix 废弃 → NewsEngine 自建 RSS 抓取) + 老公决策

- [ ] 从 Crucix 的 29 个源中迁移可用的 RSS 源（P2-3 确认的可迁移源）：
  - **WHO** 疫情新闻（`https://www.who.int/news` RSS）
  - 其他免费 RSS feed（从 Crucix 源码中筛选）

- [ ] 实现 `RssAdapter` 类：
  - `fetch_feed(url)` → 解析 RSS/Atom feed → 返回条目列表
  - `to_episode(item)` → 将 RSS 条目转为 Graphiti Episode 格式
  - `filter_relevant(items, ticker_whitelist)` → 按 ticker 白名单过滤

- [ ] 去重逻辑：相同 `link` 或 `guid` 跳过
- [ ] 调度：跟随 feed 的 `updatePeriod` 配置
- [ ] 错误处理：RSS feed 不可达 → 跳过 + 日志 WARNING

### N2-3 AkShare 个股新闻适配器

> **依据**: Proposal §3.3

- [ ] 实现 `AkShareAdapter` 类：
  - `fetch_stock_news(symbol)` → `ak.stock_news_em(symbol)` → 返回新闻列表
  - `to_episode(item)` → 将 AkShare 新闻转为 Graphiti Episode 格式
  - `filter_relevant(items)` → AkShare 已按 symbol 过滤，无需二次过滤

- [ ] AkShare 新闻字段映射：
  | AkShare 字段 | Graphiti Episode 字段 | 说明 |
  |-------------|---------------------|------|
  | title | `summary` | 新闻标题 |
  | content | `content` | 新闻正文 |
  | time | `valid_at` | 发布时间 |
  | source | `source` | 来源 |
  | symbol | `entities[].ticker` | 股票代码 |

- [ ] 限速保护：每只股票查询间隔 0.5s
- [ ] 调度：每 15 分钟（跟随 GDELT 节奏）

### N2-4 Treasury API 适配器（Phase 2+）

> **依据**: Proposal §3.4 (推迟到 Phase 2+)

- [ ] 实现 `TreasuryAdapter` 类：
  - `fetch_yield_curve()` → 美国国债收益率曲线
  - `to_episode(data)` → 将结构化数据转为 Graphiti Episode
  - 标记 `source_type: "structured"` 与新闻流区分

- [ ] 调度：日级低频（每天 1 次）

---

## Phase N3: Graphiti 集成

> **目标**: Graphiti 知识图完整配置 + Episode 写入链路
> **前置**: N1 + N2 完成（依赖 + graphiti-core + 适配器就绪）

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N3-1** | 实体类型定义 (Pydantic) | [x] ✅ (P2-3 已验证) | Tech Lead | 0.5 天 | `src/graphiti/entity_types.py` | |
| **N3-2** | 关系类型定义 | [x] ✅ | Tech Lead | 0.5 天 | `src/graphiti/relation_types.py` | 2026-06-09 |
| **N3-3** | Episode 写入链路 | [x] ✅ | Tech Lead | 1 天 | `src/graphiti/episode_writer.py` | 2026-06-09 |
| **N3-4** | Ticker 白名单同步 | [x] ✅ | Tech Lead | 0.5 天 | `src/sync/ticker_sync.py` | 2026-06-09 |

### N3-1 实体类型定义

> **依据**: Proposal §4.1

P2-3 已验证 Graphiti 能自动提取实体。现在需要定义金融专用实体类型：

- [ ] 实现 `StockEntity`：
  ```python
  class StockEntity:
      ticker: str        # "0700.HK"
      name: str          # "腾讯控股"
      sector: str        # "互联网平台"
      exchange: str      # "HKEX"
  ```

- [ ] 实现 `SectorEntity`：
  ```python
  class SectorEntity:
      name: str          # "互联网平台"
      code: str          # "GICS_50"
  ```

- [ ] 实现 `CountryEntity`：
  ```python
  class CountryEntity:
      name: str          # "中国"
      code: str          # "CN"
  ```

- [ ] 实现 `PolicyEntity`：
  ```python
  class PolicyEntity:
      name: str          # "反垄断调查"
      type: str          # "regulatory"
      status: str        # "rumor / confirmed / resolved"
  ```

### N3-2 关系类型定义

> **依据**: Proposal §4.2

- [ ] 定义 6 种关系类型：
  | 关系 | 方向 | 示例 |
  |------|------|------|
  | `AFFECTS` | Event → Stock | 监管传闻 → AFFECTS → 0700.HK |
  | `CAUSED_BY` | Event → Event | 股价跳水 → CAUSED_BY → 监管传闻 |
  | `MITIGATES` | Event → Event | 公司回应 → MITIGATES → 市场恐慌 |
  | `BELONGS_TO` | Stock → Sector | 0700.HK → BELONGS_TO → 互联网平台 |
  | `LOCATED_IN` | Stock → Country | 0700.HK → LOCATED_IN → 中国 |
  | `RELATED_TO` | Event → Policy | 股价波动 → RELATED_TO → 反垄断调查 |

### N3-3 Episode 写入链路

> **依据**: Proposal §4.3 + P2-3 验证结果

- [ ] 实现 `EpisodeWriter` 类：
  - `write_episode(adapter, record, ticker_whitelist)` → 将适配器输出写入 Graphiti
  - `write_episode_batch(adapter, records)` → 批量写入（每 15 分钟一轮）
  - 内部流程：
    ```
    新记录到达
      → hash 变化检测（去重）
      → LLM 实体提取（Qwen3.6-plus）
      → 实体消歧 & 已有节点匹配
      → 关系推断（因果/关联）
      → 写入 Neo4j（Graphiti SDK）
    ```

- [ ] 去重逻辑：
  - content_hash 相同 → 跳过
  - 相同 source_url → 跳过

- [ ] 错误处理：
  - LLM 提取失败 → 降级为关键词匹配
  - Neo4j 连接失败 → 重试 3 次 → 本地缓存 → WARNING

### N3-4 Ticker 白名单同步

> **依据**: Design Doc §2.2（POST /api/tickers/whitelist）

- [ ] 实现 `TickerSync` 类（Push 模式 — 被动接收）：
  - `get_whitelist()` → 返回 ticker 列表（用于 GDELT/RSS 过滤）
  - 优先返回内存缓存（SynapseEngine 最近 Push 的数据）
  - 降级到本地缓存文件 `data/ticker_whitelist.json`
  - 不再主动拉取 SynapseEngine（Push 模式，由 SynapseEngine 启动+变化时 POST）

- [ ] 降级链：内存缓存 → 本地文件缓存 → 空白名单（GDPA/AkShare 全线不过滤）

---

## Phase N4: REST API 层

> **目标**: 5 个 REST API 端点全部实现
> **前置**: N3 完成（Graphiti 知识图配置就绪）

⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N4-1** | FastAPI 应用骨架 | [x] ✅ | Tech Lead | 0.5 天 | `src/api/server.py` | 2026-06-10 |
| **N4-2** | GET /api/events/active | [x] ✅ | Tech Lead | 0.5 天 | 端点实现 + 测试 | 2026-06-10 |
| **N4-3** | GET /api/events/entity/:ticker | [x] ✅ | Tech Lead | 0.5 天 | 端点实现 + 测试 | 2026-06-10 |
| **N4-4** | GET /api/events/sector/:name | [x] ✅ | Tech Lead | 0.5 天 | 端点实现 + 测试 | 2026-06-10 |
| **N4-5** | GET /api/events/risk-summary | [x] ✅ | Tech Lead | 0.5 天 | 端点实现 + 测试 | 2026-06-10 |
| **N4-6** | GET /api/events/health | [x] ✅ | Tech Lead | 0.5 天 | 端点实现 + 测试 | 2026-06-10 |
| **N4-7** | FastAPI 依赖注入 | [x] ✅ | Tech Lead | 0.5 天 | `src/api/deps.py` | 2026-06-10 |
| **N4-8** | API 响应 Pydantic 模型 | [x] ✅ | Tech Lead | 0.5 天 | `src/api/models.py` | 2026-06-10 |
| **N4-9** | 调度器 + 管线（ingestion） | [x] ✅ | Tech Lead | 1 天 | `src/ingestion/scheduler.py` (428行) + `pipeline.py` (250行) + `briefing_aggregator.py` (249行) | 2026-06-10 |
| **N4-10** | 进程入口 main.py | [x] ✅ | Tech Lead | 0.5 天 | `main.py` (366行，启动+关闭+信号处理) | 2026-06-10 |

### N4-0 基础设施补完（N4-1 前必须完成）

> **依据**: Design Doc Part 3 / Part 4 / Part 8

N1 阶段遗留的空壳文件，N4 实施前必须补完：

- [x] 创建 `src/core/config.py` — Pydantic Settings + 校验（Design Doc §4.1）| 177行 | 2026-06-09
- [x] 创建 `src/core/neo4j_client.py` — Neo4j Driver 单例 + 生命周期（Design Doc §8.1.2）| 64行 | 2026-06-09
- [x] 创建 `src/core/graphiti_client.py` — Graphiti SDK 实例化（Design Doc §3.5）| 64行 | 2026-06-09
- [x] 创建 `src/utils/logging_config.py` — 结构化日志配置 | 108行 | 2026-06-09
- [x] 创建 `src/utils/time_utils.py` — HKT 转换 + ISO 8601 | 115行 | 2026-06-09
- [x] 创建 `.env.example` — 配置模板（20 个字段，见 Design Doc §4.1.3）| 141行 | 2026-06-09
- [x] 填充所有 `__init__.py` — `__version__ = "1.0.0"` + `__all__` 导出
- [x] `requirements.txt` 补充 `pydantic-settings`
- [x] 验收：5 个文件 import 成功 + Settings 加载 + logging 初始化 + time_utils 正常
- [x] QA Auditor: 24/24 requirements 验证通过 | Token: `[VT-SUCCESS-N4-003]` | 归档: `.openclaw/opsx-changes/archive/2026-06-09-n4-infrastructure/`

### N4-7 FastAPI 依赖注入（deps.py）
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc Part 3
- [ ] 创建 `src/api/deps.py`：
  - `get_settings()` — 依赖注入配置
  - `get_neo4j_driver()` — 依赖注入 Neo4j Driver
  - `get_graphiti()` — 依赖注入 Graphiti 实例
  - `get_episode_writer()` — 依赖注入 EpisodeWriter
  - `get_aggregator()` — 依赖注入 SectorBriefingAggregator

### N4-8 API 响应 Pydantic 模型（models.py）
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc §5.7 + Part 2 接口契约
- [ ] 创建 `src/api/models.py`：
  - `EventItem` — 单个事件响应模型
  - `ActiveEventsResponse` — `/api/events/active` 响应
  - `EntityEventsResponse` — `/api/events/entity/:ticker` 响应
  - `SectorEventsResponse` — `/api/events/sector/:name` 响应（含 `sector_briefing: str | None`）
  - `RiskSummaryResponse` — `/api/events/risk-summary` 响应
  - `HealthResponse` — `/api/events/health` 响应
  - 所有模型字段与 Design Doc Part 2 接口契约一致

### N4-9 调度器 + 管线（ingestion/）
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc Part 5（sector_briefing 链路）+ Part 3（模块依赖）
**这是 N4 阶段最大的新增工作量**，整合适配器 + Graphiti + TickerSync 为可运行的服务。

- [ ] 创建 `src/ingestion/scheduler.py` — 多源调度编排：
  - 每 15 分钟轮询：GDELT CSV → Episode → Neo4j
  - 每 15 分钟轮询：RSS Feed → Episode → Neo4j
  - 每 15 分钟轮询：AkShare 个股新闻 → Episode → Neo4j
  - Ticker 白名单由 SynapseEngine Push 更新（启动+变化时），NewsEngine 被动接收
  - 每次轮询结束：调用 SectorBriefingAggregator.aggregate_all()
  - 错误处理：单源失败不阻断其他源

- [ ] 创建 `src/ingestion/pipeline.py` — 完整管线：
  - fetch → normalize → dedup → write（通过 EpisodeWriter）→ health check
  - 支持单条和批量两种模式

- [ ] 创建 `src/ingestion/briefing_aggregator.py` — SectorBriefingAggregator（Design Doc §5.4 完整实现）：
  - `aggregate_all()` — 对所有 sector 执行聚合（增量检测）
  - `_query_sector_events()` — Neo4j Cypher 查询（top 20 事件）
  - `_compute_fingerprint()` — SHA256 事件指纹变化检测
  - `_call_llm()` — 百炼 qwen-plus 生成简报
  - `get_cached()` — 内存缓存读取（O(1)，API 层调用）
  - 降级：LLM 不可用 → sector_briefing = null

### N4-10 进程入口（main.py）
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc Part 3 §3.4（生命周期管理）
- [ ] 创建 `main.py` — NewsEngine 进程入口：
  - 启动顺序（FIFO）：
    1. load_settings() → 校验 .env
    2. setup_logging() → 结构化日志
    3. Neo4jDriver.open() → 连接验证（硬阻塞）
    4. setup_whitelist_route(app) → 监听 POST /api/tickers/whitelist
    5. create_graphiti() → SDK 初始化
    6. EpisodeWriter(graphiti) → 写入器
    7. start_ingestion_scheduler() → 后台任务
    8. uvicorn.run(app) → FastAPI 服务
  - 关闭顺序（LIFO）：scheduler → uvicorn → writer → driver
  - asyncio event loop 共享（API + scheduler 并发安全）

### N4-1 FastAPI 应用骨架
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc Part 2
- [ ] 创建 `src/api/server.py`：
  ```python
  from fastapi import FastAPI
  from src.api.routers.events import router as events_router
  from src.api.routers.health import router as health_router

  app = FastAPI(title="NewsEngine", version="1.0.0")
  app.include_router(events_router, prefix="/api/events")
  app.include_router(health_router, prefix="/api/events")
  ```

- [ ] 端口配置：`8100`（不与 SynapseEngine `8000` 冲突）
- [ ] CORS 配置：允许 SynapseEngine 跨域访问
- [ ] uvicorn 启动脚本：`uvicorn src.api.server:app --host 0.0.0.0 --port 8100`

### N4-2 GET /api/events/active
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc §2.3（完整接口契约）
- [ ] 参数: `limit` (int, 默认 50), `min_severity` (string, 默认 "medium"), `sector` (string, 可选)
- [ ] 响应格式: `{events: [...], total: int, freshness: {...}}`
- [ ] 排序: severity 降序 + last_updated 降序
- [ ] 错误码: 503 (Neo4j 不可用), 500 (内部错误)

### N4-3 GET /api/events/entity/:ticker
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc §2.4
- [ ] 路径参数: `ticker` (格式: `0700.HK`)
- [ ] ticker 格式转换: SynapseEngine 的 `HK.00700` → NewsEngine 的 `0700.HK`
- [ ] 响应格式: `{ticker: str, events: [...], summary: {...}}`
- [ ] summary 包含: `total_events`, `avg_severity`, `risk_level`, `news_sentiment_score`
- [ ] 错误码: 404 (无该 ticker 事件), 503, 500

### N4-4 GET /api/events/sector/:name
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc §2.5
- [ ] 路径参数: `sector_name` (中文，如 "互联网平台")
- [ ] 响应格式: `{sector: str, events: [...], statistics: {...}, sector_briefing: str}`
- [ ] **`sector_briefing` 字段**: LLM 聚合的 300-500 字行业情报简报，由 `SectorBriefingAggregator` 异步预计算（见 Design Doc §5）
- [ ] 错误码: 404 (无该行业事件)

### N4-5 GET /api/events/risk-summary
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc §2.6
- [ ] 响应格式: `{overall_risk, risk_score, top_risks, sector_risk_levels, summary, generated_at}`
- [ ] `top_risks`: Top-5 高风险事件，包含 `potential_impact`（LLM 生成的影响分析）
- [ ] `sector_risk_levels`: 各行业风险等级映射
- [ ] 缓存策略: 结果缓存 5 分钟（降低 Neo4j 查询压力）

### N4-6 GET /api/events/health
⚠️ **铁律：不重复造轮子。** 以下 N4-0 已完成的基础件，后续任务如果涉及对应功能，必须复用，禁止自建等价逻辑：

| 基础件 | 功能 | 调用方式 |
|--------|------|----------|
| `get_settings()` | 配置加载（.env → Pydantic Settings） | `from src.core.config import get_settings` |
| `get_neo4j_driver()` | Neo4j 连接（单例） | `from src.core.neo4j_client import get_neo4j_driver` |
| `create_graphiti()` | Graphiti SDK 实例化 | `from src.core.graphiti_client import create_graphiti` |
| `get_logger(__name__)` | JSON 结构化日志 | `from src.utils.logging_config import get_logger` |
| `now_hkt()` | HKT 时区（UTC+8） | `from src.utils.time_utils import now_hkt` |
| `to_iso8601()` | 时间 → ISO 8601 字符串 | `from src.utils.time_utils import to_iso8601` |

> **依据**: Design Doc §2.7
- [ ] 响应格式: `{status, uptime_seconds, data_sources: {...}, neo4j: {...}, graphiti: {...}}`
- [ ] 数据源状态: `gdelt_csv`, `rss`, `akshare`, `treasury`
- [ ] 新鲜度判定: 最近数据源更新在 30 分钟内 = healthy，否则 = stale
- [ ] Neo4j 状态: 节点数、关系数
- [ ] Graphiti 状态: 今日 Episode 数

---

## Phase N4.5: 历史回补（7 天）

> **目标**: 首次联调前，回补最近 7 天 ticker 相关新闻，确保 sector briefing 有足够数据支撑
> **前置**: N4 完成（所有 API + 调度器 + 管线就绪）
> **定位**: 一次性回补脚本，跑完后自动切回正常 15 分钟轮询

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N4.5-1** | 7 天回补脚本 | [ ] | Tech Lead | 0.5 天 | `scripts/backfill_7d.py` | |

### N4.5-1 7 天回补脚本

> **预估数据量**: ticker 白名单 ~50 只，GDELT 每天相关 ~200-500 条，7 天合计 ~1,400-3,500 条
> **预估耗时**: 30 分钟（串行 LLM 提取），API 成本 <$5
> **Neo4j 增量**: ~1.5 万节点 / 2 万关系

- [ ] 创建 `scripts/backfill_7d.py` — 一次性回补脚本：
  - 读取 ticker 白名单（本地 `data/ticker_whitelist.json` 或手动传入）
  - 时间窗口：过去 7 天，按天分片（每次处理 1 天数据）
  - 数据源：GDELT GKG CSV（按日期范围下载，`https://data.gdeltproject.org/gdeltv2/masterlist.gkg.csv` 或直接按日抓取）
  - 流程：按天下载 → 解析 GKG → 按 ticker 白名单过滤 → 去重 → 调用 EpisodeWriter 写入 Neo4j
  - 限速保护：LLM 429 → 指数退避重试（3 次），连续失败跳过当天
  - 进度追踪：记录已处理天数到本地状态文件，支持中断续跑
  - 完成后自动运行 `aggregate_all()` 生成 sector briefing
  - 完成后切回正常模式（提示用户 `python main.py` 启动服务）

- [ ] 验收标准：
  - [x] 7 天数据全部写入 Neo4j（节点数 > 1000）
  - [x] `GET /api/events/active` 返回回补数据
  - [x] `GET /api/events/sector/:name` 返回 `sector_briefing` 字段（非 null）
  - [x] 无 unhandled exception
  - [x] API 成本在 $5 以内

---

## Phase N5: SynapseEngine Push 联调

> **目标**: SynapseEngine → NewsEngine ticker 白名单 Push 端到端联调
> **前置**: N4 完成（NewsEngine `POST /api/tickers/whitelist` 端点可访问）+ SynapseEngine P3-C2.6（push_ticker_whitelist）完成

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N5-1** | Ticker Push 端到端联调验证 | [ ] | Tech Lead | 0.5 天 | 联调验证报告 | |

### N5-1 Ticker Push 端到端联调验证

> **前置依赖**: SynapseEngine 需先完成 P3-C2.6（启动时 + ticker 变化时调用 `push_ticker_whitelist()`）
- [ ] SynapseEngine 启动 → 调用 `POST /api/tickers/whitelist` → NewsEngine 返回 200 OK
- [ ] NewsEngine `data/ticker_whitelist.json` 缓存文件写入成功
- [ ] SynapseEngine 持仓变动/watchlist 增删 → 再次 push → NewsEngine 白名单更新
- [ ] GDELT 过滤器使用 push 后的 ticker 白名单正确过滤
- [ ] 降级验证: NewsEngine 先于 SynapseEngine 启动 → 使用本地缓存文件兜底 → SynapseEngine 后启动 push → 白名单刷新

---

## Phase N6: 端到端集成测试

> **目标**: 全链路验证 NewsEngine 能独立运行 + SynapseEngine 可消费
> **前置**: N5 完成（Ticker Push 联调通过）

| # | 任务 | 状态 | 负责人 | 预估 | 产出物 | 完成日期 |
|---|------|------|--------|------|--------|----------|
| **N6-1** | 全链路端到端测试 | [ ] | Tech Lead | 1 天 | 测试报告 | |

### N6-1 全链路测试

**测试场景**:
1. GDELT CSV 下载 → 解析 → 过滤 → Episode 写入 → Neo4j 可见 → REST API 可查询
2. RSS feed 拉取 → Episode 写入 → Neo4j 可见 → REST API 可查询
3. AkShare 个股新闻 → Episode 写入 → Neo4j 可见 → `/api/events/entity/:ticker` 可查询
4. `/api/events/active` 返回正确的事件列表 + freshness 信息
5. `/api/events/sector/:name` 返回行业事件 + `sector_briefing` 字段
6. `/api/events/risk-summary` 返回风险摘要
7. `/api/events/health` 返回各数据源状态
8. 降级测试: NewsEngine 不可用时 SynapseEngine 使用 news_events 缓存
9. 超时测试: GDELT 下载超时 → 跳过 → 等下一轮
10. 数据新鲜度测试: 15 分钟轮询确认数据及时更新

**通过标准**:
- 场景 1-7 全部通过
- 降级行为正确（不阻断 SynapseEngine 主流程）
- 无 unhandled exception

---

## 阻塞项

| # | 阻塞项 | 严重性 | 状态 | 解决方 |
|---|--------|--------|------|--------|
| 1 | SynapseEngine `push_ticker_whitelist()` (P3-C2.6) 未实现 | 🟡 中等 | 待 P3-C2.6 | Tech Lead |
| 2 | GDELT HTTPS API 被墙 | 🟢 已缓解 | HTTP CSV 通路可用 | 接受 15 分钟延迟 / 后续海外 VPS |
| 3 | sector 命名对齐 | 🟢 待实施 | Phase 1 统一中文映射表 | Tech Lead + 老公确认 |
| 4 | RSS feed URL 列表 | 🟢 低优先级 | 从 Crucix 源码筛选 | Tech Lead |

---

## 变更记录

| 日期 | 变更内容 | 操作人 |
|------|----------|--------|
| 2026-06-08 | 初始创建 | 灵汐 |
| 2026-06-09 | N1/N2/N3 全部完成，更新状态标注。N3-2/3/4 代码已完成（Tech Lead 实现），修正 IMPLEMENT_PLAN 滞后标注。 | 灵汐 |

---

*NewsEngine 实施计划 v1.1 — 2026-06-09 更新：N1/N2/N3 完成，N4 进行中*
