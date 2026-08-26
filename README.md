<p align="center">
  <h1 align="center">📡 NewsEngine</h1>
  <p align="center">
    <strong>Multi-Source Financial News Ingestion → Knowledge Graph</strong>
  </p>
  <p align="center">
    <em>From raw noise to structured intelligence, automatically.</em>
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Neo4j-5.x-green?logo=neo4j&logoColor=white" alt="Neo4j">
  <img src="https://img.shields.io/badge/License-Private-orange" alt="License">
  <img src="https://img.shields.io/badge/Status-Production%20Ready-success" alt="Status">
</p>

---

## 🎯 What It Does

NewsEngine is a **real-time financial news pipeline** that:

1. **Ingests** from 9 global/regional sources (GDELT, RSS, CLS, CNInfo, EastMoney, AkShare, Treasury...)
2. **Bypasses** anti-bot protections with a 5-tier fetch funnel (Chrome → CloakBrowser → Camoufox)
3. **Extracts** entities & relationships using LLMs (Qwen/DeepSeek/Gemini)
4. **Builds** a live knowledge graph in Neo4j via Graphiti SDK

```
Raw News ──► 5-Tier Fetch ──► LLM Extraction ──► Knowledge Graph
   │              │                  │                   │
   │         Cloudflare?        Entity/Relation      Neo4j + Graphiti
   │         No problem.        Auto-dedup           Real-time queries
   │                                                      │
   └──────────────────────────────────────────────────────┘
                        Closed Loop
```

---

## ✨ Why It's Different

| Feature | Traditional Scrapers | NewsEngine |
|---------|---------------------|------------|
| **Anti-bot bypass** | Manual proxy rotation | 5-tier progressive funnel with cookie pooling |
| **Content extraction** | Regex / XPath | Tier 0 static + Trafilatura heuristic |
| **Entity extraction** | Rule-based NER | LLM-powered (Qwen/DeepSeek/Gemini) |
| **Knowledge graph** | Static DB dumps | Real-time Neo4j via Graphiti SDK |
| **Deduplication** | URL-based | Content-hash + semantic similarity |
| **Infrastructure** | Cron + scripts | Async pipeline + FastAPI + health monitoring |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          DATA SOURCES                                │
├─────────────────────────────────────────────────────────────────────┤
│  🌍 GDELT    📰 RSS    📱 CLS    📋 CNInfo    💹 EastMoney    📊 AkShare  │
└────┬──────────┬─────────┬──────────┬──────────────┬────────────┬───┘
     │          │         │          │              │            │
     ▼          ▼         ▼          ▼              ▼            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       ADAPTER LAYER                                  │
│  fetch() → filter() → normalize() → NormalizedEpisode               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     CONTENT FETCHER                                  │
│                                                                      │
│  Tier 0 ──► Tier 1 ──► Tier 1.5 ──► Tier 2 ──► Tier 3 ──► Fallback │
│  Static      Chrome      Alt TLS     CloakBrowser  Camoufox  Trafilatura│
│             (fast)      (fingerprints) (stealth)   (ultimate)  (heuristic)│
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   INGESTION PIPELINE                                 │
│  Severity enrichment → Content-hash dedup → EpisodeWriter           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    KNOWLEDGE GRAPH                                   │
│  Graphiti SDK → LLM entity/relation extraction → Neo4j              │
└─────────────────────────────────────────────────────────────────────┘
```

### 🕷️ 5-Tier Fetch Funnel

The secret sauce: **progressive escalation** with **cookie pooling**.

| Tier | Engine | Speed | Stealth | Success Rate |
|------|--------|-------|---------|--------------|
| **0** | Static extraction (`__NEXT_DATA__`, JSON-LD) | Instant | N/A | ~30% of pages |
| **1** | curl_cffi (chrome146) | ~1s/page | Low | ~60% of remaining |
| **1.5** | firefox135 → safari15_5 | ~1s/page | Medium | ~15% of remaining |
| **2** | CloakBrowser (Chromium, 71 C++ patches) | ~5-15s/page | High | ~10% of remaining |
| **3** | Camoufox (Firefox + Juggler) | ~10-15s/page | Highest | ~5% of remaining |

**Cookie Pooling**: Cloudflare `cf_clearance` cookies are bound to TLS fingerprints. We harvest them from Tier 2/3 success and reuse them in Tier 1/1.5 requests. Key: `(domain, fingerprint)`, TTL: 25min.

---

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- Neo4j 5.x (running locally or remote)
- API keys: 阿里百炼, DeepSeek, (optional) Google Gemini

### Installation

```bash
# Clone
git clone <repo-url> && cd NewsEngine

# Virtual environment
python -m venv .venv && source .venv/bin/activate

# Dependencies
pip install -r requirements.txt

# Optional: Tier 3 Camoufox (Firefox-based anti-bot)
pip install "camoufox[geoip]"
```

### Configuration

Create `.env`:

```bash
# Required
OPENAI_API_KEY=***
DEEPSEEK_API_KEY=***

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=***

# Optional: Google Gemini
GEMINI_API_KEY=***
GRAPHITI_LLM_PROVIDER=gemini  # or 'openai'
```

### Run

```bash
# Normal mode (Neo4j + Graphiti + FastAPI)
python main.py

# Dry-run mode (validation only, no infrastructure)
python main.py --dry-run --fetch-content

# Specific source
python main.py --dry-run --source rss
```

---

## 📊 Data Sources

| Source | Type | Coverage | Content |
|--------|------|----------|---------|
| 🌍 **GDELT** | Global events | Worldwide | Geopolitical events, conflicts, diplomacy |
| 📰 **RSS** | News feeds | Global | Financial news (Dow Jones, FT, EIA, mining.com) |
| 📱 **CLS Telegraph** | Flash news | China | Real-time financial telegraph (财联社) |
| 📋 **CNInfo** | Regulatory | China/HKEX | Official announcements (巨潮资讯) |
| 💹 **EastMoney** | Market data | China A-shares | Stock news, analyst research reports (PDF) |
| 📈 **AkShare** | Market data | China | Stock quotes, financial indicators |
| 💵 **Treasury** | Rates | US | US Treasury yield curves |

---

## 🔧 Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | *(required)* | OpenAI 兼容 API key（百炼 DashScope / 本地 llama-server） |
| `DEEPSEEK_API_KEY` | *(required)* | DeepSeek API key |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | *(required)* | Neo4j password |
| `GRAPHITI_LLM_PROVIDER` | `openai` | LLM provider: `openai` (百炼) or `gemini` |
| `GEMINI_API_KEY` | `""` | Google Gemini API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model name |
| `OPENAI_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼 OpenAI-compatible endpoint |
| `EMBEDDING_MODEL` | `text-embedding-v4` | Embedding model name |
| `LLM_MODEL` | `qwen3.7-plus` | LLM model name |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API base URL |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek model name |
| `API_HOST` | `0.0.0.0` | Server bind address |
| `API_PORT` | `8000` | Server port |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_FILE` | `logs/newsengine.log` | Log file path |

---

## 🧪 Testing

```bash
# Run all tests
pytest

# Specific modules
pytest tests/test_adapters/test_cookie_pool.py
pytest tests/test_adapters/test_funnel_orchestration.py
pytest tests/test_adapters/test_tier0_extraction.py

# With coverage
pytest --cov=src
```

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `scrapling` | Web scraping framework (FetcherSession, Spider) |
| `trafilatura` | Heuristic content extraction |
| `curl_cffi` | TLS fingerprint impersonation |
| `cloakbrowser` | Patched Chromium for stealth browsing |
| `camoufox` | Firefox-based anti-bot bypass (optional) |
| `feedparser` | RSS/Atom feed parsing |
| `PyMuPDF` | PDF text extraction (research reports) |
| `graphiti-core` | Knowledge graph SDK |
| `neo4j` | Graph database driver |
| `fastapi` + `uvicorn` | API server |
| `pydantic-settings` | Configuration management |

---

## 🔍 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health + per-source health status |
| `/api/events` | GET | Query recent episodes from knowledge graph |
| `/api/tickers/whitelist` | GET/POST | Ticker whitelist management |

---

## 🎓 How It Works

### 1. Adapter Layer

Each source has an adapter that implements:
- `fetch()` → raw records from the source
- `filter()` → relevance filtering (ticker whitelist, date range)
- `normalize()` → unified `NormalizedEpisode` format

### 2. Content Fetcher

The 5-tier funnel tries progressively stealthier methods:
1. **Tier 0**: Static extraction from `__NEXT_DATA__` or JSON-LD (instant, no HTTP)
2. **Tier 1**: Fast HTTP with Chrome TLS fingerprint
3. **Tier 1.5**: Retry with Firefox/Safari fingerprints
4. **Tier 2**: CloakBrowser (Chromium with 71 C++ stealth patches)
5. **Tier 3**: Camoufox (Firefox + Juggler protocol, ultimate stealth)

Cookies harvested from Tier 2/3 are pooled and reused in Tier 1/1.5.

### 3. Ingestion Pipeline

- **Severity enrichment**: Rule-based scoring (keywords, entities)
- **Deduplication**: Content-hash + semantic similarity
- **EpisodeWriter**: Converts `NormalizedEpisode` → Graphiti format

### 4. Knowledge Graph

Graphiti SDK uses LLMs to extract:
- **Entities**: People, organizations, locations, stocks, sectors
- **Relationships**: Acquisitions, partnerships, conflicts, dependencies
- **Episodes**: Time-stamped events with source attribution

All stored in Neo4j for real-time querying.

---

## 📈 Performance

Typical dry-run results (all sources, `--fetch-content`):

| Source | Fetched | Normalized | Time |
|--------|---------|------------|------|
| GDELT | ~850 | ~90 | ~370s |
| RSS | ~70 | ~70 | ~65s |
| CLS | ~50 | ~50 | ~0.3s |
| CNInfo | ~20 | ~6 | ~3s |
| EastMoney | ~6 | ~1 | ~2s |
| **Total** | **~1000** | **~220** | **~440s** |

Content success rate: **86%** (172/200 items with >100 chars body text)

---

## 🛠️ Troubleshooting

### Neo4j Connection Failed

```
CRITICAL: Neo4j connection FAILED
```

Check Neo4j is running and `.env` has correct `NEO4J_URI`.

### Camoufox Not Installed

```
WARNING: camoufox not installed, Tier 3 unavailable
```

Expected if you don't need Tier 3. Install with:
```bash
pip install "camoufox[geoip]"
```

### Cloudflare Blocks

If Tier 1/1.5 fail with 403, the funnel auto-escalates:
- `Tier 2 CloakBrowser succeeded` → stealth browser worked
- `Tier 3 Camoufox recovered` → ultimate fallback worked

Check logs for cookie pooling activity.

---

## 📝 License

Private — MyWallet project.

---

## 🔗 Related

- **[SynapseEngine](https://github.com/your-org/synapse-engine)** — Trading signal engine (whitelist provider)
- **[Graphiti](https://github.com/getzep/graphiti)** — Zep's knowledge graph SDK
- **[Scrapling](https://github.com/D4Vinci/Scrapling)** — Python web scraping library
- **[Camoufox](https://github.com/daijro/camoufox)** — Firefox-based anti-detect browser

---

<p align="center">
  <strong>Built with ❤️ for financial intelligence</strong>
</p>
