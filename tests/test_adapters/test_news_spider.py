"""Unit tests for NewsSpider Cloudflare detection and is_blocked logic.

Part of newsengine-spider-oom-fix change.
"""

import pytest
from scrapling.spiders import Response

from src.utils.news_spider import NewsSpider


def _make_response(url: str, status: int, html_content: str) -> Response:
    """Helper to build a minimal scrapling Response for testing."""
    return Response(
        url=url,
        content=html_content.encode("utf-8"),
        status=status,
        reason="OK" if status == 200 else "Forbidden",
        cookies={},
        headers={},
        request_headers={},
    )


class TestCloudflareDetection:
    """Tests for _has_cloudflare_challenge() helper."""

    def test_has_cloudflare_challenge_with_just_a_moment(self):
        spider = NewsSpider(urls=[])
        html = "<html><title>Just a moment...</title></html>"
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_challenge_platform(self):
        spider = NewsSpider(urls=[])
        html = '<html><div id="challenge-platform"></div></html>'
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_turnstile(self):
        spider = NewsSpider(urls=[])
        html = '<html><div class="cf-turnstile"></div></html>'
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_cdn_cgi(self):
        spider = NewsSpider(urls=[])
        html = '<html><script src="cdn-cgi/challenge-platform/h/b/cf-challenge.js"></script></html>'
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_attention_required(self):
        spider = NewsSpider(urls=[])
        html = "<html><title>Attention Required</title></html>"
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_security_check(self):
        spider = NewsSpider(urls=[])
        html = "<html><title>Security Check</title></html>"
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_without_markers(self):
        spider = NewsSpider(urls=[])
        html = "<html><body>403 Forbidden</body></html>"
        assert spider._has_cloudflare_challenge(html) is False

    def test_has_cloudflare_challenge_empty_html(self):
        spider = NewsSpider(urls=[])
        html = ""
        assert spider._has_cloudflare_challenge(html) is False

    def test_has_cloudflare_challenge_large_page_skipped(self):
        """Pages > 15KB are never challenge pages."""
        spider = NewsSpider(urls=[])
        html = "<html><title>Just a moment...</title>" + "x" * 20000 + "</html>"
        assert spider._has_cloudflare_challenge(html) is False

    def test_has_cloudflare_challenge_normal_page_with_cloudflare_mention(self):
        """Normal pages mentioning 'cloudflare' in content are not challenge pages."""
        spider = NewsSpider(urls=[])
        html = "<html><body>This site is powered by Cloudflare CDN</body></html>"
        assert spider._has_cloudflare_challenge(html) is False


class TestIsBlocked:
    """Tests for is_blocked() async method.

    V5.0: is_blocked() always returns False — blocked detection is handled
    in parse() callback via _is_blocked_response().
    """

    @pytest.mark.asyncio
    async def test_is_blocked_always_false(self):
        """is_blocked() always returns False (V5.0 design)."""
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 403,
            "<html><title>Just a moment...</title></html>",
        )
        assert await spider.is_blocked(response) is False

    @pytest.mark.asyncio
    async def test_is_blocked_without_cloudflare(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 403,
            "<html><body>403 Forbidden</body></html>",
        )
        assert await spider.is_blocked(response) is False

    @pytest.mark.asyncio
    async def test_is_blocked_with_200(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 200,
            "<html><body>OK</body></html>",
        )
        assert await spider.is_blocked(response) is False

    @pytest.mark.asyncio
    async def test_is_blocked_429_with_cloudflare(self):
        """Even 429 with CF markers returns False from is_blocked() (V5.0)."""
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 429,
            "<html><title>Just a moment...</title></html>",
        )
        assert await spider.is_blocked(response) is False

    @pytest.mark.asyncio
    async def test_is_blocked_503_without_cloudflare(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 503,
            "<html><body>Service Unavailable</body></html>",
        )
        assert await spider.is_blocked(response) is False

    @pytest.mark.asyncio
    async def test_is_blocked_with_empty_html(self):
        spider = NewsSpider(urls=[])
        response = _make_response("https://example.com", 403, "")
        assert await spider.is_blocked(response) is False


class TestIsBlockedResponse:
    """Tests for _is_blocked_response() — the internal blocked detection."""

    def test_blocked_with_cloudflare_challenge(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 403,
            "<html><title>Just a moment...</title></html>",
        )
        assert spider._is_blocked_response(response) is True

    def test_not_blocked_without_cloudflare(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 403,
            "<html><body>403 Forbidden</body></html>",
        )
        assert spider._is_blocked_response(response) is False

    def test_not_blocked_with_200(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 200,
            "<html><body>OK</body></html>",
        )
        assert spider._is_blocked_response(response) is False

    def test_blocked_429_with_cloudflare(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 429,
            "<html><title>Just a moment...</title></html>",
        )
        assert spider._is_blocked_response(response) is True

    def test_blocked_503_with_cloudflare(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 503,
            "<html><title>Just a moment...</title></html>",
        )
        assert spider._is_blocked_response(response) is True

    def test_blocked_status_no_html(self):
        """Blocked status with minimal HTML → not treated as blocked (no CF markers)."""
        spider = NewsSpider(urls=[])
        response = _make_response("https://example.com", 403, "")
        # Empty HTML gets parsed to <html></html> by scrapling, which has no CF markers
        assert spider._is_blocked_response(response) is False
