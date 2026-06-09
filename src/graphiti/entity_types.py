"""金融实体类型定义 — 供 graphiti-core v0.29.2 的 entity_types 参数使用。

graphiti-core 通过 model_json_schema() 使用这些 Pydantic 模型生成 LLM
的结构化输出 JSON Schema，LLM 按 schema 提取实体并填充对应字段。
提取的实体属性通过 EntityNode.attributes (dict) 存储为 Neo4j 节点属性。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "StockEntity",
    "SectorEntity",
    "CountryEntity",
    "PolicyEntity",
    "ENTITY_TYPES",
]


class StockEntity(BaseModel):
    """股票实体 — 在新闻中出现的可交易标的。

    属性映射到 EntityNode.attributes，可通过 Neo4j Cypher 查询:
        MATCH (n:Stock) WHERE n.ticker = '0700.HK' RETURN n
    Neo4j 节点标签: Entity:Stock
    """

    ticker: str = Field(
        ...,
        description="股票代码，格式: {biz_code}.{exchange}，例如 0700.HK, BABA.US, 600000.SS",
    )
    entity_name: str = Field(
        ...,
        description="股票名称，使用中文原名，例如 '腾讯控股', '阿里巴巴'",
    )
    sector: str | None = Field(
        default=None,
        description="所属行业/板块名称，例如 '互联网平台', '半导体'",
    )
    exchange: str | None = Field(
        default=None,
        description="交易所代码: HKEX, NYSE, NASDAQ, SSE, SZSE",
    )


class SectorEntity(BaseModel):
    """行业/板块实体 — 新闻报道中涉及的行业概念。

    示例: "互联网平台", "半导体", "新能源", "房地产"
    Neo4j 节点标签: Entity:Sector
    """

    entity_name: str = Field(
        ...,
        description="行业名称，使用中文，例如 '互联网平台', '新能源', '房地产'",
    )
    code: str | None = Field(
        default=None,
        description=(
            "行业代码（可选），例如 GICS 分类码 'GICS_50'，留空表示未分类"
        ),
    )


class CountryEntity(BaseModel):
    """国家/地区实体 — 新闻中涉及的国家或地区。

    示例: "中国", "美国", "日本", "欧盟"
    Neo4j 节点标签: Entity:Country
    """

    entity_name: str = Field(
        ...,
        description="国家/地区名称，使用中文，例如 '中国', '美国', '欧盟'",
    )
    code: str | None = Field(
        default=None,
        description=(
            "ISO 3166-1 alpha-2 国家代码，例如 CN, US, JP。"
            "留空表示非国家实体（如欧盟）"
        ),
    )


class PolicyEntity(BaseModel):
    """政策/监管实体 — 新闻报道中涉及的政策事件、监管行动。

    示例:
    - 反垄断调查 (type="regulatory", status="rumor")
    - 降息 (type="monetary", status="confirmed")
    - 财政刺激 (type="fiscal", status="announced")
    Neo4j 节点标签: Entity:Policy
    """

    entity_name: str = Field(
        ...,
        description="政策名称/描述，例如 '反垄断调查', '降息', '新能源汽车补贴'",
    )
    type: str = Field(
        ...,
        description=(
            "政策类型枚举: regulatory (监管), monetary (货币), fiscal (财政), "
            "trade (贸易), industrial (产业), environmental (环境), other (其他)"
        ),
    )
    status: str | None = Field(
        default="rumor",
        description=(
            "政策状态: rumor (传闻), announced (宣布), proposed (提案), "
            "confirmed (确认), implemented (已实施), resolved (已解决)"
        ),
    )


# ── 实体类型注册表 ──────────────────────────────────────────────────────

ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Stock": StockEntity,
    "Sector": SectorEntity,
    "Country": CountryEntity,
    "Policy": PolicyEntity,
}
"""所有支持的实体类型。

传递给 graphiti-core 的 add_episode(entity_types=...) 参数，
使 LLM 按这 4 种 schema 提取结构化实体。
"""
