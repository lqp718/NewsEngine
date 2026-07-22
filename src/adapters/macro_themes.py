"""GDELT GKG macro theme whitelist for financial event filtering.

19 core themes organized by category. Any theme keyword matching a GKG
record's V2.8 Themes field will cause the record to be retained (OR logic).

See Also:
    ``NEWSENGINE-DESIGN-DOC.md §3.6.2`` — 19 个核心金融主题白名单
"""

# ── 19 个核心宏观主题（GDELT GKG V2.8 Themes 匹配） ──────────────
# 使用 GKG 实际的 theme 格式（如 "Econ Inflation" 而不是 "INFLATION"）
# 匹配逻辑：单词边界匹配（避免 "WAR" 匹配 "WARSAW"）

MACRO_THEME_KEYWORDS: set[str] = {
    # 货币政策 (3)
    "Econ Centralbank",
    "Econ Interest Rates",
    "MONETARY_POLICY",  # fallback for older format
    # 宏观指标 (3)
    "Econ Inflation",
    "Econ Deflation",
    "RECESSION",
    "GDP",
    # 贸易制裁 (3)
    "Econ Freetrade",
    "TARIFF",
    "SANCTION",
    "EMBARGO",
    "BLOCKADE",
    # 地缘政治 (2)
    "GEOPOLITICAL",
    "WAR",
    "ARMEDCONFLICT",
    "CEASEFIRE",
    # 监管 (2)
    "REGULATION",
    "ANTITRUST",
    # 市场 (2)
    "Econ Stockmarket",
    "Econ Currency Exchange Rate",
    "Econ Bitcoin",
    "Econ Goldprice",
    "Econ Oilprice",
    # 科技 (1)
    "SEMICONDUCTOR",
    # 能源 (1)
    "ENERGY",
    "Econ Dieselprice",
    "Econ Gasolineprice",
    # 债务风险 (1)
    "Econ Debt",
    "Econ Budget Deficit",
    "Econ Bankruptcy",
}

__all__ = ["MACRO_THEME_KEYWORDS"]
