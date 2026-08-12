"""Unit tests for the in-memory cookie pool (scraping-pipeline-optimization §2).

Covers:
- pool_put / pool_get round-trip
- TTL expiry with lazy eviction
- fingerprint key isolation (cross-fingerprint = no injection)
- pool_invalidate behavior
"""

from __future__ import annotations

import pytest

import src.utils.news_spider as ns


@pytest.fixture(autouse=True)
def _reset_cookie_pool():
    """Clear the module-level pool before and after each test."""
    ns._domain_cookie_pool.clear()
    yield
    ns._domain_cookie_pool.clear()


class TestCookiePool:
    async def test_put_then_get_roundtrip(self):
        await ns.pool_put(
            "example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "abc123"}
        )
        cookies = await ns.pool_get("example.com", ns.TIER1_FINGERPRINT)
        assert cookies == {"cf_clearance": "abc123"}

    async def test_get_missing_key_returns_none(self):
        assert await ns.pool_get("example.com", ns.TIER1_FINGERPRINT) is None

    async def test_get_returns_copy(self):
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "x"})
        cookies = await ns.pool_get("example.com", ns.TIER1_FINGERPRINT)
        assert cookies is not None
        cookies["cf_clearance"] = "mutated"
        again = await ns.pool_get("example.com", ns.TIER1_FINGERPRINT)
        assert again == {"cf_clearance": "x"}

    async def test_put_empty_cookies_ignored(self):
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {})
        assert ("example.com", ns.TIER1_FINGERPRINT) not in ns._domain_cookie_pool

    async def test_ttl_expiry_lazy_eviction(self):
        """Expired entry is removed on read and treated as a miss."""
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "x"})
        entry = ns._domain_cookie_pool[("example.com", ns.TIER1_FINGERPRINT)]
        entry.fetched_at -= (ns.COOKIE_TTL_SEC + 10)

        assert await ns.pool_get("example.com", ns.TIER1_FINGERPRINT) is None
        assert ("example.com", ns.TIER1_FINGERPRINT) not in ns._domain_cookie_pool

    async def test_fresh_entry_within_ttl_served(self):
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "x"})
        entry = ns._domain_cookie_pool[("example.com", ns.TIER1_FINGERPRINT)]
        entry.fetched_at -= (ns.COOKIE_TTL_SEC - 60)  # still valid
        cookies = await ns.pool_get("example.com", ns.TIER1_FINGERPRINT)
        assert cookies == {"cf_clearance": "x"}

    async def test_fingerprint_key_isolation(self):
        """(domain, chrome146) must NOT be served to (domain, firefox135)."""
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "x"})
        assert await ns.pool_get("example.com", "firefox135") is None
        assert await ns.pool_get("example.com", "safari15_5") is None

    async def test_domain_key_isolation(self):
        await ns.pool_put("a.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "a"})
        await ns.pool_put("b.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "b"})
        assert (await ns.pool_get("a.com", ns.TIER1_FINGERPRINT)) == {"cf_clearance": "a"}
        assert (await ns.pool_get("b.com", ns.TIER1_FINGERPRINT)) == {"cf_clearance": "b"}

    async def test_invalidate_removes_entry(self):
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "x"})
        await ns.pool_invalidate("example.com", ns.TIER1_FINGERPRINT)
        assert ("example.com", ns.TIER1_FINGERPRINT) not in ns._domain_cookie_pool
        assert await ns.pool_get("example.com", ns.TIER1_FINGERPRINT) is None

    async def test_invalidate_missing_key_is_noop(self):
        # Should not raise
        await ns.pool_invalidate("example.com", ns.TIER1_FINGERPRINT)

    async def test_invalidate_only_removes_target_fingerprint(self):
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "c"})
        await ns.pool_put("example.com", "firefox135", {"cf_clearance": "f"})
        await ns.pool_invalidate("example.com", ns.TIER1_FINGERPRINT)
        assert ("example.com", ns.TIER1_FINGERPRINT) not in ns._domain_cookie_pool
        assert ("example.com", "firefox135") in ns._domain_cookie_pool

    async def test_put_replaces_existing_entry(self):
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "old"})
        await ns.pool_put("example.com", ns.TIER1_FINGERPRINT, {"cf_clearance": "new"})
        cookies = await ns.pool_get("example.com", ns.TIER1_FINGERPRINT)
        assert cookies == {"cf_clearance": "new"}
