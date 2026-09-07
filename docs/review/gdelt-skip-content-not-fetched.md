# Code Review: GDELT 跳过 content_fetched=false episode

- **日期**: 2026-09-07
- **审查员**: Code Reviewer
- **范围**: `src/adapters/gdelt_adapter.py` + 3 个测试文件
- **结论**: **PASS** — 签发 `[CODE-REVIEW-PASS-20260907-GDELT-SKIP]`

## 改动概述

Boss 决策（2026-09-07）：GDELT 模板摘要（GKG ~400 chars / CAMEO ~300 chars）实体抽取质量差、LLM ROI 低，不再入库。

- `normalize()` / `_normalize_event_record()` 返回 `NormalizedEpisode | None`
- 内容抓取后加守卫：`content_fetched=False` → `return None`
- `run()` 新增 Phase 2.5：dedup 前过滤 None
- 计数器 `_skipped_content_not_fetched` + info 级汇总日志

## Review 要点逐项验证

### 1. 守卫位置：LLM preprocess 之前 ✓

**两条路径守卫均在 LLM 调用之前**：

| 路径 | 守卫行 | LLM compress | LLM synthesize (template) |
|------|--------|--------------|---------------------------|
| GKG `normalize()` | L1177 | L1204（`if full_text:` 块内） | L1210-1222（else 死代码） |
| Events `_normalize_event_record()` | L1337 | L1356 | L1374-1393（else 死代码） |

守卫返回 None 时不会触碰 `_llm_preprocessor.preprocess()` —— ROI 点达成。
测试 `test_no_llm_preprocess_call_on_skip` + `test_event_skipped_when_fetch_fails` + `test_gkg_skipped_when_fetch_fails` 均验证 `preprocess.assert_not_awaited()`。

### 2. None 过滤：dedup 之前 ✓

`run()` Phase 2.5（L1672-1683）在 `self.dedup()` 之前过滤 None。`dedup()`（base.py L60）直接访问 `ep.content_hash`，None 会 AttributeError —— 过滤次序正确，且与 `BaseAdapter.run()` 既有契约一致（base.run 本来就过滤 None）。

`asyncio.gather` 单事件循环并发下计数器自增无 await 间隙，无竞态。

### 3. 日志级别：INFO 汇总合适 ✓

- 逐条跳过：`logger.debug`（GDELT 每 15min 窗口可达千条，per-episode INFO 会刷屏；DEBUG 合适）
- `run()` 汇总：`logger.info("skipped %d/%d episodes ...")` —— 每次 cycle 一条，可观测性足够
- 无 fetcher 时 `logger.warning`（全量跳过预警）—— 好

### 4. 测试覆盖：充分 ✓

新增 `TestSkipContentNotFetched` 8 例 + events 路径 3 例改造：

| 场景 | 覆盖 |
|------|------|
| 无 content_fetcher | ✓ `test_skip_when_no_content_fetcher` |
| fetch 返回失败 | ✓ `test_skip_when_fetch_fails` |
| fetch 抛异常 | ✓ `test_skip_when_fetch_raises` |
| 预抓取结果失败 | ✓ `test_skip_when_pre_fetched_result_failed` |
| 无 source_url | ✓ `test_skip_when_no_source_url` |
| 成功路径不跳过 | ✓ `test_not_skipped_when_fetch_succeeds` |
| skip 不触发 LLM | ✓ `test_no_llm_preprocess_call_on_skip` |
| run() 集成（过滤+计数） | ✓ `test_run_filters_skipped_and_counts` |
| events 路径 skip | ✓ `test_event_skipped_when_fetch_fails` |
| gkg 路径 skip | ✓ `test_gkg_skipped_when_fetch_fails` |
| 禁用 preprocessor 仍 skip | ✓ `test_disabled_no_fetcher_skips_episode` |

顺带修复：原 `test_disabled_no_preprocessing_original_behavior`（历史已知失败，LLMPreprocessor 单例断言）被改写为正确 patch `get_settings` 的新测试 —— 消灭一个存量失败。

### 5. 向后兼容：不影响已 ingest 数据 ✓

- 已入库数据零影响（只影响新 episode 的 ingest 决策）
- 外部调用方：仅 scheduler → `pipeline.run_pipeline()` → `adapter.run()`，run() 已过滤 None、返回 list，pipeline 对空列表有优雅处理（L193 `else: logger.debug("no new episodes")`）
- `BaseAdapter.run()` 契约本就容忍 normalize 返回 None（date cutoff 过滤同理）
- 行为变化是刻意的：无 fetcher/抓取失败 → 新 episode 不再入库（旧行为：模板摘要入库）。scheduler 正常模式必配 ContentFetcher（L734），capture 模式亦然；仅 dry-run 无 `--fetch-content` 时全跳过（有 warning 提示）
- health registry `total_episodes` 会降低 —— 符合预期，非回归

## 非阻塞建议（P3）

1. **计数器冗余**：`_skipped_content_not_fetched` 只被测试断言、不被 src 消费（run() 汇总用局部变量 `skipped`），且跨 cycle 累积不重置。建议：run() 直接用 `self._skipped_content_not_fetched` 做汇总并在 run() 末尾归零，或删除该属性——二选一，避免双份状态。
2. **死代码**：两条路径的 `else:`（template body + LLM synthesize）已不可达（content_fetched=True ⟹ full_text 非空真值，`if full_text:` 恒真）。注释已声明作为回滚安全网 + 技术债跟踪 —— 可接受，但 `mode="synthesize"` 在 GDELT 路径已整体失效，建议 Architect 确认 synthesize 模式无其他消费者后安排删除。

## 验证记录

| 套件 | 结果 |
|------|------|
| `tests/test_gdelt_adapter.py` + `test_gdelt_adapter_events.py` | 78 passed |
| `tests/test_adapters/` 全量 | 429 passed |
| `tests/test_integration/test_gdelt_integration.py` | 1 passed + 4 skipped（网络受限，符合预期） |

## 签发

**PASS** — `[CODE-REVIEW-PASS-20260907-GDELT-SKIP]`