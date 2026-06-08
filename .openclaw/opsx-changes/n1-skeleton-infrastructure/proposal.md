# Proposal: n1-skeleton-infrastructure

## Intent
NewsEngine 项目尚处于空白状态，缺少标准项目骨架和运行时基础设施，开发工作无法开展。需要一次性搭好目录结构、Neo4j 数据库、Python 依赖三大支柱，确保后续功能开发有统一入口和运行环境。

## Scope

**In scope:**
- 完整目录树创建（src 业务代码 + tests 测试 + data 数据卷 + docs 文档）
- 占位 `__init__.py` 和空模块文件，建立 Python 包体系
- Neo4j 容器（newsengine-neo4j）通过 docker-compose 启动并验证可用
- `requirements.txt` 依赖声明 + pip install 验证
- `.env.example` 脱敏模板生成
- `.gitignore` 安全排除文件生成
- `docs/` 目录放置实现计划文档
- QA 验证所有验收点真实通过（非 mock）

**Out of scope:**
- 任何业务逻辑实现（adapter、graphiti、API、sync 模块仅占位）
- API server 启动与路由注册
- Graphiti episode 写入逻辑
- 数据源对接（GDELT / RSS / akshare）
- CI/CD pipeline
- 前端 UI

## Approach

**技术选型**：
- Neo4j 5 Community Edition 作为图数据库底座
- Graphiti Core 作为知识图谱构建框架
- FastAPI + Uvicorn 作为 REST API 层
- 阿里百炼 API（OpenAI 兼容模式）作为 LLM / Embedding 后端

**架构决策**：
- 分模块设计：adapters（数据接入）/ graphiti（图谱构建）/ api（接口层）/ sync（同步调度）/ core（配置与连接）/ utils（工具层）
- 数据持久化到 `./data/neo4j/`，由 docker-compose volume 挂载
- 环境配置通过 `.env` + `python-dotenv` 管理，不硬编码
- 所有业务模块以空文件占位，待后续 change 逐个实现

**关键依赖**：
- docker-compose.yml 已存在且配置正确，无需修改
- `.env` 已存在且包含有效凭据，不修改
- `test_graphiti_episode.py` 已存在且可用，不修改

## Capabilities
- 新能力：skeleton-infrastructure

## Impact
- 受影响的代码/模块：N/A（greenfield，无现有代码）
- API 变更：无
- 依赖变更：新增 `requirements.txt`（首次定义 Python 依赖）
- 数据库变更：Neo4j 5 容器初始化（空数据库，无 schema）
