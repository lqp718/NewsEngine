# Gemma 4 12B 本地模型替代 Gemini API 可行性研究报告

**日期**: 2026-08-26
**作者**: Architect（子代理调研）
**状态**: 调研完成，未改任何代码
**结论速览**: ✅ **可行，且改动量极小（1 个文件 ~20 行 + .env 3 行）**。推荐「方案 B 混合降级」：默认走本地 Gemma 4 12B，保留百炼 Qwen 作为一键切换的质量兜底。详见第 5 节。

---

## 1. 现有 Gemini API 使用概述

### 1.1 调用点（全项目仅 1 处）

| 文件 | 位置 | 作用 |
|------|------|------|
| `src/core/config.py` | `graphiti_llm_provider` / `gemini_api_key` / `gemini_model` | 配置项：provider 开关（`gemini` \| `openai`） |
| `src/core/graphiti_client.py` | `create_graphiti()` L41–L56 | **唯一实例化点**：`GRAPHITI_LLM_PROVIDER=gemini` 时使用 `graphiti_core.llm_client.gemini_client.GeminiClient` |
| `src/graphiti/episode_writer.py` | 模块级并发/退避/熔断 | 针对 Gemini 免费配额的 429 治理（非 Gemini 专属逻辑，本地化后可放宽） |

**关键事实：项目已有双 provider 架构。** `.env` 的 `GRAPHITI_LLM_PROVIDER` 切到 `openai` 就走百炼 `BailianOpenAIClient`（基于 `graphiti_core` 的 `OpenAIGenericClient`，OpenAI 兼容协议）。**这条通道天然支持任意 OpenAI 兼容端点，包括本地 llama.cpp。**

### 1.2 依赖 Gemini 的功能场景

Gemini 不是直接被业务代码调用，而是作为 **graphiti-core（v0.29.3）知识图谱构建的 LLM 后端**，每个 episode 写入（`add_episode()`）内部触发一组结构化抽取任务：

| 子任务 | 输入 → 输出 | 调用特征 |
|--------|------------|---------|
| 实体抽取（extract_nodes） | episode 正文 → `{"entities": [...]}` | 结构化 JSON，pydantic response_model |
| 关系/边抽取（extract_edges） | 正文 + 实体列表 → 边 + `fact` + `valid_at` | 同上，中文输出（本项目新闻以中文为主） |
| 边去重（dedupe_extracted_edges） | 候选边 + 已有边 → 去重决策 | 结构化 |
| 时间属性推断 | 边事实 → `invalid_at` 等 | 结构化，按批 |
| 摘要/属性 | 实体属性、episode 摘要 | 结构化 |

- **输入输出格式**：全部为 chat messages + pydantic 模型约束的 JSON；Gemini 分支显式 `max_tokens=32768`（注释：`gemini-3.5-flash-lite` 不在 `GEMINI_MODEL_MAX_TOKENS` 映射，默认 8192 会截断导致 JSON 解析失败）。
- **调用频率/批量**：`episode_writer.py` 全局信号量 `_LLM_SEMAPHORE = asyncio.Semaphore(3)`；ingest 批大小 20（`ingest_batch_size`）；日志显示日均写入相关活动约 130–230 条记录（8/21–8/25），属**中低频批处理**，非实时。
- **错误处理**：429 退避下限 37s + jitter（尊重 API `retryDelay`）；连续 3 次 429 熔断冷却 60s（`_CIRCUIT_*`）；graphiti-core 内部 tenacity 重试 4 次（含 JSONDecodeError 重试）。
- **Fallback**：无自动跨 provider fallback；仅有手动 `.env` 切换。注释明确记录："gemini 免费但不稳定，偶发 503/JSON 解析失败"。

### 1.3 周边依赖（不受影响）

- **Embedding**：百炼 `text-embedding-v4`（`BailianEmbedder`）——与 LLM provider 无关，不变。
- **Reranker**：本地 BGE（`BGERerankerClient`）——无 API 成本。
- **结论：Gemini 只承担 Graphiti 的结构化抽取/推理任务，替换面收敛且清晰。**

---

## 2. Gemma 4 12B 能力评估

### 2.1 本机部署现状（实测，2026-08-26）

**本地已有服务在跑**（无需新部署）：

```
/opt/homebrew/bin/llama-server
  -m ~/models/Gemma4/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf
  --mmproj ... -ngl 999 -fa on --cache-type-k/v q8_0
  -c 32768 --parallel 1 --jinja --reasoning off
  --host 127.0.0.1 --port 8080     （自 8 月 18 日运行中）
```

| 实测项 | 结果 |
|--------|------|
| `/v1/chat/completions` | ✅ OpenAI 兼容，可用 |
| `response_format: json_object` | ✅ 输出合法 JSON，中文金融实体抽取正确（宁德时代/匈牙利/73亿欧元/欧盟委员会均正确识别） |
| `response_format: json_schema`（约束解码） | ✅ **llama.cpp 原生支持**，输出完全符合 schema —— 比 json_object 更可靠 |
| 生成速度 | ⚠️ **~10 tok/s**（冷启动首请求 9.8，热状态 10.3）；prompt 处理 ~70 tok/s |
| 输出质量 | 可用；轻微瑕疵：把 "3.2%" 抽成实体（type 空缺），边界实体偏多 |

**速度瓶颈诊断**：实测 10 tok/s 远低于 M4 24GB 跑 12B Q4_K_M 的正常水平（预期 25–45 tok/s）。系统当时状态：`PhysMem 23G used / 94M free`，**压缩内存 6.8GB，swapout 250 万次**，load avg 4.5 —— 机器整体内存压力极大，模型权重被换页/压缩拖慢。**清理内存后预计可达 30–45 tok/s。**

### 2.2 模型规格（公开资料，2026-06-03 发布）

| 项目 | Gemma 4 12B | 现用 Gemini 3.5 Flash-Lite |
|------|-------------|---------------------------|
| 参数 | 11.95B（dense，48 层） | 未公开（远大于 12B） |
| 上下文 | **256K**（本机服务配置 32K，足够） | 1M |
| 模态 | 文本+图像+音频输入 | 文本+图像 |
| 许可 | Apache 2.0（可商用） | API 计费/免费配额 |
| 基准 | MMLU Pro ~77.2%，**超过 Gemma 3 27B**；RULER 32k 96.4% | flash-lite 定位轻任务 |
| 结构化输出 | 原生 JSON/function-calling 支持；llama.cpp 约束解码可用 | 原生 |
| 多语言 | 140+ 语言，中文良好（实测可用） | 优秀 |
| 量化 | 官方 QAT Q4 变体；Q4_K_M ≈ 6.6–8GB 内存 | — |
| 推荐采样 | temperature 1.0 / top_p 0.95 / top_k 64（抽取任务建议低温 0.1–0.3） | — |

### 2.3 ⚠️ 必须提示的两个本地风险

1. **本机模型是社区微调版**：`Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M` —— "Uncensored/Balanced" 社区变体，对齐行为与官方 `-it` 有差异。抽取任务影响通常有限，但建议上线前跑 A/B 评测（见 6.2）。
2. **推理模式已关**（`--reasoning off`）——对结构化抽取是正确选择（避免 thinking token 浪费）。

### 2.4 吞吐估算（关键可行性数据）

Graphiti `add_episode()` 单 episode 约 **5–10 次 LLM 调用、输出合计 ~1.5K–6K tokens**（本项目日志出现过 6534 字符的超长 `fact` 字段，属高输出场景）。

| 场景 | 生成速度 | 单 episode 耗时（按 3K 输出 tokens 中位数） | 日均 200 episodes |
|------|---------|--------------------------------------------|-------------------|
| 现状（内存挤压） | 10 tok/s | ~5 min | ~17 h ❌ 超标 |
| 清理内存后 | 35 tok/s | ~1.5 min | ~5 h（摊到全天多周期可行）✅ |
| MTP drafter 加速后 | ~70–100 tok/s* | ~40 s | ~2.2 h ✅✅ |
| Gemini API（对照） | — | ~5–30 s，但受 4M tok/min 配额 + 429 抖动 | 分钟级 |

*MTP（Multi-Token Prediction）投机解码：llama.cpp 自 2026-06-07 起支持，Gemma 4 官方提供 drafter，宣称最高 3× 提速。需额外 ~1–2GB 内存。

**结论：本地方案吞吐"够用但不富裕"**。当前瓶颈是整机内存压力而非模型本身；本项目是后台批处理（Tier 1 间隔 15 min、非实时），容忍度高。

---

## 3. 集成方案（代码改动方向与示例）

> 按任务要求，本节只给出方案与伪代码，**未修改任何代码**。

### 3.1 方案一（推荐）：新增 `local` provider 分支，~20 行改动

`src/core/graphiti_client.py` 的 `create_graphiti()` 增加分支（复用 `OpenAIGenericClient` 的 `json_schema` 约束解码，比百炼分支的 `json_object` 更稳）：

```python
# src/core/config.py 新增（示意）
local_llm_base_url: str = Field(
    "http://127.0.0.1:8080/v1",
    description="本地 llama.cpp OpenAI 兼容端点",
)

# src/core/graphiti_client.py create_graphiti() 新增分支（示意）
elif settings.graphiti_llm_provider == "local":
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    llm_client = OpenAIGenericClient(
        config=LLMConfig(
            api_key="***",                    # llama.cpp 不校验
            model=settings.llm_model,          # 任意字符串即可
            base_url=settings.local_llm_base_url,
        ),
        max_tokens=8192,                       # 本地 ctx 32K，留余量给 prompt
        structured_output_mode='json_schema',  # llama.cpp 支持约束解码 ✅
    )
```

`.env` 切换（3 行）：

```ini
GRAPHITI_LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1
LLM_MODEL=gemma4-12b        # llama.cpp 忽略模型名，任意值
```

**为什么不需要照搬 `BailianOpenAIClient` 的 schema-echo 修复**：那是针对百炼 qwen 返回 schema 定义/裸 list 的补丁；llama.cpp 的 `json_schema` 约束解码从词法层面保证结构，不产生这类问题。若保守起见也可直接继承复用，无副作用。

### 3.2 `episode_writer.py` 的配套调整（可选，二期）

429 治理逻辑是为 Gemini 免费配额设计的；本地无配额限制，但 llama-server 是 `--parallel 1` 单槽串行：

- `_LLM_SEMAPHORE` 3 → 1（避免无意义排队抖动；若想提速需重启服务加 `--parallel N`，代价是显存和单请求速度摊薄，24GB 下 N=2 可行）。
- 熔断器保留但语义改为"本地服务宕机保护"（连续失败 → 暂停写入 → 提示重启 llama-server）。
- **建议给 `create_graphiti` 调用方加健康检查**：启动时 `GET /health`，未就绪则拒绝启动写入循环（当前如果服务挂了会表现为连接错误重试）。

### 3.3 零改动应急方案（今天就能生效）

不改任何代码，仅改 `.env`：

```ini
GRAPHITI_LLM_PROVIDER=openai
OPENAI_BASE_URL=http://127.0.0.1:8080/v1
LLM_MODEL=gemma4-12b
```

`BailianOpenAIClient` 走 `json_object` 模式 + schema 注入 prompt，对 llama.cpp 同样有效（已实测 `json_object` 输出合法 JSON）。**适合作为切换前的冒烟测试通道**；长期还是建议 3.1 的 `json_schema` 分支。

### 3.4 性能影响与质量风险

| 维度 | 评估 |
|------|------|
| 延迟 | 单 episode 从秒级变分钟级；批处理可接受，`ingest_lease_sec=900` 已为最坏耗时留了余量 |
| 吞吐 | 受 `--parallel 1` 限制，纯串行；日均 200 episodes 在 35 tok/s 下约 5h，分布在全天周期内可行 |
| 质量 | 12B vs Gemini：简单抽取相当，复杂多跳边推理/去重偏弱；社区微调版需 A/B 验证；超长 `fact`（>6K 字符）场景建议先观察 |
| 运维 | 多一个常驻进程依赖（已在跑）；机器重启后需拉起（建议后续做 launchd 托管） |

---

## 4. 方案对比表

| 方案 | 成本 | 质量 | 速度 | 改动量 | 风险 |
|------|------|------|------|--------|------|
| **A. 完全用本地 Gemma 4 12B** | ¥0（电费） | 中等（12B，社区微调，抽取可用） | 慢（10–45 tok/s，串行） | **极小**（~20 行 + .env） | 内存挤压拖慢吞吐；质量未经 A/B 验证；进程运维 |
| **B. 混合：本地为主 + 百炼兜底（推荐）** | ≈¥0（偶发走百炼才计费） | 中高（可一键切回高质量） | 中 | 小（同 A + provider 切换纪律/脚本） | 同 A，但有逃生通道 |
| C1. 换低成本 API（OpenRouter/Groq/Together 托管 Gemma 4 或 Llama） | 低（$0.05–0.3/1M tok 级；部分有免费档） | 高（可用更大模型） | 快 | 小（同 3.3 零改动路径，换 base_url） | 新供应商依赖、免费档限流、数据出境 |
| C2. 继续用百炼 qwen3.7-plus | 低（按量计费） | 高 | 快 | **零**（现有通道） | 持续费用；之前因"收费"才有本调研 |
| D. 充值恢复 Gemini | 免费档 ¥0 / 付费档低 | 高 | 快 | 零 | 历史问题复现（429/503/JSON 解析失败已记录在案）；配额共享 |

---

## 5. 推荐方案及理由

**推荐方案 B：本地 Gemma 4 12B 为默认，百炼 Qwen 保留为一键兜底。**

理由：

1. **架构已就绪**：双 provider 开关 + OpenAI 兼容通道是现成的，本地接入是配置级改动，风险最低、可随时回退（`.env` 一行切回 `openai`/`gemini`）。
2. **成本归零**：Gemini 欠费的直接诉求是摆脱付费/配额依赖；本地方案边际成本为 0，且模型已在运行。
3. **能力已实测验证**：json_schema 约束解码 + 中文金融实体抽取均通过实测；Graphiti 的 prompt 都是短输入结构化输出，正是 12B 模型的胜任区（256K 上下文远超需求）。
4. **吞吐缺口可控**：本项目是分钟级后台批处理而非实时链路；前提是先解决内存压力（见 6.1），必要时上 MTP drafter。
5. **质量兜底存在**：若 A/B 评测发现抽取质量不达标（如边去重错误率超标），一行配置切百炼或低成本托管 API，无需回滚代码。

**不推荐方案 A（纯本地、无兜底）的原因**：社区微调版质量未经系统验证，去掉逃生通道过于激进。
**不推荐立刻换托管 API（C）的原因**：本地方案几乎零成本且已具备，托管 API 作为二期备选（若本地吞吐实测不达标再启用）。

---

## 6. 实施步骤（如采纳方案 B）

### 6.1 前置：释放内存（预计收益 3–4× 速度）

- 排查并关闭高内存进程（当前 23G/24G，压缩内存 6.8GB，大量 swap）；
- 重启 llama-server 让权重重新驻留；重启后复测生成速度应达 30+ tok/s；
- （可选）后续用 launchd 托管，避免手工拉起。

### 6.2 质量 A/B 验证（切换前必做，~1 天）

1. 从 `data/landing` 或 `output/dry_run_*.json` 抽 30–50 条代表性 episode（覆盖中/英文、宏观/个股）；
2. 分别用 Gemini（若仍可访问）或百炼、本地 Gemma 跑 `test_graphiti_episode.py` 式抽取；
3. 对比实体召回/边类型归一（对照 `CORE_EDGE_TYPES` 7 类）/JSON 解析失败率；
4. **通过线建议**：JSON 解析失败率 <2%（有 tenacity 4 次重试兜底）、核心实体召回 >90%、边类型合法率 >95%。

### 6.3 代码改动（半天）

1. `config.py`：新增 `local_llm_base_url` 字段；
2. `graphiti_client.py`：新增 `local` 分支（3.1 节示例，用 `json_schema` 模式，`max_tokens=8192`）；
3. 单测：mock `AsyncOpenAI` 验证分支选择；真实环境对本地端点跑 1 条 episode 冒烟。

### 6.4 灰度切换

1. `.env` 设 `GRAPHITI_LLM_PROVIDER=local`，先跑 1 个 Tier 2 低频周期观察；
2. 观察指标：单 episode 耗时、JSON 解析重试次数、`ingest` dead 行数、llama-server 内存；
3. 稳定 3 天后放开全 Tier；`episode_writer` 的 429 治理参数按 3.2 节二期调整。

### 6.5 回退预案

- `.env` 一行切回 `openai`（百炼）即可，无代码回滚；
- 若百炼也需省成本，二期评估 OpenRouter/Together 上托管的 `gemma-4-12b` / `gemma-4-26b-a4b`（同一套 `local` 分支代码，仅换 base_url/key）。

---

## 附录：本次调研的实测证据

- `GET :8080/health` → `{"status":"ok"}`；`/v1/models` 返回 Gemma4 GGUF；
- 中文抽取实测（宁德时代匈牙利建厂）：输出合法 JSON，实体/关系基本正确，406 tokens 生成耗时 41.3s（10 tok/s，内存挤压状态）；
- 英文 `json_schema` 约束解码实测：输出严格符合 schema（Apple/Organization、Vietnam/Location 等）；
- `graphiti-core==0.29.3`，`OpenAIGenericClient` 源码确认支持任意 OpenAI 兼容端点 + `json_schema`/`json_object` 双模式 + tenacity 重试；
- 硬件：Mac mini M4 / 24GB 统一内存（`sysctl`/`system_profiler` 实测）。

**外部资料**（供查证）：HuggingFace `google/gemma-4-12B-it` 模型卡、Gemma 4 Technical Report (arXiv:2607.02770)、ai.google.dev/gemma/docs/core、unsloth.ai/docs/models/gemma-4、ollama.com/library/gemma4。
