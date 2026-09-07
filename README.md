<p align="center">
  <h1 align="center">📡 NewsEngine</h1>
  <p align="center">
    <strong>金融事件情报系统 — 从多源噪音到结构化知识图谱</strong>
  </p>
  <p align="center">
    <em>Multi-Source Financial News → Knowledge Graph → Event Intelligence</em>
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Neo4j-5.x-green?logo=neo4j&logoColor=white" alt="Neo4j">
  <img src="https://img.shields.io/badge/Graphiti-0.29-orange" alt="Graphiti">
  <img src="https://img.shields.io/badge/License-MIT-blue" alt="License">
</p>

---

## 项目简介

NewsEngine 是一个**金融事件情报系统**，从全球多个数据源实时采集新闻，通过 LLM 抽取实体与关系，构建知识图谱，为下游决策系统提供**事件上下文**。

与传统的新闻聚合器不同，NewsEngine 的核心价值在于**关系网络**——通过知识图谱构建**宏观-行业-个股**的事件脉络，让用户能看到完整的事件链路，而不仅仅是扁平的新闻列表。

---

## 核心业务场景

### 场景一：行业事件推演

> **目标**：查询某个行业时，能看到相关的宏观事件和个股反应，形成完整的事件链路。

```
查询：纺织行业

├── 宏观事件：India's textiles sector gains tariff advantage
├── 行业影响：纺织出口企业受益于关税优势
├── 个股反应：鲁泰A 涨停
└── 时间线：宏观政策 → 行业影响 → 个股表现
```

系统通过共享的 `Sector` 实体桥接不同来源的事件：

```
宏观新闻 → 提取 Sector "纺织行业"
                    ↓ 共享实体
个股新闻 → 提取 Sector "纺织行业"
                    ↓
Stock "鲁泰A" → BELONGS_TO → Sector "纺织行业"
```

### 场景二：个股事件上下文

> **目标**：查询某只股票时，能看到与之相关的宏观事件、行业动态，而不仅仅是个股公告。

```
查询：000858.SZ（五粮液）

├── 个股事件：五粮液财报发布、涨停
├── 行业动态：白酒行业政策、消费趋势
├── 宏观背景：中国消费刺激政策、贸易关税
└── 关联图：五粮液 → 白酒 → 中国
```

API 返回图结构，包含节点和关系：

```json
{
  "ticker": "000858.SZ",
  "graph": {
    "nodes": [
      {"id": "五粮液", "type": "Stock"},
      {"id": "白酒", "type": "Sector"},
      {"id": "中国", "type": "Country"}
    ],
    "edges": [
      {"source": "五粮液", "target": "白酒", "type": "BELONGS_TO"},
      {"source": "白酒", "target": "中国", "type": "HAPPENED_IN"}
    ]
  },
  "episodes": [...]
}
```

### 场景三：事件脉络追踪

> **目标**：追踪某个事件的传播链路和连锁反应。

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

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          DATA SOURCES                                │
│  🌍 GDELT    📰 RSS    📱 CLS    📋 CNInfo    💹 EastMoney    📊 AkShare  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       ADAPTER LAYER                                  │
│  多源适配 → 内容抓取 → 标准化输出 → NormalizedEpisode                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     CONTENT FETCHER (5-Tier Funnel)                  │
│  静态提取 → Chrome → Alt TLS → CloakBrowser → Camoufox → Fallback   │
│  渐进式反爬绕过，Cookie 池复用                                       │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   INGESTION PIPELINE                                 │
│  Landing Zone → IngestWorker → EpisodeWriter                        │
│  • Content-hash 去重                                                │
│  • Entity 名称归一化                                                │
│  • Sector 语义准入                                                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    KNOWLEDGE GRAPH (Graphiti + Neo4j)                │
│  • LLM 实体/关系抽取                                                │
│  • 8 种核心关系类型                                                  │
│  • 实体去重 + 社区检测                                              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         API LAYER (FastAPI)                          │
│  GET /api/events/active          — 活跃事件                         │
│  GET /api/events/entity/{ticker} — 股票相关事件 + 图结构            │
│  GET /api/events/sector/{name}   — 行业事件聚合                     │
│  GET /api/events/risk-summary    — LLM 风险摘要                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 数据源

| 来源 | 覆盖范围 | 内容类型 |
|------|----------|----------|
| 🌍 **GDELT** | 全球 | 地缘政治事件、冲突、外交 |
| 📰 **RSS** | 全球 | 金融新闻（Dow Jones, FT, EIA, mining.com）|
| 📱 **CLS Telegraph** | 中国 | 财联社实时快讯 |
| 📋 **CNInfo** | 中国/港交所 | 巨潮资讯官方公告 |
| 💹 **EastMoney** | 中国 A 股 | 个股新闻、分析师研报 |
| 📈 **AkShare** | 中国 | 股票行情、财务指标 |
| 💵 **Treasury** | 美国 | 国债收益率曲线 |

---

## 知识图谱设计

### 核心关系类型

| 关系 | 语义 | 示例 |
|------|------|------|
| `RELATES_TO` | 通用关联 | 事件 A 与事件 B 相关 |
| `BELONGS_TO` | 归属关系 | 股票 → 行业 |
| `PART_OF` | 结构组成 | 子公司 → 母公司 |
| `TRADED_ON` | 上市地点 | 股票 → 交易所 |
| `HAPPENED_IN` | 地点归属 | 事件 → 国家 |
| `INVOLVES` | 主体参与 | 人物 → 组织 |
| `AFFECTS` | 影响关系 | 政策 → 行业 |
| `TRIGGERS` | 因果关系 | 事件 A → 事件 B |

### 实体类型

- **Stock** — 股票（带 ticker）
- **Sector** — 行业/板块
- **Organization** — 公司/机构
- **Person** — 人物
- **Country** — 国家/地区
- **Policy** — 政策/法规
- **Event** — 事件
- **Topic** — 主题/概念

---

## 快速开始

### 环境要求

- Python 3.12+
- Neo4j 5.x
- LLM API（OpenAI 兼容 / DeepSeek / Gemini）

### 安装

```bash
git clone https://github.com/your-org/newengine.git
cd NewsEngine

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=***

# LLM Provider (OpenAI 兼容)
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=***
LLM_MODEL=qwen3.7-plus

# Embedding
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=***
EMBEDDING_MODEL=text-embedding-v4
```

### 运行

```bash
# 正常模式
python main.py

# 验证模式（不写 Neo4j）
python main.py --dry-run

# 指定数据源
python main.py --source gdelt,rss
```

---

## API 使用

### 查询股票相关事件

```bash
curl http://localhost:8000/api/events/entity/000858.SZ
```

响应：

```json
{
  "ticker": "000858.SZ",
  "events": [
    {
      "event_id": "ep_123",
      "title": "五粮液发布年报",
      "summary": "...",
      "severity": "medium",
      "valid_at": "2026-09-07T10:00:00+08:00",
      "entities": [
        {"name": "五粮液", "type": "Stock", "ticker": "000858.SZ"},
        {"name": "白酒", "type": "Sector"}
      ]
    }
  ],
  "graph": {
    "nodes": [...],
    "edges": [...]
  }
}
```

### 查询行业事件

```bash
curl http://localhost:8000/api/events/sector/白酒
```

### 查询活跃事件

```bash
curl http://localhost:8000/api/events/active?limit=20&min_severity=medium
```

---

## 测试

```bash
# 全部测试
pytest

# 带覆盖率
pytest --cov=src

# 指定模块
pytest tests/test_adapters/
pytest tests/test_graphiti/
```

---

## 设计原则

1. **桥接优先** — 宏观、行业、个股事件通过共享实体关联，形成完整脉络
2. **图结构优先** — API 返回节点+关系的图结构，而非扁平列表
3. **Ticker 为核心索引** — 股票查询依赖 ticker，保证覆盖率
4. **数据质量优先** — 宁可少做功能，也要保证图谱质量

---

## 技术栈

| 组件 | 用途 |
|------|------|
| [Graphiti](https://github.com/getzep/graphiti) | 知识图谱 SDK |
| [Neo4j](https://neo4j.com/) | 图数据库 |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | 网页抓取 |
| [Camoufox](https://github.com/daijro/camoufox) | 反检测浏览器 |
| [FastAPI](https://fastapi.tiangolo.com/) | API 框架 |

---

## License

MIT License. See [LICENSE](LICENSE).

---

<p align="center">
  <strong>从噪音到决策，构建金融事件的完整脉络</strong>
</p>
