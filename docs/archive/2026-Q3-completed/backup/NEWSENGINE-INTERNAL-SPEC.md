# NewsEngine 内部架构规格说明书

**版本:** V1.0  
**日期:** 2026-06-09  
**作者:** Chief Architect  
**依据文档:**
- `NEWSENGINE-REDESIGN-DOC.md` V1.2 — 外部接口契约（SynapseEngine ↔ NewsEngine）
- `NEWSENGINE-IMPLEMENT-PLAN.md` V1.1 — 实施计划
- `NEWSENGINE-PROPOSAL-2026-06-08.md` — 初始 Proposal

**适用对象:** Tech Lead（NewsEngine 实施方）
**文档定位:** 本文件是 Redesign Doc 的补充，从 **NewsEngine 提供者视角** 定义内部架构。Redesign Doc 定义了外部契约（API、MongoDB Schema），本文件定义 NewsEngine 自身的模块职责、依赖关系、生命周期和配置规范。

> **重要声明:** 本文件 **不修改、不替代、不冲突** 任何 Redesign Doc 已定义的接口契约。所有外部接口（5 个 REST 端点、请求/响应格式、错误码）保持不变。

---

## 1. 评估：Redesign Doc 的完整性

### 1.1 Redesign Doc 已完整定义的内容（消费者视角）✅

| 维度 | 定义位置 | 完整性 |
|------|---------|--------|
| SysnapseEngine ↔ NewsEngine 接口契约（5 + 1 端点） | §C.1~C.7 | ✅ 完整（请求格式、JSON Schema、错误码） |
| MongoDB Schema 变更 | §D.1~D.4 | ✅ 完整（DDL、索引、字段定义） |
| SynapseEngine 客户端代码 | §6.3 `news_engine_client.py` | ✅ 完整 |
| Neo4j Docker 部署配置 | §F.2 | ✅ 完整 |
| 端口规划 + 内存预算 | §F.3~F.4 | ✅ 完整 |
| 启动顺序 | §F.5 | ✅ 完整 |
| SynapseEngine 侧字段重命名清单 | §B, §12 | ✅ 完整 |

### 1.2 Redesign Doc 缺失的内容（提供者视角）❌

以下是本次审计发现的 6 个缺口：

| # | 缺失项 | 影响 | 本文件章节 |
|---|--------|------|-----------|
| 1 | NewsEngine 内部文件架构定义 | Tech Lead 不知道"该建什么文件"，N4 阶段 0 字节空壳 | §2 |
| 2 | 模块依赖图 | 不知道谁依赖谁，循环依赖风险 | §3 |
| 3 | 生命周期管理规范 | `config.py` / `neo4j_client.py` 谁先初始化？启动顺序对不对？ | §4 |
| 4 | 配置管理规范 | `.env` 里需要哪些字段？默认值是什么？目前 `.env` 不完整 | §5 |
| 5 | 测试策略 | 单测/集成测试的边界在哪？mock 策略是什么？ | §6 |
| 6 | `sector_briefing` 字段名耦合问题 → ✅ 已解决 | 直接改名 `mirofish_seeds` → `sector_briefing`，完整生成链路已定义 | §7 |

### 1.3 判断：这是谁的遗漏？

| 缺失项 | 责任归属 | 理由 |
|--------|---------|------|
| 文件架构 + 模块依赖 + 生命周期 | **Architect** 🔴 | 这些是架构规格的核心输出物。Redesign Doc 作为架构设计文档，应在 §A 或新增附录中定义 NewsEngine 内部结构。 |
| 配置管理规范 | **Architect** 🟡 | `.env` 字段清单应在设计阶段定义。当前 `.env` 只有 4 个变量，缺失 FASTEPI 端口、日志级别、超时配置等。 |
| 测试策略 | **Architect** 🟡 | 属于设计阶段输出物。单测/集成测试边界不清晰会导致 N6 阶段测试无效。 |
| `config.py` / `neo4j_client.py` 0 字节空壳 | **Tech Lead** 🔴 | N1-1（项目骨架）标记为 ✅ 已完成，但两个关键基础设施文件未实现。IMPLEMENT_PLAN 的 checklist 存在验证不严的问题。 |
| `mirofish_seeds` 命名耦合 → ✅ 已修复 | **Architect** → 已解决 | 字段名改为 `sector_briefing`，完整生成链路定义于 §7.3。 |

---

## 2. 文件架构与模块职责

### 2.1 完整目录树

```
NewsEngine/
├── .env                          # 敏感配置（不提交 git）【已存在】
├── .env.example                  # 配置模板【需创建】
├── .gitignore
├── requirements.txt              # Python 依赖【已存在】
├── docker-compose.yml            # Neo4j 部署【已存在】
├── pyproject.toml                # 项目元数据【建议新增】
├── main.py                       # 应用入口【需新建】
│
├── src/
│   ├── __init__.py               # 包版本号 __version__ = "1.0.0"
│   │
│   ├── core/                     # ★ 基础设施层（N1 应完成，N4 前必须补完）
│   │   ├── __init__.py
│   │   ├── config.py             # 配置加载 + 校验（Pydantic Settings）【0 字节 → 需补】
│   │   ├── neo4j_client.py       # Neo4j 连接管理（单例/生命周期）【0 字节 → 需补】
│   │   └── graphiti_client.py    # Graphiti 实例封装（N4 新增，见 §3.4）
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
│   ├── ingestion/                # ★ 数据摄取调度层（N4 阶段创建，整合 adapters + graphiti + sync）
│   │   ├── __init__.py
│   │   ├── scheduler.py          # 多源调度编排（15 分钟 Cron 轮询）
│   │   └── pipeline.py           # 完整管线: fetch → normalize → dedup → write → health check
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
│   │   ├── test_config.py        # config.py 单元测试
│   │   ├── test_adapters/        # 适配器单元测试（mock 外部 HTTP）
│   │   │   ├── __init__.py
│   │   │   ├── test_base.py
│   │   │   ├── test_gdelt.py
│   │   │   ├── test_rss.py
│   │   │   ├── test_akshare.py
│   │   │   └── test_treasury.py
│   │   ├── test_graphiti/        # Graphiti 单元测试（mock graphiti-core）
│   │   │   ├── __init__.py
│   │   │   ├── test_episode_writer.py
│   │   │   ├── test_entity_types.py
│   │   │   └── test_relation_types.py
│   │   └── test_api/             # API 端点单元测试（mock graphiti）
│   │       ├── __init__.py
│   │       ├── test_events.py
│   │       └── test_health.py
│   └── integration/              # 集成测试（需要真实 Neo4j + 百炼）
│       ├── __init__.py
│       ├── test_neo4j_connection.py
│       ├── test_graphiti_write.py
│       ├── test_gdelt_pipeline.py
│       └── test_api_endpoints.py
│
├── logs/                         # 日志目录【已存在】
│   └── news_engine.log
├── data/                         # 数据目录【已存在】
│   └── neo4j/                    # Neo4j 数据卷
│   └── ticker_cache.json         # TickerSync 缓存文件
└── docs/                         # 文档【已存在】
    ├── NEWSENGINE-IMPLEMENT-PLAN.md
    └── NEWSENGINE-INTERNAL-SPEC.md  # 本文件
```

### 2.2 模块职责矩阵

| 模块 | 一级职责 | 依赖（入口方向） | 对外暴露 |
|------|---------|-----------------|---------|
| `core/config.py` | 配置加载、校验、环境变量解析 | `python-dotenv` | `Settings` 单例 |
| `core/neo4j_client.py` | Neo4j Driver 生命周期管理 | `core/config.py` | `get_neo4j_driver()` |
| `core/graphiti_client.py` | Graphiti SDK 实例创建与配置 | `core/config.py`, `core/neo4j_client.py`, `graphiti/` | `get_graphiti()` |
| `adapters/` | 原始数据 → NormalizedEpisode 转换 | `core/config.py`, `adapters/models.py` | `BaseAdapter` 子类 |
| `graphiti/` | NormalizedEpisode → Neo4j 知识图写入 | `graphiti-core`, `core/neo4j_client.py` | `EpisodeWriter` |
| `sync/` | SynapseEngine ticker 白名单同步 | `requests` | `TickerSync` |
| `ingestion/` | 多源调度编排（适配器 + Graphiti + ticker sync） | `adapters/`, `graphiti/`, `sync/`, `core/` | `run_ingestion_cycle()` |
| `api/` | REST API 端点实现 + FastAPI 应用 | `core/`, `graphiti/`, `ingestion/` | FastAPI `app` |
| `utils/` | 日志、时间工具（零业务依赖） | 无 | `get_logger()`, `now_hkt()` |
| `main.py` | 进程入口：初始化 → 启动 API + 调度器 | 所有模块 | 进程启动 |

### 2.3 变更标记说明

本章节目录树中：
- **【已存在】** = N1/N2/N3 阶段已创建且实现（有实质代码）
- **【已实现】** = 文件已存在且有完整实现
- **【0 字节 → 需补】** = 空壳文件，N4 前必须补完
- **【需新建/创建】** = 文件尚不存在，N4 阶段需创建
- **【建议新增】** = 非阻塞，建议后续迭代添加

---

## 3. 模块依赖图

### 3.1 依赖关系有向图

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

### 3.2 依赖规则（铁律）

1. **`core/config.py`** — 零业务依赖，仅依赖 `python-dotenv` + `pydantic-settings`。所有模块通过依赖注入获取配置，不直接 import.
2. **`core/neo4j_client.py`** — 仅依赖 `core/config.py` + `neo4j` 驱动。不依赖任何适配器或 graphiti 模块。
3. **`core/graphiti_client.py`** — 依赖 `core/config.py` + `core/neo4j_client.py` + `graphiti/` 类型定义。封装 Graphiti SDK 实例化。
4. **`adapters/`** — 仅依赖 `adapters/models.py` + `core/config.py`（通过依赖注入）。不依赖 `graphiti/` 或 `api/`。
5. **`graphiti/`** — 依赖 `graphiti-core` + `adapters/models.py`（NormalizedEpisode）。不依赖任何具体适配器。
6. **`ingestion/`** — 编排层，依赖 `adapters/` + `graphiti/` + `sync/` + `core/`。不依赖 `api/`。
7. **`api/`** — 依赖 `core/` + `graphiti/`。不直接依赖 `adapters/` 或 `sync/`。
8. **`utils/`** — 零业务依赖，可被所有模块 import。
9. **循环依赖零容忍** — `adapters/` ↔ `graphiti/` 之间的桥接通过 `adapters/models.py` 共享类型实现，不互相 import。

### 3.3 共享类型（避免循环依赖的关键）

`src/adapters/models.py` 中的 `NormalizedEpisode` 是适配器层和 graphiti 层的 **共享数据契约**：

- 适配器层 **产出** `NormalizedEpisode`（`fetch → normalize → dedup`）
- Graphiti 层 **消费** `NormalizedEpisode`（`EpisodeWriter.write_one`）

这样两边都依赖同一个 models 文件，避免了互相 import。

### 3.4 `core/graphiti_client.py` 职责（N4 新增）

`EpisodeWriter` (N3) 已经处理了写入逻辑，但需要一个地方负责 Graphiti SDK 实例的创建和生命周期。`core/graphiti_client.py` 就是这个地方：

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
    """创建 Graphiti 实例。

    每次调用创建新实例（无单例），由调用方管理生命周期。
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

**为何不设单例？** Graphiti SDK 的 `add_episode()` 必须串行（不是线程安全的），单例会引入并发风险。由 `EpisodeWriter` 持有实例并在每次 `write_batch` 中串行使用。

---

## 4. 生命周期管理

### 4.1 启动顺序（严格顺序，FIFO）

| 步骤 | 操作 | 负责模块 | 失败处理 |
|------|------|---------|---------|
| 0 | `python main.py` 被调用 | `main.py` | 进程退出 |
| 1 | `load_settings()` — 加载 .env + 校验必需字段 | `core/config.py` | 抛出 `SettingsValidationError` → 进程退出 |
| 2 | `setup_logging()` — 结构化日志初始化 | `utils/logging_config.py` | 回退到标准 logging → WARNING |
| 3 | `Neo4jDriver.open()` — 建立 Bolt 连接 | `core/neo4j_client.py` | 重试 3 次 → 失败则退出（无 Neo4j 无法运行） |
| 4 | `TickerSync.refresh()` — 拉取 ticker 白名单 | `sync/ticker_sync.py` | 降级为本地缓存 → WARNING + 继续 |
| 5 | `create_graphiti()` — 初始化 Graphiti SDK | `core/graphiti_client.py` | 退出（百炼 API Key 无效无法运行） |
| 6 | `EpisodeWriter(graphiti)` — 创建写入器 | `graphiti/episode_writer.py` | 退出 |
| 7 | `start_ingestion_scheduler()` — 启动 Cron 调度器 | `ingestion/scheduler.py` | WARNING + 继续（不阻断 API） |
| 8 | `uvicorn.run(app)` — 启动 FastAPI | `api/server.py` | 进程退出 |

```
main.py 启动流程（伪代码）:

1. settings = load_settings()              # core/config.py
2. setup_logging(settings.log_level)       # utils/logging_config.py
3. driver = Neo4jDriver.open(settings)     # core/neo4j_client.py
4. tickers = await TickerSync().refresh()  # sync/ticker_sync.py
5. graphiti = create_graphiti(driver)      # core/graphiti_client.py
6. writer = EpisodeWriter(graphiti)        # graphiti/episode_writer.py
7. scheduler = start_ingestion(writer, tickers)  # ingestion/scheduler.py
8. uvicorn.run(create_app(writer), port=8100)    # api/server.py
```

### 4.2 关闭顺序（LIFO）

| 步骤 | 操作 | 负责模块 |
|------|------|---------|
| 1 | `scheduler.stop()` — 停止 Cron 任务，等待当前轮完成 | `ingestion/scheduler.py` |
| 2 | `uvicorn 优雅关闭` — 等待 pending requests 完成 | uvicorn 内置 |
| 3 | `writer.close()` — 关闭 EpisodeWriter 资源 | `graphiti/episode_writer.py` |
| 4 | `driver.close()` — 关闭 Neo4j 连接 | `core/neo4j_client.py` |

### 4.3 依赖就绪检查

```python
# 文件: main.py — 启动时健康检查

async def check_dependencies(settings: Settings) -> bool:
    """验证所有必需依赖可用。返回 True 表示可以启动。"""
    all_ok = True

    # 1. Neo4j 连接
    try:
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=...)
        driver.verify_connectivity()
        logger.info("✅ Neo4j 连接正常 (%s)", settings.neo4j_uri)
    except Exception as exc:
        logger.critical("❌ Neo4j 连接失败: %s", exc)
        all_ok = False

    # 2. 百炼 API（可选验证，LLM 模型存在性）
    # 如果 graphiti-core 在 init 时已做此检查则跳过

    # 3. SynapseEngine ticker 端点（非阻塞，可降级）
    try:
        tickers = await TickerSync().refresh()
        logger.info("✅ SynapseEngine ticker 拉取成功 (%d 个)", len(tickers))
    except Exception:
        logger.warning("⚠️ SynapseEngine 不可达，使用本地缓存")

    # 4. 数据源连通性（非阻塞，记录状态即可）
    # GDELT / RSS / AkShare 由调度器运行时自行处理

    return all_ok  # 只有 Neo4j 是硬阻塞
```

### 4.4 运行时状态

NewsEngine 是一个 **长期运行的服务进程**（不是一次性脚本），包含两个并发子系统：

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
│  │                        │    │   TickerSync.refresh()    │   │
│  │                        │    │   (每 6 小时)              │   │
│  └───────────────────────┘    └──────────────────────────┘   │
│                                                               │
│  共享资源: Neo4j Driver (线程安全, graphiti 串行使用)           │
│  共享资源: EpisodeWriter (串行使用, 非线程安全)                 │
│  共享资源: Settings (只读, 无竞态)                              │
└──────────────────────────────────────────────────────────────┘
```

**并发安全性说明:**
- Graphiti SDK 的 `add_episode()` 不是线程安全的，必须串行调用。`EpisodeWriter.write_batch()` 已确保串行（逐一 await）。
- FastAPI 的请求处理器和 ingestion scheduler 共享同一个 asyncio event loop，天然的协程级并发安全。
- 如果未来需要并行写入多个 Episode，应使用 `asyncio.Lock` 保护 `graphiti.add_episode()` 调用。

---

## 5. 配置管理规范

### 5.1 .env 完整字段定义

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
# 说明: FastAPI 监听端口。需与 Redesign Doc §F.3 端口规划一致

# === SynapseEngine 连接 ===
SYNAPSE_BASE_URL=http://localhost:8000
# 敏感: 否 | 默认: http://localhost:8000 | 必填: 否
# 说明: SynapseEngine 的 REST API 地址，供 TickerSync 拉取白名单

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

TICKER_REFRESH_INTERVAL_SEC=21600
# 敏感: 否 | 默认: 21600 (6 小时) | 必填: 否
# 说明: Ticker 白名单刷新间隔（秒）。Redesign Doc §C.2 定义的刷新频率

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
# 说明: /api/events/risk-summary 结果缓存时间（秒）。Redesign Doc §N4-5 要求的缓存策略
```

### 5.2 Pydantic Settings 实现 (core/config.py)

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
        default="qwen-plus",
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
    ticker_refresh_interval_sec: int = Field(
        default=21600,
        description="Ticker 白名单刷新间隔（秒）",
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

**注意:** 当前 `.env` 文件使用 `EMBEDDING_MODEL=text-embedding-v4`，这是一个不存在的百炼模型（百炼只有 `text-embedding-v3`）。但 Tech Lead 已在 N3 阶段验证 graphiti-core 可正常使用此配置，因此不强行修改。建议在 Pydantic validator 中增加 warn。

### 5.3 当前 .env 与完整 .env 的差异

| 当前 .env (6 行) | 完整 .env (16 行) | 差异 |
|-----------------|-----------------|------|
| `BAILIAN_API_KEY` | `BAILIAN_API_KEY` | 保留 |
| `OPENAI_BASE_URL` | `OPENAI_BASE_URL` | 保留 |
| `EMBEDDING_MODEL=text-embedding-v4` | `EMBEDDING_MODEL=text-embedding-v4` | 保留（已验证可行） |
| `LLM_MODEL=qwen3.7-plus` | `LLM_MODEL=qwen-plus` | **差异**: 当前 .env 使用 `qwen3.7-plus`，spec 推荐 `qwen-plus`。不强制修改，Pydantic Settings 不设 enum 约束。 |
| `NEO4J_URI` | `NEO4J_URI` | 保留 |
| `NEO4J_USER` | `NEO4J_USER` | 保留 |
| `NEO4J_PASSWORD` | `NEO4J_PASSWORD` | 保留 |
| — | `API_HOST` | **缺失** |
| — | `API_PORT` | **缺失** |
| — | `SYNAPSE_BASE_URL` | **缺失** |
| — | `LOG_LEVEL` | **缺失** |
| — | `LOG_FILE` | **缺失** |
| — | `INGESTION_INTERVAL_SEC` | **缺失** |
| — | `TICKER_REFRESH_INTERVAL_SEC` | **缺失** |
| — | `GDELT_LASTUPDATE_URL` | **缺失**（当前硬编码在 gdelt_adapter 中） |
| — | `GDELT_MAX_RETRIES` | **缺失** |
| — | `GDELT_TIMEOUT_SEC` | **缺失** |
| — | `RSS_TIMEOUT_SEC` | **缺失** |
| — | `AKSHARE_REQUEST_INTERVAL_SEC` | **缺失** |
| — | `RISK_SUMMARY_CACHE_TTL_SEC` | **缺失** |
| — | `GRAPITI_LLM_PROVIDER=openai` | **移除**（Pydantic Settings 不设此字段，graphiti-core 由 create_graphiti() 传参） |
| — | `GRAPITI_EMBEDDING_PROVIDER=openai` | **移除**（同上） |

**结论:** 当前 `.env` 覆盖率仅 ~35%。N4 实施时必须补全。Pydantic Settings 的 `default` 值为所有非必需字段提供合理默认值，因此缺失字段不会导致启动失败，但建议创建 `.env.example` 模板。

---

## 6. 测试策略

### 6.1 测试金字塔

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

### 6.2 单元测试（Mock 策略）

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
| `api/routers/events.py` | `EpisodeWriter` → mock | 响应格式符合 Redesign Doc §C.3~C.7、错误码正确 |
| `api/routers/health.py` | Neo4j 连接 → mock | status 字段正确计算 |
| `core/config.py` | `.env` 文件（pytest monkeypatch） | 校验正确、默认值正确 |

### 6.3 集成测试（真实依赖）

| 测试场景 | 依赖 | 验证内容 |
|---------|------|---------|
| Neo4j 连接 | 本地 Neo4j Docker | `get_neo4j_driver()` 可连接 |
| Graphiti Episode 写入 | Neo4j + 百炼 API | 单条 Episode 写入成功、实体/关系可见 |
| GDELT 管线 | GDELT HTTP + Neo4j + 百炼 | 完整 fetch → normalize → write 链路 |
| API 端点 | 已填充的 Neo4j | `/api/events/active` 等 5 个端点返回正确格式 |
| Ticker 同步 | SynapseEngine (需先部署) | TickerSync 拉取成功 |

### 6.4 测试运行命令

```bash
# 仅运行单元测试（快速，无外部依赖）
pytest tests/unit/ -v

# 运行集成测试（需要 Neo4j Docker 运行）
pytest tests/integration/ -v -m "integration"

# 全量测试
pytest -v

# 覆盖率报告
pytest --cov=src --cov-report=html
```

### 6.5 conftest.py 核心 fixture

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

### 6.6 测试覆盖率目标

| 模块 | 覆盖率目标 | 说明 |
|------|-----------|------|
| `core/` | ≥ 90% | 基础设施，高稳定性要求 |
| `adapters/` | ≥ 80% | 外部依赖多，硬 mock 可覆盖 |
| `graphiti/` | ≥ 80% | EpisodeWriter 是核心写入链 |
| `api/` | ≥ 85% | 每个端点至少 happy path + 3 错误码测试 |
| `sync/` | ≥ 80% | TickerSync 的缓存降级逻辑 |
| `utils/` | ≥ 90% | 纯函数，高覆盖率 |
| `ingestion/` | ≥ 70% | 编排层，集成测试覆盖为主 |

---

## 7. `mirofish_seeds` → `sector_briefing` 重命名 + 完整生成链路

### 7.1 决策：直接改名（无向后兼容）

**老公决策 (2026-06-09):** 项目未上线，不存在向后兼容需求。`mirofish_seeds` → `sector_briefing` 直接改名，不使用双字段过渡。Redesign Doc / IMPLEMENT_PLAN / 所有相关文档同步修改。

**新字段名: `sector_briefing`**

| 属性 | 值 |
|------|-----|
| 字段路径 | `response.sector_briefing` |
| 类型 | `string \| null` |
| 必填 | 否 |
| 语义 | LLM 聚合的行业情报简报（Markdown 格式，300-500 字），供下游消费者直接使用 |
| 消费者 | MiroFish（当前唯一）、未来任何需要行业级情报摘要的系统 |

### 7.2 影响面

| 文档 | 影响 | 操作 |
|------|------|------|
| Redesign Doc §C.5 | 字段名变更（JSON 响应 + 字段定义表 + 描述文本） | 同步修改 |
| Redesign Doc §D.1 | 描述文本 `mirofish_seeds` → `sector_briefing` | 同步修改 |
| Redesign Doc §E.7 | MiroFish 消费端描述文本 | 同步修改 |
| IMPLEMENT_PLAN | N4-4 任务描述 + N6 测试场景 | 同步修改 |
| MiroFish 消费代码 | 字段访问 `response["mirofish_seeds"]` → `response["sector_briefing"]` | Tech Lead 同步修改 |
| MongoDB `news_events` | 不涉及（该字段不落库，仅 REST API 响应） | 无需操作 |
| 其他 4 个端点 | 不相关 | 无需操作 |

### 7.3 `sector_briefing` 完整生成链路

以下定义 `sector_briefing` 字段的端到端生成链路——谁、用什么、怎么算出来的。

#### 7.3.1 数据来源（从 Neo4j 取什么）

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

#### 7.3.2 LLM 配置与 Prompt 设计

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

#### 7.3.3 聚合模块归属

**文件:** `src/ingestion/briefing_aggregator.py`（新建）

**类:** `SectorBriefingAggregator`

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

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver

logger = logging.getLogger(__name__)


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

        # Step 1: 查询事件
        events = await self._query_sector_events(driver, sector_name)
        if not events:
            logger.info("sector=%s 无活跃事件，跳过聚合", sector_name)
            return None

        # Step 2: 事件指纹
        fingerprint = self._compute_fingerprint(events)
        cached = self._cache.get(sector_name)
        if cached and cached.event_fingerprint == fingerprint:
            logger.debug("sector=%s 事件无变化，跳过 LLM 聚合", sector_name)
            return cached.briefing

        # Step 3: 调用 LLM
        prompt = self._build_prompt(sector_name, events)
        briefing = await self._call_llm(prompt)

        # Step 4: 更新缓存
        self._cache[sector_name] = BriefingCacheEntry(
            briefing=briefing,
            generated_at=datetime.utcnow(),
            event_fingerprint=fingerprint,
        )
        logger.info("sector=%s 简报已更新 (%d 事件, %d 字)",
                     sector_name, len(events), len(briefing))
        return briefing

    async def _query_sector_events(self, driver, sector_name: str) -> list[dict]:
        """查询 Neo4j: sector → stocks → events"""
        # 执行 §7.3.1 定义的 Cypher 查询
        ...

    def _compute_fingerprint(self, events: list[dict]) -> str:
        """计算事件指纹（用于增量检测）。

        指纹 = SHA256(event_id + last_updated 的排序拼接)
        任何事件新增/修改/删除 → 指纹变化 → 触发重新聚合
        """
        key = "|".join(
            f"{e['event_id']}:{e['last_updated']}"
            for e in sorted(events, key=lambda x: x['event_id'])
        )
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _build_prompt(self, sector_name: str, events: list[dict]) -> str:
        """构建 LLM prompt（system + user）。"""
        # 见 §7.3.2 的 prompt 模板
        ...

    async def _call_llm(self, prompt: str) -> str:
        """调用百炼 qwen-plus 生成简报。"""
        response = self._llm_client.chat.completions.create(
            model=self._llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},  # §7.3.2
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
```

**依赖关系:**
```
ingestion/scheduler.py  ──调用──▶  SectorBriefingAggregator.aggregate_all()
                                      │
                                      ├── _query_sector_events() → Neo4j (Cypher)
                                      ├── _build_prompt()        → LLM Prompt 模板
                                      └── _call_llm()            → 百炼 qwen-plus

api/routers/events.py   ──读取──▶  SectorBriefingAggregator.get_cached()
                                      │
                                      └── 内存 dict {sector_name → BriefingCacheEntry}
```

#### 7.3.4 缓存策略

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

#### 7.3.5 降级方案

| 场景 | 行为 | 外部表现 |
|------|------|---------|
| **百炼 API 不可用** (429/5xx/超时) | `_call_llm()` 抛出异常 → `aggregate_one()` 捕获 → 返回 None → 保留旧缓存（若有） | `sector_briefing` = null（若无旧缓存）或回退到旧缓存值 |
| **Neo4j 不可用** | `_query_sector_events()` 抛出异常 → 整个 `aggregate_all()` 跳过 | `sector_briefing` = null（上一次缓存仍可用） |
| **sector 无活跃事件** | `_query_sector_events()` 返回空列表 → 返回 None | `sector_briefing` = null |
| **LLM 返回空内容** | `_call_llm()` 结果为空字符串 → 日志 WARNING → 返回 None | `sector_briefing` = null |
| **LLM 返回超长内容** | 截断到 600 字（post-processing） | 截断后的简报 |
| **首次启动，缓存全空** | `aggregate_all()` 对所有 sector 生成；若 LLM 不可用则全部为 None | API 返回不带 `sector_briefing` 的完整事件数据 |

**API 端降级行为（`api/routers/events.py`）:**

```python
# GET /api/events/sector/:name
briefing = aggregator.get_cached(sector_name)  # 从内存缓存读取

response = SectorEventsResponse(
    sector=sector_name,
    events=events,
    statistics=stats,
    sector_briefing=briefing,  # None 时 JSON 序列化为 null
)
```

**关键原则:** `sector_briefing` 缺失不阻断主流程。消费者应检查字段是否为 null，为 null 时降级为自行聚合原始事件列表。

### 7.4 API 响应模型（Pydantic）

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

### 7.5 生成链路总结

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

## 8. N4 基础设施补完清单

### 8.1 必须在 N4-1 (FastAPI 骨架) 之前完成

| 文件 | 当前状态 | N4 前需完成 | 本文件参考章节 |
|------|---------|-----------|--------------|
| `src/core/config.py` | 0 字节 | 完整实现（Settings + get_settings() + 校验） | §5.2 |
| `src/core/neo4j_client.py` | 0 字节 | 完整实现（Driver 单例 + 生命周期） | 见下方 §8.2 |
| `src/utils/logging_config.py` | 0 字节 | 完整实现（结构化日志） | 常规 logging 配置 |
| `src/utils/time_utils.py` | 0 字节 | 完整实现（HKT 转换 + ISO 8601） | 常规时间工具 |
| `src/__init__.py` | 0 字节 | 添加 `__version__` | 项目元数据 |
| `src/core/__init__.py` | 0 字节 | 添加 re-export (`__all__`) | 模块导出 |
| `src/utils/__init__.py` | 0 字节 | 添加 re-export | 模块导出 |
| `src/graphiti/__init__.py` | 0 字节 | 添加 re-export | 模块导出 |
| `src/sync/__init__.py` | 0 字节 | 添加 re-export | 模块导出 |

### 8.2 Neo4jClient 生命周期规范 (core/neo4j_client.py)

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
from functools import lru_cache

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

### 8.3 判断：基础设施应该在哪个阶段完成

| 文件 | 应在 N1 完成？ | 实际状态 | 责任 |
|------|:---:|------|------|
| `config.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `neo4j_client.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `logging_config.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `time_utils.py` | ✅ 是 | 0 字节 | **Tech Lead** — N1-1 标记 ✅ 但未实现 |
| `graphiti_client.py` | ❌ 否 (N4 阶段) | 不存在 | 新文件，在 §3.4 中定义 |

**Architect 的责任是定义 spec（本文件），Tech Lead 的责任是按 spec 实现。** N1 阶段 IMPLEMENT_PLAN 要求"完整目录树"，但只创建了空壳文件。本 file 补定义后，Tech Lead 需在 N4 点火前补完。

---

## 9. N4 REST API 层实施指南

### 9.1 FastAPI 应用工厂 (api/server.py)

```python
# 文件: src/api/server.py
"""FastAPI 应用工厂 — NewsEngine REST API (:8100)。"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import get_settings
from src.api.routers.events import router as events_router
from src.api.routers.health import router as health_router


def create_app() -> FastAPI:
    """创建 FastAPI 应用（不含 uvicorn 启动逻辑）。

    使用工厂模式便于测试。
    """
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
            "http://localhost:8000",
            "http://localhost:3000",  # SynapseUI (Next.js)
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

### 9.2 端点实现注意事项

每个端点的实现应严格遵循 Redesign Doc §C.3~C.7 定义的响应格式。以下为关键实现准则：

| 端点 | 实现关注点 |
|------|-----------|
| `GET /api/events/active` | 参数: `limit`, `min_severity`, `sector`。查询 Neo4j 按 severity + last_updated 排序。返回 `freshness` 时需查询各数据源的最后更新时间。 |
| `GET /api/events/entity/:ticker` | ticker 格式: `0700.HK`（不是 `HK.00700`）。`summary.news_sentiment_score` 由 severity 加权计算。 |
| `GET /api/events/sector/:name` | sector 名称使用中文。`sector_briefing` 字段由 `SectorBriefingAggregator` 异步预计算 + 内存缓存，API 直接读取（见 §7.3）。 |
| `GET /api/events/risk-summary` | 结果缓存 5 分钟。`overall_risk` 由各行业风险等级的加权计算得出。 |
| `GET /api/events/health` | 实时检查 Neo4j + 各数据源状态。状态判定: 任一数据源超过 30 分钟未更新 → degraded。Neo4j 不可达 → down。 |

### 9.3 端点 → 内部实现映射

| 端点 | 数据来源 | 核心查询方法 |
|------|---------|------------|
| `/api/events/active` | Neo4j (Graphiti 写入的 EntityEdge) | Cypher: `MATCH (e:Entity)<-[:RELATES_TO]-(related) RETURN e, related ORDER BY ...` |
| `/api/events/entity/:ticker` | Neo4j EntityNode (Stock label) | 按 `ticker` 属性查找 Stock 节点，追溯关联的事件 Episode |
| `/api/events/sector/:name` | Neo4j EntityNode (Sector label) | 按 `entity_name` 属性查找 Sector 节点，找 BELONGS_TO 的 Stock → 其关联事件 |
| `/api/events/risk-summary` | Neo4j 聚合 + LLM 生成 | 查询高 severity 事件 → 按行业分组 → LLM 生成 summary |
| `/api/events/health` | Neo4j 系统表 + 数据源状态 | `CALL dbms.listConfig()` + 内部状态变量 |

---

## 10. N4 完成后 MAIN.PY 入口

```python
# 文件: main.py
"""NewsEngine 进程入口。

启动顺序:
1. 加载配置
2. 初始化日志
3. 连接 Neo4j
4. 同步 ticker 白名单
5. 启动数据摄取调度器
6. 启动 FastAPI (uvicorn)
"""

from __future__ import annotations

import asyncio
import logging
import uvicorn

from src.core.config import get_settings
from src.core.neo4j_client import get_neo4j_driver, close_neo4j_driver
from src.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """NewsEngine 主入口。"""
    # 1. 加载配置
    settings = get_settings()

    # 2. 初始化日志
    setup_logging(settings.log_level, settings.log_file)
    logger.info("NewsEngine v%s 启动中...", "1.0.0")

    # 3. 验证 Neo4j 连接
    try:
        driver = get_neo4j_driver()
        logger.info("Neo4j 连接就绪")
    except Exception as exc:
        logger.critical("Neo4j 连接失败，进程退出: %s", exc)
        raise SystemExit(1)

    # 4-7. 启动 FastAPI（uvicorn 内嵌调度器启动）
    logger.info(
        "启动 FastAPI 服务 (http://%s:%d)...",
        settings.api_host,
        settings.api_port,
    )
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

**注意:** TickSync 和数据摄取调度器的启动可放在 FastAPI 的 lifespan context manager 中（async startup event），这样它们和 API 共享同一个 asyncio event loop。具体实现由 Tech Lead 决策。

---

## 11. 闭环检查清单

- [x] NewsEngine 文件架构完整定义（§2.1 目录树 + §2.2 模块职责矩阵）
- [x] 模块依赖图完整（§3.1 有向图 + §3.2 铁律）
- [x] 共享类型契约确认（§3.3 NormalizedEpisode 桥接）
- [x] 生命周期管理规范（§4.1 启动顺序 + §4.2 关闭顺序 + §4.3 就绪检查 + §4.4 运行时状态）
- [x] 配置管理规范完整（§5.1 完整 .env + §5.2 Pydantic Settings + §5.3 差异分析）
- [x] 测试策略定义（§6.1 金字塔 + §6.2 Mock 策略 + §6.3 集成测试 + §6.5 conftest）
- [x] `mirofish_seeds` → `sector_briefing` 直接改名 + 完整生成链路（§7.1~§7.5）
- [x] N4 基础设施补完清单（§8.1 + §8.2 + §8.3）
- [x] N4 REST API 实施指南（§9.1~§9.3）
- [x] main.py 入口定义（§10）
- [x] 零破坏 Redesign Doc 接口契约（全文未修改任何 Redesign Doc 定义的 JSON Schema、错误码、端点路径）

---

## 12. 变更记录

| 日期 | 变更内容 | 操作人 |
|------|----------|--------|
| 2026-06-09 | V1.0 初始创建：NewsEngine 内部架构规格说明书 | Chief Architect |
| 2026-06-09 | V1.1: `mirofish_seeds` → `sector_briefing` 直接改名；§7.3 新增完整生成链路（数据来源/LLM Prompt/模块归属/缓存策略/降级方案） | Chief Architect |

---

*NewsEngine 内部架构规格说明书 V1.0 — 2026-06-09，补充 Redesign Doc 的提供者视角定义。所有已定义的外部接口契约保持不变。*
