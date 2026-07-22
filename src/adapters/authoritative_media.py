"""Authoritative Media Domains — trusted financial/economics news sources.

Maintains ``AUTHORITATIVE_MEDIA_DOMAINS``, a ``set[str]`` of ~30 authoritative
financial and economics news domains for Plan D filter logic in GdeltAdapter.

All domain names are lowercase for O(1) case-insensitive lookup
(compare against ``.lower()`` of the record domain).

Replaces the former ``FINANCIAL_DOMAIN_WHITELIST`` (V2.3 → V2.4).

Plan D logic:
    - Domain in ``AUTHORITATIVE_MEDIA_DOMAINS`` → unconditional pass (no theme check)
    - Otherwise → must match ``MACRO_THEME_KEYWORDS`` in GKG V2.8 Themes

Coverage categories:
    - Global financial newswires (reuters.com, bloomberg.com, apnews.com, afp.com, bbc.com)
    - Top financial media (wsj.com, ft.com, economist.com, cnbc.com, marketwatch.com)
    - Central banks & international orgs (federalreserve.gov, ecb.europa.eu, etc.)
    - Asia-Pacific financial (scmp.com, nikkei.com, channelnewsasia.com, straitstimes.com)
    - Chinese financial (caixin.com, cls.cn, yicai.com, 21jingji.com, stcn.com)

Extensibility: Add new domains to the set — no code changes required elsewhere.
"""

from __future__ import annotations

AUTHORITATIVE_MEDIA_DOMAINS: set[str] = {
    # ── Global financial newswires ────────────────────────────────
    "reuters.com",
    "bloomberg.com",
    "apnews.com",
    "afp.com",
    "bbc.com",
    "bbc.co.uk",
    # ── Top financial media ───────────────────────────────────────
    "wsj.com",
    "ft.com",
    "economist.com",
    "cnbc.com",
    "marketwatch.com",
    "barrons.com",
    "investing.com",
    "finance.yahoo.com",
    # ── Central banks & international organizations ──────────────
    "federalreserve.gov",
    "ecb.europa.eu",
    "imf.org",
    "worldbank.org",
    "bis.org",
    "oecd.org",
    "bankofengland.co.uk",
    "boj.or.jp",
    "pbc.gov.cn",
    # ── Asia-Pacific financial ───────────────────────────────────
    "scmp.com",
    "nikkei.com",
    "channelnewsasia.com",
    "businesstimes.com.sg",
    "straitstimes.com",
    "theedgemalaysia.com",
    # NOTE: indiatimes.com removed — it's general news, not financial media
    # ── Chinese financial ─────────────────────────────────────────
    "caixin.com",
    "cls.cn",
    "yicai.com",
    "21jingji.com",
    "stcn.com",
}
"""Set of ~30 authoritative financial/economics news domains for Plan D filtering.

Usage::
    from src.adapters.authoritative_media import AUTHORITATIVE_MEDIA_DOMAINS

    domain = record.get("domain", "").strip().lower()
    if domain in AUTHORITATIVE_MEDIA_DOMAINS:
        # Record passes Plan D — unconditional pass
"""

__all__ = ["AUTHORITATIVE_MEDIA_DOMAINS"]
