# ARCH Review v3：《数据质量与架构诊断报告 v3.0》复核结论

**Reviewer**: Architect
**日期**: 2026-09-05
**被审文档**: `docs/DATA_QUALITY_AND_ARCHITECTURE_REVIEW_20260905.md`（v3.0）
**上轮结论**: `docs/ARCH_REVIEW_DATA_QUALITY_20260905_v2.md`
**验证方法**: 上轮 5 项修订要求逐条对照 + v3.0 新增章节（§2.2/§3.6.1/§3.7/§3.8/§5.8/§5.9/P2-3）全部引用点源码抽验（relation_types.py / cls_adapter.py / eastmoney_adapter.py / ticker_whitelist.json / canonical_entities.yaml / entity_canonical.py / episode_writer.py / events.py / models.py + git 历史）

---

## 〇、总体结论

**接近闭环，但 §3.7 有两处与代码库现状不符的事实错误，需第四轮微修（两行改动）后即可 APPROVE。**

上轮 5 项修订要求（M-1 ~ M-5）**全部落实**，位置与内容均正确。但本轮对 §3.7"实施路径"的基础设施描述做实证核查时，发现两处事实错误——其中第一处源自 v2 review 自身的核查疏漏（被文档照抄），在此一并更正并向文档维护者致歉。

---

## 一、重点核验结果（逐项）

### 1. §3.4 个股管线内部英文 sector — ✅ 已补充

根因表新增"个股管线内部英文"一行（白名单 Tech/Consumer/Finance + CLS 硬编码 "STAR Market"），与源码证据一致：
- `data/ticker_whitelist.json`：本轮实测 sector 值集合 = `['Consumer', 'Finance', 'Tech']`，✅
- `cls_adapter.py` L266：`kwargs["sector"] = "STAR Market"`，行号逐字核对 ✅
- `eastmoney_adapter.py` L284/L402-403：白名单 sector 经 `_ticker_sector` 原样进入 pre-extracted `EntityItem` ✅

### 2. §3.6.1 个股管线 sector 词汇自身分裂 — ✅ 已新增

三行表格（白名单英文粗类 / CLS 硬编码 / LLM 中文细分类）与证据一致；"P1-2 必须覆盖这两个数据源"结论正确，§六 P1-2 行也已同步注明"白名单/CLS 硬编码"。

### 3. §3.7 实施路径 — ⚠️ 章节已补，但含两处事实错误（见二、N-1/N-2）

复用基础设施的方向正确（entity_canonical.py + canonical_entities.yaml + CANONICAL ENTITY NAMES 注入通道三件套齐全，episode_writer.py L891 行号核对无误），但对两件基础设施的现状描述不准。

### 4. §3.8 EventItem.relations 死字段处置 — ✅ 已补充

"前置清理"段落明确"方案 A 落地前必须决定去留"，§5.8 同步收录。源码复核：`models.py` L46 类型描述确为 `"Relation type: CAUSED_BY / MITIGATES / RELATED_TO"`（已废弃类型），`relations` 字段定义在 ~L102，引用属实。

### 5. §2.2 schema 错配 + 存量修正配套 — ✅ 已补充

- 第 2 条 schema 错配：`LocatedInEdge` docstring "股票上市地/注册地所在国家"（L101 起，核对一致）；`BelongsToEdge` 仅有 `fact` 字段、无 `valid_at`（L87-97 全类核对确认）✅
- 第 4 条存量修正配套：LOCATED_IN 映射变更影响面 + PART_OF 149 条混有地点语义边 + FEILI CO LIMITED 实例 ✅
- 精度脚注（不要求修订）：`DEFAULT_EDGE_TYPE_MAP` 无显式 `(Organization, Country)` 条目，该实体对的 LOCATED_IN 来自 `("Entity", "Entity")` 兜底项（其中含 LOCATED_IN）。文档表述"影响 (Stock, Country) 和 (Organization, Country)"实质正确，仅机制上是兜底命中而非专设映射。

### 6. P2-3 sector 查询标签过滤 — ✅ 已并入

§六 P2-3 = "entity 端点接收 limit/min_severity + 窗口调整 + sector 查询加标签过滤"，§5.9 同步收录。源码复核 `events.py` L360-361：`MATCH (sector_ent:Entity) WHERE sector_ent.name = $sector_name` 确无 `'Sector' IN labels(...)` 过滤，引用属实。

### 7. 遗漏检查 — ✅ 无新增遗漏

上轮 5 项修订要求全部落位；§1.5 迁移表虽未加 LOCATED_IN 存量项，但上轮要求为"§2.2/§1.5"二处任一，§2.2 第 4 条已覆盖，可接受。P1-2 维持 2-3 天估算，在复用路径明确（且实际比文档描述的更省事，见 N-1）的前提下可信。

---

## 二、本轮新发现问题（按严重度）

### 🟡 一般级

**N-1：§3.7 称 `canonical_entities.yaml`"当前无行业别名条目，需新增 sector 类别" — 事实错误。**

实测该文件自 **2026-08-23 commit `e0199d8`** 起就有 `# ── Sectors / Themes ──` 区块（L312 起），含 **6 个行业规范名条目**：半导体、人工智能、电动汽车、可再生能源、太阳能、风电，映射方向正是**英文别名 → 中文规范名**（如 `半导体: [Semiconductor, Semiconductors, Chip, Chips]`）——与 P1-2 语言统一目标方向完全一致。

> 更正说明：此错误论断源自 v2 review（"实测无行业别名条目"），系上轮核查疏漏，文档系照抄。责任在 Architect 侧。

**影响**：实施步骤 1 按字面执行会"新增"一个已存在的类别；且既有区块已确立"中文规范名 + 英文别名列表"的格式约定，绕过它另建会引入两套格式。
**修订要求**：§3.7 表格该行改为"已有 Sectors/Themes 区块（6 个行业条目，英文→中文方向），需**扩充**至覆盖白名单粗类（Tech/Consumer/Finance）、STAR Market 及高频宏观行业词"；步骤 1 同步改为"扩充现有区块"。

**N-2：§3.7 称 `entity_canonical.py`"支持按类型扩展"、步骤 2"扩展 ALIAS_MAP 支持 Sector 类型" — 描述失实，方向误导。**

实测（entity_canonical.py L97-129）：
- `canonical_name(name, entity_type)` 的 `entity_type` 参数 docstring 明文写 **"Currently unused but reserved for future type-specific rules"**；
- `ALIAS_MAP` 是**平面小写 alias→canonical 字典**（`_load_alias_map` 从 YAML 反转生成），**无类型维度**。既有 6 个 sector 条目已在该平面字典中对所有调用生效，**不需要任何"类型扩展"**。

**影响**：步骤 2 按字面执行会给 ALIAS_MAP 加类型维度——这正是文档自己反对的"另起炉灶"，且会破坏现有 9 处调用点（scheduler.py L1364、episode_writer.py L712/L730、fred_adapter.py L448-449、models.py L74、eastmoney_adapter.py L399、cls_adapter.py L260 等）。
**真正的缺口**在别处：`adapters/models.py` L74 的 EntityItem 归一只作用于 `name` 字段，**`sector` 字段不经过 `canonical_name`**——这才是白名单 "Tech"/"STAR Market" 能原样入库的原因。
**修订要求**：步骤 2 改为"让 sector 字段也走 `canonical_name` 归一（models.py EntityItem 归一当前只覆盖 name），或在注入/写图侧对 sector 值调用归一"；§3.7 表格"支持按类型扩展"改为"entity_type 参数已预留但未启用；ALIAS_MAP 为平面映射，sector 别名天然生效"。

### 🟢 轻微级（不要求修订）

- §2.2 第 4 条 (Organization, Country) 的 LOCATED_IN 来自 `("Entity","Entity")` 兜底而非专设映射（实质结论不变，见一.5 脚注）。

---

## 三、数值与代码引用核对表（v3.0 新增/修订部分）

| v3.0 主张 | 核验 | 判定 |
|-----------|------|------|
| 白名单 sector = Tech/Consumer/Finance（仅 3 类） | 本轮实测集合一致 | ✅ |
| cls_adapter.py L266 硬编码 "STAR Market" | 行号+内容逐字核对 | ✅ |
| eastmoney_adapter 把白名单 sector 塞进 pre-extracted entities | L284/L402-403 核对 | ✅ |
| episode_writer.py L891 CANONICAL ENTITY NAMES 只注入 name+ticker | L880-899 核对，仅 name/ticker 两分支 | ✅ |
| canonical_entities.yaml "当前无行业别名条目" | L312 起有 Sectors/Themes 区块 6 条目，git 追溯至 2026-08-23 | ❌（N-1） |
| entity_canonical.py "支持按类型扩展" | entity_type 参数 unused-but-reserved；ALIAS_MAP 平面无类型维度 | ❌（N-2） |
| LocatedInEdge docstring "股票上市地/注册地所在国家" | relation_types.py L100-104 核对 | ✅ |
| BelongsToEdge 无 valid_at | L87-97 全类核对，仅 fact 字段 | ✅ |
| LOCATED_IN 生效于 (Stock, Country) | DEFAULT_EDGE_TYPE_MAP L156 核对 | ✅ |
| (Organization, Country) 也受影响 | 经 ("Entity","Entity") 兜底含 LOCATED_IN，实质成立 | ⚠️ 机制表述不精确 |
| events.py L360-361 sector 查询无标签过滤 | 逐字核对 | ✅ |
| models.py L46 引用废弃类型 / relations 恒 None | L46 核对属实 | ✅ |
| 沿用上轮的库内数值（关系分布/573 vs 22/4/88/145/46 对/42 个/177 条） | v2 已实库复核，本轮未重复查库 | ✅（沿用） |

---

## 四、修订要求（发回灵汐，第四轮，微修）

1. **§3.7 表格 + 步骤 1**：更正 canonical_entities.yaml 现状描述——已有 Sectors/Themes 区块（6 条目，英文→中文），动作由"新增 sector 类别"改为"扩充现有区块"。（N-1，必须）
2. **§3.7 表格 + 步骤 2**：更正 entity_canonical.py 能力描述（entity_type 未启用、ALIAS_MAP 平面映射），步骤 2 改为"让 sector 字段走 canonical_name 归一（models.py L74 现只归一 name）"，不得给 ALIAS_MAP 加类型维度。（N-2，必须）

以上 2 项均为 §3.7 局部文字修订，不涉及其他章节。**修订后抽验 §3.7 即可 APPROVE，无需再送审全文。**

---

## 附：本轮验证证据来源

- NewsEngine 源码：`src/graphiti/relation_types.py`（L87-97/L100-104/L131-165）、`src/adapters/cls_adapter.py`（L255-268）、`src/adapters/eastmoney_adapter.py`（L284/L359/L396-406）、`src/adapters/models.py`（L74）、`src/utils/entity_canonical.py`（L40-95 `_load_alias_map`、L97-155 `canonical_name`）、`src/graphiti/episode_writer.py`（L704-730/L880-899）、`src/api/routers/events.py`（L355-365）、`src/api/models.py`（L40-50/L95-110）
- 数据：`data/ticker_whitelist.json`（sector ∈ {Consumer, Finance, Tech}）、`data/canonical_entities.yaml`（340 行，L312 起 Sectors/Themes 区块）
- git 历史：`e0199d8`（2026-08-23）引入 canonical_entities.yaml sector 条目
- 上轮（v2）实库复核结果沿用，本轮未重复查 Neo4j
