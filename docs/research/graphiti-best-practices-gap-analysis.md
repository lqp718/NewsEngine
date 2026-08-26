# Graphiti 最佳实践 Gap 分析报告

- 日期：2026-08-26
- 环境：graphiti-core **0.29.3**（requirements.txt 约束 `>=0.29.2,<1.0`）
- 项目：NewsEngine（`src/graphiti/episode_writer.py`、`src/core/graphiti_client.py`）
- 触发背景：Gemini API 频繁 429 → circuit breaker 触发 → writer 进入 cooldown

> 说明：本文档只做调研与分析，不修改任何代码。所有源码行号以当前 checkout 为准。

---

## 1. 现有实现概述

### 1.1 初始化链路

```
scheduler._init_components()
  ├─ get_neo4j_driver()                     # 独立单例驱动（pool=50, lifetime=3600）
  ├─ create_graphiti()                      # 工厂函数，无 graph_driver 实参
  │    ├─ GeminiClient(LLMConfig(...), max_tokens=32768)   # 或 BailianOpenAIClient
  │    ├─ BailianEmbedder(text-embedding-v4, dim=1024)
  │    ├─ BGERerankerClient()               # 本地模型
  │    └─ Graphiti(uri, user, password, llm_client, embedder, cross_encoder)
  │         └─ 内部再自建一个 Neo4j driver（graph_driver=None）
  ├─ EpisodeWriter(graphiti, neo4j_driver, MACRO_ENTITY_TYPES)
  └─ EpisodeWriter(graphiti, neo4j_driver, SYMBOL_ENTITY_TYPES, whitelist)
```

要点：

- **两个 Neo4j 驱动并存**：`get_neo4j_driver()` 单例（应用层，用于写入后 Cypher）+ Graphiti 内部自建驱动（`create_graphiti()` 调用时不传 `graph_driver`，于是 Graphiti 用 `uri/user/password` 自己新建 driver）。两者各维护一个连接池，指向同一个库。
- **单个 Graphiti 实例被宏/个股两个 writer 共享**（`add_episode` 不是线程安全/并发安全的，但本项目串行调用，问题不大）。
- **LLM 双 provider**：`gemini`（免费但不稳定）与 `openai`（百炼，稳定收费），通过 `graphiti_llm_provider` 切换。
- **`max_tokens=32768` 显式指定**：绕开 graphiti 对未知 Gemini 小模型 8192 token 兜底导致的 JSON 截断问题（对应社区 issue #760「Output length exceeded max tokens」）。
- **`small_model=gemini_model`**：强制 extraction 等小任务也用主模型，避免回落有 bug 的 `gemini-2.5-flash-lite`。

### 1.2 写入路径（EpisodeWriter）

```
write_batch(episodes) ──for 循环串行──> write_one(episode)
write_one:
  1. 控制字符清洗（_clean_text）
  2. 内存去重（content_hash / source_url）
  3. _build_extended_body（追加 ENTITY RESOLUTION RULES + 规范实体名）
  4. 归一 edge_types / edge_type_map（每次调用都重新算）
  5. for attempt in 1..max_retries(5):
       - _wait_breaker()   # 熔断冷却等待
       - async with _LLM_SEMAPHORE(3):  # 全局信号量
           graphiti.add_episode(name, episode_body, source_description,
                                reference_time, source=EpisodeType.text,
                                entity_types, edge_types, edge_type_map,
                                custom_extraction_instructions)
       - 成功后：ticker 接地 / severity 补写 / 边类型归一 / content_scope 透传
       - 429 → 尊重 retryDelay（下限 37s + jitter），连续 3 次开熔断（60s 冷却）
       - 其他错误 → 指数退避
```

### 1.3 错误处理策略（自研，独立于 graphiti-core）

| 机制 | 实现 | 位置 |
|---|---|---|
| 429 识别 | `graphiti_core.llm_client.errors.RateLimitError` + 类名兜底 | `_is_rate_limit()` |
| retryDelay 提取 | 沿异常链递归解析（属性 / headers / body / details / 文本正则），支持 `"44s"`、`{"seconds":N,"nanos":N}` 等形态 | `_extract_retry_delay()` |
| 429 退避 | `max(retryDelay, 37s) + jitter(0~2s)` | `_backoff_for_429()` |
| 熔断 | 全局连续 429 ≥ 3 次 → 60s 冷却，全队列阻塞等待 | `_record_429()` / `_wait_breaker()` |
| 通用重试 | 指数退避 `2^attempt`，最多 5 次 | `write_one()` |
| 并发上限 | 模块级 `asyncio.Semaphore(3)` | `_LLM_SEMAPHORE` |

### 1.4 未使用的 Graphiti 能力

- `add_episode_bulk()` — 完全未用
- `build_indices_and_constraints()` — **全项目（src/scripts/main.py）无任何调用**
- `build_communities()` / `update_communities` — 未用（可接受，社区功能可选）
- `group_id` / `previous_episode_uuids` / `excluded_entity_types` — 未传
- `max_coroutines` 参数 / `SEMAPHORE_LIMIT` 环境变量 — 未设置

---

## 2. 官方最佳实践要点

以下来自 Graphiti 官方 README、help.getzep.com 文档、mcp_server README 与社区 issue。

### 2.1 并发控制：`SEMAPHORE_LIMIT`

- 官方：**Graphiti 摄取管线设计为高并发，但默认并发设得较低，专门用来避免 LLM provider 429**。
- `SEMAPHORE_LIMIT` 环境变量控制「可同时处理的 episode 数」；由于**每个 episode 会发起多次 LLM 调用**（实体抽取、去重、摘要等），**实际并发 LLM 请求数是 episode 并发数的数倍**。
- 官方调优指引（按 provider 档位）：
  - 默认档 50 RPM → `SEMAPHORE_LIMIT=5-8`
  - OpenAI Tier 2（60 RPM）→ `5-8`；Tier 3（500 RPM）→ `10-15`；Tier 4 → `20-50`
  - 硬件/本地模型依赖 → `1-5`
- 症状对照：**过高 → 429 + 成本上升**；**过低 → 吞吐慢、配额浪费**。
- 出现 429 时的官方第一建议就是：**调低 `SEMAPHORE_LIMIT`**。
- 本项目实际安装的 0.29.3 中，`SEMAPHORE_LIMIT` 默认值为 **20**（`helpers.py:38`），与 README 宣称的 10 不同；`.env` 未显式设置，即当前取默认 20。

### 2.2 add_episode 的串行语义

`add_episode()` docstring 明确：

> It is recommended to run this method as a background process, such as in a queue. **It's important that each episode is added sequentially and awaited before adding the next one.** For web applications, consider FastAPI background tasks or a dedicated task queue like Celery.

- 官方推荐的并发模型是：**外层串行（按顺序 await）+ 内层并发（`SEMAPHORE_LIMIT` 控制单 episode 内部的 node/edge 解析并发）**。
- MCP server 官方实现正是「queue-based processing with configurable concurrency」。
- 社区 issue #1331 印证：**在同一个 Graphiti 实例上并发调用 `add_episode()`（尤其不同 group_id）会踩到 `self.driver` 被并发改写的数据污染 bug**，官方 workaround 是「把所有 `add_episode` 串行化到单一全局队列」。

### 2.3 批量导入：`add_episode_bulk`

- 官方文档（Adding Episodes）：**`add_episode_bulk` 用于大规模批量导入，性能优于逐条 `add_episode`**。
- 文档历史警示：bulk 管线「不做 edge invalidation」，只建议用于「填充空图或不需要失效边」的场景。
  - ⚠️ 版本差异：本项目 0.29.3 的 `add_episode_bulk` docstring 已更新为「**bulk 路径同样执行 edge invalidation 和 date extraction**」（上游 #1476 已删除过时警告，bulk 管线已被重写、复用逐条解析原语）。因此**当前版本 bulk 与单条在边失效语义上已基本对齐**，官方文档页尚未同步更新。
- 社区（callsphere 分析）：**把多段内容合并进单个 episode / 用 bulk，可省 30-50% 抽取 LLM 成本**；实体抽取是主要成本与耗时瓶颈。
- 风险提示：bulk 对超大批次应做 chunking / 限流（docstring 原文「Consider implementing rate limiting or chunking for very large batches」）。

### 2.4 索引构建时机

- 官方 quickstart 明确在**初始化阶段调用一次** `build_indices_and_constraints()`。
- 不构建的后果有实证（issue #354）：全文索引缺失时，`get_relevant_nodes` → `node_fulltext_search` 直接抛 `There is no such fulltext schema index: node_name_and_summary`。
- 该调用幂等、可在启动时重复执行（代价：大库上重建索引耗时，故 `delete_existing` 默认 False）。

### 2.5 Neo4j 连接配置

- 官方：**v0.17+ 支持用 `graph_driver` 参数传入自建 driver**（用于自定义 database name、复用连接、自定义 pool）。
- 数据库名默认 `neo4j`（Neo4jDriver 硬编码）；需要自定义时必须在 driver 构造时指定。
- **group_id 与数据库名的耦合（0.29.3 实测）**：Neo4j 场景下显式传 `group_id != driver._database` 会触发 `driver.clone(database=group_id)`，即 **group_id 不是普通命名空间，而是切到另一个 Neo4j 数据库**（本项目 `docs/entity-groupid-quality-research-20260822.md` 已有结论）。

### 2.6 其它要点

- **结构化输出**：Graphiti 在支持 Structured Output 的 LLM（OpenAI/Anthropic/Gemini）上效果最好；小模型/本地模型易产出不合 schema 的 JSON（README「Structured output and small models」）。
- **JSON episode 要紧凑**：必须能塞进 LLM 上下文窗口。
- **免费 Gemini 配额的现实**：issue #544 表明 Gemini（含付费）embedding 也会因 `BatchEmbedContentsRequestsPerMinutePerProjectPerRegion` 打满而 429，且 **graphiti 对 embedding 429 无内置重试/退避**，官方建议仍是「用 `SEMAPHORE_LIMIT` 限流」。
- **长文拆分**：issue #1516 指出 `add_episode` 对 >5KB 内容「不切实际地慢」，建议拆分 episode。
- **确定性数据可用 `custom_extraction_instructions` 强化 / 或走 skip-extraction 诉求**（issue #1299），对应本项目已有的白名单接地思路。

---

## 3. Gap 分析（按优先级排序）

| # | 问题 | 当前做法 | 最佳实践 | 改进建议 | 优先级 |
|---|---|---|---|---|---|
| G1 | **索引从未构建** | `build_indices_and_constraints()` 全项目无调用 | 初始化时调用一次，幂等 | 启动时（或首次写前）`await graphiti.build_indices_and_constraints()`，用 try/except 包裹并记日志；对已有大库避免 `delete_existing=True` | **P0** |
| G2 | **双 Neo4j 驱动 / 双连接池** | `create_graphiti()` 不传 `graph_driver`，Graphiti 内部自建 driver；应用另有 pool=50 单例 driver | 复用单一 driver（`graph_driver` 参数） | scheduler 改为 `create_graphiti(graph_driver=<Neo4jDriver/共享 driver>)`；统一 pool 配置，避免连接数翻倍与配置漂移 | **P0** |
| G3 | **并发信号量形同虚设** | `asyncio.Semaphore(3)` 包住整个 `add_episode`，但 `write_batch`/`IngestWorker` 都是串行 for 循环，实际并发恒为 1 | 用官方 `SEMAPHORE_LIMIT` 控制 episode 级并发（或保持串行 + 调低内层并发） | 二选一：① 保留串行、显式设 `SEMAPHORE_LIMIT` 匹配 Gemini 配额；② 引入受控并发队列（每个 worker 串行、worker 间并发，MCP server 模式），并用信号量限流 | **P1** |
| G4 | **429 是「事后反应」而非「事前限流」** | 429 才退避（下限 37s），熔断 60s | 客户端限流（token bucket / 每秒请求上限）+ `SEMAPHORE_LIMIT` 防患于未然 | 增加**令牌桶/滑动窗口限流器**（按 Gemini 4M tokens/min 配额做 token-aware 节流），把「等 429 再退避」改为「主动控制速率」；保留现有退避/熔断作为兜底 | **P1** |
| G5 | **逐条 add_episode 成本高、吞吐低** | 每条 episode 独立一次 LLM 抽取链路 | 批量合并（`add_episode_bulk` 或合并 body）可省 30-50% 抽取成本 | 对 landing 队列做**微批**：攒 N 条（如 5-10）同 tier 的 episode 走 `add_episode_bulk`；需先确认 0.29.3 bulk 与逐条在 ticker 接地/severity 补写的衔接方式（见 §4.3） | **P1** |
| G6 | **同步 Neo4j 调用阻塞事件循环** | `_ground_tickers` / `_normalize_written_edges` / severity 用同步 `session.run` / `execute_query`，在 async 函数里直接阻塞；ticker 接地**每个节点一条 query** | async driver 或批量 `UNWIND`，避免 per-node 往返 | ① 用 `neo4j.AsyncDriver` 或 `run_in_executor`；② ticker 接地改为**单条 UNWIND 批量 Cypher**（一次会话完成所有节点） | **P1** |
| G7 | **重复计算 edge_types 归一** | `_normalized_edge_types()` / `_normalized_edge_type_map()` 在每次 `write_one` 内重算 | 一次计算，实例内缓存 | 移到 `__init__` 或首次调用后缓存到 `self._norm_edge_types` | **P2** |
| G8 | **去重缓存随白名单刷新被清空** | `set_whitelist()` 每个 cycle 重置 `_seen_hashes/_seen_urls` | 跨 cycle 持久化去重（landing 层已做 content_hash 去重，但 writer 层语义应与之一致） | 明确两层去重职责：writer 层只做「本次运行内」去重并改名为 `_seen_this_run`；跨周期去重统一交给 landing store，避免误清 | **P2** |
| G9 | **熔断冷却时间与配额窗口不匹配** | 60s 冷却 | Gemini 配额窗口约 1min，但 37s 是官方 retryDelay 下限；冷却期过短可能在恢复瞬间再次 429 | 熔断冷却改为可配置 + 恢复后「慢启动」（半开状态先放 1 个请求探路） | **P2** |
| G10 | **未显式管理 `max_coroutines` / `SEMAPHORE_LIMIT`** | 依赖库默认 20 | 按 provider 档位显式设定 | 在 `.env` 设 `SEMAPHORE_LIMIT`，Gemini 免费档建议 1-3；百炼按实际配额定 | **P1** |
| G11 | **group_id 未利用（且需谨慎）** | 全部 episode 落同一默认库，宏/个股实体混合 | 需要隔离时用 group_id（但 0.29.3 中 = 数据库名） | 短期**保持现状**（避免 `driver.clone` 副作用）；若需宏/个股分区，改用「独立 Neo4j 数据库 + 独立 Graphiti 实例」，而非传 group_id | **P3（观察）** |
| G12 | **社区功能未用** | 无 `build_communities` | 可选，用于摘要检索 | 非阻塞项；如需社区摘要检索再评估，当前可略过 | **P3（可选）** |

---

## 4. 具体改进建议（可执行方向）

### 4.1 【P0】启动时构建索引（G1）

在 scheduler 建好 Graphiti 实例后、首次写之前调用一次。幂等，可用 `delete_existing=False`：

```python
# src/ingestion/scheduler.py — _init_components() 内 create_graphiti() 之后
self._graphiti = create_graphiti()

# Graphiti 首次启动/每次启动都幂等构建索引与约束
# （Neo4j fulltext 索引 node_name_and_summary / name_embedding vector 索引，
#   缺失会导致 hybrid_node_search 抛 "no such fulltext schema index"）
try:
    await self._graphiti.build_indices_and_constraints(delete_existing=False)
    logger.info("Graphiti indices and constraints ensured")
except Exception as exc:  # 不阻塞启动，但要可见
    logger.error("build_indices_and_constraints failed: %s", exc, exc_info=True)
```

> 注意：`build_indices_and_constraints` 是 async 方法，需在 async 上下文调用；若 `_init_components` 是同步方法，用 `asyncio.create_task` / 移到第一个 async cycle 执行。对已有大库，首次构建会耗时，建议只执行一次并在成功后打标记。

### 4.2 【P0】复用单一 Neo4j driver（G2）

`create_graphiti` 已支持 `graph_driver` 参数，只是 scheduler 没传。方向：让 Graphiti 复用应用层的 driver（或反向：应用层复用 Graphiti 的 driver）。

```python
# src/ingestion/scheduler.py
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from src.core.config import get_settings

settings = get_settings()
# 方案 A：构建共享 Neo4jDriver，同时给 Graphiti 和应用层 Cypher 用
neo4j_driver = Neo4jDriver(
    uri=settings.neo4j_uri,
    user=settings.neo4j_user,
    password=settings.neo4j_password,
)
self._graphiti = create_graphiti(graph_driver=neo4j_driver)
```

配套改动：
- `create_graphiti()` 里当 `graph_driver` 传入时，**不要再传 `uri/user/password`**（否则 Graphiti 仍可能新建 driver；需确认 0.29.3 构造逻辑——传入 `graph_driver` 时应忽略 uri 三元组）。
- `EpisodeWriter` 的 `_ground_tickers` / `_normalize_written_edges` 等改用同一个 driver 的 async 接口。

### 4.3 【P1】限流前置 + 熔断慢启动（G4/G9/G10）

在现有「事后退避」之上叠加「事前限流」。Gemini 免费档是 tokens/min 配额（约 4M tokens/min，多 tier 共享），因此 **token-aware 令牌桶**比纯请求数限流更贴合：

```python
# 伪代码：模块级 token bucket（可放 episode_writer.py 或独立 ratelimit.py）
class TokenBucket:
    def __init__(self, tokens_per_min: float, burst: float):
        self._rate = tokens_per_min / 60.0   # tokens/s
        self._capacity = burst
        self._tokens = burst
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, estimated_tokens: float) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= estimated_tokens:
                    self._tokens -= estimated_tokens
                    return
                deficit = estimated_tokens - self._tokens
            await asyncio.sleep(deficit / self._rate)   # 主动等待补 token

_LLM_TOKEN_BUCKET = TokenBucket(tokens_per_min=settings.gemini_tpm_limit, burst=...)
```

在 `write_one` 调用 `add_episode` 前：

```python
estimated = estimate_tokens(extended_body)   # 可用 len(body)/4 粗略估算
await _LLM_TOKEN_BUCKET.acquire(estimated)
```

熔断恢复改为**半开（half-open）**：冷却结束后先放行 1 个请求，成功则复位、失败则重新冷却：

```python
def _record_success():
    global _CIRCUIT_CONSECUTIVE_429, _CIRCUIT_STATE
    _CIRCUIT_CONSECUTIVE_429 = 0
    _CIRCUIT_STATE = 'closed'
```

`SEMAPHORE_LIMIT` 显式化（.env 或 `max_coroutines`）：

```python
# Gemini 免费档保守 1-3；百炼按实际配额
# .env: SEMAPHORE_LIMIT=3
# 或代码显式：
graphiti = Graphiti(..., max_coroutines=int(os.getenv("GRAPHITI_MAX_COROUTINES", "3")))
```

### 4.4 【P1】同步 Cypher → 批量/异步（G6）

ticker 接地当前是「每个节点一次 session.run」，改成单条 `UNWIND`：

```python
# 伪代码：一次会话批量处理所有节点
rows = [
    {"uuid": node_uuid, "ticker": matched_ticker}
    for node in result.nodes
    if (node_uuid := self._node_attr(node, "uuid"))
]
# 白名单命中 → SET；未命中 → REMOVE，两条 UNWIND 批量完成
with self._neo4j_driver.session() as session:
    session.run(
        "UNWIND $rows AS r "
        "MATCH (n) WHERE n.uuid = r.uuid SET n.ticker = r.ticker",
        rows=[r for r in rows if r["ticker"]],
    )
    session.run(
        "UNWIND $uuids AS u "
        "MATCH (n) WHERE n.uuid = u AND n.ticker IS NOT NULL REMOVE n.ticker",
        uuids=[r["uuid"] for r in rows if not r["ticker"]],
    )
```

更彻底：改用 `neo4j.AsyncDriver`（或 `await asyncio.to_thread(...)` 包装同步调用），消除事件循环阻塞。`_normalize_written_edges`、severity 补写同理合并。

### 4.5 【P1】微批合并写入（G5，需先验证衔接）

先验证 0.29.3 `add_episode_bulk` 的返回结构（`AddBulkEpisodeResults`）能否驱动现有 `_ground_tickers` / `_normalize_written_edges`。若可行，在 IngestWorker 的 drain 循环做微批：

```python
# 伪代码：按 tier/entity_types 分组攒批，满 N 条或超时 flush
BULK_BATCH_SIZE = 8
pending: list[NormalizedEpisode] = []

async def flush_bulk():
    raws = [RawEpisode(name=e.name, content=extended_body(e),
                       source=EpisodeType.text,
                       source_description=e.source_description,
                       reference_time=e.valid_at)
            for e in pending]
    result = await graphiti.add_episode_bulk(
        raws,
        entity_types=MACRO_ENTITY_TYPES,      # 注意：bulk 只传一份 entity_types
        edge_types=..., edge_type_map=...,
        custom_extraction_instructions=...,
    )
    # 之后仍需批量 ticker 接地 / severity / edge 归一 / metadata
    pending.clear()
```

> 关键约束：`add_episode_bulk` 的 `entity_types` 是**单份**，而本项目宏/个股用不同 entity_types。因此**批量只能在「同一 tier」内分组**，宏批与个股批分开攒。且 bulk 返回后要重新适配 per-episode 的后处理（ticker/severity/metadata 需要按 episode 粒度映射）。

### 4.6 【P2】缓存 edge 归一结果（G7）

```python
# __init__ 内一次算好
self._norm_edge_types = _normalized_edge_types(self._edge_types)
self._norm_edge_type_map = _normalized_edge_type_map(self._edge_type_map)
# write_one 中直接复用，删掉每次重算
```

### 4.7 【P2】熔断冷却可配置化（G9）

把 `_CIRCUIT_COOLDOWN_SEC` / `_MIN_429_BACKOFF_SEC` / `_CIRCUIT_MAX_CONSECUTIVE_429` 提升为 settings 字段（带默认值），便于按 Gemini 实际配额窗口调参，而非硬编码。

---

## 5. 总结

### 当前实现的优点（保留）

1. **429 处理非常精细**：retryDelay 解析覆盖了 Google rpc Duration、Retry-After、字符串/数字多种形态，且尊重 37s 下限 + jitter——这比 graphiti-core 默认（对 429 直接 fail-fast，无内置退避）强得多。
2. **熔断器 + 全局信号量** 提供了跨 writer/tier 的协调，方向正确。
3. **`max_tokens=32768` 显式指定 + `small_model` 兜底** 精准规避了两个已知上游坑（JSON 截断、小模型 bug）。
4. **写入后确定性校正**（ticker 接地、边类型归一、severity、content_scope）不信任 LLM、用 Cypher 兜底，工程严谨。
5. **landing 队列（IngestWorker + lease 恢复）** 与官方「queue-based processing」推荐模式一致，具备持久化与失败重放能力。

### 核心差距（按影响排序）

1. **【P0】索引未构建** —— 潜在会导致 hybrid 检索失效/报错，是正确性隐患。
2. **【P0】双 Neo4j 驱动** —— 资源浪费 + 配置漂移风险。
3. **【P1】429 是「反应式」而非「主动限流」** —— 现有退避/熔断只能止损，不能防患；应叠加 token-aware 限流器 + 显式 `SEMAPHORE_LIMIT`。
4. **【P1】逐条 add_episode + 串行** —— 成本高、吞吐低；可评估同 tier 微批 bulk。
5. **【P1】同步 Cypher 阻塞事件循环 + per-node 查询** —— 应批量 UNWIND / 异步化。

### 一句话结论

项目在「错误恢复」上已做得比 graphiti-core 默认更稳健，主要短板在**「事前限流」与「基础设施配置」**：把 429 的应对从「等它发生再退避」升级为「令牌桶主动控速 + 显式并发上限」，补齐**索引构建**与**驱动复用**两个基础设施缺口，再按需引入**同 tier 微批 bulk** 降本，即可在保持现有数据质量兜底的前提下显著改善稳定性与吞吐。

---

## 附：参考来源

- 官方 README：https://github.com/getzep/graphiti （「Default to Low Concurrency; LLM Provider 429」「Structured output and small models」「graph_driver 参数」）
- 官方文档 Adding Episodes：https://help.getzep.com/graphiti/core-concepts/adding-episodes （`add_episode_bulk` 使用与 edge invalidation 警示）
- mcp_server README：https://github.com/getzep/graphiti/blob/main/mcp_server/README.md （SEMAPHORE_LIMIT 分档调优表）
- issue #544：Gemini embedding 429 无内置重试，官方建议限流
- issue #354：缺全文索引报错 `node_name_and_summary`
- issue #1331：同一实例并发 add_episode 的 driver 数据污染
- issue #760：max tokens 超限（本项目已用 32768 规避）
- issue #1516：>5KB 内容过慢，建议拆分
- issue #1299：确定性数据的 skip-extraction 诉求
- 上游 release #1476：删除 add_episode_bulk 过时警告（bulk 已支持边失效）
- 项目内部：`docs/entity-groupid-quality-research-20260822.md`（group_id = 数据库名 的版本行为）
