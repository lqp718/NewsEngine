# entity_type / group_id / 数据质量 根因研究报告

日期：2026-08-22 | 环境：graphiti-core **0.29.3**，Neo4j bolt://localhost:7687
数据快照：1573 Entity + 391 Episodic + 929 RELATES_TO（全部写于 2026-08-22 12:49–13:28 UTC，单次运行产物）

---

## 问题 1：entity_type 为什么全部 None？

### 根因：`entity_type` 这个属性在 graphiti-core 0.29.3 中**根本不存在**

这是版本语义变更，不是我们的代码缺陷：

- 0.29.3 的 `EntityNode`（`nodes.py:499-504`）只有 `name / name_embedding / summary / attributes / group_id / labels`，**没有 `entity_type` 字段**。
- 实体类型被改为 **Neo4j 标签（label）** + 节点上冗余存储的 `labels` 属性数组。

证据链：
| 环节 | 位置 | 说明 |
|---|---|---|
| LLM 抽取 | `prompts/extract_nodes.py:30` | LLM 输出 `entity_type_id`（整数），映射到我们传入的 entity_types |
| ID → 类型名 | `utils/maintenance/node_operations.py:302-313` | `type_id → entity_type_name → labels = ['Entity', 类型名]`；越界/缺省回退为裸 `'Entity'` |
| 落库 | `utils/bulk_utils.py:178` | `'labels': list(set(node.labels + ['Entity']))` 写入 Neo4j |
| Neo4j 实测 | `n.entity_type IS NOT NULL` | **0/1573**，Neo4j 直接警告 "property key entity_type is not in the database" |

**结论：检查口径错了。** 类型信息实际在 `labels(n)` / `n.labels` 上，且我们的注册是**正确生效的**：

```
Organization 606 | Country 286 | Topic 34 | Policy 29 | Sector 15 | Stock 10 | Event 1 | 裸 Entity 592
```

### 次生发现：592 个实体（37.6%）只有裸 `Entity` 标签

- 这 592 个节点全部没有 `entity_name` 属性（typed 节点 980/981 有），说明它们**未走类型化抽取路径**。
- 抽样显示绝大多数是 **OFAC 制裁名单人物/船只**（穆罕默德·礼萨·…、Mohammadreza Ashrafi Ghehi、EP-MTB 等）——`MACRO_ENTITY_TYPES`（`src/graphiti/entity_types.py`）中**没有 Person/Vessel 类型**，LLM 只能回退到兜底 type_id=0 → 裸 Entity。
- 名称中英文混杂的"罪魁祸首"也是这批：西语大写名（ARCA DE NOE III、AGENCIA DE CONTRATACION…）全部来自 OFAC SDN 原始数据；`custom_extraction_instructions` 的"翻译成中文"指令对陌生专有名词约束力弱（帕特·瑞安被翻了，ACINOX COMERCIAL 没翻）。

### 修复建议
1. **不要再查 `n.entity_type`**，统一用 `labels(n)` 或 `n.labels`（`src/graphiti/translation.py` 的 `entity_type_from_labels()` 已经是正确写法）。
2. 若希望制裁人物/船只类型化：在 `MACRO_ENTITY_TYPES` 增加 `Person` / `Vessel` 类型；否则接受 37.6% 裸 Entity 现状并在下游按 `unknown` 处理。
3. 若想补写 `entity_type` 属性（兼容旧查询），可一次性 Cypher 回填：`MATCH (n:Entity) SET n.entity_type = head([l IN labels(n) WHERE l <> 'Entity'])`——但建议改查询而非加冗余属性。

---

## 问题 2：group_id 的作用

### 我们的现状：全部为 `''`（空字符串，非 NULL），符合 0.29.3 默认行为

- `episode_writer.py:152-163` 调用 `add_episode()` 时**未传 `group_id`** → 走默认：`graphiti.py:1073-1076` → `helpers.py:68-76 get_default_group_id()`，Neo4j 场景返回 **`''`**。
- 实测：1573 Entity + 391 Episodic + 929 边，`group_id` 100% 为 `''`。

### group_id 的三个作用（源码证据）

| 作用 | 证据 | 影响 |
|---|---|---|
| **去重作用域** | `node_operations.py:443` 候选搜索 `node_similarity_search(..., [node.group_id], ...)` | 实体/边合并只在同 group 内进行，跨 group 不 dedup |
| **检索过滤** | `search_utils.py:96,217-219,237`：fulltext 与 Cypher 检索按 `group_id` 过滤 | 传 `group_ids=[x]` 时只返回该 group 数据；传 `None` 则不过滤 |
| **⚠️ 0.29.3 特殊行为：group_id = 数据库名** | `graphiti.py:1079-1082`：若显式传的 `group_id != driver._database`，会 `driver.clone(database=group_id)` | 传 `group_id="news"` 会直接**切到名为 news 的另一个 Neo4j 数据库**，不是普通命名空间！ |

### 结论与建议

- **当前 `''` 不是 bug**，是 Neo4j provider 的合法默认值，单租户场景下功能完全正常（我们的去重也很干净：同名实体仅"美国"×3）。
- 我们的下游（`translation.py` + `events.py`）走**原生 Cypher** 而非 graphiti search API，group_id 目前**不参与任何下游过滤**。
- 建议：**短期维持现状**（`''`）。若未来需要按来源/租户隔离，注意 0.29.3 的"group_id 即数据库"语义——要么接受多数据库，要么升级/评估后再用，且必须同步迁移存量数据的 `group_id` 属性（节点+边+Episodic），否则 dedup/search 会割裂。

---

## 问题 3：数据质量评估（实测）

### 字段完整性矩阵

| 对象 | 字段 | 覆盖率 | 评价 |
|---|---|---|---|
| Entity (1573) | name | 100% | ✅ |
| | name_embedding | 100% | ✅ |
| | created_at / uuid / group_id / labels 属性 | 100% | ✅ |
| | summary | 90.7%（146 缺） | ⚠️ 缺失的均为裸 Entity 兜底节点 |
| | 类型化标签 | 62.4% | ⚠️ 见问题 1 |
| | entity_name | 62.3%（=typed 集合） | ⚠️ 与类型化同源 |
| | ticker | 仅 5 个（10 个 Stock 中 5 个） | ⚠️ 白名单接地会 REMOVE 非白名单标的，属预期行为 |
| RELATES_TO (929) | fact | 100%，长度 7–244，均值 50 字 | ✅ 抽样为干净中文陈述句，无乱码 |
| | fact_embedding / episodes / reference_time | 100% | ✅ |
| | valid_at | 96.1% | ✅ |
| | invalid_at/expired_at | 97 条（10.4%） | ✅ 属 graphiti 时序失效机制的正常产物 |
| | severity | 仅 17 条（1.8%） | ⚠️ 只有 AFFECTS 边类型定义该属性（`relation_types.py:41`），AFFECTS 边本身只有 17 条 |
| | confidence | 1 条 | ⚠️ 同上，仅 CAUSED_BY 定义 |
| Episodic (391) | name/content/valid_at/source | 100% | ✅ |
| | episode_metadata（content_scope 透传） | 100% | ✅ 透传链路工作正常 |
| | 孤立（无 MENTIONS） | 2 条 | ✅ 可忽略（对应 content_fetched=false） |

### 结构健康度

- **完全孤立 Entity：0**（每个实体至少 1 条 MENTIONS，均值 1.46，最大 47）✅
- **无 RELATES_TO 边的 Entity：671（42.7%）** ⚠️ 这些实体只在 episode 中被提及，没有事实边——下游若靠 `rel.uuid IN e.entity_edges` 组装事件会漏掉它们（但作为实体列表展示仍可出现）。
- **实体去重质量：极好**，1573 节点仅 1 组同名重复（"美国"×3，可能是不同语义的 Country/Topic）。
- **边类型分布**：RELATED_TO 488、LOCATED_IN 216、WORKS_AT 35、AFFECTS/BELONGS_TO 各 17……分布合理，`RELATED_TO` 高占比源于 OFAC/GDELT 人物-组织类事实。

### 下游消费就绪度（对照 `translation.py` + `api/routers/events.py`）

| 下游需求 | 状态 |
|---|---|
| EventItem 的 title/summary/first_seen | ✅ Episodic 字段齐全 |
| entities 列表（name/type/ticker） | ✅ `RETURN e, entities` 返回整节点，`labels` 属性可读，`entity_type_from_labels()` 工作正常 |
| 实体类型映射完整性 | ⚠️ `LABEL_TYPE_MAP` 只映射 Sector/Stock/Country/Policy；Organization(606)/Topic(34)/Event(1) 全部落到 `"unknown"` —— 39% 的类型化实体会显示为 unknown |
| severity | ❌ `translation.py` 从 **Episodic 节点**读 severity，但写入侧从未在 Episodic 上写过该属性（17 条 severity 都在**边**上）→ 下游永远 "medium" |
| relations | ❌ `translate_episode_to_event()` 硬编码 `relations=None`，929 条干净的事实边**未暴露给 API** |
| briefing 的 affected_tickers | ⚠️ 仅 5 个实体有 ticker，affected_tickers 命中率会很低（白名单接地策略决定，属设计取舍） |

### 整体评估

**结论：基础质量可以支撑下游消费，但有 3 个明确的消费侧缺口。**

- ✅ **能用**：Episodic 100% 完整、fact 100% 且为干净中文、embedding 齐全、去重优秀、无孤立节点、content_scope 透传正常。事件列表/实体列表类 API 今天就可以消费。
- ❌ **影响功能的缺口**（按优先级）：
  1. **severity 读错位置**（读 Episodic 应读边，或写入侧补写）→ 所有事件显示 medium；
  2. **relations 恒为 None** → 929 条高质量事实边被浪费，事件详情无因果信息；
  3. **LABEL_TYPE_MAP 不全** → Organization/Topic/Event 实体类型显示 unknown；
- ⚠️ **数据侧可改进项**（不阻塞消费）：补 Person 类型覆盖 37.6% 裸 Entity；42.7% 无事实边的被提及实体；OFAC 西语名可按需做确定性归一（类似已有的 `_ground_tickers` 模式）。

---

## 修复清单汇总（仅建议，未改动代码）

| # | 问题 | 建议 | 复杂度 |
|---|---|---|---|
| 1 | 检查口径 `n.entity_type` | 改用 `labels(n)`/`n.labels`；可选一次性回填属性 | 低 |
| 2 | 37.6% 裸 Entity | MACRO_ENTITY_TYPES 增加 Person/Vessel | 中 |
| 3 | group_id 全 `''` | 维持现状；如要隔离先研究 0.29.3 "group_id=数据库"语义并整体迁移 | 低（不动）/ 高（迁移） |
| 4 | severity 读错位置 | `translation.py` 改从 AFFECTS 边聚合，或写入侧在 Episodic 上写 severity | 低 |
| 5 | relations 恒 None | 在 events.py 查询中带回 `rel.fact / rel.name` 并填充 relations | 中 |
| 6 | LABEL_TYPE_MAP 不全 | 增加 ORGANIZATION/TOPIC/EVENT 映射（需先确认 EventItem schema 支持） | 低 |
