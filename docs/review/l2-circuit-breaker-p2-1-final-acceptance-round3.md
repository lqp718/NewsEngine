# 最终 Code Review（第三轮）：L2 断路 + P2-1 图结构 API + valid_at 全链路修复

- 日期: 2026-09-06
- 范围: 全链路验收（scheduler → aggregator 单例 → API 端点 → translation → time_utils）
- 结论: **PASS** — 签发 [CODE-REVIEW-PASS-20260906-R3]

---

## 1. Review 清单逐项验收

### 1.1 ✅ L2 单例：scheduler 和 API 共享同一个缓存
- `src/ingestion/briefing_aggregator.py:264` `get_shared_aggregator()` — 进程级 `_shared_aggregator` 单例，懒初始化
- `src/ingestion/scheduler.py:383` — `IngestionScheduler(dry_run=False)._aggregator = get_shared_aggregator()`；`dry_run=True` 保持 `None`（原语义不变，测试覆盖）
- `src/api/deps.py:136-138` `get_aggregator()` — 直接委托 `get_shared_aggregator()`，杜绝自建实例（L2 断路根因回归修复）
- `main.py` 以编程式 `uvicorn.Server` 启动，scheduler 与 API 共享同一事件循环、同一进程 → 单例缓存可互通
- **验证**：`tests/test_api/test_events_sector_briefing.py` 四项单例断言（幂等、deps 委托、scheduler 非 dry-run 同源、dry-run 为 None）+ 运行时 import 无循环依赖

### 1.2 ✅ L2 端点：`/events/sector/{name}` 返回 sector_briefing
- `get_sector_events()` 从 `aggregator.get_cached(sector_name)` 读取：
  - 命中 → 返回 Markdown 简报
  - 未命中 → `None`（消费方降级自行聚合）
  - 缓存读取异常 → `None` 且不影响 events 主链路（try 包裹）
- **验证**：4 个用例覆盖 命中/未命中/异常降级/非硬编码 None
- 写入方键：scheduler `_extract_sector_names()` 取 whitelist 的 `sector` 字段（实测 35 个中文板块名，如 白酒/互联网/半导体…）；读取方键：URL `sector_name` — 键契约一致（均中文名，与 `:Sector` 实体 name 对齐）

### 1.3 ✅ P2-1：`/events/entity/{ticker}` 返回 graph 字段
- `_build_entity_graph_query(hops)` — 变长路径 `*1..{hops}`（1..3 字面量内插 + 双侧校验防注入）；`hops` 越界抛 ValueError（测试覆盖 0/4/-1）
- `_build_graph_structure()` — 节点按 name 去重、边按 (source,target,type) 去重、孤立起点行（rel=NULL）仍产出 start 节点、edge_type 缺失兜底 `RELATED_TO`
- `_build_entity_episodes_query(hops)` — 1..hops 内实体参与的 Episodic 事件脉络，`$window_days` 与 events 主查询一致
- `_build_episode_items()` — 按 ep.uuid 归组、实体去重、title 取 content 首行（回退 name）、valid_at 升序、`$episode_row_limit`
- `include_graph=true`（默认）→ graph+episodes；`false` → 不发图查询且字段 None；图查询异常 → 降级 None，events 不受影响
- `GraphNode/GraphEdge/GraphStructure/EpisodeItem` 模型在 `src/api/models.py`（140-215 行），序列化形状与诊断报告样例一致（测试覆盖）
- 向后兼容：`EntityEventsResponse` 新增 `graph`/`episodes` 为可选字段（默认 None），既有 `ticker/events/summary` 契约不变（测试 `test_legacy_payload_still_valid`）

### 1.4 ✅ valid_at：所有端点时间戳正确
- **根因**：neo4j.time.DateTime 不是 datetime 子类 → 旧 `isinstance()` 检查静默回退 `now_hkt()`
- **修复**：`time_utils.coerce_datetime()`（datetime 直通 / 有 `to_native()` 则调用并校验 / 否则 None）+ `translation.py` 两处（`translate_episode_to_event` 146-152、`translate_episode_to_briefing_input` 287-291）全量改用
- **端点级验证**（fake driver 喂 Neo4j DateTime HKT 2026-09-01 20:30）：
  - `/events/active` → first_seen == `2026-09-01T12:30:00+00:00Z`（UTC 等值）✅
  - `/events/entity/{ticker}` → 同上 ✅
  - `/events/sector/{name}` → 同上 ✅
  - `/events/risk-summary` → 经同一 `translate_episode_to_event` 路径（EventItem 构造）✅
  - 排序：按真实 valid_at 降序（非插入序）✅
  - 手动脚本复验：`translate_episode_to_event` 对 neo4j.time.DateTime 输出 `2026-09-01T12:30:00+00:00Z` / `13:00:00+00:00Z` ✅
- 全仓 `grep` 确认无残留 `isinstance(x, datetime)` 业务分支（仅 time_utils 内部两处受控判断）

### 1.5 ✅ 向后兼容
- EventItem 字段（ticker/events/summary/…）保留；graph/episodes 可选
- `translate_episode_to_event` 的 `{"e": ep}` 记录形态在 4 个端点共用，签名不变
- `dry_run` 调度语义不变
- `CORE_EDGE_TYPES` 移除 INVESTS_IN/EXPOSED_TO（P1-3，前轮已验收），归一回落 RELATES_TO，测试覆盖废弃类型不产出

### 1.6 ✅ 测试覆盖
- 本轮 5 个新测试文件：sector_briefing(8) / datetime_coercion(12) / graph_structure(19) / entity_query(8) / sector_query + 原有
- `tests/test_api/` 全部 **71 passed**
- 全仓（排除 test_e2e）：**803 passed, 4 skipped, 14 failed, 1 error** — 14 个失败全部为 **HEAD 已存在的存量失败**（见 §3），本轮到文件无一命中

---

## 2. 代码质量观察（非阻断）

| 位置 | 说明 | 处置 |
|---|---|---|
| `_build_high_risk_query` | `episode_metadata CONTAINS 'MACRO'` 为字符串包含匹配，理论上有误报风险（SYMBOL 事件元数据中出现 "MACRO" 字样）。**HEAD 已存在，非本轮改动** | 记录，建议后续改为结构化 `content_scope` 字段或 `apoc.convert.fromJsonMap` |
| `_build_graph_structure` | 去重键不含 fact（注记已声明）；当前数据单 fact，可接受 | 已注明 |
| graphiti driver close | 集成测试 `RuntimeError: Task attached to a different loop` 来自 graphiti_core 三方库，非本仓库代码 | 记录，需在 graphiti 升级/锁环修复 |

---

## 3. 存量失败清单（与 HEAD 对照，非本轮引入）

| 测试 | 根因 | 是否本轮 diff |
|---|---|---|
| test_sync/test_multi_tier_scheduler.py ×3 | 测试 fake 缺 `landing_store` 参数（HEAD 的 scheduler 已传） | 否（test 文件未改） |
| test_integration/test_graphiti_integration.py ×9（+1 error） | graphiti_core asyncio loop 冲突；`len(MACRO_ENTITY_TYPES)==6` 断言过时（代码 7 项，含 Policy 类型，entity_types.py 未改） | 否 |
| test_graphiti/test_local_provider.py ×2 | `_LLM_SEMAPHORE` 值断言与设置不同（20 vs 3），该文件最后修改于 6b0b3c6 | 否 |
| test_adapters/test_gdelt_adapter_events.py ×1 | LLMPreprocessor 单例存在断言 `is None` 失败；gdelt/llm_preprocessor/测试均未改 | 否 |

---

## 4. 结论

**PASS** — 签发 `[CODE-REVIEW-PASS-20260906-R3]`

三项目标全部达成：
1. L2 断路：scheduler/API 共享进程级单例，写读同缓存
2. P2-1：图结构 API（nodes+edges+episodes）完整、参数化、越界防御、降级不阻断
3. valid_at：`coerce_datetime` 全链路（4 端点 + translation 2 处）统一归一化，真实事件时间不再回退当前时间

无新增阻断项；存量 14 个失败均与 HEAD 一致，不阻塞本轮签发。建议后续单独清理存量失败（尤其是 stale 断言与 graphiti 三方库 loop 冲突）。