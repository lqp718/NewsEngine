"""LLM Preprocessing Layer — compress episode content before JSON write.

Design: docs/design-llm-preprocessing-layer.md

Context: Graphiti's entity resolution step (``_resolve_with_llm``) concatenates
the current episode + the 10 most recent previous episodes into a single prompt.
Long episodes (>32K tokens) overflow the local LLM context and fail ingestion.

This module adds an optional LLM preprocessing step between the adapter's
content fetch and the JSON write:

- **compress** mode: fetched article text longer than ``compress_threshold``
  chars is summarized down to ~``compress_target`` chars while preserving
  entities, numbers, dates, and causal claims. Used by both GDELT and RSS.

Graceful degradation: on any LLM failure (timeout, HTTP error, malformed
response, empty output) ``preprocess()`` returns the original content
unchanged and logs a warning. The ingestion pipeline is never blocked by a
preprocessor failure.

v2 hardening:

- **Deterministic output**: ``temperature=0`` so the same content compresses
  to the same summary (stable ``content_hash``).
- **Structured output**: the LLM is forced to emit JSON via
  ``response_format: json_schema``; parsing is deterministic instead of
  relying on free-text length instructions.
- **Circuit breaker**: after ``_CIRCUIT_BREAKER_THRESHOLD`` consecutive
  failures the LLM is skipped for ``_CIRCUIT_BREAKER_COOLDOWN_SECS`` so a
  downed endpoint doesn't serialize N×timeout stalls.
- **Traceability**: ``preprocess()`` writes a truncated SHA-256 of the
  original content into ``metadata["original_content_hash"]`` so the
  pre-compression source text can be traced after it is not persisted.

The LLM is called over the OpenAI-compatible chat completions API
(reuses the existing llama-server / Gemma 4 12B deployment) via ``httpx`` —
no new dependencies.

Opt-in: adapters only instantiate ``LLMPreprocessor`` when
``settings.llm_preprocessor_enabled`` is True (default False).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# Lower bound below which compression is pointless (design §2.3).
_MIN_COMPRESS_CHARS = 500

# Circuit breaker: after this many consecutive LLM failures the endpoint is
# considered down and skipped for the cooldown window (design §2.4).
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SECS = 60.0

# JSON schemas for structured output (``response_format: json_schema``).
COMPRESS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Compressed summary preserving key facts"},
        "key_entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names mentioned"},
        "key_numbers": {"type": "array", "items": {"type": "string"}, "description": "Numbers/dates preserved"},
        "core_claim": {"type": "string", "description": "One-sentence core fact"},
    },
    "required": ["summary"],
}

COMPRESS_PROMPT = """You are a news content compressor. Given the following article, \
produce a concise summary that preserves:

1. All entity names (people, organizations, countries, companies)
2. All numbers, dates, and quantitative data
3. Key factual claims and causal relationships
4. The core event/action described

IMPORTANT: Maintain factual accuracy — do not infer or hallucinate. \
Preserve direct quotes when they contain critical information.

---
ARTICLE:
{content}
---"""


class LLMPreprocessor:
    """LLM-based episode content preprocessor (opt-in preprocessing layer).

    Args:
        endpoint: OpenAI-compatible base URL, e.g. ``http://192.168.0.27:8080/v1``.
        model: Model name served at the endpoint (e.g. ``gemma-4-12b``).
        api_key: API key; llama-server accepts any non-empty value.
        compress_threshold: Content length (chars) above which compress mode
            is applied. Content at or below this length is passed through.
        compress_target: Target length (chars) for compressed summaries.
        timeout: Per-request timeout (seconds).
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "local",
        compress_threshold: int = 5000,
        compress_target: int = 2500,
        timeout: float = 30.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._compress_threshold = compress_threshold
        self._compress_target = compress_target
        self._timeout = timeout
        # Circuit breaker state (design §2.4).
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    # ── public API ───────────────────────────────────────────────────

    @property
    def compress_threshold(self) -> int:
        """Content length (chars) above which compress mode engages."""
        return self._compress_threshold

    async def preprocess(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Preprocess episode content before it is written to JSON.

        Args:
            content: Original content (full article text).
            metadata: Episode metadata. When not ``None``, a truncated SHA-256 of
                ``content`` is written back under ``original_content_hash``.

        Returns:
            Preprocessed content, or the original ``content`` unchanged when:
            - content is empty,
            - the circuit breaker is open (endpoint recently down),
            - content is short (< ``compress_threshold``, including the < 500 char floor),
            - the LLM call fails or returns no usable summary
              (graceful degradation).
        """
        if not content or not content.strip():
            return content

        # Circuit breaker: if the endpoint has been failing, skip the LLM
        # entirely and return the original content (design §2.4).
        if time.monotonic() < self._circuit_open_until:
            logger.debug("LLMPreprocessor: circuit breaker open, skipping LLM")
            return content

        # Below threshold (including the no-compress floor) → pass through.
        if len(content) < max(self._compress_threshold, _MIN_COMPRESS_CHARS):
            return content
        prompt = COMPRESS_PROMPT.format(content=content)
        schema = COMPRESS_SCHEMA
        target = self._compress_target

        # Traceability: record a truncated hash of the original content so
        # the pre-compression source text can be identified downstream.
        if metadata is not None:
            metadata["original_content_hash"] = hashlib.sha256(
                content.encode()
            ).hexdigest()[:16]

        result = await self._call_llm(prompt, schema)
        if result is None:
            # Graceful degradation: LLM unavailable — keep original content.
            return content

        summary = result.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            logger.warning(
                "LLMPreprocessor: LLM returned an empty summary — "
                "falling back to original content"
            )
            return content

        # Hard guard: if the model ignored the length instruction and
        # returned something no shorter than the input (compress) or far
        # beyond the target, fall back to original content so we never
        # make the overflow problem worse.
        if mode == "compress" and len(summary) >= len(content):
            logger.warning(
                "LLMPreprocessor: compress output not shorter than input "
                "(%d >= %d chars) — falling back to original content",
                len(summary),
                len(content),
            )
            return content
        return summary

    # ── LLM call ─────────────────────────────────────────────────────

    async def _call_llm(
        self, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Call the OpenAI-compatible chat completions endpoint.

        Forces structured JSON output via ``response_format: json_schema``
        and parses the assistant's ``content`` as JSON.

        Returns the parsed response object (a dict), or ``None`` on any
        failure (network error, timeout, non-200 status, malformed/invalid
        JSON response). Never raises. Updates circuit-breaker state.
        """
        url = f"{self._endpoint}/chat/completions"
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "llm_preprocessor_output",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            text = data["choices"][0]["message"]["content"]
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("LLM response is not a JSON object")
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                self._circuit_open_until = (
                    time.monotonic() + _CIRCUIT_BREAKER_COOLDOWN_SECS
                )
                logger.warning(
                    "LLMPreprocessor: circuit breaker opened "
                    "(%d failures, cooldown %.0fs)",
                    self._consecutive_failures,
                    _CIRCUIT_BREAKER_COOLDOWN_SECS,
                )
            logger.warning(
                "LLMPreprocessor: LLM call failed (%s: %s) — falling back",
                type(exc).__name__,
                exc,
            )
            return None

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        return result


__all__ = [
    "LLMPreprocessor",
    "COMPRESS_PROMPT",
    "COMPRESS_SCHEMA",
]
