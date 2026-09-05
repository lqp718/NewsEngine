# EIA Adapter 优化记录 (2026-08-14)

## 背景
EIA adapter 已修复可正常工作，但存在隐藏 dedup bug + 需要按官方文档优化。

## 1. URL dedup Bug 修复
- **问题**：WCRIMUS2/WCREXUS2 同路由 `petroleum/move/wkly`，`normalize()` 生成的
  `source_url` 不含 series 标识 → 两个系列 URL 相同 → `BaseAdapter.dedup()` 的 URL 检查
  把第二个当重复丢弃。
- **修复**：`_build_eia_source_url(series_id)` — source_url = route + 该系列 facets 的
  query string（`facets[series][]=WCRIMUS2` vs `facets[series][]=WCREXUS2`），
  用 `urllib.parse.urlencode` 正确编码（`%5B`/`%5D`）。
- **验证**：dry-run 5 fetched / 0 filtered / 5 normalized；跨周期模拟 Cycle1=5, Cycle2=0(去重), Cycle3=5。

## 2. EIA API 文档要点（研读自 https://www.eia.gov/opendata/documentation.php）
- **频率限制**：超每秒/每小时阈值 → key 临时挂起（冷却后自动恢复）。需节流 + 尊重 Retry-After。
- **错误处理**：HTTP 状态码 + JSON body `{"error": "...", "code": 400}`；in-return `warning`
  （如 5000 行截断警告）。
- **最佳实践**：`data[]` 只取所需列；facets 收窄；`length` 限行数；`sort` 保证确定性顺序；
  `start`/`end` 约束日期范围（减少 payload）；JSON 值自 2024-01 起统一为字符串。
- **数据格式**：v2 RESTful 路由，metadata 查询 `/v2/{route}` 可发现 facets/frequency。

## 3. 代码优化（按文档）
- `frequency=weekly` 显式指定（`petroleum/pri/gnd` 还支持 monthly/annual，显式指定防默认值漂移）
- `start` 参数（65 天回看 = 60 天窗口 + 5 天缓冲，API 侧只做粗过滤，normalize 做权威过滤）
- `_get_with_retry()`：429/5xx 指数退避重试（最多 2 次），尊重 `Retry-After` 头
- `_eia_error_message()`：从 JSON error/warning body 提取可读错误信息
- 响应内 `warning` 字段记录日志
- 系列间 0.25s 节流延迟（5 系列 ≈ +1.25s/周期）

## 验证
- `pytest tests/test_adapters/test_eia_adapter.py` → 23 passed（原 11 + 新增 12）
- `pytest tests/test_adapters/` → 356 passed, 1 skipped
- `main.py --dry-run --source eia --fetch-content` → 5/5 episodes, ~4.2s
- 注意：`tests/test_integration/test_graphiti_integration.py` 5 failed 1 error 为
  既有环境问题（需真实 Neo4j，event loop 兼容性），与 EIA 无关（该文件 0 处 EIA 引用）。
