"""金融关系类型定义 — 供 graphiti-core v0.29.2 的 edge_types/edge_type_map 参数使用。

- edge_types dict 定义关系字段 schema（LLM 按此 schema 提取关系事实）
- edge_type_map dict 定义哪些实体类型对之间可以提取哪些关系
- Edge 的 name 字段 = 关系类型名称（如 "AFFECTS"），写入 Neo4j 为
  :RELATES_TO {name: 'AFFECTS'} 关系
- 额外属性通过 EntityEdge.attributes dict 存储为 Neo4j 关系属性

G8 精简（2026-09-08）: 删除实际数据为 0 条的死类型 CAUSED_BY / MITIGATES /
LOCATED_IN，新增 LLM 已大量使用的 INVOLVES（272 条）。收敛为 4 种:
AFFECTS / INVOLVES / BELONGS_TO / RELATED_TO（兜底）。
历史遗留边名由 episode_writer.normalize_edge_type / scripts/renormalize_edge_types.py
在写入侧与存量侧分别归一，不因本次删除产生孤儿数据。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "AffectsEdge",
    "InvolvesEdge",
    "BelongsToEdge",
    "RelatedToEdge",
    "EDGE_TYPES",
    "DEFAULT_EDGE_TYPE_MAP",
]


class AffectsEdge(BaseModel):
    """AFFECTS 关系: 影响/冲击 — 一个实体对另一个实体产生正面或负面影响。

    语义定义: 金融知识图谱中因果影响的核心载体，覆盖事件冲击、政策影响、
    地缘扰动等一切 "A 对 B 造成影响" 的事实。

    方向约定: 影响方 → 被影响方（A AFFECTS B = A 影响 B）。
    实体对约束: 任意实体类型 → 任意实体类型（不限于股票）。
    典型示例:
    - "监管传闻 → AFFECTS → 腾讯控股"（Event → Stock）
    - "芯片出口管制 → AFFECTS → 半导体"（Policy → Sector）
    - "关税加征 → AFFECTS → 中国"（Event → Country）
    """

    fact: str = Field(
        ...,
        description="描述 A 如何影响 B 的一句话事实陈述，使用中文",
    )
    valid_at: str = Field(
        ...,
        description="关系生效的日期 (YYYY-MM-DD)，从新闻发布时间推导",
    )
    severity: str | None = Field(
        default="medium",
        description="影响程度: low | medium | high | critical",
    )


class InvolvesEdge(BaseModel):
    """INVOLVES 关系: 参与关系 — 主体与事件/组织之间的参与者角色关系。

    语义定义: 表达 "谁参与了什么" — 人物在组织任职/隶属、事件涉及的
    人物或机构、组织与组织之间的交往互动（合作/谈判/调查）。

    方向约定: 主体侧 → 对象侧。
    实体对约束与典型示例:
    - Person → Organization（任职/隶属）: "鲍威尔 → INVOLVES → 美联储"
    - Event → Person（事件涉及人物）: "降息决议 → INVOLVES → 鲍威尔"
    - Organization → Organization（机构间互动）: "证监会 → INVOLVES → 腾讯控股"
    """

    fact: str = Field(
        ...,
        description="描述参与关系的一句话事实陈述，使用中文",
    )
    valid_at: str | None = Field(
        default=None,
        description="关系成立的日期 (YYYY-MM-DD)",
    )


class BelongsToEdge(BaseModel):
    """BELONGS_TO 关系: 分类归属 — 实体属于某个板块/行业/分类概念。

    语义定义: 表达 "X 属于分类 Y"，是标的到板块的结构映射，
    支撑 "宏观事件 → 板块 → 个股" 的影响传导查询路径。

    方向约定: 成员 → 分类（A BELONGS_TO B = A 属于 B）。
    实体对约束: Stock → Sector（个股归属行业/板块）。
    典型示例:
    - "腾讯控股 → BELONGS_TO → 互联网平台"
    - "中芯国际 → BELONGS_TO → 半导体"
    """

    fact: str = Field(
        ...,
        description="描述实体与分类的归属关系事实，使用中文",
    )


class RelatedToEdge(BaseModel):
    """RELATED_TO 关系: 通用关联 — 兜底类型。

    语义定义: 两个实体之间存在值得记录的关联，但该关联无法归入
    AFFECTS / INVOLVES / BELONGS_TO 任一具体类型时使用。

    兜底约定: 这是唯一合法的兜底类型 — 无法匹配具体关系类型时一律使用
    RELATED_TO，禁止自创关系类型名。

    方向约定: 无严格方向，按文本叙述顺序书写（A → B）。
    实体对约束: 任意实体类型 → 任意实体类型。
    典型示例:
    - "股价波动 → RELATED_TO → 反垄断调查"
    - "加息 → RELATED_TO → 通胀数据"
    """

    fact: str = Field(
        ...,
        description="描述两个实体之间关联关系的事实，使用中文",
    )
    valid_at: str = Field(
        ...,
        description="关系成立的日期 (YYYY-MM-DD)",
    )


# ── 关系类型注册表 ──────────────────────────────────────────────────────

EDGE_TYPES: dict[str, type[BaseModel]] = {
    "AFFECTS": AffectsEdge,
    "INVOLVES": InvolvesEdge,
    "BELONGS_TO": BelongsToEdge,
    "RELATED_TO": RelatedToEdge,
}
"""所有支持的关系类型（G8 精简后共 4 种）。

传递给 graphiti-core 的 add_episode(edge_types=...) 参数。
删除的死类型（实际数据 0 条）: CAUSED_BY / MITIGATES / LOCATED_IN。
"""

DEFAULT_EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Entity", "Entity"): [
        "AFFECTS",
        "INVOLVES",
        "BELONGS_TO",
        "RELATED_TO",
    ],
    ("Entity", "Stock"): ["AFFECTS"],
    ("Stock", "Sector"): ["BELONGS_TO"],
    ("Entity", "Policy"): ["RELATED_TO"],
    # INVOLVES 参与关系映射（G8 新增）
    ("Person", "Organization"): ["INVOLVES"],
    ("Event", "Person"): ["INVOLVES"],
    ("Organization", "Organization"): ["INVOLVES"],
    # Event 实体关系映射
    ("Event", "Country"): ["AFFECTS"],
    ("Event", "Organization"): ["AFFECTS"],
    ("Event", "Sector"): ["AFFECTS"],
    ("Event", "Topic"): ["RELATED_TO"],
}
"""默认关系类型映射。

定义哪些实体类型对之间可以提取哪些关系。
传递给 graphiti-core 的 add_episode(edge_type_map=...) 参数。

G8 变更:
    - 删除 ("Stock", "Country"): [LOCATED_IN]（死类型）
    - 新增 ("Person", "Organization") / ("Event", "Person") /
      ("Organization", "Organization"): [INVOLVES] 参与关系映射

Event 实体类型映射:
    - (Event, Country): [AFFECTS] — 事件影响国家
    - (Event, Organization): [AFFECTS] — 事件影响组织
    - (Event, Sector): [AFFECTS] — 事件影响行业/板块
    - (Event, Topic): [RELATED_TO] — 事件与主题关联
"""

# G8_FIX_COMPLETE
