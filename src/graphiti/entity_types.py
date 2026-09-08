"""金融实体类型定义 — 供 graphiti-core v0.29.2 的 entity_types 参数使用。

graphiti-core 通过 model_json_schema() 使用这些 Pydantic 模型生成 LLM
的结构化输出 JSON Schema，LLM 按 schema 提取实体并填充对应字段。
提取的实体属性通过 EntityNode.attributes (dict) 存储为 Neo4j 节点属性。

两套 entity_types:
- MACRO_ENTITY_TYPES: 宏观管线（GDELT、RSS）使用。包含 Country, Policy, Organization, Topic, Sector, Event, Person
- SYMBOL_ENTITY_TYPES: 个股管线（AkShare）使用。包含 Stock, Sector, Organization, Country, Policy, Event, Person

StockEntity.sector 与 SectorEntity.name 统一使用中文行业名（如 "互联网平台"），保持语义一致。
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
    "PersonEntity",
    "MACRO_ENTITY_TYPES",
    "SYMBOL_ENTITY_TYPES",
]


class StockEntity(BaseModel):
    """股票实体 — 在新闻中出现的可交易标的（个股/ETF）。

    注意：上市公司 = Stock，不是 Organization。
    指数（沪深300、标普500、恒生指数）不是股票，不要给它们填 ticker。

    属性映射到 EntityNode.attributes，可通过 Neo4j Cypher 查询:
        MATCH (n:Stock) WHERE n.ticker = '0700.HK' RETURN n
    Neo4j 节点标签: Entity:Stock
    """

    ticker: str | None = Field(
        default=None,
        description=(
            "股票代码，格式: {biz_code}.{exchange}。"
            "⚠️ 仅当新闻原文中明确出现代码时才填写；"
            "禁止根据公司名猜测或记忆生成代码。不确定时留空（系统会自动补全）。"
            "指数没有股票代码，请勿为指数填写此字段。"
        ),
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
    """行业/板块实体 — 新闻报道中涉及的行业分类概念。

    业务意义: Sector 是连接宏观新闻与个股表现的核心桥梁 —
    宏观事件（政策、地缘、产业周期）先作用于板块，再通过板块传导到
    板块内个股。查询 "某政策影响哪些股票" 时，Sector → Stock 的
    BELONGS_TO 归属关系是关键路径。

    典型示例: "互联网平台", "半导体", "新能源", "房地产", "医药", "军工"。
    新闻中提到的行业分类概念（含受政策监管、补贴、产业周期影响的
    行业层面对象）都应归类为 Sector。
    Neo4j 节点标签: Entity:Sector
    """


class CountryEntity(BaseModel):
    """国家/地区实体 — 新闻中涉及的主权国家或地区。

    业务意义: Country 是宏观分析中的地缘政治参与者 — 贸易战、制裁、
    关税、外交冲突均以国家为行为主体或受体。国家层面的事件通过
    "Country → Sector → Stock" 路径传导影响，是地缘风险分析的起点。

    典型示例: "中国", "美国", "日本", "欧盟", "俄罗斯"。
    新闻中作为行为主体或受影响方出现的主权国家、经济体联盟、
    独立关税区都应归类为 Country。
    Neo4j 节点标签: Entity:Country
    """


class PolicyEntity(BaseModel):
    """政策/监管实体 — 新闻报道中涉及的政策事件、监管行动、官方举措。

    业务意义: Policy 是金融市场最常见的外生冲击来源 — 货币政策
    （加息/降息）、财政政策（刺激计划）、监管行动（反垄断调查）、
    贸易政策（关税/出口管制）直接改变板块估值逻辑与企业盈利预期。
    Policy 实体把 "政府行为" 与 "市场反应" 连接起来，是政策驱动分析的起点。

    典型示例:
    - 反垄断调查 (type="regulatory", status="rumor")
    - 降息 (type="monetary", status="confirmed")
    - 财政刺激 (type="fiscal", status="announced")
    Neo4j 节点标签: Entity:Policy
    """

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
    """组织/机构实体 — 新闻中涉及的未上市企业、政府机构、国际组织。

    业务意义: Organization 覆盖参与市场事件但本身不是可交易标的的机构 —
    监管方（发布政策、发起调查）、央行（制定货币政策）、国际组织
    （协调地缘事务）。这些机构的行为常是股票/板块影响的上游原因，
    也是 INVOLVES 参与关系（Person → Organization）的对象侧。

    与 Stock 的区别: 上市公司（股票可交易、有 ticker）提取为 Stock；
    未上市企业、政府机构、监管部门、国际组织提取为 Organization。

    典型示例: "美联储", "证监会", "世界卫生组织", "财政部", "OpenAI"（未上市）。
    Neo4j 节点标签: Entity:Organization
    """


class TopicEntity(BaseModel):
    """主题/话题实体 — 宏观新闻中反复出现的主题概念。

    业务意义: Topic 把跨事件、跨时间的宏观叙事（如 "贸易战"、
    "芯片出口管制"）凝聚为可追踪对象，是比单个 Event 更高层的抽象。
    多个相关事件可关联到同一 Topic，用于主题级情绪追踪与历史回顾 —
    回答 "某主题如何演变" 时，Topic 是聚合锚点。

    典型示例: "加息", "贸易战", "芯片出口管制", "新冠"。
    跨多条新闻反复出现的宏观叙事概念归类为 Topic；
    单次发生的具体事件归类为 Event。
    Neo4j 节点标签: Entity:Topic
    """
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
    - name: 事件描述，使用中文，一句话概括事件内容
    - actor1: 发起方名称，翻译后的中文名（如 "中国"）
    - actor2: 接收方名称，翻译后的中文名（如 "美国"）
    - cameo_code: CAMEO 事件代码，如 "141"、"173"
    - goldstein_scale: Goldstein 合作/冲突评分 (-10 ~ +10)
    - tone: 新闻语调评分 (-100 ~ +100，已归一化)
    - event_date: 事件发生日期，格式 YYYY-MM-DD

    Neo4j 节点标签: Entity:Event
    """

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
    - name: 事件描述，使用中文
    - actor1: 发起方名称
    - actor2: 接收方名称
    - event_date: 事件发生日期，格式 YYYY-MM-DD

    Neo4j 节点标签: Entity:Event
    """

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


class PersonEntity(BaseModel):
    """自然人实体 — 新闻中涉及的个人。

    业务意义: 关键人物的言行具有显著市场影响力 — 央行行长的表态
    影响货币政策预期，公司高管的动向影响公司基本面，政治人物的决策
    影响地缘风险。Person 实体把 "个人言行" 与 "市场/机构反应" 连接起来，
    是 INVOLVES 参与关系（Person → Organization, Event → Person）的主体来源。

    典型示例: "鲍威尔", "易纲", "马斯克"。
    新闻中作为行为主体出现（发表言论、担任职务、被调查等）的
    自然人都应归类为 Person。
    Neo4j 节点标签: Entity:Person
    """
    title: str | None = Field(
        default=None,
        description="职务/头衔，例如 '美联储主席', '财政部部长'",
    )
    nationality: str | None = Field(
        default=None,
        description="国籍，例如 '美国', '中国'",
    )


# ── 实体类型注册表（两套） ─────────────────────────────────────────────

MACRO_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Topic": TopicEntity,
    "Policy": PolicyEntity,
    "Sector": SectorEntity,
    "Event": EventEntity,
    "Person": PersonEntity,
}
"""宏观管线使用的实体类型（GDELT、RSS）。

包含: Organization, Country, Topic, Policy, Sector, Event, Person

宏观新闻中提到的行业概念（如 "互联网平台监管"、"新能源补贴退坡"）提取为 SectorEntity。
"""

SYMBOL_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Stock": StockEntity,
    "Sector": SectorEntity,
    "Organization": OrganizationEntity,
    "Country": CountryEntity,
    "Policy": PolicyEntity,
    "Event": SymbolEventEntity,
    "Person": PersonEntity,
}
"""个股管线使用的实体类型（AkShare）。

包含: Stock, Sector, Organization, Country, Policy, Event, Person
"""

# G8_FIX_COMPLETE
