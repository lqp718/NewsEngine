# JSON 持久化层设计 — 解耦抓取与入库

**状态**: Proposed
**作者**: Architect
**日期**: 2026-08-23
**项目**: NewsEngine (`/Users/liuqipeng/Projects/MyWallet/NewsEngine`)
**关联文档**: `NEWSENGINE-DESIGN-DOC.md` V2.2、`ARCH_REVIEW_GDELT_REFACTOR.md` V1.1

---

## 0. 摘要

当前 ingestion 流程把「抓取」和「入库」耦合在同一个 cycle 内：

```
GDELT API → fetch → normalize → dedup → write_one → graphiti LLM → Neo4j
        └──────────────── 单个 cycle 内串行完成 ────────────────┘
```

问题根源：`write_one` 是慢操作（~7.7s/episode，含 LLM 抽取），而 GDELT 只返回「最近 15 分钟」窗口。任何导致 episode 未被本 cycle 内成功写入的原因（`MAX_EPISODES_PER_CYCLE=80` 截断、429 限流、JSON 截断、进程崩溃、清库），都意味着该 episode 被**永久丢弃**——因为下一个 cycle 抓取的是全新的 15 分钟窗口，旧数据不会重现。

本设计在「抓取」和「入库」之间插入一个**磁盘持久化层（landing zone）**：

```
Stage A (capture):  fetch → normalize → dedup → 写入 JSONL + 登记 SQLite (pending)
Stage B (ingest):   从 SQLite 认领 pending → 读 JSONL → write_one → Neo4j → 标记 done
```

两阶段解耦后，抓取不再受 LLM 写入速率拖累，所有抓到的数据先落盘、后入库，天然获得数据不丢失、断点续传、可重放、可审计四类能力。

---

## 1. 现状诊断（基于源码确认）

### 1.1 当前数据流

| 组件 | 位置 | 职责 |
|------|------|------|
| `main.py` | 根目录 | CLI 入口：`--dry-run` / `--source` / `--fetch-content` |
| `IngestionScheduler` | `src/ingestion/scheduler.py` | 多 Tier 调度（Tier1=900s、Tier2=4h、Tier3=12h、Tier4=24h） |
| `run_pipeline` | `src/ingestion/pipeline.py:69` | 单 adapter 单 cycle 编排 |
| `BaseAdapter.run` | `src/adapters/base.py` | `fetch → normalize → dedup`（dedup 只过滤不登记） |
| `BaseAdapter.dedup` | `src/adapters/base.py:59` | 按 `content_hash` + 批次内 `source_url` 去重 |
| `BaseAdapter.mark_written` | `src/adapters/base.py:97` | 写入成功后登记 hash 进 `dedup_cache` |
| `EpisodeWriter.write_batch/write_one` | `src/graphiti/episode_writer.py` | 串行写 graphiti；429 退避（≥37s+jitter）+ 熔断（3 次）+ 全局信号量 |
| `GdeltAdapter` | `src/adapters/gdelt_adapter.py` | GDELT fetch；`MAX_EPISODES_PER_CYCLE=80` 截断 |
| `NormalizedEpisode` | `src/adapters/models.py:111` | 统一中间表示（pydantic） |

### 1.2 关键事实（设计约束）

1. **`dedup_cache` 是内存态**（`scheduler.py:220 self._dedup_cache: set[str] = set()`），进程重启即清空。跨 cycle 去重依赖它，重启后同批数据会被重新抓取（好在 `write_one` 内部有 `_seen_hashes` 兜底 `skipped_duplicate`）。
2. **`MAX_EPISODES_PER_CYCLE=80` 截断逻辑**（`gdelt_adapter.py:1505`）注释声称「截断的 episode 下个 cycle 会重试」，但这是**错误假设**：下个 cycle 的 GDELT 是新的 15 分钟窗口，被截断的 240 个不会重现。
3. **P0-1 时序契约**：`dedup()` 只过滤不登记，`mark_written()` 仅在 `write_batch` 返回 `ok/skipped_duplicate` 后调用。写入失败的 episode 不登记 → 下个 cycle 重试。对 RSS/stock 等「可重复抓取」的源有效，对 GDELT「窗口式」源无效（窗口已滑走）。
4. **`write_one` 串行**：graphiti-core 要求串行 `add_episode`，单 episode ~7.7s；429 时退避 ≥37s，连续 3 次熔断整个队列。
5. **`dry_run` 已是「fetch → normalize → JSON」**（`main.py:382 main_dry_run`）：写入 `output/dry_run_{timestamp}.json`（indent=2 的 JSON 数组，`model_dump(mode='json')`），无状态、不登记 dedup、不改库。**这为本设计提供了现成的序列化原语**。
6. **`.gitignore` 已忽略** `data/*.json`、`data/*.sqlite`、`data/*.db`、`output/`——落盘位置可直接放在 `data/` 下，无需改动 ignore 规则。

### 1.3 磁盘量级实测

`output/dry_run_*.json`（indent=2 全源）：单 cycle **0.6–1.9 MB**。紧凑 JSONL（无 indent）约为其 **35–45%**，即单 cycle **0.25–0.8 MB**。

---

## 2. 目标架构

### 2.1 整体架构图

```
                    ┌─────────────────────────────────────────────────────┐
                    │            NewsEngine 进程（单进程，asyncio）         │
                    │                                                     │
 GDELT/RSS/stock/   │   Stage A — Capture（按 Tier 调度，低成本）          │
 macro 适配器 ──────┼──► fetch → normalize → dedup(内存)                  │
 (15min~24h)        │          │                                          │
                    │          ▼                                          │
                    │   ┌───────────────────────────────────┐             │
                    │   │  LandingStore (持久化)             │             │
                    │   │  • 写 JSONL（原子 tmp+rename）     │             │
                    │   │  • INSERT OR IGNORE 登记 pending   │             │
                    │   └───────────────┬───────────────────┘             │
                    │                   │                                 │
                    │   Stage B — Ingest（独立 worker，慢速，LLM）          │
                    │   ┌───────────────▼───────────────────┐             │
                    │   │  IngestWorker                      │             │
                    │   │  1. 认领 pending 批（lease）        │             │
                    │   │  2. 读 JSONL 行 → 反序列化 Episode  │             │
                    │   │  3. write_one（复用 429/熔断逻辑）   │             │
                    │   │  4. 标记 done / failed              │             │
                    │   └───────────────┬───────────────────┘             │
                    └───────────────────┼─────────────────────────────────┘
                                        ▼
                              graphiti LLM → Neo4j
```

### 2.2 数据流图（状态视角）

```
        capture                    claim                    write_one
[fetched] ─────► pending ─────────► processing ───────────────► done
                   ▲                    │                          │
                   │                    │ ok                       │ skipped_duplicate
                   │                    │                          ▼
                   │                    │                        skipped
                   │                    │ err(429/截断/异常)
                   │                    ▼
                   │                  failed ── attempts<max 且已退避 ──► pending
                   │                    │
                   │                    └── attempts≥max ──► dead（人工介入）
                   │
                   └── lease 超时（进程崩溃后恢复）──► processing 重置为 pending
```

### 2.3 与现有架构的对应关系

| 现有 | 新架构 |
|------|--------|
| `run_pipeline` Stage2 `writer.write_batch` | 改为 `landing_store.capture_batch`（Stage A） |
| `_mark_written`（内存 dedup 登记） | 由 `INSERT OR IGNORE` 返回值取代（持久化去重） |
| `write_batch`（cycle 内串行写） | 移到独立 `IngestWorker`（Stage B） |
| `MAX_EPISODES_PER_CYCLE=80` | **删除**——截断不再需要 |
| `dedup_cache`（内存跨 cycle 去重） | 降级为 capture 阶段的快速路径；权威去重由 SQLite PK 承担 |
| `--dry-run` | 保持不变（预览/校验，不改库、不写 landing） |

---

## 3. JSON 文件格式设计

### 3.1 选型：JSONL，按 (cycle, source) 分文件

| 方案 | 结论 | 理由 |
|------|------|------|
| 单个大 JSON（数组） | ❌ | 追加需整文件重写；崩溃易全损；无法按源/时间局部重放 |
| 每 episode 一个文件 | ❌ | 每天数千文件，inode 压力大、目录扫描慢 |
| 每 cycle 一个文件（全源混合） | ⚠️ | 可重放但粒度太粗，无法按源过滤重放 |
| **每 (cycle, source) 一个 JSONL** | ✅ | 原子追加友好、崩溃局部化、可按源/时间重放、命名即元数据 |

**选型：JSONL，每行一个 episode envelope，每 (cycle, source) 一个文件。**

### 3.2 Episode Envelope Schema（每行）

```json
{
  "v": 1,
  "captured_at": "2026-08-23T14:15:03.123Z",
  "cycle_id": "20260823T141500Z",
  "episode": {
    "episode_body": "...",
    "name": "gdelt-20260823--ab12cd34ef56",
    "source_description": "...",
    "source_type": "gdelt",
    "source_url": "https://...",
    "valid_at": "2026-08-23T14:00:00Z",
    "content_hash": "ab12cd34ef56...",
    "entities": [],
    "is_plain_text": true,
    "severity": "medium",
    "keywords": [],
    "metadata": {}
  }
}
```

**字段说明：**

- `v`：envelope 版本号（int，当前 1）。未来 schema 演进时按版本解析/迁移。
- `captured_at`：抓取时间戳（UTC ISO 8601，毫秒），审计用。
- `cycle_id`：所属 cycle 标识，格式 `YYYYMMDDTHHMMSSZ`（UTC），与文件名 stem 一致。
- `episode`：**完整** `NormalizedEpisode.model_dump(mode='json')`。不裁剪任何字段——`entities`、`keywords`、`severity`、`metadata` 全部保留，保证「清库重建」「调整 entity_types 重放」时**零信息损失**。

> 设计原则：`episode` 必须是入库所需的**自包含完整快照**，反序列化后可直接喂给 `EpisodeWriter.write_one`，不再依赖任何外部状态（如原 adapter、原 GDELT 记录）。

### 3.3 文件命名规则

```
{source_type}-{cycle_id}.jsonl
例: gdelt-20260823T141500Z.jsonl
    rss-20260823T141500Z.jsonl
```

- `cycle_id` = UTC 时间戳 `YYYYMMDDTHHMMSSZ`，粒度到秒。
- 同名 cycle 内不同 source 天然分文件，无冲突。
- 排序（glob `*.jsonl` 字典序）即按时间排序，便于 `--replay --since` 做范围过滤。

### 3.4 原子写入

```
1. 序列化所有 episode → 逐行写入 {name}.jsonl.tmp
2. fsync(tmp)  +  close
3. os.replace(tmp, final)      # POSIX 原子
4. SQLite 事务：INSERT OR IGNORE 每行（batch_file=final, line_no=i）
```

**顺序理由**：文件是「真相」，SQLite 是「索引」。先落文件、后写索引。若第 3 步后崩溃（文件存在但无索引），启动时「孤儿文件扫描」补登记（幂等，见 §8.2）。若第 4 步崩溃，事务回滚，下个启动扫描补齐。

---

## 4. 状态管理设计

### 4.1 选型：SQLite（非文件名后缀、非独立 JSON 状态文件）

| 方案 | 结论 | 理由 |
|------|------|------|
| 文件名后缀（.pending/.done） | ❌ | 粒度为文件级，无法定位单条失败 episode；重放整文件浪费 LLM |
| 独立状态 JSON 文件 | ❌ | 并发写需要锁；崩溃易损坏；无事务；无法原子认领 |
| **SQLite** | ✅ | 原子事务、原子认领（UPDATE...WHERE）、崩溃安全（WAL）、可查询审计、Python 标准库自带 |

### 4.2 状态存储位置

`data/landing/state.db`（`.gitignore` 已忽略 `data/*.db`）。

### 4.3 Schema

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS landed_episodes (
    content_hash  TEXT PRIMARY KEY,          -- episode 内容 SHA256，权威去重键
    name          TEXT NOT NULL,             -- episode 全局唯一名（graphiti 键）
    source_type   TEXT NOT NULL,
    batch_file    TEXT NOT NULL,             -- JSONL 相对路径（相对 data/landing/）
    line_no       INTEGER NOT NULL,          -- 文件内行号（0-based）
    valid_at      TEXT NOT NULL,             -- 事件时间（UTC）
    captured_at   TEXT NOT NULL,             -- 抓取时间（UTC）
    cycle_id      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','done','skipped','failed','dead')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    lease_owner   TEXT,                      -- 认领者标识（进程 uuid）
    lease_expires TEXT,                      -- lease 超时时间（UTC）
    updated_at    TEXT NOT NULL,
    ingested_at   TEXT                       -- done 时间（UTC）
);
CREATE INDEX IF NOT EXISTS idx_status_captured ON landed_episodes(status, captured_at);
CREATE INDEX IF NOT EXISTS idx_source_status  ON landed_episodes(source_type, status);
CREATE INDEX IF NOT EXISTS idx_batch          ON landed_episodes(batch_file);

CREATE TABLE IF NOT EXISTS capture_runs (
    cycle_id    TEXT NOT NULL,
    source_type TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    batch_file  TEXT NOT NULL,
    total       INTEGER NOT NULL,   -- 本 cycle 该源抓到的 episode 总数
    new_rows    INTEGER NOT NULL,   -- 实际 INSERT 的行数
    dup_rows    INTEGER NOT NULL,   -- INSERT OR IGNORE 跳过的行数（跨 cycle 去重命中）
    PRIMARY KEY (cycle_id, source_type)
);
```

### 4.4 状态机

状态集合：`pending / processing / done / skipped / failed / dead`

| 转移 | 触发 | 动作 |
|------|------|------|
| `pending → processing` | IngestWorker 认领 | 事务内 `UPDATE ... SET status='processing', lease_owner=?, lease_expires=now+lease_sec WHERE content_hash IN (SELECT ... WHERE status='pending' LIMIT ?)` |
| `processing → done` | `write_one` 返回 `ok` | `status='done', ingested_at=now` |
| `processing → skipped` | `write_one` 返回 `skipped_duplicate` | 视同完成，`status='skipped', ingested_at=now` |
| `processing → failed` | `write_one` 返回 `error` 或抛异常 | `attempts++`, `last_error=...`, `status='failed'` |
| `processing → pending` | lease 超时（崩溃恢复） | 启动/周期扫描：`UPDATE ... SET status='pending', lease_owner=NULL WHERE status='processing' AND lease_expires < now` |
| `failed → pending` | 重试：`attempts < max` 且距上次失败 ≥ backoff(attempts) | `status='pending'`（不清 attempts，保留计数） |
| `failed → dead` | `attempts ≥ max` | 告警日志，人工介入（`--retry-dead` 可复位） |

### 4.5 失败重试逻辑

- `write_one` **内部重试保持不变**（429 退避 ≥37s+jitter、连续 3 次熔断、全局 `_LLM_SEMAPHORE`）。IngestWorker 只处理「write_one 最终仍失败」的情况。
- 行级重试退避（IngestWorker 侧）：`backoff = min(6h, 10min * 2^attempts)`，即 10min → 20min → 40min → ... 上限 6h。默认 `max_attempts = 3`。
- 重试由 IngestWorker 周期性扫描 `failed` 行触发，**不阻塞** pending 的正常消费（用 `ORDER BY captured_at` 保序 + 独立 failed 扫描）。

### 4.6 认领（claim）语义

- 认领必须**原子**，避免「两个进程同时认领同一行」（scheduler 常驻 + 用户手动 `--ingest-only` 并发场景）。
- 实现：`BEGIN IMMEDIATE` 事务内 `SELECT ... WHERE status='pending' ORDER BY captured_at LIMIT ?` → 逐行 `UPDATE` → `COMMIT`。SQLite 写事务串行化，保证原子。
- `lease_owner` = 每个进程启动时生成的 uuid，用于日志区分和崩溃后诊断。
- `lease_expires = now + lease_sec`（`lease_sec` 默认 900s，须大于 `write_one` 最坏耗时（429 退避下可达数分钟））。

---

## 5. 目录结构

```
NewsEngine/
├── data/
│   ├── landing/                          # ★ 新增：持久化层根目录
│   │   ├── state.db                      #   SQLite 状态 + 索引
│   │   ├── state.db-wal / state.db-shm   #   WAL 文件（运行时生成）
│   │   ├── 2026-08-23/                   #   按日分区
│   │   │   ├── gdelt-20260823T141500Z.jsonl
│   │   │   ├── rss-20260823T141500Z.jsonl
│   │   │   └── stock-20260823T141500Z.jsonl
│   │   └── 2026-08-24/
│   │       └── ...
│   ├── neo4j/                            #   （现有，Docker 卷）
│   ├── codebooks/                        #   （现有）
│   └── ...                               #   （现有）
├── output/                               #   （现有，dry-run 预览输出，不改）
├── src/
│   ├── persistence/                      # ★ 新增模块
│   │   ├── __init__.py
│   │   ├── models.py                     #   EpisodeEnvelope / EpisodeStatus / CaptureRunRecord
│   │   ├── landing_store.py              #   LandingStore（SQLite + JSONL IO）
│   │   └── ingest_worker.py              #   IngestWorker（异步 drain 循环）
│   └── ...
└── main.py                               #   扩展 CLI
```

### 5.1 清理策略（保留多少天）

- 配置项 `landing_retention_days`（默认 **14**）。
- 每日清理任务：删除 `captured_at < now - retention_days` **且** `status IN ('done','skipped','dead')` 的行，并删除对应日期目录下的 `.jsonl` 文件。
- **绝不自动删除** `pending / processing / failed` 的行或文件——未入库数据永不清理。
- 清理任务挂靠现有 Tier4（日频）循环或独立 daily 任务；触发条件「日期跨天」即可，无需独立 cron。

---

## 6. CLI 接口设计

在 `main.py` 现有 argparse（`--dry-run` / `--source` / `--fetch-content`）基础上扩展：

| 命令 | 行为 |
|------|------|
| `python main.py`（默认） | **capture + ingest**：启动 scheduler，Tier 循环抓取（Stage A）+ IngestWorker 消费（Stage B） |
| `python main.py --fetch-only` | **只抓取**：只跑 Stage A（capture → 写 JSONL + 登记 pending），不初始化 graphiti/Neo4j、不启动 IngestWorker |
| `python main.py --ingest-only` | **只入库**：只跑 Stage B，drain 所有 pending 后退出（不抓取） |
| `python main.py --ingest-only --watch` | 只入库 + 常驻监听（持续消费新 pending） |
| `python main.py --replay --source gdelt --since 2026-08-20 [--until ...]` | 把指定范围 `done/skipped` 复位为 `pending`，重新入库（配合调整 entity_types 后重放） |
| `python main.py --replay-all` | 全部复位重放（清库重建场景） |
| `python main.py --retry-dead` | `dead → pending` 且 `attempts=0`（人工确认后恢复） |
| `python main.py --stats` | 打印按 status/source/日期分组的计数（审计） |
| `python main.py --rebuild-index` | 扫描所有 JSONL 重建 SQLite 索引（DB 损坏恢复，见 §8.3） |
| `python main.py --dry-run` | **不变**：预览模式，写 `output/dry_run_*.json`，不改库、不写 landing |

### 6.1 与 dry-run 的关系

- `--dry-run`：**预览/校验**用途。fetch → normalize → dedup（内存，不跨 cycle）→ 写 `output/dry_run_{ts}.json`（indent 数组）→ 退出。**不写 landing、不登记状态、不连 Neo4j**。保留原行为，用于人工抽查数据质量。
- `--fetch-only`：**生产抓取**。fetch → normalize → dedup（含跨 cycle 持久化去重）→ 写 landing JSONL + 登记 pending。是 dry-run 的「生产化」形态。
- 两者并存，语义不冲突：dry-run 是「看」，fetch-only 是「存」。

---

## 7. 与现有代码的集成

### 7.1 需要修改的文件

| 文件 | 改动 |
|------|------|
| `main.py` | 新增 `--fetch-only / --ingest-only / --watch / --replay / --replay-all / --retry-dead / --stats / --rebuild-index` 参数与分支；scheduler 构造时注入 `landing_enabled` 等 |
| `src/ingestion/pipeline.py` | `run_pipeline` Stage2 由 `writer.write_batch` 改为 `landing_store.capture_batch`（受 `landing_enabled` 开关控制）；dry-run 分支不变 |
| `src/ingestion/scheduler.py` | 初始化 `LandingStore` + `IngestWorker`；启动时 recovery（lease 复位 + 孤儿文件扫描）；常驻 ingest task；retention 清理任务；`--ingest-only` 模式构造 |
| `src/core/config.py` | 新增 settings：`landing_enabled / landing_dir / landing_retention_days / ingest_batch_size / ingest_poll_interval_sec / ingest_lease_sec / ingest_max_attempts` |
| `src/adapters/gdelt_adapter.py` | **Phase 3 删除** `MAX_EPISODES_PER_CYCLE=80` 截断逻辑（不再需要） |
| `.env.example` | 记录新增环境变量及默认值 |

### 7.2 可复用（不改动）

| 组件 | 复用方式 |
|------|----------|
| 全部 adapter（`src/adapters/*`） | `fetch / normalize / dedup` 完全不变 |
| `EpisodeWriter.write_one` | IngestWorker 原样调用；429 退避、熔断、`_LLM_SEMAPHORE` 全部保留 |
| `NormalizedEpisode.model_dump(mode='json')` | envelope 序列化直接使用（dry-run 已验证） |
| `run_pipeline` Stage1 | fetch → normalize → dedup 编排不变 |
| `--dry-run` 全链路 | 保持原样 |

### 7.3 新增的文件

| 文件 | 内容 |
|------|------|
| `src/persistence/__init__.py` | 模块导出 |
| `src/persistence/models.py` | `EpisodeEnvelope`（pydantic，含 `v/captured_at/cycle_id/episode`）、`EpisodeStatus`（str Enum）、`CaptureRunRecord` |
| `src/persistence/landing_store.py` | `LandingStore`：`capture_batch`（写 JSONL + INSERT OR IGNORE）、`claim_batch`（原子认领）、`complete/fail/skip`（状态更新）、`recover_leases`、`scan_orphan_files`、`replay/since`、`retention_sweep`、`stats` |
| `src/persistence/ingest_worker.py` | `IngestWorker`：异步 drain 循环，认领→读行→反序列化→`write_one`→状态更新；failed 重试扫描；pending 高水位告警 |
| `tests/test_persistence/test_landing_store.py` | capture/claim/complete/fail/replay 单测 |
| `tests/test_persistence/test_ingest_worker.py` | worker 循环、重试退避、lease 恢复单测（mock writer） |
| `tests/test_persistence/test_recovery.py` | 崩溃恢复（孤儿文件、lease 超时）单测 |

### 7.4 迁移策略（分阶段）

> 关键：全程 feature-flag 灰度，任何阶段可回退到旧架构（flag 关闭即回到 `write_batch` 直写）。

**Phase 1 — 基础模块（无行为变化）**
- 新建 `src/persistence/`（models / landing_store / ingest_worker）+ 单测。
- `landing_enabled` 默认 `False`，代码路径未激活。旧架构完全不动。

**Phase 2 — Capture 灰度（双写对比）**
- `landing_enabled=True` 时：`run_pipeline` Stage2 改为 `capture_batch`，IngestWorker 启动消费。
- 在 staging 跑 24–48h，对比三项指标：`landed 总数 == legacy 写入总数`、`done+skipped == Neo4j 新增 episode 数`、零丢失（每个抓到的 episode 都落入 done/skipped/failed/dead 之一）。
- 此时 `MAX_EPISODES_PER_CYCLE` 暂保留（避免 Phase 2 同时引入大量 pending 造成行为剧变）。

**Phase 3 — 全量切换 + 删除 hack**
- 删除 `MAX_EPISODES_PER_CYCLE=80`：capture 不再截断，所有 episode 落盘，IngestWorker 后台慢慢消化（~7.7s/ep 串行）。
- `landing_enabled` 默认 `True`。加监控：pending 数量高水位（默认 3000）告警。
- 内存 `dedup_cache` 降级为 capture 阶段快速路径，权威去重交给 SQLite `INSERT OR IGNORE`。

**Phase 4 — 运营能力**
- retention 清理任务上线；`--replay / --stats / --rebuild-index` 打磨；可选：done 批次 gzip 归档（省 ~5–10× 空间）。
- 可选：评估是否移除内存 `dedup_cache`（若 INSERT OR IGNORE 已足够，可简化为纯持久化去重）。

---

## 8. 风险与边界情况

### 8.1 磁盘空间估算

**JSONL**（实测外推，紧凑格式约为 indent=2 的 35–45%）：
- 单 cycle（Tier1 全源）：0.25–0.8 MB。
- Tier1 频率 96 cycle/天；Tier2/3/4 更低频但单次量大。
- 估量：**30–80 MB/天**，14 天保留 ≈ **0.5–1.2 GB**；极端（GDELT 去重率低、每次 500+ episode）上限 ~3 GB/14 天。

**SQLite**：
- 单行 ~300–400 B；极端 5 万行/天 × 14 天 ≈ 70 万行 ≈ **< 300 MB**（含索引）。
- retention 清理同步删除行，稳态可控。

**缓解**：
- `landing_retention_days` 可调（默认 14）。
- Phase 4 可选 gzip 归档 done 批次（JSON 文本压缩比 5–10×）。
- 监控：`state.db` 大小、`data/landing/` 磁盘占用、pending 数。

### 8.2 并发控制

- **进程内**：asyncio 单事件循环。多个 capture 任务（各 Tier）+ 单一 IngestWorker。每个 (cycle, source) 独立 JSONL 文件 → capture 之间无写冲突；capture 与 ingest 读的是不同文件（capture 写当天新文件，ingest 读历史 pending 文件）→ 无读写竞争。
- **进程间**（scheduler 常驻 + 手动 `--ingest-only`）：SQLite WAL + `busy_timeout=5000`；认领用 `BEGIN IMMEDIATE` 原子，保证同一行只会被一个进程认领。
- **双保险**：graphiti 侧 episode `name` 唯一（`write_one` 的 `_seen_hashes` 内存态在跨进程场景下失效，但 Neo4j 的 episode 节点唯一性由 name 保证——若 graphiti-core 对重名 episode 报错/去重，则天然幂等；此为需在 Phase 2 验证项）。
- **可选加固**：scheduler 单例用 `fcntl.flock` 对 `data/landing/.scheduler.lock` 加 advisory lock，防止误启两个常驻进程。

### 8.3 文件/DB 损坏处理

| 场景 | 处理 |
|------|------|
| 写入中途崩溃（`.tmp` 残留） | 原子 `os.replace` 保证无半成品 `.jsonl`；启动清理 `.tmp` |
| 文件已写、DB 未登记（孤儿文件） | 启动扫描：`capture_runs` 未记录的 `.jsonl` → 逐行 INSERT OR IGNORE（幂等，靠 content_hash PK） |
| DB 已登记、文件缺失 | 认领后读文件失败 → 标记 `failed`，`last_error="batch_file missing"`，告警；其余行继续 |
| 单行 JSON 损坏（截断/非法） | `json.JSONDecodeError` 捕获 → 该行标记 `failed`（`last_error="corrupt line"`），不中断整批 |
| `state.db` 损坏 | `--rebuild-index`：扫描所有 `.jsonl` 重建索引；**注意**：重建后 done 状态丢失 → 已入库 episode 可能被重写。缓解：重建前优先尝试 SQLite 自愈（WAL 恢复）；若不可避免，依赖 graphiti 侧 name 幂等（需验证）。此操作记录告警 |
| 磁盘写满 | capture 失败 → 该 cycle 数据无法落盘，记录 CRITICAL 告警，**绝不在无持久化情况下直写 Neo4j**（宁可丢一个 cycle 的落盘机会也不制造「半入库无快照」状态）；重启后靠磁盘空间恢复重新抓取 |

### 8.4 其他边界

- **同 hash 不同元数据**：`INSERT OR IGNORE` 保留首次落盘版本。后续 cycle 若抓到相同 content 但元数据被增强（如补充 entities），会被忽略——可接受，因为 content 决定知识抽取结果，元数据差异不改变抽取。
- **GDELT 窗口真空期**：进程停机 >15 分钟期间，GDELT 窗口数据「从未被抓到」——持久化层**无法恢复未抓到的数据**（上游限制）。这是本设计的明确边界，需在文档中标注；可选未来方案：GDELT DOC 2.0 全历史 API 回填（超出本设计范围）。
- **backpressure**：清库重建后一次性涌入大量 pending，IngestWorker 按 `ingest_batch_size`（默认 20）串行消化，pending 峰值高水位告警；capture 持续追加不丢数据。
- **时钟**：所有时间戳统一 UTC ISO 8601；`cycle_id` 用 UTC，避免 DST/时区歧义。
- **回滚**：任何阶段关闭 `landing_enabled` 即回到旧 `write_batch` 直写路径，landing 目录可保留不影响。

---

## 9. 配置项清单（新增）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `LANDING_ENABLED` | `false`→`true`(Phase3) | 总开关，控制 Stage2 走 capture 还是直写 |
| `LANDING_DIR` | `data/landing` | landing 根目录 |
| `LANDING_RETENTION_DAYS` | `14` | JSONL + done 行保留天数 |
| `INGEST_BATCH_SIZE` | `20` | 每轮认领行数 |
| `INGEST_POLL_INTERVAL_SEC` | `30` | 队列空时的轮询间隔 |
| `INGEST_LEASE_SEC` | `900` | 认领 lease 超时（须 > write_one 最坏耗时） |
| `INGEST_MAX_ATTEMPTS` | `3` | 失败重试上限，超限转 dead |
| `INGEST_PENDING_HIGH_WATER` | `3000` | pending 高水位告警阈值 |

---

## 10. 验收标准（对应设计要求逐条）

1. **数据不丢失**：抓到的每个 episode 都落盘 JSONL 并登记 SQLite；崩溃后 recover（lease 复位 + 孤儿扫描）恢复 pending。✅
2. **GDELT 分片解决**：删除 `MAX_EPISODES_PER_CYCLE`，capture 不截断，ingest 消费多少处理多少、处理完标 done。✅
3. **可重放**：`--replay --since ...` 将 done/skipped 复位 pending，重放时 `write_one` 读当前 entity_types。✅
4. **断点续传**：进程重启后 IngestWorker 从 `pending` 继续；`processing` 超 lease 复位。✅
5. **清库重建不丢数据**：Neo4j 清库后 `--replay-all` 从保留的 JSONL 全量重建，无需重新抓取。✅
6. **可审计**：原始 JSONL 快照 + SQLite 状态 + `--stats` 查询 + `capture_runs` 去重统计。✅
