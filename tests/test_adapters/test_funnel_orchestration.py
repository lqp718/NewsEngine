"""Unit tests for the 5-tier funnel orchestration (scraping-pipeline-optimization §4.2).

Verifies:
- Tier 1 → 1.5 → 2 → 3 degradation order
- ``SpiderResult.fetch_tier`` marking on each tier's success path
- Tier 1.5 fingerprint order (firefox135 before safari15_5) and cookie injection
- Cookie-pool invalidation on continued block at Tier 1.5
- 404-type semantic failures never enter the retry set
- Backward compatibility of the public signature
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scrapling.engines.toolbelt.custom import Response

import src.utils.news_spider as ns
from src.utils.news_spider import (
    NewsSpider,
    SpiderResult,
    fetch_urls_with_spider,
)


def _make_response(
    url: str, status: int, html: str, request=None
) -> Response:
    resp = Response(
        url=url,
        content=html.encode("utf-8"),
        status=status,
        reason="OK" if status == 200 else "Forbidden",
        cookies={},
        headers={},
        request_headers={},
    )
    if request is not None:
        resp.request = request
    return resp


async def _stream_from(items: list[dict]):
    """Async generator replacement for NewsSpider.stream()."""
    for item in items:
        yield item


def _make_stream_fake(items: list[dict]):
    """Return a ``stream``-compatible function (bound to self) yielding items."""

    async def _stream(self, *args, **kwargs):
        for item in items:
            yield item

    return _stream


def _success_item(url: str, status: int = 200, html: str = "<html>ok</html>"):
    return {
        "url": url,
        "status": status,
        "html_content": html,
        "error": None,
        "used_stealth": False,
        "fetch_tier": "1",
    }


def _blocked_item(url: str, status: int = 403):
    return {
        "url": url,
        "status": status,
        "html_content": "",
        "error": "Tier 1 blocked, queued for retry",
        "used_stealth": False,
        "fetch_tier": "1",
    }


@pytest.fixture(autouse=True)
def _reset_pool():
    ns._domain_cookie_pool.clear()
    yield
    ns._domain_cookie_pool.clear()


class TestFunnelOrdering:
    """Tier 1 → 1.5 → 2 → 3 ordering and fetch_tier marking."""

    @patch.object(NewsSpider, "stream", new=_make_stream_fake([_success_item("https://a.com/1")]))
    @patch.object(NewsSpider, "_close_cloak_browser", new_callable=AsyncMock)
    @patch.object(NewsSpider, "_retry_with_alt_fingerprints", new_callable=AsyncMock)
    async def test_tier1_success_no_fallbacks(self, mock_tier15, mock_close):
        """All Tier 1 success → fetch_tier='1', no fallback tiers invoked."""
        results = await fetch_urls_with_spider(["https://a.com/1"])
        assert len(results) == 1
        assert results[0].fetch_tier == "1"
        assert results[0].html_content == "<html>ok</html>"
        mock_tier15.assert_not_called()

    @patch.object(NewsSpider, "stream", new=_make_stream_fake([_blocked_item("https://a.com/1")]))
    @patch.object(NewsSpider, "_close_cloak_browser", new_callable=AsyncMock)
    async def test_tier15_success_sets_fetch_tier(self, mock_close):
        """Tier 1.5 success → fetch_tier='1.5', CloakBrowser NOT launched."""
        async def _fake_tier15(self, results, failed_urls):
            for idx, result in failed_urls:
                results[idx] = SpiderResult(
                    url=result.url,
                    status=200,
                    html_content="<html>tier15 body</html>",
                    error=None,
                    used_stealth=False,
                    fetch_tier="1.5",
                )
            return []

        with patch.object(
            NewsSpider, "_retry_with_alt_fingerprints", new=_fake_tier15
        ), patch.object(
            NewsSpider, "_get_cloak_browser", new_callable=AsyncMock
        ) as mock_cloak_init, patch.object(
            NewsSpider, "_fetch_with_camoufox", new_callable=AsyncMock
        ) as mock_camoufox:
            results = await fetch_urls_with_spider(["https://a.com/1"])

        assert len(results) == 1
        assert results[0].fetch_tier == "1.5"
        assert results[0].used_stealth is False
        mock_cloak_init.assert_not_called()
        mock_camoufox.assert_not_called()

    @patch.object(NewsSpider, "stream", new=_make_stream_fake([_blocked_item("https://a.com/1")]))
    @patch.object(NewsSpider, "_close_cloak_browser", new_callable=AsyncMock)
    async def test_tier2_success_after_tier15_fails(self, mock_close):
        """Tier 1.5 fails → Tier 2 CloakBrowser succeeds → fetch_tier='2'."""
        async def _fake_tier15(self, results, failed_urls):
            return failed_urls  # all still failed

        with patch.object(
            NewsSpider, "_retry_with_alt_fingerprints", new=_fake_tier15
        ), patch.object(
            NewsSpider, "_get_cloak_browser", new_callable=AsyncMock
        ) as mock_cloak_init, patch.object(
            NewsSpider, "_fetch_with_cloak",
            new=AsyncMock(return_value=("<html>cloak body</html>", None)),
        ) as mock_cloak, patch.object(
            NewsSpider, "_fetch_with_camoufox", new_callable=AsyncMock
        ) as mock_camoufox:
            results = await fetch_urls_with_spider(["https://a.com/1"])

        assert len(results) == 1
        assert results[0].fetch_tier == "2"
        assert results[0].used_stealth is True
        assert results[0].html_content == "<html>cloak body</html>"
        mock_cloak_init.assert_called_once()
        mock_cloak.assert_awaited_once()
        mock_camoufox.assert_not_called()

    @patch.object(NewsSpider, "stream", new=_make_stream_fake([_blocked_item("https://a.com/1")]))
    @patch.object(NewsSpider, "_close_cloak_browser", new_callable=AsyncMock)
    async def test_tier3_success_after_tier2_fails(self, mock_close):
        """Tier 2 fails → Tier 3 Camoufox succeeds → fetch_tier='3'."""
        async def _fake_tier15(self, results, failed_urls):
            return failed_urls

        with patch.object(
            NewsSpider, "_retry_with_alt_fingerprints", new=_fake_tier15
        ), patch.object(
            NewsSpider, "_get_cloak_browser", new_callable=AsyncMock
        ), patch.object(
            NewsSpider, "_fetch_with_cloak",
            new=AsyncMock(return_value=("", "CloakBrowser error: crash")),
        ), patch.object(
            NewsSpider, "_fetch_with_camoufox",
            new=AsyncMock(
                return_value=SpiderResult(
                    url="https://a.com/1",
                    status=200,
                    html_content="<html>camoufox body</html>",
                    error=None,
                    used_stealth=True,
                    fetch_tier="3",
                )
            ),
        ) as mock_camoufox:
            results = await fetch_urls_with_spider(["https://a.com/1"])

        assert len(results) == 1
        assert results[0].fetch_tier == "3"
        assert results[0].used_stealth is True
        assert results[0].html_content == "<html>camoufox body</html>"
        mock_camoufox.assert_awaited_once()

    @patch.object(NewsSpider, "stream", new=_make_stream_fake([_success_item("https://a.com/1", status=404, html="<html>not found</html>")]))
    @patch.object(NewsSpider, "_close_cloak_browser", new_callable=AsyncMock)
    @patch.object(NewsSpider, "_retry_with_alt_fingerprints", new_callable=AsyncMock)
    @patch.object(NewsSpider, "_get_cloak_browser", new_callable=AsyncMock)
    @patch.object(NewsSpider, "_fetch_with_camoufox", new_callable=AsyncMock)
    async def test_404_semantic_failure_not_retried(
        self, mock_camoufox, mock_cloak_init, mock_tier15, mock_close
    ):
        """404 with content is a semantic failure — never enters retry set."""
        results = await fetch_urls_with_spider(["https://a.com/1"])
        assert len(results) == 1
        assert results[0].status == 404
        assert results[0].error is None
        mock_tier15.assert_not_called()
        mock_cloak_init.assert_not_called()
        mock_camoufox.assert_not_called()

    async def test_backward_compat_signature(self):
        """Existing call signature still works without new kwargs."""
        # Just verify the signature is unchanged (no TypeError on construction)
        spider = NewsSpider(urls=["https://a.com"], timeout_ms=15000)
        assert spider._timeout_ms == 15000
        # SpiderResult with only old kwargs
        r = SpiderResult(url="https://a.com", status=200, html_content="x")
        assert r.fetch_tier == "1"  # default


class _FakeSessionsContext:
    """Context manager combining the FetcherSession patcher and managers dict."""

    def __init__(self, patcher, managers):
        self._patcher = patcher
        self.managers = managers

    def __enter__(self):
        self._patcher.start()
        return self.managers

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


class TestTier15Routing:
    """Direct tests of _retry_with_alt_fingerprints with a fake FetcherSession."""

    class _FakeSession:
        def __init__(self, responses: list[Response]):
            self.responses = list(responses)
            self.calls: list[tuple[str, dict]] = []  # (url, kwargs)

        async def get(self, url: str, **kwargs):
            self.calls.append((url, kwargs))
            if not self.responses:
                raise RuntimeError("no more responses")
            return self.responses.pop(0)

    class _FakeSessionManager:
        def __init__(self, responses: list[Response]):
            self.session = TestTier15Routing._FakeSession(responses)

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *exc):
            return False

    def _spider_with_fake_sessions(self, responses_by_fp: dict[str, list[Response]]):
        """Patch news_spider.FetcherSession to return per-fingerprint fake sessions.

        Returns a context manager that yields ``managers`` (dict fp → fake
        session manager) on entry.
        """
        managers: dict[str, TestTier15Routing._FakeSessionManager] = {}

        def _factory(impersonate=None, **kwargs):
            fp = impersonate
            if fp not in managers:
                managers[fp] = self._FakeSessionManager(responses_by_fp.get(fp, []))
            return managers[fp]

        patcher = patch.object(ns, "FetcherSession", side_effect=_factory)
        return _FakeSessionsContext(patcher, managers)

    @pytest.mark.asyncio
    async def test_firefox135_first_then_safari(self):
        """firefox135 succeeds → safari15_5 never used."""
        ff_response = _make_response(
            "https://a.com/1", 200, "<html><body>firefox body</body></html>"
        )
        spider = NewsSpider(urls=["https://a.com/1"], timeout_ms=15000)
        result = SpiderResult(
            url="https://a.com/1",
            status=403,
            error="Tier 1 blocked, queued for retry",
            html_content="",
        )

        with self._spider_with_fake_sessions(
            {"firefox135": [ff_response], "safari15_5": []}
        ) as managers:
            holder = [result]
            remaining = await spider._retry_with_alt_fingerprints(holder, [(0, result)])

        assert remaining == []
        assert holder[0].fetch_tier == "1.5"
        assert holder[0].html_content == "<html><body>firefox body</body></html>"
        assert holder[0].used_stealth is False
        # safari15_5 session was never created/used
        assert "safari15_5" not in managers or managers["safari15_5"].session.calls == []

    @pytest.mark.asyncio
    async def test_blocked_firefox_falls_through_to_safari(self):
        """firefox135 blocked → cookies invalidated → safari15_5 retried."""
        ff_blocked = _make_response(
            "https://a.com/1",
            403,
            "<html><title>Just a moment...</title></html>",
        )
        safari_ok = _make_response(
            "https://a.com/1", 200, "<html><body>safari body</body></html>"
        )
        spider = NewsSpider(urls=["https://a.com/1"], timeout_ms=15000)
        result = SpiderResult(
            url="https://a.com/1",
            status=403,
            error="Tier 1 blocked, queued for retry",
            html_content="",
        )
        # Pre-seed a stale cookie for firefox135 → should be invalidated on block
        await ns.pool_put("a.com", "firefox135", {"cf_clearance": "stale"})

        with self._spider_with_fake_sessions(
            {"firefox135": [ff_blocked], "safari15_5": [safari_ok]}
        ) as managers:
            holder = [result]
            remaining = await spider._retry_with_alt_fingerprints(holder, [(0, result)])

        assert remaining == []
        assert holder[0].fetch_tier == "1.5"
        assert holder[0].html_content == "<html><body>safari body</body></html>"
        # stale firefox135 entry invalidated after the block
        assert ("a.com", "firefox135") not in ns._domain_cookie_pool
        # firefox135 injected the stale cookie before being blocked
        ff_calls = managers["firefox135"].session.calls
        assert ff_calls and ff_calls[0][1].get("cookies") == {"cf_clearance": "stale"}
        # safari15_5 had no pool entry → no cookies injected
        safari_calls = managers["safari15_5"].session.calls
        assert safari_calls and safari_calls[0][1].get("cookies") is None

    @pytest.mark.asyncio
    async def test_both_fingerprints_fail_returns_remaining(self):
        """Both fingerprints blocked → returns the pair for Tier 2."""
        ff_blocked = _make_response(
            "https://a.com/1",
            503,
            "<html><title>Just a moment...</title></html>",
        )
        safari_blocked = _make_response(
            "https://a.com/1",
            403,
            '<html><div id="challenge-platform"></div></html>',
        )
        spider = NewsSpider(urls=["https://a.com/1"], timeout_ms=15000)
        result = SpiderResult(
            url="https://a.com/1",
            status=403,
            error="Tier 1 blocked, queued for retry",
            html_content="",
        )

        with self._spider_with_fake_sessions(
            {"firefox135": [ff_blocked], "safari15_5": [safari_blocked]}
        ):
            holder = [result]
            remaining = await spider._retry_with_alt_fingerprints(holder, [(0, result)])

        assert len(remaining) == 1
        assert remaining[0][1] is result
        assert holder[0].fetch_tier == "1"  # unchanged (still the Tier 1 failure)

    @pytest.mark.asyncio
    async def test_connection_error_kept_in_remaining(self):
        """Connection-level failure at Tier 1.5 → kept for Tier 2."""
        spider = NewsSpider(urls=["https://a.com/1"], timeout_ms=15000)
        result = SpiderResult(
            url="https://a.com/1",
            status=0,
            error="No active session available.",
            html_content="",
        )
        with self._spider_with_fake_sessions({"firefox135": [], "safari15_5": []}):
            holder = [result]
            remaining = await spider._retry_with_alt_fingerprints(holder, [(0, result)])
        assert len(remaining) == 1

    @pytest.mark.asyncio
    async def test_pool_cookie_injected_and_served(self):
        """A valid pool entry is injected into the Tier 1.5 request."""
        await ns.pool_put("a.com", "firefox135", {"cf_clearance": "fresh"})
        ok = _make_response("https://a.com/1", 200, "<html>ok</html>")
        spider = NewsSpider(urls=["https://a.com/1"], timeout_ms=15000)
        result = SpiderResult(
            url="https://a.com/1", status=403, error="blocked", html_content=""
        )
        with self._spider_with_fake_sessions({"firefox135": [ok]}):
            holder = [result]
            remaining = await spider._retry_with_alt_fingerprints(holder, [(0, result)])
        assert remaining == []
        assert holder[0].fetch_tier == "1.5"
