# ARCH Review v2：《数据质量与架构诊断报告 v2.0》复核结论

**Reviewer**: Architect
**日期**: 2026-09-05
**被审文档**: `docs/DATA_QUALITY_AND_ARCHITECTURE_REVIEW_20260905.md`（v2.0）
**上轮结论**: `docs/ARCH_REVIEW_DATA_QUALITY_20260905.md`
**验证方法**: 上轮 5 项修订要求逐条对照 + v2.0 新增引用重新核实源码（episode_writer.py / relation_types.py / entity_canonical.py / api/models.py / api/routers/events.py / SynapseEngine main_dispatcher.py / ticker_whitelist.json / cls_adapter.py）

---

## 〇、总体结论

**未完全闭环。上轮两处关键错误（§1.4 自相矛盾、§2 删除 normalize_edge_type）已正确修正，§4 跨仓库契约完整，优先级排序合理；但上轮修订要求第 3、5 条各有遗漏，其中 1 处严重级（L-6 个股管线 sector 词汇自身分裂）会导致 P1-2 按文档执行时修不干净。需第三轮小幅修订后即可 APPROVE。**

---

## 一、重点核验结果（逐项）

### 1. §1.4 澄清 — ✅ 已修正

- 表格与结论一致：**保留 8 种（RELATES_TO/INVOLVES/HAPPENED_IN/AFFECTS/PART_OF/BELONGS_TO/TRADED_ON/TRIGGERS），删除 2 种（INVESTS_IN/EXPOSED_TO）**，与上轮 Architect 意见（保 7 + RELATES_TO 兜底 = 8）吻合。
- TRIGGERS 改判保留，理由（因果脉络业务目标需要）成立。
- §1.5 删除成本正确：存量 **177 条**（103+74，TRIGGERS 保留后不再计入，数字自洽）、44 个单测联动、renormalize 脚本联动，三项齐全。

### 2. §2 normalize_edge_type — ✅ 已正确撤回并重构

- "删除"结论已撤回，改为"保留，但需修正"。
- 3 个调用点描述经源码复核**全部准确**：`_normalized_edge_types()`（episode_writer.py L256，L440 调用）、`_normalized_edge_type_map()`（L278，L441 调用）、`_normalize_written_edges()`（L756，L502 调用，9 天日志 0 触发）。
- graphiti-core 0.29.3 prompt 允许 LLM 发明 SCREAMING_SNAKE_CASE 类型、schema 只约束属性不约束类型名 — 与上轮验证一致。
- 代码片段引用错误已承认并修正（前缀匹配 + 默认归 RELATES_TO）。
- ⚠️ **两处小遗漏**（见二、M-1/M-2）：schema 错配修复与存量修正配套未写入。

### 3. §3 供给量/ticker 覆盖率前置阻塞 — ✅ 主体已补，⚠️ 方案要素有遗漏

- §3.4 根因表已加入**供给量（SYMBOL 22 vs MACRO 573）**与 **ticker 覆盖（4/88，白名单 10 只）**两行；§六 P0-1/P0-2 对应列项；§七总结"最紧急的是 P0"。前置阻塞定位正确。
- §3.6 Sector 类型污染已补，"先语义准入 → 再语言归一 → 最后合并存量"顺序正确。
- ❌ **遗漏 1（严重级，上轮 L-6）**：§3.4 根因表仍是"宏观英文 vs 个股中文"简单二分。**个股管线自身也产英文 sector**，实测证据：
  - `data/ticker_whitelist.json`：10 只标的的 sector 字段全部是英文粗类 **'Tech' / 'Consumer' / 'Finance'**（仅 3 类），`eastmoney_adapter.py` 把它原样塞进 pre-extracted entities；
  - `cls_adapter.py` L266：硬编码 `kwargs["sector"] = "STAR Market"`。
  按 v2.0 执行 P1-2（只改宏观管线语言规则 + Sector 准入），白名单英文粗类和 CLS 硬编码仍会持续污染 Sector 词汇，统一修不干净。
- ❌ **遗漏 2（上轮修订要求第 3 条明列）**：Sector 方案未提**复用现成基础设施**——`src/utils/entity_canonical.py`（ALIAS_MAP + `canonical_name(name, entity_type)`，已支持按类型扩展）与 `data/canonical_entities.yaml`（实测无行业别名条目，需新增 sector 类别），以及 **CANONICAL ENTITY NAMES 注入通道改造**（episode_writer.py L891 该区块当前只注入 episode.entities 的 name+ticker，Sector 规范名无注入通道）。不复用而另起炉灶，P1-2 的 2-3 天估算不可信。

### 4. §4 跨仓库契约 — ✅ 完整

四项全部收录且经源码复核属实：
- 4.1 ticker 转换 bug：`main_dispatcher.py` L113-117 引用逐字符核对一致（无条件 `.HK` 后缀、`lstrip("HK")` 字符集剥除问题）；
- 4.2 sector 契约中英颠倒：两端文档字符串引用一致；
- 4.3 limit/min_severity 被忽略：`get_entity_events` 签名复核确认只有 `ticker`（events.py L275-277）；
- 4.4 3 天硬编码窗口：events.py L265 `duration({days: 3})` 确认。
- P0-3 明确"服务端归一化 + 客户端重写"双侧修，定位正确。

### 5. 优先级排序 — ✅ 合理

P0-1 供给量 → P0-2 ticker 覆盖 → P0-3 跨仓库契约 → P1 Sector（准入先于语言统一，顺序正确）→ P1-3 删类型 → P2 API 图结构 → P3 清洗项。与上轮 L-14 建议顺序一致。P1-1（语义准入+存量清理）排在 P1-2（语言统一）之前是对的——先清掉族群/哲学/期货节点再做归一，避免给垃圾节点做翻译。

### 6. §5 其他问题 — ✅ 大部分收录，⚠️ 缺 2 项

已收录：双向边数字修正为 ~1.5%/~3%（L-12 ✅）、42 空实体 episode、实体碎片化、stock.sector='Unknown'（L-9 ✅）、指数归类 Stock（L-10 ✅）、HTML 实体泄漏（L-11 ✅）。
缺失：**L-7（EventItem.relations 死字段）**、**L-13（sector 查询无标签过滤）**，见下。

---

## 二、剩余问题清单（按严重度）

### 🟠 严重级

**M-1（=上轮 L-6）：个股管线 sector 词汇自身分裂未写入 §3.4/§3.6。**
证据见一.3 遗漏 1。修订要求：§3.4 根因表加一行"个股管线内部英文 sector 注入（白名单 Tech/Consumer/Finance + CLS 硬编码 STAR Market）"；P1-2 工作项明确覆盖 `ticker_whitelist.json` sector 字段中文化（或映射）与 `cls_adapter.py` L266 硬编码替换。

### 🟡 一般级

**M-2（=上轮 L-7）：`EventItem.relations` 死字段未提。**
复核确认：`api/models.py` L102 定义存在、L46 类型描述仍引用已废弃的 "CAUSED_BY / MITIGATES / RELATED_TO"、翻译层恒置 None。§3.7 方案 A 落地前必须决定该字段去留（删除或改名为 event 级关系专用），否则新增 `graph` 字段后会有两个语义混淆的"关系"字段。

**M-3（=上轮 L-13）：sector 查询 Cypher 无标签过滤未提。**
复核确认：events.py L360-361 `MATCH (sector_ent:Entity) WHERE sector_ent.name = $sector_name`，无 `'Sector' IN labels(sector_ent)`。库内存在同名跨类型实体（如"恒生指数"Stock/Sector 两侧都有，§5.6 自己也写了），会误命中。一行修复，建议并入 P2-3。

**M-4：§2.2 缺 schema 错配修复项。**
上轮 §二.2 指出：收敛后 PART_OF 复用的 schema 是 `LocatedInEdge`（复核确认 relation_types.py L100-104，docstring 写"股票上市地/注册地所在国家"），BELONGS_TO 复用 `relation_types.BelongsToEdge`（无 valid_at 字段）。"重写 EDGE_TYPES 注册表"时应一并修复模型与语义错配，v2.0 §2.2 第 1 条未提。

**M-5：§2.2 LOCATED_IN→HAPPENED_IN 改映射缺存量修正配套。**
该映射当前实际生效于 `(Stock, Country)` 实体对（relation_types.py L156 复核确认），库内 PART_OF 149 条中混有地点语义边；只改映射不修存量，历史 PART_OF 边语义仍然错。§1.5 的迁移表应加这一项。

---

## 三、数值主张核对表（v2.0，全部对照上轮实库复核结果 + 本轮源码抽验）

| v2.0 主张 | 核验 | 判定 |
|-----------|------|------|
| 关系分布 840/700/623/319/149/112/103/88/74/21 | 与上轮实库复核逐项一致 | ✅ |
| 保留 8 删 2、存量迁移 177 条（103+74） | 算术自洽，与保留 TRIGGERS 决策一致 | ✅ |
| 3 个 normalize 调用点及触发情况 | 源码 L256/L278/L440-441/L502/L756 复核一致 | ✅ |
| 9 天日志 0 次 "edge type normalized" | 与上轮日志检索一致 | ✅ |
| SYMBOL 22 vs MACRO 573、桥接=0 | 与上轮实库复核一致 | ✅ |
| ticker 覆盖 4/88、白名单 10 只 | 白名单本轮实测 10 只，sector=Tech/Consumer/Finance | ✅ |
| 145 Sector 节点、真行业 <50% | 与上轮实库取证一致 | ✅ |
| 46 对双向边 ~1.5%（按边 ~3%） | 已按上轮 L-12 修正 | ✅ |
| 42 个空实体 episode = 7% | 42/595=7.06% | ✅ |
| `_build_extraction_instructions` 第 849-850 行强制英文 | 实测 else 分支 language_rule 位于 ~L847-851，引用近似准确 | ✅ |
| main_dispatcher.py L113-117 ticker 转换代码 | 逐字符核对一致 | ✅ |
| entity 端点签名只有 ticker、3 天窗口 | events.py L275-277 / L265 复核一致 | ✅ |

**数值与代码引用层面：v2.0 无错误。** 上轮"文档引用代码片段错误"的问题已修正，本轮全部引用经源码核对属实。

---

## 四、修订要求（发回灵汐，第三轮）

1. **§3.4/§3.6 + P1-2**：补个股管线内部英文 sector 注入（白名单 3 类英文粗分类 + CLS "STAR Market" 硬编码），P1-2 工作项覆盖这两个数据源。（M-1，必须）
2. **§3.6/P1-2**：补"复用 `entity_canonical.py` ALIAS_MAP + `canonical_entities.yaml` 新增 sector 类别 + CANONICAL ENTITY NAMES 注入通道改造（episode_writer.py L891）"作为实施路径，并据此复核 2-3 天估算。（必须）
3. **§3.7**：补 `EventItem.relations` 死字段处置决定（删或明确区别于新增 graph 字段）。（M-2）
4. **P2-3**：并入 sector 查询标签过滤修复（events.py L360）。（M-3）
5. **§2.2/§1.5**：补 schema 错配修复（LocatedInEdge→PART_OF、BelongsToEdge 无 valid_at）与 LOCATED_IN 映射变更的存量 PART_OF 修正配套。（M-4/M-5）

以上 5 项完成后无需再送审全文，修订处抽验即可 APPROVE。

---

## 附：本轮验证证据来源

- NewsEngine 源码：`src/graphiti/episode_writer.py`（L256/L278/L440-441/L502/L756/L847-851/L891）、`src/graphiti/relation_types.py`（L100-104/L137/L156）、`src/api/models.py`（L46/L102）、`src/api/routers/events.py`（L265/L275-277/L360-361）、`src/utils/entity_canonical.py`（L94-129）、`src/adapters/cls_adapter.py`（L266）
- SynapseEngine：`src/dispatcher/main_dispatcher.py`（L113-117）
- 数据：`data/ticker_whitelist.json`（10 只，sector ∈ {Tech, Consumer, Finance}）
- 上轮实库复核结果（Neo4j newsengine-neo4j，2026-09-05）沿用，本轮未重复查库
