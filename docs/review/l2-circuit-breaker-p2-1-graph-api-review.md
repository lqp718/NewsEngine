# Code Review — L2 断路修复 + P2-1 图结构 API

- **日期**: 2026-09-06
- **审查人**: Code Reviewer
- **结论**: **NEEDS_FIX**（1× P1，修复后可签发 PASS）
- **范围**: `briefing_aggregator.py` / `scheduler.py` / `deps.py` / `events.py` / `models.py` / 新增测试

---

## 验证方式

- 全量定向测试: `tests/test_api/test_events_graph_structure.py` + `test_events_sector_briefing.py` + `test_events_entity_query.py` + `test_events_sector_query.py` → **52 passed**
- 真实 Neo4j 实测（3698 Entity / 595 Episodic / 3034 RELATES_TO）: 图查询 3 跳 3.6ms、episodes 17.7ms、events 14.8ms —— 无超时风险
- 进程拓扑确认: `main.py` 单进程单事件循环（scheduler + programmatic uvicorn 共享 loop）→ 进程级单例是正确修复方向
- 真实驱动返回类型实测: `neo4j.time.DateTime`（非 `datetime.datetime`）

---

## P1 — episodes.valid_at 生产环境恒为 None（Bug 1）

**位置**: `src/api/routers/events.py` → `_build_episode_items()`

```python
"valid_at": to_iso8601(valid_at_raw)
if isinstance(valid_at_raw, datetime)
else None,
```

**问题**: Neo4j 驱动返回 `neo4j.time.DateTime`，它不是 `datetime.datetime` 子类。实测真实查询结果 `type(va).__name__ == 'DateTime'`、`isinstance(va, datetime) == False` → 生产环境 **episodes[].valid_at 永远为 None**，且 `sort(key=lambda it: it.valid_at or "")` 退化为插入序，「事件脉络按时间升序」契约失效。

**为什么测试没抓到**: 测试用普通 `datetime` 对象喂 `_build_episode_items`；无任何用例覆盖真实驱动类型形态。这是本改动唯一的 P1。

**复现证据**（真实 Neo4j + 真实查询构造器）:

```
to_iso8601 直接传 neo4j DateTime → TypeError: DateTime.iso_format() got an unexpected keyword argument 'timespec'
va.to_native() → 2026-08-28 16:00:00+00:00
to_iso8601(va.to_native()) → '2026-08-28T16:00:00+00:00Z'  ✓
```

**修复建议**（二选一）:

```python
if isinstance(valid_at_raw, datetime):
    valid_at = to_iso8601(valid_at_raw)
elif hasattr(valid_at_raw, "to_native"):
    valid_at = to_iso8601(valid_at_raw.to_native())
else:
    valid_at = None
```

并补一条用 `neo4j.time.DateTime` 输入的回归测试（断言 valid_at 非 None 且升序排序仍然成立）。

---

## 通过项

### L2 单例（线程安全 / dry_run 语义）
- `get_shared_aggregator()` 懒初始化，monkeypatch 回归测试验证幂等性；同进程单事件循环下无竞态（`get_cached` 无 await 点，dict 读写原子）
- dry_run 语义正确: `IngestionScheduler.__init__` 中 `self._aggregator = get_shared_aggregator() if not dry_run else None`，`aggregate_all` 调用点有 `if sector_names and self._aggregator` 双保险；dry-run 路径（`main_dry_run`）不初始化 Neo4j/聚合器
- `deps.py::get_aggregator` 变为薄代理，删除自建实例（L2 断路根因回归测试覆盖）

### L2 缓存读取与降级
- `get_sector_events` 正确读 `aggregator.get_cached(sector_name)`；miss → None、异常 → 捕获降级 None，均不阻断 events 主链路；响应不再硬编码 None（回归测试 `test_no_hardcoded_none_in_response_path`）

### P2-1 向后兼容
- `EntityEventsResponse` 仅新增 `graph`/`episodes`（default None），既有 `ticker`/`events`/`summary` 契约不变；`include_graph=false` 时跳过图查询且字段为 None；图查询异常 → 降级 None 不阻断主链路（fake driver 注入异常测试通过）
- `EventRelationItem`/`relations` 删除系此前 P2-3 已审查签发的死字段清理，非本次引入

### Cypher 性能
- 变长遍历 `[:RELATES_TO*1..hops]` + `hops` 字面量内插前有 `1..3` 防御校验（杜绝注入面）；`$edge_limit=200` 兜底
- 实测 3 跳图查询 3.6ms / episodes 17.7ms；hub 实体（China 度 71）在当前数据量下无爆炸风险
- `UNWIND (rels + [null])` 保证孤立 start 节点仍产出（nodes 至少含目标股票）✓；边按 (src,tgt,type) 去重、节点按 name 去重 ✓

### 响应模型
- `GraphNode`（id=规范名、type 走 LABEL_TYPE_MAP + ticker 兜底）、`GraphEdge`（type 取 RELATES_TO.name、缺失兜底 RELATED_TO、附 fact）结构合理；`entity_type_from_labels` 已在 translation 层复用同一枚举

### 测试覆盖
- 52 passed：单例幂等/委托/dry_run 四态、缓存命中/miss/异常三态、graph 组装去重/孤立行/兜底、episodes 归组/去重/标题首行、端点 include_graph 开关与降级
- **缺口**: 无 neo4j.time.DateTime 形态用例（即 P1 漏网原因），需补

---

## P2（不阻塞，建议）

1. **`Entity.ticker` 无索引**: `SHOW INDEXES` 确认 Entity 仅有 name/created_at/group_id/uuid 索引，图查询与 events 查询的启动点 `MATCH (start:Entity) WHERE start.ticker=$ticker` 均为 label scan。当前 3698 节点实测毫秒级，数据量增长后建议补 `CREATE INDEX entity_ticker IF NOT EXISTS FOR (e:Entity) ON (e.ticker)`（本次图查询为此模式新增消费方，值得一并落地）
2. 边去重键 `(src,tgt,type)` 未含 `fact`，同型同两端不同 fact 的边会丢后者 —— 当前数据可接受，注明即可