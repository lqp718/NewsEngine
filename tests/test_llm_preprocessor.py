"""Unit tests for the LLM preprocessing layer (src/adapters/llm_preprocessor.py).

All tests mock the underlying httpx call — no real LLM/HTTP requests.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.adapters.llm_preprocessor import (
    COMPRESS_PROMPT,
    COMPRESS_SCHEMA,
    SYNTHESIZE_PROMPT,
    SYNTHESIZE_SCHEMA,
    LLMPreprocessor,
)


def make_preprocessor(**kwargs) -> LLMPreprocessor:
    defaults = dict(
        endpoint="http://192.168.0.27:8080/v1",
        model="gemma-4-12b",
        api_key="local",
        compress_threshold=5000,
        compress_target=2500,
        synthesize_target=1500,
        timeout=30.0,
    )
    defaults.update(kwargs)
    return LLMPreprocessor(**defaults)


LONG_TEXT = "The European Central Bank raised interest rates by 50 basis points. " * 100
SHORT_TEXT = "A short article."


# ── httpx mock helpers ──────────────────────────────────────────────


def _patch_async_client(post_result) -> MagicMock:
    """Patch httpx.AsyncClient to return ``post_result`` from .post().

    ``_call_llm`` uses ``async with httpx.AsyncClient(...) as client:`` so we
    wire up the async-context-manager protocol on the mock instance.
    """
    client = AsyncMock()
    client.post = post_result
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    return mock_cls


def _raw_response(content: str) -> AsyncMock:
    """Return a 200 response whose assistant ``content`` is the raw string."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return AsyncMock(return_value=resp)


def _ok_response(payload: dict) -> AsyncMock:
    """Return a 200 response whose assistant ``content`` is ``json.dumps(payload)``."""
    return _raw_response(json.dumps(payload))


# ── compress mode (preprocess logic, _call_llm mocked) ──────────────


class TestCompressMode:
    @pytest.mark.asyncio
    async def test_compress_calls_llm_and_returns_summary(self):
        pp = make_preprocessor()
        compressed = "ECB raised rates by 50bp, citing persistent inflation."

        with patch.object(
            pp, "_call_llm", AsyncMock(return_value={"summary": compressed})
        ) as mock_call:
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == compressed
        mock_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compress_below_threshold_skips_llm(self):
        """len(content) < threshold → no LLM call, original returned."""
        pp = make_preprocessor(compress_threshold=5000)

        with patch.object(pp, "_call_llm", AsyncMock()) as mock_call:
            result = await pp.preprocess(SHORT_TEXT, mode="compress")

        assert result == SHORT_TEXT
        mock_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_compress_very_short_content_skips_llm(self):
        """Content below the 500-char floor never triggers compression."""
        pp = make_preprocessor(compress_threshold=10)

        with patch.object(pp, "_call_llm", AsyncMock()) as mock_call:
            result = await pp.preprocess(SHORT_TEXT, mode="compress")

        assert result == SHORT_TEXT
        mock_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_compress_output_not_shorter_falls_back(self):
        """If the model returns something no shorter, keep original."""
        pp = make_preprocessor()

        with patch.object(
            pp, "_call_llm", AsyncMock(return_value={"summary": LONG_TEXT + " MORE"})
        ):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == LONG_TEXT


# ── synthesize mode ─────────────────────────────────────────────────


class TestSynthesizeMode:
    @pytest.mark.asyncio
    async def test_synthesize_returns_result(self):
        pp = make_preprocessor()
        summary = "China issued a cooperative statement toward the United States."
        template = "## GDELT Events Report\n**Event**: MAKE PUBLIC STATEMENT (CAMEO 01)"

        with patch.object(pp, "_call_llm", AsyncMock(return_value={"summary": summary})):
            result = await pp.preprocess(
                template,
                metadata={"actor1": "China", "actor2": "United States"},
                mode="synthesize",
            )

        assert result == summary

    @pytest.mark.asyncio
    async def test_synthesize_output_too_long_falls_back(self):
        pp = make_preprocessor(synthesize_target=1500)
        oversized = "word " * 5000  # way beyond 2x target
        template = "## GDELT Events Report"

        with patch.object(
            pp, "_call_llm", AsyncMock(return_value={"summary": oversized})
        ):
            result = await pp.preprocess(template, metadata={}, mode="synthesize")

        assert result == template

    @pytest.mark.asyncio
    async def test_synthesize_none_result_falls_back(self):
        pp = make_preprocessor()
        template = "## GDELT Events Report"

        with patch.object(pp, "_call_llm", AsyncMock(return_value=None)):
            result = await pp.preprocess(template, metadata={}, mode="synthesize")

        assert result == template


# ── error handling / graceful degradation ───────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_llm_none_falls_back_to_original(self):
        pp = make_preprocessor()

        with patch.object(pp, "_call_llm", AsyncMock(return_value=None)):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == LONG_TEXT

    @pytest.mark.asyncio
    async def test_unknown_mode_returns_original(self):
        pp = make_preprocessor()
        result = await pp.preprocess(LONG_TEXT, mode="bogus")  # type: ignore[arg-type]
        assert result == LONG_TEXT

    @pytest.mark.asyncio
    async def test_empty_content_returns_original(self):
        pp = make_preprocessor()
        assert await pp.preprocess("", mode="compress") == ""
        assert await pp.preprocess("   ", mode="synthesize") == "   "


# ── circuit breaker ─────────────────────────────────────────────────


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_three_failures_open_circuit(self):
        """3 consecutive failures open the breaker; the 4th call skips the LLM."""
        pp = make_preprocessor()
        post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            for _ in range(3):
                result = await pp.preprocess(LONG_TEXT, mode="compress")
                assert result == LONG_TEXT

            assert post.await_count == 3
            assert pp._consecutive_failures == 3
            assert pp._circuit_open_until > time.monotonic()

            # 4th call: circuit open → return original, no additional HTTP call
            result = await pp.preprocess(LONG_TEXT, mode="compress")
            assert result == LONG_TEXT
            assert post.await_count == 3

    @pytest.mark.asyncio
    async def test_circuit_recovers_after_cooldown(self):
        """After the cooldown window elapses, LLM calls resume and reset state."""
        pp = make_preprocessor()
        post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            for _ in range(3):
                await pp.preprocess(LONG_TEXT, mode="compress")
            assert pp._circuit_open_until > time.monotonic()

        # Simulate the cooldown window elapsing.
        pp._circuit_open_until = 0.0

        ok_post = _ok_response({"summary": "ECB raised rates by 50bp."})
        with patch("httpx.AsyncClient", _patch_async_client(ok_post)):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == "ECB raised rates by 50bp."
        assert pp._consecutive_failures == 0
        assert pp._circuit_open_until == 0.0


# ── JSON schema structured output ───────────────────────────────────


class TestJsonSchemaOutput:
    @pytest.mark.asyncio
    async def test_valid_json_response_extracts_summary(self):
        pp = make_preprocessor()
        post = _ok_response({"summary": "ECB raised rates.", "key_entities": ["ECB"]})

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == "ECB raised rates."

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back(self):
        pp = make_preprocessor()
        post = _raw_response("this is not valid json")

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == LONG_TEXT

    @pytest.mark.asyncio
    async def test_empty_summary_falls_back(self):
        pp = make_preprocessor()
        post = _ok_response({"summary": ""})

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == LONG_TEXT


# ── original content hash in metadata ───────────────────────────────


class TestOriginalContentHash:
    @pytest.mark.asyncio
    async def test_metadata_gets_original_content_hash(self):
        pp = make_preprocessor()
        metadata = {"source_url": "http://example.com/a", "valid_at": "20250101"}

        with patch.object(
            pp, "_call_llm", AsyncMock(return_value={"summary": "compressed"})
        ):
            await pp.preprocess(LONG_TEXT, metadata=metadata, mode="compress")

        assert "original_content_hash" in metadata
        assert isinstance(metadata["original_content_hash"], str)
        assert len(metadata["original_content_hash"]) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", metadata["original_content_hash"])

    @pytest.mark.asyncio
    async def test_original_content_hash_matches_sha256(self):
        pp = make_preprocessor()
        metadata: dict = {}

        with patch.object(
            pp, "_call_llm", AsyncMock(return_value={"summary": "compressed"})
        ):
            await pp.preprocess(LONG_TEXT, metadata=metadata, mode="compress")

        expected = hashlib.sha256(LONG_TEXT.encode()).hexdigest()[:16]
        assert metadata["original_content_hash"] == expected

    @pytest.mark.asyncio
    async def test_none_metadata_does_not_crash(self):
        pp = make_preprocessor()

        with patch.object(
            pp, "_call_llm", AsyncMock(return_value={"summary": "compressed"})
        ):
            result = await pp.preprocess(LONG_TEXT, metadata=None, mode="compress")

        assert result == "compressed"


# ── _call_llm HTTP layer (httpx mocked) ─────────────────────────────


class TestCallLLM:
    @pytest.mark.asyncio
    async def test_call_llm_returns_dict_and_prompt_construction(self):
        pp = make_preprocessor()
        post = _ok_response({"summary": "ECB raised rates by 50bp."})

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp._call_llm("PROMPT-BODY", COMPRESS_SCHEMA)

        assert result == {"summary": "ECB raised rates by 50bp."}
        # Verify payload construction (endpoint, model, prompt)
        _, kwargs = post.await_args
        assert post.await_args.args[0].endswith("/chat/completions")
        assert kwargs["json"]["model"] == "gemma-4-12b"
        assert kwargs["json"]["messages"][0]["content"] == "PROMPT-BODY"

    @pytest.mark.asyncio
    async def test_call_llm_temperature_is_zero(self):
        pp = make_preprocessor()
        post = _ok_response({"summary": "ok"})

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            await pp._call_llm("PROMPT", COMPRESS_SCHEMA)

        _, kwargs = post.await_args
        assert kwargs["json"]["temperature"] == 0

    @pytest.mark.asyncio
    async def test_call_llm_response_format_json_schema(self):
        pp = make_preprocessor()
        post = _ok_response({"summary": "ok"})

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            await pp._call_llm("PROMPT", SYNTHESIZE_SCHEMA)

        _, kwargs = post.await_args
        rf = kwargs["json"]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "llm_preprocessor_output"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["schema"] == SYNTHESIZE_SCHEMA

    @pytest.mark.asyncio
    async def test_call_llm_timeout_returns_none(self):
        pp = make_preprocessor(timeout=30.0)
        post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp._call_llm("PROMPT", COMPRESS_SCHEMA)

        assert result is None

    @pytest.mark.asyncio
    async def test_call_llm_http_error_returns_none(self):
        pp = make_preprocessor()
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        post = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp._call_llm("PROMPT", COMPRESS_SCHEMA)

        assert result is None

    @pytest.mark.asyncio
    async def test_call_llm_malformed_response_returns_none(self):
        pp = make_preprocessor()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"unexpected": "shape"}  # no "choices"
        post = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp._call_llm("PROMPT", COMPRESS_SCHEMA)

        assert result is None

    @pytest.mark.asyncio
    async def test_call_llm_empty_response_returns_none(self):
        pp = make_preprocessor()
        post = _raw_response("   ")

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp._call_llm("PROMPT", COMPRESS_SCHEMA)

        assert result is None


# ── exception fallback (non-HTTPError must not penetrate) ───────────


class TestExceptionFallback:
    @pytest.mark.asyncio
    async def test_value_error_falls_back(self):
        """A non-HTTPError exception (e.g. httpx.InvalidURL) must not penetrate."""
        pp = make_preprocessor()
        post = AsyncMock(side_effect=ValueError("bad url"))

        with patch("httpx.AsyncClient", _patch_async_client(post)):
            result = await pp.preprocess(LONG_TEXT, mode="compress")

        assert result == LONG_TEXT


# ── prompt construction ─────────────────────────────────────────────


class TestPromptConstruction:
    def test_compress_prompt_includes_content(self):
        prompt = COMPRESS_PROMPT.format(content="ARTICLE-BODY")
        assert "ARTICLE-BODY" in prompt

    def test_synthesize_prompt_includes_metadata_and_content(self):
        metadata = '{"actor1": "China", "actor2": "United States"}'
        prompt = SYNTHESIZE_PROMPT.format(
            metadata=metadata, content="TEMPLATE-BODY"
        )
        assert "China" in prompt
        assert "United States" in prompt
        assert "TEMPLATE-BODY" in prompt


# ── threshold property ──────────────────────────────────────────────


class TestThreshold:
    def test_compress_threshold_property(self):
        assert make_preprocessor(compress_threshold=7000).compress_threshold == 7000
