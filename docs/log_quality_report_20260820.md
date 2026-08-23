# NewsEngine 运行日志 & 数据库质量分析报告

**分析时间**: 2026-08-21 00:21 (HKT) | **日志窗口**: 2026-08-20 23:40 → 00:15 HKT (~35 min)
**分析者**: code_reviewer (deepseek-v4-flash-0731)

---

## 1. 日志问题清单（按严重度排序）

### 🔴 P0 — 日志级别配置掩盖了所有生命周期信息（进度不可见的根因）
- **`LOG_LEVEL=WARNING`**（`.env`）导致全部应用级 INFO 被抑制：
  - `=== NewsEngine starting ===`、`IngestionScheduler started`、`=== Tier N cycle starting ===`、`=== Tier N cycle complete (dur, guard) ===`、`Tier N cycle: all sources OK [dur] total=Nep`——**全部不可见**
  - 关停序列 `LIFO step 1/4…` / `=== NewsEngine shutdown complete ===` 也被隐藏
- **讽刺的是**：第三方库 scrapling 的 117 条 INFO（`Fetched (200/403) <GET...>` 逐请求日志）**照常输出**——scrapling 有自己的 logger 级别配置，绕过 root level
- **结果**：日志 35 分钟窗口内 **0 条 cycle 标记**（已 grep 验证 `cycle` 出现 0 次）——用户"cycle 到了但不知道跑没跑完"的直接原因
- **修复建议**：`LOG_LEVEL=INFO`；或将 scheduler 的 cycle start/end/complete 日志提升为 WARNING 级别（生命周期关键事件应独立于全局级别可见）

### 🔴 P1 — 进程被强制杀掉（SIGINT 双重打断）
```
16:15:34.054  Received SIGINT — initiating graceful shutdown
16:15:34.237  Double SIGINT received — forcing immediate exit   ← 仅 183ms 后
```
- 优雅关停序列被第二次 Ctrl+C 打断 → **循环任务可能未完成、未写 TTL 清理、Neo4j driver 未关闭**
- 这也部分解释了用户感知的"跑没跑完"——最后一次运行确实没正常结束

### 🟠 P2 — Gemini JSON 解析失败（4 次，3 次伴随重试）
- `Failed to parse structured response: Unterminated string starting at: line 1 column 10 (char 9)` × 4（Task-4331/9866/11854/12094）
- 原始输出含有大量 `\t`、`\b`（退格符）填充——**抓取的正文中残留了 HTML/控制字符**，LLM 结构化输出被污染
- 影响：这些 episode 的 fact 提取进入 salvage（部分挽救），关联到下游 MENTIONS 边缺失 fact
- 修复方向：抽取前清洗控制字符/HTML 实体；或给 gemini 客户端加 pre-parse 清洗

### 🟠 P3 — 抓取对抗高发（WARNING 252 条，占日志近半）
| 模式 | 次数 |
|---|---|
| news_spider 各级指纹被 block + Camoufox fallback | 80+ |
| trafilatura 丢弃数据 / precision 失败 fallback | 91 |
| SSL 连接错误（humanevents.com curl 35, ECB RSS SSL_EOF） | ~10 |
| CloakBrowser anti-bot 挑战页（ktbb.com ×12） | 12 |

- humanevents.com 该篇 **8 次连续 403** 后靠 Camoufox 恢复——单 URL 消耗 ~5 分钟
- 属可接受的自愈链路（Camoufox 成功恢复多次），但噪音巨大且拖长 cycle 时长

### 🟡 P4 — Neo4j UnknownPropertyKeyWarning × 139（纯噪音但暴露 schema 问题）
- `name_embedding` × 19、`episodes` × 50、`reference_time` × 50、`fact_embedding` × 20
- 均为 Graphiti 库内部查询引用未存在属性（Entity 全部有 name_embedding，这些 warning 来自跨标签查询/旧属性名）
- 无功能影响，但占 WARNING 总量 27%，污染日志检索

### 🟡 P5 — 全局配置噪音
- ACLED 未配置（每次 cycle 都提示）、google_genai AFC 弃用警告、`Source/Target entity not found in nodes` 边提取警告（RELATED_TO/PUBLISHED_ARTICLE_BY）

---

## 2. 数据库质量评估

**图规模**（全部为 2026-08-20 写入，库内无更早数据——TTL 默认值为 MACRO 14 天 / SECTOR 7 天 / SYMBOL 3 天，排除 TTL 因素，推断**图被重建过或今日首跑**）

| 指标 | 值 |
|---|---|
| Episodic 节点 | 174（rss 51 / cls 50 / gdelt 28 / cninfo 16 / eastmoney 8 / 宏观数据源 ~21）|
| Entity 节点 | 966（Organization 433 / Country 140 / Topic 19 / Stock 14 / Policy 11 / Sector 6 / Event 2 / 裸 Entity 341）|
| 关系 | 1843（MENTIONS 1309 / RELATES_TO 534）|
| 孤立实体 | 0 ✅ |

### 提取质量评价

**✅ 好的方面**
- **正文质量良好**：CLS 中文新闻、RSS 英文文章内容完整干净；**0 条 episode 含控制字符填充**（\t\b 污染被挡在写入前）
- **实体提取合理**：hub 节点符合预期（United States 60 度、贵州茅台 27、Iran 26、中国 23、平安银行 21）；短电报类（CLS avg 3.1 实体/篇）与实际内容吻合
- 结构化类型体系工作正常（Organization/Country/Stock/Policy 分层）

**🔴 B1 — MENTIONS 边全部缺失 fact 与 mention_count（1309/1309 为空）**
- 边仅有 `created_at/group_id/uuid` 三个属性；而 RELATES_TO 的 fact 是完整的（534/534）
- **后果**：无法回答"为什么提到这个实体"——检索/解释链路数据缺失
- 方向：检查 graphiti extract_edges 的 fact 提取配置或版本差异（很可能是 P2 Gemini 解析失败 + salvage 链路的结果，也可能配置未开启）

**🟠 B2 — content_scope 缺失 21/174（12%）**
- 有值是 129 MACRO + 24 SYMBOL；缺失的全部是宏观数据适配器：**fred / bls / treasury / china_macro**
- 根因：这些 adapter 的 NormalizedEpisode.metadata 未带 `content_scope`，writer 的透传逻辑对它们写空
- 后果：TTL 分级清理（SYMBOL 3d/SECTOR 7d/MACRO 14d）对这类节点**永远匹配不上** → 永不淘汰；且 API 层按 scope 查询会漏

**🟠 B3 — 实体重复（同一实体多个节点）**
- 样例：`Bougainville Copper` / `Bougainville Copper Ltd.` / `Bougainville Minerals` / `Bougainville Minerals Ltd.` / `Bougainville` 5 个节点；`Lloyds Metals` vs `Lloyds Metals & Energy Ltd.`
- 关联：**342/966 实体缺 `entity_name`**（35%），与去重/引用解析失败吻合
- 方向：graphiti 实体解析的 canonical name 规范化 + 合并逻辑需检查

**🟡 B4 — 66 个实体无 summary（6.8%）**
- 与日志 `attribute_le…(truncated)` 截断警告相关

---

## 3. 进度可见性问题分析

### 现状
- `LOG_LEVEL=WARNING` 导致所有 cycle 生命周期日志被抑制
- 用户无法从日志判断：
  - cycle 是否开始
  - cycle 是否完成
  - cycle 耗时多久
  - 处理了多少条数据

### 改进建议

**短期（改配置）**：
1. `.env` 改 `LOG_LEVEL=INFO`
2. 或将 scheduler 的关键生命周期日志提升为 WARNING：
   - `=== Tier N cycle starting ===`
   - `=== Tier N cycle complete ===`
   - `Source X: N episodes written`

**中期（增强日志）**：
1. 每个 cycle 输出摘要：
   ```
   [Tier 1] Cycle complete | duration=45s | sources=5 | episodes=23 | errors=0
   ```
2. 每个 source 输出进度：
   ```
   [RSS] Fetching... (3/10 feeds)
   [RSS] Complete | duration=12s | episodes=8
   ```
3. 可选：输出到 stdout 或独立 progress.log

**长期（监控面板）**：
1. Prometheus metrics 或简单 HTTP endpoint `/metrics`
2. 暴露：cycle_count、last_cycle_duration、episodes_total、errors_total

---

## 4. 修复优先级建议

| 优先级 | 问题 | 修复工作量 |
|--------|------|-----------|
| **P0** | LOG_LEVEL=WARNING 掩盖生命周期 | 改 .env 配置，5 分钟 |
| **P1** | MENTIONS 边缺失 fact | 检查 graphiti 配置/版本，可能需要升级或调整参数 |
| **P1** | content_scope 缺失（fred/bls/treasury/china_macro） | 改 adapter 代码，补 metadata |
| **P2** | Gemini JSON 解析失败 | 加 pre-parse 清洗，或换用更稳定的 LLM |
| **P2** | 实体重复 | 检查 graphiti 实体解析配置 |
| **P3** | 抓取对抗噪音 | 优化 fallback 策略，减少 WARNING 输出 |

---

## 5. 总结

**好消息**：
- 数据源全部跑通，174 episodes 写入成功
- 实体提取质量良好，hub 节点符合预期
- 自愈链路（Camoufox fallback）工作正常

**待修复**：
- 日志级别配置（P0，5 分钟搞定）
- MENTIONS 边 fact 缺失（P1，需排查 graphiti 配置）
- content_scope 缺失（P1，改 adapter 代码）

**进度可见性**：
- 当前完全不可见（LOG_LEVEL=WARNING）
- 短期改配置即可解决
- 中期可增强日志输出

---

**报告生成时间**: 2026-08-21 00:35 HKT
**下一步**: 明日根据优先级修复
