# NewsEngine

Multi-source financial news ingestion pipeline with knowledge graph construction.

## Overview

NewsEngine collects, filters, and processes financial news from multiple global and regional sources, extracts entities and relationships using LLMs, and builds a real-time knowledge graph in Neo4j via [Graphiti](https://github.com/getzep/graphiti).

### Key Capabilities

- **9 data source adapters** covering global events, RSS feeds, Chinese financial telegraph, regulatory filings, analyst research, and market data
- **5-tier fetch funnel** with progressive anti-bot bypass (Chrome → alternate TLS fingerprints → CloakBrowser → Camoufox)
- **Tier 0 static extraction** from structured data (`__NEXT_DATA__`, JSON-LD) before falling back to heuristic parsers
- **Knowledge graph ingestion** via Graphiti SDK with LLM-based entity/relation extraction
- **Ticker whitelist filtering** for stock-specific news targeting
- **Dry-run mode** for pipeline validation without infrastructure dependencies

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data Sources                              │
├─────────────────────────────────────────────────────────────────┤
│  GDELT  │  RSS  │  CLS  │  CNInfo  │  EastMoney  │  AkShare    │
│  Events │  Feeds│  Tel. │  Announce│  Research   │  Treasury   │
└────┬────┴───┬───┴───┬───┴────┬─────┴──────┬──────┴──────┬──────┘
     │        │       │        │            │             │
     ▼        ▼       ▼        ▼            ▼             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Adapter Layer                                │
│  fetch() → filter() → normalize() → NormalizedEpisode           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Content Fetcher                                │
│  Tier 0: __NEXT_DATA__ / JSON-LD static extraction              │
│  Tier 1: FetcherSession (curl_cffi, chrome146)                  │
│  Tier 1.5: Alternate TLS fingerprints (firefox135, safari15_5)  │
│  Tier 2: CloakBrowser (Chromium, C++ stealth patches)           │
│  Tier 3: Camoufox (Firefox + Juggler, ultimate fallback)        │
│  → Trafilatura heuristic extraction (final fallback)            │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Ingestion Pipeline                              │
│  Severity enrichment → Dedup (content_hash) → EpisodeWriter     │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Knowledge Graph                                 │
│  Graphiti SDK → LLM entity/relation extraction → Neo4j          │
└─────────────────────────────────────────────────────────────────┘
```

## Data Sources

| Source | Type | Coverage | Content |
|--------|------|----------|---------|
| **GDELT** | Global events | Worldwide | Geopolitical events, conflicts, diplomacy |
| **RSS** | News feeds | Global | Financial news from Dow Jones, FT, EIA, mining.com |
| **CLS Telegraph** | Flash news | China | Real-time financial telegraph (财联社) |
| **CNInfo** | Regulatory | China/HKEX | Official announcements (巨潮资讯) |
| **EastMoney** | Market data | China A-shares | Stock news, analyst research reports |
| **AkShare** | Market data | China | Stock quotes, financial indicators |
| **Treasury** | Rates | US | US Treasury yield curves |

## Project Structure

```
NewsEngine/
├── main.py                      # Entry point (normal + dry-run modes)
├── src/
│   ├── adapters/                # Data source adapters
│   │   ├── base.py             # BaseAdapter abstract class
│   │   ├── models.py           # NormalizedEpisode, EntityItem
│   │   ├── gdelt_adapter.py    # GDELT CSV/Events parser
│   │   ├── rss_adapter.py      # RSS feed aggregator
│   │   ├── cls_adapter.py      # CLS telegraph adapter
│   │   ├── cninfo_adapter.py   # CNInfo announcements
│   │   ├── eastmoney_adapter.py        # EastMoney stock news
│   │   ├── eastmoney_research_adapter.py  # EastMoney research PDFs
│   │   ├── akshare_adapter.py  # AkShare market data
│   │   └── treasury_adapter.py # US Treasury yields
│   ├── api/                    # FastAPI server
│   │   ├── server.py           # App factory
│   │   └── routers/            # Health, events, whitelist endpoints
│   ├── core/                   # Infrastructure
│   │   ├── config.py           # Pydantic settings (.env)
│   │   ├── neo4j_client.py     # Neo4j driver singleton
│   │   ├── graphiti_client.py  # Graphiti SDK wrapper
│   │   ├── bailian_llm_client.py   # 阿里百炼 LLM client
│   │   └── bailian_embedder.py     # Embedding client
│   ├── graphiti/               # Knowledge graph
│   │   ├── episode_writer.py   # Episode → graphiti ingestion
│   │   ├── entity_types.py     # Entity type definitions
│   │   └── relation_types.py   # Relation type definitions
│   ├── ingestion/              # Pipeline orchestration
│   │   ├── pipeline.py         # Unified fetch→normalize→write
│   │   ├── scheduler.py        # Periodic ingestion scheduler
│   │   ├── severity_enricher.py # Rule-based severity scoring
│   │   └── briefing_aggregator.py  # Sector briefing generation
│   ├── sync/                   # External sync
│   │   └── ticker_sync.py      # Ticker whitelist sync
│   └── utils/                  # Shared utilities
│       ├── news_spider.py      # 5-tier fetch funnel (V7.0)
│       ├── content_fetcher.py  # Content extraction (V5.0)
│       ├── logging_config.py   # Structured JSON logging
│       └── time_utils.py       # Timezone utilities
├── data/                       # Static data files
│   ├── ticker_whitelist.json   # Stock ticker filter list
│   ├── rss_feeds.json          # RSS feed URL configuration
│   └── codebooks/              # GDELT codebooks
├── tests/                      # Test suite
├── output/                     # Dry-run output JSON files
└── logs/                       # Application logs
```

## Quick Start

### Prerequisites

- Python 3.12+
- Neo4j 5.x (running locally or remote)
- API keys: 阿里百炼, DeepSeek, (optional) Google Gemini

### Installation

```bash
# Clone and enter project
cd NewsEngine

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Optional: Tier 3 Camoufox (Firefox-based anti-bot bypass)
pip install "camoufox[geoip]"
```

### Configuration

Create a `.env` file in the project root:

```bash
# Required
BAILIAN_API_KEY=your_bailian_api_key
DEEPSEEK_API_KEY=your_deepseek_api_key

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password

# Optional: Google Gemini (alternative LLM provider)
GEMINI_API_KEY=your_gemini_api_key
GRAPHITI_LLM_PROVIDER=openai  # or "gemini"

# Optional: Server
API_HOST=0.0.0.0
API_PORT=8000
LOG_LEVEL=INFO
```

### Running

#### Normal Mode (with Neo4j + Graphiti + API server)

```bash
python main.py
```

Startup sequence (FIFO):
1. Load `.env` configuration
2. Initialize structured JSON logging
3. Connect to Neo4j (hard block on failure)
4. Create FastAPI app + register routers
5. Initialize Graphiti SDK (LLM + Embedder)
6. Create EpisodeWriter
7. Start IngestionScheduler (15-minute cycles)
8. Start uvicorn server

Shutdown sequence (LIFO): SIGINT/SIGTERM → scheduler stop → uvicorn stop → writer close → Neo4j close

#### Dry-Run Mode (validation only, no infrastructure)

```bash
# All sources
python main.py --dry-run --fetch-content

# Specific source
python main.py --dry-run --source gdelt
python main.py --dry-run --source rss --fetch-content
```

Dry-run output is written to `output/dry_run_YYYYMMDD_HHMMSS.json`.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health + per-source health status |
| `/api/events` | GET | Query recent episodes from knowledge graph |
| `/api/tickers/whitelist` | GET/POST | Ticker whitelist management |

## Fetch Funnel (V7.0)

The 5-tier fetch architecture maximizes content retrieval while minimizing resource usage:

| Tier | Engine | Speed | Stealth | Use Case |
|------|--------|-------|---------|----------|
| **0** | Static extraction | Instant | N/A | `__NEXT_DATA__`, JSON-LD |
| **1** | curl_cffi (chrome146) | ~1s/page | Low | Default fast fetch |
| **1.5** | curl_cffi (firefox/safari) | ~1s/page | Medium | Alternate TLS fingerprints |
| **2** | CloakBrowser (Chromium) | ~5-15s/page | High | 71 C++ stealth patches |
| **3** | Camoufox (Firefox) | ~10-15s/page | Highest | Ultimate fallback |

### Cookie Pooling

Cloudflare `cf_clearance` cookies are bound to the TLS/JA3 fingerprint that obtained them. The in-memory cookie pool:
- Keys by `(domain, fingerprint)`
- Harvests cookies from CloakBrowser (→ chrome146 pool) and Camoufox (→ firefox135 pool)
- TTL: 25 minutes (below cf_clearance's ~30min lifetime)
- Invalidates on blocked responses to prevent stale reuse

## Ticker Whitelist

Stock-specific adapters (EastMoney, AkShare, CNInfo) filter by a configurable ticker list:

```json
// data/ticker_whitelist.json
{
  "tickers": [
    {"ticker": "0700.HK", "sector": "Tech", "name": "腾讯控股", "exchange": "HKEX"},
    {"ticker": "000001.SZ", "sector": "Finance", "name": "平安银行", "exchange": "SZSE"},
    {"ticker": "600519.SH", "sector": "Consumer", "name": "贵州茅台", "exchange": "SSE"}
  ]
}
```

## Testing

```bash
# Run all tests
pytest

# Run specific test modules
pytest tests/test_adapters/test_cookie_pool.py
pytest tests/test_adapters/test_funnel_orchestration.py
pytest tests/test_adapters/test_tier0_extraction.py

# Run with verbose output
pytest -v
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `BAILIAN_API_KEY` | (required) | 阿里百炼 API key |
| `DEEPSEEK_API_KEY` | (required) | DeepSeek API key |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | (required) | Neo4j password |
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

## Dependencies

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

## License

Private — MyWallet project.
