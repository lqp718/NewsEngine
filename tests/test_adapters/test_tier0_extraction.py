"""Unit tests for Tier 0 static extraction (scraping-pipeline-optimization).

Covers:
- ``extract_next_data``: Next.js fixture hit, parse-failure fallback,
  chompjs-missing silent skip, quality-threshold fallback
- ``extract_json_ld``: articleBody hit, multiple blocks, @graph nesting,
  description fallback, below-threshold fallback
- ``ContentFetcher._spider_result_to_content``: tier0 engine marking and
  Trafilatura short-circuit (1.3)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.utils.content_fetcher import (
    TIER0_MIN_TEXT_LEN,
    ContentFetcher,
    extract_json_ld,
    extract_next_data,
)
from src.utils.news_spider import SpiderResult

# ── Fixtures ───────────────────────────────────────────────────────────

NEXT_DATA_JSON = json.dumps(
    {
        "props": {
            "pageProps": {
                "article": {
                    "title": "Test Article",
                    "body": "This is the article body text from a Next.js page. " * 12,
                    "meta": {"author": "Jane Doe"},
                }
            }
        }
    }
)

NEXT_DATA_HTML = (
    '<html><head>\n'
    '<script id="__NEXT_DATA__" type="application/json">'
    + NEXT_DATA_JSON
    + '</script>\n'
    '</head><body><h1>Fallback title</h1><p>Fallback body</p></body></html>'
)

JSONLD_HTML = (
    '<html><head>\n'
    '<script type="application/ld+json">'
    + json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "Test Headline",
            "articleBody": "This is the JSON-LD article body. " * 12,
        }
    )
    + '</script>\n'
    '</head><body><p>Fallback body</p></body></html>'
)

SHORT_BODY = "x" * (TIER0_MIN_TEXT_LEN - 10)


def _make_spider_result(html: str) -> SpiderResult:
    return SpiderResult(url="https://example.com/article", status=200, html_content=html)


# ── extract_next_data ──────────────────────────────────────────────────


class TestExtractNextData:
    def test_next_data_hit_prefers_article_body(self):
        """Next.js fixture: article.body under props.pageProps is extracted."""
        text = extract_next_data(NEXT_DATA_HTML)
        assert text is not None
        assert "article body text from a Next.js page" in text
        assert len(text) >= TIER0_MIN_TEXT_LEN

    def test_no_next_data_script_returns_none(self):
        assert extract_next_data("<html><body>plain</body></html>") is None

    def test_empty_html_returns_none(self):
        assert extract_next_data("") is None

    def test_broken_json_returns_none(self):
        html = '<script id="__NEXT_DATA__">{"props": broken</script>'
        assert extract_next_data(html) is None

    def test_broken_json_with_chompjs_unavailable_returns_none(self):
        """json.loads fails AND chompjs not installed → silent skip, no ImportError."""
        html = '<script id="__NEXT_DATA__">{"props": broken</script>'
        with patch.dict("sys.modules", {"chompjs": None}):
            # Simulate chompjs being unavailable at import time
            import importlib

            with patch.object(importlib, "import_module", side_effect=ImportError):
                assert extract_next_data(html) is None

    def test_broken_json_with_chompjs_success(self):
        """json.loads fails but chompjs parses the JS object → text found."""
        html = (
            '<script id="__NEXT_DATA__">{props:{article:{body:"'
            + "chompjs body text. " * 12
            + '"}}}</script>'
        )

        class _FakeChompjs:
            @staticmethod
            def parse_js_object(raw):
                return {
                    "props": {
                        "article": {"body": "chompjs body text. " * 12},
                    }
                }

        with patch.dict("sys.modules", {"chompjs": _FakeChompjs}):
            text = extract_next_data(html)
        assert text is not None
        assert "chompjs body text" in text

    def test_below_threshold_returns_none(self):
        """Body shorter than TIER0_MIN_TEXT_LEN is rejected."""
        html = f'<script id="__NEXT_DATA__">{{"props":{{"article":{{"body":"{SHORT_BODY}"}}}}}}</script>'
        assert extract_next_data(html) is None

    def test_html_body_candidate_is_stripped(self):
        """bodyHtml candidates with markup are cleaned before returning."""
        body = (
            "<p>Paragraph one.</p><p>Paragraph two.</p>"
            + ("<p>More article text here. " * 10)
            + "</p>"
        )
        payload = json.dumps(
            {"props": {"article": {"bodyHtml": body}}}
        )
        html = (
            '<script id="__NEXT_DATA__">'
            + payload
            + "</script>"
        )
        text = extract_next_data(html)
        assert text is not None
        assert "<p>" not in text
        assert "Paragraph one." in text
        assert len(text) >= TIER0_MIN_TEXT_LEN


# ── extract_json_ld ────────────────────────────────────────────────────


class TestExtractJsonLd:
    def test_jsonld_article_body_hit(self):
        text = extract_json_ld(JSONLD_HTML)
        assert text is not None
        assert "JSON-LD article body" in text
        assert len(text) >= TIER0_MIN_TEXT_LEN

    def test_no_jsonld_returns_none(self):
        assert extract_json_ld("<html><body>plain</body></html>") is None

    def test_multiple_blocks_picks_first_qualifying(self):
        """First block is not an article, second one qualifies."""
        html = (
            '<html><head>\n'
            '<script type="application/ld+json">'
            + json.dumps({"@type": "WebSite", "name": "Example"})
            + '</script>\n'
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "BlogPosting",
                    "headline": "Post",
                    "articleBody": "Blog posting body text. " * 12,
                }
            )
            + '</script>\n</head></html>'
        )
        text = extract_json_ld(html)
        assert text is not None
        assert "Blog posting body text" in text

    def test_graph_nesting_expanded(self):
        html = (
            '<html><head>\n'
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@context": "https://schema.org",
                    "@graph": [
                        {"@type": "Organization", "name": "Example Corp"},
                        {
                            "@type": "Article",
                            "headline": "Graph article",
                            "articleBody": "Graph nested body. " * 12,
                        },
                    ],
                }
            )
            + '</script>\n</head></html>'
        )
        text = extract_json_ld(html)
        assert text is not None
        assert "Graph nested body" in text

    def test_description_fallback(self):
        html = (
            '<html><head>\n'
            '<script type="application/ld+json">'
            + json.dumps(
                {
                    "@type": "NewsArticle",
                    "headline": "No body",
                    "description": "Long description used as fallback body. " * 10,
                }
            )
            + '</script>\n</head></html>'
        )
        text = extract_json_ld(html)
        assert text is not None
        assert "Long description used as fallback body" in text

    def test_below_threshold_returns_none(self):
        html = (
            '<script type="application/ld+json">'
            + json.dumps(
                {"@type": "NewsArticle", "articleBody": SHORT_BODY}
            )
            + "</script>"
        )
        assert extract_json_ld(html) is None

    def test_malformed_json_block_skipped(self):
        html = (
            '<html><head>\n'
            '<script type="application/ld+json">{not valid json</script>\n'
            '<script type="application/ld+json">'
            + json.dumps(
                {"@type": "Article", "articleBody": "Valid block body. " * 12}
            )
            + '</script>\n</head></html>'
        )
        text = extract_json_ld(html)
        assert text is not None
        assert "Valid block body" in text

    def test_empty_html_returns_none(self):
        assert extract_json_ld("") is None


# ── _spider_result_to_content integration (1.3) ────────────────────────


class TestTier0Integration:
    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_next_data_short_circuits_trafilatura(self, mock_extract):
        """Tier 0a hit → engine=tier0_next_data, Trafilatura NOT called."""
        fetcher = ContentFetcher()
        result = fetcher._spider_result_to_content(
            _make_spider_result(NEXT_DATA_HTML), "https://example.com/article"
        )
        assert result.success is True
        assert result.engine == "tier0_next_data"
        assert "article body text" in result.text
        mock_extract.assert_not_called()

    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_jsonld_short_circuits_trafilatura(self, mock_extract):
        """Tier 0b hit → engine=tier0_jsonld, Trafilatura NOT called."""
        fetcher = ContentFetcher()
        result = fetcher._spider_result_to_content(
            _make_spider_result(JSONLD_HTML), "https://example.com/article"
        )
        assert result.success is True
        assert result.engine == "tier0_jsonld"
        assert "JSON-LD article body" in result.text
        mock_extract.assert_not_called()

    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_below_threshold_falls_back_to_trafilatura(self, mock_extract):
        """Tier 0 candidates below threshold → Trafilatura is used."""
        mock_extract.return_value = "Trafilatura extracted body."
        html = (
            '<script id="__NEXT_DATA__">{"props":{"article":{"body":"'
            + SHORT_BODY
            + '"}}}</script>'
        )
        fetcher = ContentFetcher()
        result = fetcher._spider_result_to_content(
            _make_spider_result(html), "https://example.com/article"
        )
        assert result.success is True
        assert result.engine == "news_spider+trafilatura"
        assert result.text == "Trafilatura extracted body."
        mock_extract.assert_called_once()

    @patch("src.utils.content_fetcher.ContentFetcher._extract_content")
    def test_plain_html_uses_trafilatura(self, mock_extract):
        """No structured data → Trafilatura (existing behavior preserved)."""
        mock_extract.return_value = "Plain body."
        fetcher = ContentFetcher()
        result = fetcher._spider_result_to_content(
            _make_spider_result("<html><body>plain</body></html>"),
            "https://example.com/article",
        )
        assert result.success is True
        assert result.engine == "news_spider+trafilatura"
        mock_extract.assert_called_once()

    def test_tier0_applies_to_stealth_html(self):
        """Tier 0 works on HTML from any tier (e.g. CloakBrowser result)."""
        fetcher = ContentFetcher()
        spider_result = _make_spider_result(JSONLD_HTML)
        spider_result.used_stealth = True
        spider_result.fetch_tier = "2"
        result = fetcher._spider_result_to_content(
            spider_result, "https://example.com/article"
        )
        assert result.success is True
        assert result.engine == "tier0_jsonld"
