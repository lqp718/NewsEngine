# NewsEngine Graphiti 实体/关系定义修复方案

- **作者**: Architect Agent
- **日期**: 2026-08-21
- **状态**: Proposed
- **关联问题**: P0-1 edge_type_map 软约束失效 / P0-2 Stock ticker 幻觉 / P1-1 缺 Person 类型 / P1-2 sector 中英混合 / P2 指数被当作 Stock

---

## 0. 数据验证结果（2026-08-21 实测）

以下数字来自对本地 Neo4j 生产图的直接查询，作为本方案的决策依据：

| 指标 | 实测值 |
|------|--------|
| 总边数 (`RELATES_TO`) | 589 |
| 未定义关系类型边数 | **192 (32.6%)**，分布在 **100 种**自造类型（WORKS_AT×15, OPERATES_IN×15, OWNS×10 …） |
| `CAUSED_BY` / `MITIGATES` 使用量 | **0 / 0**（定义了但 LLM 从未使用） |
| `AFFECTS` 使用量 | 仅 13（占比 2.2%） |
| `RELATED_TO` / `LOCATED_IN` / `BELONGS_TO` | 243 / 111 / 30 |
| 实体标签分布 | Organization 326, **仅 `['Entity']` 无类型 219**, Country 94, Stock 21, Topic 19, Policy 9, Sector 9, Event 1 |
| ticker 幻觉实证 | 沪深300指数→`300750.SZ`（宁德时代）、标普500指数→`300750.SZ`、恒生指数→`9618.HK`（京东）、纳斯达克指数→`IXIC.GI` |
| sector 实测值 | `互联网平台`×6、`Unknown`×5、**`Tech`×2、`Consumer`×2、`Finance`×1、`Power Equipment`×1**、`锂电池`、`电力设备`、`信息技术` |
| `data/ticker_whitelist.json` 的 sector 字段 | **全部英文**：`Tech`×6、`Finance`×2、`Consumer`×2（共 10 只） |

**任务描述之外新发现的两个根因：**

1. **`CAUSED_BY`/`MITIGATES` 零使用**说明当前 6 种关系词表与 LLM 的自然表达严重错位——LLM 天然倾向输出 `WORKS_AT`/`OWNS`/`OPERATES_IN` 这类常识关系词。只做 prompt 改进（P0-1 方案 B）无法根治，因为词表本身不覆盖 LLM 的自然输出空间。
2. **sector 中英混合的上游污染源是 `data/ticker_whitelist.json` 本身**——akshare_adapter 从白名单注入 `sector: "Tech"`，最终写入 Entity 属性。只改 field description 治不了已注入的英文 sector。

---

## 1. 总体架构决策

**核心原则：不再信任 LLM 的 schema 遵从性，把"校验与规范化"从 prompt 层下沉到写入后处理层。**

Graphiti v0.29.2 的 `edge_type_map` 和 field description 都是 prompt 级软约束，本次数据证明其遵从率不足 70%（边）且属性会产生幻觉。因此本方案的主线是：

```
LLM 提取（软约束，照旧）
   ↓
写入后处理 Normalizer（新增，硬约束）   ← 本方案核心
   ↓
Neo4j（保证数据 100% 合规）
   ↓
translation.py（读侧兜底，防御纵深）
```

新增一个模块 `src/graphiti/post_normalizer.py`，在 `episode_writer.write_one()` 中 `add_episode()` 成功后同步调用，承担：
1. 边类型规范化（P0-1）
2. ticker 接地（grounding）与幻觉清除（P0-2）
3. sector 中文归一（P1-2）

另配一次性迁移脚本 `scripts/migrate_graph_normalization.py` 清理存量数据。

---

## 2. 逐问题方案

### P0-1: edge_type_map 软约束失效

**方案评估：**

| 方案 | 评估 |
|------|------|
| A. episode_writer 后处理强制映射 | ✅ **推荐为主方案**。在数据落库后立即规范化，保证 Neo4j 中 100% 合规；对所有现有查询透明；成本仅为每 episode 一次轻量 Cypher。`add_episode()` 返回值 `result.edges` 带边 UUID，可精确定位本批新边 |
| B. 改 prompt + 扩展 edge_type_map | ⚠️ 辅助。数据已证明软约束遵从率 <70%，单独使用不可靠。但可**适度**收敛：在 `edge_type_map` 的 `("Entity","Entity")` docstring 中补 2-3 个 few-shot 示例。不建议扩展词表去容纳 100 种自造类型（API 语义会爆炸） |
| C. translation.py 读侧兼容 | ✅ **推荐作为防御纵深**（非主方案）。只改读不改写，存量脏数据仍然存在；且每个查询点都要改，遗漏风险高。作为 Normalizer 上线前的快速止血手段，以及防止未来 Graphiti 升级行为变化 |

**推荐：A 为主 + C 兜底 + B 轻量补充。**

**实现（新增 `src/graphiti/post_normalizer.py`）：**

```python
"""写入后规范化 — 将 LLM 软约束产物强制收敛到定义的类型词表。"""

DEFINED_EDGE_TYPES: set[str] = set(EDGE_TYPES.keys())

# 同义映射：LLM 高频自造类型 → 语义最近的定义类型
EDGE_SYNONYM_MAP: dict[str, str] = {
    "OPERATES_IN":      "LOCATED_IN",
    "LOCATED_NEAR":     "LOCATED_IN",
    "HEADQUARTERED_IN": "LOCATED_IN",
    "BELONGS_TO_SECTOR":"BELONGS_TO",
    "PART_OF":          "BELONGS_TO",
    "IMPACTS":          "AFFECTS",
    "INFLUENCES":       "AFFECTS",
    "CAUSES":           "CAUSED_BY",
    "RESULTED_FROM":    "CAUSED_BY",
    # 其余所有未定义类型一律 → RELATED_TO（下方 fallback）
}
FALLBACK_EDGE_TYPE = "RELATED_TO"


def normalize_edge_type(name: str) -> str:
    if name in DEFINED_EDGE_TYPES:
        return name
    return EDGE_SYNONYM_MAP.get(name.upper(), FALLBACK_EDGE_TYPE)


async def normalize_episode_edges(driver, edge_uuids: list[str]) -> dict[str, int]:
    """将本批新边中不合规的 name 改写为定义类型。返回 {改写数, 保留数}。"""
    # 1) 拉取本批边的 (uuid, name)
    # 2) Python 侧计算 uuid -> 目标name（确定性、可单测）
    # 3) 按目标 name 分组，逐组执行：
    #    MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids SET r.name = $name
    ...
```

**episode_writer.py 接线（diff 伪代码）：**

```python
# write_one() 中 add_episode() 成功后、content_scope 透传之前：
  result = await self._graphiti.add_episode(...)
+ # 硬约束：规范化本批所有新边的关系类型
+ if self._neo4j_driver and getattr(result, "edges", None):
+     stats = await normalize_episode_edges(
+         self._neo4j_driver, [e.uuid for e in result.edges]
+     )
+     if stats["rewritten"]:
+         logger.info("edge normalization: %d/%d rewritten",
+                     stats["rewritten"], len(result.edges))
```

**translation.py 读侧兜底（方案 C，防御纵深）：**

API 查询（`src/api/routers/events.py` 中的 Cypher）目前不按 `rel.name` 过滤，因此读侧主要影响未来按类型过滤的场景。在 translation.py 增加：

```python
def normalize_relation_name(name: str | None) -> str:
    """读侧兜底：未知关系类型统一按 RELATED_TO 处理。"""
    if not name:
        return "RELATED_TO"
    return EDGE_SYNONYM_MAP.get(name.upper(), name if name in DEFINED_EDGE_TYPES else "RELATED_TO")
```

任何后续按关系类型过滤/展示的 API 一律先过此函数，保证即使绕过 Normalizer 也不会出现未知类型泄漏到 API 响应。

**存量数据迁移（一次性脚本）：**

```cypher
// scripts/migrate_graph_normalization.py 核心语句
MATCH ()-[r:RELATES_TO]->()
WHERE NOT r.name IN ['AFFECTS','CAUSED_BY','MITIGATES','BELONGS_TO','LOCATED_IN','RELATED_TO']
WITH r LIMIT 5000
SET r.original_name = r.name,          // 保留原值便于审计/回滚
    r.name = 'RELATED_TO'              // Python 侧按 EDGE_SYNONYM_MAP 分组执行
```

> 注意：Python 侧按同义映射分组批量 SET，而不是全部无脑写 RELATED_TO（`OPERATES_IN` 应归 `LOCATED_IN`）。原类型存入 `original_name` 属性，可逆。

**风险：**
- `result.edges` 属性名依赖 graphiti-core 0.29.2 的 `AddEpisodeReturn` 结构，升级时需回归（当前代码已用 `hasattr` 防御，保持该风格）。
- 同义映射是启发式的（如 `OWNS`→`RELATED_TO` 丢失了所有权语义）。这是"词表收敛"的必然代价；`original_name` 保留了原始语义，未来若需要可恢复。
- 迁移脚本对 589 条边量级无性能风险，但建议先 `DRY RUN`（只统计不写）再执行。

---

### P0-2: Stock ticker 幻觉

**方案评估：**

| 方案 | 评估 |
|------|------|
| A. 指数/ETF 单拆 IndexEntity（无 ticker） | ✅ **推荐**。直击 P2 根因：指数无 ticker，强塞进 StockEntity 的必填 `ticker` 字段，LLM 只能幻觉。拆出后 ticker 字段从 schema 中消失，幻觉源头消除 |
| B. StockEntity field description 排除指数 | ⚠️ 辅助。仍是软约束，且指数被 LLM 分到 Stock 后依然面对必填 ticker。单独使用无效 |
| C. adapter 注入 ticker→name 映射 | ✅ **升级为主方案的关键部分**。现有 `_build_extended_body()` 已在 symbol 管线注入 ticker，但**宏观管线（RSS/GDELT）episode 无 entities，注入为空**——实测幻觉 ticker 全部来自宏观管线创建的实体（沪深300、恒生指数）。需要更可靠的机制：见下方方案 D |
| **D. 写入后 ticker 接地（新增，推荐为主方案）** | ✅ **最可靠**。"LLM 报名字，机器赋代码"：LLM 不再负责填 ticker（字段改 optional），写入后 Normalizer 用 `data/ticker_whitelist.json` 的 name→ticker 权威映射做精确/别名匹配，命中才赋值，未命中一律置 None |

**推荐：A + D 组合，B 作为 schema 内的文字保险。**

理由：
1. ticker 是**结构化代码**，本质上是可查表的数据，不该由生成式模型产生。只要 ticker 仍由 LLM 填充（哪怕给了 prompt 提示），幻觉概率就非零。
2. 白名单是权威数据源（当前 10 只，随 `ticker_sync.py` 增长），name→ticker 查表是确定性操作，准确率 100%。
3. 指数拆到 IndexEntity 后，"指数被迫填 ticker"这条幻觉路径被 schema 层面物理切断。

**实现：**

**① `entity_types.py` — StockEntity.ticker 改 optional + 新增 IndexEntity：**

```python
  class StockEntity(BaseModel):
      """股票实体 — 在新闻中出现的可交易标的（个股/ETF）。
+
+     注意：指数（沪深300、标普500、恒生指数）不是股票，请提取为 Index 类型。
      """
-     ticker: str = Field(
-         ...,
-         description="股票代码，格式: {biz_code}.{exchange}...",
-     )
+     ticker: str | None = Field(
+         default=None,
+         description=(
+             "股票代码，格式: {biz_code}.{exchange}。"
+             "⚠️ 仅当新闻原文中明确出现代码时才填写；"
+             "禁止根据公司名猜测或记忆生成代码。不确定时留空（系统会自动补全）。"
+             "指数没有股票代码，请勿为指数填写此字段。"
+         ),
+     )
      ...

+ class IndexEntity(BaseModel):
+     """市场指数实体 — 股票/行业指数，不可单独交易，无股票代码。
+
+     示例: "沪深300指数", "标普500", "恒生指数", "纳斯达克综合指数"
+     注意: ETF（如"恒生科技ETF天弘"）是可交易基金，请提取为 Stock，不要放这里。
+     Neo4j 节点标签: Entity:Index
+     """
+     entity_name: str = Field(
+         ...,
+         description="指数名称，使用中文，例如 '沪深300指数', '恒生指数'",
+     )
+     region: str | None = Field(
+         default=None,
+         description="指数所属市场: 中国A股/港股/美股/全球。不确定留空",
+     )
```

**② 两套 entity_types 注册表均加入 Index：**

```python
  MACRO_ENTITY_TYPES = { ..., "Index": IndexEntity }   # 指数主要出现在宏观管线
  SYMBOL_ENTITY_TYPES = { ..., "Index": IndexEntity }  # 个股新闻也会提到大盘指数
```

**③ `post_normalizer.py` — ticker 接地：**

```python
class TickerGrounder:
    """基于 ticker_whitelist.json 的权威 name→ticker 接地器。"""

    def __init__(self, whitelist_path: str = "data/ticker_whitelist.json"):
        self._name_to_ticker: dict[str, str] = {}
        self._ticker_set: set[str] = set()
        self._load(whitelist_path)

    def ground(self, entity_name: str, llm_ticker: str | None) -> str | None:
        """白名单命中 → 返回权威 ticker；未命中 → 一律返回 None。
        LLM 填的 ticker 只有与白名单一致时才保留（双重校验）。"""
        hit = self._name_to_ticker.get(canonical_name(entity_name, "stock"))
        if hit:
            return hit
        if llm_ticker and llm_ticker in self._ticker_set:
            return llm_ticker        # LLM 填对了（白名单内合法代码）
        return None                  # 幻觉或白名单外 → 清除
```

接线：`write_one()` 后处理中，对 `result.nodes` 中的 Stock 标签节点执行 ground()，通过 Cypher `SET n.ticker = $ticker REMOVE n.ticker`（None 时 REMOVE）落库。同时对 Index 标签节点强制 `REMOVE n.ticker`（双保险）。

> name→ticker 匹配建议同时索引白名单的 `name` 与 `biz_code`，并用 `entity_canonical.canonical_name()` 归一后匹配。白名单只有 10 只时命中率低是正常的——此时正确行为就是 ticker=None，而不是幻觉值。**ticker 查询的可靠性 > 覆盖率。**

**④ 存量数据清理（迁移脚本）：**

```cypher
// 指数类 Stock 重打标 + 清除幻觉 ticker
MATCH (n:Stock)
WHERE n.name CONTAINS '指数' OR n.name CONTAINS 'Index' OR n.name =~ '.*\\d{2,}0指数.*'
SET n:Index, n.entity_type = 'Index'
REMOVE n:Stock, n.ticker, n.exchange

// 其余 Stock 的 ticker 不在白名单内的 → 清除（Python 侧读白名单比对）
```

**风险：**
- ticker 覆盖率短期下降（幻觉值被清除后按 ticker 查询命中的实体变少）。**这是预期行为**：此前按 ticker 查询命中本来就是错的（查到宁德时代的边挂在沪深300上）。白名单扩容后覆盖率自然恢复。
- 白名单 name 与新闻名称不一致时匹配失败 → 走别名表扩展（可复用 `entity_canonical.EN_ZH_MAP` 模式，逐步沉淀 `TICKER_ALIAS_MAP`）。
- `REMOVE n:Stock` 会影响 Graphiti 已建立的实体消歧缓存——迁移后旧 episode 不会重建，可接受。

---

### P1-1: 缺少 Person 类型

**结论：需要新增 PersonEntity，两套 entity_types 都加。**

数据依据：219 个仅 `['Entity']` 标签的节点中，抽样 25 个即有李彦宏、王军、陈华、周雪、斯科特·贝森特、劉淑儀等 ≥6 个人名（约 24%），其余为指数、产品、技术名词等杂项。没有 Person 类型时，人名只能落入无标签兜底或误分为 Organization。

**实现：**

```python
# entity_types.py
class PersonEntity(BaseModel):
    """人物实体 — 新闻中出现的具体人物（企业家、官员、分析师等）。

    示例: "李彦宏", "斯科特·贝森特", "任正非"
    Neo4j 节点标签: Entity:Person
    """
    entity_name: str = Field(
        ...,
        description="人物姓名，使用中文译名（外国人名用通用中文译名）",
    )
    title: str | None = Field(
        default=None,
        description="职务/头衔，例如 '百度CEO', '美国财政部长'。不知道留空",
    )

# 两套注册表均加入
MACRO_ENTITY_TYPES = { ..., "Person": PersonEntity }
SYMBOL_ENTITY_TYPES = { ..., "Person": PersonEntity }
```

**连带修改：**

```python
# translation.py
LABEL_TYPE_MAP: dict[str, str] = {
    "SECTOR": "sector",
    "STOCK": "stock",
+   "INDEX": "index",        # 见 P0-2
+   "PERSON": "person",
    "COUNTRY": "country",
    "POLICY": "policy",
}
```

- `src/api/models.py` 的 `EventEntityItem.type` 是自由 `str`（已验证非 Literal 枚举），新增 `"person"`/`"index"` 不会破坏 API 契约，但需**同步更新字段 description 文档**（"stock / sector / country / policy / person / index"）。
- Person 相关的边（WORKS_AT/IS_CEO_OF 等）正是 P0-1 中自造类型的大头，新增 Person 后这类边会继续以自造类型出现，由 edge Normalizer 统一收敛到 RELATED_TO——两个修复互相配合，顺序上应先上 Normalizer 再上 Person 类型。

**风险：** 低。纯新增类型。存量 219 个无标签节点不做批量回溯打标（人名自动识别误判风险高，且收益有限）；新写入数据自然带上 Person 标签。如后续需要，可做离线 LLM 批分类脚本单独立项。

---

### P1-2: sector 中英混合

**根因有两条，都要堵：**

1. **上游污染**：`data/ticker_whitelist.json` 的 sector 值本身是英文（Tech/Finance/Consumer），经 akshare_adapter 注入 EntityItem → 写入图。
2. **LLM 自由发挥**：宏观管线中 LLM 按自己习惯填英文 sector（field description 虽然写了中文，但仍是软约束）。

**推荐：三管齐下（数据修正 + schema 约束 + 写入后归一）。**

**① 修正白名单数据源：**

```json
// data/ticker_whitelist.json
- { "ticker": "0700.HK", "sector": "Tech", ... }
+ { "ticker": "0700.HK", "sector": "互联网平台", ... }
```

10 条映射：Tech→按公司性质细分（腾讯/阿里/美团→互联网平台，小米→消费电子）；Finance→金融；Consumer→消费。同时建议在 `ticker_sync.py` 的白名单生成/维护流程中加入"sector 必须为中文"的校验。

**② StockEntity.sector / SectorEntity field description 加枚举约束：**

```python
SECTOR_TAXONOMY = [
    "互联网平台", "半导体", "新能源", "金融", "房地产", "医药",
    "消费", "军工", "汽车", "电力设备", "通信", "原材料", "其他",
]

sector: str = Field(
    ...,
    description=(
        "所属行业/板块，必须使用中文，且优先从以下列表选择: "
        + "、".join(SECTOR_TAXONOMY)
        + "。禁止使用英文（如 Tech/Finance）。无法归类填 '其他'。"
    ),
)
```

（同时把现有的 `'Unknown'` 兜底改为 `'其他'`，保持全中文。）

**③ post_normalizer.py 写入后归一（硬约束兜底）：**

```python
SECTOR_EN_ZH_MAP = {
    "tech": "互联网平台", "technology": "互联网平台",
    "finance": "金融", "financial": "金融",
    "consumer": "消费", "consumer discretionary": "消费",
    "healthcare": "医药", "energy": "新能源",
    "power equipment": "电力设备", "semiconductor": "半导体",
    "real estate": "房地产",
    "unknown": "其他",
}

def normalize_sector(value: str | None) -> str:
    if not value:
        return "其他"
    zh = SECTOR_EN_ZH_MAP.get(value.strip().lower())
    if zh:
        return zh
    # 已是中文但不在 taxonomy 内 → 保留（允许新行业词，如"锂电池"）
    return value.strip()
```

Stock/Sector 节点写入后统一过此函数再落库。

**④ 存量清理：**

```cypher
MATCH (n:Entity) WHERE n.sector IS NOT NULL
// Python 侧按 normalize_sector 分组批量 SET
```

**风险：** 低。sector 仅用于展示与筛选聚合，归一不影响图结构。"锂电池"等 taxonomy 外的中文词保留不强制映射，避免过度收敛损失粒度。

---

### P2: 指数被当作 Stock

已在 P0-2 方案中一并解决（IndexEntity 拆分 + 存量迁移重打标）。核心取舍：

| 选项 | 结论 |
|------|------|
| IndexEntity 独立类型 | ✅ 采纳。指数无 ticker、不可交易，与 Stock 的查询语义完全不同（用户按 ticker 查的是个股事件，指数事件应走宏观视角）。独立类型后 schema 层面根除 ticker 幻觉 |
| 继续留在 Stock，ticker 填 null | ❌ 否决。`entity_type_from_labels()` 的 ticker fallback 逻辑、API 的 affected_tickers 聚合都会被指数污染，且 LLM 面对必填 ticker 字段仍会幻觉 |
| ETF 归属 | ETF（可交易、有代码）保留在 Stock；IndexEntity 的 docstring 已明确排除 ETF |

---

## 3. 需要修改的文件清单

| 文件 | 修改类型 | 内容 |
|------|---------|------|
| `src/graphiti/post_normalizer.py` | **新增** | 边类型规范化 + ticker 接地 + sector 归一（本方案核心模块） |
| `src/graphiti/entity_types.py` | 修改 | 新增 `IndexEntity`、`PersonEntity`；`StockEntity.ticker` 改 optional；sector description 加枚举；两套注册表加 Index/Person |
| `src/graphiti/relation_types.py` | 微调 | （可选）在 `("Entity","Entity")` 的 map 注释中补 few-shot 示例；定义不变 |
| `src/graphiti/episode_writer.py` | 修改 | `write_one()` 成功后调用 post_normalizer；注入 TickerGrounder 依赖 |
| `src/graphiti/translation.py` | 修改 | `LABEL_TYPE_MAP` 加 INDEX/PERSON；新增 `normalize_relation_name()` 读侧兜底 |
| `data/ticker_whitelist.json` | 修改 | sector 英文→中文（10 条） |
| `src/api/models.py` | 微调 | `EventEntityItem.type` 字段 description 更新类型清单（字段本身是 str，无需改结构） |
| `scripts/migrate_graph_normalization.py` | **新增** | 存量数据一次性迁移（边归一/指数重打标/幻觉 ticker 清除/sector 归一），支持 dry-run |
| `tests/graphiti/test_post_normalizer.py` | **新增** | normalize_edge_type / ground / normalize_sector 单元测试 |

---

## 4. 风险评估汇总

| 风险 | 等级 | 缓解 |
|------|------|------|
| graphiti-core 升级导致 `result.edges`/`result.nodes` 结构变化 | 中 | 沿用现有 `hasattr` 防御式写法；Normalizer 失败只 log warning 不阻断写入（降级为当前行为） |
| 同义边映射的语义损失（OWNS→RELATED_TO） | 低 | `original_name` 属性保留原值，可审计可回滚 |
| ticker 覆盖率短期下降 | 低（预期内） | 清除的是错误数据；白名单扩容后恢复。宁可 None 不可错 |
| 存量迁移误伤（如指数正则误匹配） | 中 | 迁移脚本先 dry-run 输出统计；`original_name` 留痕；小批量 LIMIT 执行 |
| Person 类型引入后自造边增多 | 低 | edge Normalizer 先行上线兜底 |
| 白名单 name 与新闻名称不匹配导致 ticker 接地失败 | 低 | 别名表渐进扩展；接地失败置 None 是安全行为 |

---

## 5. 实施优先级

**Phase 1 — P0 止血（建议 1-2 天，单个 PR）**
1. `post_normalizer.py`：edge 规范化 + ticker 接地 + sector 归一
2. `episode_writer.py` 接线
3. `entity_types.py`：ticker optional + IndexEntity
4. `translation.py`：读侧兜底 + LABEL_TYPE_MAP
5. 迁移脚本 dry-run → 执行
6. 验收标准（跑 Cypher 核对）：
   - `MATCH ()-[r:RELATES_TO]->() WHERE NOT r.name IN [...6种...] RETURN count(r)` → **0**
   - `MATCH (n:Stock) WHERE n.name CONTAINS '指数' RETURN count(n)` → **0**
   - 白名单内股票 ticker 正确率 100%，白名单外 Stock ticker 为 None

**Phase 2 — P1 完善（建议 0.5-1 天）**
7. PersonEntity 上线（两套注册表）
8. `ticker_whitelist.json` sector 中文化 + sector description 枚举
9. API models description 更新

**Phase 3 — 持续治理（后续迭代）**
10. 将第 6 条验收 Cypher 固化为每日 cron 数据质量巡检（复用 HEARTBEAT 机制），发现未定义类型占比 >5% 告警
11. ticker 别名表随白名单扩容沉淀
12. 观察 CAUSED_BY/MITIGATES 使用率：若持续为 0，下一轮评估从词表移除（减少 prompt token）或改为 episode 级标注

---

## 6. 附：ADR

### ADR-001: 采用写入后处理硬约束取代 prompt 软约束

- **Status**: Proposed
- **Context**: Graphiti v0.29.2 的 `edge_type_map` 与 Pydantic field description 均为 prompt 级软约束。生产数据验证：32.6% 边违反关系词表（100 种自造类型），指数实体 ticker 100% 幻觉，sector 中英混合。软约束遵从率不足以支撑 API 层的数据契约。
- **Decision**: 新增 `post_normalizer.py` 作为 `add_episode()` 后的强制规范化层，在数据落库前完成边类型收敛、ticker 接地（白名单查表）、sector 归一。prompt 层保留作为"尽力而为"的第一道引导，但不再承担契约保证。
- **Consequences**:
  - ✅ Neo4j 数据 100% 符合定义词表，API 层无需防御未知类型
  - ✅ ticker 从"生成式"变为"查表式"，幻觉归零
  - ⚠️ 每个 episode 增加一次后处理 Cypher 往返（量级可忽略）
  - ⚠️ 部分 LLM 提取的细粒度语义（OWNS、WORKS_AT）被收敛为 RELATED_TO，原始值通过 `original_name` 属性保留
  - ⚠️ Normalizer 逻辑与 graphiti-core 返回结构耦合，SDK 升级需回归
