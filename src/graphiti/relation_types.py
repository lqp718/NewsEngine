"""金融关系类型定义 — 供 graphiti-core v0.29.2 的 edge_types/edge_type_map 参数使用。

- edge_types dict 定义关系字段 schema（LLM 按此 schema 提取关系事实）
- edge_type_map dict 定义哪些实体类型对之间可以提取哪些关系
- Edge 的 name 字段 = 关系类型名称（如 "AFFECTS"），写入 Neo4j 为
  :RELATES_TO {name: 'AFFECTS'} 关系
- 额外属性通过 EntityEdge.attributes dict 存储为 Neo4j 关系属性
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "AffectsEdge",
    "CausedByEdge",
    "MitigatesEdge",
    "BelongsToEdge",
    "LocatedInEdge",
    "RelatedToEdge",
    "EDGE_TYPES",
    "DEFAULT_EDGE_TYPE_MAP",
]


class AffectsEdge(BaseModel):
    """AFFECTS 关系: 事件/新闻对股票的影响。

    方向: (任意 Entity) → Stock
    示例: "监管传闻 → AFFECTS → 0700.HK"
    """

    fact: str = Field(
        ...,
        description="描述事件如何影响该股票，一句话，使用中文",
    )
    valid_at: str = Field(
        ...,
        description="关系生效的日期 (YYYY-MM-DD)，从新闻发布时间推导",
    )
    severity: str | None = Field(
        default="medium",
        description="影响程度: low | medium | high | critical",
    )


class CausedByEdge(BaseModel):
    """CAUSED_BY 关系: 事件 A 被事件 B 引起。

    方向: (任意 Entity) → (任意 Entity)
    示例: "股价跳水 → CAUSED_BY → 监管传闻"
    """

    fact: str = Field(
        ...,
        description="描述因果关系的单向事实陈述，使用中文",
    )
    valid_at: str = Field(
        ...,
        description="因果关系成立的日期 (YYYY-MM-DD)",
    )
    confidence: str | None = Field(
        default="medium",
        description=(
            "因果推断置信度: low (猜测) | medium (合理推断) | high (明确声明)"
        ),
    )


class MitigatesEdge(BaseModel):
    """MITIGATES 关系: 事件 B 缓解事件 A 的负面影响。

    方向: (任意 Entity) → (任意 Entity)
    示例: "公司回购 → MITIGATES → 股价下跌"
    """

    fact: str = Field(
        ...,
        description="描述缓解关系的单向事实陈述，使用中文",
    )
    valid_at: str = Field(
        ...,
        description="缓解操作发生的日期 (YYYY-MM-DD)",
    )


class BelongsToEdge(BaseModel):
    """BELONGS_TO 关系: 股票属于某个行业/板块。

    方向: Stock → Sector
    示例: "0700.HK → BELONGS_TO → 互联网平台"
    """

    fact: str = Field(
        ...,
        description="描述股票与行业的归属关系事实，使用中文",
    )


class LocatedInEdge(BaseModel):
    """LOCATED_IN 关系: 股票上市地/注册地所在国家。

    方向: Stock → Country
    示例: "0700.HK → LOCATED_IN → 中国"
    """

    fact: str = Field(
        ...,
        description="描述股票与国家的归属关系，使用中文",
    )


class RelatedToEdge(BaseModel):
    """RELATED_TO 关系: 事件与政策之间的关联。

    方向: (任意 Entity) → Policy
    示例: "股价波动 → RELATED_TO → 反垄断调查"
    """

    fact: str = Field(
        ...,
        description="描述事件与政策的关联关系，使用中文",
    )
    valid_at: str = Field(
        ...,
        description="关系成立的日期 (YYYY-MM-DD)",
    )


# ── 关系类型注册表 ──────────────────────────────────────────────────────

EDGE_TYPES: dict[str, type[BaseModel]] = {
    "AFFECTS": AffectsEdge,
    "CAUSED_BY": CausedByEdge,
    "MITIGATES": MitigatesEdge,
    "BELONGS_TO": BelongsToEdge,
    "LOCATED_IN": LocatedInEdge,
    "RELATED_TO": RelatedToEdge,
}
"""所有支持的关系类型。

传递给 graphiti-core 的 add_episode(edge_types=...) 参数。
"""

DEFAULT_EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Entity", "Entity"): [
        "AFFECTS",
        "CAUSED_BY",
        "MITIGATES",
        "BELONGS_TO",
        "LOCATED_IN",
        "RELATED_TO",
    ],
    ("Entity", "Stock"): ["AFFECTS"],
    ("Stock", "Sector"): ["BELONGS_TO"],
    ("Stock", "Country"): ["LOCATED_IN"],
    ("Entity", "Policy"): ["RELATED_TO"],
}
"""默认关系类型映射。

定义哪些实体类型对之间可以提取哪些关系。
传递给 graphiti-core 的 add_episode(edge_type_map=...) 参数。
"""
