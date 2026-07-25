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

    def test_has_cloudflare_challenge_with_cloudflare_keyword(self):
        spider = NewsSpider(urls=[])
        html = "<html><body>Cloudflare challenge</body></html>"
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_cf_challenge_class(self):
        spider = NewsSpider(urls=[])
        html = '<html><div class="cf-challenge"></div></html>'
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_challenge_platform(self):
        spider = NewsSpider(urls=[])
        html = '<html><div id="challenge-platform"></div></html>'
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_with_turnstile(self):
        spider = NewsSpider(urls=[])
        html = '<html><div class="cf-turnstile"></div></html>'
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_without_markers(self):
        spider = NewsSpider(urls=[])
        html = "<html><body>403 Forbidden</body></html>"
        assert spider._has_cloudflare_challenge(html) is False

    def test_has_cloudflare_challenge_case_insensitive(self):
        spider = NewsSpider(urls=[])
        html = "<html><title>CLOUDFLARE CHALLENGE</title></html>"
        assert spider._has_cloudflare_challenge(html) is True

    def test_has_cloudflare_challenge_empty_html(self):
        spider = NewsSpider(urls=[])
        html = ""
        assert spider._has_cloudflare_challenge(html) is False


class TestIsBlocked:
    """Tests for is_blocked() async method."""

    @pytest.mark.asyncio
    async def test_is_blocked_with_cloudflare(self):
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 403,
            "<html><title>Just a moment...</title></html>",
        )
        assert await spider.is_blocked(response) is True

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
        spider = NewsSpider(urls=[])
        response = _make_response(
            "https://example.com", 429,
            "<html><title>Just a moment...</title></html>",
        )
        assert await spider.is_blocked(response) is True

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
