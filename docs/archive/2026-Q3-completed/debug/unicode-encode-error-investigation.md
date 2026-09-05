# Stage B Ingest UnicodeEncodeError 根因调查报告

> 调查时间：2026-08-27
> 调查范围：只调查不修改代码
> 状态：**根因已 100% 确认（含可复现实验）**

---

## 结论速览

**根因不在代码，不在 JSON 中间层，而在 `.env`：三个 API Key（OPENAI_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY）被"打码"了 —— 值中间混入了字面量省略号字符 `…`（U+2026），不是合法的 ASCII Key。**

两种错误分布对应两种失败路径，全部由同一个根因触发：

| 错误形态 | 数量 | 触发路径 |
|---|---|---|
| `UnicodeEncodeError: 'ascii' codec can't encode character '\u2026' in position 13` | 741 (68.5%) | OpenAI 兼容客户端（provider=`local`/`openai`）→ httpx 构建 HTTP 头 → `Authorization: Bearer ***…` 含 U+2026 → ASCII 编码崩溃（发生在任何网络请求之前） |
| `Exception: `（空消息） | 340 (31.5%) | Gemini 客户端（provider=`gemini`）→ Google 返回 `401 UNAUTHENTICATED`（Key 无效）→ graphiti `gemini_client.py` 第 361 行 `raise Exception from e`（裸 `Exception()`，无消息） |

**"position 13" 之谜：** 出错的不是 episode body（body 里 `…` 在 position 5209），而是 HTTP 头 `Authorization` 的值：
`"Bearer "`（7 字符）+ `sk-xxx…yyyy`（11 字符，`…` 在第 6 位）→ `…` 恰好落在这个 18 字符字符串的 index 13。已用脚本精确复现，报错信息逐字一致。

---

## 1. 完整调用链（来自 logs/news_engine.log.1 的真实 Traceback）

```
episode_writer.py:337  write_one → self._graphiti.add_episode(...)
graphiti.py:1122       add_episode → extract_nodes(...)            ← LLM 实体抽取
node_operations.py:250 _extract_nodes_single → _call_extraction_llm
node_operations.py:275 → llm_client.generate_response(...)
openai_generic_client.py:222 → _generate_response_with_retry
openai_generic_client.py:155 → self.client.chat.completions.create(...)
openai/_base_client.py:1794 → _post → request
openai/_base_client.py:1515 → request → _build_request
openai/_base_client.py:495  → _build_request → _build_headers
openai/_base_client.py:439  → headers = httpx.Headers(headers_dict)   ← 崩溃点
httpx/_models.py:156        → _normalize_header_value(v, encoding)
httpx/_models.py:82         → value.encode(encoding or "ascii")
UnicodeEncodeError: 'ascii' codec can't encode character '\u2026' in position 13
```

**关键事实：**
- `.venv` 中 httpx 的头部规范化按 HTTP 规范强制 ASCII（`value.encode(encoding or "ascii")`），这是 httpx 的正确行为，不是 bug。
- openai SDK 的 `default_headers` 包含 `"Authorization": f"Bearer {api_key}"`（`AsyncOpenAI(api_key=...)` 构造），key 值原样进头。
- 错误发生在**任何网络请求发出之前**（`_build_request` 阶段），所以 741 条全部确定性失败，与 llama-server 是否在跑、episode 内容是什么无关。

## 2. 证据链

### 2.1 `.env` 中的 Key 含 U+2026（已脚本确认，打码输出）

```
KEY OPENAI_API_KEY: 11 字符, 非 ASCII 位于 index 6: 0x2026 '…'   （形如 sk-xxx…yyyy）
KEY GEMINI_API_KEY: 同样 index 6 为 0x2026
KEY DEEPSEEK_API_KEY: 同样 index 6 为 0x2026
```

> 三个 Key 全部是同一打码模式（`前缀3字符 + … + 后缀4字符`），是人为打码/脱敏的结果。`.env.example`（git 受管）中无 U+2026 —— 打码只发生在本地 `.env`。

### 2.2 精确复现（venv 内运行，与线上报错逐字一致）

```python
from dotenv import dotenv_values
import httpx
key = dotenv_values('.env')['OPENAI_API_KEY']
header = 'Bearer ' + key
# len(header)=18, 非 ASCII 位于 index 13: 0x2026 '…'
httpx.Headers({'Authorization': header})
# → UnicodeEncodeError: 'ascii' codec can't encode character '\u2026' in position 13: ordinal not in range(128)
```

### 2.3 `Exception: `（空消息）的来源

`graphiti_core/llm_client/gemini_client.py` 第 355-362 行：

```python
except Exception as e:
    ...
    logger.error(f'Error in generating LLM response: {e}')
    raise Exception from e      # ← 裸 Exception()，str() == ''，只保留 __cause__
```

- Google 对无效 Key 返回 `401 UNAUTHENTICATED`（当前 logs/news_engine.log 中可见 406+ 条）。
- gemini_client 捕获后 `raise Exception from e` → 上层 `write_one` 捕获 → `f"{type(exc).__name__}: {exc}"` = `"Exception: "`（空消息）。
- 日志中对应形态：`write_one failed after 5 attempts for 'gdelt_events-...': `（冒号后为空）✅。

### 2.4 数据侧佐证（state.db）

```
错误类型     条数   updated_at 范围                     attempts
Unicode    741   2026-08-26T18:46Z → 22:22Z           3（全部）
Exception  340   2026-08-26T18:53Z → 22:16Z           3（全部）
```
- 两组错误在同一批 ingest 运行窗口内交错出现 → 同一队列被**两个不同 provider 配置的进程**同时消费（见 §4 时间线）。
- `attempts=3` 说明每行被 3 轮 fail() 后置 `dead`（`write_one` 内部每轮又重试 5 次）。

### 2.5 佐证：为什么"数组"持续失败但形态不同

| 日志文件 | 时间范围 (UTC) | 主要错误 |
|---|---|---|
| news_engine.log.5 | 08-21T07:28Z → 08-26T15:03Z | 前段仅 RSS SSL 警告；**08-26T13:02Z 起** 401 + Unicode 同时出现 |
| news_engine.log.4 | 08-26T15:03Z → 22:22Z | Unicode ×4375 |
| news_engine.log.3 | 08-26T15:03Z → 17:53Z | 401 ×3020 + Unicode ×1692 |
| news_engine.log.2 | 08-26T17:52Z → 21:47Z | 401 ×~4400 |
| news_engine.log.1 | 08-26T17:53Z → 22:16Z | Unicode ×2627（含完整 traceback）|
| news_engine.log（当前）| 08-26T21:47Z → 22:16Z | 401（logger=gemini_client）+ 空消息 write_one 失败 |

## 3. 历史对比：之前是怎么"没有这个问题"的

### 3.1 git 历史搜索结论

| 搜索 | 结果 |
|---|---|
| `git log --grep="unicode\|encode\|ascii\|utf" -i` | 仅 04677f0（README 文档），**无任何代码修复 commit** |
| `git log -S "ensure_ascii"` | 命中 9 个 commit，全部只是 CLI `--stats` 输出 `json.dumps(..., ensure_ascii=False)`，与本次 bug 无关 |
| `git log -S "UnicodeEncodeError"` | **0 命中** |
| `git log -- src/graphiti/episode_writer.py` | 最近改动 6b0b3c6（local provider + 并发配置化）/ ce79197（P0-P2 修复），均不涉及编码 |

**结论：本项目从未有过针对该 UnicodeEncodeError 的修复。**

### 3.2 时间线重构（为什么 Boss 觉得"JSON 中间层之前没问题"）

```
08-21 ~ 08-25 白天  ingest 时代（JSON 层之前）：
                   失败形态 = gemini 429 限流（"Rate limit exceeded"）+ Neo4j 约束错误
                   （"name cannot be used as an attribute for Person as it is a
                     protected attribute"）—— 都是可重试/可恢复的业务错误，
                   不会让"整队列"变 dead，且当时没有 state.db 持久化失败状态，
                   失败只在日志里闪现 → 体感"没有这个问题"。

08-25 18:47 +0800   6da6f24 JSON 持久化层落地（Stage A/B 解耦）。此时一切仍正常。

08-26 ~21:02 +0800（13:02Z）  .env 三个 Key 被打了码（混入 U+2026）——
                   这是".env 事故"，与 JSON 层无关。从此任何 LLM 调用必失败:
                   · openai/local provider → httpx ASCII 头崩溃（UnicodeEncodeError）
                   · gemini provider    → 401 → 裸 Exception

08-26 22:26 +0800（14:26Z）   .env 最后一次修改（当前最终态：provider=local）

08-26 23:03 +0800（15:03Z）   调度器 + 手动 --ingest-only 多进程开跑，
                   1081 条 pending 全部 3 轮失败 → dead
```

**为什么 JSON 层"背锅"：** JSON 层把失败从"日志里的一行行"变成了"state.db 里 1081 条 dead 的可见事实"，且第一次让 Stage A 成功、Stage B 全灭的对比变得极其醒目。但真正引入 bug 的是 08-26 晚上的 Key 打码。JSON 层本身（6da6f24）没有引入任何编码问题。

### 3.3 旧代码编码处理回顾

- 旧 pipeline 用 `writer.write_batch()` / `write_one()` 直写，与现在的写入路径一致（同一个 `add_episode`）。
- 项目代码所有 `.encode()` 均显式 `"utf-8"`；本 bug 不在我们的代码里。
- 旧（JSON 层前）失败形态与现在不同（429/Neo4j 约束），佐证"不是编码问题一直存在、只是现在才暴露"，而是 Key 被打码后才出现。

## 4. 为什么两种错误交错存在（进程拓扑推断）

日志文件时间窗口大量重叠（log.3 与 log.4 同时从 15:03Z 开始，log.1 与 log.2 覆盖同一窗口），且 DB 中两种错误 interleave —— 典型的多进程并发消费同一队列：

- **进程 A**：早于 14:26Z 启动，加载了旧 .env 快照（provider=gemini、Key 无效但当时为 ASCII 或同样打码）→ 401 路径 → `Exception: ` 行
- **进程 B**：15:03Z 后启动，加载最终 .env（provider=local）→ UnicodeEncodeError 路径

两进程各自持有独立日志文件句柄 → 日志文件重叠。此推断不影响根因结论。

## 5. 修复建议

### 5.1 立即修复（恢复数据，不动代码）

1. **把 `.env` 中三个 Key 换回真实的 ASCII Key**（OPENAI_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY）。
   - 若用 `local` provider（llama-server 不校验 Key），可填 ASCII 占位如 `sk-local`（注意 `src/core/config.py:385` 的占位符黑名单：`***`、`sk-***`、`your-api-key`、`YOUR_API_KEY`、`your_api_key` 会被拒，`sk-local` 不在此列）。
   - 任一 Key 都**必须全 ASCII**（可用 `grep -nP '[^\x00-\x7F]' .env` 检查）。
2. 确认 `GRAPHITI_LLM_PROVIDER` 与真实在用 LLM 一致。
3. 恢复后执行：
   ```bash
   .venv/bin/python main.py --retry-dead   # dead → pending, attempts=0（1081 条）
   .venv/bin/python main.py --ingest-only  # 重新入库
   ```

### 5.2 防御性改进（建议的代码改动，本次未实施）

1. **`src/core/config.py` `validate_openai_api_key` 增加非 ASCII 校验**：`if any(ord(c) > 127 for c in v): raise ValueError("API Key 含非 ASCII 字符...")` —— 启动即失败并给出明确报错，而不是 1081 条 dead 之后才从海量日志里查。
2. （可选）给 Gemini 客户端做与 `BailianOpenAIClient` 同款的子类包装，修复 graphiti `raise Exception from e` 丢失错误消息的问题，让 `last_error` 能记录真实原因（401）。此为观感改善，非必须。
3. （可选）为 `--retry-dead` 之前的行做 dry-run 校验（`--ingest-only` 单条试点），避免再次整队列打 dead。

### 5.3 不需要的改动

- **不要改 httpx / openai / graphiti-core**：httpx 对头部强制 ASCII 是 HTTP 规范的正确实现；根本问题是不合法的 Key。
- **不要回滚 JSON 中间层**：与本次故障无关。

## 6. 附：复现脚本（已执行，输出与线上一致）

```python
# .venv/bin/python
from dotenv import dotenv_values
import httpx
key = dotenv_values('.env')['OPENAI_API_KEY']
header = 'Bearer ' + key
print(len(header))          # 18
for i, c in enumerate(header):
    if ord(c) > 127:
        print(f'non-ascii at index {i}: {hex(ord(c))}')   # index 13: 0x2026
httpx.Headers({'Authorization': header})  # → 与线上完全相同的 UnicodeEncodeError
```

## 7. 涉及文件清单

| 文件 | 角色 |
|---|---|
| `.env`（本地，非 git） | **根因所在**：3 个 API Key 被打码含 U+2026 |
| `.venv/.../httpx/_models.py:82` | 崩溃点（行为正确，非 bug） |
| `.venv/.../openai/_base_client.py:439,651` | Bearer 头组装点 |
| `.venv/.../graphiti_core/llm_client/openai_generic_client.py:155` | OpenAI 兼容客户端调用点 |
| `.venv/.../graphiti_core/llm_client/gemini_client.py:361` | `raise Exception from e`（空消息来源） |
| `src/persistence/ingest_worker.py:337-365` | 错误落库路径（记录 `type: msg`） |
| `src/graphiti/episode_writer.py:410-464` | write_one 重试与错误字符串组装 |
| `src/core/config.py:377-389` | API Key 校验器（缺非 ASCII 检查） |
| `data/landing/state.db` | 1081 条 dead（`--retry-dead` 可恢复） |