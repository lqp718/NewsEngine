"""金融实体类型定义 — 供 graphiti-core v0.29.2 的 entity_types 参数使用。

graphiti-core 通过 model_json_schema() 使用这些 Pydantic 模型生成 LLM
的结构化输出 JSON Schema，LLM 按 schema 提取实体并填充对应字段。
提取的实体属性通过 EntityNode.attributes (dict) 存储为 Neo4j 节点属性。

两套 entity_types:
- MACRO_ENTITY_TYPES: 宏观管线（GDELT、RSS）使用。包含 Country, Policy, Organization, Topic, Sector, Event
- SYMBOL_ENTITY_TYPES: 个股管线（AkShare）使用。包含 Stock, Sector, Organization, Country, Policy, Event

StockEntity.sector 与 SectorEntity.entity_name 统一使用中文行业名（如 "互联网平台"），保持语义一致。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "StockEntity",
    "SectorEntity",
    "CountryEntity",
    "PolicyEntity",
    "OrganizationEntity",
    "TopicEntity",
    "EventEntity",
    "SymbolEventEntity",
    "MACRO_ENTITY_TYPES",
    "SYMBOL_ENTITY_TYPES",
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
    sector: str = Field(
        ...,
        description=(
            "所属行业/板块名称，使用中文，"
            "例如 '互联网平台', '半导体', '新能源', '金融', '房地产', '医药', '消费', '军工'。"
            "根据白名单参考或公司名称推断行业。如果无法推断填 'Unknown'。"
        ),
    )
    exchange: str = Field(
        ...,
        description=(
            "交易所代码。根据股票代码后缀或公司名称推断: "
            "HKEX (港股，代码以0/1/2/3开头或后缀.HK), "
            "NYSE (纽交所), NASDAQ (纳斯达克), "
            "SSE (上交所，后缀.SS或60/68开头), "
            "SZSE (深交所，后缀.SZ或00/30开头)。"
            "如果无法确定填 'Unknown'。"
        ),
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


class CountryEntity(BaseModel):
    """国家/地区实体 — 新闻中涉及的国家或地区。

    示例: "中国", "美国", "日本", "欧盟"
    Neo4j 节点标签: Entity:Country
    """

    entity_name: str = Field(
        ...,
        description="国家/地区名称，使用中文，例如 '中国', '美国', '欧盟'",
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
    status: str = Field(
        ...,  # 保持必填
        description=(
            "政策状态: rumor (传闻), announced (宣布), proposed (提案), "
            "confirmed (确认), implemented (已实施), resolved (已解决)。"
            "如果新闻中未明确提到状态，填 'rumor'。"
        ),
    )


class OrganizationEntity(BaseModel):
    """组织/机构/公司实体 — 新闻中涉及的企业、机构、政府部门。

    示例: "腾讯控股", "美联储", "证监会", "世界卫生组织"
    Neo4j 节点标签: Entity:Organization
    """
    entity_name: str = Field(
        ...,
        description="组织/机构/公司名称，使用中文原名",
    )


class TopicEntity(BaseModel):
    """主题/话题实体 — 宏观新闻中涉及的主题概念。

    示例: "加息", "贸易战", "芯片出口管制", "新冠"
    Neo4j 节点标签: Entity:Topic
    """
    entity_name: str = Field(
        ...,
        description="主题/事件/概念名称",
    )
    category: str | None = Field(
        default=None,
        description="主题分类: 货币政策/贸易/科技/地缘政治/公共卫生 等。"
                    "不知道请省略。",
    )


class EventEntity(BaseModel):
    """事件实体 — 从 GDELT Events CSV 或新闻文本中提取的 CAMEO 事件。

    LLM 通过 MACRO_ENTITY_TYPES 提取事件时，按此 schema 填充字段。
    用于建立 "事件→国家"、"事件→行业"、"事件→股票" 的因果关系网络。

    字段说明:
    - entity_name: 事件描述，使用中文，一句话概括事件内容
    - actor1: 发起方名称，翻译后的中文名（如 "中国"）
    - actor2: 接收方名称，翻译后的中文名（如 "美国"）
    - cameo_code: CAMEO 事件代码，如 "141"、"173"
    - goldstein_scale: Goldstein 合作/冲突评分 (-10 ~ +10)
    - tone: 新闻语调评分 (-100 ~ +100，已归一化)
    - event_date: 事件发生日期，格式 YYYY-MM-DD

    Neo4j 节点标签: Entity:Event
    """

    entity_name: str = Field(
        ...,
        description="事件描述，使用中文，一句话概括事件内容，例如 '中国对美国加征关税'",
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），翻译后的中文名，例如 '中国'",
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），翻译后的中文名，例如 '美国'",
    )
    cameo_code: str | None = Field(
        default=None,
        description="CAMEO 事件代码，如 '141'、'173'、'163'",
    )
    goldstein_scale: float | None = Field(
        default=None,
        description="Goldstein 合作/冲突评分，范围 -10 ~ +10，数值越大越合作",
    )
    tone: float | None = Field(
        default=None,
        description="新闻语调评分，范围 -100 ~ +100，已归一化数值。"
                    "GDELT GKG CSV 的 V2Tone 字段",
    )
    event_date: str | None = Field(
        default=None,
        description="事件发生日期，格式 YYYY-MM-DD",
    )


class SymbolEventEntity(BaseModel):
    """简化事件实体 — 供个股管线（AkShare）使用的轻量事件类型。

    SYMBOL 版本不含 CAMEO 字段（cameo_code、goldstein_scale、tone），
    因为个股新闻通常不携带 CAMEO 编码，让 LLM 填写只会产生幻觉。
    简化版减少 LLM token 消耗且降低噪音。

    字段说明:
    - entity_name: 事件描述，使用中文
    - actor1: 发起方名称
    - actor2: 接收方名称
    - event_date: 事件发生日期，格式 YYYY-MM-DD

    Neo4j 节点标签: Entity:Event
    """

    entity_name: str = Field(
        ...,
        description="事件描述，使用中文，例如 '腾讯被纳入恒生科技指数'",
    )
    actor1: str | None = Field(
        default=None,
        description="Actor1 名称（发起方），翻译后的中文名",
    )
    actor2: str | None = Field(
        default=None,
        description="Actor2 名称（接收方），翻译后的中文名",
    )
    event_date: str | None = Field(
        default=None,
        description="事件发生日期，格式 YYYY-MM-DD",
    )


# ── 实体类型注册表（两套） ─────────────────────────────────────────────

MACRO_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Topic": TopicEntity,
    "Policy": PolicyEntity,
    "Sector": SectorEntity,
    "Event": EventEntity,
}
"""宏观管线使用的实体类型（GDELT、RSS）。

包含: Organization, Country, Topic, Policy, Sector, Event

宏观新闻中提到的行业概念（如 "互联网平台监管"、"新能源补贴退坡"）提取为 SectorEntity。
"""

SYMBOL_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Stock": StockEntity,
    "Sector": SectorEntity,
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Policy": PolicyEntity,
    "Event": SymbolEventEntity,
}
"""个股管线使用的实体类型（AkShare）。

包含: Stock, Sector, Organization, Country, Policy, Event
"""
