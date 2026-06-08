# n1-skeleton-infrastructure Design

## 1. Technical Design

### 技术上下文

| 维度 | 选型 | 版本/说明 |
|------|------|-----------|
| **运行时** | Python | 3.11+（项目虚拟环境 `.venv` 已存在） |
| **图数据库** | Neo4j | 5 Community Edition (Docker) |
| **知识图谱框架** | Graphiti Core | ≥0.29.2, <1.0 |
| **LLM 后端** | 阿里百炼 API | OpenAI 兼容模式 |
| **API 框架** | FastAPI | ≥0.115.0, <1.0 |
| **ASGI 服务器** | Uvicorn | ≥0.30.0, <1.0 |
| **数据验证** | Pydantic | ≥2.11.5, <3.0 |
| **配置管理** | python-dotenv | ≥1.0.1, <2.0 |
| **重试机制** | Tenacity | ≥9.0.0, <10.0 |
| **HTTP 客户端** | httpx + requests | httpx for async, requests for sync |
| **测试** | pytest + pytest-asyncio | ≥8.0.0, <9.0 |

**技术红线**：
- 禁止在代码中硬编码密钥/凭据
- 禁止直接操作 Neo4j 数据卷文件（所有数据操作通过 Bolt 协议）
- 禁止修改预存在的 `.env`、`docker-compose.yml`、`test_graphiti_episode.py`

### 目录结构决策

```
src/
├── adapters/        # 数据源适配器层 — GDELT / RSS / akshare 数据拉取
├── graphiti/        # Graphiti 集成层 — entity/relation 类型定义 + episode 写入
├── api/             # REST API 层 — FastAPI server + routers
├── sync/            # 同步调度层 — 定时任务 ticker
├── core/            # 核心配置层 — config 加载 + neo4j 客户端
└── utils/           # 工具层 — 日志 + 时间工具
```

**设计理由**：
- `adapters/`、`graphiti/`、`api/` 三层独立，互不耦合，可并行开发
- `core/` 提供共享基础设施（config、neo4j 连接），其他模块依赖它
- `sync/` 作为编排层，调用 adapters + graphiti 完成数据流水线
- 当前阶段所有业务文件为空占位，防止 import 错误的同时建立清晰包结构

### Neo4j 拓扑

```
┌──────────────────────────────────────┐
│          Docker Host (WSL2)          │
│                                      │
│  ┌────────────────────────────────┐  │
│  │  Container: newsengine-neo4j   │  │
│  │  Image: neo4j:5-community      │  │
│  │                                │  │
│  │  Port 7474 → HTTP/Browser      │  │
│  │  Port 7687 → Bolt Protocol     │  │
│  │                                │  │
│  │  JVM Heap: 512M initial, 2G max│  │
│  │  Page Cache: 512M              │  │
│  │                                │  │
│  │  Volumes:                      │  │
│  │    ./data/neo4j/data → /data   │  │
│  │    ./data/neo4j/logs → /logs   │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
```

**配置反馈**：现有 `docker-compose.yml` 已包含所需全部 neo4j 配置（容器名、端口、JVM 参数、volume 挂载、healthcheck、restart 策略），无需修改，仅需确认启动。

### API 契约

本 change 不涉及 API 端点。`api/` 目录下文件为空占位，后续 change 实现。

### 数据流

本 change 无运行时数据流，仅建立文件和基础设施骨架。数据流将在 adapters → graphiti → neo4j 流水线实现后定义。

## 2. Interaction Design — reserved for UX Designer

## 3. Visual Design System — reserved for UX Designer
