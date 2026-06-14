"""GDELT GKG macro theme whitelist for financial event filtering.

19 core themes organized by category. Any theme keyword matching a GKG
record's V2.8 Themes field will cause the record to be retained (OR logic).

See Also:
    ``NEWSENGINE-DESIGN-DOC.md §3.6.2`` — 19 个核心金融主题白名单
"""

# ── 19 个核心宏观主题（GDELT GKG V2.8 Themes OR 匹配） ──────────────

MACRO_THEME_KEYWORDS: set[str] = {
    # 货币政策 (3)
    "MONETARY_POLICY",
    "CENTRAL_BANK",
    "INTEREST_RATE",
    # 宏观指标 (3)
    "GDP",
    "INFLATION",
    "RECESSION",
    # 贸易制裁 (3)
    "TRADE",
    "TARIFF",
    "SANCTION",
    # 地缘政治 (2)
    "GEOPOLITICAL",
    "WAR",
    # 监管 (2)
    "REGULATION",
    "ANTITRUST",
    # 市场 (2)
    "STOCK_MARKET",
    "CURRENCY",
    # 科技 (1)
    "SEMICONDUCTOR",
    # 能源 (1)
    "ENERGY",
    # 债务风险 (1)
    "DEBT",
    # 汇率 (1)
    "EXCHANGE_RATE",
}

__all__ = ["MACRO_THEME_KEYWORDS"]
